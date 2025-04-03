from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from .widget_schema import WidgetSchema
from .step_schema import StepSchema

class PromptSchema(BaseModel):
    content: str = ""  # Default to an empty string
    reasoning: Optional[str] = None  # Default to None
    widget_id: Optional[str] = None  # Default to None
    step_id: Optional[str] = None  # Default to None
    mentions: Optional[List[dict]] = None  # Default to None

    class Config:
        from_attributes = True

class CompletionBase(BaseModel):
    prompt: Optional[PromptSchema]

class CompletionCreate(CompletionBase):
    pass

class CompletionSchema(CompletionBase):
    id: str
    completion: Optional[PromptSchema] = None  # Default to None
    model: str = "gpt4o"
    status: str = "success"
    turn_index: int = 0
    parent_id: Optional[str]
    message_type: str = "ai_completion"
    role: str = "system"
    report_id: str = None
    created_at: datetime
    updated_at: datetime
    widget: Optional[WidgetSchema] = None
    main_router: str = "table"
    step_id: Optional[str] = None
    step: Optional[StepSchema] = None

    class Config:
        from_attributes = True


class CompletionPlanSchema(BaseModel):
    id: str
    completion_id: str
    content: dict
    created_at: datetime
    updated_at: datetime
