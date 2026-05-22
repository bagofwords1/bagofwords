"""Schema for the list_agents internal planner tool (training mode)."""

from typing import List, Optional

from pydantic import BaseModel, Field


class ListAgentsInput(BaseModel):
    name_search: Optional[str] = Field(
        default=None,
        description="Case-insensitive substring match on agent name.",
    )
    type: Optional[str] = Field(
        default=None,
        description="Filter by primary connection type (e.g. 'postgresql', 'mcp', 'custom_api').",
    )
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=100)


class AgentSummary(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    type: Optional[str] = None
    is_public: bool = False
    connection_count: int = 0
    table_count: int = 0
    created_at: Optional[str] = None


class ListAgentsOutput(BaseModel):
    success: bool
    total: int = 0
    agents: List[AgentSummary] = Field(default_factory=list)
    error_message: Optional[str] = None
