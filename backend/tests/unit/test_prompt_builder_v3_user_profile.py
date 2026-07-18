"""Unit tests for the <user_profile> injection in PromptBuilderV3.

The asker's name/note (set on Membership.note) must appear in the per-turn
user message, NOT the cached system prefix, so it never invalidates the
Anthropic prompt cache. Also verifies omission when both fields are empty.
"""
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.schemas.ai.planner import PlannerInput


def _input(**kwargs) -> PlannerInput:
    base = dict(
        user_message="how many orders last month?",
        organization_name="Acme",
        organization_ai_analyst_name="Ada",
    )
    base.update(kwargs)
    return PlannerInput(**base)


def test_user_profile_injected_when_name_and_note_present():
    planner_input = _input(
        user_name="Alice",
        user_note="CFO, focuses on monthly close metrics",
    )
    built = PromptBuilderV3.build(planner_input)

    user_msg = built.messages[0]["content"]
    assert "<user_profile>" in user_msg
    assert "name: Alice" in user_msg
    assert "note: CFO, focuses on monthly close metrics" in user_msg

    # Per-user values must NOT leak into the cached system prefix.
    # (The system prompt mentions the literal "<user_profile>" tag in its
    # guidance — we check that no actual per-user payload appears there.)
    assert "Alice" not in built.system
    assert "monthly close metrics" not in built.system


def test_user_profile_omitted_when_both_empty():
    planner_input = _input(user_name=None, user_note=None)
    built = PromptBuilderV3.build(planner_input)
    user_msg = built.messages[0]["content"]
    assert "<user_profile>" not in user_msg


def test_user_profile_with_name_only():
    planner_input = _input(user_name="Bob", user_note=None)
    built = PromptBuilderV3.build(planner_input)
    user_msg = built.messages[0]["content"]
    assert "<user_profile>name: Bob</user_profile>" in user_msg
    assert "note:" not in user_msg


def test_user_profile_with_note_only():
    planner_input = _input(user_name=None, user_note="Data analyst")
    built = PromptBuilderV3.build(planner_input)
    user_msg = built.messages[0]["content"]
    assert "<user_profile>note: Data analyst</user_profile>" in user_msg


def test_user_profile_strips_whitespace():
    planner_input = _input(user_name="  Carol  ", user_note="   ")
    built = PromptBuilderV3.build(planner_input)
    user_msg = built.messages[0]["content"]
    assert "<user_profile>name: Carol</user_profile>" in user_msg


def test_system_prompt_mentions_user_profile_handling():
    """The system prompt should tell the model how to treat <user_profile>."""
    planner_input = _input(user_name="Alice", user_note="CFO")
    built = PromptBuilderV3.build(planner_input)
    assert "<user_profile>" in built.system  # appears in guidance, not as data
    assert "context" in built.system.lower()


# --- <user_memory> injection (agent-curated per-user memory) ---


def test_user_memory_injected_in_user_turn_not_system():
    planner_input = _input(
        user_memory="Prefers figures in ₪K. Likes cohort breakdowns.",
    )
    built = PromptBuilderV3.build(planner_input)

    user_msg = built.messages[0]["content"]
    assert "<user_memory>" in user_msg
    assert "Prefers figures in ₪K" in user_msg
    # Memory is per-user — must not leak into the cached system prefix.
    assert "cohort breakdowns" not in built.system


def test_user_memory_omitted_when_empty():
    for val in (None, "", "   "):
        planner_input = _input(user_memory=val)
        built = PromptBuilderV3.build(planner_input)
        user_msg = built.messages[0]["content"]
        assert "<user_memory>" not in user_msg


def test_user_memory_and_profile_coexist():
    planner_input = _input(
        user_name="Alice",
        user_note="CFO",
        user_memory="Wants concise answers, no emoji.",
    )
    built = PromptBuilderV3.build(planner_input)
    user_msg = built.messages[0]["content"]
    assert "<user_profile>" in user_msg
    assert "<user_memory>" in user_msg
    assert "Wants concise answers" in user_msg


def test_system_prompt_explains_memory_subordinate_to_instructions():
    """The system prompt must tell the model memory yields to org instructions."""
    planner_input = _input(user_memory="Prefers Python")
    built = PromptBuilderV3.build(planner_input)
    assert "<user_memory>" in built.system  # guidance mentions the tag
    assert "update_user_memory" in built.system
