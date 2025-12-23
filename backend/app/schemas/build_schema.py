from datetime import datetime
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field

from app.schemas.base import UTCDatetime


class InstructionBuildBase(BaseModel):
    """Base schema for InstructionBuild."""
    source: str = Field(default='user', description="Source of the build: user | git | ai")
    commit_sha: Optional[str] = Field(None, description="Git commit SHA (for git source)")
    branch: Optional[str] = Field(None, description="Git branch name (for git source)")


class InstructionBuildCreate(InstructionBuildBase):
    """Schema for creating a new build."""
    pass


class InstructionBuildSchema(BaseModel):
    """Full schema for InstructionBuild."""
    id: str
    build_number: int
    status: str  # draft | pending_approval | approved | rejected
    source: str  # user | git | ai
    is_main: bool
    
    # Trigger links
    metadata_indexing_job_id: Optional[str] = None
    agent_execution_id: Optional[str] = None
    
    # Git info
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    
    # Test integration
    test_run_id: Optional[str] = None
    test_status: Optional[str] = None
    
    # Statistics
    total_instructions: int = 0
    added_count: int = 0
    modified_count: int = 0
    removed_count: int = 0
    
    # Approval
    approved_by_user_id: Optional[str] = None
    approved_at: Optional[UTCDatetime] = None
    rejection_reason: Optional[str] = None
    
    # Git push info (populated when pushing TO git)
    git_branch_name: Optional[str] = None
    git_pr_url: Optional[str] = None
    git_pushed_at: Optional[UTCDatetime] = None
    
    # Organization and creator
    organization_id: str
    created_by_user_id: Optional[str] = None
    
    # Timestamps
    created_at: Optional[UTCDatetime] = None
    updated_at: Optional[UTCDatetime] = None
    
    class Config:
        from_attributes = True


class InstructionBuildListSchema(BaseModel):
    """Schema for build list item (lighter version)."""
    id: str
    build_number: int
    status: str
    source: str
    is_main: bool
    commit_sha: Optional[str] = None
    branch: Optional[str] = None
    total_instructions: int = 0
    added_count: int = 0
    modified_count: int = 0
    removed_count: int = 0
    created_at: Optional[UTCDatetime] = None
    approved_at: Optional[UTCDatetime] = None
    
    # Git push info
    git_branch_name: Optional[str] = None
    git_pr_url: Optional[str] = None
    git_pushed_at: Optional[UTCDatetime] = None
    
    # Test integration
    test_run_id: Optional[str] = None
    test_status: Optional[str] = None
    test_passed: Optional[int] = None
    test_failed: Optional[int] = None
    
    class Config:
        from_attributes = True


class BuildContentSchema(BaseModel):
    """Schema for BuildContent (instruction in a build)."""
    id: str
    build_id: str
    instruction_id: str
    instruction_version_id: str
    
    # From related InstructionVersion
    version_number: Optional[int] = None
    text: Optional[str] = None
    title: Optional[str] = None
    content_hash: Optional[str] = None
    load_mode: Optional[str] = None
    
    # From related Instruction
    instruction_status: Optional[str] = None
    instruction_category: Optional[str] = None
    
    class Config:
        from_attributes = True


class BuildContentCreateSchema(BaseModel):
    """Schema for adding/updating content in a build."""
    instruction_version_id: str


class ModifiedInstructionSchema(BaseModel):
    """Schema for a modified instruction in a diff."""
    instruction_id: str
    from_version_id: str
    to_version_id: str
    from_version_number: Optional[int] = None
    to_version_number: Optional[int] = None


class BuildDiffSchema(BaseModel):
    """Schema for diff between two builds."""
    build_a_id: str
    build_b_id: str
    added: List[str]  # instruction_ids added in B
    removed: List[str]  # instruction_ids removed in B
    modified: List[ModifiedInstructionSchema]
    added_count: int
    removed_count: int
    modified_count: int


class DiffInstructionItem(BaseModel):
    """Schema for a single instruction in a detailed diff."""
    instruction_id: str
    change_type: str  # 'added' | 'removed' | 'modified'
    title: Optional[str] = None
    text: str
    category: Optional[str] = None
    source_type: Optional[str] = None
    status: Optional[str] = None
    load_mode: Optional[str] = None
    
    # For modified instructions - previous version info
    previous_text: Optional[str] = None
    previous_title: Optional[str] = None
    previous_status: Optional[str] = None
    previous_load_mode: Optional[str] = None
    previous_category: Optional[str] = None
    
    # Summary of what fields changed
    changed_fields: Optional[List[str]] = None  # ['text', 'status', 'load_mode']
    
    from_version_number: Optional[int] = None
    to_version_number: Optional[int] = None


class BuildDiffDetailedSchema(BaseModel):
    """Schema for detailed diff between two builds with full instruction content."""
    build_a_id: str
    build_b_id: str
    build_a_number: int
    build_b_number: int
    items: List[DiffInstructionItem]
    added_count: int
    modified_count: int
    removed_count: int


class BuildSubmitSchema(BaseModel):
    """Schema for submitting a build."""
    pass  # No additional fields needed


class BuildApproveSchema(BaseModel):
    """Schema for approving a build."""
    pass  # No additional fields needed


class BuildRejectSchema(BaseModel):
    """Schema for rejecting a build."""
    reason: Optional[str] = Field(None, description="Reason for rejection")


class BuildPromoteSchema(BaseModel):
    """Schema for promoting a build to main."""
    pass  # No additional fields needed


class BuildRollbackSchema(BaseModel):
    """Schema for rolling back to a previous build."""
    pass  # No additional fields needed


class BuildDiffRequestSchema(BaseModel):
    """Schema for requesting a diff between builds."""
    compare_to: str = Field(..., description="ID of the build to compare against")


class PaginatedBuildResponse(BaseModel):
    """Paginated response for build lists."""
    items: List[InstructionBuildListSchema]
    total: int
    page: int
    per_page: int
    pages: int


class PaginatedBuildContentResponse(BaseModel):
    """Paginated response for build contents."""
    items: List[BuildContentSchema]
    total: int
    build_id: str
    build_number: int

