from typing import Any, Dict, List, Optional, Literal
from pydantic import BaseModel


class ToolDescriptor(BaseModel):
    name: str
    description: Optional[str] = None
    research_accessible: bool = False
    schema: Optional[Dict[str, Any]] = None
    category: Optional[str] = None
    version: Optional[str] = None
    max_retries: Optional[int] = None
    timeout_seconds: Optional[int] = None
    tags: Optional[List[str]] = None
    is_active: Optional[bool] = True
    observation_policy: Optional[Literal["never", "on_trigger", "always"]] = None


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
    mode: Optional[str] = "chat" # "chat" | "deep"
    user_message: str
    instructions: Optional[str] = None
    schemas_excerpt: Optional[str] = None
    # Files context rendered from uploaded report files (schemas/metadata)
    files_context: Optional[str] = None
    history_summary: Optional[str] = None
    # Detailed conversation messages context for better planning
    messages_context: Optional[str] = None
    # Resources context from metadata resources (git repos, documentation, etc.)
    resources_context: Optional[str] = None
    # Mentions context rendered from the current user turn mentions
    mentions_context: Optional[str] = None
    # Entities context rendered from catalog search
    entities_context: Optional[str] = None
    # A compact dictionary describing the most recent tool observation (if any)
    last_observation: Optional[Dict[str, Any]] = None
    # Full list of recorded tool observations in execution order
    past_observations: Optional[List[Dict[str, Any]]] = None
    tool_catalog: Optional[List[ToolDescriptor]] = None

    # Identity
    organization_name: Optional[str] = None
    organization_ai_analyst_name: Optional[str] = None


