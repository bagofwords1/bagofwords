"""Deterministic run-control helpers for long-running agent work.

The planner understands whether work is meaningful, but it must not own the
mechanical circuit breakers that decide whether one failed approach or an
unfinished checklist ends the entire run. This module keeps those invariants
small and independently testable.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.models.note import Note

_CHECKBOX_RE = re.compile(
    r"^\s*[-*+]\s+\[(?P<mark>[ xX])\]\s+(?P<label>.+?)\s*$"
)
MAX_COMPLETION_REJECTIONS = 2


def _observation_failed(observation: Any) -> bool:
    if not isinstance(observation, dict):
        return False
    return bool(observation.get("error") or observation.get("success") is False)


def tool_approach_signature(outcome: dict) -> str:
    """Return a stable, non-sensitive identity for one tool approach."""
    tool_name = str(outcome.get("tool_name") or "unknown")
    arguments = outcome.get("tool_input") or {}
    normalized = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{digest}"


@dataclass(frozen=True)
class ApproachFailureRound:
    signatures: tuple[str, ...]
    current_streaks: dict[str, int]
    exhausted_signatures: tuple[str, ...]


class ApproachFailureTracker:
    """Track only identical failures in adjacent planner rounds.

    A failure at round 1 and another at round 55 is historical telemetry, not
    a two-strike streak. A batch counts once per distinct tool+arguments
    signature, and a success for that exact signature wins over a failed batch
    mate in the same round.
    """

    def __init__(self, threshold: int = 3) -> None:
        self.threshold = max(1, int(threshold))
        self._last_round: int | None = None
        self._current_streaks: dict[str, int] = {}

    def record_round(self, round_index: int, outcomes: Iterable[dict]) -> ApproachFailureRound:
        verdicts: dict[str, bool] = {}
        for outcome in outcomes:
            if not isinstance(outcome, dict) or outcome.get("skipped"):
                continue
            signature = tool_approach_signature(outcome)
            failed = _observation_failed(outcome.get("observation"))
            verdicts[signature] = verdicts.get(signature, True) and failed

        failed_signatures = tuple(sorted(sig for sig, failed in verdicts.items() if failed))
        adjacent = self._last_round is not None and round_index == self._last_round + 1
        current = {
            signature: (self._current_streaks.get(signature, 0) + 1 if adjacent else 1)
            for signature in failed_signatures
        }
        self._last_round = round_index
        self._current_streaks = current
        exhausted = tuple(
            signature
            for signature in failed_signatures
            if current[signature] >= self.threshold
        )
        return ApproachFailureRound(
            signatures=failed_signatures,
            current_streaks=dict(current),
            exhausted_signatures=exhausted,
        )


def apply_failure_strategy_policy(
    tracker: ApproachFailureTracker,
    *,
    round_index: int,
    outcomes: Iterable[dict],
) -> ApproachFailureRound:
    """Annotate exhausted approaches for the next planner turn.

    This deliberately never writes ``analysis_complete`` or ``final_answer``:
    exhausting one approach is evidence for replanning, not a run outcome.
    """
    outcomes = list(outcomes)
    failure_round = tracker.record_round(round_index, outcomes)
    exhausted = set(failure_round.exhausted_signatures)
    for outcome in outcomes:
        observation = outcome.get("observation") if isinstance(outcome, dict) else None
        if not _observation_failed(observation):
            continue
        if tool_approach_signature(outcome) not in exhausted:
            continue
        tool_name = str(outcome.get("tool_name") or "tool")
        observation.update(
            {
                "approach_exhausted": True,
                "suggested_action": "change_strategy",
                "strategy_warning": (
                    f"The same {tool_name} approach failed in three consecutive "
                    "planner rounds. Do not repeat it unchanged: narrow or change "
                    "the arguments, use another tool, or ask the user if no "
                    "alternative exists."
                ),
            }
        )
    return failure_round


@dataclass(frozen=True)
class CompletionChecklist:
    found: bool
    pending_items: tuple[str, ...] = ()
    checked_items: tuple[str, ...] = ()

    @property
    def can_complete(self) -> bool:
        # Simple tasks legitimately have no Plan note. Once a current-run Plan
        # checklist exists, every item becomes part of the completion gate.
        return not self.found or not self.pending_items


@dataclass(frozen=True)
class CompletionGateDecision:
    accepted: bool
    reason: str | None = None


def evaluate_completion_gate(
    checklist: CompletionChecklist, *, plan_required: bool
) -> CompletionGateDecision:
    """Decide whether a planner end-turn may become run success."""
    # ``plan_required`` remains in the signature for caller compatibility, but
    # a missing Plan can never be a hard completion blocker. Production showed
    # that retroactively requiring one after useful work creates a liveness
    # deadlock when the planner keeps requesting end_turn. An existing Plan is
    # still a deterministic contract and its unchecked items remain enforceable.
    _ = plan_required
    if checklist.pending_items:
        return CompletionGateDecision(accepted=False, reason="unchecked_plan")
    return CompletionGateDecision(accepted=True)


def should_reject_completion(
    decision: CompletionGateDecision,
    *,
    prior_rejections: int,
    max_rejections: int = MAX_COMPLETION_REJECTIONS,
) -> bool:
    """Bound checklist review so the completion gate cannot exhaust a run."""
    return not decision.accepted and prior_rejections < max(0, int(max_rejections))


def completion_checklist_for_notes(
    notes: Iterable[Any], *, execution_id: str
) -> CompletionChecklist:
    """Parse current-run, agent-authored notes titled exactly ``Plan``."""
    pending: list[str] = []
    checked: list[str] = []
    found = False
    execution_id = str(execution_id)

    for note in notes:
        if str(getattr(note, "agent_execution_id", "") or "") != execution_id:
            continue
        if str(getattr(note, "source", "") or "").casefold() != "agent":
            continue
        if str(getattr(note, "title", "") or "").strip().casefold() != "plan":
            continue

        for line in str(getattr(note, "content", "") or "").splitlines():
            match = _CHECKBOX_RE.match(line)
            if not match:
                continue
            found = True
            label = match.group("label").strip()
            if match.group("mark").casefold() == "x":
                checked.append(label)
            else:
                pending.append(label)

    return CompletionChecklist(
        found=found,
        pending_items=tuple(pending),
        checked_items=tuple(checked),
    )


async def load_run_completion_checklist(db: Any, *, execution_id: str) -> CompletionChecklist:
    """Load untruncated notes for one execution and evaluate its Plan."""
    result = await db.execute(
        select(Note).where(
            Note.agent_execution_id == str(execution_id),
            Note.source == "agent",
            Note.deleted_at.is_(None),
        )
    )
    return completion_checklist_for_notes(
        result.scalars().all(), execution_id=str(execution_id)
    )
