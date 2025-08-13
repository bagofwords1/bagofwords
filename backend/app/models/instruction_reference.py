from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema


class InstructionReference(BaseSchema):
    __tablename__ = "instruction_references"

    instruction_id = Column(String(36), ForeignKey("instructions.id"), nullable=False)
    object_type = Column(String(50), nullable=False)  # e.g., metadata_resource | datasource_table | memory
    object_id = Column(String(36), nullable=False)
    column_name = Column(String(255), nullable=True)  # optional column within the resource
    relation_type = Column(String(50), nullable=True)  # e.g., scope | mention (optional)
    display_text = Column(String(255), nullable=True)

    instruction = relationship("Instruction", back_populates="references")

