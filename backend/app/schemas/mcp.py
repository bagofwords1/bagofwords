from app.schemas.artifact_resource_schema import ResourceDefinition
"""MCP API schemas - request/response models for MCP endpoints."""

from dataclasses import dataclass, field
from pydantic import BaseModel, Field
from typing import Dict, Any, Literal, Optional, List, TYPE_CHECKING

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
    # Published saved queries (catalog entities) on the agents in play, as the
    # planner sees them — so generated code reuses a saved definition instead
    # of inventing one.
    entities_text: str = ""
    
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


class EntityInfo(BaseModel):
    """Summary of a published saved query (catalog entity).

    Title, description and declared parameters only — the SQL and the rows come
    from ``describe_entity``.
    """
    id: str
    slug: Optional[str] = None
    type: str
    title: str
    description: Optional[str] = None
    agents: List[str] = Field(default_factory=list, description="Names of the agents (data sources) this saved query reads.")
    parameters: List[str] = Field(default_factory=list, description="Names of the parameters describe_entity accepts for it.")


class GetContextInput(BaseModel):
    """Input for get_context MCP tool."""
    report_id: str = Field(..., description="Session ID from create_report. Required.")
    patterns: Optional[List[str]] = Field(default=None, description="Regex patterns to filter tables/resources.")


class ContextInstructionInfo(BaseModel):
    """One instruction in the report's scope, as get_context lists it."""
    id: str
    title: Optional[str] = None
    kind: str = Field(default="instruction", description="'instruction' or 'skill' (a procedure to follow when it fits the request)")
    load_mode: str = Field(..., description="'always' (applies to every request) or 'intelligent' (applies when relevant)")
    required: bool = Field(..., description="True for 'always' instructions: they apply to every request in this report")
    text: Optional[str] = Field(
        default=None,
        description="Full text, for 'always' instructions that fit the inline budget. Unset: call read_instruction for it.",
    )
    description: Optional[str] = Field(default=None, description="One line on what it covers (unset when text is given)")
    tables: List[str] = Field(default_factory=list, description="Tables it is about")
    references: List[str] = Field(default_factory=list, description="Other objects it references, as 'kind:name'")
    tokens: Optional[int] = Field(default=None, description="Approximate size of the full text, in tokens")


class GetContextOutput(BaseModel):
    """Output for get_context MCP tool."""
    report_id: str
    data_sources: List[DataSourceInfo]
    resources: List[ResourceInfo]
    entities: List[EntityInfo] = Field(default_factory=list, description="Published saved queries available on these agents.")
    instructions: List[ContextInstructionInfo] = Field(
        default_factory=list,
        description=(
            "The organization's instructions that apply in this report: 'always' ones with their "
            "text (or required: true when too long to inline), the rest as entries to open with "
            "read_instruction."
        ),
    )
    instructions_total: int = Field(default=0, description="How many instructions are in scope")
    instructions_truncated: bool = Field(
        default=False,
        description="True when the list was capped: search the rest with list_instructions(search=...)",
    )
    instructions_hint: Optional[str] = Field(default=None, description="How to use the instructions list.")
    tools_hint: Optional[str] = Field(default=None, description="Set when agents expose tools: how to get their full input schemas.")
    entities_hint: Optional[str] = Field(default=None, description="Set when saved queries exist: use them instead of writing a definition from scratch.")


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
    instruction_ids: Optional[List[str]] = Field(
        default=None,
        max_length=50,
        description=(
            "Ids of instructions you read (from get_context / read_instruction) that apply "
            "to this request. Their full text is given to the code writer in addition to the "
            "rules it loads itself. Ids outside this report's scope are ignored."
        ),
    )


class MCPInspectDataOutput(BaseInspectDataOutput):
    """Output for inspect_data MCP tool. Extends base with report_id."""
    report_id: str
    url: Optional[str] = Field(default=None, description="Link to view the report. Always share this with the user.")


# === create_data ===

# Rows returned inline by create_data. The result lands in the caller's model
# context as one JSON text block, so it is bounded; the full result is always
# persisted in the report. The default is the org's `mcp_create_data_preview_rows`
# setting; the ceiling is a hard stop no org value can exceed.
MCP_CREATE_DATA_DEFAULT_PREVIEW_ROWS = 1000
MCP_CREATE_DATA_MAX_PREVIEW_ROWS = 10000


