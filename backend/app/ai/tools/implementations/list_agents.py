"""list_agents — training-mode catalog browse.

Mirrors the HTTP ``GET /api/agents`` filter (public + grants) via the
existing ``DataSourceService.get_data_sources`` helper. No tool-level
permission gate; visibility is enforced inside.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator, Dict, Any, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.list_agents import (
    AgentSummary,
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
                {
                    "input": {"page_size": 50},
                    "description": "List all agents the caller can see.",
                },
                {
                    "input": {"name_search": "revenue"},
                    "description": "Find agents with 'revenue' in the name.",
                },
                {
                    "input": {"type": "mcp"},
                    "description": "Only agents whose primary connection is MCP.",
                },
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
                payload={
                    "error": "Missing runtime context (db, organization, user).",
                    "code": "MISSING_CONTEXT",
                },
            )
            return

        try:
            from app.services.data_source_service import DataSourceService

            service = DataSourceService()
            items = await service.get_data_sources(db, user, organization)

            # In-memory filters — get_data_sources already enforces visibility.
            needle = (data.name_search or "").strip().lower()
            type_filter = (data.type or "").strip().lower()

            filtered: list = []
            for item in items:
                name = (item.name or "").lower()
                if needle and needle not in name:
                    continue
                if type_filter and (item.type or "").lower() != type_filter:
                    continue
                filtered.append(item)

            total = len(filtered)
            start = (data.page - 1) * data.page_size
            end = start + data.page_size
            page = filtered[start:end]

            agents = []
            for it in page:
                connections = getattr(it, "connections", None) or []
                table_count = sum(
                    int(getattr(c, "table_count", 0) or 0) for c in connections
                )
                agents.append(
                    AgentSummary(
                        id=str(it.id),
                        name=it.name,
                        description=getattr(it, "description", None),
                        type=getattr(it, "type", None),
                        is_public=bool(getattr(it, "is_public", False)),
                        connection_count=len(connections),
                        table_count=table_count,
                        created_at=(
                            it.created_at.isoformat()
                            if getattr(it, "created_at", None) and hasattr(it.created_at, "isoformat")
                            else (str(it.created_at) if getattr(it, "created_at", None) else None)
                        ),
                    )
                )

            output = ListAgentsOutput(success=True, total=total, agents=agents)
            summary = f"Listed {len(agents)} agent(s) (total {total})."
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": output.model_dump(),
                    "observation": {
                        "summary": summary,
                        "artifacts": [
                            {
                                "type": "agent_list",
                                "count": len(agents),
                                "total": total,
                            }
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
