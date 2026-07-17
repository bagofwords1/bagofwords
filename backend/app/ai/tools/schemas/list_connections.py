from typing import List, Optional
from pydantic import BaseModel, Field


class ListConnectionsInput(BaseModel):
    """Input schema for list_connections tool.

    Lists org connections the caller can build agents on (requires the
    create-agents tier on each connection). Use get_connection for the full
    table/tool/file catalog of one connection.
    """

    search: Optional[str] = Field(
        None,
        description=(
            "Optional case-insensitive filter on connection name or type. "
            "Supports glob patterns (e.g. 'snow*', '*prod*'); a plain string "
            "matches as a substring."
        ),
    )

    limit: int = Field(
        50,
        description="Maximum number of connections to return (1-100).",
        ge=1,
        le=100,
    )


class ListConnectionsItem(BaseModel):
    """A single connection the caller can build an agent on."""

    id: str
    name: str
    type: str = Field(..., description="Connection type, e.g. 'postgresql', 'snowflake', 'mcp'.")
    data_shape: str = Field(
        "tables",
        description="What the connection exposes: 'tables' | 'files' | 'objects' | 'tools'.",
    )
    auth_policy: Optional[str] = Field(
        None, description="'system_only' (shared credentials) or 'user_required' (each user signs in)."
    )
    schemas: List[str] = Field(
        default_factory=list,
        description="Distinct database schema names in the catalog (tables-shaped connections only).",
    )
    table_count: int = Field(0, description="Discovered tables/indices/files in the connection catalog.")
    tool_count: int = Field(0, description="Discovered tools (MCP / API connections).")
    linked_agents: List[str] = Field(
        default_factory=list, description="Names of agents already built on this connection."
    )
    can_create_agent: bool = Field(
        True, description="Whether the caller can create an agent on this connection."
    )


class ListConnectionsOutput(BaseModel):
    """Output schema for list_connections tool response."""

    success: bool = Field(..., description="Whether the listing succeeded")
    connections: List[ListConnectionsItem] = Field(default_factory=list)
    total: int = Field(0, description="Total connections the caller can build on (before limit).")
    message: Optional[str] = Field(None, description="Status or error message.")
