"""list_agents — training-mode catalog browse.

Thin wrapper over ``AgentCatalogService.list_agents``.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Dict, Any, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.list_agents import (
    ListAgentsInput,
    ListAgentsOutput,
)
from app.ai.tools.schemas.events import (
    ToolEndEvent,
    ToolErrorEvent,
    ToolEvent,
    ToolStartEvent,
)


logger = logging.getLogger(__name__)


class ListAgentsTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="list_agents",
            description=(
                "List the agents (data sources) in this organization that the caller can see. "
                "Returns name, description, primary connection type, and counts. Use this in "
                "training mode to discover existing agents before fetching one with get_agent "
                "or before creating a new one. Filters: name_search (substring), type (e.g. "
                "'postgresql', 'mcp'), page / page_size."
            ),
            category="research",
            version="1.0.0",
            input_schema=ListAgentsInput.model_json_schema(),
            output_schema=ListAgentsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["agent", "catalog", "training"],
            allowed_modes=["training"],
            examples=[
                {"input": {"page_size": 50}, "description": "List all agents the caller can see."},
                {"input": {"name_search": "revenue"}, "description": "Find agents with 'revenue' in the name."},
                {"input": {"type": "mcp"}, "description": "Only agents whose primary connection is MCP."},
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return ListAgentsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListAgentsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = ListAgentsInput(**tool_input)
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

            output = await AgentCatalogService().list_agents(
                db,
                organization,
                user,
                name_search=data.name_search,
                type_filter=data.type,
                page=data.page,
                page_size=data.page_size,
            )
            summary = f"Listed {len(output.agents)} agent(s) (total {output.total})."
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {"type": "agent_list", "count": len(output.agents), "total": output.total}
                        ],
                    },
                },
            )
        except Exception as e:
            logger.exception(f"list_agents failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": str(e), "code": "QUERY_FAILED"},
            )
