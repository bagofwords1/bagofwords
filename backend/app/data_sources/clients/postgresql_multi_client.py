import json
import logging
import re
import threading
from time import monotonic

import duckdb
import pandas as pd
import sqlglot
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlglot import exp

from app.ai.code_execution.sql_parser import parse_guard_sql
from app.ai.code_execution.table_scope import TableScopeViolation, refuse_unchecked_reads, table_refs
from app.ai.prompt_formatters import Table, TableFormatter
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.database_spec import parse_database_spec, select_databases, spec_matches
from app.data_sources.clients.discovery import IncompleteDiscoveryError, require_complete_discovery
from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.data_sources.clients.postgresql_multi_catalog import TENANT_COLUMN, unify_catalogs
from app.data_sources.clients.postgresql_multi_engine import (
    PgConn,
    TenantEngine,
    build_default_engine,
    evict_engine,
    failure_category,
    forced_rebuild_allowed,
    get_engine,
    invalidate_engines,
    peek_engine,
    redact_connection_details,
)
from app.data_sources.clients.progress import (
    discovery_items,
    discovery_progress,
    format_name_list,
    unreadable_note,
)
from app.data_sources.engine_pool import ephemeral
from app.data_sources.query_cancellation import track
from app.utils.safe_regex import compile_bounded

logger = logging.getLogger(__name__)

ENGINE_TTL_SECONDS = 600
DIALECT = "duckdb"
UNREADABLE_TENANTS_KEY = "unreadable_tenants"
INDEXED_TENANTS_KEY = "indexed_tenants"
NO_READABLE_TABLES_REASON = "no readable tables (check USAGE/SELECT grants)"

_DUCKDB_CATALOG_READERS = re.compile(
    r"^(query_table|query|duckdb_.*|pragma_.*|sql_auto_complete|json_execute_serialized_sql|"
    r"json_deserialize_sql|which_secret|glob|pg_get_viewdef|pg_get_constraintdef|has_.*_privilege|"
    r"pg_.*_is_visible|current_setting|getvariable|format_type)$",
    re.IGNORECASE,
)

_DISCOVERY_SQL = "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname"

_READABLE_TABLES_SQL = (
    "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
    "WHERE table_schema NOT IN ('information_schema', 'pg_catalog'){schema_filter})"
)

_GENERIC_TYPES = ("ARRAY", "USER-DEFINED")

_CONNECT_FAILURE = re.compile(r"(?:IO Error: )?Unable to connect to Postgres\b")
_UNKNOWN_TYPE = re.compile(r"Type with name .* does not exist", re.DOTALL)

_VANISHED_RELATION = re.compile(
    r'Failed to prepare COPY "COPY \(.*\) TO STDOUT \(FORMAT "binary"\);": ERROR:\s+relation "[^"]*" does not exist',
    re.DOTALL,
)

_EXACT_TYPES_SQL = """
    SELECT c.table_schema, c.table_name, c.column_name, format_type(a.atttypid, NULL)
    FROM information_schema.columns c
    JOIN pg_catalog.pg_namespace n ON n.nspname = c.table_schema
    JOIN pg_catalog.pg_class r ON r.relnamespace = n.oid AND r.relname = c.table_name
    JOIN pg_catalog.pg_attribute a ON a.attrelid = r.oid AND a.attname = c.column_name
    WHERE c.data_type IN ('ARRAY', 'USER-DEFINED')
"""


