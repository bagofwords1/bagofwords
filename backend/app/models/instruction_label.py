from sqlalchemy import Column, String, ForeignKey, Table
from sqlalchemy.orm import relationship

from app.models.base import BaseSchema


# Association table for many-to-many relationship between instructions and labels
instruction_label_association = Table(
    "instruction_label_association",
    BaseSchema.metadata,
    Column("instruction_id", String(36), ForeignKey("instructions.id"), primary_key=True),
    Column("label_id", String(36), ForeignKey("instruction_labels.id"), primary_key=True),
)


class InstructionLabel(BaseSchema):
    __tablename__ = "instruction_labels"

    name = Column(String(100), nullable=False)
    color = Column(String(50), nullable=True)

    # Organization ownership and creator
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False)
    created_by_user_id = Column(String(36), ForeignKey("users.id"), nullable=True)

    # Relationships
    organization = relationship("Organization")
    created_by = relationship("User")
    instructions = relationship(
        "Instruction",
        secondary=instruction_label_association,
        back_populates="labels",
        lazy="selectin",
    )

    def __repr__(self) -> str:
        return f"<InstructionLabel {self.name} ({self.color})>"


