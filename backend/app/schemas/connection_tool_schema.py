"""
Connection tool schemas for MCP/API tool management.
"""
from typing import Optional, List, Any

from pydantic import BaseModel


class ConnectionToolSchema(BaseModel):
    """Schema for tool list view."""
    id: str
    name: str
    description: Optional[str] = None
    is_enabled: bool
    policy: str
    connection_id: str
    input_schema: Optional[dict] = None
    output_schema: Optional[dict] = None

    class Config:
        from_attributes = True


class ConnectionToolUpdate(BaseModel):
    """Schema for updating a single tool."""
    is_enabled: Optional[bool] = None
    policy: Optional[str] = None


class BatchToolUpdate(BaseModel):
    """Schema for batch enabling/disabling tools."""
    tool_ids: List[str]
    is_enabled: bool
