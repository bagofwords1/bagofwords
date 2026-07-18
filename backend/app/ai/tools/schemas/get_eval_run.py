from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class GetEvalRunsInput(BaseModel):
    """Input schema for ``get_eval_runs`` (list mode — cheap summaries only)."""

    status: Literal["in_progress", "success", "error", "stopped", "all"] = Field(
        default="all",
        description="Filter by run status. 'in_progress' lists currently executing runs.",
    )
    limit: int = Field(default=10, ge=1, le=50, description="Max runs to return (most recent first).")


class EvalRunSummary(BaseModel):
    run_id: str
    title: Optional[str] = None
    status: str
    trigger_reason: Optional[str] = None
    build_number: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    total: int = 0
    finished: int = 0
    passed: int = 0
    failed: int = 0


class GetEvalRunsOutput(BaseModel):
    success: bool
    items: List[EvalRunSummary] = Field(default_factory=list)
    total: int = 0
    message: Optional[str] = None


class GetEvalRunInput(BaseModel):
    """Input schema for ``get_eval_run`` (single-run detail)."""

    run_id: str = Field(..., description="The TestRun id to read.")
    compare_to_previous: bool = Field(
        default=False,
        description=(
            "Also diff this run against the most recent prior terminal run "
            "sharing at least one case — answers 'did my instruction change "
            "fix it?' in one call (fixed / regressed / same per case)."
        ),
    )
    include_transcript: bool = Field(
        default=False,
        description=(
            "Attach the agent's execution transcript for FAILED cases (tool "
            "calls, data digests). Off by default to keep reads small; turn on "
            "to debug *why* the agent produced a failing answer beyond the rule "
            "verdicts."
        ),
    )


class EvalRuleView(BaseModel):
    """A compact expectation rule (what the case asserts)."""
    type: Optional[str] = None
    summary: Optional[str] = None


class EvalRuleResultView(BaseModel):
    """The verdict for one rule — the judge's message / expected-vs-actual."""
    rule: Optional[str] = None
    status: str
    message: Optional[str] = None
    actual: Optional[Any] = None


class EvalCaseDetail(BaseModel):
    """Per-case detail: enough to see why a case passed or failed without a
    second lookup. ``failure_reason`` is derived from failing rules when the
    persisted column is null (the common case for a plain ``fail``)."""
    case_id: str
    case_name: Optional[str] = None
    status: str
    failure_reason: Optional[str] = None
    prompt: Optional[str] = None
    rules: List[EvalRuleView] = Field(default_factory=list)
    rule_results: List[EvalRuleResultView] = Field(default_factory=list)
    # Populated only when include_transcript=true and the case failed.
    transcript: Optional[str] = None


class GetEvalRunOutput(BaseModel):
    success: bool
    run_id: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    trigger_reason: Optional[str] = None
    build_number: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    total: int = 0
    finished: int = 0
    passed: int = 0
    failed: int = 0
    results: List[EvalCaseDetail] = Field(default_factory=list)
    # Present only when compare_to_previous=true and a baseline run exists:
    # {against_run: {...}, summary: {fixed, regressed, same, added, removed},
    #  flips: [{case_id, case_name, base_status, status, flip}, ...]}
    compare: Optional[Dict[str, Any]] = None
    rejected_reason: Optional[str] = None
    message: Optional[str] = None
