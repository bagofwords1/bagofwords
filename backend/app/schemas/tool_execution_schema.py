from pydantic import BaseModel
from typing import Optional, Any, Dict
from datetime import datetime
from app.schemas.base import OptionalUTCDatetime, UTCDatetime


class ToolExecutionSchema(BaseModel):
    id: str
    agent_execution_id: str
    plan_decision_id: Optional[str]
    tool_name: str
    tool_action: Optional[str]
    arguments_json: Dict[str, Any]
    status: str
    success: bool
    started_at: OptionalUTCDatetime
    completed_at: OptionalUTCDatetime
    duration_ms: Optional[float]
    attempt_number: int
    max_retries: int
    token_usage_json: Optional[Dict[str, Any]]
    result_summary: Optional[str]
    result_json: Optional[Dict[str, Any]]
    artifact_refs_json: Optional[Dict[str, Any]]
    created_widget_id: Optional[str]
    created_step_id: Optional[str]
    context_snapshot_id: Optional[str]
    error_message: Optional[str]
    created_at: UTCDatetime
    updated_at: UTCDatetime

    class Config:
        from_attributes = True
