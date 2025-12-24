from sqlalchemy import Column, String, Text, Integer, Boolean, ForeignKey, Table, JSON
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema
from app.models.instruction_label import instruction_label_association

# Association table for many-to-many relationship between instructions and data sources
instruction_data_source_association = Table(
    'instruction_data_source_association',
    BaseSchema.metadata,
    Column('instruction_id', String(36), ForeignKey('instructions.id'), primary_key=True),
    Column('data_source_id', String(36), ForeignKey('data_sources.id'), primary_key=True)
)

class Instruction(BaseSchema):
    __tablename__ = "instructions"
    
    # Core instruction content
    text = Column(Text, nullable=False)
    
    # Rating/approval system
    thumbs_up = Column(Integer, nullable=False, default=0)
    
    # Overall status for visibility/usability
    status = Column(String(50), nullable=False, default="draft")
    
    # Categorization
    category = Column(String(50), nullable=False, default="general")
    
    # User who created the instruction (always the original creator)
    user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    
    # DEPRECATED: Dual-status lifecycle management (approval workflow moved to builds)
    # Kept for backward compatibility - will be removed in future migration
    private_status = Column(String(50), nullable=True)  # DEPRECATED - not used
    global_status = Column(String(50), nullable=True)   # DEPRECATED - not used
    
    # User experience controls
    is_seen = Column(Boolean, nullable=False, default=True)         # visible in UI lists
    can_user_toggle = Column(Boolean, nullable=False, default=True) # user can enable/disable
    
    # DEPRECATED: Audit trail (approval tracking moved to builds)
    reviewed_by_user_id = Column(String(36), ForeignKey('users.id'), nullable=True)  # DEPRECATED - not used
    
    # Legacy field - keeping for potential future use
    source_instruction_id = Column(String(36), ForeignKey('instructions.id'), nullable=True)

    # if ai generated:
    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=True)
    trigger_reason = Column(String(255), nullable=True)
    # provenance label for AI-created instructions (e.g., 'completion', 'batch_job')
    ai_source = Column(String(50), nullable=True)
    
    # Organization ownership
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    
    # === Unified Instructions System fields ===
    
    # Source tracking: where this instruction came from
    source_type = Column(String(20), default='user')  # 'user' | 'ai' | 'git'
    
    # Git source info (only populated when source_type='git')
    source_metadata_resource_id = Column(String(36), ForeignKey('metadata_resources.id'), nullable=True)
    source_git_commit_sha = Column(String(40), nullable=True)
    source_sync_enabled = Column(Boolean, default=True)  # False when user "unlinks" from git
    source_file_path = Column(String(500), nullable=True)  # Git file path for 1 file = 1 instruction
    content_hash = Column(String(64), nullable=True)  # SHA-256 hash for fast change detection
    
    # Loading behavior for AI context
    load_mode = Column(String(20), default='always')  # 'always' | 'intelligent' | 'disabled'
    
    # Display title (especially for git-sourced instructions)
    title = Column(String(255), nullable=True)
    
    # Structured data (raw resource data) + formatted content (readable text)
    structured_data = Column(JSON, nullable=True)
    formatted_content = Column(Text, nullable=True)
    
    # === Build System fields ===
    # Points to the currently active version of this instruction
    # Note: FK constraint not enforced at DB level for SQLite compatibility
    current_version_id = Column(String(36), nullable=True)
    
    # Relationships
    data_sources = relationship(
        "DataSource",
        secondary=instruction_data_source_association,
        back_populates="instructions",
        lazy="selectin",
        passive_deletes=True,
    )
    labels = relationship(
        "InstructionLabel",
        secondary=instruction_label_association,
        back_populates="instructions",
        lazy="selectin",
    )
    user = relationship("User", foreign_keys=[user_id], lazy="selectin")
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_user_id], lazy="selectin")
    organization = relationship("Organization")
    references = relationship("InstructionReference", back_populates="instruction", lazy="selectin", cascade="all, delete-orphan")
    agent_execution = relationship("AgentExecution", foreign_keys=[agent_execution_id], lazy="selectin")
    source_metadata_resource = relationship("MetadataResource", foreign_keys=[source_metadata_resource_id], lazy="selectin")
    
    # Version history relationship
    versions = relationship(
        "InstructionVersion",
        back_populates="instruction",
        lazy="selectin",
        foreign_keys="InstructionVersion.instruction_id",
        cascade="all, delete-orphan"
    )
    current_version = relationship(
        "InstructionVersion",
        primaryjoin="Instruction.current_version_id == InstructionVersion.id",
        foreign_keys="Instruction.current_version_id",
        lazy="selectin",
        post_update=True,
        uselist=False
    )
    
    # Usage tracking relationships
    usage_events = relationship("InstructionUsageEvent", back_populates="instruction", lazy="dynamic")
    feedback_events = relationship("InstructionFeedbackEvent", back_populates="instruction", lazy="dynamic")
    stats = relationship("InstructionStats", back_populates="instruction", lazy="dynamic")
    
    def __repr__(self):
        return f"<Instruction {self.category}:{self.text[:50]}...>"
    
    @property
    def is_published(self) -> bool:
        """Returns True if the instruction is published and visible"""
        return self.status == "published"
    
    @property
    def is_draft(self) -> bool:
        """Returns True if the instruction is in draft status"""
        return self.status == "draft"
    
    @property
    def is_archived(self) -> bool:
        """Returns True if the instruction is archived"""
        return self.status == "archived"
    
    @property
    def is_global_data_sources(self) -> bool:
        """Returns True if this instruction applies to all data sources (no specific data sources assigned)"""
        return len(self.data_sources) == 0
    
    @property
    def applies_to_specific_data_sources(self) -> bool:
        """Returns True if this instruction is linked to specific data sources"""
        return len(self.data_sources) > 0
    
    @property
    def data_source_names(self) -> list[str]:
        """Returns list of data source names this instruction applies to"""
        return [ds.name for ds in self.data_sources]

    @property
    def label_names(self) -> list[str]:
        """Returns list of label names applied to this instruction"""
        return [label.name for label in self.labels]
    
    @property
    def is_git_sourced(self) -> bool:
        """Returns True if this instruction originated from a git repository"""
        return self.source_type == 'git'
    
    @property
    def is_ai_generated(self) -> bool:
        """Returns True if this instruction was AI-generated"""
        return self.source_type == 'ai' or self.ai_source is not None
    
    @property
    def is_user_created(self) -> bool:
        """Returns True if this instruction was created by a user"""
        return self.source_type == 'user' and self.ai_source is None
    
    @property
    def is_synced_with_git(self) -> bool:
        """Returns True if this git instruction is still synced (not unlinked)"""
        return self.is_git_sourced and self.source_sync_enabled
    
    @property
    def display_title(self) -> str:
        """Returns display title, falling back to text snippet"""
        if self.title:
            return self.title
        return self.text[:100] + "..." if len(self.text) > 100 else self.text


# Deferred imports to resolve circular dependencies for relationships
# These models reference Instruction, and Instruction references them back
from app.models.instruction_usage_event import InstructionUsageEvent  # noqa: E402, F401
from app.models.instruction_feedback_event import InstructionFeedbackEvent  # noqa: E402, F401
from app.models.instruction_stats import InstructionStats  # noqa: E402, F401
