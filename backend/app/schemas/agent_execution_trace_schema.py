from pydantic import BaseModel
from datetime import datetime
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


class ConversationTurnSchema(BaseModel):
    """One user→assistant turn in a report conversation, with diagnosis badges.

    Used by the conversation-first trace modal: the left rail renders these
    turns in order; selecting one lazy-loads the full per-turn trace via the
    existing agent_execution trace endpoint (keyed by ``agent_execution_id``).
    """
    # User side
    user_completion_id: Optional[str] = None
    user_prompt: Optional[str] = None
    role: str = "user"  # 'user' | 'external'

    # Assistant side
    completion_id: Optional[str] = None  # system completion
    agent_execution_id: Optional[str] = None
    assistant_content: Optional[str] = None
    status: str = "success"  # success | error | in_progress

    # Badges
    total_tools: int = 0
    total_failed_tools: int = 0
    total_successful_tools: int = 0
    tool_names: List[str] = []
    step_titles: List[str] = []
    feedback_status: str = "none"  # positive | negative | none
    feedback_direction: int = 0
    feedback_message: Optional[str] = None
    instructions_effectiveness: Optional[int] = None
    context_effectiveness: Optional[int] = None
    response_score: Optional[int] = None
    # LLM judge per-dimension score + reasoning (preferred over the scalar
    # scores above when present). None when the judge didn't run.
    judge: Optional[Dict[str, Any]] = None
    total_duration_ms: Optional[float] = None
    created_at: Optional[datetime] = None

    # Rendered blocks for the chat-style left pane (same shape the report chat
    # uses). Empty for turns still in progress / without blocks.
    completion_blocks: List[CompletionBlockV2Schema] = []


class ConversationTraceResponse(BaseModel):
    """A whole report conversation as an ordered list of turns + roll-up."""
    report_id: str
    report_title: Optional[str] = None
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    total_turns: int = 0
    failed_turns: int = 0
    negative_feedback_turns: int = 0
    turns: List[ConversationTurnSchema] = []


