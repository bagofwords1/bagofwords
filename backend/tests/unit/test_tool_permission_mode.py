"""Pure rules behind the run permission mode and the built-in blocklist."""
import pytest

from app.services import tool_permission_mode as pm
from app.services.tool_blocklist import block_reason, shell_block_reason, sql_block_reason


# ── blocklist ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "rm -rf /", "rm -fr ~", "sudo rm -rf /*", "rm --recursive --force $HOME",
    "ls && rm -rf /", "cd /tmp; rm -Rf *", "rm --no-preserve-root -rf /",
    "mkfs.ext4 /dev/sdb1", "dd if=/dev/zero of=/dev/sda bs=1M", "shutdown -h now",
    "sudo reboot", ":(){ :|:& };:", "echo x > /dev/sda",
    "Format-Volume -DriveLetter D", "Remove-Item -Recurse -Force C:\\",
    "systemctl reboot", "/sbin/shutdown -r now", "sudo -n reboot", "sh -c 'rm -rf /'",
    "bash -lc \"cd / && rm -rf *\"", "echo `rm -rf ~`", "x=$(rm -rf /)",
    # Wrapper options that take a value, and trailing slashes.
    "sudo -u root rm -rf /", "timeout 10 rm -rf /", "nice -n 5 rm -rf /", "env X=1 rm -rf /",
    "sudo -u postgres timeout 60s rm -fr /var/", "rm -rf /etc/", "rm -rf /home/", "rm -rf ${HOME}/",
    "find / -print0 | xargs -0 rm -rf /", "sudo -u root shutdown -h now", "timeout 5 reboot",
])
def test_destructive_shell_commands_are_blocked(command):
    assert shell_block_reason(command)


@pytest.mark.parametrize("command", [
    "ls -la", "rm -rf ./build", "rm -rf /tmp/run-123", "rm old.log", "cat /etc/hostname",
    "echo shutdown", "grep -r reboot /var/log", "df -h", "dd if=a.img of=b.img",
    "dd if=/dev/sda of=/dev/null bs=1M count=1", "Get-Date -Format H:mm", "systemctl status nginx",
    "sudo -u postgres psql -c 'select 1'", "timeout 10 ls /", "rm -rf /tmp/build/", "env X=1 echo reboot",
])
def test_ordinary_shell_commands_are_not_blocked(command):
    assert shell_block_reason(command) is None


@pytest.mark.parametrize("sql", [
    "DROP TABLE users", "drop database prod;", "select 1; DROP SCHEMA s",
    "TRUNCATE TABLE orders", "truncate orders", "DELETE FROM orders",
    "/* cleanup */ delete from orders;", "-- x\nDROP TABLE t",
    "SELECT 1 DROP TABLE x", "select 1\nGO\ndrop table x", "insert into a values (1); truncate b",
    "TRUNCATE TABLE orders CASCADE", "truncate table a, b", "TRUNCATE ONLY t RESTART IDENTITY",
    "DELETE orders", "delete from only orders",
])
def test_destructive_sql_is_blocked(sql):
    assert sql_block_reason(sql)


@pytest.mark.parametrize("text", [
    "SELECT * FROM orders", "DELETE FROM orders WHERE id = 3", "how do I drop table x?",
    "Explain TRUNCATE vs DELETE", "update t set a = 1 where id = 2", "",
])
def test_ordinary_text_and_sql_are_not_blocked(text):
    assert sql_block_reason(text) is None


@pytest.mark.parametrize("key,text", [
    ("subject", "Delete from inbox"), ("title", "x; truncate the axis"), ("body", "truncate orders"),
])
def test_prose_outside_sql_arguments_is_not_blocked(key, text):
    assert block_reason(tool_name="send_message", arguments={key: text}) is None


def test_block_reason_reads_nested_arguments_and_tool_names():
    assert block_reason(tool_name="run_query", arguments={"opts": {"sql": ["select 1", "DROP TABLE t"]}})
    assert block_reason(tool_name="delete_all_rows", arguments={})
    assert block_reason(tool_name="drop_table", arguments={})
    assert block_reason(tool_name="get_table", arguments={"q": "x"}) is None
    for name in ("purge_all_data", "drop_table_if_exists", "db.drop_table", "srv__wipe_database"):
        assert block_reason(tool_name=name, arguments={}), name
    assert block_reason(tool_name="run", arguments={"sql": "DELETE FROM orders"})
    # Shell patterns only apply to the remote shell tool.
    assert block_reason(tool_name="search", arguments={"command": "rm -rf /"}) is None
    assert block_reason(tool_name="execute_shell", arguments={"command": "rm -rf /"}, remote_shell=True)


# ── mode resolution ─────────────────────────────────────────────────────────

