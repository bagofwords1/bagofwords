from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class InstructionLabelBase(BaseModel):
    name: str
    color: Optional[str] = None
    description: Optional[str] = None


class InstructionLabelCreate(InstructionLabelBase):
    """Payload for creating a new instruction label."""


class InstructionLabelUpdate(BaseModel):
    """Payload for updating an existing instruction label."""

    name: Optional[str] = None
    color: Optional[str] = None
    description: Optional[str] = None


class InstructionLabelSchema(InstructionLabelBase):
    """Read model for instruction labels."""

    id: str
    organization_id: str
    created_by_user_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


