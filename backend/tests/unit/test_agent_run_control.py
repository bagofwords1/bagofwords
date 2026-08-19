"""Run-control invariants for long-running agent work.

Failures remain useful observations instead of terminal decisions, distant
failures do not form a streak, and a current-run Plan note is the deterministic
completion gate for multi-step work.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ai.run_control import (
    ApproachFailureTracker,
    apply_failure_strategy_policy,
    completion_checklist_for_notes,
    evaluate_completion_gate,
)


def _outcome(tool: str, arguments: dict, *, failed: bool) -> dict:
    observation = {"summary": "result"}
    if failed:
        observation["error"] = {"type": "runtime_error", "message": "boom"}
    return {
        "tool_name": tool,
        "tool_input": arguments,
        "observation": observation,
    }


def test_distant_failures_of_same_tool_do_not_accumulate():
    tracker = ApproachFailureTracker(threshold=3)

    first = tracker.record_round(1, [_outcome("inspect_data", {"query": "A"}, failed=True)])
    tracker.record_round(2, [_outcome("create_data", {"query": "B"}, failed=False)])
    distant = tracker.record_round(55, [_outcome("inspect_data", {"query": "A"}, failed=True)])

    assert first.current_streaks == {first.signatures[0]: 1}
    assert distant.current_streaks == {distant.signatures[0]: 1}
    assert distant.exhausted_signatures == ()


def test_only_consecutive_identical_failed_approaches_hit_threshold():
    tracker = ApproachFailureTracker(threshold=3)
    failed = _outcome("inspect_data", {"query": "same"}, failed=True)

    assert tracker.record_round(3, [failed]).exhausted_signatures == ()
    assert tracker.record_round(4, [failed]).exhausted_signatures == ()
    third = tracker.record_round(5, [failed])

    assert third.exhausted_signatures == third.signatures


def test_exhausted_approach_requests_replanning_without_ending_run():
    tracker = ApproachFailureTracker(threshold=3)
    rounds = [
        [_outcome("inspect_data", {"query": "same"}, failed=True)]
        for _ in range(3)
    ]

    for round_index, outcomes in enumerate(rounds):
        apply_failure_strategy_policy(
            tracker, round_index=round_index, outcomes=outcomes
        )

    observation = rounds[-1][0]["observation"]
    assert observation["approach_exhausted"] is True
    assert observation["suggested_action"] == "change_strategy"
    assert "analysis_complete" not in observation
    assert "final_answer" not in observation


def test_changed_arguments_are_a_different_approach():
    tracker = ApproachFailureTracker(threshold=3)

    tracker.record_round(7, [_outcome("inspect_data", {"query": "wide"}, failed=True)])
    changed = tracker.record_round(8, [_outcome("inspect_data", {"query": "narrow"}, failed=True)])

    assert tuple(changed.current_streaks.values()) == (1,)
    assert changed.exhausted_signatures == ()


@dataclass
class _Note:
    title: str
    content: str
    source: str = "agent"
    agent_execution_id: str | None = "run-current"


def test_only_current_run_plan_checklist_controls_completion():
    notes = [
        _Note("Plan", "- [ ] old unfinished item", agent_execution_id="run-old"),
        _Note("Plan", "- [x] locate source\n- [ ] identify root cause"),
        _Note("Findings", "- [ ] hypothesis, not a required task"),
        _Note("Plan", "- [ ] human note", source="user", agent_execution_id=None),
    ]

    status = completion_checklist_for_notes(notes, execution_id="run-current")

    assert status.found is True
    assert status.pending_items == ("identify root cause",)
    assert status.can_complete is False


def test_fully_checked_current_plan_allows_completion():
    status = completion_checklist_for_notes(
        [_Note("Plan", "- [x] retrieve logs\n- [X] support conclusion with evidence")],
        execution_id="run-current",
    )

    assert status.found is True
    assert status.pending_items == ()
    assert status.can_complete is True
    assert evaluate_completion_gate(status, plan_required=True).accepted is True


def test_multistep_run_requires_a_plan_but_simple_run_does_not():
    no_plan = completion_checklist_for_notes([], execution_id="run-current")

    multistep = evaluate_completion_gate(no_plan, plan_required=True)
    simple = evaluate_completion_gate(no_plan, plan_required=False)

    assert multistep.accepted is False
    assert multistep.reason == "missing_plan"
    assert simple.accepted is True
