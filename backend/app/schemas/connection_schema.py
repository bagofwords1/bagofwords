"""
Connection schemas for database connection management.
"""
from datetime import datetime
from typing import Any, Dict, Optional, List

from pydantic import BaseModel

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


class ConnectionSchema(BaseModel):
    """Schema for connection list view."""
    id: str
    name: str
    type: str
    is_active: bool
    auth_policy: str
    last_synced_at: Optional[str] = None
    organization_id: str
    table_count: int = 0
    domain_count: int = 0
    domain_names: List[str] = []  # Names of linked domains (for delete confirmation)
    indexing: Optional[Dict[str, Any]] = None

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
    domain_count: int = 0
    domain_names: List[str] = []  # Names of linked domains (for delete confirmation)
    has_credentials: bool = False  # Whether system credentials are set

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

