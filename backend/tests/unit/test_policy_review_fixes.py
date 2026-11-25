"""Policy rules that must hold however a preference got stored.

- A tool's effect caps the policy at resolution time: a destructive action
  never runs silently and an external one is never left to the model judge,
  even when a user preference (an old row, an "Always allow" click) says so.
- A locked connection policy is a floor for remote shell too: shell
  configuration and personal choices can tighten a command's decision, never
  loosen it.
- The approval card's display context travels apart from the call's
  arguments, so every argument the tool receives (``_id`` included) is shown.
"""
import asyncio
from types import SimpleNamespace as N

import pytest

from app.services.remote_shell_policy import display_policy, effective_shell_policy
from app.services.tool_policy_service import ToolPolicyResolution, clamp_to_effect, pick_via_agent


def tool(effect=None, policy="allow", locked=False):
    return N(is_enabled=True, policy=policy, policy_locked=locked, effect=effect, id="t1")


@pytest.mark.parametrize("effect, stored, expected", [
    ("destructive", "allow", "ask"), ("destructive", "auto", "ask"), ("destructive", "deny", "deny"),
    ("external", "auto", "ask"), ("external", "allow", "allow"),
    ("modify", "auto", "auto"), (None, "allow", "allow"),
])
def test_the_effect_caps_the_policy_whatever_was_stored(effect, stored, expected):
    assert clamp_to_effect(tool(effect), stored) == expected
    # Through the resolver, with the value arriving as a user preference.
    assert pick_via_agent(tool(effect, policy="ask"), ["a"], {}, user_policy=stored).effective == expected


SHELL = N(type="remote_ssh", config={"shell_policy": "allow"})


@pytest.mark.parametrize("locked, expected", [(True, "ask"), (False, "allow")])
def test_a_locked_ask_is_a_floor_for_shell_listing(locked, expected):
    assert display_policy(SHELL, "execute_shell", "ask", "allow", locked=locked) == expected


@pytest.mark.parametrize("locked, expected", [(True, "ask"), (False, "allow")])
def test_a_locked_ask_is_a_floor_for_shell_execution(locked, expected):
    t = N(is_enabled=True, policy="ask", policy_locked=locked, id="t1")
    res = ToolPolicyResolution(effective="ask", admin_policy="ask", user_policy="allow", is_enabled=True)
    got = asyncio.run(effective_shell_policy(None, SHELL, t, res, None, {"command": "ls"}))
    assert got == expected


def test_a_locked_policy_never_relaxes_a_shell_deny():
    t = N(is_enabled=True, policy="ask", policy_locked=True, id="t1")
    res = ToolPolicyResolution(effective="ask", admin_policy="ask", user_policy=None, is_enabled=True)
    deny_cfg = N(type="remote_ssh", config={"shell_policy": "deny"})
    assert asyncio.run(effective_shell_policy(None, deny_cfg, t, res, None, {"command": "ls"})) == "deny"


def test_the_card_payload_keeps_every_argument_and_carries_display_apart():
    from app.services.tool_confirmation_service import KIND_MCP_TOOL_POLICY, confirmation_payload
    row = N(kind=KIND_MCP_TOOL_POLICY, id="c1", tool_name="delete_item", connection_id="x",
            connection_tool_id="t", tool_execution_id="te",
            arguments={"_id": "rec-42", "reason": "dup"},
            display={"via_agent": {"id": "a", "name": "Ops"}, "effect": "destructive"})
    p = confirmation_payload(row)
    assert p["arguments"] == {"_id": "rec-42", "reason": "dup"}
    assert p["display"]["via_agent"]["name"] == "Ops" and p["display"]["effect"] == "destructive"
