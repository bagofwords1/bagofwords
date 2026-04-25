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
    rejected_reason: Optional[str] = None
    message: Optional[str] = None


# Progress event kinds emitted as ``ToolProgressEvent.payload.kind``.
EVAL_RUN_STARTED = "eval.run_started"
EVAL_CASE_STARTED = "eval.case_started"
EVAL_CASE_FINISHED = "eval.case_finished"
EVAL_RUN_FINISHED = "eval.run_finished"

EVAL_TERMINAL_STATUSES = {"pass", "fail", "error", "stopped"}
EVAL_RUN_TERMINAL_STATUSES = {"success", "error", "stopped"}
