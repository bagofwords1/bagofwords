from pydantic import BaseModel
from typing import Any, Dict, List, Optional, Union


class DataSourceSummarySchema(BaseModel):
    """Minimal DataSource schema for summaries."""
    id: str
    name: str
    type: Optional[str] = None  # Computed from connection
    context: Optional[str] = None
    # Manager-set publishing lifecycle: "published" | "draft" | "disabled".
    publish_status: Optional[str] = None
    # Reliability-loop lifecycle: "ok" | "training" | "development". Orthogonal
    # to publish_status — a source can be published (live) while still "training"
    # (being actively improved). Surfaced to the planner to set its clarify posture.
    reliability_status: Optional[str] = None
    # Optional per-agent custom icon override ("emoji:<grapheme>" | "preset:<key>").
    # None = use the default type/connector icon.
    icon: Optional[str] = None

    class Config:
        from_attributes = True

class DataSourceMinimalSchema(BaseModel):
    """Minimal DataSource schema."""
    id: str
    name: str
    type: Optional[str] = None  # Computed from connection
    description: Optional[str] = None
    # Optional per-agent custom icon override ("emoji:<grapheme>" | "preset:<key>").
    icon: Optional[str] = None

    class Config:
        from_attributes = True

from pydantic import BaseModel, Field, validator, field_validator
from typing import Optional, Dict, Any, List, Literal
from app.schemas.data_source_registry import default_credentials_schema_for, credentials_schema_for
import uuid
from datetime import datetime
import json
from app.schemas.git_repository_schema import GitRepositorySchema
from app.schemas.base import OptionalUTCDatetime, UTCDatetime


class DataSourceMembershipSchema(BaseModel):
    id: str
    data_source_id: str
    principal_type: str  # "user" or "group"
    principal_id: str
    principal_name: Optional[str] = None  # resolved display name
    permissions: Optional[List[str]] = None  # RBAC resource permissions
    config: Optional[Dict[str, Any]] = None  # For future row-level access
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True


class DataSourceMembershipCreate(BaseModel):
    principal_type: str = "user"  # Default to "user"
    principal_id: str
    config: Optional[Dict[str, Any]] = None


class DataSourceUserStatus(BaseModel):
    has_user_credentials: bool
    auth_mode: Optional[str] = None
    is_primary: Optional[bool] = None
    last_used_at: OptionalUTCDatetime = None
    expires_at: OptionalUTCDatetime = None
    connection: Literal["offline", "not_connected", "success", "unknown"] = "unknown"
    last_checked_at: OptionalUTCDatetime = None
    effective_auth: Literal["user", "system", "none"] = "none"
    uses_fallback: bool = False
    credentials_id: Optional[str] = None
    # Admin query-identity toggle (delegated/OBO connections only).
    # query_identity: which identity the user's queries run under — "self" (their own
    #   delegated token) or "service_account" (the connection's system/principal creds).
    # can_switch_identity: whether this user (admin/owner) may flip the toggle.
    query_identity: Optional[Literal["self", "service_account"]] = None
    can_switch_identity: bool = False


