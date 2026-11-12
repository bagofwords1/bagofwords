"""Sample rows from one introspected table on a connection.

Lets a connection manager look at real rows before deciding which tables an
agent should use, and gives the "create custom table" flow a starting query.

The query is built here, never taken from the client: only a table the catalog
already knows can be previewed, and every identifier is quoted for the source's
own language. Each connection type declares how it is sampled in `SPECS`; a
type that is not listed there shows no preview.

How a sample is fetched:

  * **SQL sources** get a query naming the catalog's columns (not ``SELECT *``:
    the rows then match what the agent sees, and the request does not drag
    unlisted blob columns along), bounded in the dialect's own syntax so the
    admin sees exactly what runs. The fetch is bounded again by the extractor,
    which stops the source once it has enough rows.
  * **BigQuery** reads the first rows straight from table storage
    (``tabledata.list``), which is free; a ``SELECT … LIMIT`` is billed as a
    scan of every selected column. The SQL is still returned as the editable
    starting point.
  * **Power BI** samples with ``EVALUATE '<table>'`` against the table's own
    semantic model; the Power BI source bounds it with ``TOPN``.
  * **PostHog** samples with a bounded HogQL query.

Rows come back through the connection's normal credential resolution, so on a
per-user connection each manager sees the rows their own login can see.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_sources.fast import extractor
from app.errors import AppError
from app.errors.codes import ErrorCode
from app.models.connection import Connection
from app.models.connection_table import KIND_TABLE, ConnectionTable
from app.models.user import User
from app.services.custom_query_service import _jsonable

logger = logging.getLogger(__name__)

# One bound for the whole preview, shared with the edited-query path
# (custom-query preview), so the same modal never shows two different caps.
PREVIEW_ROW_LIMIT = extractor.PREVIEW_ROW_LIMIT

# Past this many catalog columns the select list stops being readable, and a
# wide table is exactly where `SELECT *` costs least extra.
MAX_NAMED_COLUMNS = 60

# A preview is for looking at values, not reading documents: long text, JSON
# and blobs are cut so one cell cannot dominate the response.
MAX_CELL_CHARS = 500

_DOUBLE = ('"', '"')
_BACKTICK = ("`", "`")
_BRACKET = ("[", "]")


@dataclass(frozen=True)
class PreviewSpec:
    """How one connection type is sampled."""

    language: str = "sql"            # sql | dax | hogql — also the editor language
    quotes: tuple = _DOUBLE
    bound: str = "limit"             # limit | top | rownum | fetch_first | none (source bounds it)
    # Names stored as "<owner>.<table>" with no namespace in metadata_json.
    first_dot_is_namespace: bool = False
    # Names stored bare while metadata_json records the schema they live in;
    # qualify them so the query does not depend on the session's search path.
    qualify_bare_name: bool = False
    name_columns: bool = True
    # How the source folds an unquoted identifier: "lower" (Postgres family),
    # "upper" (Oracle, Snowflake) or None (kept as written / case-insensitive).
    # Decides when an editor suggestion must be quoted to mean the same name.
    case_fold: str | None = None


# Keyed by the stored `Connection.type`, lower-cased (SQL Server is "MSSQL").
SPECS: dict[str, PreviewSpec] = {
    # Cacheable sources (custom tables are supported on these too).
    "postgresql": PreviewSpec(case_fold="lower"),
    "oracledb": PreviewSpec(bound="rownum", case_fold="upper"),
    # Not "…": SQLite reads a double-quoted name that resolves to nothing as a
    # string literal, so a stale catalog column would show its own name as
    # every value instead of failing. Backticks are always identifiers.
    "sqlite": PreviewSpec(quotes=_BACKTICK),
    "snowflake": PreviewSpec(case_fold="upper"),
    "mysql": PreviewSpec(quotes=_BACKTICK),
    "mariadb": PreviewSpec(quotes=_BACKTICK),
    "clickhouse": PreviewSpec(quotes=_BACKTICK),
    "bigquery": PreviewSpec(quotes=_BACKTICK, first_dot_is_namespace=True),
    "mssql": PreviewSpec(quotes=_BRACKET, bound="top"),
    "ms_fabric": PreviewSpec(quotes=_BRACKET, bound="top"),
    # Sybase stores "owner.table" and records no namespace.
    "sybase": PreviewSpec(quotes=_BRACKET, bound="top", first_dot_is_namespace=True),
    "powerbi": PreviewSpec(language="dax", bound="none", name_columns=False),
    "posthog": PreviewSpec(language="hogql", quotes=_BACKTICK, name_columns=False),
    # Preview-only SQL sources.
    "aws_redshift": PreviewSpec(qualify_bare_name=True, case_fold="lower"),
    "aws_athena": PreviewSpec(case_fold="lower"),
    "trino": PreviewSpec(case_fold="lower"),
    "starburst": PreviewSpec(case_fold="lower"),
    "databricks_sql": PreviewSpec(quotes=_BACKTICK),
    "spark_connect": PreviewSpec(quotes=_BACKTICK),
    "duckdb": PreviewSpec(case_fold="lower"),
    "vertica": PreviewSpec(qualify_bare_name=True, case_fold="lower"),
    "teradata": PreviewSpec(bound="top"),
    "druid": PreviewSpec(),
    "pinot": PreviewSpec(),
    "sap_hana": PreviewSpec(case_fold="upper"),
    # FETCH FIRST works on every Db2 release and platform; LIMIT needs
    # LUW 11.1+ / z/OS 12+ / IBM i 7.2 TR3+.
    "db2": PreviewSpec(bound="fetch_first", case_fold="upper"),
    "ibmi": PreviewSpec(bound="fetch_first", case_fold="upper"),
}

# Namespace chains a client can record in metadata_json, longest first, so a
# three-part "catalog.schema.table" is not split as "catalog" + "schema.table".
_NAMESPACE_CHAINS = (
    ("catalog", "schema"),
    ("project", "dataset"),
    # Snowflake with several databases: "DATABASE.SCHEMA.TABLE".
    ("database", "schema"),
    ("schema",),
    ("database",),
    ("dataset",),
)


def spec_for(conn_type: str | None) -> PreviewSpec | None:
    return SPECS.get((conn_type or "").lower())


def is_previewable_type(conn_type: str | None) -> bool:
    return spec_for(conn_type) is not None


def is_previewable_table(conn_type: str | None, table: ConnectionTable) -> bool:
    spec = spec_for(conn_type)
    if spec is None or table is None or table.kind != KIND_TABLE:
        return False
    if getattr(table, "deleted_at", None) is not None:
        return False
    meta = table.metadata_json or {}
    # A Snowflake semantic view is queried through SEMANTIC_VIEW(...), not a
    # plain select — sampling it would only fail.
    if meta.get("type") == "semantic_view":
        return False
    if spec.language == "dax":
        # The semantic model a table belongs to is what the DAX runs against.
        return bool((meta.get("powerbi") or {}).get("datasetId"))
    return True


def _quote_ident(part: str, quotes: tuple[str, str]) -> str:
    open_q, close_q = quotes
    # Escape the closing quote by doubling it — the rule in every dialect above.
    return f"{open_q}{part.replace(close_q, close_q * 2)}{close_q}"


def name_parts(spec: PreviewSpec, table: ConnectionTable) -> list[str]:
    """The stored table name split into [namespace…, table].

    Splits on the namespace the client recorded rather than on dots, so a
    table whose own name contains a dot still resolves to the right relation.
    """
    name = table.name
    meta = table.metadata_json or {}
    for chain in _NAMESPACE_CHAINS:
        parts = [meta.get(k) for k in chain]
        if not all(isinstance(p, str) and p for p in parts):
            continue
        prefix = ".".join(parts) + "."
        if name.startswith(prefix):
            return [*parts, name[len(prefix):]]
    if spec.first_dot_is_namespace and "." in name:
        owner, rest = name.split(".", 1)
        return [owner, rest]
    schema = meta.get("schema")
    if spec.qualify_bare_name and isinstance(schema, str) and schema and "." not in name:
        return [schema, name]
    return [name]


def quoted_table_ref(conn_type: str, table: ConnectionTable) -> str:
    spec = spec_for(conn_type)
    return ".".join(_quote_ident(p, spec.quotes) for p in name_parts(spec, table))


def _select_list(spec: PreviewSpec, table: ConnectionTable, named: bool) -> str:
    cols = [c.get("name") for c in (table.columns or []) if isinstance(c, dict)]
    if not (named and spec.name_columns) or not cols or len(cols) > MAX_NAMED_COLUMNS:
        return "*"
    if not all(isinstance(c, str) and c for c in cols):
        return "*"
    return ", ".join(_quote_ident(c, spec.quotes) for c in cols)


def _dax_table(table: ConnectionTable) -> str:
    meta = (table.metadata_json or {}).get("powerbi") or {}
    name = meta.get("tableName") or table.name.split("/", 1)[-1]
    return "'" + str(name).replace("'", "''") + "'"


def base_table_sql(conn_type: str, table: ConnectionTable, *, named: bool = True) -> str:
    """The whole table, unbounded — the starting point for a custom table."""
    spec = spec_for(conn_type)
    if spec.language == "dax":
        return f"EVALUATE {_dax_table(table)}"
    return f"SELECT {_select_list(spec, table, named)} FROM {quoted_table_ref(conn_type, table)}"


def preview_sql(conn_type: str, table: ConnectionTable, limit: int, *, named: bool = True) -> str:
    """The default preview query: the table's first ``limit`` rows, with the
    bound written in the source's own syntax so it is visible and editable."""
    spec = spec_for(conn_type)
    n = int(limit)
    if spec.language == "dax" or spec.bound == "none":
        return base_table_sql(conn_type, table, named=named)
    cols = _select_list(spec, table, named)
    ref = quoted_table_ref(conn_type, table)
    if spec.bound == "top":
        return f"SELECT TOP {n} {cols} FROM {ref}"
    if spec.bound == "rownum":
        # ROWNUM rather than FETCH FIRST so Oracle 11g sources work.
        return f"SELECT {cols} FROM {ref} WHERE ROWNUM <= {n}"
    if spec.bound == "fetch_first":
        return f"SELECT {cols} FROM {ref} FETCH FIRST {n} ROWS ONLY"
    return f"SELECT {cols} FROM {ref} LIMIT {n}"


