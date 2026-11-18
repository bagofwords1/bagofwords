import hashlib
import json
import logging
import os
import re
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from time import monotonic

import duckdb

from app.data_sources.clients.postgresql_multi_catalog import TENANT_COLUMN

DEFAULT_STATEMENT_TIMEOUT_MS = 300_000
DEFAULT_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 4
DEFAULT_PG_CONNECTION_LIMIT = 4
DEFAULT_PG_IDLE_TIMEOUT_MS = 30_000
DEGRADED_ENGINE_TTL_SECONDS = 30
EXTENSION_DIR_ENV = "BOW_DUCKDB_EXTENSION_DIR"

_SECRET_NAME = "bow_tenant_server"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PgConn:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    sslmode: str


class TenantEngine:
    def __init__(self, con: duckdb.DuckDBPyConnection, aliases: dict[str, str], failed_tenants: list[dict]):
        self._con = con
        self._aliases = aliases
        self._failed = {f["name"] for f in failed_tenants}
        self.failed_tenants = failed_tenants
        self.missing_views: list[str] = []
        self.left_out: list[dict] = []
        self._settled: dict[str, tuple[str, ...]] = {}
        self._reading: dict[str, tuple[str, ...]] = {}
        self._created: set[str] = set()
        self._lock = threading.Lock()
        self.cache_key = None

    @property
    def attached(self) -> bool:
        return bool(self._aliases)

    @property
    def degraded(self) -> bool:
        return bool(self.failed_tenants or self.missing_views or self.left_out)

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def close(self) -> None:
        self._con.close()

    def vanished_tables(self, view_names: list[str]) -> list[tuple[str, str]]:
        relations: dict[str, list[tuple[str, str]]] = {}
        for name in sorted(set(view_names)):
            for tenant in self._reading.get(name, ()):
                if tenant in self._aliases:
                    relations.setdefault(tenant, []).append((name, _qualified_pg_name(*_split_view_name(name))))
        vanished = []
        cur = self._con.cursor()
        try:
            for tenant, members in sorted(relations.items()):
                names = ", ".join(_pg_literal(qualified) for _, qualified in members)
                probe = f"SELECT n FROM unnest(ARRAY[{names}]::text[]) AS n WHERE to_regclass(n) IS NULL"
                try:
                    rows = cur.execute(
                        f"SELECT * FROM postgres_query({_sql_literal(self._aliases[tenant])}, {_sql_literal(probe)})"
                    ).fetchall()
                except duckdb.Error as exc:
                    logger.warning("Could not check tenant database %s for vanished tables: %s",
                                   tenant, failure_category(exc))
                    continue
                gone = {row[0] for row in rows}
                vanished.extend((name, tenant) for name, qualified in members if qualified in gone)
        finally:
            cur.close()
        return vanished

    def ensure_views(self, views: dict[str, list[str]]) -> None:
        wanted = {name: tuple(sorted(set(members))) for name, members in views.items()}
        if all(self._settled.get(name) == members for name, members in wanted.items()):
            return
        with self._lock:
            cur = self._con.cursor()
            try:
                for name, members in sorted(wanted.items()):
                    if self._settled.get(name) != members:
                        self._create_view(cur, name, members)
                        self._settled[name] = members
            finally:
                cur.close()

    def _forget(self, name: str) -> None:
        self.missing_views = [v for v in self.missing_views if v != name]
        self.left_out = [e for e in self.left_out if e["view"] != name]

    def _leave_out(self, name: str, tenant: str, reason: str) -> None:
        logger.warning("Tenant database %s left out of view %s: %s", tenant, name, reason)
        self.left_out.append({"view": name, "tenant": tenant, "reason": reason})

    def _create_view(self, cur: duckdb.DuckDBPyConnection, name: str, members: tuple[str, ...]) -> None:
        self._forget(name)
        self._reading.pop(name, None)
        schema, table = _split_view_name(name)
        readable = []
        for tenant in members:
            if tenant in self._failed:
                continue
            if tenant not in self._aliases:
                self._leave_out(name, tenant, "not attached")
                continue
            try:
                cur.execute(f"DESCRIBE {_table_ref(self._aliases[tenant], schema, table)}")
            except duckdb.Error as exc:
                self._leave_out(name, tenant, failure_category(exc))
            else:
                readable.append(tenant)
        if not readable:
            self._drop(cur, name)
            return
        union = " UNION ALL BY NAME ".join(
            f"SELECT {_sql_literal(t)} AS {TENANT_COLUMN}, * FROM {_table_ref(self._aliases[t], schema, table)}"
            for t in readable
        )
        try:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS memory.{_ident(schema)}")
            replace = "OR REPLACE " if name in self._created else ""
            cur.execute(f"CREATE {replace}VIEW memory.{_ident(schema)}.{_ident(table)} AS {union}")
            self._created.add(name)
            self._reading[name] = tuple(readable)
        except duckdb.Error as exc:
            logger.warning("Could not create view %s: %s", name, failure_category(exc))
            self._drop(cur, name)

    def _drop(self, cur: duckdb.DuckDBPyConnection, name: str) -> None:
        self.missing_views.append(name)
        if name in self._created:
            schema, table = _split_view_name(name)
            cur.execute(f"DROP VIEW IF EXISTS memory.{_ident(schema)}.{_ident(table)}")
            self._created.discard(name)


