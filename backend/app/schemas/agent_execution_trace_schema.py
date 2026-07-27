from pydantic import BaseModel
from datetime import datetime
from typing import List, Optional, Any, Dict

from .base import OptionalUTCDatetime
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
    # Per-turn LLM usage from the quota pipeline (usage_events rows written by
    # the agent's UsageLimitContext, keyed by the head/user completion id).
    # Covers every LLM call in the run (planner + tool codegen), but is only
    # recorded on instances licensed for the usage_limits feature — None
    # elsewhere (the UI falls back to the planner-only token_usage_json).
    llm_tokens: Optional[int] = None
    llm_cost_usd: Optional[float] = None
    created_at: OptionalUTCDatetime = None

    # Rendered blocks for the chat-style left pane (same shape the report chat
    # uses). Empty for turns still in progress / without blocks.
    completion_blocks: List[CompletionBlockV2Schema] = []


class ConversationTraceResponse(BaseModel):
    """A whole report conversation as an ordered list of turns + roll-up."""
    report_id: str
    report_title: Optional[str] = None
    user_name: Optional[str] = None
    user_email: Optional[str] = None
    external_platform: Optional[str] = None  # 'slack', 'teams', 'email'; null = web UI
    total_turns: int = 0
    failed_turns: int = 0
    negative_feedback_turns: int = 0
    # LLM usage roll-up aggregated from llm_usage_records for this report.
    # Counts only the turns' own work — the planner loop plus the tool calls it
    # makes — and excludes background/observability scopes (judge grading,
    # follow-up/title generation, context compaction). Tokens include prompt +
    # completion + cache read/write; cost may be 0 when the model has no
    # pricing configured.
    total_llm_tokens: Optional[int] = None
    total_llm_cost_usd: Optional[float] = None
    turns: List[ConversationTurnSchema] = []


