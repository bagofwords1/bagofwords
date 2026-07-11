"""
Connection schemas for database connection management.
"""
from datetime import datetime
from typing import Any, Dict, Optional, List

from pydantic import BaseModel, validator

from app.schemas.base import UTCDatetime


class ConnectionCreate(BaseModel):
    """Schema for creating a new database connection."""
    name: str
    type: str
    config: dict
    credentials: Optional[dict] = None
    auth_policy: str = "system_only"
    allowed_user_auth_modes: Optional[list] = None


class ConnectionUpdate(BaseModel):
    """Schema for updating an existing connection."""
    name: Optional[str] = None
    config: Optional[dict] = None
    credentials: Optional[dict] = None
    is_active: Optional[bool] = None
    auth_policy: Optional[str] = None  # system_only, user_required
    allowed_user_auth_modes: Optional[list] = None
    # Scheduled auto-reindex (enterprise `scheduled_reindex` feature).
    auto_reindex_enabled: Optional[bool] = None
    reindex_interval_hours: Optional[int] = None  # legacy; superseded by minutes
    # Either an interval (minutes) OR a fixed daily time ("HH:MM").
    reindex_schedule_mode: Optional[str] = None  # "interval" | "time"
    reindex_interval_minutes: Optional[int] = None
    reindex_at_time: Optional[str] = None
    # Per-connection request rate limit (enterprise `connection_rate_limit`).
    # A per-window value of 0 (or null) means "no limit for that window".
    rate_limit_enabled: Optional[bool] = None
    rate_limit_per_minute: Optional[int] = None
    rate_limit_per_hour: Optional[int] = None
    rate_limit_per_day: Optional[int] = None

    @validator("rate_limit_per_minute", "rate_limit_per_hour", "rate_limit_per_day")
    def _validate_rate_limit(cls, v):
        if v is not None and v < 0:
            raise ValueError("rate limit must be >= 0 (0 means no limit)")
        return v

    @validator("reindex_schedule_mode")
    def _validate_mode(cls, v):
        if v is not None and v not in ("interval", "time"):
            raise ValueError("reindex_schedule_mode must be 'interval' or 'time'")
        return v

    @validator("reindex_interval_minutes")
    def _validate_minutes(cls, v):
        if v is not None and v < 10:
            raise ValueError("reindex_interval_minutes must be at least 10")
        return v

    @validator("reindex_at_time")
    def _validate_time(cls, v):
        if v in (None, ""):
            return v
        try:
            hh, mm = v.strip().split(":")
            if 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59:
                return f"{int(hh):02d}:{int(mm):02d}"
        except (ValueError, AttributeError):
            pass
        raise ValueError("reindex_at_time must be 'HH:MM' (24h)")


class ConnectionSchema(BaseModel):
    """Schema for connection list view."""
    id: str
    name: str
    type: str
    is_active: bool
    auth_policy: str
    # User auth modes allowed for user_required connections (e.g. ["oauth"]).
    # The frontend uses this to skip the credentials modal and redirect
    # straight to the provider when OAuth is the only option.
    allowed_user_auth_modes: Optional[list] = None
    last_synced_at: Optional[str] = None
    organization_id: str
    table_count: int = 0
    tool_count: int = 0
    agent_count: int = 0
    agent_names: List[str] = []  # Names of linked agents (for delete confirmation)
    indexing: Optional[Dict[str, Any]] = None
    # Per-user auth status for the requesting user (user_required connections):
    # has_user_credentials / effective_auth / uses_fallback / connection.
    user_status: Optional[Dict[str, Any]] = None
    # Catalog key for a known connector (e.g. "notion", "monday") so the UI can
    # render the provider's brand icon even though `type` is just "mcp". None for
    # generic connections.
    connector_key: Optional[str] = None

    class Config:
        from_attributes = True


class ConnectionDetailSchema(BaseModel):
    """Extended schema for editing - includes config but not credentials (never sent back)."""
    id: str
    name: str
    type: str
    is_active: bool
    auth_policy: str
    allowed_user_auth_modes: Optional[list] = None
    config: dict  # Non-sensitive connection parameters
    last_synced_at: Optional[str] = None
    organization_id: str
    table_count: int = 0
    tool_count: int = 0
    agent_count: int = 0
    agent_names: List[str] = []  # Names of linked agents (for delete confirmation)
    has_credentials: bool = False  # Whether system credentials are set
    # Non-secret credential fields, safe to send back so the edit form can
    # pre-fill them (OAuth endpoints, client_id, scopes, api_key header). Secrets
    # (client_secret, token, api_key, password) are NEVER included.
    credentials_meta: Optional[dict] = None
    # Scheduled auto-reindex config (enterprise `scheduled_reindex` feature).
    auto_reindex_enabled: bool = True
    reindex_interval_hours: Optional[int] = None  # NULL -> default cadence
    reindex_schedule_mode: str = "interval"  # "interval" | "time"
    reindex_interval_minutes: Optional[int] = None  # NULL -> default cadence
    reindex_at_time: Optional[str] = None  # "HH:MM" when mode == "time"
    next_retry_at: Optional[str] = None
    last_reindex_error: Optional[str] = None
    # Per-connection request rate limit (enterprise `connection_rate_limit`).
    rate_limit_enabled: bool = False
    rate_limit_per_minute: Optional[int] = None
    rate_limit_per_hour: Optional[int] = None
    rate_limit_per_day: Optional[int] = None

    class Config:
        from_attributes = True


class ConnectionTableSchema(BaseModel):
    """Schema for connection table info."""
    id: str
    name: str
    column_count: int = 0
    
    class Config:
        from_attributes = True


class ConnectionTestOverride(BaseModel):
    """Optional overrides when testing a connection with new (unsaved) values."""
    config: Optional[dict] = None
    credentials: Optional[dict] = None


class ConnectionTestResult(BaseModel):
    """Schema for connection test results."""
    success: bool
    message: str
    connectivity: bool = False
    schema_access: bool = False
    table_count: int = 0
    # Optional richer info; older consumers ignore these.
    timings: Optional[Dict[str, float]] = None
    details: Optional[Dict[str, Any]] = None


class ConnectionIndexingProgress(BaseModel):
    """Lightweight payload inlined into the connection payload and returned
    from the indexing polling endpoint.
    """
    id: str
    status: str  # pending | running | completed | failed | cancelled
    phase: Optional[str] = None
    current_item: Optional[str] = None
    progress_done: int = 0
    progress_total: int = 0
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    error: Optional[str] = None
    stats: Optional[Dict[str, Any]] = None
    events: Optional[List[Dict[str, Any]]] = None

    class Config:
        from_attributes = True


class ConnectionIndexingSchema(ConnectionIndexingProgress):
    """Full indexing row (same shape today; kept separate for future expansion)."""
    connection_id: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

