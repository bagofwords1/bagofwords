"""The dashboards guidance carries the clarify-by-stage rule; the general CLARIFY section stays.

Whether the planner asks before a build and finishes once it has started is model
behaviour, measured by agent runs (see the PR). This only guards that the rule is
present where page requests read it, by its title, like the other prompt-marker tests.
"""
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput

STAGE_RULE = "**Ask before, not during.**"


def _prompt() -> str:
    return PromptBuilderV3.build_prompt(PlannerInput(
        user_message="Build a dashboard of sales", organization_name="Acme", organization_ai_analyst_name="Analyst"))


def test_the_stage_rule_is_in_the_dashboards_section():
    prompt = _prompt()
    dashboards = prompt[prompt.index("DASHBOARDS\n"):prompt.index("DOCUMENTS\n")]
    assert dashboards.count(STAGE_RULE) == 1


def test_the_general_clarify_section_is_still_there():
    prompt = _prompt()
    assert prompt.count("CLARIFY (") == 1
