from sqlalchemy import Column, String, Text, Integer, Boolean, ForeignKey, DateTime, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import BaseSchema


class InstructionBuild(BaseSchema):
    """
    Represents an immutable, point-in-time snapshot of all instructions.
    Similar to a Docker image - a build contains a specific set of instruction versions.
    Only one build per organization can have is_main=True (the active/live build).
    """
    __tablename__ = "instruction_builds"
    
    # Auto-incrementing build number per organization (1, 2, 3...)
    build_number = Column(Integer, nullable=False, index=True)
    
    # Auto-generated or user-provided title (e.g., "Added 2 instructions")
    title = Column(String(255), nullable=True)
    
    # Lifecycle status: draft | pending_approval | approved | rejected
    status = Column(String(20), nullable=False, default='draft')
    
    # Source of the build: git | user | ai
    source = Column(String(20), nullable=False, default='user')
    
    # Only ONE build per organization can have is_main=True
    is_main = Column(Boolean, default=False, nullable=False, index=True)
    
    # Base build this was forked from (for auto-merge on deploy)
    base_build_id = Column(String(36), ForeignKey('instruction_builds.id'), nullable=True)
    
    # Trigger links - one populated based on source
    metadata_indexing_job_id = Column(String(36), ForeignKey('metadata_indexing_jobs.id', ondelete='SET NULL'), nullable=True)
    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=True)
    
    # Git info (populated when source='git' - syncing FROM git)
    commit_sha = Column(String(40), nullable=True)
    branch = Column(String(255), nullable=True)
    
    # Git push info (populated when pushing TO git)
    git_branch_name = Column(String(255), nullable=True)  # e.g., "BOW-42"
    git_pr_url = Column(String(512), nullable=True)  # PR URL if created
    git_pushed_at = Column(DateTime, nullable=True)  # When pushed to git
    
    # Test integration
    test_run_id = Column(String(36), ForeignKey('test_runs.id'), nullable=True)
    test_status = Column(String(20), nullable=True)  # pending | passed | failed
    
    # Statistics
    total_instructions = Column(Integer, default=0)
    added_count = Column(Integer, default=0)
    modified_count = Column(Integer, default=0)
    removed_count = Column(Integer, default=0)
    
    # Approval tracking
    approved_by_user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)
    
    # Organization ownership
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    
    # Who created this build
    created_by_user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    
    # Relationships
    organization = relationship("Organization", lazy="selectin")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id], lazy="selectin")
    approved_by_user = relationship("User", foreign_keys=[approved_by_user_id], lazy="selectin")
    metadata_indexing_job = relationship("MetadataIndexingJob", foreign_keys=[metadata_indexing_job_id], lazy="selectin")
    agent_execution = relationship("AgentExecution", foreign_keys=[agent_execution_id], lazy="selectin")
    # test_run relationship removed - we query latest test run per build in service
    
    # Base build for auto-merge
    base_build = relationship("InstructionBuild", remote_side="InstructionBuild.id", 
                              foreign_keys=[base_build_id], lazy="selectin")
    
    # Build contents - the instruction versions in this build
    contents = relationship("BuildContent", back_populates="build", lazy="selectin", cascade="all, delete-orphan")
    
    # Composite index for finding the main build per org
    __table_args__ = (
        Index('ix_instruction_builds_org_is_main', 'organization_id', 'is_main'),
    )
    
    def __repr__(self):
        return f"<InstructionBuild #{self.build_number} ({self.status}) is_main={self.is_main}>"
    
    @property
    def is_draft(self) -> bool:
        """Returns True if this build is still in draft (mutable) status"""
        return self.status == 'draft'
    
    @property
    def is_approved(self) -> bool:
        """Returns True if this build has been approved"""
        return self.status == 'approved'
    
    @property
    def is_pending(self) -> bool:
        """Returns True if this build is pending approval"""
        return self.status == 'pending_approval'
    
    @property
    def is_rejected(self) -> bool:
        """Returns True if this build was rejected"""
        return self.status == 'rejected'
    
    @property
    def can_be_edited(self) -> bool:
        """Returns True if build contents can still be modified (drafts and pending_approval)"""
        return self.is_draft or self.is_pending
    
    @property
    def can_be_submitted(self) -> bool:
        """Returns True if this build can be submitted for approval"""
        return self.is_draft
    
    @property
    def can_be_approved(self) -> bool:
        """Returns True if this build can be approved"""
        return self.is_pending
    
    @property
    def can_be_promoted(self) -> bool:
        """Returns True if this build can be promoted to main"""
        return self.is_approved and not self.is_main
    

