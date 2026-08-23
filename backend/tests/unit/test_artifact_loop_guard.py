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


def test_artifact_budget_is_raised_and_non_terminal():
    src = _main_loop_source()
    assert re.search(r"max_total_artifact_calls\s*=\s*4", src)
    # The budget refusal block must not force termination: extract the
    # refusal observation dict and check it carries no terminal keys.
    m = re.search(
        r'artifact_budget_exhausted.*?\}\s*,\s*\n\s*\}\s*,?\s*\n\s*\}', src, re.S
    )
    assert m, "budget refusal block not found"
    block_start = src.rfind("if tool_name in (\"create_artifact\", \"edit_artifact\")", 0, m.start())
    refusal_block = src[block_start:m.end()]
    assert '"analysis_complete"' not in refusal_block
    assert '"final_answer"' not in refusal_block


def test_outcome_ends_run_treats_budget_refusal_as_non_terminal():
    refusal_outcome = {
        "skipped": True,
        "observation": {
            "summary": "Artifact call budget reached",
            "error": {"code": "artifact_budget_exhausted", "message": "artifact call budget reached"},
        },
    }
    assert AgentV2._outcome_ends_run(refusal_outcome) is False
    # Sanity: a genuinely terminal observation still ends the run
    assert AgentV2._outcome_ends_run({"observation": {"analysis_complete": True}}) is True
