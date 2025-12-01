from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime

from .widget_schema import WidgetSchema
from .step_schema import StepSchema
from .tool_execution_schema import ToolExecutionSchema
from .agent_execution_schema import PlanDecisionReducedSchema
from .visualization_schema import VisualizationSchema
from .completion_feedback_schema import CompletionFeedbackSchema


class ToolExecutionUISchema(ToolExecutionSchema):
    """UI-focused tool execution with embedded created artifacts when available."""
    created_widget: Optional[WidgetSchema] = None
    created_step: Optional[StepSchema] = None
    created_visualizations: Optional[list[VisualizationSchema]] = None


class ArtifactChangeSchema(BaseModel):
    """Delta describing incremental updates to a step/widget during this block (optional)."""
    type: Literal["step", "widget", "visualization"]
    step_id: Optional[str] = None
    widget_id: Optional[str] = None
    visualization_id: Optional[str] = None
    revision: Optional[int] = None
    partial: Optional[bool] = True
    changed_fields: List[str] = []
    fields: Dict[str, Any] = {}


class BlockTextDeltaSchema(BaseModel):
    """Tiny text delta for progressive token/char streaming on a block field."""
    block_id: str
    field: Literal["reasoning", "content"]
    text: str
    token_index: Optional[int] = None
    is_final_chunk: Optional[bool] = None

class PromptSchema(BaseModel):
    content: str = ""
    widget_id: Optional[str] = None
    step_id: Optional[str] = None
    mentions: Optional[List[dict]] = None
    mode: Optional[str] = 'chat'
    model_id: Optional[str] = None

    class Config:
        from_attributes = True

class CompletionBase(BaseModel):
    prompt: Optional[PromptSchema]

class CompletionCreate(CompletionBase):
    stream: Optional[bool] = False


class CompletionContextEstimateSchema(BaseModel):
    model_id: str
    model_name: Optional[str] = None
    prompt_tokens: int
    model_limit: Optional[int] = None
    remaining_tokens: Optional[int] = None
    near_limit: bool = False
    context_usage_pct: Optional[float] = None


class CompletionBlockV2Schema(BaseModel):
    id: str
    completion_id: str
    agent_execution_id: Optional[str]

    # Ordering
    seq: Optional[int] = None
    block_index: int
    loop_index: Optional[int]

    # Render fields
    title: str
    status: str  # in_progress | completed | error | planning
    icon: Optional[str]
    content: Optional[str]
    reasoning: Optional[str]

    # Source objects
    plan_decision: Optional[PlanDecisionReducedSchema] = None
    tool_execution: Optional[ToolExecutionUISchema] = None

    # Optional artifact deltas for progressive UIs
    artifact_changes: Optional[List[ArtifactChangeSchema]] = None

    # Timing
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CompletionV2Schema(BaseModel):
    id: str
    role: str
    status: str
    model: str
    turn_index: int
    parent_id: Optional[str]
    report_id: str

    agent_execution_id: Optional[str] = None

    prompt: Optional[Dict[str, Any]] = None

    completion_blocks: List[CompletionBlockV2Schema] = []

    # Final artifacts for quick render
    created_widgets: List[WidgetSchema] = []
    created_steps: List[StepSchema] = []
    created_visualizations: List[VisualizationSchema] = []

    # Small summary for UI
    summary: Dict[str, Any] = {}

    # Suggested instructions produced during this agent execution (optional, outside blocks)
    instruction_suggestions: Optional[List[Dict[str, Any]]] = None

    # Feedback - pre-loaded to avoid N+1 API calls
    feedback_score: int = 0  # Legacy aggregate score from Completion model
    user_feedback: Optional[CompletionFeedbackSchema] = None  # Current user's feedback if any

    # Control & timing
    sigkill: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CompletionsV2Response(BaseModel):
    report_id: str
    completions: List[CompletionV2Schema]
    total_completions: int
    total_blocks: int
    total_widgets_created: int
    total_steps_created: int
    earliest_completion: Optional[datetime] = None
    latest_completion: Optional[datetime] = None
    # Cursor pagination
    has_more: bool = False
    next_before: Optional[datetime] = None


