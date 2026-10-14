from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.utils.reasoning_effort import normalize_effort
from app.utils.request_metadata import validate_request_metadata
from typing import Optional, List, Dict, Any, Literal
from datetime import datetime

from .widget_schema import WidgetSchema
from .step_schema import StepSchema
from .tool_execution_schema import ToolExecutionSchema
from .agent_execution_schema import PlanDecisionReducedSchema
from .visualization_schema import VisualizationSchema
from .completion_feedback_schema import CompletionFeedbackSchema
from .file_schema import FileSchema


class ToolExecutionDataSourceSchema(BaseModel):
    """Lightweight data source info for display in tool execution UI.

    Rides along with every tool execution in every view — the report page, the
    shared ``/c/{token}`` page and the report summary — which is why the data
    tools read their icon from here rather than from a page-level prop the
    shared views never passed.
    """
    id: str
    name: Optional[str] = None
    type: Optional[str] = None  # connection type e.g. 'postgres', 'bigquery'
    # Resolved display icon ("emoji:<grapheme>" | "type:<key>" | None), from
    # app.schemas.agent_icon — the same token the agents list and report payload
    # carry, so one agent draws one icon everywhere.
    icon_token: Optional[str] = None


class ToolExecutionUISchema(ToolExecutionSchema):
    """UI-focused tool execution with embedded created artifacts when available."""
    created_widget: Optional[WidgetSchema] = None
    created_step: Optional[StepSchema] = None
    created_visualizations: Optional[list[VisualizationSchema]] = None
    data_sources: Optional[list[ToolExecutionDataSourceSchema]] = None
    # Running tool waiting on (or past) a user review: pending|approved|denied|expired.
    # Lets a reload rebuild the approval card the live stream showed.
    review_state: Optional[str] = None
    confirmation: Optional[Dict[str, Any]] = None
    progress_stage: Optional[str] = None


class ArtifactChangeSchema(BaseModel):
    """Delta describing incremental updates to a step/widget during this block (optional)."""
    type: Literal["step", "widget", "visualization"]
    step_id: Optional[str] = None
    widget_id: Optional[str] = None
    visualization_id: Optional[str] = None
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

REPLACED_PROMPT_OPT_OUTS = ("follow_ups", "suggest_instructions")
# Protocol tools a run cannot finish without; inherited by sub-agents, so
# disabling one would strand every child.
UNDISABLEABLE_TOOLS = frozenset({"finish_subtask"})


class PromptDisable(BaseModel):
    """What the caller turns off for this turn. It can only switch things off:
    an org setting that is already off stays off whatever is sent here."""
    # A misspelled key would otherwise be dropped and silently do nothing.
    model_config = ConfigDict(extra="forbid")
    follow_ups: bool = False  # suggested next questions (completion.follow_ups)
    suggest_instructions: bool = False  # knowledge harness (draft instruction suggestions)
    # Built-in agent tools by name. Hidden from the planner, refused if called
    # anyway. execute_mcp / submit_list also cover their per-tool native forms.
    tools: List[str] = []

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, v):
        from app.ai.registry import builtin_tool_names

        names = list(dict.fromkeys(t.strip() for t in v if isinstance(t, str) and t.strip()))
        protected = [t for t in names if t in UNDISABLEABLE_TOOLS]
        if protected:
            raise ValueError(f"cannot disable internal tool(s): {', '.join(protected)}")
        known = builtin_tool_names()
        unknown = [t for t in names if t not in known]
        if unknown:
            raise ValueError(
                f"unknown tool(s) in disable.tools: {', '.join(unknown)}. "
                f"Valid names: {', '.join(sorted(known - UNDISABLEABLE_TOOLS))}"
            )
        return names


def request_disable(prompt) -> dict:
    """prompt.disable of a stored completion prompt (a dict), or {}."""
    disable = prompt.get("disable") if isinstance(prompt, dict) else None
    return disable if isinstance(disable, dict) else {}