_FAILURE_CATEGORIES = (
    ("too many clients", "server has no free connection slots"),
    ("too many connections", "server has no free connection slots"),
    ("remaining connection slots", "server has no free connection slots"),
    ("password authentication failed", "authentication failed"),
    ("permission denied", "permission denied"),
    ("does not exist", "does not exist"),
    ("already exists", "name collides with another view"),
    ("statement timeout", "timed out"),
    ("timeout expired", "timed out"),
    ("timed out", "timed out"),
    ("could not translate host name", "connection failed"),
    ("connection refused", "connection failed"),
    ("could not connect", "connection failed"),
    ("unable to connect", "connection failed"),
)


def failure_category(error: BaseException | str, default: str | None = None) -> str:
    text = str(error).lower()
    for needle, category in _FAILURE_CATEGORIES:
        if needle in text:
            return category
    if default is not None:
        return default
    return type(error).__name__ if isinstance(error, BaseException) else "unknown error"


_CONNECTION_DETAILS = (
    (re.compile(r'(?i)(unable to connect to postgres) at "[^"]*"'), r"\1"),
    (re.compile(r'(?i)(connection to server) (?:at "[^"]*"(?: \([^)]*\))?, port \d+|on socket "[^"]*")'), r"\1"),
    (re.compile(r'(?i)(could not translate host name) "[^"]*"'), r"\1"),
    (re.compile(r'(?i)(no pg_hba\.conf entry) for host "[^"]*", user "[^"]*", database "[^"]*"'), r"\1"),
    (re.compile(r'(?i)(server certificate) for "[^"]*"(?: \(and \d+ other names?\))? (does not match host name) "[^"]*"'),
     r"\1 \2"),
    (re.compile(r'(?i)(for user) "[^"]*"'), r"\1"),
)


def redact_connection_details(text: str, secrets: Iterable[str]) -> str:
    for pattern, replacement in _CONNECTION_DETAILS:
        text = pattern.sub(replacement, text)
    for secret in secrets:
        if secret:
            text = re.sub(rf"(?<![\w.-]){re.escape(str(secret))}(?![\w.-])", "<redacted>", text)
    return text


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _pg_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _qualified_pg_name(schema: str, table: str) -> str:
    return _ident(schema) + "." + _ident(table)


