"""Unit tests for MCP tool policy normalization + layered resolution."""

import pytest

from app.services.tool_policy_service import (
    TOOL_POLICY_ALLOW,
    TOOL_POLICY_ASK,
    TOOL_POLICY_AUTO,
    TOOL_POLICY_DENY,
    ToolPolicyService,
    normalize_tool_policy,
    resolve_effective_policy,
)


class TestNormalizeToolPolicy:
    @pytest.mark.parametrize("value,expected", [
        ("allow", TOOL_POLICY_ALLOW),
        ("ask", TOOL_POLICY_ASK),
        ("deny", TOOL_POLICY_DENY),
        ("auto", TOOL_POLICY_AUTO),
        ("ALLOW", TOOL_POLICY_ALLOW),
        (" ask ", TOOL_POLICY_ASK),
    ])
    def test_valid_values_pass_through(self, value, expected):
        assert normalize_tool_policy(value) == expected

    def test_legacy_confirm_maps_to_ask(self):
        assert normalize_tool_policy("confirm") == TOOL_POLICY_ASK
        assert normalize_tool_policy("CONFIRM") == TOOL_POLICY_ASK

    @pytest.mark.parametrize("value", [None, "", "yolo", "block", "42"])
    def test_unknown_values_fall_back_to_default(self, value):
        assert normalize_tool_policy(value) == TOOL_POLICY_ALLOW
        assert normalize_tool_policy(value, default=None) is None


class TestResolveEffectivePolicy:
    def test_user_preference_wins_over_admin_policy(self):
        assert resolve_effective_policy("allow", "deny") == TOOL_POLICY_DENY
        assert resolve_effective_policy("allow", "ask") == TOOL_POLICY_ASK
        assert resolve_effective_policy("ask", "allow") == TOOL_POLICY_ALLOW
        assert resolve_effective_policy("ask", "auto") == TOOL_POLICY_AUTO
        assert resolve_effective_policy("auto", "deny") == TOOL_POLICY_DENY

    def test_admin_deny_is_absolute(self):
        for user_policy in ("allow", "ask", "auto", "deny", None):
            assert resolve_effective_policy("deny", user_policy) == TOOL_POLICY_DENY

    def test_no_user_preference_inherits_admin_policy(self):
        assert resolve_effective_policy("allow", None) == TOOL_POLICY_ALLOW
        assert resolve_effective_policy("ask", None) == TOOL_POLICY_ASK
        assert resolve_effective_policy("auto", None) == TOOL_POLICY_AUTO

    def test_legacy_confirm_resolves_as_ask(self):
        assert resolve_effective_policy("confirm", None) == TOOL_POLICY_ASK
        assert resolve_effective_policy("allow", "confirm") == TOOL_POLICY_ASK

    def test_garbage_admin_policy_fails_open_to_allow_but_user_still_applies(self):
        # Unknown stored values must not crash resolution.
        assert resolve_effective_policy("bogus", None) == TOOL_POLICY_ALLOW
        assert resolve_effective_policy("bogus", "deny") == TOOL_POLICY_DENY


class _FakeToolExecution:
    tool_name = "execute_mcp"

    def __init__(self, result_json=None, arguments_json=None, status="success"):
        self.result_json = result_json or {}
        self.arguments_json = arguments_json or {}
        self.status = status


class TestExecuteMcpDigestUserAction:
    """The planner's conversation digest must record the user's decision on
    'ask' approvals (and auto verdicts / admin deny blocks) so later turns
    don't re-attempt calls the user already settled."""

    def _digest(self, rj, args=None):
        from app.ai.context.builders.message_context_builder import _digest_execute_mcp
        return _digest_execute_mcp(_FakeToolExecution(result_json=rj, arguments_json=args or {
            "tool_name": "create_item", "arguments": {"board_id": 1},
        }))

    def test_user_decline_is_digested(self):
        d = self._digest({"success": False, "error_message": "declined",
                          "blocked_by_policy": "ask",
                          "approval": {"approved": False, "remember": False, "timed_out": False}})
        assert "USER DECLINED" in d
        assert "do not retry" in d

    def test_remembered_decisions_are_digested(self):
        d = self._digest({"success": True,
                          "approval": {"approved": True, "remember": True, "timed_out": False}})
        assert "user approved" in d
        assert "always allow" in d
        d = self._digest({"success": False, "blocked_by_policy": "ask",
                          "approval": {"approved": False, "remember": True, "timed_out": False}})
        assert "always deny" in d

    def test_timeout_is_digested(self):
        d = self._digest({"success": False, "blocked_by_policy": "ask",
                          "approval": {"approved": False, "remember": False, "timed_out": True}})
        assert "timed out" in d

    def test_auto_verdict_and_admin_deny_are_digested(self):
        d = self._digest({"success": False, "blocked_by_policy": "auto",
                          "policy_verdict": {"approved": False, "reason": "destructive"}})
        assert "auto policy review declined" in d
        assert "destructive" in d
        d = self._digest({"success": False, "blocked_by_policy": "deny",
                          "error_message": "denied by policy"})
        assert "blocked by admin policy" in d

    def test_plain_success_has_no_policy_noise(self):
        d = self._digest({"success": True, "content_type": "json"})
        assert "USER" not in d and "policy" not in d


class _FakeCompletion:
    def __init__(self, scheduled_prompt_id=None):
        self.scheduled_prompt_id = scheduled_prompt_id


class _FakeUser:
    id = "u1"


class TestIsInteractiveRun:
    def _ctx(self, **overrides):
        ctx = {
            "user": _FakeUser(),
            "is_eval_run": False,
            "platform": None,
            "head_completion": _FakeCompletion(),
        }
        ctx.update(overrides)
        return ctx

    def test_web_run_with_user_is_interactive(self):
        assert ToolPolicyService.is_interactive_run(self._ctx()) is True

    def test_eval_run_is_not_interactive(self):
        assert ToolPolicyService.is_interactive_run(self._ctx(is_eval_run=True)) is False

    def test_platform_run_is_not_interactive(self):
        for platform in ("slack", "teams", "email"):
            assert ToolPolicyService.is_interactive_run(self._ctx(platform=platform)) is False

    def test_missing_user_is_not_interactive(self):
        assert ToolPolicyService.is_interactive_run(self._ctx(user=None)) is False

    def test_scheduled_run_is_not_interactive(self):
        ctx = self._ctx(head_completion=_FakeCompletion(scheduled_prompt_id="sp1"))
        assert ToolPolicyService.is_interactive_run(ctx) is False
