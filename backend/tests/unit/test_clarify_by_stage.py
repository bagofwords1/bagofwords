"""Clarify before a build starts; once it has started, finish it or ask the user."""
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput


def _prompt() -> str:
    return PromptBuilderV3.build_prompt(PlannerInput(
        user_message="Build a dashboard of sales", organization_name="Acme", organization_ai_analyst_name="Analyst"))


def test_the_dashboards_rule_is_about_the_stage():
    prompt = _prompt()
    rule = prompt[prompt.index("**Ask before, not during.**"):]
    rule = rule[:rule.index("\n")]
    assert "Before the first query" in rule
    assert "Once you have started, finish the build" in rule
    assert "ask the user with clarify" in rule and "never end with a note to yourself" in rule


def test_partial_data_no_longer_triggers_a_question():
    assert "data covering only part of the ask → clarify" not in _prompt()


def test_the_general_clarify_section_is_unchanged():
    assert "CLARIFY (default posture: act, don't ask)" in _prompt()
