"""
Lightweight mock MCP tool provider for testing.
Instead of running a real MCP server, this provides a MockToolProviderClient
that can be injected via monkeypatch to replace the real client construction.
"""
from typing import List, Dict, Any
from app.data_sources.clients.tool_provider_base import ToolProviderClient


# Default tool definitions for the mock server
DEFAULT_MOCK_TOOLS = [
    {
        "name": "echo",
        "description": "Returns the input as-is. Useful for basic call testing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Message to echo back"},
            },
            "required": ["message"],
        },
        "output_schema": {},
    },
    {
        "name": "get_records",
        "description": "Returns a list of sample records as tabular data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of records to return", "default": 5},
            },
        },
        "output_schema": {},
    },
    {
        "name": "search_docs",
        "description": "Search documents and return text content.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
        "output_schema": {},
    },
    {
        "name": "failing_tool",
        "description": "Always fails with an error. Useful for error handling testing.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
        "output_schema": {},
    },
]


class MockToolProviderClient(ToolProviderClient):
    """
    In-process mock that replaces the real MCP/API client for testing.
    No network calls, no subprocess — pure Python.
    """

    def __init__(self, tools: List[Dict[str, Any]] = None, **kwargs):
        self._tools = tools or DEFAULT_MOCK_TOOLS
        self._call_log: List[Dict[str, Any]] = []

    def list_tools(self) -> List[Dict[str, Any]]:
        return self._tools

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        self._call_log.append({"tool_name": tool_name, "arguments": arguments})

        if tool_name == "echo":
            return {
                "success": True,
                "data": {"echoed": arguments.get("message", "")},
                "content_type": "json",
                "error": None,
            }

        if tool_name == "get_records":
            count = arguments.get("count", 5)
            records = [
                {"id": i, "name": f"Record {i}", "status": "active" if i % 2 == 0 else "inactive"}
                for i in range(1, count + 1)
            ]
            return {
                "success": True,
                "data": records,
                "content_type": "tabular",
                "error": None,
            }

        if tool_name == "search_docs":
            query = arguments.get("query", "")
            return {
                "success": True,
                "data": f"Search results for '{query}':\n1. Document A - relevant content\n2. Document B - more content",
                "content_type": "text",
                "error": None,
            }

        if tool_name == "failing_tool":
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": "This tool always fails for testing purposes",
            }

        return {
            "success": False,
            "data": None,
            "content_type": "text",
            "error": f"Unknown tool: {tool_name}",
        }

    def test_connection(self) -> Dict[str, Any]:
        return {
            "success": True,
            "message": f"Mock MCP server connected. {len(self._tools)} tool(s) available.",
        }

    def set_tools(self, tools: List[Dict[str, Any]]):
        """Update the tools list (for testing upsert/delete scenarios)."""
        self._tools = tools

    @property
    def call_log(self) -> List[Dict[str, Any]]:
        """Access the log of all tool calls made."""
        return self._call_log
