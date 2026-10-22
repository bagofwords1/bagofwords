"""``call_tool``: call a tool found with ``search_tools``.

The agent rewrites a ``call_tool`` for a known name into the equivalent
``execute_mcp`` call before dispatch (``AgentV2._rewrite_native_mcp_action``),
so policy, per-user identity, result files and audit run on the one MCP path.
This implementation only runs when that rewrite found nothing: an unknown or
out-of-scope name.
"""
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import ToolEndEvent, ToolEvent
from app.ai.tools.schemas.call_tool import CallToolInput, CallToolOutput


class CallToolTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="call_tool",
            description=(
                "Call a tool that search_tools returned, by its exact name, with arguments "
                "matching the input_schema it returned. Only for tools found with "
                "search_tools: tools in your tool list are called directly by their own name."
            ),
            category="both",
            version="1.0.0",
            input_schema=CallToolInput.model_json_schema(),
            output_schema=CallToolOutput.model_json_schema(),
            tags=["mcp", "tools"],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CallToolInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return CallToolOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        name = (tool_input or {}).get("name") if isinstance(tool_input, dict) else None
        message = (
            f"Unknown tool '{name}'. Use a name exactly as search_tools returned it, "
            "or call a tool from your tool list directly."
        )
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": {"success": False, "message": message},
                "observation": {"summary": message, "success": False,
                                "error": {"code": "unknown_tool", "message": message}},
            },
        )