def _libpq_value(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _split_view_name(name: str) -> tuple[str, str]:
    schema, table = name.split(".", 1)
    return schema, table


def _load_postgres_extension(con: duckdb.DuckDBPyConnection) -> None:
    extension_dir = os.environ.get(EXTENSION_DIR_ENV)
    if extension_dir:
        con.execute(f"SET extension_directory = {_sql_literal(extension_dir)}")
        con.execute("SET autoinstall_known_extensions = false")
    else:
        con.execute("INSTALL postgres")
    con.execute("LOAD postgres")


def _create_secret(con: duckdb.DuckDBPyConnection, conn: PgConn) -> None:
    try:
        con.execute(
            f"CREATE SECRET {_SECRET_NAME} (TYPE postgres, HOST {_sql_literal(conn.host)}, "
            f"PORT {int(conn.port)}, USER {_sql_literal(conn.user)}, PASSWORD {_sql_literal(conn.password)})"
        )
    except duckdb.Error:
        raise duckdb.InvalidInputException("Could not register the Postgres credentials with DuckDB") from None


def _attach_string(database: str, conn: PgConn, statement_timeout_ms: int) -> str:
    parts = [f"dbname={_libpq_value(database)}"]
    if conn.sslmode:
        parts.append(f"sslmode={_libpq_value(conn.sslmode)}")
    parts.append(f"options={_libpq_value(f'-c statement_timeout={int(statement_timeout_ms)}')}")
    return " ".join(parts)


def _table_ref(alias: str, schema: str, table: str) -> str:
    return f"{alias}.{_ident(schema)}.{_ident(table)}"


def build_engine(
    conn: PgConn,
    views: dict[str, list[str]],
    *,
    statement_timeout_ms: int,
    memory_limit: str,
    threads: int,
    pg_connection_limit: int,
    pg_idle_timeout_ms: int,
    tenants: list[str] | None = None,
) -> TenantEngine:
    con = duckdb.connect()
    try:
        _load_postgres_extension(con)
        con.execute(f"SET memory_limit = {_sql_literal(memory_limit)}")
        con.execute(f"SET threads = {int(threads)}")
        con.execute(f"SET pg_pool_max_connections = {int(pg_connection_limit)}")
        con.execute("SET pg_pool_acquire_mode = 'wait'")
        con.execute(f"SET pg_pool_idle_timeout_millis = {int(pg_idle_timeout_ms)}")
        con.execute("SET pg_pool_enable_reaper_thread = true")
        _create_secret(con, conn)

        failures: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for number, tenant in enumerate(_engine_tenants(views, tenants), start=1):
            alias = f"__t{number:04d}"
            attach = _sql_literal(_attach_string(tenant, conn, statement_timeout_ms))
            try:
                con.execute(f"ATTACH {attach} AS {alias} (TYPE postgres, READ_ONLY, SECRET {_SECRET_NAME})")
            except duckdb.Error as exc:
                failures[tenant] = failure_category(exc)
                logger.warning("Could not attach tenant database %s: %s", tenant, failures[tenant])
                logger.debug(
                    "Attach of tenant database %s failed: %s",
                    tenant, redact_connection_details(str(exc), (conn.password, conn.host, conn.user)),
                )
            else:
                aliases[tenant] = alias

        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        engine = TenantEngine(
            con, aliases, [{"name": name, "reason": reason} for name, reason in sorted(failures.items())]
        )
        engine.ensure_views(views)
    except BaseException:
        con.close()
        raise
    return engine


def build_default_engine(conn: PgConn, views: dict[str, list[str]], *, tenants: list[str] | None = None) -> TenantEngine:
    return build_engine(
        conn,
        views,
        statement_timeout_ms=DEFAULT_STATEMENT_TIMEOUT_MS,
        memory_limit=DEFAULT_MEMORY_LIMIT,
        threads=DEFAULT_THREADS,
        pg_connection_limit=DEFAULT_PG_CONNECTION_LIMIT,
        pg_idle_timeout_ms=DEFAULT_PG_IDLE_TIMEOUT_MS,
        tenants=_engine_tenants(views, tenants),
    )


def _engine_tenants(views: dict[str, list[str]], tenants: list[str] | None) -> list[str]:
    return sorted(set(tenants or ()) | {tenant for members in views.values() for tenant in members})


_CacheKey = tuple[str, str]

_cache: dict[_CacheKey, tuple[TenantEngine, float]] = {}
_cache_lock = threading.Lock()
_build_locks: dict[_CacheKey, threading.Lock] = {}
_building: dict[_CacheKey, object] = {}
_forced_rebuilds: dict[_CacheKey, float] = {}
_finished_builds: dict[_CacheKey, int] = {}
_last_builds: dict[_CacheKey, TenantEngine] = {}
_waiting: dict[_CacheKey, int] = {}


def _cache_key(conn: PgConn, tenants: list[str], connection_id: str) -> _CacheKey:
    payload = {"conn": asdict(conn), "tenants": tenants}
    return connection_id or "", hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _drop_expired(now: float) -> None:
    for key in [k for k, (_, expires_at) in _cache.items() if expires_at <= now]:
        del _cache[key]


def _expiry(engine: TenantEngine, now: float, ttl_seconds: int) -> float:
    return now + (min(ttl_seconds, DEGRADED_ENGINE_TTL_SECONDS) if engine.degraded else ttl_seconds)


def peek_engine(conn: PgConn, *, tenants: list[str], connection_id: str = "") -> TenantEngine | None:
    key = _cache_key(conn, _engine_tenants({}, tenants), connection_id)
    with _cache_lock:
        _drop_expired(monotonic())
        cached = _cache.get(key)
    return cached[0] if cached else None


def get_engine(
    conn: PgConn,
    views: dict[str, list[str]],
    ttl_seconds: int = 600,
    *,
    tenants: list[str] | None = None,
    connection_id: str = "",
) -> TenantEngine:
    all_tenants = _engine_tenants(views, tenants)
    key = _cache_key(conn, all_tenants, connection_id)
    with _cache_lock:
        build_lock = _build_locks.setdefault(key, threading.Lock())
        seen_builds = _finished_builds.get(key, 0)
        _waiting[key] = _waiting.get(key, 0) + 1
    try:
        with build_lock:
            return _cached_or_built(conn, views, ttl_seconds, all_tenants, key, seen_builds)
    finally:
        with _cache_lock:
            _waiting[key] = _waiting.get(key, 1) - 1
            if _waiting[key] <= 0:
                _waiting.pop(key, None)
                _last_builds.pop(key, None)


def _cached_or_built(conn, views, ttl_seconds, all_tenants, key, seen_builds) -> TenantEngine:
    token = object()
    with _cache_lock:
        _drop_expired(monotonic())
        cached = _cache.get(key)
        if not cached and _finished_builds.get(key, 0) != seen_builds and key in _last_builds:
            cached = (_last_builds[key], 0.0)
        if not cached:
            _building[key] = token
    if cached:
        engine = cached[0]
        was_degraded = engine.degraded
        engine.ensure_views(views)
        if engine.degraded and not was_degraded:
            with _cache_lock:
                if key in _cache:
                    _cache[key] = (engine, min(_cache[key][1], _expiry(engine, monotonic(), ttl_seconds)))
        return engine
    try:
        engine = build_default_engine(conn, views, tenants=all_tenants)
    except BaseException:
        with _cache_lock:
            if _building.get(key) is token:
                del _building[key]
        raise
    with _cache_lock:
        if _building.get(key) is token:
            del _building[key]
            _finished_builds[key] = _finished_builds.get(key, 0) + 1
            if engine.attached:
                engine.cache_key = key
                _cache[key] = (engine, _expiry(engine, monotonic(), ttl_seconds))
            elif _waiting.get(key, 0) > 1:
                _last_builds[key] = engine
    return engine


def forced_rebuild_allowed(last: float | None, now: float) -> bool:
    return last is None or now - last >= DEGRADED_ENGINE_TTL_SECONDS


def evict_engine(engine: TenantEngine) -> bool:
    key = getattr(engine, "cache_key", None)
    if key is None:
        return False
    now = monotonic()
    with _cache_lock:
        cached = _cache.get(key)
        if key in _building or cached is None or cached[0] is not engine:
            return True
        if not forced_rebuild_allowed(_forced_rebuilds.get(key), now):
            return False
        _forced_rebuilds[key] = now
        del _cache[key]
    return True


def invalidate_engines(connection_id: str | None = None) -> None:
    # Engines are dropped, not closed: queries still running on their cursors keep them alive.
    with _cache_lock:
        for registry in (_cache, _build_locks, _building, _forced_rebuilds, _finished_builds, _last_builds, _waiting):
            for key in [k for k in registry if connection_id is None or k[0] == connection_id]:
                del registry[key]
