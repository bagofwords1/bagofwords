"""get_connection — training-mode connection detail.

Thin wrapper over ``AgentCatalogService.get_connection``. Requires
``manage_connections`` (enforced both by metadata gating and inside
the service).
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolErrorEvent,
    ToolEvent,
    ToolStartEvent,
)
from app.ai.tools.schemas.get_connection import (
    GetConnectionInput,
    GetConnectionOutput,
)


logger = logging.getLogger(__name__)


class GetConnectionTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="get_connection",
            description=(
                "Fetch full detail for one connection by name. Returns the connection's "
                "config (credentials stripped), indexed table list (top-N, optional column "
                "previews), tools (for mcp/custom_api connections), and the agents using "
                "it. Use this in training mode when drafting a new agent to explore what's "
                "available on a connection before linking it. Requires manage_connections "
                "permission."
            ),
            category="research",
            version="1.0.0",
            input_schema=GetConnectionInput.model_json_schema(),
            output_schema=GetConnectionOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=30,
            idempotent=True,
            required_permissions=["manage_connections"],
            tags=["connection", "detail", "training", "schema"],
            allowed_modes=["training"],
            examples=[
                {"input": {"name": "postgres-prod"}, "description": "Top 50 tables on postgres-prod."},
                {"input": {"name": "postgres-prod", "with_columns": True, "table_limit": 20}, "description": "20 tables with columns."},
                {"input": {"name": "postgres-prod", "table_search": "invoice"}, "description": "Tables matching 'invoice'."},
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
            data = GetConnectionInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={
                "name": data.name,
                "table_limit": data.table_limit,
                "with_columns": data.with_columns,
                "with_tools": data.with_tools,
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
            from app.services.agent_catalog_service import AgentCatalogService

            output = await AgentCatalogService().get_connection(
                db,
                organization,
                user,
                name=data.name,
                table_search=data.table_search,
                table_limit=data.table_limit,
                with_columns=data.with_columns,
                with_tools=data.with_tools,
                enforce_manage_connections=True,
            )
            if not output.success:
                # Service-level not-found or access denied.
                code = (
                    "FORBIDDEN"
                    if (output.error_message or "").startswith("Access denied")
                    else "tool.end"
                )
                if code == "FORBIDDEN":
                    yield ToolErrorEvent(
                        type="tool.error",
                        payload={"error": output.error_message, "code": "FORBIDDEN"},
                    )
                    return
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {"summary": output.error_message or "connection unavailable"},
                    },
                )
                return

            conn = output.connection
            summary = (
                f"Loaded connection '{conn.name}' "
                f"({conn.type}, {conn.tables_total} tables, {conn.tools_total} tools, "
                f"{len(conn.agents)} agents)."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {"summary": summary},
                },
            )
        except Exception as e:
            logger.exception(f"get_connection failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": str(e), "code": "QUERY_FAILED"},
            )