class MCPCreateDataInput(BaseModel):
    """Input for create_data MCP tool."""
    report_id: str = Field(..., description="Session ID from create_report. Required.")
    prompt: str = Field(..., description="What data to create.")
    title: Optional[str] = Field(default=None, description="Title for the visualization.")
    visualization_type: Optional[str] = Field(default=None, description="Chart type hint (table, bar_chart, line_chart, etc.).")
    tables: Optional[List[TablesBySource]] = Field(default=None, description="Explicit tables. Auto-discovered if not provided.")
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=MCP_CREATE_DATA_MAX_PREVIEW_ROWS,
        description=(
            "Maximum number of result rows to return in data_preview. Omit to get "
            "the organization's default; a larger value is capped at that default. "
            "The full result is always persisted in the report; data_preview.total_rows "
            "reports the true count and data_preview.truncated tells whether rows "
            "were left out."
        ),
    )
    instruction_ids: Optional[List[str]] = Field(
        default=None,
        max_length=50,
        description=(
            "Ids of instructions you read (from get_context / read_instruction) that apply "
            "to this request. Their full text is given to the code writer in addition to the "
            "rules it loads itself. Ids outside this report's scope are ignored."
        ),
    )


class MCPCreateDataOutput(BaseModel):
    """Output for create_data MCP tool."""
    report_id: str
    query_id: Optional[str] = None
    visualization_id: Optional[str] = None
    success: bool
    data_preview: Optional[Dict[str, Any]] = None
    data_notices: Optional[List[str]] = Field(
        default=None,
        description="Notes the data source attached to this result, e.g. that a row limit cut it off. "
        "When present, the result may be incomplete: never describe it as all of the data.",
    )
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
    text: str = Field(..., description="The instruction text — only its opening chars when text_truncated is true")
    text_truncated: bool = Field(
        default=False,
        description="True when `text` is shortened. Call read_instruction for the full text.",
    )
    text_length: int = Field(default=0, description="Length of the full instruction text, in chars")
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


# === read_instruction / edit_instruction ===

class MCPReadInstructionInput(BaseModel):
    """Input for read_instruction MCP tool.

    Long bodies are read in windows, like the in-app agent's read_instruction:
    a line range (offset/limit) or a line-level grep (grep_pattern + context).
    """
    instruction_id: Optional[str] = Field(
        default=None,
        description="ID of the instruction (from get_context or list_instructions). Give this or instruction_ids.",
    )
    instruction_ids: Optional[List[str]] = Field(
        default=None,
        min_length=1,
        max_length=20,
        description=(
            "Several instruction ids to read in one call (plain reads; a long one comes back as "
            "its opening lines). Ids that don't fit the size budget are returned in not_read_ids — "
            "read them in another call. Not combinable with offset / col_offset / limit / grep_pattern."
        ),
    )
    report_id: Optional[str] = Field(
        default=None,
        description=(
            "The report you're working in (from create_report). When set, only instructions in "
            "that report's scope (global, or on its agents) can be read, and the read is recorded "
            "for the report so its data tools apply what you read."
        ),
    )
    offset: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "RANGE READ: 1-based line to start from. Use it to read on from `next_offset` when "
            "a read came back with `truncated: true`. The lines are verbatim — safe to anchor "
            "edit_instruction on."
        ),
    )
    col_offset: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "RANGE READ: continue a line too long for one read — pass the `next_col_offset` "
            "a read returned, with `offset` set to that line. The text it returns continues "
            "the previous piece directly (no line break between them)."
        ),
    )
    limit: Optional[int] = Field(
        default=None,
        ge=1,
        le=2000,
        description="RANGE READ: number of lines from `offset` (default 400). Long lines can end the window earlier — continue from `next_offset`.",
    )
    grep_pattern: Optional[str] = Field(
        default=None,
        description=(
            "GREP READ: Python regex matched against each line, e.g. 'revenue|refund'. Returns "
            "matching lines (verbatim, 1-based line numbers) with before/after context. "
            "Takes precedence over offset/limit."
        ),
    )
    ignore_case: bool = Field(default=True, description="GREP READ: case-insensitive matching")
    before: int = Field(default=0, ge=0, le=20, description="GREP READ: context lines before each match")
    after: int = Field(default=0, ge=0, le=20, description="GREP READ: context lines after each match")
    max_matches: int = Field(default=50, ge=1, le=200, description="GREP READ: cap on returned matches")


class MCPInstructionAgentRef(BaseModel):
    id: str
    name: Optional[str] = None


