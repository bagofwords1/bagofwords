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
    model_id: Optional[str] = None  # Optional model override
    mode: Optional[str] = None  # Optional mode for agent execution

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
    feedback_score: int = 0
    sigkill: Optional[datetime] = None
    parent_id: Optional[str]
    message_type: str = "ai_completion"
    role: str = "system"
    report_id: str = None
    created_at: datetime
    updated_at: datetime
    widget: Optional[WidgetSchema] = None
    main_router: str = "table"
    instructions_effectiveness: Optional[int] = None
    context_effectiveness: Optional[int] = None
    response_score: Optional[int] = None
    step_id: Optional[str] = None
    step: Optional[StepSchema] = None
    external_platform: Optional[str] = None
    external_message_id: Optional[str] = None
    external_user_id: Optional[str] = None

    class Config:
        from_attributes = True


class CompletionPlanSchema(BaseModel):
    id: str
    completion_id: str
    content: dict
    created_at: datetime
    updated_at: datetime
