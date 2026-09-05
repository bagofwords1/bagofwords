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
    kind: str = "instruction"  # 'instruction' (normal) | 'skill'
    
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

    # Scoping: which agent run-modes and delivery channels this instruction
    # applies to. None or empty list => applies everywhere.
    applicable_modes: Optional[List[str]] = None      # e.g. ['chat', 'training']
    applicable_channels: Optional[List[str]] = None   # e.g. ['app', 'slack', 'teams', 'email', 'mcp']

    # Display title (especially for git-sourced instructions)
    title: Optional[str] = None

    # Optional user-authored description (advertised for skills)
    description: Optional[str] = None

    # Structured data (raw resource data) + formatted content (readable text)
    structured_data: Optional[Dict[str, Any]] = None
    formatted_content: Optional[str] = None

    # Reserved for pre-built skill provenance. None for user/AI rows.
    catalog_key: Optional[str] = None
    catalog_version: Optional[str] = None

class InstructionCreate(InstructionBase):
    data_source_ids: Optional[List[str]] = []  # Empty list means applies to all data sources
    references: Optional[List[InstructionReferenceCreate]] = []
    label_ids: Optional[List[str]] = []  # Optional labels applied to this instruction

class InstructionUpdate(BaseModel):
    text: Optional[str] = None
    status: Optional[str] = None
    category: Optional[str] = None
    kind: Optional[str] = None  # 'instruction' | 'skill'
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
    description: Optional[str] = None
    applicable_modes: Optional[List[str]] = None      # None = no change; [] = all modes
    applicable_channels: Optional[List[str]] = None   # None = no change; [] = all channels
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

    # Latest non-main build (draft or pending_approval) that contains this
    # instruction, if any. Populated by the service layer — not an ORM column.
    # Used by the UI to show "part of unpublished build" warnings.
    current_build_id: Optional[str] = None
    current_build_status: Optional[str] = None

    # Brief evidence stamped on the current version when it was AI-suggested
    # (create/edit_instruction tool calls). Populated by the service layer —
    # not an ORM column. None for user/git-authored versions.
    evidence: Optional[str] = None

    # Data sources for which this instruction is the primary — populated by service layer
    primary_for: List[DataSourceMinimalSchema] = []

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

#: How much of an instruction's body the light list projection carries. Long
#: enough that the tree label (title, else the first line) and the in-page
#: filter still work on ordinary instructions, short enough that the row stays
#: a few hundred bytes.
PREVIEW_CHARS = 280


def build_preview(text: Optional[str]) -> Optional[str]:
    """First `PREVIEW_CHARS` of an instruction body, for the light list."""
    if not text:
        return text
    return text[:PREVIEW_CHARS]


