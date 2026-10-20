"""MCP Tools - External API for LLM integrations (Claude, Cursor, etc.)

MCP tools are fully separate from internal planner tools.
They can wrap internal services/tools as needed.
"""

from .create_report import CreateReportTool
from .get_context import GetContextTool
from .inspect_data import InspectDataMCPTool
from .create_data import CreateDataMCPTool
from .create_artifact import CreateArtifactMCPTool
from .edit_artifact import EditArtifactMCPTool
from .artifact_resources import ManageArtifactResourcesMCPTool
from .send_email import SendEmailMCPTool
from .list_agent_tools import ListAgentToolsMCPTool
from .execute_mcp import ExecuteMCPMCPTool
from .instructions import (
    ListInstructionsMCPTool,
    ReadInstructionMCPTool,
    CreateInstructionMCPTool,
    EditInstructionMCPTool,
    DeleteInstructionMCPTool,
)
from .schema_tools import DescribeTablesMCPTool, EditSchemaMCPTool
from .app_tools import GetVisualizationMCPTool, GetArtifactDataMCPTool

MCP_TOOLS = {
    "create_report": CreateReportTool,
    "get_context": GetContextTool,
    "inspect_data": InspectDataMCPTool,
    "create_data": CreateDataMCPTool,
    "create_artifact": CreateArtifactMCPTool,
    "edit_artifact": EditArtifactMCPTool,
    "manage_artifact_resources": ManageArtifactResourcesMCPTool,
    "send_email": SendEmailMCPTool,
    # Agent tool gateway (MCP servers + custom APIs attached to an agent)
    "list_agent_tools": ListAgentToolsMCPTool,
    "execute_mcp": ExecuteMCPMCPTool,
    # Instruction management tools
    "list_instructions": ListInstructionsMCPTool,
    "read_instruction": ReadInstructionMCPTool,
    "create_instruction": CreateInstructionMCPTool,
    "edit_instruction": EditInstructionMCPTool,
    "delete_instruction": DeleteInstructionMCPTool,
    # Agent schema tools (read: anyone who can see the agent; write: agent managers)
    "describe_tables": DescribeTablesMCPTool,
    "edit_schema": EditSchemaMCPTool,
    # App-only tools (hidden from LLM, used by MCP App UIs)
    "get_visualization": GetVisualizationMCPTool,
    "get_artifact_data": GetArtifactDataMCPTool,
}


def get_mcp_tool(name: str):
    """Get an MCP tool class by name."""
    return MCP_TOOLS.get(name)


def list_mcp_tools(include_app_only: bool = True):
    """List available MCP tools with their schemas.

    Args:
        include_app_only: If True, includes all tools. If False, excludes
            tools with visibility=["app"] (for tools/list sent to the LLM).
    """
    tools = []
    for tool_cls in MCP_TOOLS.values():
        tool = tool_cls()
        if not tool.is_available:
            continue
        if not include_app_only and "model" not in tool.visibility:
            continue
        tools.append(tool.to_schema())
    return tools