def request_disabled_tools(prompt) -> frozenset:
    tools = request_disable(prompt).get("tools")
    return frozenset(t for t in tools if isinstance(t, str)) if isinstance(tools, list) else frozenset()


def mcp_tools_hidden(prompt) -> bool:
    """disable.tools includes execute_mcp: the MCP gateway and the native
    mcp__ tools it routes are off, so schemas must not advertise MCP tools."""
    return "execute_mcp" in request_disabled_tools(prompt)


def inherited_disable(prompt) -> Optional[dict]:
    """prompt.disable to carry into a turn that continues this request (a
    sub-agent, its fan-in, a wait wake, a steer that became a new turn).
    Tools no longer registered are dropped instead of failing validation, so a
    deploy that renames a tool cannot break a continuation."""
    disable = request_disable(prompt)
    if not disable:
        return None
    from app.ai.registry import builtin_tool_names

    known = builtin_tool_names()
    tools = [t for t in disable.get("tools") or [] if isinstance(t, str) and t in known and t not in UNDISABLEABLE_TOOLS]
    return {**disable, "tools": tools}


class PromptSchema(BaseModel):
    content: str = ""
    widget_id: Optional[str] = None
    step_id: Optional[str] = None
    mentions: Optional[List[dict]] = None
    mode: Optional[str] = 'chat'
    model_id: Optional[str] = None
    platform: Optional[str] = None  # 'excel', 'slack', 'teams', etc. None = web
    platform_context: Optional[Dict[str, Any]] = None  # Platform-specific context (e.g. Excel selection data)
    # Per-completion reasoning effort (the model picker's level). Resolution:
    #   per-completion > report level > trigger words > LLMModel.config default > off
    # None = Default. Each provider client clamps it to what the model accepts.
    reasoning_effort: Optional[str] = None  # off|minimal|low|medium|high|xhigh|max
    # Free-form values the calling app attaches to this question (e.g. its chat
    # id). Stored with the completion; merged into MCP tool-call metadata on
    # connections with Dynamic params on. Never shown to the LLM. See
    # app.utils.request_metadata.
    metadata: Optional[Dict[str, Any]] = None
    # Set only on the hidden head completion of a sub-agent (mode='subagent'):
    # the sub_agents row this child run reports to. See app.models.sub_agent.
    sub_agent_id: Optional[str] = None
    # Per-request opt-outs (see PromptDisable). Stored with the completion so
    # queued and re-run turns keep them; sub-agents and their fan-in inherit it.
    disable: Optional[PromptDisable] = None

    @model_validator(mode="before")
    @classmethod
    def _reject_replaced_opt_outs(cls, data):
        # 0.0.588 shipped these at the top level of prompt; they moved under
        # prompt.disable. Fail loudly rather than silently ignore them. A null
        # means "not set" (clients that serialize every field send it), so only
        # a real value is rejected.
        if isinstance(data, dict):
            for key in REPLACED_PROMPT_OPT_OUTS:
                if data.get(key) is not None:
                    raise ValueError(f"prompt.{key} was replaced by prompt.disable.{key} (set it to true)")
        return data

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _validate_reasoning_effort(cls, v):
        return normalize_effort(v)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, v):
        return validate_request_metadata(v)

    class Config:
        from_attributes = True

class CompletionBase(BaseModel):
    prompt: Optional[PromptSchema]

class CompletionCreate(CompletionBase):
    stream: Optional[bool] = False
    # When true and a completion is already running on the report, the prompt
    # is persisted as a role='user' status='queued' row instead of starting a
    # second concurrent agent run. The queue dispatcher starts it when the
    # running turn finishes successfully.
    queue: Optional[bool] = False


class CompactionStateSchema(BaseModel):
    tokens_compacted_total: int = 0
    covered_turns: int = 0
    last_compaction_at: Optional[str] = None
    can_compact: bool = False
    # Fold boundary: the transcript renders the compaction divider directly
    # after this completion (state-derived — there are no divider rows).
    covers_until_completion_id: Optional[str] = None


