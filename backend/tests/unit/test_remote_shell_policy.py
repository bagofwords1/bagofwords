import pytest

from app.services.remote_shell_policy import resolve_shell_policy


@pytest.mark.parametrize("personal", ["allow", "ask", "deny", None])
def test_connection_allow_applies_to_every_user(personal):
    assert resolve_shell_policy({"shell_policy": "allow"}, "printf ready", personal=personal) == "allow"


def test_default_and_strict_modes():
    assert resolve_shell_policy({}, "ls /logs") == "ask"
    assert resolve_shell_policy({"shell_policy": "strict"}, "ls", personal="allow") == "ask"
    assert resolve_shell_policy({"shell_policy": "deny"}, "ls", personal="allow") == "deny"


def test_user_choices_and_rules_are_scoped():
    rules = [{"pattern": "docker ps*", "action": "allow", "match": "glob", "working_directory": "/srv"}]
    assert resolve_shell_policy({}, "docker ps -a", user_rules=rules, cwd="/srv") == "allow"
    assert resolve_shell_policy({}, "docker ps -a", user_rules=rules, cwd="/elsewhere") == "ask"
    assert resolve_shell_policy({}, "docker ps; rm -rf /srv", user_rules=rules, cwd="/srv") == "ask"
    assert resolve_shell_policy({}, "ls", personal="allow") == "allow"


def test_connection_deny_rule_cannot_be_relaxed():
    cfg = {"shell_policy": "policy", "shell_rules": [{"pattern": "docker rm*", "action": "deny", "match": "glob"}]}
    assert resolve_shell_policy(cfg, "docker rm web", personal="allow") == "deny"


@pytest.mark.parametrize("command", ["echo ready; docker rm web", "echo ready && docker rm web", "echo ready\ndocker rm web", "'docker' rm web"])
def test_admin_command_denials_cover_compound_segments(command):
    config = {"shell_rules": [{"pattern": "docker rm*", "action": "deny", "match": "glob"}]}
    assert resolve_shell_policy(config, command, personal="allow") == "deny"


@pytest.mark.parametrize("personal", ["ask", "deny"])
def test_personal_choice_can_restrict_administrator_command_allow(personal):
    config = {"shell_rules": [{"pattern": "docker ps*", "action": "allow", "match": "glob"}]}
    assert resolve_shell_policy(config, "docker ps -a", personal=personal) == personal


@pytest.mark.parametrize("mode", ["strict", "allow"])
def test_admin_deny_rules_apply_in_every_mode(mode):
    cfg = {"shell_policy": mode, "shell_rules": [{"pattern": "rm -rf*", "action": "deny", "match": "glob"}]}
    assert resolve_shell_policy(cfg, "rm -rf /srv/data", personal="allow") == "deny"


def test_shell_display_uses_enabled_default_but_honors_explicit_disable():
    from app.services.remote_shell_policy import display_policy
    assert display_policy({"type": "remote_ssh", "config": {}}, "execute_shell", "ask", None) == "ask"
    assert display_policy({"type": "remote_ssh", "config": {"enable_shell": False, "shell_policy": "allow"}}, "execute_shell", "ask", None) == "deny"
