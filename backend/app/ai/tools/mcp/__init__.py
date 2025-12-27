"""MCP Tools - External API for LLM integrations (Claude, Cursor, etc.)

MCP tools are fully separate from internal planner tools.
They can wrap internal services/tools as needed.
"""

from .create_report import CreateReportTool
from .get_context import GetContextTool
from .inspect_data import InspectDataMCPTool
from .create_data import CreateDataMCPTool
from .instructions import (
    ListInstructionsMCPTool,
    CreateInstructionMCPTool,
    DeleteInstructionMCPTool,
)

MCP_TOOLS = {
    "create_report": CreateReportTool,
    "get_context": GetContextTool,
    "inspect_data": InspectDataMCPTool,
    "create_data": CreateDataMCPTool,
    # Instruction management tools
    "list_instructions": ListInstructionsMCPTool,
    "create_instruction": CreateInstructionMCPTool,
    "delete_instruction": DeleteInstructionMCPTool,
}


def get_mcp_tool(name: str):
    """Get an MCP tool class by name."""
    return MCP_TOOLS.get(name)


def list_mcp_tools():
    """List all available MCP tools with their schemas."""
    return [tool().to_schema() for tool in MCP_TOOLS.values()]
