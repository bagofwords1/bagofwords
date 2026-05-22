"""list_connections — training-mode connection catalog browse.

Mirrors the HTTP ``GET /api/connections`` visibility filter:
- admins (``manage_connections`` or ``full_admin_access``) see all
- members see connections they have a grant on, or connections backing a
  DataSource they can access (public + grants)
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Dict, Any, List, Type

from pydantic import BaseModel
from sqlalchemy import func, select

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolErrorEvent,
    ToolEvent,
    ToolStartEvent,
)
from app.ai.tools.schemas.list_connections import (
    ConnectionSummary,
    ListConnectionsInput,
    ListConnectionsOutput,
)


logger = logging.getLogger(__name__)


TOOL_PROVIDER_TYPES = {"mcp", "custom_api"}


class ListConnectionsTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_connections",
            description=(
                "List database / MCP / custom-API connections in this organization that the "
                "caller can see. Includes which agents already use each connection (the "
                "agent_names field) — useful for understanding what's already wired up before "
                "editing or creating an agent. Filter by type (e.g. 'postgresql', 'mcp', "
                "'custom_api'), name_search (substring), or only_tool_providers (restrict to "
                "mcp + custom_api)."
            ),
            category="research",
            version="1.0.0",
            input_schema=ListConnectionsInput.model_json_schema(),
            output_schema=ListConnectionsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["connection", "catalog", "training"],
            allowed_modes=["training"],
            examples=[
                {
                    "input": {"page_size": 50},
                    "description": "List all visible connections.",
                },
                {
                    "input": {"only_tool_providers": True},
                    "description": "MCP and custom-API connections only.",
                },
                {
                    "input": {"type": "postgresql"},
                    "description": "Just the Postgres connections.",
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
            data = ListConnectionsInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "name_search": data.name_search,
                "type": data.type,
                "only_tool_providers": data.only_tool_providers,
                "page": data.page,
                "page_size": data.page_size,
            },
        )

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        if not all([db, organization, user]):
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": "Missing runtime context.", "code": "MISSING_CONTEXT"},
            )
            return

        try:
            from app.core.permission_resolver import FULL_ADMIN, resolve_permissions
            from app.models.connection_table import ConnectionTable
            from app.models.connection_tool import ConnectionTool
            from app.models.data_source import DataSource
            from app.services.connection_service import ConnectionService

            connections = await ConnectionService().get_connections(db, organization)

            # Visibility filter (mirrors routes/connection.py:108-136)
            resolved = await resolve_permissions(
                db, str(user.id), str(organization.id)
            )
            is_admin = (
                FULL_ADMIN in resolved.org_permissions
                or resolved.has_org_permission("manage_connections")
            )

            if not is_admin:
                granted_conn_ids = {
                    rid
                    for (rtype, rid) in resolved.resource_permissions
                    if rtype == "connection"
                }
                granted_ds_ids = {
                    rid
                    for (rtype, rid) in resolved.resource_permissions
                    if rtype == "data_source"
                }
                public_rows = await db.execute(
                    select(DataSource.id).where(
                        DataSource.organization_id == str(organization.id),
                        DataSource.is_public.is_(True),
                    )
                )
                accessible_ds_ids = granted_ds_ids | {str(r) for (r,) in public_rows.all()}

                def _visible(c) -> bool:
                    if str(c.id) in granted_conn_ids:
                        return True
                    if c.data_sources:
                        return any(
                            str(ds.id) in accessible_ds_ids for ds in c.data_sources
                        )
                    return False

                connections = [c for c in connections if _visible(c)]

            # Apply filters
            needle = (data.name_search or "").strip().lower()
            type_filter = (data.type or "").strip().lower()
            filtered = []
            for c in connections:
                if needle and needle not in (c.name or "").lower():
                    continue
                if type_filter and c.type.lower() != type_filter:
                    continue
                if data.only_tool_providers and c.type not in TOOL_PROVIDER_TYPES:
                    continue
                filtered.append(c)

            total = len(filtered)
            start = (data.page - 1) * data.page_size
            end = start + data.page_size
            page = filtered[start:end]

            # Per-page counts (small N)
            out_rows: List[ConnectionSummary] = []
            for c in page:
                tc = await db.execute(
                    select(func.count(ConnectionTable.id)).where(
                        ConnectionTable.connection_id == str(c.id)
                    )
                )
                table_count = int(tc.scalar() or 0)

                if c.type in TOOL_PROVIDER_TYPES:
                    tlc = await db.execute(
                        select(func.count(ConnectionTool.id)).where(
                            ConnectionTool.connection_id == str(c.id)
                        )
                    )
                    tool_count = int(tlc.scalar() or 0)
                else:
                    tool_count = 0

                out_rows.append(
                    ConnectionSummary(
                        id=str(c.id),
                        name=c.name,
                        type=c.type,
                        auth_policy=c.auth_policy,
                        is_active=bool(c.is_active),
                        is_indexed=table_count > 0 or tool_count > 0,
                        table_count=table_count,
                        tool_count=tool_count,
                        agent_names=[ds.name for ds in (c.data_sources or [])],
                        last_synced_at=(
                            c.last_synced_at.isoformat()
                            if getattr(c, "last_synced_at", None)
                            and hasattr(c.last_synced_at, "isoformat")
                            else None
                        ),
                    )
                )

            output = ListConnectionsOutput(
                success=True, total=total, connections=out_rows
            )
            summary = f"Listed {len(out_rows)} connection(s) (total {total})."
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {"summary": summary},
                },
            )
        except Exception as e:
            logger.exception(f"list_connections failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": str(e), "code": "QUERY_FAILED"},
            )