class PostgresqlMultiClient(DataSourceClient):

    sql_literal_style = "standard"

    exact_table_scope_catalogs = ("memory",)

    relative_date_hint = "Relative dates (DuckDB): current_date, current_date - INTERVAL 7 DAY, date_trunc('month', current_date); the clock is the app server's local time."

    def __init__(self, host, port, databases, user, password="", schema=None, sslmode="prefer",
                 discovery_database="postgres", table_exclude_pattern: str | None = None):
        self.host = host
        self.port = port
        self.databases = databases
        self.user = user
        self.password = password
        self.schema = schema
        self.sslmode = sslmode
        self.discovery_database = discovery_database or "postgres"
        self.table_exclude_pattern = (table_exclude_pattern or "").strip() or None
        self._includes, self._excludes, _ = parse_database_spec(databases)
        self._schemas = [s.strip() for s in schema.split(",") if s.strip()] if isinstance(schema, str) else []
        self._views: dict[str, list[str]] = {}
        self._indexed_tenants: list[str] = []
        self._excluded_tenants: dict[str, list[str]] = {}
        self._unreadable: list[dict] = []
        self._unreadable_names: list[str] = []
        self._type_conflicts: list[dict] = []
        self._query_failures: list[str] = []
        self._query_left_out: dict[str, list[str]] = {}
        self._query_missing_views: list[str] = []
        self._own_engine: TenantEngine | None = None
        self._own_engine_lock = threading.Lock()
        self._own_rebuilt_at: float | None = None

    def _tenant_client(self, database: str) -> PostgresqlClient:
        return PostgresqlClient(
            host=self.host,
            port=self.port,
            database=database,
            user=self.user,
            password=self.password,
            schema=self.schema,
            sslmode=self.sslmode,
        )

    def _pg_conn(self) -> PgConn:
        return PgConn(
            host=self.host, port=int(self.port), user=self.user, password=self.password or "", sslmode=self.sslmode,
        )

    def _in_spec(self, database: str) -> bool:
        return spec_matches(database, self._includes, self._excludes, case_sensitive=True)

    def _no_match_message(self) -> str:
        return f"No database on the server matches '{self.databases}'."

    def _discover(self) -> list[str]:
        with self._tenant_client(self.discovery_database).connect() as conn:
            names = [row[0] for row in conn.execute(text(_DISCOVERY_SQL)).fetchall()]
        return select_databases(names, self._includes, self._excludes, case_sensitive=True)

    def _exclude_matcher(self):
        if not self.table_exclude_pattern:
            return None
        matcher = compile_bounded(self.table_exclude_pattern)
        if matcher is None:
            raise ValueError("Invalid table_exclude_pattern")
        return matcher

    def _keep_table(self, table: Table, matcher) -> bool:
        if matcher is None:
            return True
        excluded = matcher.search(table.name)
        if matcher.dead:
            raise ValueError("table_exclude_pattern timed out; simplify the regular expression")
        return not excluded

    def _redacted(self, text_: str) -> str:
        return redact_connection_details(text_, (self.password, self.host, self.user))

    def _unreadable_reason(self, client: PostgresqlClient, error: Exception) -> str:
        probe = client.test_connection()
        if probe.get("success"):
            return failure_category(error, default="discovery incomplete")
        return failure_category(str(probe.get("message") or ""), default="connection failed")

    def _with_exact_types(self, client: PostgresqlClient, tables: list[Table]) -> list[Table]:
        if not any(c.dtype in _GENERIC_TYPES for t in tables for c in t.columns or []):
            return tables
        with client.connect() as conn:
            rows = conn.execute(text(_EXACT_TYPES_SQL)).fetchall()
        exact = {(f"{schema}.{table}", column): dtype for schema, table, column, dtype in rows}
        for table in tables:
            for column in table.columns or []:
                if column.dtype in _GENERIC_TYPES:
                    column.dtype = exact.get((table.name, column.name), column.dtype)
        return tables

    def _read_tenant(self, database: str, matcher) -> list[Table] | None:
        client = self._tenant_client(database)
        try:
            tables = self._with_exact_types(client, require_complete_discovery(client, client.get_tables()))
        except (NotImplementedError, RecursionError):
            raise
        except (IncompleteDiscoveryError, SQLAlchemyError, RuntimeError) as exc:
            reason = self._unreadable_reason(client, exc)
            logger.warning("Could not read tenant database %s: %s", database, reason)
            logger.debug("Reading tenant database %s failed: %s", database, self._redacted(str(exc)))
            self._unreadable.append({"name": database, "reason": reason})
            return None
        if not tables:
            logger.warning("Tenant database %s has no readable tables", database)
            self._unreadable.append({"name": database, "reason": NO_READABLE_TABLES_REASON})
            return None
        return [t for t in tables if self._keep_table(t, matcher)]

    @discovery_progress
    def get_schemas(self, progress_callback=None) -> list[Table]:
        matcher = self._exclude_matcher()
        self._unreadable = []
        per_tenant: dict[str, list[Table]] = {}
        with ephemeral():
            tenants = self._discover()
            if not tenants:
                raise RuntimeError(self._no_match_message())
            for database in discovery_items(tenants, "databases", label=lambda d: d):
                tables = self._read_tenant(database, matcher)
                if tables is not None:
                    per_tenant[database] = tables
        if not per_tenant:
            raise RuntimeError(f"None of the {len(tenants)} matching tenant databases could be read")

        catalog = unify_catalogs(per_tenant)
        self._type_conflicts = catalog.excluded
        self._unreadable_names = sorted(d["name"] for d in self._unreadable)
        indexed = sorted(per_tenant)
        for table in catalog.tables:
            table.metadata_json[UNREADABLE_TENANTS_KEY] = list(self._unreadable_names)
            table.metadata_json[INDEXED_TENANTS_KEY] = list(indexed)
        self._learn([(t.name, t.metadata_json) for t in catalog.tables], replace=True)
        if self._connection_id():
            invalidate_engines(self._connection_id())
        self._own_engine = None
        return catalog.tables

    def _learn(self, entries, *, replace: bool) -> None:
        views = {} if replace else dict(self._views)
        excluded = {} if replace else dict(self._excluded_tenants)
        unreadable = set() if replace else set(self._unreadable_names)
        indexed = set() if replace else set(self._indexed_tenants)
        for name, meta in entries:
            if not isinstance(name, str) or "." not in name or not isinstance(meta, dict):
                continue
            indexed.update(t for t in meta.get(INDEXED_TENANTS_KEY) or [] if isinstance(t, str) and self._in_spec(t))
            tenants = [t for t in meta.get("tenants") or [] if isinstance(t, str) and self._in_spec(t)]
            if not tenants:
                continue
            views[name] = tenants
            excluded[name] = [t for t in meta.get("excluded_tenants") or [] if isinstance(t, str) and self._in_spec(t)]
            unreadable.update(
                t for t in meta.get(UNREADABLE_TENANTS_KEY) or [] if isinstance(t, str) and self._in_spec(t)
            )
        self._views = views
        self._excluded_tenants = {k: v for k, v in excluded.items() if v}
        self._unreadable_names = sorted(unreadable)
        self._indexed_tenants = sorted(indexed)

    def attach_table_metadata(self, tables: list[dict]) -> None:
        self._learn(
            [(t.get("name"), t.get("metadata_json")) for t in tables or [] if isinstance(t, dict)],
            replace=False,
        )

    def index_stats(self) -> dict:
        entries = list(self._unreadable) + [_left_out_entry(e) for e in self._type_conflicts]
        if not entries:
            return {}
        return {"unreadable_datasets": entries, "unreadable_dataset_count": len(entries)}

    def unindexed_table_prefixes(self) -> list[str]:
        return []

    def _check_table_refs(self, sql: str) -> None:
        refs = table_refs(sql, DIALECT)
        refuse_unchecked_reads(sql, DIALECT, extra_denied=_DUCKDB_CATALOG_READERS)
        statements = parse_guard_sql(sql, DIALECT)
        if any(isinstance(stmt, exp.Pragma) for stmt in statements):
            raise TableScopeViolation(
                "PRAGMA statements are not allowed: the unified tables are already described in the schema."
            )
        foreign_types = [t for stmt in statements for t in _catalog_qualified_types(stmt)]
        foreign_types += [t for stmt in statements for t in _json_structure_types(stmt)]
        if foreign_types:
            raise TableScopeViolation(
                f"Type(s) outside this connection's unified tables: {', '.join(dict.fromkeys(foreign_types))}. "
                "Cast to built-in types only."
            )
        known = {tuple(name.lower().split(".", 1)) for name in self._views}
        outside = [
            ".".join(r) for r in refs
            if not ((len(r) == 2 and r in known) or (len(r) == 3 and r[0] == "memory" and r[1:] in known))
        ]
        if outside:
            raise TableScopeViolation(
                f"Table(s) not in this connection's unified tables: {', '.join(dict.fromkeys(outside))}. "
                "Name every table as <schema>.<table>, for example public.orders; only indexed tables can be queried."
            )

    def _engine_tenants(self) -> list[str]:
        return sorted(set(self._indexed_tenants) | {t for members in self._views.values() for t in members})

    def _connection_id(self) -> str:
        return str(getattr(self, "_bow_connection_id", "") or "")

    def _learn_engine(self, engine: TenantEngine) -> None:
        self._query_failures = sorted(f["name"] for f in engine.failed_tenants)
        left_out: dict[str, list[str]] = {}
        for entry in list(engine.left_out):
            if entry["view"] in self._views:
                left_out.setdefault(entry["view"], []).append(entry["tenant"])
        self._query_left_out = left_out
        self._query_missing_views = sorted({v for v in engine.missing_views if v in self._views})

    def _engine(self) -> TenantEngine:
        if self._connection_id():
            engine = get_engine(
                self._pg_conn(),
                self._views,
                ttl_seconds=ENGINE_TTL_SECONDS,
                tenants=self._engine_tenants(),
                connection_id=self._connection_id(),
            )
        else:
            engine = self._unshared_engine()
        self._learn_engine(engine)
        return engine

    def _unshared_engine(self) -> TenantEngine:
        with self._own_engine_lock:
            engine = self._own_engine
            if engine is None:
                engine = build_default_engine(self._pg_conn(), self._views, tenants=self._engine_tenants())
                self._own_engine = engine if engine.attached else None
            else:
                engine.ensure_views(self._views)
            return engine

    def _referenced_views(self, sql: str) -> list[str]:
        by_key = {tuple(name.lower().split(".", 1)): name for name in self._views}
        referenced = {
            by_key[ref[-2:]] for ref in table_refs(sql, DIALECT) if len(ref) >= 2 and ref[-2:] in by_key
        }
        return sorted(referenced) or sorted(self._views)

    def _discard(self, engine: TenantEngine) -> bool:
        if self._connection_id():
            return evict_engine(engine)
        now = monotonic()
        with self._own_engine_lock:
            if not forced_rebuild_allowed(self._own_rebuilt_at, now):
                return False
            self._own_rebuilt_at = now
            if self._own_engine is engine:
                self._own_engine = None
        return True

    def _run(self, engine: TenantEngine, sql: str) -> pd.DataFrame:
        cursor = engine.cursor()
        try:
            with track(self, cursor):
                return cursor.execute(sql).fetchdf()
        finally:
            cursor.close()

    def _without_connection_details(self, exc: duckdb.Error) -> duckdb.Error:
        if isinstance(exc, duckdb.CatalogException) and _UNKNOWN_TYPE.search(str(exc)):
            return duckdb.CatalogException(
                "Catalog Error: that type does not exist here. Cast to a built-in DuckDB type."
            )
        if not _postgres_side(exc):
            return exc
        if _CONNECT_FAILURE.match(str(exc)):
            text_ = self._redacted(str(exc))
        else:
            text_ = f"A tenant database read failed: {failure_category(exc)}"
        if text_ == str(exc):
            return exc
        try:
            return type(exc)(text_)
        except TypeError:
            return duckdb.Error(text_)

    def execute_query(self, sql: str) -> pd.DataFrame:
        self._check_table_refs(sql)
        engine = self._engine()
        try:
            return self._run(engine, sql)
        except duckdb.Error as exc:
            vanished = engine.vanished_tables(self._referenced_views(sql)) if _relation_vanished(exc) else []
            if not (vanished and self._discard(engine)):
                raise self._without_connection_details(exc) from None
            logger.warning(
                "Rebuilding the tenant engine of connection %s: %s",
                self._connection_id() or "(unsaved)",
                ", ".join(f"{view} is gone from {tenant}" for view, tenant in vanished),
            )
        del engine
        try:
            return self._run(self._engine(), sql)
        except duckdb.Error as exc:
            raise self._without_connection_details(exc) from None

    def test_connection(self) -> dict:
        try:
            with ephemeral():
                tenants = self._discover()
                if not tenants:
                    return {
                        "success": False,
                        "message": self._no_match_message(),
                    }
                unreadable = []
                for database in tenants:
                    reason = self._probe_tenant(database)
                    if reason:
                        unreadable.append({"name": database, "reason": reason})
        except Exception as e:
            logger.debug("Listing databases from %s failed: %s", self.discovery_database, self._redacted(str(e)))
            return {
                "success": False,
                "message": f"Could not list the databases from '{self.discovery_database}': {failure_category(e)}",
            }
        if len(unreadable) == len(tenants):
            return {
                "success": False,
                "message": f"None of the {len(tenants)} matching databases could be read." + unreadable_note(unreadable),
            }
        return {
            "success": True,
            "message": f"Successfully connected to PostgreSQL: {len(tenants)} databases match." + unreadable_note(unreadable),
        }

    def _probe_tenant(self, database: str) -> str | None:
        schema_filter = " AND table_schema = ANY(:schemas)" if self._schemas else ""
        try:
            with self._tenant_client(database).connect() as conn:
                readable = conn.execute(
                    text(_READABLE_TABLES_SQL.format(schema_filter=schema_filter)), {"schemas": self._schemas}
                ).scalar()
        except Exception as exc:
            logger.debug("Probing tenant database %s failed: %s", database, self._redacted(str(exc)))
            return failure_category(exc)
        return None if readable else NO_READABLE_TABLES_REASON

    def get_schema(self, table_id: str) -> Table:
        raise NotImplementedError("get_schema() is obsolete. Use get_tables() instead.")

    def prompt_schema(self):
        return TableFormatter(self.get_schemas()).table_str

    def _missing_tenants(self) -> list[str]:
        return sorted(set(self._unreadable_names) | set(self._query_failures))

    def _conflict_notes(self) -> list[str]:
        columns: dict[tuple[str, str], list[str]] = {}
        for entry in self._type_conflicts:
            if entry.get("tenant") and entry.get("column"):
                columns.setdefault((entry["table"], entry["tenant"]), []).append(entry["column"])
        notes = []
        for table, tenants in sorted(self._excluded_tenants.items()):
            for tenant in tenants:
                cols = columns.get((table, tenant))
                where = ", ".join(f"{table}.{c}" for c in cols) if cols else table
                notes.append(f"{table} leaves out {tenant} (column type conflict on {where})")
        for table, tenants in sorted(self._query_left_out.items()):
            for tenant in sorted(tenants):
                notes.append(f"{table} leaves out {tenant} (its table could not be read at query time)")
        return notes

    def _peek_engine(self) -> None:
        if not self._views:
            return
        if not self._connection_id():
            engine = self._own_engine
        else:
            engine = peek_engine(self._pg_conn(), tenants=self._engine_tenants(), connection_id=self._connection_id())
        if engine is not None:
            self._learn_engine(engine)

    @property
    def description(self) -> str:
        self._peek_engine()
        tenants = sorted({t for members in self._views.values() for t in members})
        listed = f"({len(tenants)}): {format_name_list(tenants, 50)}" if tenants else "(none indexed yet)"
        text_ = (
            f"PostgreSQL tenant databases {listed}\n\n"
            "You can call the execute_query method to run SQL queries. Write DuckDB SQL.\n"
            "Every table is the union of that table across all tenant databases, named <schema>.<table> "
            f"(for example public.orders). Its first column, {TENANT_COLUMN}, holds the name of the tenant "
            "database each row comes from.\n"
            f"For one tenant, filter on it: WHERE {TENANT_COLUMN} = '<database>'.\n"
            f"Every join between two tables must include a.{TENANT_COLUMN} = b.{TENANT_COLUMN}: each tenant "
            "has its own id space, so a join on ids alone matches rows of different tenants.\n"
            "Queries that name a table without its schema are refused.\n"
        )
        missing = self._missing_tenants()
        if missing:
            text_ += (
                f"Not readable, so missing from every table and every result: {format_name_list(missing, 50)}. "
                "Tell the user results exclude them.\n"
            )
        conflicts = self._conflict_notes()
        if conflicts:
            text_ += "Tenants left out of single tables: " + "; ".join(conflicts) + ".\n"
        if self._query_missing_views:
            text_ += (
                "No tenant database could serve these tables right now, so they cannot be queried: "
                f"{format_name_list(self._query_missing_views, 50)}. Tell the user if they ask for them.\n"
            )
        text_ += (
            "\nExamples:\n"
            "```python\n"
            f'df = client.execute_query("SELECT {TENANT_COLUMN}, count(*) FROM public.orders GROUP BY {TENANT_COLUMN}")\n'
            f'df = client.execute_query("SELECT o.id, u.name FROM public.orders o JOIN public.users u '
            f'ON o.{TENANT_COLUMN} = u.{TENANT_COLUMN} AND o.user_id = u.id")\n'
            "```\n"
        )
        return text_


