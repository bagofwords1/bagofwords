from sqlalchemy import Boolean, Column, String, ForeignKey, UniqueConstraint, Index, false
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

    # True when this row is an actual CHANGE relative to the build's base build —
    # i.e. the base build holds a different version of this instruction, doesn't
    # hold it at all, or the build has no base. False marks a carry-over row
    # inherited unchanged from the base.
    #
    # A build snapshots EVERY instruction (see BuildService._copy_build_contents),
    # so the overwhelming majority of rows are carry-over. Recording that at write
    # time is what keeps the pending-review sweep
    # (InstructionService.get_pending_change_instruction_ids) reading the handful
    # of real changes instead of anti-joining the org's whole snapshot corpus —
    # which grew as (open draft builds × instructions) and was the dominant cost
    # of the /agents tree's instruction badges.
    is_change = Column(Boolean, nullable=False, default=False, server_default=false())

    # Relationships
    build = relationship("InstructionBuild", back_populates="contents", lazy="raise")
    instruction = relationship("Instruction", lazy="raise")
    instruction_version = relationship("InstructionVersion", lazy="raise")
    
    # Ensure only one version of each instruction per build
    __table_args__ = (
        UniqueConstraint('build_id', 'instruction_id', name='uq_build_content_build_instruction'),
        # FK columns are not auto-indexed by SQLite/Postgres. The pending sweep
        # (get_pending_change_instruction_ids) filters instruction_id directly and
        # joins on build_id; without these it seq-scans this (large) table.
        Index('ix_build_contents_instruction_id', 'instruction_id'),
        Index('ix_build_contents_build_id', 'build_id'),
        Index('ix_build_contents_instruction_version_id', 'instruction_version_id'),
        # The pending sweep selects on is_change first and then joins the build,
        # so lead with the flag: the tiny "real change" slice is reachable
        # without touching the carry-over rows.
        Index('ix_build_contents_is_change_build', 'is_change', 'build_id'),
    )
    
    def __repr__(self):
        return f"<BuildContent build={self.build_id} instruction={self.instruction_id}>"

