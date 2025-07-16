from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime
from app.schemas.user_schema import UserSchema
from app.schemas.data_source_schema import DataSourceSchema
from enum import Enum

class InstructionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"

class InstructionCategory(str, Enum):
    CODE_GEN = "code_gen"
    DATA_MODELING = "data_modeling"
    GENERAL = "general"

class InstructionBase(BaseModel):
    text: str
    thumbs_up: int = 0
    status: InstructionStatus = InstructionStatus.DRAFT
    category: InstructionCategory = InstructionCategory.GENERAL

class InstructionCreate(InstructionBase):
    data_source_ids: Optional[List[str]] = []  # Empty list means applies to all data sources

class InstructionUpdate(BaseModel):
    text: Optional[str] = None
    thumbs_up: Optional[int] = None
    status: Optional[InstructionStatus] = None
    category: Optional[InstructionCategory] = None
    data_source_ids: Optional[List[str]] = None

class InstructionSchema(InstructionBase):
    id: str
    user_id: str
    organization_id: str
    user: UserSchema
    data_sources: List[DataSourceSchema] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @property
    def is_global(self) -> bool:
        """Returns True if this instruction applies to all data sources"""
        return len(self.data_sources) == 0

    @property
    def data_source_names(self) -> List[str]:
        """Returns list of data source names this instruction applies to"""
        return [ds.name for ds in self.data_sources]

class InstructionListSchema(BaseModel):
    """Schema for listing instructions without full relationships"""
    id: str
    text: str
    thumbs_up: int
    status: InstructionStatus
    category: InstructionCategory
    user_id: str
    organization_id: str
    data_source_count: int = 0  # Number of associated data sources
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
