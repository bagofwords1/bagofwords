"""Sample rows from one introspected table on a connection.

Lets a connection manager look at real rows before deciding which tables an
agent should use, and gives the "create custom table" flow a starting query.

The query is built here, never taken from the client: only a table the catalog
already knows can be previewed, and its identifier is quoted for the source's
dialect. The row bound is written into the query itself, in the source's own
syntax (``TOP 50`` / ``LIMIT 50`` / ``ROWNUM``), so the admin sees exactly what
runs and can edit it.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data_sources.fast import extractor
from app.errors import AppError
from app.errors.codes import ErrorCode
from app.models.connection import Connection
from app.models.connection_table import KIND_TABLE, ConnectionTable
from app.models.user import User

logger = logging.getLogger(__name__)

PREVIEW_ROW_LIMIT = 50

# Identifier quote style per stored `Connection.type` (lower-cased). Only SQL
# sources whose catalog rows are real, SELECT-able relations are listed: Power
# BI (DAX) and PostHog (HogQL) are not, so they are left out on purpose.
_DOUBLE = ('"', '"')
_BACKTICK = ("`", "`")
_BRACKET = ("[", "]")
_QUOTES = {
    "postgresql": _DOUBLE,
    "oracledb": _DOUBLE,
    "sqlite": _DOUBLE,
    "snowflake": _DOUBLE,
    "mysql": _BACKTICK,
    "mariadb": _BACKTICK,
    "clickhouse": _BACKTICK,
    "bigquery": _BACKTICK,
    "mssql": _BRACKET,
    "ms_fabric": _BRACKET,
    "sybase": _BRACKET,
}

# How each dialect writes "first N rows" for a plain single-table SELECT.
# Oracle uses ROWNUM rather than FETCH FIRST so 11g sources work (the same
# choice as sql_dialect.bounded_sql). Sybase ASE has had TOP since 12.5.3.
_TOP_TYPES = {"mssql", "ms_fabric", "sybase"}
_ROWNUM_TYPES = {"oracledb"}

# Keys the clients use in `metadata_json` to record a table's namespace, which
# is the prefix of its stored "<namespace>.<table>" name.
_NAMESPACE_KEYS = ("schema", "database", "dataset")


def is_previewable_type(conn_type: Optional[str]) -> bool:
    return (conn_type or "").lower() in _QUOTES


def is_previewable_table(conn_type: Optional[str], table: ConnectionTable) -> bool:
    if table.kind != KIND_TABLE or not is_previewable_type(conn_type):
        return False
    # A Snowflake semantic view is queried through SEMANTIC_VIEW(...), not
    # SELECT * — a plain select would only fail.
    meta = table.metadata_json or {}
    return meta.get("type") != "semantic_view"


def _quote_ident(part: str, quotes: tuple[str, str]) -> str:
    open_q, close_q = quotes
    # Escape the closing quote by doubling it — the rule in every dialect above.
    return f"{open_q}{part.replace(close_q, close_q * 2)}{close_q}"


def quoted_table_ref(conn_type: str, table: ConnectionTable) -> str:
    """The table's stored name as a quoted, dialect-correct reference.

    Names are stored as ``<namespace>.<table>`` when the client records a
    namespace, and bare otherwise (SQLite, MySQL). The split uses the recorded
    namespace rather than the first dot, so a table whose own name contains a
    dot still resolves to the right relation.
    """
    quotes = _QUOTES[(conn_type or "").lower()]
    name = table.name
    meta = table.metadata_json or {}
    for key in _NAMESPACE_KEYS:
        ns = meta.get(key)
        if ns and name.startswith(f"{ns}."):
            return ".".join(
                (_quote_ident(ns, quotes), _quote_ident(name[len(ns) + 1:], quotes))
            )
    return _quote_ident(name, quotes)


def base_table_sql(conn_type: str, table: ConnectionTable) -> str:
    """The whole table, unbounded — the starting point for a custom table."""
    return f"SELECT * FROM {quoted_table_ref(conn_type, table)}"


def preview_sql(conn_type: str, table: ConnectionTable, limit: int) -> str:
    """The default preview query: the table's first ``limit`` rows, with the
    bound written in the dialect's own syntax so it is visible and editable."""
    ref = quoted_table_ref(conn_type, table)
    n = int(limit)
    t = (conn_type or "").lower()
    if t in _TOP_TYPES:
        return f"SELECT TOP {n} * FROM {ref}"
    if t in _ROWNUM_TYPES:
        return f"SELECT * FROM {ref} WHERE ROWNUM <= {n}"
    return f"SELECT * FROM {ref} LIMIT {n}"


_MAX_REASON_CHARS = 300


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


def _jsonable(v):
    import datetime as _dt
    import decimal

    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return v.decode("utf-8", "replace")
    return str(v)


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

    async def preview(
        self,
        db: AsyncSession,
        connection: Connection,
        table: ConnectionTable,
        current_user: Optional[User] = None,
    ) -> dict:
        if not is_previewable_table(connection.type, table):
            raise AppError.bad_request(
                ErrorCode.TABLE_PREVIEW_UNSUPPORTED,
                f"Data preview is not available for '{connection.type}' connections.",
                type=connection.type,
            )

        sql = preview_sql(connection.type, table, PREVIEW_ROW_LIMIT)
        client = await self.connection_service.construct_client(db, connection, current_user)

        try:
            # The query carries its own bound; the fetch bound is only a
            # backstop and never cuts below what the query already returns.
            cols, rows = await asyncio.to_thread(
                extractor.preview, client, sql, PREVIEW_ROW_LIMIT
            )
        except AppError:
            raise
        except Exception as e:
            logger.info("table preview failed for connection %s: %s", connection.id, e)
            reason = _source_reason(e)
            raise AppError.bad_request(
                ErrorCode.TABLE_PREVIEW_FAILED,
                f"Preview failed: {reason}",
                reason=reason,
            )

        return {
            "table_id": str(table.id),
            "table_name": table.name,
            "sql": sql,
            "base_sql": base_table_sql(connection.type, table),
            "columns": cols,
            "rows": [[_jsonable(v) for v in r] for r in rows],
            "row_limit": PREVIEW_ROW_LIMIT,
            "truncated": len(rows) >= PREVIEW_ROW_LIMIT,
            "total_rows": table.no_rows or None,
        }


table_preview_service = TablePreviewService()
