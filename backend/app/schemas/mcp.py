"""MCP API schemas - request/response models for MCP endpoints."""

from dataclasses import dataclass, field
from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List, TYPE_CHECKING

# Reuse existing schemas
from app.ai.tools.schemas.create_widget import TablesBySource
from app.ai.tools.schemas.inspect_data import InspectDataOutput as BaseInspectDataOutput

if TYPE_CHECKING:
    from app.ai.context import ContextHub
    from app.schemas.settings import OrganizationSettings
    from app.models.llm_model import LLMModel


@dataclass
class MCPRichContext:
    """Rich context prepared for MCP tool execution.
    
    Contains all the context needed for code generation: schemas, instructions,
    resources, data source clients, and discovered tables.
    """
    # Core objects
    context_hub: "ContextHub"
    ds_clients: Dict[str, Any]
    org_settings: "OrganizationSettings"
    model: Optional["LLMModel"]
    
    # Discovered/resolved tables
    tables_by_source: List[Dict[str, Any]] = field(default_factory=list)
    
    # Rendered context strings (ready for prompt inclusion)
    schemas_excerpt: str = ""
    instructions_text: str = ""
    resources_text: str = ""
    files_text: str = ""
    
    # Data source connection status
    connected_sources: List[str] = field(default_factory=list)
    failed_sources: List[str] = field(default_factory=list)


class MCPToolSchema(BaseModel):
    """Schema for a single MCP tool in the catalog."""
    name: str
    description: str
    input_schema: Dict[str, Any]


class MCPToolsResponse(BaseModel):
    """Response for listing available MCP tools."""
    tools: List[MCPToolSchema]


# === get_context ===

class TableInfo(BaseModel):
    """Summary of a table for MCP response."""
    name: str
    columns: List[str]
    description: Optional[str] = None
    referenced_instructions_count: Optional[int] = None


class ToolInfo(BaseModel):
    """Summary of an MCP / custom-API tool exposed by an agent's connection.

    Name + description only (mirrors the internal ``<mcp_tools>`` block) to keep
    the overview small. Fetch full input schemas with ``list_agent_tools`` before
    calling ``execute_mcp``.
    """
    name: str
    description: Optional[str] = None
    connection_name: Optional[str] = None
    connection_type: Optional[str] = None
    policy: str = "allow"


class DataSourceInfo(BaseModel):
    """Summary of a data source with its tables."""
    id: str
    name: str
    type: Optional[str] = None
    tables: List[TableInfo]
    tools: List[ToolInfo] = Field(default_factory=list, description="MCP / custom-API tools this agent can run via execute_mcp.")


class ResourceInfo(BaseModel):
    """Summary of a metadata resource."""
    name: str
    resource_type: str
    description: Optional[str] = None


class GetContextInput(BaseModel):
    """Input for get_context MCP tool."""
    report_id: str = Field(..., description="Session ID from create_report. Required.")
    patterns: Optional[List[str]] = Field(default=None, description="Regex patterns to filter tables/resources.")


class GetContextOutput(BaseModel):
    """Output for get_context MCP tool."""
    report_id: str
    data_sources: List[DataSourceInfo]
    resources: List[ResourceInfo]
    tools_hint: Optional[str] = Field(default=None, description="Set when agents expose tools: how to get their full input schemas.")


# === list_agent_tools (gateway discovery) ===

class MCPListAgentToolsInput(BaseModel):
    """Input for the list_agent_tools MCP tool."""
    report_id: Optional[str] = Field(default=None, description="Scope to the agents attached to this report. Provide this OR data_source_ids.")
    data_source_ids: Optional[List[str]] = Field(default=None, description="Scope to specific agent (data source) IDs. Provide this OR report_id.")
    query: Optional[str] = Field(default=None, description="Optional case-insensitive substring to filter tools by name/description.")


class AgentToolDetail(BaseModel):
    """A single tool with its full input schema, ready for execute_mcp."""
    name: str
    description: Optional[str] = None
    data_source_id: str
    connection_id: str
    connection_name: Optional[str] = None
    connection_type: Optional[str] = None
    policy: str = "allow"
    input_schema: Dict[str, Any] = Field(default_factory=dict)


class MCPListAgentToolsOutput(BaseModel):
    """Output for the list_agent_tools MCP tool."""
    tools: List[AgentToolDetail]
    total_count: int


# === execute_mcp (gateway invocation) ===

