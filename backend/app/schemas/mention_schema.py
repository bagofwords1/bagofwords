from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.models.mention import MentionType

class MentionBase(BaseModel):
    type: MentionType
    object_id: str
    mention_content: str

class MentionCreate(MentionBase):
    report_id: str
    completion_id: str

class MentionUpdate(BaseModel):
    type: Optional[MentionType] = None
    object_id: Optional[str] = None
    mention_content: Optional[str] = None

class MentionResponse(MentionBase):
    id: str
    report_id: str
    completion_id: str
    created_at: datetime

    class Config:
        from_attributes = True
   