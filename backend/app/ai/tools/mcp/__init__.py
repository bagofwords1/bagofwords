"""MCP Tools - External API for LLM integrations (Claude, Cursor, etc.)

MCP tools are fully separate from internal planner tools.
They can wrap internal services/tools as needed.
"""

from .create_report import CreateReportTool

MCP_TOOLS = {
    "create_report": CreateReportTool,
}


def get_mcp_tool(name: str):
    """Get an MCP tool class by name."""
    return MCP_TOOLS.get(name)


def list_mcp_tools():
    """List all available MCP tools with their schemas."""
    return [tool().to_schema() for tool in MCP_TOOLS.values()]
