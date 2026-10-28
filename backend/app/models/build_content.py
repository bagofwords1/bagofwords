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

    # The version this build's change to `instruction_id` was written AGAINST —
    # what the instruction looked like on main the moment this build first
    # edited it. Stamped once, on the write that turns this row into a change,
    # and never updated again (later edits in the same build accumulate on top,
    # so the baseline of the whole pending change stays the first one).
    #
    # This exists because InstructionBuild.base_build_id — the build's fork
    # point — is the WRONG baseline for a diff. It is stamped once at build
    # creation and never rebased, so every promotion of main while this build
    # is open (accepting any suggestion, a direct user edit, a git sync) moves
    # main out from under it. Diffing base_build -> proposed then attributes
    # everything main gained in the meantime to this suggestion, which renders
    # the instruction's own current text as green additions and, on accept,
    # appends a duplicate copy of it.
    #
    # NULL means either a carry-over row (no change to diff) or a row written
    # before this column existed; readers fall back to the base-build lookup,
    # which is exactly the old behavior.
    #
    # Deliberately not a ForeignKey: it is a soft pointer read through that
    # fallback, so a dangling value degrades to the old behavior rather than
    # breaking a read — and SQLite cannot add a real FK without a table rebuild.
    base_version_id = Column(String(36), nullable=True, index=True)

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

