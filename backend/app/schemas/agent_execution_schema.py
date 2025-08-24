from pydantic import BaseModel
from typing import Optional, Any, Dict
from datetime import datetime


class AgentExecutionSchema(BaseModel):
    id: str
    completion_id: str
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    report_id: Optional[str] = None
    status: str
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    total_duration_ms: Optional[float] = None
    first_token_ms: Optional[float] = None
    thinking_ms: Optional[float] = None
    latest_seq: int
    token_usage_json: Optional[Dict[str, Any]] = None
    error_json: Optional[Dict[str, Any]] = None
    config_json: Optional[Dict[str, Any]] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ContextSnapshotSchema(BaseModel):
    id: str
    agent_execution_id: str
    kind: str
    context_view_json: Dict[str, Any]
    prompt_text: Optional[str]
    prompt_tokens: Optional[int]
    hash: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PlanDecisionSchema(BaseModel):
    id: str
    agent_execution_id: str
    seq: int
    loop_index: int
    plan_type: Optional[str]
    analysis_complete: bool
    reasoning: Optional[str]
    assistant: Optional[str]
    final_answer: Optional[str]
    action_name: Optional[str]
    action_args_json: Optional[Dict[str, Any]]
    metrics_json: Optional[Dict[str, Any]]
    context_snapshot_id: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ToolExecutionSchema(BaseModel):
    id: str
    agent_execution_id: str
    plan_decision_id: Optional[str]
    tool_name: str
    tool_action: Optional[str]
    arguments_json: Dict[str, Any]
    status: str
    success: bool
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_ms: Optional[float]
    attempt_number: int
    max_retries: int
    token_usage_json: Optional[Dict[str, Any]]
    result_summary: Optional[str]
    result_json: Optional[Dict[str, Any]]
    artifact_refs_json: Optional[Dict[str, Any]]
    created_widget_id: Optional[str]
    created_step_id: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