_MAX_REASON_CHARS = 300

# How the dialects word "that column is not there": Postgres/MySQL/SQL Server
# say "column", Oracle/Snowflake/Databricks say "invalid identifier" or
# "unresolved", SQLite says "no such column".
_MISSING_COLUMN = re.compile(r"column|identifier|unresolved", re.IGNORECASE)


def _source_reason(exc: Exception) -> str:
    """The source's own one-line complaint ("no such table", "permission
    denied for relation x"), which is what a manager needs to act on.

    SQLAlchemy wraps the driver error and appends the SQL and a docs link;
    responses must not carry library internals, so unwrap to the driver's
    error (``.orig``) and keep only its first line.
    """
    inner = getattr(exc, "orig", None) or exc
    text = str(inner).strip().splitlines()[0] if str(inner).strip() else type(inner).__name__
    return text[:_MAX_REASON_CHARS]


def _cell(v):
    v = _jsonable(v)
    if isinstance(v, str) and len(v) > MAX_CELL_CHARS:
        return v[:MAX_CELL_CHARS] + "…"
    return v


def _storage_id(client, spec: PreviewSpec, table: ConnectionTable) -> str | None:
    """`project.dataset.table` for a BigQuery storage read, else None."""
    parts = name_parts(spec, table)
    project = getattr(client, "project_id", None)
    if len(parts) == 2 and project:
        return f"{project}.{parts[0]}.{parts[1]}"
    if len(parts) == 3:
        return ".".join(parts)
    return None


