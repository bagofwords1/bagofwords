from pydantic import BaseModel
from typing import List, Optional, Any, Dict

from .agent_execution_schema import AgentExecutionSchema, ContextSnapshotSchema
from .completion_v2_schema import CompletionBlockV2Schema
from .completion_feedback_schema import CompletionFeedbackSchema
from .build_schema import InstructionBuildSchema


class IterationTimingSchema(BaseModel):
    loop_index: Optional[int] = None
    block_index: Optional[int] = None
    llm_ms: Optional[float] = None
    tool_name: Optional[str] = None
    tool_ms: Optional[float] = None
    sub_timings: Optional[Dict[str, Any]] = None


class TimingBreakdownSchema(BaseModel):
    setup_ms: Optional[float] = None
    total_duration_ms: Optional[float] = None
    total_tool_ms: Optional[float] = None
    total_llm_ms: Optional[float] = None
    total_db_ms: Optional[float] = None
    iterations: List[IterationTimingSchema] = []


class AgentExecutionTraceResponse(BaseModel):
    agent_execution: AgentExecutionSchema
    completion_blocks: List[CompletionBlockV2Schema]
    head_prompt_snippet: Optional[str] = None
    head_context_snapshot: Optional[ContextSnapshotSchema] = None
    latest_feedback: Optional[CompletionFeedbackSchema] = None
    build: Optional[InstructionBuildSchema] = None
    timing_breakdown: Optional[TimingBreakdownSchema] = None


