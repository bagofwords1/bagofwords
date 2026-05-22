"""YAML manifest schema for declarative Agent (DataSource) apply.

Apply semantics:
- Resources are identified by ``(organization_id, name)``. Renames create a
  new resource — there is no separate slug.
- Omitted optional fields revert to defaults. The YAML expresses *desired
  state*, not a patch. ``apply(get(x)) == unchanged`` for every field.
- Refs (connections, groups, users, tools) are by name/email. Resolution
  is collect-all-errors and returns ``did-you-mean`` suggestions where
  possible.

Instructions are intentionally NOT part of this manifest. They live in
the org-wide ``instructions`` table with their own lifecycle (UI,
git-sync from markdown, ``create_instruction`` MCP tool) and attach to
agents via the M:N association table. Putting them inside the agent
file made the ownership ambiguous (org-wide rows nested under one
agent), so authors create instructions separately and reference them
by ``data_source_ids`` in the InstructionCreate payload.

Not in scope for v1:
- Inline ``Connection`` definitions in YAML (creds + env-var refs).
- Group resolution by ``external_id`` (AD/Okta/SCIM).
- Glob patterns beyond ``*`` for tool / table matching.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Manifest body
# ---------------------------------------------------------------------------


class TableRules(BaseModel):
    """Include/exclude rules applied against ``{connection}.{schema}.{table}``.

    Supports ``*`` as a wildcard segment. ``include`` defaults to ``["*"]``
    (all tables) when omitted; ``exclude`` defaults to empty. A table is
    active iff it matches *any* include and *no* exclude.
    """

    include: Optional[List[str]] = None
    exclude: List[str] = Field(default_factory=list)


class ToolsOverlay(BaseModel):
    """Per-agent tool policy overlay for a single MCP/custom_api connection.

    Each list contains tool *names* (matched against ``ConnectionTool.name``)
    and accepts ``"*"`` as a wildcard. A tool ends up at the most restrictive
    bucket it matches: deny > confirm > allow.
    """

    allow: List[str] = Field(default_factory=list)
    confirm: List[str] = Field(default_factory=list)
    deny: List[str] = Field(default_factory=list)


class MemberRef(BaseModel):
    """Polymorphic member entry. Use ``user:`` for an email or ``group:`` for
    a group name. ``permissions`` defaults to ``["view", "view_schema"]``."""

    user: Optional[str] = None
    group: Optional[str] = None
    permissions: Optional[List[str]] = None

    @model_validator(mode="after")
    def _one_of(self) -> "MemberRef":
        if (self.user is None) == (self.group is None):
            raise ValueError("MemberRef requires exactly one of 'user' or 'group'")
        return self


class AgentManifest(BaseModel):
    """Declarative agent description. ``name`` is the identity key (within
    the organization)."""

    name: str
    description: Optional[str] = None
    context: Optional[str] = None
    is_public: bool = False
    use_llm_sync: bool = False

    connections: List[str] = Field(default_factory=list)
    tables: Optional[TableRules] = None
    tools: Dict[str, ToolsOverlay] = Field(default_factory=dict)
    conversation_starters: List[str] = Field(default_factory=list)
    members: List[MemberRef] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("name must not be empty")
        return v.strip()

    @field_validator("connections")
    @classmethod
    def _unique_connections(cls, v: List[str]) -> List[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate entries in 'connections'")
        return v


# ---------------------------------------------------------------------------
# Error / response envelope
# ---------------------------------------------------------------------------


class ApplyErrorCode(str, Enum):
    YAML_PARSE_ERROR = "yaml_parse_error"
    SCHEMA_INVALID = "schema_invalid"
    CONNECTION_NOT_FOUND = "connection_not_found"
    CONNECTION_TYPE_MISMATCH = "connection_type_mismatch"
    TOOL_NOT_FOUND = "tool_not_found"
    GROUP_NOT_FOUND = "group_not_found"
    USER_NOT_FOUND = "user_not_found"
    DUPLICATE_ENTRY = "duplicate_entry"
    LICENSE_REQUIRED = "license_required"
    PERMISSION_DENIED = "permission_denied"
    ENUM_INVALID = "enum_invalid"


class ApplyWarningCode(str, Enum):
    CONNECTION_INDEXING_PENDING = "connection_indexing_pending"
    TABLES_FILTER_EMPTY = "tables_filter_empty"
    GLOB_OVERLAP = "glob_overlap"


class ApplyError(BaseModel):
    loc: List[Union[str, int]]
    code: ApplyErrorCode
    message: str
    value: Optional[Any] = None
    suggestion: Optional[str] = None


class ApplyWarning(BaseModel):
    loc: List[Union[str, int]] = Field(default_factory=list)
    code: ApplyWarningCode
    message: str


class ApplyStatus(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    ERROR = "error"
    DRY_RUN = "dry_run"


class ApplyResult(BaseModel):
    """Single response envelope for both success and failure paths.

    Success: ``status`` ∈ {created, updated, unchanged, dry_run},
    ``errors`` empty.
    Failure: ``status == error``, ``errors`` populated; ``id`` may be
    ``None`` (no resource exists yet or write was aborted).
    """

    status: ApplyStatus
    id: Optional[str] = None
    name: Optional[str] = None
    diff: Optional[Dict[str, Any]] = None
    warnings: List[ApplyWarning] = Field(default_factory=list)
    errors: List[ApplyError] = Field(default_factory=list)
