"""The planner SYSTEM prompt must carry the code-visibility constraint when the
asker's role withholds `view_code`, and omit it otherwise.

Scope note, because this is easy to over-trust: these tests prove the constraint
REACHES the model. They cannot prove the model obeys it, and measurement says it
often does not — on Claude 4.5 Haiku, a user who asks "what query did you use?"
still gets the SQL back in prose. The constraint reduces unprompted code in
answers; the payload redaction in app/core/code_visibility.py is what actually
withholds code. Do not let a green run here be read as "code cannot leak".
"""
import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput

MARKER = "does not see generated code"


def _system(**overrides):
    fields = dict(user_message="top 5 artists", mode="chat")
    fields.update(overrides)
    return PromptBuilderV3._build_system(PlannerInput(**fields))


def test_constraint_present_when_the_asker_cannot_view_code():
    assert MARKER in _system(can_view_code=False)


def test_constraint_absent_when_the_asker_can_view_code():
    assert MARKER not in _system(can_view_code=True)


def test_constraint_absent_by_default():
    """Default-on visibility: an input that never sets the field must not
    silently make every answer avoid technical detail."""
    assert MARKER not in _system()


def test_constraint_is_rendered_under_org_constraints():
    """It belongs with the other org-set limits (row caps, disabled tools) —
    the section the prompt already tells the model are intentional and not to be
    worked around."""
    sys_prompt = _system(can_view_code=False)
    assert "ORG CONSTRAINTS" in sys_prompt
    assert sys_prompt.index("ORG CONSTRAINTS") < sys_prompt.index(MARKER)


def test_row_limit_constraint_still_renders_alongside():
    """Both constraints share one section; adding the second must not have
    displaced the first."""
    sys_prompt = _system(can_view_code=False, limit_row_count=500)
    assert "capped at 500 rows" in sys_prompt
    assert MARKER in sys_prompt


def test_org_constraints_section_omitted_when_nothing_constrains():
    assert "ORG CONSTRAINTS" not in _system()


def test_constraint_tells_the_model_what_to_do_instead():
    """Without a sanctioned alternative the model has only "show it" or "refuse
    blankly". Naming the fallback is what keeps a refusal from reading as a
    broken answer."""
    sys_prompt = _system(can_view_code=False)
    start = sys_prompt.index(MARKER)
    assert "plain words" in sys_prompt[start:start + 400]


def test_constraint_is_short():
    """It shares a system prompt with tool protocol and analytics standards. A
    constraint that grows into a policy document starts costing what it
    protects."""
    sys_prompt = _system(can_view_code=False)
    start = sys_prompt.index("- This user does not see")
    end = sys_prompt.index("\n", start)
    assert end - start < 400, f"constraint has grown to {end - start} chars"
