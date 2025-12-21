from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from app.schemas.base import UTCDatetime


class InstructionVersionBase(BaseModel):
    """Base schema for InstructionVersion."""
    text: str
    title: Optional[str] = None
    structured_data: Optional[Dict[str, Any]] = None
    formatted_content: Optional[str] = None
    load_mode: str = Field(default='always', description="Loading behavior: always | intelligent | disabled")
    references_json: Optional[List[Dict[str, Any]]] = None
    data_source_ids: Optional[List[str]] = None
    label_ids: Optional[List[str]] = None
    category_ids: Optional[List[str]] = None


class InstructionVersionCreate(InstructionVersionBase):
    """Schema for creating a version from explicit data."""
    instruction_id: str


class InstructionVersionSchema(BaseModel):
    """Full schema for InstructionVersion."""
    id: str
    instruction_id: str
    version_number: int
    
    # Content
    text: str
    title: Optional[str] = None
    structured_data: Optional[Dict[str, Any]] = None
    formatted_content: Optional[str] = None
    
    # Loading behavior
    load_mode: str = 'always'
    
    # Relationships as JSON (denormalized)
    references_json: Optional[List[Dict[str, Any]]] = None
    data_source_ids: Optional[List[str]] = None
    label_ids: Optional[List[str]] = None
    category_ids: Optional[List[str]] = None
    
    # Hash
    content_hash: str
    
    # Audit
    created_by_user_id: Optional[str] = None
    created_at: Optional[UTCDatetime] = None
    
    class Config:
        from_attributes = True


class InstructionVersionListSchema(BaseModel):
    """Lighter schema for version lists."""
    id: str
    instruction_id: str
    version_number: int
    title: Optional[str] = None
    content_hash: str
    load_mode: str = 'always'
    created_at: Optional[UTCDatetime] = None
    created_by_user_id: Optional[str] = None
    
    class Config:
        from_attributes = True


class VersionChangeSchema(BaseModel):
    """Schema for a single field change between versions."""
    field: str
    from_value: Any = Field(None, alias="from")
    to_value: Any = Field(None, alias="to")


class VersionCompareSchema(BaseModel):
    """Schema for comparing two versions."""
    version_a: Dict[str, Any]
    version_b: Dict[str, Any]
    changes: List[VersionChangeSchema]
    has_changes: bool


class PaginatedVersionResponse(BaseModel):
    """Paginated response for version lists."""
    items: List[InstructionVersionListSchema]
    total: int
    page: int
    per_page: int
    pages: int
    instruction_id: str

