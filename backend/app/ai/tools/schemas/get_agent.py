"""Schema for the get_agent internal planner tool (training mode)."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ConnectionRef(BaseModel):
    id: str
    name: str
    type: str
    auth_policy: str
    is_indexed: bool = False
    is_active: bool = True
    table_count: int = 0
    tool_count: int = 0


class TableEntry(BaseModel):
    name: str
    connection_name: str
    columns_preview: List[Dict[str, Any]] = Field(default_factory=list)
    no_rows: Optional[int] = None
    centrality_score: Optional[float] = None


class ToolEntry(BaseModel):
    connection_name: str
    tool_name: str
    is_enabled: bool = True
    policy: str = "allow"
    has_overlay: bool = False


class MemberEntry(BaseModel):
    principal_type: str  # 'user' | 'group'
    name_or_email: str
    permissions: List[str] = Field(default_factory=list)


class AgentDetail(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    context: Optional[str] = None
    is_public: bool = False
    use_llm_sync: bool = False
    owner_user_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    connections: List[ConnectionRef] = Field(default_factory=list)
    conversation_starters: List[str] = Field(default_factory=list)

    tables: List[TableEntry] = Field(default_factory=list)
    tables_truncated: bool = False
    tables_total: int = 0

    tools: List[ToolEntry] = Field(default_factory=list)

    members: List[MemberEntry] = Field(default_factory=list)


class GetAgentInput(BaseModel):
    name: str = Field(..., description="Org-unique agent name.")
    table_limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="Top-N active tables to include (ranked by centrality_score desc, then name asc).",
    )


class GetAgentOutput(BaseModel):
    success: bool
    agent: Optional[AgentDetail] = None
    error_message: Optional[str] = None
