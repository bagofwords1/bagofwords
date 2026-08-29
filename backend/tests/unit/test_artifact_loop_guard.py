"""Artifact loop-guard behavior: budget-only, no forced terminations.

The old guard force-ended the turn after two consecutive artifact edits with
a canned "The dashboard has been created and rendered successfully." — but
two different edits in one turn is a normal convergent plan, and free-text
edit prompts never repeat verbatim, so "same tool twice" was noise. The only
artifact-specific guard now is the per-turn cost budget, and its refusal is
a plain failed-tool observation the planner sees and wraps up from — never a
forced analysis_complete with a fabricated summary.
"""

import inspect
import re

from app.ai.agent_v2 import AgentV2


def _main_loop_source() -> str:
    return inspect.getsource(AgentV2)


def test_no_consecutive_artifact_breaker():
    src = _main_loop_source()
    assert "consecutive_artifact_tool_count" not in src
    assert "max_consecutive_artifact_calls" not in src
    # The canned fake-success sign-off must be gone entirely
    assert "The dashboard has been created and rendered successfully." not in src


def test_artifact_budget_raised_to_four():
    assert re.search(r"max_total_artifact_calls\s*=\s*4", _main_loop_source())


def test_budget_refusal_escalates_nudge_then_stop():
    """First refusal must be non-terminal (planner writes its own close);
    a second refusal must end the turn. A purely non-terminal refusal made a
    small model re-request edit_artifact ~24x to the step limit and then
    narrate the refused edit as completed (observed in the sandbox)."""
    src = _main_loop_source()
    assert 'artifact_refusals = {"n": 0}' in src
    assert 'artifact_refusals["n"] += 1' in src
    # Terminal keys are set ONLY under the >1 escalation branch
    m = re.search(
        r'if artifact_refusals\["n"\] > 1:(.*?)return await _refuse_before_dispatch',
        src, re.S,
    )
    assert m, "escalation branch not found"
    branch = m.group(1)
    assert '"analysis_complete"' in branch
    assert '"final_answer"' in branch
    # The forced answer must not fabricate success for refused work
    assert "deferred rather than" in branch
    # ...and must not read as a broken artifact either: the versions that DID
    # save are intact, and saying otherwise is what made users think a working
    # dashboard was destroyed.
    assert "Nothing is broken and nothing was lost" in branch


def test_budget_refusal_reports_the_saved_state():
    """A refusal is a deferral, not a failure. Both the planner-facing summary
    and the user-facing error message must name what IS saved, so the turn is
    narrated as 'done through vN, one change deferred' rather than 'the edit
    failed' (the observed report: a working v8 dashboard described as broken).
    """
    src = _main_loop_source()
    assert "last_artifact_state" in src
    m = re.search(r'artifact_refusals\["n"\] \+= 1(.*?)return await _refuse_before_dispatch', src, re.S)
    assert m, "refusal block not found"
    block = m.group(1)
    assert "NOTHING IS BROKEN" in block
    assert "saved" in block
    # The user-visible card renders error.message — it must carry the same
    # honest framing, not a bare "artifact call budget reached".
    assert "Edit limit for this turn reached" in block
    assert "was not applied" in block
    assert "never describe the artifact as failed or broken" in block.lower()


def test_outcome_ends_run_first_refusal_non_terminal():
    first_refusal = {
        "skipped": True,
        "observation": {
            "summary": "Artifact call budget reached",
            "error": {"code": "artifact_budget_exhausted", "message": "artifact call budget reached"},
        },
    }
    assert AgentV2._outcome_ends_run(first_refusal) is False
    escalated = {
        "skipped": True,
        "observation": {**first_refusal["observation"], "analysis_complete": True},
    }
    assert AgentV2._outcome_ends_run(escalated) is True
