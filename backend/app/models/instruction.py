from sqlalchemy import Column, String, Text, Integer, Boolean, ForeignKey, Table
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema

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
    
    # Status management
    status = Column(String(50), nullable=False, default="draft")
    
    # Categorization
    category = Column(String(50), nullable=False, default="general")
    
    # User who created the instruction
    user_id = Column(String(36), ForeignKey('users.id'), nullable=False)
    
    # Organization ownership
    organization_id = Column(String(36), ForeignKey('organizations.id'), nullable=False)
    
    # Relationships
    data_sources = relationship(
        "DataSource", 
        secondary=instruction_data_source_association, 
        back_populates="instructions",
        lazy="selectin"
    )
    user = relationship("User", lazy="selectin")
    organization = relationship("Organization")
    
    def __repr__(self):
        return f"<Instruction {self.category}:{self.text[:50]}...>"
    
    @property
    def is_global(self) -> bool:
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
