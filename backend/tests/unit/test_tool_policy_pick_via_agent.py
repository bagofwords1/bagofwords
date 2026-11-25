"""Rules for which agent a connection-tool call goes through.

Each agent keeps its own rule; several agents' settings are never merged.
The connection is the ceiling: off there is off for every agent, and a locked
connection policy can be made stricter but not looser, by agents or users.
"""
from types import SimpleNamespace as N

import pytest

from app.services.tool_policy_service import (
    agent_admin_setting, pick_via_agent, resolve_effective_policy,
)


def tool(enabled=True, policy="allow", locked=False):
    return N(is_enabled=enabled, policy=policy, policy_locked=locked)


def ov(enabled=True, policy="allow"):
    return N(is_enabled=enabled, policy=policy)


@pytest.mark.parametrize("policies, winner", [
    ({"a": "deny", "b": "allow"}, "b"),
    ({"a": "allow", "b": "deny"}, "a"),
    ({"a": "ask", "b": "auto"}, "b"),
    ({"a": "ask", "b": "ask", "c": "allow"}, "c"),
])
def test_the_least_strict_agent_that_enables_the_tool_is_used(policies, winner):
    pick = pick_via_agent(tool(), list(policies), {a: ov(policy=p) for a, p in policies.items()})
    assert pick.agent_id == winner
    assert pick.effective == policies[winner]
    assert pick.is_enabled


def test_an_agent_that_disables_the_tool_never_wins_even_if_its_policy_is_looser():
    pick = pick_via_agent(tool(), ["a", "b"], {"a": ov(enabled=False, policy="allow"), "b": ov(policy="ask")})
    assert (pick.agent_id, pick.effective) == ("b", "ask")


def test_an_agent_without_its_own_row_uses_the_connection_setting():
    pick = pick_via_agent(tool(policy="ask"), ["a", "b"], {"a": ov(policy="deny")})
    assert (pick.agent_id, pick.effective) == ("b", "ask")


def test_ties_keep_the_runs_agent_order():
    for order in (["a", "b"], ["b", "a"]):
        assert pick_via_agent(tool(), order, {}).agent_id == order[0]


def test_no_agent_enabling_it_means_disabled():
    pick = pick_via_agent(tool(), ["a", "b"], {"a": ov(enabled=False), "b": ov(enabled=False)})
    assert pick.is_enabled is False


def test_the_connection_switch_is_a_ceiling():
    assert agent_admin_setting(tool(enabled=False), ov(enabled=True))[0] is False
    assert pick_via_agent(tool(enabled=False), ["a"], {"a": ov(enabled=True)}).is_enabled is False


@pytest.mark.parametrize("conn, agent, expected", [
    ("ask", "allow", "ask"), ("ask", "auto", "ask"), ("ask", "deny", "deny"), ("auto", "allow", "auto"),
])
def test_a_locked_policy_only_tightens_for_agents(conn, agent, expected):
    assert agent_admin_setting(tool(policy=conn, locked=True), ov(policy=agent))[1] == expected
    # Unlocked: the agent's own policy applies, looser or not.
    assert agent_admin_setting(tool(policy=conn), ov(policy=agent))[1] == agent


@pytest.mark.parametrize("admin, user, locked, expected", [
    ("ask", "allow", True, "ask"), ("ask", "allow", False, "allow"),
    ("ask", "deny", True, "deny"), ("allow", "ask", True, "ask"), ("deny", "allow", False, "deny"),
])
def test_a_user_preference_cannot_loosen_a_locked_policy(admin, user, locked, expected):
    assert resolve_effective_policy(admin, user, locked=locked) == expected
