from typing import List, Optional
from pydantic import BaseModel, Field


class GetConnectionInput(BaseModel):
    """Input schema for get_connection tool.

    Returns one connection's catalog — tables (grouped by schema), tools, or
    files/file-scope — so you can plan a create_agent selection before any
    agent exists. Supports glob filtering and pagination.
    """

    connection_id: str = Field(..., description="The connection ID (UUID) from list_connections.")

    pattern: Optional[str] = Field(
        None,
        description=(
            "Optional case-insensitive glob filter (e.g. 'sales.*', '*invoice*', "
            "'send_*') applied to table names, tool names, or file paths. A plain "
            "string (no glob metacharacters) matches as a substring."
        ),
    )

    schema_name: Optional[str] = Field(
        None,
        description="Tables-shaped connections only: filter to one database schema (exact, case-insensitive).",
    )

    page: int = Field(1, description="Page number (1-based).", ge=1)

    page_size: int = Field(
        100,
        description="Items per page (1-200).",
        ge=1,
        le=200,
    )


class ConnectionTableItem(BaseModel):
    """A table (or index/collection) in the connection catalog."""

    name: str
    schema_name: Optional[str] = Field(None, description="Database schema this table belongs to, if any.")
    column_count: int = 0
    row_count: Optional[int] = None


class ConnectionToolItem(BaseModel):
    """A tool exposed by an MCP / API connection."""

    name: str
    description: Optional[str] = None
    default_enabled: bool = True
    default_policy: str = "allow"


class ConnectionFileScope(BaseModel):
    """The configured file scope of a file-shaped connection (e.g. network_dir, S3)."""

    base: Optional[str] = Field(None, description="Root path / bucket prefix the connection is scoped to.")
    include_globs: List[str] = Field(default_factory=list, description="Configured include glob patterns.")
    index_mode: Optional[str] = Field(None, description="'content' or 'metadata' indexing.")
    token_scoped: bool = Field(
        False,
        description="True when the user's own signed-in account defines the scope (OneDrive/GDrive/Outlook).",
    )


class GetConnectionOutput(BaseModel):
    """Output schema for get_connection tool response."""

    success: bool = Field(..., description="Whether the lookup succeeded")
    connection_id: Optional[str] = None
    name: Optional[str] = None
    type: Optional[str] = None
    data_shape: str = Field("tables", description="'tables' | 'files' | 'objects' | 'tools'.")

    schemas: List[str] = Field(
        default_factory=list, description="All distinct schema names in the catalog (unfiltered)."
    )
    tables: List[ConnectionTableItem] = Field(default_factory=list)
    tools: List[ConnectionToolItem] = Field(default_factory=list)
    file_scope: Optional[ConnectionFileScope] = None
    files: List[str] = Field(default_factory=list, description="Indexed file paths (file-shaped connections).")

    total: int = Field(0, description="Total items matching the filter (before pagination).")
    page: int = 1
    page_size: int = 100
    has_more: bool = False
    message: Optional[str] = Field(None, description="Status or error message.")
    rejected_reason: Optional[str] = Field(None, description="Set when the lookup was rejected (e.g. permission_denied).")
