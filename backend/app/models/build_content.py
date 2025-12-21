from sqlalchemy import Column, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class BuildContent(BaseSchema):
    """
    Junction table linking InstructionBuild to InstructionVersion.
    Each row specifies which version of a particular instruction is included in a given build.
    This forms the complete snapshot of a build.
    """
    __tablename__ = "build_contents"
    
    # Link to the build this content belongs to
    build_id = Column(String(36), ForeignKey('instruction_builds.id', ondelete='CASCADE'), nullable=False)
    
    # Link to the instruction (for easy querying)
    instruction_id = Column(String(36), ForeignKey('instructions.id'), nullable=False)
    
    # Link to the specific version of the instruction
    instruction_version_id = Column(String(36), ForeignKey('instruction_versions.id'), nullable=False)
    
    # Relationships
    build = relationship("InstructionBuild", back_populates="contents", lazy="selectin")
    instruction = relationship("Instruction", lazy="selectin")
    instruction_version = relationship("InstructionVersion", lazy="selectin")
    
    # Ensure only one version of each instruction per build
    __table_args__ = (
        UniqueConstraint('build_id', 'instruction_id', name='uq_build_content_build_instruction'),
    )
    
    def __repr__(self):
        return f"<BuildContent build={self.build_id} instruction={self.instruction_id}>"