def _catalog_qualified_types(stmt: exp.Expression) -> list[str]:
    names = []
    for dtype in stmt.find_all(exp.DataType):
        if dtype.this != exp.DataType.Type.USERDEFINED:
            continue
        name = str(dtype.args.get("kind") or "")
        parts = name.split(".")
        if len(parts) > 1 and parts[0].lower() != "memory":
            names.append(name)
    return names


_JSON_STRUCTURE_FUNCTIONS = {"json_transform", "json_transform_strict", "from_json", "from_json_strict"}


def _json_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _json_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _json_leaves(item)


def _json_leaf_types(leaf: str) -> list[str]:
    try:
        parsed = sqlglot.parse_one(f"SELECT CAST(NULL AS {leaf})", read=DIALECT)
    except sqlglot.errors.SqlglotError:
        return [leaf]
    if not isinstance(parsed, exp.Select) or {k for k, v in parsed.args.items() if v} != {"expressions"}:
        return [leaf]
    if len(parsed.expressions) != 1 or not isinstance(parsed.expressions[0], exp.Cast):
        return [leaf]
    return _catalog_qualified_types(parsed)


def _json_structure_types(stmt: exp.Expression) -> list[str]:
    names = []
    for fn in stmt.find_all(exp.Anonymous):
        if str(fn.name).lower() not in _JSON_STRUCTURE_FUNCTIONS or len(fn.expressions) < 2:
            continue
        structure = fn.expressions[1]
        if not (isinstance(structure, exp.Literal) and structure.is_string):
            names.append(f"{fn.name} with a structure that is not a literal")
            continue
        try:
            leaves = list(_json_leaves(json.loads(structure.this)))
        except ValueError:
            names.append(f"{fn.name} with a structure that is not JSON")
            continue
        for leaf in leaves:
            names.extend(_json_leaf_types(leaf))
    return names


def _postgres_side(exc: duckdb.Error) -> bool:
    return type(exc) is duckdb.Error or isinstance(exc, (duckdb.IOException, duckdb.ConnectionException))


def _relation_vanished(exc: duckdb.Error) -> bool:
    return type(exc) is duckdb.Error and bool(_VANISHED_RELATION.match(str(exc)))


def _left_out_entry(entry: dict) -> dict:
    table = entry.get("table")
    if entry.get("column"):
        return {
            "name": f"{table} ({entry.get('tenant')})",
            "reason": (
                f"column type conflict: {table}.{entry['column']} "
                f"({entry.get('type')} vs {entry.get('expected_type')})"
            ),
        }
    reason = f"dropped: {entry.get('reason')}"
    if entry.get("with"):
        reason += f" with {', '.join(entry['with'])}"
    return {"name": table, "reason": reason}
