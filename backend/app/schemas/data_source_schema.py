from pydantic import BaseModel
from typing import Optional


class DataSourceSummarySchema(BaseModel):
    """Minimal DataSource schema for summaries."""
    id: str
    name: str
    type: Optional[str] = None  # Computed from connection
    context: Optional[str] = None

    class Config:
        from_attributes = True

class DataSourceMinimalSchema(BaseModel):
    """Minimal DataSource schema."""
    id: str
    name: str
    type: Optional[str] = None  # Computed from connection
    description: Optional[str] = None

    class Config:
        from_attributes = True

from pydantic import BaseModel, Field, validator
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
    is_active: bool
    is_public: bool = False
    owner_user_id: Optional[str] = None
    use_llm_sync: bool = False
    
    # Connection info (Option A: nested connection)
    connection: Optional[ConnectionEmbedded] = None
    
    # Legacy fields for backward compatibility - computed from connection
    type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    # Note: NO memberships field here
    
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
    use_llm_sync: bool = False
    owner_user_id: Optional[str] = None
    git_repository: Optional[GitRepositorySchema] = None
    memberships: Optional[List[DataSourceMembershipSchema]] = []
    
    # Connection info (Option A: nested connection)
    connection: Optional[ConnectionEmbedded] = None
    
    # Legacy fields for backward compatibility - computed from connection
    type: Optional[str] = None
    config: Optional[Dict[str, Any]] = None
    auth_policy: Optional[str] = None
    allowed_user_auth_modes: Optional[List[str]] = None
    user_status: Optional[DataSourceUserStatus] = None

    class Config:
        from_attributes = True


class DataSourceListItemSchema(BaseModel):
    """List item schema for DataSource with nested connection info."""
    id: str
    name: str
    description: Optional[str]
    conversation_starters: Optional[list] = None
    created_at: UTCDatetime
    status: str  # "active" | "inactive"
    
    # Connection info (Option A: nested connection)
    connection: Optional[ConnectionEmbedded] = None
    
    # Legacy fields for backward compatibility - computed from connection
    type: Optional[str] = None
    auth_policy: Optional[str] = None
    user_status: Optional[DataSourceUserStatus] = None

    class Config:
        from_attributes = True


class DataSourceCreate(DataSourceBase):
    """Schema for creating a new DataSource (Domain).
    
    Two modes:
    1. Create new connection: Provide type, config, credentials
    2. Link to existing connection: Provide connection_id (skips connection creation)
    """
    # Option 1: Connection-related fields (will be used to create Connection)
    type: Optional[str] = None  # Connection type: e.g., "postgresql", "bigquery", "netsuite"
    config: Optional[dict] = None  # Connection config, will be validated based on the type
    credentials: Optional[dict] = None  # Will be validated based on the data source type
    auth_policy: str = "system_only"
    allowed_user_auth_modes: Optional[List[str]] = None
    
    # Option 2: Link to existing connection
    connection_id: Optional[str] = None  # If provided, link to existing connection instead of creating new
    
    # Domain-specific fields
    generate_summary: bool = False
    generate_conversation_starters: bool = False
    generate_ai_rules: bool = False
    is_public: bool = False
    use_llm_sync: bool = False
    member_user_ids: Optional[List[str]] = []  # User IDs to grant access to

    @validator('credentials')
    def validate_credentials(cls, v, values):
        # Skip validation if linking to existing connection
        if values.get('connection_id'):
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
    
    @validator('connection_id', always=True)
    def validate_connection_or_type(cls, v, values):
        """Ensure either connection_id OR (type, config, credentials) is provided."""
        if v:
            # Using existing connection - no need for type/config/credentials
            return v
        
        # Creating new connection - require type and config
        if not values.get('type'):
            raise ValueError('Either connection_id or type must be provided')
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
    member_user_ids: Optional[List[str]] = None  # User IDs to grant access to
    
    # Connection-related fields (will be delegated to Connection update)
    config: Optional[dict] = None
    credentials: Optional[dict] = None
    auth_policy: Optional[str] = None

    class Config:
        from_attributes = True


class DataSourceInDBBase(DataSourceBase):
    """Internal schema for DataSource in DB."""
    id: str
    # Connection info
    connection: Optional[ConnectionEmbedded] = None

    class Config:
        from_attributes = True


