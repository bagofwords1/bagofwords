from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any

from pydantic import BaseModel, Field

from app.schemas.data_source_schema import DataSourceSchema, DataSourceMinimalSchema
from app.schemas.instruction_label_schema import InstructionLabelSchema
from app.schemas.instruction_reference_schema import InstructionReferenceSchema, InstructionReferenceCreate
from app.schemas.user_schema import UserSchema
from app.schemas.base import UTCDatetime

class InstructionStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"

# DEPRECATED: These enums are no longer used - approval workflow moved to builds
class InstructionPrivateStatus(str, Enum):
    """DEPRECATED: Not used - kept for backward compatibility"""
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"

class InstructionGlobalStatus(str, Enum):
    """DEPRECATED: Not used - approval workflow moved to builds"""
    SUGGESTED = "suggested"
    APPROVED = "approved"
    REJECTED = "rejected"

class InstructionCategory(str, Enum):
    CODE_GEN = "code_gen"
    DATA_MODELING = "data_modeling"
    GENERAL = "general"
    DASHBOARD = "dashboard"
    VISUALIZATION = "visualization"

class InstructionSourceType(str, Enum):
    USER = "user"
    AI = "ai"
    GIT = "git"

class InstructionLoadMode(str, Enum):
    ALWAYS = "always"       # Always included in AI context
    INTELLIGENT = "intelligent"  # Included based on search relevance
    DISABLED = "disabled"   # Never included in AI context

class InstructionBase(BaseModel):
    text: str
    thumbs_up: int = 0
    status: str = "published"  # Content lifecycle: draft | published | archived
    category: str = "general"
    
    # DEPRECATED: Dual-status lifecycle fields (approval workflow moved to builds)
    private_status: Optional[str] = None    # DEPRECATED - not used
    global_status: Optional[str] = None     # DEPRECATED - not used
    
    # User experience controls
    is_seen: bool = True          # visible in UI lists
    can_user_toggle: bool = True  # user can enable/disable
    
    # DEPRECATED: Audit (moved to builds)
    reviewed_by_user_id: Optional[str] = None  # DEPRECATED - not used
    source_instruction_id: Optional[str] = None
    # If created by AI, the provenance source label (e.g., 'completion')
    ai_source: Optional[str] = None
    
    # === Unified Instructions System fields ===
    
    # Source tracking
    source_type: str = "user"  # 'user' | 'ai' | 'git'
    
    # Git source info (populated when source_type='git')
    source_metadata_resource_id: Optional[str] = None
    source_git_commit_sha: Optional[str] = None
    source_sync_enabled: bool = True  # False when user "unlinks" from git
    source_file_path: Optional[str] = None  # Git file path for 1 file = 1 instruction
    content_hash: Optional[str] = None  # SHA-256 hash for change detection
    
    # Loading behavior for AI context
    load_mode: str = "always"  # 'always' | 'intelligent' | 'disabled'
    
    # Display title (especially for git-sourced instructions)
    title: Optional[str] = None
    
    # Structured data (raw resource data) + formatted content (readable text)
    structured_data: Optional[Dict[str, Any]] = None
    formatted_content: Optional[str] = None

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

    # Unified Instructions System fields
    load_mode: Optional[str] = None  # 'always' | 'intelligent' | 'disabled'
    title: Optional[str] = None
    source_sync_enabled: Optional[bool] = None  # Set to False to unlink from git

    # Build targeting - if set, update within this existing build instead of creating new one
    target_build_id: Optional[str] = None


class InstructionBulkUpdate(BaseModel):
    """Schema for bulk updating multiple instructions"""
    ids: List[str]  # List of instruction IDs to update
    # Updates to apply to all selected instructions
    status: Optional[str] = None  # 'draft' | 'published' | 'archived'
    load_mode: Optional[str] = None  # 'always' | 'intelligent' | 'disabled'
    set_label_ids: Optional[List[str]] = None  # Replace all labels (empty list = clear labels)
    add_label_ids: Optional[List[str]] = None  # Labels to add
    remove_label_ids: Optional[List[str]] = None  # Labels to remove
    
    # Data source (scope) operations
    set_data_source_ids: Optional[List[str]] = None  # Replace all data sources (empty list = make global)
    add_data_source_ids: Optional[List[str]] = None  # Add these data sources
    remove_data_source_ids: Optional[List[str]] = None  # Remove these data sources


class InstructionBulkDelete(BaseModel):
    """Schema for bulk deleting multiple instructions"""
    ids: List[str]  # List of instruction IDs to delete


class InstructionBulkResponse(BaseModel):
    """Response for bulk update operations"""
    updated_count: int
    failed_ids: List[str] = []
    message: str

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
    created_at: UTCDatetime
    updated_at: UTCDatetime
    agent_execution_id: Optional[str] = None
    trigger_reason: Optional[str] = None

    # === Build System fields ===
    current_version_id: Optional[str] = None

    class Config:
        from_attributes = True

    def is_git_sourced(self) -> bool:
        return self.source_type == "git"
    
    def is_synced_with_git(self) -> bool:
        return self.source_type == "git" and self.source_sync_enabled
    
    @property
    def display_title(self) -> str:
        """Returns display title, falling back to text snippet"""
        if self.title:
            return self.title
        return self.text[:100] + "..." if len(self.text) > 100 else self.text

class InstructionListSchema(BaseModel):
    """Schema for listing instructions without full relationships"""
    id: str
    text: str
    status: str
    category: str
    user_id: Optional[str] = None
    user: Optional[UserSchema] = None
    organization_id: str
    
    # DEPRECATED: Dual-status lifecycle fields (approval workflow moved to builds)
    private_status: Optional[str] = None  # DEPRECATED - not used
    global_status: Optional[str] = None   # DEPRECATED - not used
    is_seen: bool
    can_user_toggle: bool
    reviewed_by_user_id: Optional[str] = None  # DEPRECATED - not used
    # If created by AI, the provenance source label (e.g., 'completion')
    ai_source: Optional[str] = None
    
    # === Unified Instructions System fields ===
    source_type: str = "user"
    source_metadata_resource_id: Optional[str] = None
    source_git_commit_sha: Optional[str] = None
    source_sync_enabled: bool = True
    load_mode: str = "always"
    title: Optional[str] = None
    structured_data: Optional[Dict[str, Any]] = None
    formatted_content: Optional[str] = None

    # === Build System fields ===
    current_version_id: Optional[str] = None

    # Minimal DS projection for list view
    data_sources: List[DataSourceMinimalSchema] = []
    labels: List[InstructionLabelSchema] = []
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True

    @property
    def display_title(self) -> str:
        """Returns display title, falling back to text snippet"""
        if self.title:
            return self.title
        return self.text[:100] + "..." if len(self.text) > 100 else self.text
    
    @property
    def is_git_sourced(self) -> bool:
        return self.source_type == "git"
    
    @property
    def is_synced_with_git(self) -> bool:
        return self.source_type == "git" and self.source_sync_enabled

class InstructionStatsSchema(BaseModel):
    """Statistics about instructions"""
    total_private: int
    total_suggestions: int
    total_global: int
    user_private: int
    user_suggestions: int
    user_global_owned: int
