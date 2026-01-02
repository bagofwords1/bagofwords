"""
Connection schemas for database connection management.
"""
from datetime import datetime
from typing import Optional, List

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


class ConnectionTestResult(BaseModel):
    """Schema for connection test results."""
    success: bool
    message: str
    connectivity: bool = False
    schema_access: bool = False
    table_count: int = 0

