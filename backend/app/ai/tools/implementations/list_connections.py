"""List Connections Tool - Show connections the caller can build agents on (training mode).

Returns only connections where the caller holds the create-agents tier
(per-connection `create_data_sources`, implied by connection admin grants, org
`manage_connections`, or full admin) AND the org-level `create_data_source`
permission — the exact pair POST /data_sources enforces. Summary-only by
design: use get_connection for the table/tool/file catalog of one connection.
"""

from typing import AsyncIterator, Dict, Any, Type
from pydantic import BaseModel
import logging

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.list_connections import (
    ListConnectionsInput,
    ListConnectionsItem,
    ListConnectionsOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)
from app.ai.tools.implementations.connection_catalog_common import (
    can_create_agents,
    can_create_on_connection,
    compile_patterns,
    conn_data_shape,
)

logger = logging.getLogger(__name__)


class ListConnectionsTool(Tool):
    """List the org connections the caller can build a new agent on."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_connections",
            description=(
                "RESEARCH: List the organization's connections you can build a NEW agent on "
                "(the caller must hold create-agent access on the connection). Returns name, "
                "type, shape (tables/files/tools), schema names, table/tool counts and the "
                "agents already built on each connection. Use BEFORE create_agent; use "
                "get_connection to inspect one connection's full table/tool/file catalog."
            ),
            category="research",
            version="1.0.0",
            input_schema=ListConnectionsInput.model_json_schema(),
            output_schema=ListConnectionsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=["create_data_source"],
            tags=["training", "connection", "agent-building"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {},
                    "description": "List every connection the caller can build an agent on.",
                },
                {
                    "input": {"search": "snow*"},
                    "description": "Find Snowflake-ish connections by glob.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListConnectionsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListConnectionsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = ListConnectionsInput(**(tool_input or {}))
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"search": data.search, "limit": data.limit},
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
            from sqlalchemy import func, select
            from sqlalchemy.orm import selectinload

            from app.core.permission_resolver import resolve_permissions
            from app.models.connection import Connection
            from app.models.connection_table import ConnectionTable
            from app.models.connection_tool import ConnectionTool

            resolved = await resolve_permissions(db, str(user.id), str(organization.id))

            if not can_create_agents(resolved):
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": ListConnectionsOutput(
                            success=True,
                            connections=[],
                            total=0,
                            message=(
                                "You don't hold the create_data_source permission, so there are "
                                "no connections you can build an agent on."
                            ),
                        ).model_dump(),
                        "observation": {
                            "summary": "No buildable connections: caller lacks create_data_source.",
                            "artifacts": [],
                        },
                    },
                )
                return

            conn_q = await db.execute(
                select(Connection)
                .options(selectinload(Connection.data_sources))
                .where(Connection.organization_id == str(organization.id))
                .order_by(Connection.name)
            )
            connections = [
                c for c in conn_q.scalars().all()
                if can_create_on_connection(resolved, str(c.id))
            ]

            compiled = compile_patterns([data.search] if data.search else None)
            if compiled:
                connections = [
                    c for c in connections
                    if compiled[0][1].match(c.name or "") or compiled[0][1].match(c.type or "")
                ]

            total = len(connections)
            connections = connections[: data.limit]
            conn_ids = [str(c.id) for c in connections]

            table_counts: Dict[str, int] = {}
            tool_counts: Dict[str, int] = {}
            schemas_by_conn: Dict[str, list] = {}
            if conn_ids:
                t_rows = await db.execute(
                    select(ConnectionTable.connection_id, func.count(ConnectionTable.id))
                    .where(ConnectionTable.connection_id.in_(conn_ids))
                    .group_by(ConnectionTable.connection_id)
                )
                table_counts = {str(cid): int(n or 0) for cid, n in t_rows.all()}

                tool_rows = await db.execute(
                    select(ConnectionTool.connection_id, func.count(ConnectionTool.id))
                    .where(ConnectionTool.connection_id.in_(conn_ids))
                    .group_by(ConnectionTool.connection_id)
                )
                tool_counts = {str(cid): int(n or 0) for cid, n in tool_rows.all()}

                # Distinct schema names per connection (portable JSON extraction).
                bind = db.get_bind()
                dialect_name = bind.dialect.name if bind else "sqlite"
                if dialect_name == "postgresql":
                    schema_expr = ConnectionTable.metadata_json.op("->>")("schema")
                else:
                    schema_expr = func.json_extract(ConnectionTable.metadata_json, "$.schema")
                s_rows = await db.execute(
                    select(ConnectionTable.connection_id, schema_expr)
                    .where(ConnectionTable.connection_id.in_(conn_ids))
                    .distinct()
                )
                for cid, schema in s_rows.all():
                    if schema:
                        schemas_by_conn.setdefault(str(cid), []).append(str(schema))

            items = []
            for c in connections:
                cid = str(c.id)
                shape = conn_data_shape(c.type)
                items.append(
                    ListConnectionsItem(
                        id=cid,
                        name=c.name,
                        type=c.type,
                        data_shape=shape,
                        auth_policy=getattr(c, "auth_policy", None),
                        schemas=sorted(schemas_by_conn.get(cid, [])) if shape in ("tables", "objects") else [],
                        table_count=table_counts.get(cid, 0),
                        tool_count=tool_counts.get(cid, 0),
                        linked_agents=sorted(
                            ds.name for ds in (c.data_sources or []) if getattr(ds, "name", None)
                        ),
                        can_create_agent=True,
                    )
                )

            msg = f"Found {len(items)} connection(s) you can build an agent on (total: {total})"
            output = ListConnectionsOutput(success=True, connections=items, total=total, message=msg)

            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": msg,
                        "artifacts": [
                            {
                                "type": "connection_list",
                                "count": len(items),
                                "total": total,
                                "items": [
                                    {
                                        "id": i.id,
                                        "name": i.name,
                                        "type": i.type,
                                        "data_shape": i.data_shape,
                                        "table_count": i.table_count,
                                        "tool_count": i.tool_count,
                                        "schemas": i.schemas,
                                        "linked_agents": i.linked_agents,
                                    }
                                    for i in items
                                ],
                            }
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"list_connections failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Listing connections failed: {e}", "code": "LIST_FAILED"},
            )
