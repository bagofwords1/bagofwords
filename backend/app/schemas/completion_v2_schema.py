from pydantic import BaseModel
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime

from .widget_schema import WidgetSchema
from .step_schema import StepSchema
from .tool_execution_schema import ToolExecutionSchema
from .agent_execution_schema import PlanDecisionSchema


class ToolExecutionUISchema(ToolExecutionSchema):
    """UI-focused tool execution with embedded created artifacts when available."""
    created_widget: Optional[WidgetSchema] = None
    created_step: Optional[StepSchema] = None


class ArtifactChangeSchema(BaseModel):
    """Delta describing incremental updates to a step/widget during this block (optional)."""
    type: Literal["step", "widget"]
    step_id: Optional[str] = None
    widget_id: Optional[str] = None
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
    plan_decision: Optional[PlanDecisionSchema] = None
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
    completion: Optional[Dict[str, Any]] = None

    completion_blocks: List[CompletionBlockV2Schema] = []

    # Final artifacts for quick render
    created_widgets: List[WidgetSchema] = []
    created_steps: List[StepSchema] = []

    # Small summary for UI
    summary: Dict[str, Any] = {}

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