def _settings(**kw):
    return pm.PermissionSettings(**{**pm.normalize_settings({}), **kw})


def test_allow_all_is_refused_unless_explicitly_permitted():
    assert pm.effective_mode("allow_all", _settings(allow_all_enabled=True)) == "ask"


def test_report_mode_wins_over_the_org_default():
    s = _settings(default_mode="auto")
    assert pm.effective_mode(None, s) == "auto"
    assert pm.effective_mode("ask", s) == "ask"


@pytest.mark.parametrize("switch,permitted,expect", [
    (True, True, "allow_all"), (False, True, "ask"), (True, False, "ask"),
])
def test_allow_all_needs_switch_and_a_watched_owner_run(switch, permitted, expect):
    s = _settings(allow_all_enabled=switch)
    assert pm.effective_mode("allow_all", s, allow_all_permitted=permitted) == expect


def test_strictest_agent_cap_applies():
    s = _settings(allow_all_enabled=True)
    assert pm.effective_mode("allow_all", s, agent_caps=["auto"], allow_all_permitted=True) == "auto"
    assert pm.effective_mode("allow_all", s, agent_caps=["auto", "ask", None], allow_all_permitted=True) == "ask"
    assert pm.effective_mode("ask", s, agent_caps=["auto"]) == "ask"  # a cap never loosens


@pytest.mark.parametrize("p,expect", [
    (0.99, "allow"), (0.85, "allow"), (0.5, "ask"), (0.3, "refuse"), (0.0, "refuse"),
    (None, "ask"), (float("nan"), "ask"), ("junk", "ask"),
])
def test_decision_model_probability_maps_to_three_outcomes(p, expect):
    assert pm.verdict_from_probability(p, _settings()) == expect


@pytest.mark.parametrize("approve,conf,expect", [
    (True, 0.9, "allow"), (False, 0.9, "refuse"), (True, 0.5, "ask"), (False, 0.0, "ask"),
])
def test_small_model_acts_only_on_a_confident_answer(approve, conf, expect):
    assert pm.verdict_from_small_model(approve, conf, _settings()) == expect


def test_settings_normalize_bad_values_to_safe_defaults():
    s = pm.normalize_settings({
        "default_mode": "allow_all", "allow_all_enabled": "yes", "unattended": "sure",
        "allow_threshold": 0.4, "refuse_threshold": 0.9, "junk": 1,
    })
    assert s["default_mode"] == "ask"          # allow_all needs a real True switch
    assert s["allow_all_enabled"] is False
    assert s["unattended"] == "refuse"
    assert s["allow_threshold"] >= pm.MIN_ALLOW_THRESHOLD
    assert s["refuse_threshold"] <= s["allow_threshold"]
    enabled = pm.normalize_settings({"default_mode": "allow_all", "allow_all_enabled": True})
    assert enabled["default_mode"] == "ask"  # never the org default
    assert "junk" not in s


@pytest.mark.parametrize("command,windows,expect", [
    ("ls -la /var/log", False, True), ("df -h", False, True),
    ("ps aux", False, False), ("ps eww", False, False),  # can print other processes' environment
    ("Get-ChildItem Env:", True, False), ("Get-Item Env:AWS_SECRET_ACCESS_KEY", True, False),
    ("ls; rm x", False, False), ("ls $(whoami)", False, False), ("rm x", False, False),
    ("ls *", False, False), ("ls > f", False, False),
    # Reads contents (could print a secret) or can also set something: judge.
    ("cat ~/.ssh/id_rsa", False, False), ("tail -F app.log", False, False),
    ("date -s 2020-01-01", False, False), ("hostname newname", False, False),
    ("Get-ChildItem C:\\data", True, True), ("dir C:\\", True, True),
    ("Remove-Item C:\\x", True, False), ("Get-Content .env", True, False),
    ("ipconfig /release", True, False),
])
def test_read_only_shell_rule(command, windows, expect):
    assert pm.is_read_only_shell_command(command, windows=windows) is expect


def test_webhook_turn_counts_as_unattended():
    from types import SimpleNamespace

    from app.services.tool_policy_service import ToolPolicyService

    user = SimpleNamespace(id="u")
    for marker in ("webhook_id", "trigger_source", "scheduled_prompt_id"):
        head = SimpleNamespace(**{"webhook_id": None, "trigger_source": None, "scheduled_prompt_id": None, marker: "x"})
        assert not ToolPolicyService.is_interactive_run({"user": user, "head_completion": head})
    head = SimpleNamespace(webhook_id=None, trigger_source=None, scheduled_prompt_id=None)
    assert ToolPolicyService.is_interactive_run({"user": user, "head_completion": head})
