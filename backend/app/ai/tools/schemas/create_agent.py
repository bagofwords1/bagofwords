from typing import List, Optional
from pydantic import BaseModel, Field


class CreateAgentInput(BaseModel):
    """Input schema for create_agent tool.

    Creates a new agent (data source) linked to one or more EXISTING
    connections, optionally selecting its active tables / enabled tools in the
    same call, and attaches it to the current training session.
    """

    name: str = Field(..., description="Agent name (unique per organization).", min_length=1, max_length=120)

    connection_ids: List[str] = Field(
        ...,
        description="Existing connection ID(s) to build the agent on (from list_connections).",
        min_length=1,
        max_length=5,
    )

    description: Optional[str] = Field(
        None,
        description="Short human-readable description of what this agent covers.",
        max_length=2000,
    )

    is_public: bool = Field(
        False,
        description="True to make the agent visible to the whole organization; false = private to the creator.",
    )

    schemas: Optional[List[str]] = Field(
        None,
        description=(
            "Tables-shaped connections: activate ALL tables in these database "
            "schemas (exact names, case-insensitive). Combined (union) with `tables`."
        ),
        max_length=20,
    )

    tables: Optional[List[str]] = Field(
        None,
        description=(
            "Tables-shaped connections: table names or case-insensitive glob "
            "patterns (e.g. 'sales.*', '*invoice*') to activate. Omit BOTH "
            "`schemas` and `tables` to keep the connection's default selection."
        ),
        max_length=100,
    )

    tools: Optional[List[str]] = Field(
        None,
        description=(
            "Tool-shaped connections (MCP/API): tool names or glob patterns to "
            "ENABLE — every other tool is disabled for this agent. Omit to keep "
            "the connection's default tool set."
        ),
        max_length=100,
    )

    use_defaults: bool = Field(
        False,
        description=(
            "Set true ONLY when the user explicitly chose 'everything' / default "
            "coverage. Without it, creating on a large catalog with no "
            "schemas/tables/tools selection is rejected with `needs_selection` "
            "and a menu of coverage groups — ask the user via clarify (clickable "
            "options) and retry with their choice."
        ),
    )


class SelectionGroup(BaseModel):
    """A coarse coverage choice offered when the request had no selection."""

    label: str = Field(..., description="Schema name, name-prefix glob, or tool-prefix glob.")
    count: int = Field(0, description="How many tables/tools the group covers.")
    kind: str = Field("schema", description="'schema' | 'prefix' | 'tool_prefix' | 'other'.")


class CreateAgentOutput(BaseModel):
    """Output schema for create_agent tool response."""

    success: bool = Field(..., description="Whether the agent was created")
    data_source_id: Optional[str] = Field(None, description="ID of the created agent (data source).")
    name: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None
    connections: List[str] = Field(default_factory=list, description="Names of the linked connections.")

    tables_total: int = Field(0, description="Tables seeded from the connection catalog(s).")
    tables_active: int = Field(0, description="Tables active after selection.")
    active_table_sample: List[str] = Field(default_factory=list, description="Up to 20 active table names.")
    tools_total: int = Field(0, description="Tools discovered on the linked connection(s).")
    tools_enabled: int = Field(0, description="Tools enabled for this agent after selection.")

    unresolved: List[str] = Field(
        default_factory=list,
        description="Requested schemas/tables/tools patterns that matched nothing (never silently dropped).",
    )
    attached_to_report: bool = Field(False, description="Whether the agent was attached to the current session.")
    requires_user_connect: bool = Field(
        False,
        description="True when a linked connection is user_required — each user must Connect before tools run.",
    )
    message: Optional[str] = Field(None, description="Status or error message.")
    selection_groups: List[SelectionGroup] = Field(
        default_factory=list,
        description=(
            "On a `needs_selection` rejection: the coverage groups to offer the "
            "user as clarify options (with an 'Everything' choice)."
        ),
    )
    rejected_reason: Optional[str] = Field(
        None,
        description=(
            "Reason when rejected: permission_denied | name_taken | limit_reached | "
            "connection_not_found | needs_selection | invalid_input."
        ),
    )
