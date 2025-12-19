from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
from .widget_schema import WidgetSchema
from .step_schema import StepSchema
from app.schemas.base import OptionalUTCDatetime, UTCDatetime


class ToolExecutionMinifiedSchema(BaseModel):
    """User-friendly version of ToolExecution for UI rendering"""
    id: str
    tool_name: str
    tool_action: Optional[str]
    status: str  # in_progress | success | error
    success: bool
    result_summary: Optional[str]
    duration_ms: Optional[float]
    
    # Artifacts (the key part for UI)
    created_widget: Optional[WidgetSchema] = None
    created_step: Optional[StepSchema] = None
    
    # Timing
    started_at: OptionalUTCDatetime
    completed_at: OptionalUTCDatetime
    
    class Config:
        from_attributes = True


class CompletionBlockSchema(BaseModel):
    id: str
    completion_id: str
    agent_execution_id: Optional[str]
    
    # Decision-Tool pair
    plan_decision_id: str
    tool_execution: Optional[ToolExecutionMinifiedSchema] = None
    
    # Ordering
    block_index: int
    loop_index: Optional[int]
    
    # Render fields (from decision)
    title: str
    status: str  # in_progress | completed | error
    icon: Optional[str]
    content: Optional[str]  # plan_decision.assistant
    reasoning: Optional[str]  # plan_decision.reasoning
    
    # Timing
    started_at: OptionalUTCDatetime
    completed_at: OptionalUTCDatetime
    created_at: UTCDatetime
    updated_at: UTCDatetime
    
    class Config:
        from_attributes = True


class CompletionTimelineSchema(BaseModel):
    """Timeline response combining completion with blocks"""
    completion_id: str
    agent_execution_id: Optional[str]
    blocks: List[CompletionBlockSchema]
    summary: dict  # {"total_blocks": 4, "widgets_created": 1, "steps_created": 2}
    
    class Config:
        from_attributes = True
