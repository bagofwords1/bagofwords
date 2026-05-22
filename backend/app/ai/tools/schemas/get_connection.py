"""Schema for the get_connection internal planner tool (training mode)."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ColumnPreview(BaseModel):
    name: str
    dtype: Optional[str] = None


class ConnectionTableEntry(BaseModel):
    name: str
    schema_: Optional[str] = Field(default=None, alias="schema")
    column_count: int = 0
    no_rows: Optional[int] = None
    columns_preview: Optional[List[ColumnPreview]] = None

    class Config:
        populate_by_name = True


class ConnectionToolEntry(BaseModel):
    name: str
    description: Optional[str] = None
    is_enabled: bool = True
    policy: str = "allow"
    input_schema: Optional[Dict[str, Any]] = None


class ConnectionAgentRef(BaseModel):
    id: str
    name: str


class ConnectionDetail(BaseModel):
    id: str
    name: str
    type: str
    auth_policy: str
    is_active: bool = True
    last_synced_at: Optional[str] = None

    is_indexed: bool = False
    indexing_status: Optional[str] = None

    tables: List[ConnectionTableEntry] = Field(default_factory=list)
    tables_truncated: bool = False
    tables_total: int = 0

    tools: List[ConnectionToolEntry] = Field(default_factory=list)
    tools_total: int = 0

    agents: List[ConnectionAgentRef] = Field(default_factory=list)
    config_preview: Dict[str, Any] = Field(default_factory=dict)


class GetConnectionInput(BaseModel):
    name: str = Field(..., description="Org-unique connection name.")
    table_search: Optional[str] = Field(
        default=None,
        description="Case-insensitive substring matching schema or table name.",
    )
    table_limit: int = Field(default=50, ge=1, le=500)
    with_columns: bool = Field(
        default=False,
        description="Include column previews per table. Off by default — large schemas blow up the response.",
    )
    with_tools: bool = Field(
        default=True,
        description="Include the tools list for mcp/custom_api connections.",
    )


class GetConnectionOutput(BaseModel):
    success: bool
    connection: Optional[ConnectionDetail] = None
    error_message: Optional[str] = None
