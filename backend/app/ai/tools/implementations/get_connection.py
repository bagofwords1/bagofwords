"""Get Connection Tool - One connection's catalog for agent planning (training mode).

Returns the connection-level catalog BEFORE any agent exists: tables grouped by
schema (tables/objects shapes), tools (MCP/API shapes), or the file scope +
indexed files (file shapes like network_dir / S3). Glob filtering + pagination
so large catalogs stay navigable. Gated on the same create-agents tier as
list_connections.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.get_connection import (
    ConnectionFileScope,
    ConnectionTableItem,
    ConnectionToolItem,
    GetConnectionInput,
    GetConnectionOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.implementations.connection_catalog_common import (
    FILE_SOURCE_TYPES,
    can_create_agents,
    can_create_on_connection,
    compile_patterns,
    conn_data_shape,
    file_scope_of,
    table_schema_of,
)

logger = logging.getLogger(__name__)


class GetConnectionTool(Tool):
    """Inspect one connection's tables / tools / files catalog."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="get_connection",
            description=(
                "RESEARCH: Inspect ONE connection's catalog before creating an agent on it — "
                "tables grouped by schema (databases/warehouses), tools (MCP/API connections), "
                "or the file scope + indexed files (network_dir/S3/drive connections). Supports "
                "a case-insensitive glob `pattern` (e.g. 'sales.*', '*invoice*'), a "
                "`schema_name` filter, and pagination for large catalogs. Use the result to "
                "pick the `schemas`/`tables`/`tools` selection for create_agent."
            ),
            category="research",
            version="1.0.0",
            input_schema=GetConnectionInput.model_json_schema(),
            output_schema=GetConnectionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=["create_data_source"],
            tags=["training", "connection", "catalog", "agent-building"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"connection_id": "<uuid>", "schema_name": "sales"},
                    "description": "All tables in the 'sales' schema of a warehouse connection.",
                },
                {
                    "input": {"connection_id": "<uuid>", "pattern": "*invoice*"},
                    "description": "Tables (or tools/files) whose name mentions invoice.",
                },
                {
                    "input": {"connection_id": "<uuid>", "pattern": "*.pdf", "page": 2},
                    "description": "Second page of indexed PDFs on a file connection.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return GetConnectionInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return GetConnectionOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = GetConnectionInput(**(tool_input or {}))
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "connection_id": data.connection_id,
                "pattern": data.pattern,
                "schema_name": data.schema_name,
                "page": data.page,
            },
        )

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")

        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={
                    "error": "Missing required runtime context (db, organization, user)",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            from sqlalchemy import select

            from app.core.permission_resolver import resolve_permissions
            from app.models.connection import Connection
            from app.models.connection_table import ConnectionTable
            from app.models.connection_tool import ConnectionTool
            from app.services.tool_policy_service import normalize_tool_policy

            conn_q = await db.execute(
                select(Connection).where(
                    Connection.id == data.connection_id,
                    Connection.organization_id == str(organization.id),
                )
            )
            conn = conn_q.scalar_one_or_none()
            if conn is None:
                yield self._reject(data, "Connection not found in this organization.", "connection_not_found")
                return

            resolved = await resolve_permissions(db, str(user.id), str(organization.id))
            if not (can_create_agents(resolved) and can_create_on_connection(resolved, str(conn.id))):
                yield self._reject(
                    data,
                    f"You don't have create-agent access on connection '{conn.name}'.",
                    "permission_denied",
                )
                return

            shape = conn_data_shape(conn.type)
            is_file_shaped = shape == "files" or conn.type in FILE_SOURCE_TYPES
            compiled = compile_patterns([data.pattern] if data.pattern else None)

            def _matches(name: str) -> bool:
                return not compiled or bool(name and compiled[0][1].match(name))

            output = GetConnectionOutput(
                success=True,
                connection_id=str(conn.id),
                name=conn.name,
                type=conn.type,
                data_shape="files" if is_file_shaped else shape,
                page=data.page,
                page_size=data.page_size,
            )
            start = (data.page - 1) * data.page_size
            end = start + data.page_size

            if shape == "tools":
                tool_q = await db.execute(
                    select(ConnectionTool)
                    .where(ConnectionTool.connection_id == str(conn.id))
                    .order_by(ConnectionTool.name)
                )
                rows = [t for t in tool_q.scalars().all() if _matches(t.name or "")]
                output.total = len(rows)
                output.has_more = end < len(rows)
                output.tools = [
                    ConnectionToolItem(
                        name=t.name,
                        description=t.description,
                        default_enabled=bool(t.is_enabled),
                        default_policy=normalize_tool_policy(t.policy),
                    )
                    for t in rows[start:end]
                ]
                summary = (
                    f"Connection '{conn.name}' ({conn.type}, tools): "
                    f"{output.total} tool(s) match; showing {len(output.tools)} (page {data.page})"
                )
            elif is_file_shaped:
                output.file_scope = ConnectionFileScope(**file_scope_of(conn))
                f_q = await db.execute(
                    select(ConnectionTable.name)
                    .where(ConnectionTable.connection_id == str(conn.id))
                    .order_by(ConnectionTable.name)
                )
                names = [n for (n,) in f_q.all() if _matches(n or "")]
                output.total = len(names)
                output.has_more = end < len(names)
                output.files = names[start:end]
                scope = output.file_scope
                scope_txt = (
                    "scope defined by each user's signed-in account"
                    if scope.token_scoped
                    else f"base={scope.base or 'n/a'}, globs={scope.include_globs or ['(all)']}"
                )
                summary = (
                    f"Connection '{conn.name}' ({conn.type}, files): {scope_txt}; "
                    f"{output.total} indexed file(s) match; showing {len(output.files)} (page {data.page})"
                )
            else:
                t_q = await db.execute(
                    select(
                        ConnectionTable.id,
                        ConnectionTable.name,
                        ConnectionTable.no_rows,
                        ConnectionTable.metadata_json,
                    )
                    .where(ConnectionTable.connection_id == str(conn.id))
                    .order_by(ConnectionTable.name)
                )
                rows = t_q.all()

                class _Row:
                    __slots__ = ("id", "name", "no_rows", "metadata_json")

                    def __init__(self, rid, name, no_rows, metadata_json):
                        self.id, self.name, self.no_rows, self.metadata_json = rid, name, no_rows, metadata_json

                all_rows = [_Row(*r) for r in rows]
                all_schemas = sorted({s for r in all_rows if (s := table_schema_of(r))})
                output.schemas = all_schemas

                wanted_schema = (data.schema_name or "").strip().lower()
                filtered = []
                for r in all_rows:
                    schema = table_schema_of(r)
                    if wanted_schema and (schema or "").lower() != wanted_schema:
                        continue
                    if not _matches(r.name or ""):
                        continue
                    filtered.append((r, schema))

                output.total = len(filtered)
                output.has_more = end < len(filtered)
                page_rows = filtered[start:end]

                # Column counts only for the page (columns JSON is heavy).
                col_counts: Dict[str, int] = {}
                page_ids = [str(r.id) for r, _ in page_rows]
                if page_ids:
                    c_q = await db.execute(
                        select(ConnectionTable.id, ConnectionTable.columns).where(
                            ConnectionTable.id.in_(page_ids)
                        )
                    )
                    for rid, cols in c_q.all():
                        col_counts[str(rid)] = len(cols) if isinstance(cols, list) else 0

                output.tables = [
                    ConnectionTableItem(
                        name=r.name,
                        schema_name=schema,
                        column_count=col_counts.get(str(r.id), 0),
                        row_count=r.no_rows,
                    )
                    for r, schema in page_rows
                ]
                summary = (
                    f"Connection '{conn.name}' ({conn.type}, {shape}): {output.total} table(s) match "
                    f"(schemas: {', '.join(all_schemas) or 'none'}); showing {len(output.tables)} (page {data.page})"
                )

            if output.total == 0 and (data.pattern or data.schema_name):
                summary += " — nothing matched the filter; retry without it or with a broader glob"
            output.message = summary

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "connection_catalog",
                                "connection_id": str(conn.id),
                                "name": conn.name,
                                "data_shape": output.data_shape,
                                "total": output.total,
                                "schemas": output.schemas,
                                "tables": [t.model_dump() for t in output.tables],
                                "tools": [t.model_dump() for t in output.tools],
                                "files": output.files,
                                "file_scope": output.file_scope.model_dump() if output.file_scope else None,
                                "page": output.page,
                                "has_more": output.has_more,
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"get_connection failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Connection lookup failed: {e}", "code": "GET_FAILED"},
            )

    @staticmethod
    def _reject(data: GetConnectionInput, message: str, reason: str) -> ToolEndEvent:
        return ToolEndEvent(
            type="tool.end",
            payload={
                "output": GetConnectionOutput(
                    success=False,
                    connection_id=data.connection_id,
                    message=message,
                    rejected_reason=reason,
                ).model_dump(),
                "observation": {"summary": f"get_connection rejected: {message}", "artifacts": []},
            },
        )
