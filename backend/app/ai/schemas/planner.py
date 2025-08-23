from typing import Optional, Dict, Any, List, Literal
from pydantic import BaseModel


class PlannerInput(BaseModel):
    user_message: str
    schemas_excerpt: str = ""
    history_summary: str = ""
    last_observation: Optional[Dict[str, Any]] = None
    external_platform: Optional[str] = None
    tool_catalog: Optional[List["ToolDescriptor"]] = None


class PlannerToolCall(BaseModel):
    type: Literal["tool_call"]
    name: str
    arguments: Dict[str, Any]


class ToolDescriptor(BaseModel):
    name: str
    description: str
    schema: Optional[Dict[str, Any]] = None


class PlannerDecision(BaseModel):
    analysis_complete: bool
    reasoning_message: Optional[str] = None
    assistant_message: Optional[str] = None
    action: Optional[PlannerToolCall] = None
    final_answer: Optional[str] = None
    metrics: Optional[Dict[str, Any]] = None  # { first_token_ms, thinking_ms, token_usage }
    error: Optional[Dict[str, Any]] = None
    streaming_complete: bool = False

