"""Unit test: the system prompt teaches the model that data sources == agents.

This terminology line lives in the cached system prefix (not the per-turn user
message) so it doesn't cost tokens per turn. Both directions matter: the model
should refer to data sources as "agents" in its replies, and it should recognize
"agent" in user messages or instructions/skills as meaning a connected data
source.
"""
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput


def _input(**kwargs) -> PlannerInput:
    base = dict(
        user_message="hi",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
    )
    base.update(kwargs)
    return PlannerInput(**base)


def test_terminology_data_source_equals_agent_in_system_prompt():
    built = PromptBuilderV3.build(_input())
    system = built.system

    # Single canonical line that ties both directions of the vocabulary.
    assert "each connected data source is an **agent**" in system
    # Reading direction: user/instruction/skill says "agent" -> data source.
    assert 'when the user' in system and '"agent,"' in system
    assert "<data_source>" in system

    # Lives in the cached system prefix, NOT the per-turn user message —
    # otherwise it would invalidate the Anthropic prompt cache every turn.
    user_msg = built.messages[0]["content"]
    assert "each connected data source is an **agent**" not in user_msg