class MCPInstructionDetail(BaseModel):
    id: str
    title: Optional[str] = None
    text: str
    category: Optional[str] = None
    status: Optional[str] = None
    load_mode: Optional[str] = None
    kind: Optional[str] = None
    state: Optional[str] = Field(default=None, description="active or inactive — what edit_instruction's status takes")
    agents: List[MCPInstructionAgentRef] = Field(
        default_factory=list, description="Agents this applies to. Empty means global (every agent).",
    )
    pending_review: bool = Field(default=False, description="True while a change waits for admin review")
    can_edit: bool = Field(default=False, description="Whether the caller may edit or delete it")

    # Window metadata — set only when `text` is not the whole instruction
    # (a long body's opening lines, a range read, or a grep read).
    read_mode: Optional[str] = Field(
        default=None,
        description="Unset = `text` is the whole instruction. 'head' (too long to return whole: opening lines + outline), 'range', or 'grep'",
    )
    total_chars: Optional[int] = Field(default=None, description="Length of the whole instruction text")
    total_lines: Optional[int] = Field(default=None, description="Line count of the whole instruction text")
    start_line: Optional[int] = Field(default=None, description="First line returned in `text`")
    end_line: Optional[int] = Field(default=None, description="Last line returned in `text`")
    truncated: Optional[bool] = Field(
        default=None,
        description="True when lines after `end_line` were not returned — read on with offset=`next_offset`",
    )
    next_offset: Optional[int] = Field(default=None, description="Pass as `offset` to continue reading")
    outline: Optional[str] = Field(default=None, description="Line-numbered outline of a long instruction, for picking a range or grep pattern")
    clipped_line: Optional[int] = Field(
        default=None,
        description="A line too long for one read: `text` holds one piece of it — read on with next_offset + next_col_offset",
    )
    clipped_line_length: Optional[int] = Field(default=None, description="Full length of `clipped_line`")
    start_col: Optional[int] = Field(default=None, description="Set when `text` starts mid-line (a col_offset read): the char it starts at")
    next_col_offset: Optional[int] = Field(
        default=None,
        description="Set when a line was too long for one read: pass as `col_offset` (with offset=`next_offset`) to read on",
    )
    matches: Optional[List[Any]] = Field(default=None, description="GREP READ: matching lines with context")
    grep_total_matches: Optional[int] = Field(default=None, description="GREP READ: total matches found")
    grep_truncated: Optional[bool] = Field(default=None, description="GREP READ: more matches exist than were returned")


class MCPReadInstructionOutput(BaseModel):
    success: bool
    instruction: Optional[MCPInstructionDetail] = None
    instructions: Optional[List[MCPInstructionDetail]] = Field(default=None, description="Set for an instruction_ids read")
    not_read_ids: Optional[List[str]] = Field(
        default=None, description="instruction_ids left out to stay within the size budget: read them in another call",
    )
    not_found_ids: Optional[List[str]] = Field(
        default=None, description="instruction_ids that don't exist, aren't visible to you, or are outside the report's scope",
    )
    message: Optional[str] = Field(default=None, description="What was read, and how to read on when it is partial")
    error_message: Optional[str] = None


class MCPEditInstructionInput(BaseModel):
    """Input for edit_instruction MCP tool — the in-app agent's anchored edit contract.

    Omitted fields stay unchanged. Unlike the in-app agent (which stages edits
    as suggestions for review), an MCP edit is published directly, exactly as
    if the caller saved it in the web app — so the caller needs the same rights.
    """
    instruction_id: str = Field(..., description="ID of the instruction to edit")
    old_text: Optional[str] = Field(
        default=None,
        description=(
            "REQUIRED whenever you pass `text`: an exact snippet of the CURRENT text, which `text` "
            "replaces. Must match exactly once — use a short unique phrase or sentence, not the "
            "whole instruction. \"\" is not an anchor. To ADD a rule, anchor the sentence it "
            "belongs after and repeat it verbatim at the start of `text`, then your addition. "
            "Several small anchored edits beat one rewrite."
        ),
    )
    replace_entire_text: bool = Field(
        default=False,
        description=(
            "Opt in to replacing the WHOLE instruction with `text`. Only for an explicit rewrite "
            "request; everything else uses `old_text`."
        ),
    )
    text: Optional[str] = Field(
        default=None, max_length=20000,
        description="Replacement for `old_text` (or the whole text with replace_entire_text). Write only the change asked for.",
    )
    title: Optional[str] = Field(default=None, max_length=200, description="New title")
    category: Optional[str] = Field(
        default=None, description="general, code_gen, visualization, dashboard or system",
    )
    load_mode: Optional[str] = Field(
        default=None,
        description="'always' or 'intelligent' (load when its tables are relevant). Not an off switch — use status.",
    )
    status: Optional[Literal["active", "inactive"]] = Field(
        default=None,
        description="Turn the instruction on ('active') or off ('inactive', kept but no longer loaded). Omit to leave unchanged.",
    )
    data_source_ids: Optional[List[str]] = Field(
        default=None,
        description="New agent scope (replaces it). [] makes it global, which needs org-level instruction admin.",
    )


