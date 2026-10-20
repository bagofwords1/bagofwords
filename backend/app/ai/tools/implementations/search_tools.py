"""``search_tools``: find the MCP / custom-API tools that did not fit ``tools_max``.

The run's tool list carries up to ``tools_max`` MCP tools natively. The rest
are listed here, already filtered by the run's agents and the user's policy
(``mcp_tool_registry.build_mcp_tool_catalog``), and reached with ``call_tool``.
Offered only when such tools exist.
"""
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.implementations._search_mcps_query import filter_tools_by_query
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.search_tools import SearchToolsInput, SearchToolsOutput


class _Entry:
    __slots__ = ("name", "description", "raw")

    def __init__(self, raw: Dict[str, Any]):
        self.raw = raw
        self.name = f"{raw.get('tool_name') or ''} {raw.get('name') or ''}"
        self.description = f"{raw.get('description') or ''} {raw.get('connection_name') or ''}"


class SearchToolsTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="search_tools",
            description=(
                "Find tools that are not in your tool list. The connected MCP / API servers "
                "have more tools than fit in it; search them by name or description and get "
                "each match's exact name and full input schema. Then call one with "
                "call_tool(name=..., arguments=...). Tools already in your tool list need no "
                "search: call those directly."
            ),
            category="research",
            version="1.0.0",
            input_schema=SearchToolsInput.model_json_schema(),
            output_schema=SearchToolsOutput.model_json_schema(),
            tags=["mcp", "tools", "discovery"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SearchToolsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SearchToolsOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        data = SearchToolsInput(**tool_input)
        yield ToolStartEvent(type="tool.start", payload={"title": data.title or "Searching tools"})

        deferred = list(runtime_ctx.get("deferred_mcp_tools") or [])
        entries = [_Entry(d) for d in deferred]
        matched = filter_tools_by_query(entries, data.query)
        shown = matched[: data.limit]
        tools = [{
            "name": e.raw["name"],
            "description": e.raw.get("description") or "",
            "connection_name": e.raw.get("connection_name") or "",
            "input_schema": e.raw.get("input_schema") or {"type": "object", "properties": {}},
            "policy": e.raw.get("policy") or "allow",
        } for e in shown]

        if not deferred:
            summary = "No tools beyond your tool list. Call the tools you have directly."
        elif len(matched) > len(shown):
            summary = (f"{len(matched)} tool(s) match; showing {len(shown)}. Narrow the query to see others. "
                       "Call one with call_tool(name=..., arguments=...).")
        else:
            summary = f"Found {len(shown)} tool(s). Call one with call_tool(name=..., arguments=...)."

        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"tools": tools, "total_count": len(matched)},
                "observation": {"summary": summary, "tools": tools, "success": bool(deferred)},
            },
        )
