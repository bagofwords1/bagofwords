from sqlalchemy import Column, String, ForeignKey
from sqlalchemy.orm import relationship
from app.models.base import BaseSchema
from typing import Optional, Union


class InstructionReference(BaseSchema):
    __tablename__ = "instruction_references"

    instruction_id = Column(String(36), ForeignKey("instructions.id"), nullable=False)
    object_type = Column(String(50), nullable=False)  # e.g., metadata_resource | datasource_table | memory
    object_id = Column(String(36), nullable=False)
    column_name = Column(String(255), nullable=True)  # optional column within the resource
    relation_type = Column(String(50), nullable=True)  # e.g., scope | mention (optional)
    display_text = Column(String(255), nullable=True)

    instruction = relationship("Instruction", back_populates="references")
    
    # Dynamic relationships to referenced objects
    @property
    def referenced_object(self) -> Optional[Union["MetadataResource", "DataSourceTable", "Memory"]]:
        """Get the actual referenced object based on object_type and object_id."""
        if self.object_type == "metadata_resource":
            return self.metadata_resource
        elif self.object_type == "datasource_table":
            return self.datasource_table
        elif self.object_type == "memory":
            return self.memory
        return None

