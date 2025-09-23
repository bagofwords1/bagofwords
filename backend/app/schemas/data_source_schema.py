from pydantic import BaseModel
from typing import Optional


class DataSourceSummarySchema(BaseModel):
    id: str
    name: str
    type: str
    context: Optional[str] = None

    class Config:
        from_attributes = True

from pydantic import BaseModel, Field, validator
from typing import Optional, Dict, Any, List
from app.schemas.data_source_registry import default_credentials_schema_for
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


class DataSourceBase(BaseModel):
    name: str = None
    type: str = None  # e.g., "postgresql", "bigquery", "netsuite"
    config: dict = None  # JSON config, will be validated based on the type


class DataSourceSchema(DataSourceBase):
    id: str
    organization_id: str
    created_at: datetime
    updated_at: datetime
    context: Optional[str]
    description: Optional[str]
    summary: Optional[str]
    conversation_starters: Optional[list]
    is_active: bool
    is_public: bool = False
    use_llm_sync: bool = False
    owner_user_id: Optional[str] = None
    config: Dict[str, Any]
    git_repository: Optional[GitRepositorySchema] = None
    memberships: Optional[List[DataSourceMembershipSchema]] = []

    @validator('config', pre=True)
    def parse_config(cls, value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                raise ValueError('Invalid JSON string for config')
        return value

    @validator('git_repository', pre=True)
    def validate_git_repository(cls, v):
        if v is None:
            return None
        try:
            if isinstance(v, list):
                return v[-1] if v else None
            return v
        except Exception:
            return None

    class Config:
        from_attributes = True
        arbitrary_types_allowed = True


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
        schema_cls = default_credentials_schema_for(ds_type)
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
    member_user_ids: Optional[List[str]] = None  # User IDs to grant access to

    class Config:
        from_attributes = True


class DataSourceInDBBase(DataSourceBase):
    id: str
    credentials: Optional[str]

    class Config:
        orm_mode = True


