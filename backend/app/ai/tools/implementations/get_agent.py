"""get_agent — training-mode agent detail.

Thin wrapper over ``AgentCatalogService.get_agent``.
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
from app.ai.tools.schemas.get_agent import GetAgentInput, GetAgentOutput


logger = logging.getLogger(__name__)


class GetAgentTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="get_agent",
            description=(
                "Fetch full structured detail for one agent by name. Returns connections, "
                "top-N active tables (default 50, ranked by relevance), per-agent tool "
                "overlay (mcp/custom_api connections only), conversation starters, and "
                "members. Use this after list_agents to inspect a candidate agent before "
                "editing via create_agent. The caller must have view permission on the "
                "agent (public agents are always visible to org members)."
            ),
            category="research",
            version="1.0.0",
            input_schema=GetAgentInput.model_json_schema(),
            output_schema=GetAgentOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=True,
            required_permissions=[],
            tags=["agent", "detail", "training"],
            allowed_modes=["training"],
            examples=[
                {"input": {"name": "revenue-analyst"}, "description": "Full detail for the agent."},
                {"input": {"name": "revenue-analyst", "table_limit": 20}, "description": "Top 20 tables only."},
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return GetAgentInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return GetAgentOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = GetAgentInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"name": data.name, "table_limit": data.table_limit},
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

            output = await AgentCatalogService().get_agent(
                db,
                organization,
                user,
                name=data.name,
                table_limit=data.table_limit,
            )
            if not output.success:
                # Service-side denial/not-found returns success=False with a
                # message. Surface as ToolEnd so the planner can read the
                # error and pick a different agent.
                yield ToolEndEvent(
                    type="tool.end",
                    payload={
                        "output": output.model_dump(),
                        "observation": {"summary": output.error_message or "agent unavailable"},
                    },
                )
                return

            agent = output.agent
            summary = (
                f"Loaded agent '{agent.name}' "
                f"({len(agent.connections)} conn / {agent.tables_total} tables / "
                f"{len(agent.tools)} tools / {len(agent.members)} members)."
            )
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {"summary": summary},
                },
            )
        except Exception as e:
            logger.exception(f"get_agent failed: {e}")
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": str(e), "code": "QUERY_FAILED"},
            )