class InstructionListItemSchema(BaseModel):
    """Light projection for list/tree surfaces (`GET /instructions?view=light`).

    The full `InstructionListSchema` carries the whole instruction body three
    times over — `text`, `formatted_content`, and `structured_data` (which for a
    git-synced instruction holds the entire source resource: every column, its
    `sql_content`, AND `raw_data.content`, i.e. the file again). Measured on a
    half-dbt-synced org that is 6.2 KB per row, of which those three fields are
    81%, and the nested `UserSchema` repeats the author's record on every row.

    None of it is read by a list or tree, which needs a label, a few badges and
    the pending marker. Dropping it takes a row to a few hundred bytes, which is
    what makes "load every instruction" affordable instead of silently
    truncating at the page limit. Bodies come from `GET /instructions/{id}` when
    a row is opened; hunks already came from `/review-hunks` that way.
    """
    id: str
    title: Optional[str] = None
    #: Body prefix — the tree label falls back to it when `title` is unset.
    preview: Optional[str] = None
    status: str
    category: str
    kind: str = "instruction"
    load_mode: str = "always"
    source_type: str = "user"
    source_file_path: Optional[str] = None
    source_sync_enabled: bool = True
    #: Author id only; names resolve from the org member list the client holds.
    user_id: Optional[str] = None
    is_seen: bool
    can_user_toggle: bool
    ai_source: Optional[str] = None
    applicable_modes: Optional[List[str]] = None
    applicable_channels: Optional[List[str]] = None
    #: Catalog provenance for a row installed from the pre-built skill catalog
    #: (None for user- and AI-authored rows). Drives the "Pre-built" badge and
    #: lets a list surface tell an installed skill from a hand-written one.
    catalog_key: Optional[str] = None
    catalog_version: Optional[str] = None

    # Build system / pending-review markers (drive the amber "Pending review"
    # dot and the Pending changes view).
    current_version_id: Optional[str] = None
    current_build_id: Optional[str] = None
    current_build_status: Optional[str] = None
    pending_source: Optional[str] = None
    pending_created_by: Optional[str] = None
    pending_created_at: Optional[UTCDatetime] = None

    #: Ids of the `datasource_table` objects this instruction references — the
    #: one piece of reference data a list surface needs, because the tree files
    #: each instruction under the tables it references. The reference rows
    #: themselves stay off the light row; open the instruction for those.
    table_ref_ids: List[str] = []

    data_sources: List[DataSourceMinimalSchema] = []
    labels: List[InstructionLabelSchema] = []
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True


class InstructionListSchema(BaseModel):
    """Schema for listing instructions without full relationships"""
    id: str
    text: str
    status: str
    category: str
    kind: str = "instruction"  # 'instruction' (normal) | 'skill'
    user_id: Optional[str] = None
    user: Optional[UserSchema] = None
    organization_id: str
    
    # DEPRECATED: Dual-status lifecycle fields (approval workflow moved to builds)
    private_status: Optional[str] = None  # DEPRECATED - not used
    global_status: Optional[str] = None   # DEPRECATED - not used
    is_seen: bool
    can_user_toggle: bool
    reviewed_by_user_id: Optional[str] = None  # DEPRECATED - not used
    reviewed_by: Optional[UserSchema] = None  # User who approved the instruction
    # If created by AI, the provenance source label (e.g., 'completion')
    ai_source: Optional[str] = None
    
    # === Unified Instructions System fields ===
    source_type: str = "user"
    source_metadata_resource_id: Optional[str] = None
    source_file_path: Optional[str] = None
    source_git_commit_sha: Optional[str] = None
    source_sync_enabled: bool = True
    load_mode: str = "always"
    title: Optional[str] = None
    description: Optional[str] = None
    applicable_modes: Optional[List[str]] = None
    applicable_channels: Optional[List[str]] = None
    #: Catalog provenance for a row installed from the pre-built skill catalog.
    catalog_key: Optional[str] = None
    catalog_version: Optional[str] = None
    structured_data: Optional[Dict[str, Any]] = None
    formatted_content: Optional[str] = None

    # === Build System fields ===
    current_version_id: Optional[str] = None
    # In-flight (non-main) build for this instruction, if any, so the list can
    # derive a "Pending review" status the same way the detail view does.
    current_build_id: Optional[str] = None
    current_build_status: Optional[str] = None
    # Provenance of the live pending change (the newest non-main draft/pending
    # build that actually altered this instruction). Populated alongside
    # current_build_* so the "Pending changes" view can show who proposed it and
    # when without a per-instruction round-trip. None when there's no pending
    # change.
    pending_source: Optional[str] = None          # 'user' | 'ai' | 'git'
    pending_created_by: Optional[str] = None       # proposer name/email
    pending_created_at: Optional[UTCDatetime] = None

    # Minimal DS projection for list view
    data_sources: List[DataSourceMinimalSchema] = []
    labels: List[InstructionLabelSchema] = []
    created_at: UTCDatetime
    updated_at: UTCDatetime

    # Data sources for which this instruction is the primary — populated by service layer
    primary_for: List[DataSourceMinimalSchema] = []

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
