from sqlalchemy import Column, String, Text, Integer, ForeignKey, DateTime, JSON, Index
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.models.base import BaseSchema


class InstructionVersion(BaseSchema):
    """
    Immutable snapshot of a single instruction's content at a point in time.
    All fields that affect AI agent quality are captured here.
    Relationships are denormalized as JSON for immutable snapshots.
    """
    __tablename__ = "instruction_versions"
    
    # Link to the parent instruction
    instruction_id = Column(String(36), ForeignKey('instructions.id', ondelete='CASCADE'), nullable=False)
    
    # Per-instruction version number: 1, 2, 3...
    version_number = Column(Integer, nullable=False)
    
    # === Content fields ===
    text = Column(Text, nullable=False)
    title = Column(String(255), nullable=True)
    structured_data = Column(JSON, nullable=True)
    formatted_content = Column(Text, nullable=True)
    
    # Instruction status at time of version (draft/published/archived)
    status = Column(String(50), nullable=True, default='published')
    
    # Loading behavior for AI context
    load_mode = Column(String(20), default='always')  # 'always' | 'intelligent' | 'disabled'
    
    # === Relationships as JSON (denormalized for immutable snapshots) ===
    # Format: [{"object_type": "...", "object_id": "...", "column_name": "...", "display_text": "..."}]
    references_json = Column(JSON, nullable=True)
    
    # List of data source IDs: ["ds_1", "ds_2"]
    data_source_ids = Column(JSON, nullable=True)
    
    # List of label IDs: ["label_1", "label_2"]
    label_ids = Column(JSON, nullable=True)
    
    # List of category IDs: ["cat_1", "cat_2"]
    category_ids = Column(JSON, nullable=True)
    
    # SHA-256 hash of content for fast change detection
    content_hash = Column(String(64), nullable=False)
    
    # === Audit fields ===
    created_by_user_id = Column(String(36), ForeignKey('users.id'), nullable=True)
    
    # Relationships
    instruction = relationship("Instruction", back_populates="versions", lazy="selectin")
    created_by_user = relationship("User", foreign_keys=[created_by_user_id], lazy="selectin")
    
    # Composite index for efficient version lookups
    __table_args__ = (
        Index('ix_instruction_versions_instruction_version', 'instruction_id', 'version_number'),
    )
    
    def __repr__(self):
        return f"<InstructionVersion {self.instruction_id} v{self.version_number}>"
    
    @property
    def display_title(self) -> str:
        """Returns display title, falling back to text snippet"""
        if self.title:
            return self.title
        return self.text[:100] + "..." if len(self.text) > 100 else self.text