class ConnectionEmbedded(BaseModel):
    """Nested connection info for DataSource responses (Option A architecture)."""
    id: str
    name: str
    type: str
    auth_policy: str = "system_only"
    allowed_user_auth_modes: Optional[List[str]] = None
    config: Optional[Dict[str, Any]] = None  # For edit flow
    is_active: bool = True
    last_synced_at: OptionalUTCDatetime = None
    user_status: Optional[DataSourceUserStatus] = None  # User's credential status for this connection
    table_count: int = 0  # Number of tables in this connection
    # Latest schema indexing run, if any. Frontend derives the "indexing"
    # effective status from this plus user_status.connection.
    indexing: Optional[Dict[str, Any]] = None
    # Catalog key for a known connector (e.g. "notion") so the UI can render the
    # provider icon even though `type` is just "mcp". None otherwise.
    connector_key: Optional[str] = None

    @validator('config', 'allowed_user_auth_modes', pre=True)
    def parse_json_fields(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return None
        return v

    @validator('connector_key', always=True)
    def derive_connector_key(cls, v, values):
        # connector_key isn't stored on the Connection model — when this schema
        # is built straight from ORM objects (e.g. report responses) fall back
        # to the preset key recorded in config at connection-create time, so
        # the UI can render the provider's brand icon.
        if v:
            return v
        cfg = values.get('config')
        if isinstance(cfg, dict):
            return cfg.get('catalog_key') or None
        return None

    class Config:
        from_attributes = True


class DataSourceReportSchema(BaseModel):
    """DataSource schema used in Report responses."""
    id: str
    name: str
    organization_id: str
    created_at: UTCDatetime
    updated_at: UTCDatetime
    context: Optional[str]
    description: Optional[str]
    summary: Optional[str]
    conversation_starters: Optional[list] = None
    is_active: bool
    is_public: bool = False
    owner_user_id: Optional[str] = None
    use_llm_sync: bool = False
    # Optional per-agent custom icon override ("emoji:<grapheme>" | "preset:<key>").
    icon: Optional[str] = None

    # Publishing lifecycle — lets the client badge non-production agents
    # (Development / Training) the same way the data source selector does.
    publish_status: str = "published"
    reliability_status: str = "training"

    # Connection info (multi-connection support)
    connections: List[ConnectionEmbedded] = []

    # Legacy fields for backward compatibility - computed from first connection
    type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    # Note: NO memberships field here
    # Note: NO primary_instruction here — report context doesn't eager-load it,
    # and Pydantic from_orm would trigger a greenlet error accessing the lazy relationship.

    class Config:
        from_attributes = True


class DataSourceBase(BaseModel):
    name: str = None


class DataSourceSchema(DataSourceBase):
    """Full DataSource (Domain) schema with nested connection info."""
    class Config:
        from_attributes = True
    id: str
    organization_id: str
    created_at: UTCDatetime
    updated_at: UTCDatetime
    context: Optional[str]
    description: Optional[str]
    summary: Optional[str]
    conversation_starters: Optional[list] = None
    is_active: bool
    is_public: bool = False
    # Manager-set publishing lifecycle: "published" | "draft" | "disabled".
    # Distinct from is_active (connection health).
    publish_status: str = "published"
    reliability_status: str = "training"
    use_llm_sync: bool = False
    # Optional per-agent custom icon override ("emoji:<grapheme>" | "preset:<key>").
    icon: Optional[str] = None
    # Per-channel availability map ({channel_type: bool}). None = available in
    # every connected channel.
    channel_availability: Optional[Dict[str, bool]] = None
    owner_user_id: Optional[str] = None
    git_repository: Optional[GitRepositorySchema] = None
    memberships: Optional[List[DataSourceMembershipSchema]] = []

    # Connection info (multi-connection support)
    connections: List[ConnectionEmbedded] = []

    # Legacy fields for backward compatibility - computed from first connection
    type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    auth_policy: Optional[str] = None
    allowed_user_auth_modes: Optional[List[str]] = None
    user_status: Optional[DataSourceUserStatus] = None

    # Primary instruction (populated by service layer — avoids circular import)
    primary_instruction: Optional[Any] = None
    primary_instruction_id: Optional[str] = None

    @field_validator('primary_instruction', mode='before')
    @classmethod
    def _guard_primary_instruction(cls, v: Any) -> Any:
        # Only accept dicts or None — reject ORM model objects which aren't serializable
        if v is None or isinstance(v, dict):
            return v
        return None

    class Config:
        from_attributes = True


class DataSourceListItemSchema(BaseModel):
    """List item schema for DataSource with nested connection info."""
    id: str
    name: str
    description: Optional[str]
    conversation_starters: Optional[list] = None
    created_at: UTCDatetime
    # Connection-health derived state, kept for backward compatibility.
    status: str  # "active" | "inactive" (mirrors is_active)
    is_public: bool = False
    # Manager-set publishing lifecycle: "published" | "draft" | "disabled".
    publish_status: str = "published"
    reliability_status: str = "training"
    # Optional per-agent custom icon override ("emoji:<grapheme>" | "preset:<key>").
    icon: Optional[str] = None

    # Connection info (multi-connection support)
    connections: List[ConnectionEmbedded] = []

    # True when every connection is a tool provider (mcp/custom_api, i.e.
    # data_shape="tools"). Lets /agents surface these as "connectors" — a
    # lightweight, often private, tools-only data source — vs analytical agents.
    is_connector: bool = False
    # Catalog key for a known connector (e.g. "notion", "monday"), so the UI can
    # render the provider's icon instead of the generic MCP glyph. None otherwise.
    connector_key: Optional[str] = None

    # Legacy fields for backward compatibility - computed from first connection
    type: Optional[str] = None
    auth_policy: Optional[str] = None
    user_status: Optional[DataSourceUserStatus] = None

    # True only when this private data source is visible solely because the
    # caller used the admin "show all" view (full_admin_access /
    # manage_connections) — i.e. it's not public and they hold no explicit
    # membership. Lets the UI flag it as an admin-only/governance entry.
    admin_only: bool = False

    class Config:
        from_attributes = True


class DataSourceCreate(DataSourceBase):
    """Schema for creating a new DataSource (Domain).

    Three modes:
    1. Create new connection: Provide type, config, credentials
    2. Link to existing connection: Provide connection_id (single connection)
    3. Link to multiple connections: Provide connection_ids (array)
    """
    # Option 1: Connection-related fields (will be used to create Connection)
    type: Optional[str] = None  # Connection type: e.g., "postgresql", "bigquery", "netsuite"
    config: Optional[dict] = None  # Connection config, will be validated based on the type
    credentials: Optional[dict] = None  # Will be validated based on the data source type
    auth_policy: str = "system_only"
    allowed_user_auth_modes: Optional[List[str]] = None

    # Option 2: Link to existing connection(s)
    connection_id: Optional[str] = None  # Single connection (backward compatible)
    connection_ids: Optional[List[str]] = None  # Multiple connections
    
    # Domain-specific fields
    generate_summary: bool = False
    generate_conversation_starters: bool = False
    generate_ai_rules: bool = False
    is_public: bool = False
    use_llm_sync: bool = False
    # Per-channel availability map ({channel_type: bool}). None = available in
    # every connected channel.
    channel_availability: Optional[Dict[str, bool]] = None
    member_user_ids: Optional[List[str]] = []  # User IDs to grant access to

    @validator('credentials')
    def validate_credentials(cls, v, values):
        # Skip validation if linking to existing connection(s)
        if values.get('connection_id') or values.get('connection_ids'):
            return v

        if v is None:
            return v

        if 'type' not in values or not values['type']:
            raise ValueError('Data source type must be specified when creating a new connection')

        ds_type = values['type']
        # Prefer auth_type from config when present (selected by UI)
        cfg = values.get('config') or {}
        auth_type = (cfg or {}).get('auth_type')
        schema_cls = credentials_schema_for(ds_type, auth_type)
        return schema_cls(**v).dict()

    @validator('connection_ids', always=True)
    def validate_connection_ids_or_type(cls, v, values):
        """Ensure either connection_id, connection_ids, OR (type, config, credentials) is provided."""
        # If connection_ids is provided, use it
        if v and len(v) > 0:
            return v

        # If connection_id (singular) is provided, that's fine too
        if values.get('connection_id'):
            return v

        # Creating new connection - require type and config
        if not values.get('type'):
            raise ValueError('Either connection_id, connection_ids, or type must be provided')
        if values.get('config') is None:
            raise ValueError('Config is required when creating a new connection')

        return v


class DataSourceUpdate(DataSourceBase):
    """Schema for updating a DataSource (Domain). Connection updates are delegated to Connection."""
    name: Optional[str] = None
    description: Optional[str] = None
    summary: Optional[str] = None
    context: Optional[str] = None
    conversation_starters: Optional[list] = None
    is_public: Optional[bool] = None
    use_llm_sync: Optional[bool] = None
    # Optional per-agent custom icon override. A namespaced token
    # ("emoji:<grapheme>" | "preset:<key>"). Pass an explicit null to clear the
    # override and fall back to the default icon; omit to leave unchanged.
    icon: Optional[str] = None
    # Per-channel availability map ({channel_type: bool}). None = leave unchanged.
    channel_availability: Optional[Dict[str, bool]] = None
    # Manager-set publishing lifecycle. Guarded by the 'manage' resource
    # permission on the data source (see routes/data_source.py).
    publish_status: Optional[str] = None
    # Manager-set lifecycle/quality stage. Normally automation-driven, but a
    # manager can override it (the "Production/Training/Development" stage in the
    # agent status control). Guarded by 'manage' like publish_status.
    reliability_status: Optional[str] = None
    member_user_ids: Optional[List[str]] = None  # User IDs to grant access to
    primary_instruction_id: Optional[Union[str, None]] = None  # None = clear, str = set

    @field_validator('publish_status')
    @classmethod
    def _validate_publish_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"published", "draft", "disabled"}
        if v not in allowed:
            raise ValueError(f"publish_status must be one of {sorted(allowed)}")
        return v

    @field_validator('reliability_status')
    @classmethod
    def _validate_reliability_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {"ok", "training", "development"}
        if v not in allowed:
            raise ValueError(f"reliability_status must be one of {sorted(allowed)}")
        return v

    @field_validator('icon')
    @classmethod
    def _validate_icon(cls, v: Optional[str]) -> Optional[str]:
        # None (clear) is allowed. Otherwise require a recognised namespaced
        # token so garbage can't be stored; the value after the prefix is left
        # to the client (the emoji picker constrains it).
        #   emoji:<grapheme>  — a custom emoji
        #   type:<key>        — pin one of the agent's connection type/connector
        #                       icons (e.g. "type:snowflake", "type:notion")
        #   preset:<key>      — reserved for a future curated preset gallery
        if v is None:
            return v
        v = v.strip()
        if not v:
            return None
        allowed_kinds = ("emoji:", "type:", "preset:")
        if not v.startswith(allowed_kinds):
            raise ValueError("icon must be a token like 'emoji:<char>', 'type:<key>' or 'preset:<key>'")
        # Guard against unbounded input (emoji + ZWJ sequences stay well under this).
        if len(v) > 64:
            raise ValueError("icon token too long")
        return v

    # Connection-related fields (will be delegated to Connection update)
    config: Optional[dict] = None
    credentials: Optional[dict] = None
    auth_policy: Optional[str] = None

    class Config:
        from_attributes = True


class DataSourceInDBBase(DataSourceBase):
    """Internal schema for DataSource in DB."""
    id: str
    # Connection info (multi-connection support)
    connections: List[ConnectionEmbedded] = []

    class Config:
        from_attributes = True


