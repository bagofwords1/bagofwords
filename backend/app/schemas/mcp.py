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
