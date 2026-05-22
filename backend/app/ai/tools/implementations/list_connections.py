"""list_connections — training-mode connection catalog browse.

Thin wrapper over ``AgentCatalogService.list_connections``.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Dict, Any, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolErrorEvent,
    ToolEvent,
    ToolStartEvent,
)
from app.ai.tools.schemas.list_connections import (
    ListConnectionsInput,
    ListConnectionsOutput,
)


logger = logging.getLogger(__name__)


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
                {"input": {"page_size": 50}, "description": "List all visible connections."},
                {"input": {"only_tool_providers": True}, "description": "MCP + custom-API only."},
                {"input": {"type": "postgresql"}, "description": "Postgres connections only."},
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
            from app.services.agent_catalog_service import AgentCatalogService

            output = await AgentCatalogService().list_connections(
                db,
                organization,
                user,
                name_search=data.name_search,
                type_filter=data.type,
                only_tool_providers=data.only_tool_providers,
                page=data.page,
                page_size=data.page_size,
            )
            summary = f"Listed {len(output.connections)} connection(s) (total {output.total})."
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
