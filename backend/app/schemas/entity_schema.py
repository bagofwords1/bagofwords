from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum

from app.schemas.view_schema import ViewSchema
from app.schemas.data_source_schema import DataSourceMinimalSchema
from app.schemas.user_schema import UserSchema
from app.schemas.base import OptionalUTCDatetime, UTCDatetime


class EntityPrivateStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class EntityGlobalStatus(str, Enum):
    SUGGESTED = "suggested"
    APPROVED = "approved"
    REJECTED = "rejected"


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
    published_at: OptionalUTCDatetime = None
    pinned: bool = False
    last_refreshed_at: OptionalUTCDatetime = None
    auto_refresh_enabled: bool = False
    auto_refresh_interval: Optional[int] = None
    auto_refresh_interval_unit: Optional[str] = None
    
    # Dual-status lifecycle fields
    private_status: Optional[str] = None    # draft, published, archived (null for global-only)
    global_status: Optional[str] = None     # null, suggested, approved, rejected
    
    # Audit and relationships
    reviewed_by_user_id: Optional[str] = None
    ai_source: Optional[str] = None  # If created by AI, the provenance source label


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
    published_at: OptionalUTCDatetime = None
    last_refreshed_at: OptionalUTCDatetime = None
    data_source_ids: Optional[List[str]] = None
    private_status: Optional[str] = None
    global_status: Optional[str] = None
    is_admin_approval: Optional[bool] = False


class EntityFromStepCreate(BaseModel):
    type: Optional[str] = None
    title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    publish: Optional[bool] = False
    data_source_ids: Optional[List[str]] = None


class EntitySchema(EntityBase):
    id: str
    organization_id: str
    owner_id: str
    owner: Optional[UserSchema] = None
    reviewed_by: Optional[UserSchema] = None
    data_sources: List[DataSourceMinimalSchema] = []
    created_at: UTCDatetime
    updated_at: UTCDatetime
    source_step_id: Optional[str] = None
    trigger_reason: Optional[str] = None

    class Config:
        from_attributes = True
    
    # Keep only essential helpers
    def is_suggested(self) -> bool:
        return self.global_status == "suggested"
    
    def is_global(self) -> bool:
        return self.global_status == "approved"
    
    def is_private(self) -> bool:
        return self.private_status == "published" and not self.global_status


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
    updated_at: UTCDatetime
    pinned: bool = False
    auto_refresh_enabled: bool = False
    auto_refresh_interval: Optional[int] = None
    auto_refresh_interval_unit: Optional[str] = None
    
    # Dual-status lifecycle fields
    private_status: Optional[str] = None
    global_status: Optional[str] = None
    reviewed_by_user_id: Optional[str] = None

    class Config:
        from_attributes = True
    
    @property
    def entity_type(self) -> str:
        """Returns the type of entity based on status combination"""
        if self.private_status and not self.global_status:
            return "private"
        elif self.private_status and self.global_status == "suggested":
            return "suggested"
        elif not self.private_status and self.global_status == "approved":
            return "global"
        else:
            return "unknown"


class EntityRunPayload(BaseModel):
    type: Optional[str] = None
    title: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None
    view: Optional[ViewSchema] = None
    status: Optional[str] = None


class EntityPreviewPayload(BaseModel):
    code: str


