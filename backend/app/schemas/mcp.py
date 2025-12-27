"""MCP API schemas - request/response models for MCP endpoints."""

from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List

# Reuse existing schemas
from app.ai.tools.schemas.create_widget import TablesBySource
from app.ai.tools.schemas.inspect_data import InspectDataOutput as BaseInspectDataOutput


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


class DataSourceInfo(BaseModel):
    """Summary of a data source with its tables."""
    id: str
    name: str
    type: str
    tables: List[TableInfo]


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