class MCPExecuteToolInput(BaseModel):
    """Input for the execute_mcp MCP tool."""
    data_source_id: str = Field(..., description="The agent (data source) ID that owns the tool. From get_context / list_agent_tools.")
    tool_name: str = Field(..., description="Name of the tool to invoke.")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Arguments matching the tool's input_schema (from list_agent_tools).")
    connection_id: Optional[str] = Field(default=None, description="Optional: disambiguate when two connections on the agent expose a tool of the same name.")


class MCPExecuteToolOutput(BaseModel):
    """Output for the execute_mcp MCP tool."""
    success: bool
    content_type: Optional[str] = Field(default=None, description="tabular | text | json | binary.")
    connection_name: Optional[str] = None
    row_count: Optional[int] = Field(default=None, description="Number of rows when tabular.")
    result: Optional[Any] = Field(default=None, description="The tool result (tabular rows truncated to a preview; text/json inline).")
    truncated: bool = Field(default=False, description="True when 'result' is a truncated preview of a larger payload.")
    error_message: Optional[str] = None
    input_schema: Optional[Dict[str, Any]] = Field(default=None, description="The tool's declared input schema; returned on failure so you can retry with the right arguments.")


# === inspect_data ===

class MCPInspectDataInput(BaseModel):
    """Input for inspect_data MCP tool."""
    report_id: str = Field(..., description="Session ID from create_report. Required.")
    prompt: str = Field(..., description="What to inspect.")
    tables: Optional[List[TablesBySource]] = Field(default=None, description="Explicit tables. Auto-discovered if not provided.")


class MCPInspectDataOutput(BaseInspectDataOutput):
    """Output for inspect_data MCP tool. Extends base with report_id."""
    report_id: str
    url: Optional[str] = Field(default=None, description="Link to view the report. Always share this with the user.")


# === create_data ===

class MCPCreateDataInput(BaseModel):
    """Input for create_data MCP tool."""
    report_id: str = Field(..., description="Session ID from create_report. Required.")
    prompt: str = Field(..., description="What data to create.")
    title: Optional[str] = Field(default=None, description="Title for the visualization.")
    visualization_type: Optional[str] = Field(default=None, description="Chart type hint (table, bar_chart, line_chart, etc.).")
    tables: Optional[List[TablesBySource]] = Field(default=None, description="Explicit tables. Auto-discovered if not provided.")


class MCPCreateDataOutput(BaseModel):
    """Output for create_data MCP tool."""
    report_id: str
    query_id: Optional[str] = None
    visualization_id: Optional[str] = None
    success: bool
    data_preview: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    url: Optional[str] = Field(default=None, description="Link to view the report. Always share this with the user.")


# === list_instructions ===

class MCPListInstructionsInput(BaseModel):
    """Input for list_instructions MCP tool."""
    status: Optional[str] = Field(default=None, description="Filter by status: draft, published, archived")
    category: Optional[str] = Field(default=None, description="Filter by category: code_gen, data_modeling, general, dashboard, visualization")
    search: Optional[str] = Field(default=None, description="Search text in instruction title and content")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum number of results to return")


class MCPInstructionItem(BaseModel):
    """Single instruction item in list response."""
    id: str
    title: Optional[str] = None
    text: str
    category: str
    status: str
    load_mode: str
    source_type: str


class MCPListInstructionsOutput(BaseModel):
    """Output for list_instructions MCP tool."""
    instructions: List[MCPInstructionItem]
    total: int


# === create_instruction ===

class MCPCreateInstructionInput(BaseModel):
    """Input for create_instruction MCP tool."""
    text: str = Field(..., description="The instruction content that guides AI behavior")
    title: Optional[str] = Field(default=None, description="Optional title for the instruction")
    category: str = Field(default="general", description="Category: code_gen, data_modeling, general, dashboard, visualization")
    load_mode: str = Field(default="always", description="When to include in AI context: always, intelligent, disabled")
    data_source_ids: Optional[List[str]] = Field(default=None, description="Specific data source IDs this applies to. Empty/null means all data sources.")


class MCPCreateInstructionOutput(BaseModel):
    """Output for create_instruction MCP tool."""
    success: bool
    instruction_id: Optional[str] = None
    build_status: Optional[str] = Field(default=None, description="Build status: approved (live) or pending_approval (needs admin review)")
    requires_approval: bool = Field(default=False, description="True if instruction needs admin approval before going live")
    error_message: Optional[str] = None


# === delete_instruction ===

class MCPDeleteInstructionInput(BaseModel):
    """Input for delete_instruction MCP tool."""
    instruction_id: str = Field(..., description="ID of the instruction to delete")


class MCPDeleteInstructionOutput(BaseModel):
    """Output for delete_instruction MCP tool."""
    success: bool
    error_message: Optional[str] = None


# === create_artifact ===

