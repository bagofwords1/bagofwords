from pydantic import BaseModel
from typing import Optional


class DataSourceSummarySchema(BaseModel):
    id: str
    name: str
    type: str
    context: Optional[str] = None

    class Config:
        from_attributes = True

class DataSourceMinimalSchema(BaseModel):
    id: str
    name: str
    type: str
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

class DataSourceReportSchema(BaseModel):
    id: str
    name: str
    type: str
    organization_id: str
    created_at: datetime
    updated_at: datetime
    context: Optional[str]
    description: Optional[str]
    summary: Optional[str]
    is_active: bool
    is_public: bool = False
    owner_user_id: Optional[str] = None
    config: Dict[str, Any]
    use_llm_sync: bool = False
    # Note: NO memberships field here
    
    @validator('config', pre=True)
    def parse_config(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError('Invalid JSON string for config')
        return value
    
    class Config:
        from_attributes = True

class DataSourceMembershipSchema(BaseModel):
    id: str
    data_source_id: str
    principal_type: str  # "user" or "group"
    principal_id: str
    config: Optional[Dict[str, Any]] = None  # For future row-level access
    created_at: datetime
    updated_at: datetime

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
    last_used_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    connection: Literal["offline", "not_connected", "success", "unknown"] = "unknown"
    last_checked_at: Optional[datetime] = None
    effective_auth: Literal["user", "system", "none"] = "none"
    uses_fallback: bool = False
    credentials_id: Optional[str] = None


class DataSourceBase(BaseModel):
    name: str = None
    type: str = None  # e.g., "postgresql", "bigquery", "netsuite"
    config: dict = None  # JSON config, will be validated based on the type
    auth_policy: str = "system_only"
    allowed_user_auth_modes: Optional[List[str]] = None


class DataSourceSchema(DataSourceBase):
    class Config:
        from_attributes = True
    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime
    context: Optional[str]
    description: Optional[str]
    summary: Optional[str]
    conversation_starters: Optional[list] = None
    is_active: bool
    is_public: bool = False
    use_llm_sync: bool = False
    owner_user_id: Optional[str] = None
    auth_policy: str = "system_only"
    allowed_user_auth_modes: Optional[List[str]] = None
    config: Dict[str, Any]
    git_repository: Optional[GitRepositorySchema] = None
    memberships: Optional[List[DataSourceMembershipSchema]] = []
    user_status: Optional[DataSourceUserStatus] = None

    class Config:
        from_attributes = True

    @validator('config', pre=True)
    def parse_config(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError('Invalid JSON string for config')
        return value


class DataSourceListItemSchema(BaseModel):
    id: str
    name: str
    type: str
    auth_policy: str
    description: Optional[str]
    conversation_starters: Optional[list] = None
    created_at: datetime
    status: str  # "active" | "inactive"
    user_status: Optional[DataSourceUserStatus] = None

    class Config:
        from_attributes = True


class DataSourceCreate(DataSourceBase):
    credentials: dict  # Will be validated based on the data source type
    generate_summary: bool = False
    generate_conversation_starters: bool = False
    generate_ai_rules: bool = False
    is_public: bool = False
    use_llm_sync: bool = False
    member_user_ids: Optional[List[str]] = []  # User IDs to grant access to

    @validator('credentials')
    def validate_credentials(cls, v, values):
        if 'type' not in values:
            raise ValueError('Data source type must be specified')

        ds_type = values['type']
        # Prefer auth_type from config when present (selected by UI)
        cfg = values.get('config') or {}
        auth_type = (cfg or {}).get('auth_type')
        schema_cls = credentials_schema_for(ds_type, auth_type)
        return schema_cls(**v).dict()


class DataSourceUpdate(DataSourceBase):
    name: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    summary: Optional[str] = None
    config: Optional[dict] = None
    context: Optional[str] = None
    conversation_starters: Optional[list] = None
    credentials: Optional[dict] = None
    is_public: Optional[bool] = None
    use_llm_sync: Optional[bool] = None
    auth_policy: Optional[str] = None
    member_user_ids: Optional[List[str]] = None  # User IDs to grant access to

    class Config:
        from_attributes = True


class DataSourceInDBBase(DataSourceBase):
    id: str
    credentials: Optional[str]

    class Config:
        from_attributes = True


