from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel


class ToolDescriptor(BaseModel):
    name: str
    description: Optional[str] = None
    research_accessible: bool = False


class TokenUsage(BaseModel):
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class PlannerMetrics(BaseModel):
    first_token_ms: Optional[float] = None
    thinking_ms: Optional[float] = None
    token_usage: Optional[TokenUsage] = None


class PlannerError(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None


class Action(BaseModel):
    type: Literal["tool_call"]
    name: str
    arguments: Dict[str, Any]


class PlannerDecision(BaseModel):
    analysis_complete: bool
    plan_type: Optional[Literal["research", "action"]] = None
    reasoning_message: Optional[str] = None
    assistant_message: Optional[str] = None
    action: Optional[Action] = None
    final_answer: Optional[str] = None
    streaming_complete: bool = False
    metrics: Optional[PlannerMetrics] = None
    error: Optional[PlannerError] = None


class PlannerInput(BaseModel):
    external_platform: Optional[str] = None
    user_message: str
    schemas_excerpt: Optional[str] = None
    history_summary: Optional[str] = None
    last_observation: Optional[Dict[str, Any]] = None
    tool_catalog: Optional[List[ToolDescriptor]] = None