class TablePreviewService:
    def __init__(self):
        from app.services.connection_service import ConnectionService

        self.connection_service = ConnectionService()

    async def get_table(
        self, db: AsyncSession, connection: Connection, table_id: str
    ) -> ConnectionTable:
        row = (
            await db.execute(
                select(ConnectionTable).where(
                    ConnectionTable.id == table_id,
                    ConnectionTable.connection_id == str(connection.id),
                    ConnectionTable.kind == KIND_TABLE,
                    ConnectionTable.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise AppError.not_found(ErrorCode.CONNECTION_TABLE_NOT_FOUND, "Table not found")
        return row

    async def _client(self, db, connection, table, spec, current_user):
        client = await self.connection_service.construct_client(db, connection, current_user)
        if spec.language == "dax":
            # Address the table's own semantic model instead of guessing it
            # from the DAX's table references.
            from app.services.custom_query_service import custom_query_service

            pbi = (table.metadata_json or {}).get("powerbi") or {}
            await custom_query_service._prepare_client(db, connection, client, {
                "datasetId": pbi.get("datasetId"),
                "workspaceId": pbi.get("workspaceId"),
                "datasetName": pbi.get("datasetName"),
            })
        return client

    @staticmethod
    def _sample(client, conn_type: str, spec: PreviewSpec, table: ConnectionTable):
        """(sql_shown, base_sql, columns, rows, sampled_from) — runs in a worker thread."""
        from app.data_sources.fast.sources import source_for

        sql = preview_sql(conn_type, table, PREVIEW_ROW_LIMIT)

        src = source_for(client)
        reader = getattr(src, "sample_table", None)
        storage_id = _storage_id(client, spec, table) if reader else None
        if reader and storage_id:
            cols, rows = reader(storage_id, PREVIEW_ROW_LIMIT)
            return sql, base_table_sql(conn_type, table), cols, rows, "storage"

        try:
            cols, rows = extractor.preview(client, sql, PREVIEW_ROW_LIMIT)
            return sql, base_table_sql(conn_type, table), cols, rows, "query"
        except Exception as e:
            # A catalog that is behind the source names a column that no
            # longer exists; the whole row still answers "what is in here".
            # Only that error is retried — a timeout must not run twice.
            fallback = preview_sql(conn_type, table, PREVIEW_ROW_LIMIT, named=False)
            if fallback == sql or not _MISSING_COLUMN.search(_source_reason(e)):
                raise
            logger.info("table preview: named-column query failed, retrying with *")
            cols, rows = extractor.preview(client, fallback, PREVIEW_ROW_LIMIT)
            return fallback, base_table_sql(conn_type, table, named=False), cols, rows, "query"

    def sql_only(self, connection: Connection, table: ConnectionTable) -> dict:
        """The preview query for `table`, built from the catalog without running
        it — so the modal can show what *would* run before the manager presses
        Run. Pure string building (no client, no source round-trip)."""
        spec = spec_for(connection.type)
        if not is_previewable_table(connection.type, table):
            raise AppError.bad_request(
                ErrorCode.TABLE_PREVIEW_UNSUPPORTED,
                f"Data preview is not available for '{connection.type}' connections.",
                type=connection.type,
            )
        return {
            "table_id": str(table.id),
            "table_name": table.name,
            "language": spec.language,
            "sql": preview_sql(connection.type, table, PREVIEW_ROW_LIMIT),
            "base_sql": base_table_sql(connection.type, table),
            "row_limit": PREVIEW_ROW_LIMIT,
        }

    async def preview(
        self,
        db: AsyncSession,
        connection: Connection,
        table: ConnectionTable,
        current_user: User | None = None,
    ) -> dict:
        spec = spec_for(connection.type)
        if not is_previewable_table(connection.type, table):
            raise AppError.bad_request(
                ErrorCode.TABLE_PREVIEW_UNSUPPORTED,
                f"Data preview is not available for '{connection.type}' connections.",
                type=connection.type,
            )

        try:
            client = await self._client(db, connection, table, spec, current_user)
            sql, base_sql, cols, rows, sampled_from = await asyncio.to_thread(
                self._sample, client, connection.type, spec, table
            )
        except (AppError, HTTPException):
            # Typed errors pass through — above all CredentialsRequiredError
            # (403) on a per-user connection, which the UI turns into a
            # "sign in to preview" prompt.
            raise
        except Exception as e:
            logger.info("table preview failed for connection %s: %s", connection.id, e)
            reason = _source_reason(e)
            raise AppError.bad_request(
                ErrorCode.TABLE_PREVIEW_FAILED,
                f"Preview failed: {reason}",
                reason=reason,
            )

        rows = rows[:PREVIEW_ROW_LIMIT]
        return {
            "table_id": str(table.id),
            "table_name": table.name,
            "language": spec.language,
            "sql": sql,
            "base_sql": base_sql,
            "sampled_from": sampled_from,
            "columns": cols,
            "rows": [[_cell(v) for v in r] for r in rows],
            "row_limit": PREVIEW_ROW_LIMIT,
            "truncated": len(rows) >= PREVIEW_ROW_LIMIT,
            "total_rows": table.no_rows or None,
        }

    async def completions(self, db: AsyncSession, connection: Connection) -> dict:
        """The editor's suggestion catalog for `connection`.

        Loads only the four fields a suggestion needs, bounded: the catalog
        can be tens of thousands of rows, each carrying pks/fks JSON.
        """
        from sqlalchemy.orm import load_only

        rows = (
            await db.execute(
                select(ConnectionTable)
                .options(load_only(
                    ConnectionTable.name, ConnectionTable.kind, ConnectionTable.columns,
                    ConnectionTable.metadata_json, ConnectionTable.deleted_at,
                ))
                .where(
                    ConnectionTable.connection_id == str(connection.id),
                    ConnectionTable.kind == KIND_TABLE,
                    ConnectionTable.deleted_at.is_(None),
                )
                .order_by(ConnectionTable.name)
                .limit(MAX_COMPLETION_TABLES)
            )
        ).scalars().all()
        return completion_catalog(connection.type, rows)



# ---------------------------------------------------------------------------
# Editor completions: the connection's tables and columns, each with the text
# to insert so it means that exact name in the source's dialect.
# ---------------------------------------------------------------------------

MAX_COMPLETION_TABLES = 5000
MAX_COMPLETION_COLUMNS = 500

_SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Words that are reserved in most SQL dialects. A name that is one of them is
# quoted on insert; quoting a non-reserved word is harmless, missing a reserved
# one is a syntax error, so this list errs toward quoting.
_RESERVED = frozenset(["all", "alter", "and", "any", "as", "asc", "between", "by", "case", "cast", "check", "column", "constraint", "create", "cross", "current", "current_date", "current_time", "current_timestamp", "current_user", "date", "default", "delete", "desc", "distinct", "drop", "else", "end", "except", "exists", "false", "fetch", "for", "foreign", "from", "full", "grant", "group", "having", "in", "index", "inner", "insert", "intersect", "interval", "into", "is", "join", "key", "left", "like", "limit", "natural", "not", "null", "of", "offset", "on", "or", "order", "outer", "over", "partition", "primary", "references", "right", "row", "rows", "select", "session_user", "set", "some", "table", "then", "time", "timestamp", "to", "top", "true", "union", "unique", "update", "user", "using", "values", "view", "when", "where", "window", "with"])


def insert_ident(spec: PreviewSpec, part: str) -> str:
    """`part` as it must be typed to mean exactly that name — bare when it can
    be, quoted when the dialect would otherwise fold its case, read it as a
    keyword, or stop at an odd character."""
    bare_ok = (
        bool(_SIMPLE_IDENT.match(part))
        and part.lower() not in _RESERVED
        and (
            spec.case_fold is None
            or (spec.case_fold == "lower" and part == part.lower())
            or (spec.case_fold == "upper" and part == part.upper())
        )
    )
    return part if bare_ok else _quote_ident(part, spec.quotes)


def completion_catalog(conn_type: str | None, tables) -> dict:
    """Tables and columns for the query editor's suggestions.

    Empty for sources whose query language the editor does not complete
    (DAX), and for types with no spec.
    """
    spec = spec_for(conn_type)
    if spec is None or spec.language == "dax":
        return {"language": spec.language if spec else "sql", "tables": []}
    out = []
    for t in tables:
        if t.kind != KIND_TABLE or getattr(t, "deleted_at", None) is not None:
            continue
        parts = name_parts(spec, t)
        cols = []
        for c in (t.columns or [])[:MAX_COMPLETION_COLUMNS]:
            name = c.get("name") if isinstance(c, dict) else None
            if isinstance(name, str) and name:
                cols.append({
                    "name": name,
                    "insert": insert_ident(spec, name),
                    "dtype": c.get("dtype"),
                })
        out.append({
            "name": t.name,
            "insert": ".".join(insert_ident(spec, p) for p in parts),
            "columns": cols,
        })
        if len(out) >= MAX_COMPLETION_TABLES:
            break
    return {"language": spec.language, "tables": out}


table_preview_service = TablePreviewService()
