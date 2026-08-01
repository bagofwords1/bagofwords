from sqlalchemy import Column, String, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


class InstructionDirectory(BaseSchema):
    """A cosmetic, per-agent folder for organizing instructions in the tree.

    Directories are purely an organizational overlay for the KnowledgeExplorer
    (the /agents tree). They carry NO semantics — an instruction behaves
    identically whether it sits in a folder or at an agent's root. Nothing the
    AI sees changes; the context builder, builds, and versioning are untouched.

    Scope: a directory belongs to exactly one agent (``data_source_id``), OR to
    the "Global instructions" group when ``data_source_id`` is NULL. Because
    instructions are m:n with agents, placement lives on the (instruction,
    agent) edge — see :class:`InstructionDirectoryPlacement` — never on the
    instruction itself, so filing an instruction under a folder for agent A
    never reorganizes how it appears inside agent B.
    """

    __tablename__ = "instruction_directories"

    # Organization ownership.
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)

    # Owning agent. NULL => the Global instructions scope.
    data_source_id = Column(String(36), ForeignKey("data_sources.id"), nullable=True)

    # Self-referential parent for nesting. NULL => top-level within the scope.
    parent_id = Column(String(36), ForeignKey("instruction_directories.id"), nullable=True)

    name = Column(String(100), nullable=False)

    # Ordering among siblings (ascending).
    position = Column(Integer, nullable=False, default=0)

    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    # Relationships
    organization = relationship("Organization")
    data_source = relationship("DataSource")
    created_by = relationship("User")
    parent = relationship("InstructionDirectory", remote_side="InstructionDirectory.id", backref="children")
    placements = relationship(
        "InstructionDirectoryPlacement",
        back_populates="directory",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<InstructionDirectory {self.name} scope={self.data_source_id or 'global'}>"


class InstructionDirectoryPlacement(BaseSchema):
    """Places one instruction inside one directory (the per-agent edge).

    Invariant enforced by the service layer: at most one placement per
    (instruction, scope), where scope is the directory's ``data_source_id``.
    No placement for a given scope => the instruction renders at that scope's
    root ("uncategorized"), which is also the zero-migration default for every
    instruction that predates this feature.
    """

    __tablename__ = "instruction_directory_placements"

    instruction_id = Column(String(36), ForeignKey("instructions.id", ondelete="CASCADE"), nullable=False)
    directory_id = Column(String(36), ForeignKey("instruction_directories.id", ondelete="CASCADE"), nullable=False)

    directory = relationship("InstructionDirectory", back_populates="placements")
    instruction = relationship("Instruction")

    __table_args__ = (
        UniqueConstraint("instruction_id", "directory_id", name="uq_instruction_directory_placement"),
    )

    def __repr__(self) -> str:
        return f"<InstructionDirectoryPlacement instr={self.instruction_id} dir={self.directory_id}>"
