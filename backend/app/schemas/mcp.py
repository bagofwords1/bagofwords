"""MCP API schemas - request/response models for MCP endpoints."""

from pydantic import BaseModel, Field
from typing import Dict, Any, Optional, List


class MCPToolSchema(BaseModel):
    """Schema for a single MCP tool in the catalog."""
    name: str
    description: str
    input_schema: Dict[str, Any]


class MCPToolsResponse(BaseModel):
    """Response for listing available MCP tools."""
    tools: List[MCPToolSchema]
