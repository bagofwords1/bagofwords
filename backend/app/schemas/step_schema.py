from pydantic import BaseModel, Field, model_validator
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
    query_id: str
    data: dict = Field(default_factory=dict)
    data_model: dict = Field(default_factory=dict)
    view: Optional[ViewSchema] = Field(default_factory=ViewSchema)

    class Config:
        from_attributes = True

    @model_validator(mode="after")
    def _ensure_view(self) -> "StepSchema":
        if self.view is None:
            self.view = ViewSchema()
        return self

class StepCreate(StepBase):
    widget_id: str
    data: dict = Field(default_factory=dict)
    data_model: dict = Field(default_factory=dict)
    view: Optional[ViewSchema] = Field(default_factory=ViewSchema)

    @model_validator(mode="after")
    def _ensure_view(self) -> "StepCreate":
        if self.view is None:
            self.view = ViewSchema()
        return self

class StepUpdate(StepBase):
    pass

