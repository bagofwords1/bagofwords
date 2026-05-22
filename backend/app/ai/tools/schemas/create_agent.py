"""Schema for the create_agent internal planner tool (training mode).

Upsert semantics: if an agent with the given ``name`` exists in the
caller's organization and the caller has ``manage`` on it, the call
updates it; otherwise creates a new one. The structured input mirrors
``AgentManifest`` but is authored as JSON args, not YAML — easier for
the planner to fill in field-by-field.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, model_validator


class ToolPolicyInput(BaseModel):
    """Per-connection tool policy overlay."""

    connection_name: str
    allow: List[str] = Field(default_factory=list, description="Tool names to allow. Supports '*' wildcard.")
    confirm: List[str] = Field(default_factory=list, description="Tool names that require user confirmation.")
    deny: List[str] = Field(default_factory=list, description="Tool names to deny.")


class MemberInput(BaseModel):
    """Polymorphic member entry: exactly one of ``user`` (email) or ``group`` (name)."""

    user: Optional[str] = Field(default=None, description="User email (must be an org member).")
    group: Optional[str] = Field(default=None, description="Group name (org-unique).")
    permissions: Optional[List[str]] = Field(
        default=None,
        description="Defaults to ['view', 'view_schema']. Use ['manage'] for full control.",
    )

    @model_validator(mode="after")
    def _one_of(self) -> "MemberInput":
        if (self.user is None) == (self.group is None):
            raise ValueError("MemberInput requires exactly one of 'user' or 'group'")
        return self


class CreateAgentInput(BaseModel):
    name: str = Field(..., description="Org-unique agent name. Re-using an existing name updates that agent.")
    description: Optional[str] = Field(default=None, description="Short summary of the agent's purpose.")
    context: Optional[str] = Field(default=None, description="Free-form context surfaced to the LLM.")
    is_public: bool = Field(default=False, description="Visibility. Default is private.")
    use_llm_sync: bool = Field(default=False)

    connection_names: List[str] = Field(
        default_factory=list,
        description="Connections (by org-unique name) to attach. At least one is usually required for a useful agent.",
    )
    tables_include: Optional[List[str]] = Field(
        default=None,
        description="Glob patterns matched against '{connection}.{schema}.{table}'. None means include all.",
    )
    tables_exclude: List[str] = Field(default_factory=list)

    tool_policies: List[ToolPolicyInput] = Field(
        default_factory=list,
        description="Per-connection tool overlays (only meaningful for mcp/custom_api connections).",
    )

    conversation_starters: List[str] = Field(
        default_factory=list,
        description="Example questions surfaced to the user.",
    )

    members: List[MemberInput] = Field(
        default_factory=list,
        description=(
            "Members granted access. Owner is added automatically — do not list the caller."
        ),
    )

    dry_run: bool = Field(
        default=False,
        description="Validate and compute the diff without writing anything.",
    )


class CreateAgentOutput(BaseModel):
    success: bool
    status: str = Field(..., description="created | updated | unchanged | dry_run | error")
    id: Optional[str] = None
    name: Optional[str] = None
    diff: Optional[Dict[str, Any]] = None
    warnings: List[Dict[str, Any]] = Field(default_factory=list)
    errors: List[Dict[str, Any]] = Field(default_factory=list)
    message: Optional[str] = None