class CompletionContextEstimateSchema(BaseModel):
    model_id: str
    model_name: Optional[str] = None
    prompt_tokens: int
    model_limit: Optional[int] = None
    remaining_tokens: Optional[int] = None
    near_limit: bool = False
    context_usage_pct: Optional[float] = None
    compaction: Optional[CompactionStateSchema] = None


class CompletionBlockV2Schema(BaseModel):
    id: str
    completion_id: str
    agent_execution_id: Optional[str]

    # Ordering
    seq: Optional[int] = None
    block_index: int
    loop_index: Optional[int]

    # Phase tag (e.g. 'knowledge_harness'); None for regular main-loop blocks
    phase: Optional[str] = None

    # Render fields
    title: str
    status: str  # in_progress | completed | error | planning
    icon: Optional[str]
    content: Optional[str]
    reasoning: Optional[str]

    # Source objects
    plan_decision: Optional[PlanDecisionReducedSchema] = None
    tool_execution: Optional[ToolExecutionUISchema] = None

    # Optional artifact deltas for progressive UIs
    artifact_changes: Optional[List[ArtifactChangeSchema]] = None

    # Timing
    started_at: Optional[datetime]
    completed_at: Optional[datetime]
    duration_ms: Optional[float] = None
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
    # 'steering' for user messages injected into a running completion
    message_type: Optional[str] = None

    agent_execution_id: Optional[str] = None
    # Wall-clock time of the agent execution behind this completion, stamped when
    # the run finishes (AgentExecution.total_duration_ms). None while in progress.
    total_duration_ms: Optional[float] = None

    prompt: Optional[Dict[str, Any]] = None

    completion_blocks: List[CompletionBlockV2Schema] = []

    # Final artifacts for quick render
    created_widgets: List[WidgetSchema] = []
    created_steps: List[StepSchema] = []
    created_visualizations: List[VisualizationSchema] = []

    # Files attached to this completion (images, etc.)
    files: List[FileSchema] = []

    # Small summary for UI
    summary: Dict[str, Any] = {}

    # Suggested instructions produced during this agent execution (optional, outside blocks)
    instruction_suggestions: Optional[List[Dict[str, Any]]] = None

    # Suggested follow-up questions (web sessions only). Persisted on the
    # completion; also delivered live via the `completion.follow_ups` SSE event.
    follow_ups: Optional[List[str]] = None

    # Instructions loaded into context during this completion (for UI indicator)
    loaded_instructions: Optional[List[Dict[str, Any]]] = None

    # Knowledge-harness build associated with this completion (if any).
    # Shape: { id, build_number, status, is_main } — authoritative build state
    # so KnowledgeGroup can render publish state without local caches.
    knowledge_harness_build: Optional[Dict[str, Any]] = None

    # Feedback - pre-loaded to avoid N+1 API calls
    feedback_score: int = 0  # Legacy aggregate score from Completion model
    user_feedback: Optional[CompletionFeedbackSchema] = None  # Current user's feedback if any

    # Control & timing
    sigkill: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    # Scheduled prompt
    scheduled_prompt_id: Optional[str] = None

    # Webhook provenance (for compact event-entry rendering + source badge)
    webhook_id: Optional[str] = None
    external_platform: Optional[str] = None
    # Machine-turn provenance ('eval_run', 'wait', ...) for event entries.
    trigger_source: Optional[str] = None

    # Marker rows (context_compaction, error, …) need the type for special rendering
    message_type: Optional[str] = None

    # Fork summary fields
    is_fork_summary: Optional[str] = None
    source_report_id: Optional[str] = None
    fork_asset_refs: Optional[List[Dict[str, Any]]] = None
    completion: Optional[Dict[str, Any]] = None  # raw completion content for fork summary rendering

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
    # Cursor pagination. next_before is an opaque cursor ("<ISO8601>~<completion id>")
    # the client echoes back verbatim as the `before` query param.
    has_more: bool = False
    next_before: Optional[str] = None
    # Rolling-compaction state so the transcript can place the divider on load
    compaction: Optional[CompactionStateSchema] = None


