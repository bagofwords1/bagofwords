from pydantic import BaseModel
from typing import Optional
from datetime import datetime
from app.schemas.user_schema import UserSchema
class MemoryBase(BaseModel):
    title: str
    description: Optional[str] = None
    is_public: bool = False

class MemoryCreate(MemoryBase):
    step_id: Optional[str] = None
    report_id: Optional[str] = None
    widget_id: Optional[str] = None


class MemoryUpdate(BaseModel):
    id: str
    title: Optional[str] = None
    description: Optional[str] = None
    is_public: Optional[bool] = None

class MemorySchema(MemoryBase):
    id: str
    user: UserSchema
    organization_id: Optional[str]
    step_id: Optional[str]
    report_id: Optional[str]
    created_at: datetime
    updated_at: datetime
    widget_id: Optional[str]

    class Config:
        from_attributes = True