class MCPEditInstructionOutput(BaseModel):
    success: bool
    instruction_id: Optional[str] = None
    build_status: Optional[str] = Field(
        default=None,
        description="approved (live), pending_approval, or draft (saved but not live: its build did not finalize)",
    )
    requires_approval: bool = False
    note: Optional[str] = Field(default=None, description="Set when the edit is saved but not live")
    previous_text: Optional[str] = None
    new_text: Optional[str] = Field(default=None, description="Full text after the edit — anchor your next edit on it")
    previous_status: Optional[str] = None
    new_status: Optional[str] = None
    instruction: Optional[MCPInstructionDetail] = None
    rejected_reason: Optional[str] = Field(
        default=None,
        description="anchor_required, anchor_not_found, ambiguous_anchor, result_too_short, invalid_*, "
                    "no_change, status_change_not_allowed, not_found, permission_denied",
    )
    current_text: Optional[str] = Field(default=None, description="Current full text, returned when an anchor fails")
    error_message: Optional[str] = None


# === describe_tables / edit_schema ===

class MCPDescribeTablesInput(BaseModel):
    """Input for describe_tables MCP tool."""
    data_source_id: str = Field(..., description="Agent (data source) ID")
    search: Optional[str] = Field(default=None, description="Filter tables by name")
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)
    include_columns: bool = Field(default=True, description="Include column names and types")


class MCPTableColumn(BaseModel):
    name: str
    dtype: Optional[str] = None
    description: Optional[str] = None


class MCPTableItem(BaseModel):
    id: Optional[str] = None
    name: str
    is_active: bool
    rows: Optional[int] = None
    connection_name: Optional[str] = None
    columns: Optional[List[MCPTableColumn]] = None
    excluded_columns: Optional[List[str]] = None


class MCPDescribeTablesOutput(BaseModel):
    success: bool
    data_source_id: Optional[str] = None
    can_edit_schema: bool = False
    tables: List[MCPTableItem] = Field(default_factory=list)
    total: int = 0
    selected_count: int = 0
    page: int = 1
    has_more: bool = False
    error_message: Optional[str] = None


class MCPEditSchemaInput(BaseModel):
    """Input for edit_schema MCP tool."""
    data_source_id: str = Field(..., description="Agent (data source) ID")
    activate: List[str] = Field(default_factory=list, description="Table names (or ids) the agent should use")
    deactivate: List[str] = Field(default_factory=list, description="Table names (or ids) the agent should stop using")
    excluded_columns: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Table id → COMPLETE list of column names to hide from the agent (replaces; [] shows all)",
    )


class MCPEditSchemaOutput(BaseModel):
    success: bool
    activated_count: int = 0
    deactivated_count: int = 0
    columns_updated_count: int = 0
    selected_count: Optional[int] = None
    error_message: Optional[str] = None


# === create_artifact ===

class MCPCreateArtifactInput(BaseModel):
    """Input for create_artifact MCP tool.

    Creates a dashboard or slide presentation from existing visualizations.
    Automatically selects all successful visualizations in the report (up to 10).
    """
    resources: Optional[List[ResourceDefinition]] = Field(default=None, max_length=50, description='Optional artifact resource definitions, configured through the same policy service as in-app authoring.')
    code: Optional[str] = Field(default=None, description='Optional complete artifact source; supplied code is validated by the standard authoring tool.')
    visualization_ids: List[str] = Field(default_factory=list)
    report_id: str = Field(..., description="Report ID (required). Visualizations are optional for static pages and resource-backed apps.")
    prompt: str = Field(..., description="Goal for the dashboard/presentation. Describe what insights to highlight, layout preferences, or specific visualizations to feature.")
    title: Optional[str] = Field(default=None, description="Title for the artifact. If not provided, one will be generated.")
    mode: str = Field(default="page", description="Artifact mode: 'page' for interactive dashboards, 'slides' for presentation decks (exportable to PPTX).")
    instruction_ids: Optional[List[str]] = Field(
        default=None,
        max_length=50,
        description=(
            "Ids of instructions you read (from get_context / read_instruction) that apply "
            "to this request. Their full text is given to the code writer in addition to the "
            "rules it loads itself. Ids outside this report's scope are ignored."
        ),
    )


