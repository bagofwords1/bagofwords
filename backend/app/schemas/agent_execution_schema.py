from pydantic import BaseModel
from typing import Optional, Any, Dict
from datetime import datetime
from app.schemas.base import OptionalUTCDatetime, UTCDatetime


class AgentExecutionSchema(BaseModel):
    id: str
    completion_id: str
    user_completion_id: Optional[str] = None
    organization_id: Optional[str] = None
    user_id: Optional[str] = None
    report_id: Optional[str] = None
    status: str
    started_at: OptionalUTCDatetime = None
    completed_at: OptionalUTCDatetime = None
    total_duration_ms: Optional[float] = None
    first_token_ms: Optional[float] = None
    thinking_ms: Optional[float] = None
    latest_seq: int
    token_usage_json: Optional[Dict[str, Any]] = None
    error_json: Optional[Dict[str, Any]] = None
    config_json: Optional[Dict[str, Any]] = None
    bow_version: Optional[str] = None
    build_id: Optional[str] = None
    # AI scoring fields (denormalized from Completion for UI convenience)
    instructions_effectiveness: Optional[int] = None
    context_effectiveness: Optional[int] = None
    response_score: Optional[int] = None
    created_at: UTCDatetime
    updated_at: UTCDatetime

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
    created_at: UTCDatetime
    updated_at: UTCDatetime

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
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True


class PlanDecisionReducedSchema(BaseModel):
    id: str
    agent_execution_id: str
    seq: int
    loop_index: int
    plan_type: Optional[str]
    analysis_complete: bool
    action_name: Optional[str]
    action_args_json: Optional[Dict[str, Any]]
    metrics_json: Optional[Dict[str, Any]]
    context_snapshot_id: Optional[str]
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True

