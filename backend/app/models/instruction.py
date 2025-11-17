from sqlalchemy import Column, String, Text, Integer, Boolean, ForeignKey, Table
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
    
    # Dual-status lifecycle management
    private_status = Column(String(50), nullable=True)  # draft, published, archived (null for global-only)
    global_status = Column(String(50), nullable=True)   # null, suggested, approved, rejected
    
    # User experience controls
    is_seen = Column(Boolean, nullable=False, default=True)         # visible in UI lists
    can_user_toggle = Column(Boolean, nullable=False, default=True) # user can enable/disable
    
    # Audit trail
    reviewed_by_user_id = Column(String(36), ForeignKey('users.id'), nullable=True)  # which admin reviewed
    
    # Legacy field - keeping for potential future use
    source_instruction_id = Column(String(36), ForeignKey('instructions.id'), nullable=True)

    # if ai generated:
    agent_execution_id = Column(String(36), ForeignKey('agent_executions.id'), nullable=True)
    trigger_reason = Column(String(255), nullable=True)
    # provenance label for AI-created instructions (e.g., 'completion', 'batch_job')
    ai_source = Column(String(50), nullable=True)
    
    # Organization ownership
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    
    # Relationships
    data_sources = relationship(
        "DataSource", 
        secondary=instruction_data_source_association, 
        back_populates="instructions",
        lazy="selectin"
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
    
    def __repr__(self):
        return f"<Instruction {self.category}:{self.text[:50]}...>"
    
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
    
    @property
    def is_private(self) -> bool:
        """Returns True if this is a private instruction"""
        return bool(self.private_status and not self.global_status)
    
    @property
    def is_suggested(self) -> bool:
        """Returns True if this is a suggested instruction"""
        return bool(self.private_status and self.global_status == "suggested")
    
    @property
    def is_global(self) -> bool:
        """Returns True if this is a global instruction"""
        return bool(not self.private_status and self.global_status == "approved")
    
    @property
    def is_editable_by_user(self) -> bool:
        """Returns True if the instruction can be edited by the user (only private)"""
        return self.is_private
    
    @property
    def can_be_suggested(self) -> bool:
        """Returns True if the instruction can be suggested (only private)"""
        return self.is_private
    
    @property
    def can_be_withdrawn(self) -> bool:
        """Returns True if the suggestion can be withdrawn by user"""
        return self.is_suggested
    
    @property
    def can_be_reviewed(self) -> bool:
        """Returns True if the instruction can be reviewed by admin"""
        return self.is_suggested
    
    @property
    def is_published(self) -> bool:
        """Returns True if the instruction is published and visible"""
        return self.status == "published"
    
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