class MCPCreateArtifactOutput(BaseModel):
    """Output for create_artifact MCP tool."""
    report_id: str
    artifact_id: Optional[str] = None
    resource_artifact_id: Optional[str] = None
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
    edits: Optional[List[Dict[str, str]]] = Field(default=None, description='Optional exact find/replace edits, using the standard validated editing tool.')
    expected_latest_version: Optional[int] = Field(default=None, ge=1)
    edit_instruction: str = Field(default='', description="Natural language description of the change. E.g., 'Remove the filter bar', 'Make the revenue chart blue'.")
    visualization_ids: Optional[List[str]] = Field(default=None, description="Optional list of NEW visualization IDs to add. Existing ones are kept automatically.")
    title: Optional[str] = Field(default=None, description="Updated title. If not provided, existing title is kept.")
    instruction_ids: Optional[List[str]] = Field(
        default=None,
        max_length=50,
        description=(
            "Ids of instructions you read (from get_context / read_instruction) that apply "
            "to this request. Their full text is given to the code writer in addition to the "
            "rules it loads itself. Ids outside this report's scope are ignored."
        ),
    )


class MCPEditArtifactOutput(BaseModel):
    """Output for edit_artifact MCP tool."""
    report_id: str
    resource_artifact_id: Optional[str] = None
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
    EMAIL_BODY_RULES,
    EMAIL_HTML_RULES,
    EMAIL_BODY_FORMAT_RULE,
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
    body: str = Field(
        ...,
        min_length=1,
        description=(
            "The email body. Keep it short and natural.\n\n"
            f"{EMAIL_BODY_RULES}\n\n{EMAIL_HTML_RULES}"
        ),
    )
    body_format: str = Field(default="text", description=EMAIL_BODY_FORMAT_RULE)
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


# === list_entities / describe_entity (saved queries) ===

MCP_ENTITY_DEFAULT_PREVIEW_ROWS = 20
MCP_ENTITY_MAX_PREVIEW_ROWS = 200


class MCPEntityParameter(BaseModel):
    """A parameter a saved query declares, as describe_entity accepts it."""
    name: str
    type: str = "string"
    source: Optional[str] = None
    default: Optional[Any] = None
    required: bool = False
    options: List[Any] = Field(default_factory=list, description="Allowed values, when the parameter declares a fixed list.")


class MCPEntityItem(BaseModel):
    """A published saved query in the catalog listing."""
    id: str
    slug: Optional[str] = None
    type: str = Field(..., description="'metric' or 'model'.")
    title: str
    description: Optional[str] = None
    agents: List[str] = Field(default_factory=list)
    parameters: List[MCPEntityParameter] = Field(default_factory=list)


class MCPListEntitiesInput(BaseModel):
    """Input for the list_entities MCP tool."""
    report_id: Optional[str] = Field(default=None, description="Scope to the agents attached to this report. Without it, every agent you can see is searched.")
    search: Optional[str] = Field(default=None, description="Optional text matched against title and description.")
    limit: int = Field(default=20, ge=1, le=100, description="Maximum saved queries to return.")


class MCPListEntitiesOutput(BaseModel):
    """Output for the list_entities MCP tool."""
    entities: List[MCPEntityItem] = Field(default_factory=list)
    total: int = 0


class MCPDescribeEntityInput(BaseModel):
    """Input for the describe_entity MCP tool."""
    name_or_id: str = Field(..., description="The saved query's id, slug or title (a partial title also matches).")
    params: Optional[Dict[str, Any]] = Field(default=None, description="Values for the parameters the saved query declares. Passing any value runs the saved code with them.")
    run: bool = Field(default=False, description="Re-execute the saved code instead of serving the stored snapshot.")
    limit: Optional[int] = Field(default=None, ge=1, le=MCP_ENTITY_MAX_PREVIEW_ROWS, description="Maximum preview rows to return.")


class MCPDescribeEntityOutput(BaseModel):
    """Output for the describe_entity MCP tool."""
    success: bool
    entity_id: Optional[str] = None
    slug: Optional[str] = None
    type: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    agents: List[str] = Field(default_factory=list)
    code: Optional[str] = Field(default=None, description="The saved query's code — the definition to reuse rather than rewrite.")
    parameters: List[MCPEntityParameter] = Field(default_factory=list)
    applied_params: Optional[Dict[str, Any]] = Field(default=None, description="The parameter values the returned rows were produced with.")
    columns: List[str] = Field(default_factory=list)
    rows: List[Dict[str, Any]] = Field(default_factory=list)
    total_rows: int = 0
    truncated: bool = False
    data_source: Optional[str] = Field(default=None, description="Where the rows came from: 'snapshot', 'executed', 'cache', or 'withheld'.")
    error_message: Optional[str] = None
