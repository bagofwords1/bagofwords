"""Schema for the list_connections internal planner tool (training mode)."""

from typing import List, Optional

from pydantic import BaseModel, Field


class ListConnectionsInput(BaseModel):
    name_search: Optional[str] = Field(default=None, description="Case-insensitive substring match.")
    type: Optional[str] = Field(default=None, description="Filter by connection type (e.g. 'postgresql', 'mcp', 'custom_api').")
    only_tool_providers: bool = Field(
        default=False, description="Restrict to mcp + custom_api connections."
    )
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class ConnectionSummary(BaseModel):
    id: str
    name: str
    type: str
    auth_policy: str
    is_active: bool = True
    is_indexed: bool = False
    table_count: int = 0
    tool_count: int = 0
    agent_names: List[str] = Field(default_factory=list)
    last_synced_at: Optional[str] = None


class ListConnectionsOutput(BaseModel):
    success: bool
    total: int = 0
    connections: List[ConnectionSummary] = Field(default_factory=list)
    error_message: Optional[str] = None
