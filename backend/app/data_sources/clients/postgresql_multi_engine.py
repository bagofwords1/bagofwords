import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from time import monotonic

import duckdb

from app.data_sources.clients.postgresql_multi_catalog import TENANT_COLUMN

DEFAULT_STATEMENT_TIMEOUT_MS = 300_000
DEFAULT_MEMORY_LIMIT = "2GB"
DEFAULT_THREADS = 4
DEFAULT_PG_CONNECTION_LIMIT = 4
EXTENSION_DIR_ENV = "BOW_DUCKDB_EXTENSION_DIR"

_SECRET_NAME = "bow_tenant_server"


@dataclass(frozen=True)
class PgConn:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    sslmode: str


class TenantEngine:
    def __init__(self, con: duckdb.DuckDBPyConnection, failed_tenants: list[dict], missing_views: list[str]):
        self._con = con
        self.failed_tenants = failed_tenants
        self.missing_views = missing_views

    def cursor(self) -> duckdb.DuckDBPyConnection:
        return self._con.cursor()

    def close(self) -> None:
        self._con.close()


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ident(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


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
) -> TenantEngine:
    con = duckdb.connect()
    try:
        _load_postgres_extension(con)
        con.execute(f"SET memory_limit = {_sql_literal(memory_limit)}")
        con.execute(f"SET threads = {int(threads)}")
        con.execute(f"SET pg_pool_max_connections = {int(pg_connection_limit)}")
        con.execute("SET pg_pool_acquire_mode = 'wait'")
        _create_secret(con, conn)

        failures: dict[str, str] = {}
        aliases: dict[str, str] = {}
        tenants = sorted({tenant for members in views.values() for tenant in members})
        for number, tenant in enumerate(tenants, start=1):
            alias = f"__t{number:04d}"
            attach = _sql_literal(_attach_string(tenant, conn, statement_timeout_ms))
            try:
                con.execute(f"ATTACH {attach} AS {alias} (TYPE postgres, READ_ONLY, SECRET {_SECRET_NAME})")
            except duckdb.Error as exc:
                failures[tenant] = str(exc)
            else:
                aliases[tenant] = alias

        for name, members in sorted(views.items()):
            schema, table = _split_view_name(name)
            for tenant in sorted(set(members)):
                if tenant not in aliases or tenant in failures:
                    continue
                try:
                    con.execute(f"DESCRIBE {_table_ref(aliases[tenant], schema, table)}")
                except duckdb.Error as exc:
                    failures[tenant] = str(exc)
                    con.execute(f"DETACH {aliases[tenant]}")

        missing_views: list[str] = []
        for name, members in sorted(views.items()):
            schema, table = _split_view_name(name)
            readable = [t for t in sorted(set(members)) if t in aliases and t not in failures]
            if not readable:
                missing_views.append(name)
                continue
            con.execute(f"CREATE SCHEMA IF NOT EXISTS memory.{_ident(schema)}")
            union = " UNION ALL BY NAME ".join(
                f"SELECT {_sql_literal(t)} AS {TENANT_COLUMN}, * FROM {_table_ref(aliases[t], schema, table)}"
                for t in readable
            )
            con.execute(f"CREATE VIEW memory.{_ident(schema)}.{_ident(table)} AS {union}")

        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
    except BaseException:
        con.close()
        raise

    failed_tenants = [{"name": name, "reason": reason} for name, reason in sorted(failures.items())]
    return TenantEngine(con, failed_tenants, missing_views)


_cache: dict[str, tuple[TenantEngine, float]] = {}
_cache_lock = threading.Lock()
_build_locks: dict[str, threading.Lock] = {}
_generation = 0


def _cache_key(conn: PgConn, views: dict[str, list[str]]) -> str:
    payload = {
        "conn": asdict(conn),
        "views": sorted((name, sorted(set(members))) for name, members in views.items()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _drop_expired(now: float) -> None:
    for key in [k for k, (_, expires_at) in _cache.items() if expires_at <= now]:
        del _cache[key]


def get_engine(conn: PgConn, views: dict[str, list[str]], ttl_seconds: int = 600) -> TenantEngine:
    key = _cache_key(conn, views)
    with _cache_lock:
        build_lock = _build_locks.setdefault(key, threading.Lock())
    with build_lock:
        with _cache_lock:
            _drop_expired(monotonic())
            cached = _cache.get(key)
            if cached:
                return cached[0]
            generation = _generation
        engine = build_engine(
            conn,
            views,
            statement_timeout_ms=DEFAULT_STATEMENT_TIMEOUT_MS,
            memory_limit=DEFAULT_MEMORY_LIMIT,
            threads=DEFAULT_THREADS,
            pg_connection_limit=DEFAULT_PG_CONNECTION_LIMIT,
        )
        with _cache_lock:
            if generation == _generation:
                _cache[key] = (engine, monotonic() + ttl_seconds)
        return engine


def invalidate_engines() -> None:
    # Engines are dropped, not closed: queries still running on their cursors keep them alive.
    global _generation
    with _cache_lock:
        _generation += 1
        _cache.clear()
        _build_locks.clear()
