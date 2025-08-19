from pydantic import BaseModel
from typing import Optional, Dict, Any
from datetime import datetime


class InstructionReferenceBase(BaseModel):
    object_type: str  # metadata_resource | datasource_table | memory
    object_id: str
    column_name: Optional[str] = None
    relation_type: Optional[str] = None  # scope | mention
    display_text: Optional[str] = None


class InstructionReferenceCreate(InstructionReferenceBase):
    pass


class InstructionReferenceUpdate(BaseModel):
    object_type: Optional[str] = None
    object_id: Optional[str] = None
    column_name: Optional[str] = None
    relation_type: Optional[str] = None
    display_text: Optional[str] = None


class InstructionReferenceSchema(InstructionReferenceBase):
    id: str
    instruction_id: str
    created_at: datetime
    updated_at: datetime
    
    # Include the referenced object as a single field
    object: Optional[Dict[str, Any]] = None

    class Config:
        from_attributes = True

