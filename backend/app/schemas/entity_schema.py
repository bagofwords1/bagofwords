from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime

from app.schemas.view_schema import ViewSchema
from app.schemas.data_source_schema import DataSourceMinimalSchema
from app.schemas.user_schema import UserSchema


class EntityBase(BaseModel):
    type: str  # 'model' | 'metric'
    title: str = ""
    slug: str
    description: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    code: str  # SQL or expression
    data: Dict[str, Any] = Field(default_factory=dict)
    view: Optional[ViewSchema] = None
    status: str = "draft"  # 'draft' | 'published'
    published_at: Optional[datetime] = None
    pinned: bool = False
    last_refreshed_at: Optional[datetime] = None
    auto_refresh_enabled: bool = False
    auto_refresh_interval: Optional[int] = None
    auto_refresh_interval_unit: Optional[str] = None


class EntityCreate(EntityBase):
    data_source_ids: Optional[List[str]] = []


class EntityUpdate(BaseModel):
    type: Optional[str] = None
    title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    code: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    view: Optional[ViewSchema] = None
    status: Optional[str] = None
    published_at: Optional[datetime] = None
    last_refreshed_at: Optional[datetime] = None
    data_source_ids: Optional[List[str]] = None


class EntityFromStepCreate(BaseModel):
    type: Optional[str] = None
    title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    publish: Optional[bool] = False


class EntitySchema(EntityBase):
    id: str
    organization_id: str
    owner_id: str
    owner: Optional[UserSchema] = None
    data_sources: List[DataSourceMinimalSchema] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class EntityListSchema(BaseModel):
    id: str
    type: str
    title: str
    description: Optional[str] = None
    slug: str
    status: str
    organization_id: str
    owner_id: str
    data_sources: List[DataSourceMinimalSchema] = []
    updated_at: datetime
    pinned: bool = False
    auto_refresh_enabled: bool = False
    auto_refresh_interval: Optional[int] = None
    auto_refresh_interval_unit: Optional[str] = None

    class Config:
        from_attributes = True


