"""Prompt-side contract of sub-agents (docs/design/sub-agents.md A4/A5/A10).

- The parent system prompt carries the DELEGATION block only when the tool
  is in the catalog, and the block follows the org posture.
- A child (mode=subagent) gets the lean sub-agent system prompt: no clarify, no
  dashboards, the finish contract, the profile addendum; and its per-turn head
  carries the remaining budget.
- Profiles never leak a blocked tool and never include delegate.
"""
import pytest

from app.ai.agents.planner.prompt_builder_v3 import PromptBuilderV3
from app.ai.sub_agent_profiles import BLOCKED_IN_SUB_AGENTS, PROFILES, allowed_tools, get_profile
from app.services.sub_agent_service import effective_concurrency
from app.schemas.ai.planner import PlannerInput


def _parent(**kw):
    return PlannerInput(
        user_message="why did paid orders drop?", organization_name="Acme", organization_ai_analyst_name="Bow",
        mode="chat", **kw,
    )


def test_delegation_block_follows_catalog_and_posture():
    off = PromptBuilderV3._build_system(_parent(sub_agents_enabled=False))
    assert "DELEGATION" not in off and "delegate" not in off.split("CLARIFY")[0].split("ROUTING")[-1].lower().replace("delegated", "")

    proactive = PromptBuilderV3._build_system(_parent(sub_agents_enabled=True, sub_agent_posture="proactive"))
    assert "DELEGATION (sub-agents)" in proactive
    assert "END YOUR TURN" in proactive
    assert "SELF-REPORT" in proactive
    # Root-cause routing names delegate as the fan-out for independent causes.
    assert "ONE `delegate` sub-agent per independent cause" in proactive
    assert "is ALWAYS delegated" in proactive

    explicit = PromptBuilderV3._build_system(_parent(sub_agents_enabled=True, sub_agent_posture="explicit"))
    assert "explicit posture" in explicit
    assert "ONLY when the user or an instruction asks" in explicit
    assert "ONE `delegate` sub-agent per independent cause" not in explicit


def test_subagent_system_prompt_is_the_lean_variant_with_finish_contract():
    pi = PlannerInput(
        user_message="GOAL\nTest the pricing hypothesis", organization_name="Acme", organization_ai_analyst_name="Bow",
        mode="subagent", sub_agent_title="Pricing change hypothesis",
        sub_agent_profile_addendum=get_profile("investigate").addendum, sub_agent_budget="sub-agent step 3 of 40",
    )
    v3 = PromptBuilderV3.build(pi)
    assert "Mode: Sub-agent" in v3.system
    assert 'delegated goal: "Pricing change hypothesis"' in v3.system
    assert "finish_subtask" in v3.system
    assert "PROFILE: investigate" in v3.system
    for absent in ("DASHBOARDS", "DOCUMENTS", "CLARIFY (default posture", "create_artifact", "<memory>"):
        assert absent not in v3.system, absent
    body = "\n".join(
        m["content"] if isinstance(m["content"], str) else str(m["content"]) for m in v3.messages
    )
    assert "<sub_agent_budget>sub-agent step 3 of 40</sub_agent_budget>" in body


def test_sub_agents_context_rides_in_the_parent_turn_head():
    pi = _parent(sub_agents_enabled=True, sub_agents_context='<sub_agents>\n  <wave id="w" status="running" total="2">\n  </wave>\n</sub_agents>')
    head = PromptBuilderV3._build_turn_head(pi)
    assert '<wave id="w" status="running"' in head
    # Once a wave exists the per-turn posture reminder steps aside for the state block.
    assert '<delegation posture="proactive">' not in head


@pytest.mark.parametrize("empty_context", [None, "", "\n", "  "])
def test_proactive_reminder_rides_in_the_turn_head_until_a_wave_exists(empty_context):
    fresh = PromptBuilderV3._build_turn_head(_parent(sub_agents_enabled=True, sub_agent_posture="proactive", sub_agents_context=empty_context))
    assert '<delegation posture="proactive">' in fresh and "END YOUR TURN" in fresh
    for pi in (
        _parent(sub_agents_enabled=True, sub_agent_posture="explicit"),
        _parent(sub_agents_enabled=False),
    ):
        assert "<delegation" not in PromptBuilderV3._build_turn_head(pi)
    child = PlannerInput(
        user_message="GOAL", organization_name="Acme", organization_ai_analyst_name="Bow", mode="subagent",
        sub_agents_enabled=False, sub_agent_budget="sub-agent step 1 of 40",
    )
    assert "<delegation" not in PromptBuilderV3._build_turn_head(child)


@pytest.mark.parametrize("key", list(PROFILES))
def test_profiles_never_include_blocked_or_parent_only_tools(key):
    for allow_cd in (False, True):
        tools = allowed_tools(get_profile(key), allow_create_data=allow_cd)
        assert "finish_subtask" in tools
        assert not (tools & BLOCKED_IN_SUB_AGENTS), tools & BLOCKED_IN_SUB_AGENTS
        assert "delegate" not in tools and "clarify" not in tools
        # The list gateway is never blocked outright: a child writes into the
        # parent's lists on the same switch as create_data; defining a list is not.
        assert "create_list" not in tools and "edit_list" not in tools
        if key == "investigate":
            assert ("create_data" in tools) is allow_cd
            assert ("submit_list" in tools) is allow_cd
        if key == "validate":
            assert "create_data" in tools
            assert "submit_list" in tools


def test_sqlite_runs_children_one_at_a_time():
    # One writer: parallel children starve each other into "database is locked".
    assert effective_concurrency(3, "sqlite") == 1
    assert effective_concurrency(1, "sqlite") == 1
    assert effective_concurrency(3, "postgresql") == 3
    assert effective_concurrency(3, "") == 3


def test_synthesized_record_never_carries_the_repeat_guard_text():
    from types import SimpleNamespace
    from app.models.sub_agent import EXIT_MAX_TOKENS, EXIT_NO_FINISH_CALL
    from app.services.sub_agent_service import _synthesized_result

    row = SimpleNamespace(report_note_id="note-1")
    guard = "Stopping: the create_note tool was called 2 times with identical parameters. Its result was already returned above"
    rec = _synthesized_result(row, "finished", EXIT_NO_FINISH_CALL, guard, None)
    assert rec["outcome"] == "partial" and rec["synthesized"] is True
    assert "Stopping" not in rec["summary"] and "note" in rec["summary"].lower()

    rec = _synthesized_result(SimpleNamespace(report_note_id=None), "finished", EXIT_MAX_TOKENS, "", None)
    assert rec["outcome"] == "partial" and "Token budget" in rec["summary"]


def test_sub_agent_limits_carry_the_token_cap():
    from app.services.sub_agent_service import sub_agent_limits
    assert sub_agent_limits(None)["max_tokens"] == 400000
