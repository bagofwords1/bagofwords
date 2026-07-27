from typing import List, Optional
from pydantic import BaseModel, Field, model_validator


class RunEvalInput(BaseModel):
    """Input schema for ``run_eval`` tool.

    Mutually exclusive: pass either ``case_ids`` (run those specific
    cases — drafts are allowed when explicitly named) or ``suite_id``
    (run all ``status='active'`` cases in that suite).
    """

    case_ids: Optional[List[str]] = Field(
        default=None,
        description="Specific case ids to run. Mutually exclusive with suite_id.",
    )
    suite_id: Optional[str] = Field(
        default=None,
        description="Run all active cases in this suite. Mutually exclusive with case_ids.",
    )
    build_id: Optional[str] = Field(
        default=None,
        description=(
            "Instruction build to evaluate against (the immutable snapshot the "
            "eval is pinned to). Pass the specific suggestion build to measure "
            "exactly that change in isolation. When omitted, falls back to the "
            "agent's draft build in context, then to the current main build."
        ),
    )
    wait_s: int = Field(
        default=0,
        ge=0,
        le=600,
        description=(
            "How long to stay attached streaming live progress, in seconds. "
            "Default 0: kick the run off in the background and return "
            "immediately with the run_id — results arrive later via the "
            "run-finished wake-up, or on demand via get_eval_run. Set a small "
            "budget (e.g. 60-120) only when the user is waiting on a quick "
            "single-case check; if the run outlives the budget it detaches "
            "and keeps executing."
        ),
    )

    @model_validator(mode="after")
    def _exactly_one(self):
        has_cases = bool(self.case_ids and len(self.case_ids) > 0)
        has_suite = bool(self.suite_id)
        if has_cases == has_suite:
            raise ValueError("Provide exactly one of case_ids or suite_id.")
        return self


class RunEvalCaseResult(BaseModel):
    case_id: str
    case_name: Optional[str] = None
    status: str
    failure_reason: Optional[str] = None


class RunEvalOutput(BaseModel):
    success: bool
    run_id: Optional[str] = None
    status: Optional[str] = None
    total: int = 0
    passed: int = 0
    failed: int = 0
    finished: int = 0
    results: List[RunEvalCaseResult] = Field(default_factory=list)
    # True when the tool returned before the run reached a terminal status —
    # the run keeps executing server-side; a wake-up completion will arrive
    # when it finishes, or read it on demand with get_eval_run.
    detached: bool = False
    # True when an identical run (same build + case set) was already in
    # progress and was returned instead of starting a duplicate.
    deduped: bool = False
    rejected_reason: Optional[str] = None
    message: Optional[str] = None


# Progress event kinds emitted as ``ToolProgressEvent.payload.kind``.
EVAL_RUN_STARTED = "eval.run_started"
EVAL_CASE_STARTED = "eval.case_started"
EVAL_CASE_FINISHED = "eval.case_finished"
EVAL_RUN_FINISHED = "eval.run_finished"
EVAL_RUN_DETACHED = "eval.run_detached"
EVAL_HEARTBEAT = "eval.heartbeat"

EVAL_TERMINAL_STATUSES = {"pass", "fail", "error", "stopped"}
EVAL_RUN_TERMINAL_STATUSES = {"success", "error", "stopped"}
