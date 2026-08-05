"""Reliability invariants shared by every planner implementation and loop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.schemas.ai.planner import PlannerDecision, PlannerError


NO_PROGRESS_ERROR_CODES = frozenset({
    "empty_stream",
    "no_progress",
    "unsupported_stream_shape",
})


@dataclass
class NoProgressBudget:
    """Hard per-run ceiling for paid planner calls that made no progress."""

    max_attempts: int = 2
    attempts: int = 0

    def record(self) -> bool:
        """Record one no-progress response; return whether the fuse is open."""
        self.attempts += 1
        return self.exhausted

    @property
    def exhausted(self) -> bool:
        return self.attempts >= self.max_attempts

    @property
    def can_retry(self) -> bool:
        return not self.exhausted


def decision_actions(decision: Any) -> list:
    """Return all actions while preserving the legacy single-action contract."""
    actions = list(getattr(decision, "actions", None) or [])
    first = getattr(decision, "action", None)
    if not actions and first is not None:
        actions = [first]
    return actions


def final_decision_progress_error(
    decision: PlannerDecision,
    *,
    details: Optional[dict] = None,
) -> Optional[PlannerError]:
    """Return an error when a final decision cannot advance or finish a run.

    ``plan_type`` is presentation metadata, not a reliability signal. A final
    decision progresses only by choosing an action or declaring a terminal
    outcome. This deliberately accepts tool-only decisions with no narration.
    """
    if getattr(decision, "error", None) is not None:
        return None
    if bool(getattr(decision, "analysis_complete", False)):
        return None
    if decision_actions(decision):
        return None
    return PlannerError(
        code="no_progress",
        message="Planner returned neither a terminal outcome nor a tool action",
        details=details or None,
    )


def guard_final_decision(
    decision: PlannerDecision,
    *,
    details: Optional[dict] = None,
) -> PlannerDecision:
    """Attach a typed error to a no-progress final decision."""
    error = final_decision_progress_error(decision, details=details)
    if error is None:
        return decision
    return decision.model_copy(update={"error": error})
