from datetime import datetime
from enum import Enum
from typing import Optional, List

from pydantic import BaseModel, Field

from app.schemas.data_source_schema import DataSourceSchema, DataSourceMinimalSchema
from app.schemas.instruction_label_schema import InstructionLabelSchema
from app.schemas.instruction_reference_schema import InstructionReferenceSchema, InstructionReferenceCreate
from app.schemas.user_schema import UserSchema

class InstructionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"

class InstructionPrivateStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"

class InstructionGlobalStatus(str, Enum):
    SUGGESTED = "suggested"
    APPROVED = "approved"
    REJECTED = "rejected"

class InstructionCategory(str, Enum):
    CODE_GEN = "code_gen"
    DATA_MODELING = "data_modeling"
    GENERAL = "general"
    DASHBOARD = "dashboard"
    VISUALIZATION = "visualization"

class InstructionBase(BaseModel):
    text: str
    thumbs_up: int = 0
    status: str = "published"  # Overall status for visibility
    category: str = "general"
    
    # Dual-status lifecycle fields
    private_status: Optional[str] = None    # draft, published, archived (null for global-only)
    global_status: Optional[str] = None     # null, suggested, approved, rejected
    
    # User experience controls
    is_seen: bool = True          # visible in UI lists
    can_user_toggle: bool = True  # user can enable/disable
    
    # Audit and relationships
    reviewed_by_user_id: Optional[str] = None
    source_instruction_id: Optional[str] = None
    # If created by AI, the provenance source label (e.g., 'completion')
    ai_source: Optional[str] = None

class InstructionCreate(InstructionBase):
    data_source_ids: Optional[List[str]] = []  # Empty list means applies to all data sources
    references: Optional[List[InstructionReferenceCreate]] = []
    label_ids: Optional[List[str]] = []  # Optional labels applied to this instruction

class InstructionUpdate(BaseModel):
    text: Optional[str] = None
    status: Optional[str] = None
    category: Optional[str] = None
    private_status: Optional[str] = None
    global_status: Optional[str] = None
    is_seen: Optional[bool] = None
    can_user_toggle: Optional[bool] = None
    data_source_ids: Optional[List[str]] = None
    is_admin_approval: Optional[bool] = False
    references: Optional[List[InstructionReferenceCreate]] = None
    label_ids: Optional[List[str]] = None

# Simplified schema without complex computed properties
class InstructionSchema(InstructionBase):
    id: str
    user_id: Optional[str] = None
    organization_id: str
    user: Optional[UserSchema] = None
    reviewed_by: Optional[UserSchema] = None
    data_sources: List[DataSourceSchema] = []
    references: List[InstructionReferenceSchema] = []
    labels: List[InstructionLabelSchema] = []
    created_at: datetime
    updated_at: datetime
    agent_execution_id: Optional[str] = None
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

class InstructionListSchema(BaseModel):
    """Schema for listing instructions without full relationships"""
    id: str
    text: str
    status: str
    category: str
    user_id: Optional[str] = None
    user: Optional[UserSchema] = None
    organization_id: str
    
    # Dual-status lifecycle fields
    private_status: Optional[str] = None
    global_status: Optional[str] = None
    is_seen: bool
    can_user_toggle: bool
    reviewed_by_user_id: Optional[str] = None
    # If created by AI, the provenance source label (e.g., 'completion')
    ai_source: Optional[str] = None
    
    # Minimal DS projection for list view
    data_sources: List[DataSourceMinimalSchema] = []
    labels: List[InstructionLabelSchema] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True

    @property
    def instruction_type(self) -> str:
        """Returns the type of instruction based on status combination"""
        if self.private_status and not self.global_status:
            return "private"
        elif self.private_status and self.global_status == "suggested":
            return "suggested"
        elif not self.private_status and self.global_status == "approved":
            return "global"
        else:
            return "unknown"

# Additional schemas for specific operations
class InstructionSuggestResponse(BaseModel):
    """Response when suggesting an instruction"""
    instruction: InstructionSchema
    message: str = "Instruction suggested for review"

class InstructionReviewResponse(BaseModel):
    """Response when approving/rejecting a suggestion"""
    instruction: InstructionSchema
    message: str
    reviewed_by: UserSchema

class InstructionStatsSchema(BaseModel):
    """Statistics about instructions"""
    total_private: int
    total_suggestions: int
    total_global: int
    user_private: int
    user_suggestions: int
    user_global_owned: int
