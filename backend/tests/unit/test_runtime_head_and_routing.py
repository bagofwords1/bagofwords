"""Per-turn runtime head + routing placement contracts (planner v3).

The design under test (see PR #837 review):
- The SYSTEM prompt carries NO routing content at all — routing state must
  never fragment a model's system-prompt cache lineage (haiku→sonnet→haiku
  keeps one cached prefix per model).
- Current model identity + the state-appropriate routing hint render in the
  per-turn USER message (<runtime> head), in both directions: escalate hint
  while on the small default, route-back-down hint after escalation.
- Org constraints (row cap) are stated as a compliance principle, not a bare
  number, and the error section carries a goal-level loop breaker for
  dead-ends where every call succeeds.
"""
from app.schemas.ai.planner import PlannerInput
from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3


def _system(**kw) -> str:
    return PromptBuilderV3._build_system(PlannerInput(user_message="x", **kw))


def _user(**kw) -> str:
    return PromptBuilderV3._build_user_message(PlannerInput(user_message="x", **kw))


def test_system_prompt_has_no_routing_content_in_any_state():
    for kw in ({}, {"current_model": "M", "routing_state": "small"},
               {"current_model": "M", "routing_state": "routed"}):
        s = _system(**kw)
        assert "route_model" not in s
        assert "MODEL ROUTING" not in s


def test_system_prompt_is_byte_stable_across_routing_states():
    base = _system()
    assert _system(current_model="GPT-5.6 Terra", routing_state="small") == base
    assert _system(current_model="GPT-5.6 Terra", routing_state="routed") == base


def test_runtime_head_small_state_hints_escalation():
    u = _user(current_model="Claude Haiku", routing_state="small")
    assert "<runtime>" in u
    assert "model: Claude Haiku" in u
    assert "route_model FIRST" in u
    # escalation is batched with the first research call, not a lone turn
    assert "batched with your first research call" in u


def test_runtime_head_routed_state_hints_deescalation_without_pingpong():
    u = _user(current_model="GPT-5.6 Terra", routing_state="routed")
    assert "model: GPT-5.6 Terra" in u
    assert "already routed" in u
    assert "route back to the small default" in u
    assert "never ping-pong" in u


def test_runtime_head_model_only_when_routing_inactive():
    u = _user(current_model="Claude Sonnet")
    assert "<runtime>model: Claude Sonnet</runtime>" in u
    assert "route_model" not in u


def test_runtime_head_absent_without_model_info():
    assert "<runtime>" not in _user()


def test_row_limit_is_a_compliance_principle_not_a_bare_number():
    s = _system(limit_row_count=1000)
    assert "ORG CONSTRAINTS" in s
    assert "1000" in s
    # the principle: work within limits, not around them
    assert "not around them" in s
    # and it never renders when the org sets no cap
    assert "ORG CONSTRAINTS" not in _system()


def test_error_section_has_goal_level_loop_breaker():
    s = _system()
    assert "Not every dead end is an error" in s
    assert "unsatisfiable" in s


def test_route_model_description_is_bidirectional():
    from app.ai.tools.implementations.route_model import RouteModelTool
    desc = RouteModelTool().metadata.description
    assert "Route back down" in desc
    assert "one-way" not in desc and "at most once" not in desc.lower()
    assert "never ping-pong" in desc
