import logging
import re

import pandas as pd
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
from app.data_sources.clients.postgresql_multi_engine import PgConn, get_engine, invalidate_engines
from app.data_sources.clients.progress import (
    discovery_items,
    discovery_progress,
    format_name_list,
    unreadable_note,
)
from app.data_sources.engine_pool import ephemeral
from app.utils.safe_regex import compile_bounded

logger = logging.getLogger(__name__)

ENGINE_TTL_SECONDS = 600
DIALECT = "duckdb"
UNREADABLE_TENANTS_KEY = "unreadable_tenants"

_DUCKDB_CATALOG_READERS = re.compile(
    r"^(query_table|query|duckdb_.*|pragma_.*|sql_auto_complete|json_execute_serialized_sql|"
    r"json_deserialize_sql|which_secret|glob)$",
    re.IGNORECASE,
)

_DISCOVERY_SQL = "SELECT datname FROM pg_database WHERE datallowconn AND NOT datistemplate ORDER BY datname"


class PostgresqlMultiClient(DataSourceClient):

    sql_literal_style = "standard"

    EXTRACTION_DIALECT = DIALECT

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
        self._views: dict[str, list[str]] = {}
        self._excluded_tenants: dict[str, list[str]] = {}
        self._unreadable: list[dict] = []
        self._unreadable_names: list[str] = []
        self._type_conflicts: list[dict] = []
        self._query_failures: list[str] = []

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

    def _unreadable_reason(self, client: PostgresqlClient, fallback: str) -> str:
        probe = client.test_connection()
        return fallback if probe.get("success") else str(probe.get("message") or fallback)

    def _read_tenant(self, database: str, matcher) -> list[Table] | None:
        client = self._tenant_client(database)
        try:
            tables = require_complete_discovery(client, client.get_tables())
        except (NotImplementedError, RecursionError):
            raise
        except (IncompleteDiscoveryError, SQLAlchemyError, RuntimeError) as exc:
            reason = self._unreadable_reason(client, str(exc))
            logger.warning("Could not read tenant database %s: %s", database, reason)
            self._unreadable.append({"name": database, "reason": reason})
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
        for table in catalog.tables:
            table.metadata_json[UNREADABLE_TENANTS_KEY] = list(self._unreadable_names)
        self._learn([(t.name, t.metadata_json) for t in catalog.tables], replace=True)
        invalidate_engines()
        return catalog.tables

    def _learn(self, entries, *, replace: bool) -> None:
        views = {} if replace else dict(self._views)
        excluded = {} if replace else dict(self._excluded_tenants)
        unreadable = set() if replace else set(self._unreadable_names)
        for name, meta in entries:
            if not isinstance(name, str) or "." not in name or not isinstance(meta, dict):
                continue
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

    def attach_table_metadata(self, tables: list[dict]) -> None:
        self._learn(
            [(t.get("name"), t.get("metadata_json")) for t in tables or [] if isinstance(t, dict)],
            replace=False,
        )

    def index_stats(self) -> dict:
        stats: dict = {}
        if self._unreadable:
            stats["unreadable_datasets"] = list(self._unreadable)
            stats["unreadable_dataset_count"] = len(self._unreadable)
        if self._type_conflicts:
            stats["type_conflicts"] = list(self._type_conflicts)
        return stats

    def unindexed_table_prefixes(self) -> list[str]:
        return []

    def _check_table_refs(self, sql: str) -> None:
        refs = table_refs(sql, DIALECT)
        refuse_unchecked_reads(sql, DIALECT, extra_denied=_DUCKDB_CATALOG_READERS)
        if any(isinstance(stmt, exp.Pragma) for stmt in parse_guard_sql(sql, DIALECT)):
            raise TableScopeViolation(
                "PRAGMA statements are not allowed: the unified tables are already described in the schema."
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

    def execute_query(self, sql: str) -> pd.DataFrame:
        self._check_table_refs(sql)
        engine = get_engine(self._pg_conn(), self._views, ttl_seconds=ENGINE_TTL_SECONDS)
        self._query_failures = sorted(f["name"] for f in engine.failed_tenants)
        cursor = engine.cursor()
        try:
            return cursor.execute(sql).fetchdf()
        finally:
            cursor.close()

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
                    probe = self._tenant_client(database).test_connection()
                    if not probe.get("success"):
                        unreadable.append({"name": database, "reason": probe.get("message")})
        except Exception as e:
            return {"success": False, "message": str(e)}
        if len(unreadable) == len(tenants):
            return {
                "success": False,
                "message": f"None of the {len(tenants)} matching databases could be read." + unreadable_note(unreadable),
            }
        return {
            "success": True,
            "message": f"Successfully connected to PostgreSQL: {len(tenants)} databases match." + unreadable_note(unreadable),
        }

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
        return notes

    @property
    def description(self) -> str:
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
        text_ += (
            "\nExamples:\n"
            "```python\n"
            f'df = client.execute_query("SELECT {TENANT_COLUMN}, count(*) FROM public.orders GROUP BY {TENANT_COLUMN}")\n'
            f'df = client.execute_query("SELECT o.id, u.name FROM public.orders o JOIN public.users u '
            f'ON o.{TENANT_COLUMN} = u.{TENANT_COLUMN} AND o.user_id = u.id")\n'
            "```\n"
        )
        return text_
