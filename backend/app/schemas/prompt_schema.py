from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from uuid import UUID

class PromptBase(BaseModel):
    text: str

class PromptCreate(PromptBase):
    pass

class PromptUpdate(PromptBase):
    title: Optional[str] = None  # Add this line
    text: Optional[str] = None

class PromptResponse(PromptBase):
    id: UUID
    created_at: datetime

    class Config:
        orm_mode = True