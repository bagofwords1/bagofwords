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
