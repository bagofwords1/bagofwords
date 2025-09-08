from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from app.schemas.view_schema import ViewSchema

class StepBase(BaseModel):
    title: str
    slug: str
    status: str
    status_reason: Optional[str] = None
    prompt: str
    code: str
    description: Optional[str] = ""
    

class StepSchema(StepBase):
    id: str
    created_at: datetime
    type: str
    data: dict = Field(default_factory=dict)
    data_model: dict = Field(default_factory=dict)
    view: ViewSchema = Field(default_factory=ViewSchema)

    class Config:
        from_attributes = True

class StepCreate(StepBase):
    widget_id: str
    data: dict = Field(default_factory=dict)
    data_model: dict = Field(default_factory=dict)
    view: ViewSchema = Field(default_factory=ViewSchema)

class StepUpdate(StepBase):
    pass