class MCPCreateArtifactInput(BaseModel):
    """Input for create_artifact MCP tool.

    Creates a dashboard or slide presentation from existing visualizations.
    Automatically selects all successful visualizations in the report (up to 10).
    """
    report_id: str = Field(..., description="Report ID (required). Must have visualizations created via create_data.")
    prompt: str = Field(..., description="Goal for the dashboard/presentation. Describe what insights to highlight, layout preferences, or specific visualizations to feature.")
    title: Optional[str] = Field(default=None, description="Title for the artifact. If not provided, one will be generated.")
    mode: str = Field(default="page", description="Artifact mode: 'page' for interactive dashboards, 'slides' for presentation decks (exportable to PPTX).")


class MCPCreateArtifactOutput(BaseModel):
    """Output for create_artifact MCP tool."""
    report_id: str
    artifact_id: Optional[str] = None
    success: bool
    visualization_count: Optional[int] = Field(default=None, description="Number of visualizations included in the artifact.")
    visualization_ids: Optional[List[str]] = Field(default=None, description="IDs of visualizations included.")
    mode: Optional[str] = None
    error_message: Optional[str] = None
    url: Optional[str] = Field(default=None, description="Link to view the artifact. Always share this with the user.")


# === edit_artifact ===

class MCPEditArtifactInput(BaseModel):
    """Input for edit_artifact MCP tool.

    Surgically edit an existing dashboard or artifact by applying targeted changes.
    Preserves existing design and only modifies what is requested.
    """
    report_id: str = Field(..., description="Report ID containing the artifact.")
    artifact_id: str = Field(..., description="ID of the existing artifact to edit.")
    edit_instruction: str = Field(..., description="Natural language description of the change. E.g., 'Remove the filter bar', 'Make the revenue chart blue'.")
    visualization_ids: Optional[List[str]] = Field(default=None, description="Optional list of NEW visualization IDs to add. Existing ones are kept automatically.")
    title: Optional[str] = Field(default=None, description="Updated title. If not provided, existing title is kept.")


class MCPEditArtifactOutput(BaseModel):
    """Output for edit_artifact MCP tool."""
    report_id: str
    artifact_id: Optional[str] = None
    success: bool
    version: Optional[int] = Field(default=None, description="New version number after edit.")
    diff_applied: Optional[bool] = Field(default=None, description="True if surgical diff was applied, False if fell back to full rewrite.")
    error_message: Optional[str] = None
    url: Optional[str] = Field(default=None, description="Link to view the edited artifact. Always share this with the user.")


# ── send_email ─────────────────────────────────────────────────────

# Reuse the attachment spec / result shapes from the internal tool so the MCP
# surface and the agent tool stay in lockstep.
from app.ai.tools.schemas.send_email import (  # noqa: E402
    EmailAttachmentSpec,
    SendEmailAttachmentResult,
)


class MCPSendEmailInput(BaseModel):
    """Input for the send_email MCP tool.

    Sends a free-form email to the authenticated user, and optionally to other
    members of their organization. The requesting user is ALWAYS included, and
    every recipient also gets an in-app notification. ``recipients`` may only
    name members (or pending invites) of the caller's org — outside addresses
    are rejected.
    """
    subject: str = Field(..., min_length=1, max_length=300, description="A clear, specific subject line.")
    body: str = Field(..., min_length=1, description="The email body. Plain text by default; keep it short and natural.")
    body_format: str = Field(default="text", description="'text' (default) or 'html'. Use 'html' only when light structure genuinely helps.")
    recipients: List[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "Optional email addresses of OTHER organization members to also notify. "
            "Each must belong to a member/invite of the caller's org; outside addresses "
            "are rejected. You are always included automatically — leave empty to email "
            "only yourself."
        ),
    )
    report_id: Optional[str] = Field(default=None, description="Report ID that owns any attachments. Required when 'attachments' is non-empty; attachments are scoped to this report.")
    attachments: List[EmailAttachmentSpec] = Field(
        default_factory=list,
        max_length=5,
        description="Optional files to attach (max 5), referenced by visualization_id / query_id / artifact_id / file_id from the report. Requires report_id.",
    )


class MCPSendEmailOutput(BaseModel):
    """Output for the send_email MCP tool."""
    success: bool
    recipient: Optional[str] = None
    recipients: List[str] = Field(default_factory=list, description="All addresses reached (self + members).")
    subject: Optional[str] = None
    attachments: List[SendEmailAttachmentResult] = Field(default_factory=list)
    rejected: List[str] = Field(default_factory=list, description="Requested addresses that are not org members and were skipped.")
    error: Optional[str] = None
