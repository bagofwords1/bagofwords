"""Sub-agents — delegated agent runs with wave fan-in.

A *sub-agent* is an ``AgentV2`` run in mode ``subagent`` on a hidden pair of
completions of the same report, launched by the parent's ``delegate`` tool.
All ``delegate`` calls in one planner response form a *wave*; when every
sub-agent in the wave is terminal, one machine turn (``trigger_source=sub_run``)
wakes the parent with the wave's records. See ``docs/design/sub-runs.md``.

The ``sub_runs`` row is the record: the typed ``finish_subtask`` result plus
the runtime envelope. It is deliberately not an Agent List row (user-authored,
data-source-scoped schemas) and not nested under the parent's execution —
the child is its own ``AgentExecution`` with the parent ids on it.
"""
from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text

from app.models.base import BaseSchema


# ── Wave status ──────────────────────────────────────────────────────────────
WAVE_COLLECTING = "collecting"  # delegate calls of one decision being recorded
WAVE_RUNNING = "running"      # children in flight
WAVE_FIRING = "firing"        # last child finished; fan-in turn being created
WAVE_DONE = "done"            # fan-in turn created
WAVE_STOPPED = "stopped"      # user/agent stopped the wave; no fan-in
WAVE_TIMED_OUT = "timed_out"  # deadline fired; fan-in created with partial records

# ── Sub-agent status ───────────────────────────────────────────────────────────
RUN_PENDING = "pending"         # recorded, not yet launched (waits for the batch / a slot)
RUN_RUNNING = "running"
RUN_FINISHED = "finished"       # finish_subtask was called
RUN_FAILED = "failed"           # the child run errored
RUN_TIMED_OUT = "timed_out"     # stopped by the wave deadline
RUN_INTERRUPTED = "interrupted" # stopped by the user or the parent
TERMINAL_RUN_STATUSES = (RUN_FINISHED, RUN_FAILED, RUN_TIMED_OUT, RUN_INTERRUPTED)

# ── Exit reasons (envelope) ──────────────────────────────────────────────────
EXIT_FINISH_CALL = "finish_call"
EXIT_MAX_STEPS = "max_steps"
EXIT_DEADLINE = "deadline"
EXIT_STOPPED = "stopped"
EXIT_ERROR = "error"
EXIT_NO_FINISH_CALL = "no_finish_call"

# ── Result outcomes (model-authored) ─────────────────────────────────────────
OUTCOMES = ("done", "partial", "blocked", "supported", "refuted", "inconclusive")

PROFILES = ("investigate", "validate")


class SubRunWave(BaseSchema):
    __tablename__ = "sub_run_waves"

    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.id"), nullable=True)
    # The parent turn that launched the wave.
    parent_completion_id = Column(String(36), ForeignKey("completions.id"), nullable=True, index=True)
    parent_execution_id = Column(String(36), ForeignKey("agent_executions.id"), nullable=True, index=True)
    # The fan-in machine turn's visible event entry, once created.
    fan_in_completion_id = Column(String(36), ForeignKey("completions.id"), nullable=True)

    status = Column(String(24), nullable=False, default=WAVE_RUNNING, index=True)
    # Children not yet terminal. Decremented with a conditional UPDATE so the
    # child that reads zero is the one that fires the fan-in.
    remaining = Column(Integer, nullable=False, default=0)
    total = Column(Integer, nullable=False, default=0)
    deadline_at = Column(DateTime, nullable=True)
    deadline_job_id = Column(String(96), nullable=True)
    fired_at = Column(DateTime, nullable=True)
    # The parent's run mode (chat|training), so the fan-in turn runs in it.
    parent_mode = Column(String(16), nullable=True)
    # The parent turn's user prompt, carried into the fan-in trigger so the
    # synthesis answers the original ask.
    parent_prompt = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_sub_run_waves_report_status", "report_id", "status"),
    )


class SubRun(BaseSchema):
    __tablename__ = "sub_runs"

    wave_id = Column(String(36), ForeignKey("sub_run_waves.id"), nullable=False, index=True)
    organization_id = Column(String(36), ForeignKey("organizations.id"), nullable=False, index=True)
    report_id = Column(String(36), ForeignKey("reports.id"), nullable=False, index=True)
    # Position inside the wave (launch order).
    wave_index = Column(Integer, nullable=False, default=0)

    # The parent's `delegate` tool execution (write-back target).
    parent_tool_execution_id = Column(String(36), ForeignKey("tool_executions.id"), nullable=True, index=True)
    parent_completion_id = Column(String(36), ForeignKey("completions.id"), nullable=True)
    parent_execution_id = Column(String(36), ForeignKey("agent_executions.id"), nullable=True)

    # The child's hidden completions and execution.
    child_head_completion_id = Column(String(36), ForeignKey("completions.id"), nullable=True)
    child_completion_id = Column(String(36), ForeignKey("completions.id"), nullable=True, index=True)
    child_execution_id = Column(String(36), ForeignKey("agent_executions.id"), nullable=True, index=True)

    title = Column(String(200), nullable=False, default="")
    profile = Column(String(32), nullable=False, default="investigate")
    goal = Column(Text, nullable=False, default="")
    context = Column(Text, nullable=True)
    # Resolved at spawn: which agents (data source ids) the child focuses on,
    # and the model it runs on.
    agent_ids_json = Column(JSON, nullable=True)
    model_id = Column(String(36), ForeignKey("llm_models.id"), nullable=True)
    model_name = Column(String(120), nullable=True)
    max_steps = Column(Integer, nullable=True)

    status = Column(String(24), nullable=False, default=RUN_RUNNING, index=True)
    exit_reason = Column(String(24), nullable=True)
    # The model-authored finish_subtask body (validated) …
    result_json = Column(JSON, nullable=True)
    # … and the runtime envelope the model never writes.
    envelope_json = Column(JSON, nullable=True)
    report_note_id = Column(String(36), ForeignKey("notes.id"), nullable=True)
    steps_used = Column(Integer, nullable=True)
    tokens_json = Column(JSON, nullable=True)
    error_message = Column(Text, nullable=True)

    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_sub_runs_report_status", "report_id", "status"),
    )
