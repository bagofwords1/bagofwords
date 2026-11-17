"""Run permission mode (Auto / Ask / Allow all) for connection tools.

Contract (app/services/tool_permission_mode.py, execute_mcp._enforce_policy):
  * the mode only loosens tools left on ask / auto: a disabled tool, a denied
    tool and a call on the built-in blocklist are refused in every mode;
  * Allow all needs the org switch, runs ask tools without a prompt, and acts
    like Ask in runs nobody is watching;
  * Auto: a read-only tool runs, a confident judge allows or refuses, an
    unsure or failing judge asks the user — never allows;
  * a prompt in a run nobody is watching follows the org's unattended
    setting (refuse by default);
  * a "for this report" answer is reused for the same user, tool and report
    only, and is not written as a lasting preference.

Real: routes, services, DB, execute_mcp, McpClient and a real streamable-HTTP
MCP server (tests/mocks/content_mcp_http_server.py). Faked: only the LLM loop
(a scripted agent makes one execute_mcp call through the REAL tool) and the
judge's model call.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import pytest
import uvicorn

import app.services.completion_service as completion_mod
from tests.mocks import content_mcp_http_server as content


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def content_url():
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(content.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "content MCP server did not start"
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


class _ScriptedAgent:
    """Stand-in for AgentV2: one execute_mcp call through the REAL tool.

    ``extra`` is merged into the tool's runtime context (e.g. a platform to
    make the run unattended). The ask timeout is zero (see ``env``), so an ask
    raises its confirmation and expires at once instead of waiting for a
    click; the test reads whether a confirmation was raised from the events.
    """

    call: dict = {}
    extra: dict = {}
    runs: list = []

    def __init__(self, **kw):
        self.kw = kw

    async def main_execution(self):
        from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
        from app.models.user import User
        from app.services.organization_settings_service import OrganizationSettingsService

        db = self.kw["db"]
        head = self.kw["head_completion"]
        system = self.kw["system_completion"]
        report = self.kw["report"]
        org = self.kw["organization"]
        user = await db.get(User, str(head.user_id))
        settings = await OrganizationSettingsService().get_settings(db, org, user)
        ctx = {"db": db, "user": user, "organization": org, "report": report,
               "settings": settings, "head_completion": head, "system_completion": system,
               **_ScriptedAgent.extra}
        events = [e async for e in ExecuteMCPTool().run_stream(json.loads(json.dumps(_ScriptedAgent.call)), ctx)]
        _ScriptedAgent.runs.append(events)
        system.completion = {"content": "done"}
        system.status = "success"
        await db.commit()


def _event_type(e) -> str:
    return getattr(e, "type", None) or (e.get("type") if isinstance(e, dict) else "")


class _Outcome:
    def __init__(self, events):
        self.events = events
        self.payload = events[-1].payload
        self.output = self.payload["output"]

    @property
    def ran(self) -> bool:
        return bool(self.output.get("success"))

    @property
    def prompted(self) -> bool:
        return any(_event_type(e) == "tool.confirmation" for e in self.events)

    @property
    def blocked_by(self):
        return self.output.get("blocked_by_policy")


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_data_source, create_report,
        refresh_connection_tools, enable_mcp, content_url, monkeypatch):
    from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool

    monkeypatch.setattr(completion_mod, "AgentV2", _ScriptedAgent)
    monkeypatch.setattr(ExecuteMCPTool, "_ASK_TIMEOUT_S", 0.0)  # nobody clicks in a test
    _ScriptedAgent.runs, _ScriptedAgent.extra = [], {}

    email = f"permmode_{uuid.uuid4().hex[:8]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    r = test_client.post("/api/llm/providers", headers=headers, json={
        "name": f"openai-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
        "credentials": {"api_key": "sk-test-not-used"},
        "models": [{"model_id": "gpt-6-luna", "name": "GPT-6 Luna", "is_custom": False, "is_default": True}],
    })
    assert r.status_code == 200, r.text
    enable_mcp(token, org_id)

    ds = create_data_source(name="Perm modes", type="mcp",
                            config={"server_url": content_url, "transport": "streamable_http"},
                            credentials={}, user_token=token, org_id=org_id)
    conn_id = next(c["id"] for c in ds["connections"])
    refresh_connection_tools(connection_id=conn_id, user_token=token, org_id=org_id)
    report_id = create_report(title="Perm", user_token=token, org_id=org_id, data_sources=[ds["id"]])["id"]

    class E:
        pass

    e = E()
    e.headers, e.token, e.org_id, e.ds_id, e.conn_id, e.report_id = headers, token, org_id, ds["id"], conn_id, report_id

    def tools():
        r = test_client.get(f"/api/data_sources/{ds['id']}/tools", headers=headers)
        assert r.status_code == 200, r.text
        body = r.json()
        items = body if isinstance(body, list) else body.get("tools") or body.get("items") or []
        return {t["name"]: t for t in items}

    def set_admin(tool_name, **payload):
        t = tools()[tool_name]
        r = test_client.put(f"/api/data_sources/{ds['id']}/tools/{t['id']}", json=payload, headers=headers)
        assert r.status_code == 200, r.text

    def set_mine(tool_name, policy):
        t = tools()[tool_name]
        r = test_client.put(f"/api/data_sources/{ds['id']}/tools/{t['id']}/my_policy",
                            json={"policy": policy}, headers=headers)
        assert r.status_code == 200, r.text

    def org_setting(**value):
        r = test_client.put("/api/organization/settings", headers=headers,
                            json={"config": {"tool_permission_mode": {"value": value}}})
        return r

    def set_mode(mode):
        return test_client.put(f"/api/reports/{report_id}", headers=headers, json={"permission_mode": mode})

    def call(tool_name, **arguments) -> _Outcome:
        _ScriptedAgent.call = {"connection_id": conn_id, "tool_name": tool_name, "arguments": arguments}
        before = len(_ScriptedAgent.runs)
        r = test_client.post(f"/api/reports/{report_id}/completions", headers=headers,
                             json={"prompt": {"content": f"run {tool_name}"}})
        assert r.status_code == 200, r.text
        assert len(_ScriptedAgent.runs) == before + 1, "scripted agent did not run"
        return _Outcome(_ScriptedAgent.runs[-1])

    e.tools, e.set_admin, e.set_mine, e.org_setting, e.set_mode, e.call = tools, set_admin, set_mine, org_setting, set_mode, call
    return e


def _judge(monkeypatch, *, approve: bool, confidence: float):
    """Fake the small-model judge's answer (the LLM is the boundary)."""
    from app.ai.classifiers import tool_call_judge as tcj

    calls = []

    async def fake(self, **kw):
        calls.append(kw["tool_name"])
        return tcj.ToolCallVerdict(approve=approve, confidence=confidence, reason="fake judge")

    monkeypatch.setattr(tcj.ToolCallJudge, "judge", fake)
    return calls


# ── Allow all ───────────────────────────────────────────────────────────────

def test_allow_all_needs_the_org_switch(env):
    assert env.set_mode("allow_all").status_code == 400
    assert env.org_setting(allow_all_enabled=True).status_code == 200
    r = env.set_mode("allow_all")
    assert r.status_code == 200, r.text
    assert r.json()["permission_mode"] == "allow_all"


def test_allow_all_runs_ask_tools_without_a_prompt(env):
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    out = env.call("delete_branch", branch="feature-x")  # starts on 'ask' (destructiveHint)
    assert out.ran and not out.prompted


def test_allow_all_switched_off_later_falls_back_to_ask(env):
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    env.org_setting(allow_all_enabled=False)
    out = env.call("delete_branch", branch="feature-x")
    assert out.prompted and not out.ran


@pytest.mark.parametrize("mode", ["allow_all", "auto", "ask"])
def test_no_mode_runs_a_denied_disabled_or_blocklisted_call(env, monkeypatch, mode):
    env.org_setting(allow_all_enabled=True)
    env.set_mode(mode)
    _judge(monkeypatch, approve=True, confidence=1.0)

    env.set_admin("delete_branch", policy="deny")
    out = env.call("delete_branch", branch="feature-x")
    assert not out.ran and not out.prompted and out.blocked_by == "deny"

    env.set_admin("mystery_action", is_enabled=False)
    out = env.call("mystery_action", x="1")
    assert not out.ran and not out.prompted

    # list_issues is read-only and on 'allow': only the blocklist stops it.
    out = env.call("list_issues", repo="x; DROP TABLE users")
    assert not out.ran and not out.prompted and out.blocked_by == "blocklist"


def test_allow_all_does_not_apply_when_nobody_is_watching(env):
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    _ScriptedAgent.extra = {"platform": "slack"}
    out = env.call("delete_branch", branch="feature-x")
    assert not out.ran and not out.prompted  # unattended default: refuse


# ── Auto ────────────────────────────────────────────────────────────────────

def test_auto_runs_a_read_only_tool_without_the_judge(env, monkeypatch):
    calls = _judge(monkeypatch, approve=False, confidence=1.0)
    env.set_mode("auto")
    env.set_mine("list_issues", "ask")  # the user's own ask; the admin left it on allow
    out = env.call("list_issues", repo="x")
    assert out.ran and not out.prompted
    assert calls == []


def test_auto_respects_an_admin_ask_on_a_read_only_tool(env, monkeypatch):
    calls = _judge(monkeypatch, approve=True, confidence=0.5)  # unsure
    env.set_mode("auto")
    env.set_admin("list_issues", policy="ask")
    out = env.call("list_issues", repo="x")
    assert calls == ["list_issues"]
    assert out.prompted and not out.ran


@pytest.mark.parametrize("approve,confidence,expect", [
    (True, 0.95, "ran"), (False, 0.95, "refused"), (True, 0.6, "prompted"), (False, 0.6, "prompted"),
])
def test_auto_judge_allows_refuses_or_asks(env, monkeypatch, approve, confidence, expect):
    _judge(monkeypatch, approve=approve, confidence=confidence)
    env.set_mode("auto")
    out = env.call("mystery_action", x="1")  # no hints → starts on 'ask'
    assert {"ran": out.ran, "refused": not out.ran and not out.prompted, "prompted": out.prompted}[expect]
    if expect != "ran":
        assert not out.ran


def test_auto_with_a_failing_judge_asks_never_allows(env, monkeypatch):
    from app.ai.llm import llm as llm_mod

    def boom(self, *a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_mod.LLM, "inference", boom)
    env.set_mode("auto")
    out = env.call("mystery_action", x="1")
    assert out.prompted and not out.ran


def test_agent_cap_limits_the_mode(env, test_client, monkeypatch):
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    r = test_client.put(f"/api/data_sources/{env.ds_id}", headers=env.headers, json={"max_permission_mode": "ask"})
    assert r.status_code == 200, r.text
    out = env.call("delete_branch", branch="feature-x")
    assert out.prompted and not out.ran


# ── Runs nobody is watching ─────────────────────────────────────────────────

# A run no person started (eval/scheduled/webhook/machine turn).
_MACHINE_RUN = {"is_eval_run": True}


@pytest.mark.parametrize("unattended,ran", [("refuse", False), ("settings", False), ("allow", True)])
def test_unattended_setting_decides_a_prompt(env, unattended, ran):
    assert env.org_setting(unattended=unattended).status_code == 200
    _ScriptedAgent.extra = dict(_MACHINE_RUN)
    out = env.call("delete_branch", branch="feature-x")
    assert not out.prompted
    assert out.ran is ran


def test_unattended_allow_does_not_cover_a_person_on_another_channel(env):
    # A Slack user can't answer the card, but they are a person, not a job.
    env.org_setting(unattended="allow")
    _ScriptedAgent.extra = {"platform": "slack"}
    out = env.call("delete_branch", branch="feature-x")
    assert not out.ran and not out.prompted


def test_unattended_allow_never_runs_a_blocklisted_call(env):
    env.org_setting(unattended="allow")
    _ScriptedAgent.extra = dict(_MACHINE_RUN)
    out = env.call("mystery_action", x="DROP DATABASE prod")
    assert not out.ran and out.blocked_by == "blocklist"


# ── Remember: once / this report / always ───────────────────────────────────

def _sync_engine(alembic_config):
    from sqlalchemy import create_engine

    return create_engine(alembic_config.get_main_option("sqlalchemy.url"))


def _insert_pending(alembic_config, **values):
    """A pending ask row as a run on another worker would have written it."""
    from datetime import datetime, timedelta

    from app.models.tool_confirmation import ToolConfirmation

    engine = _sync_engine(alembic_config)
    row = {
        "kind": "mcp_tool_policy", "status": ToolConfirmation.STATUS_PENDING,
        "created_at": datetime.utcnow(), "updated_at": datetime.utcnow(), "remember": False,
        "expires_at": datetime.utcnow() + timedelta(seconds=240), **values,
    }
    with engine.begin() as conn:
        conn.execute(ToolConfirmation.__table__.insert().values(**row))
    engine.dispose()


def _answer(env, test_client, alembic_config, tool_name, *, approved, scope, report_id=None):
    me = test_client.get("/api/users/whoami", headers=env.headers).json()
    completion_id, confirmation_id = str(uuid.uuid4()), str(uuid.uuid4())
    _insert_pending(
        alembic_config, id=confirmation_id, organization_id=str(env.org_id),
        report_id=report_id or env.report_id, system_completion_id=completion_id,
        user_id=str(me["id"]), connection_tool_id=env.tools()[tool_name]["id"],
        tool_name=tool_name, arguments={},
    )
    return test_client.post(
        f"/api/completions/{completion_id}/mcp_tool_confirmations/{confirmation_id}",
        json={"approved": approved, "permission_scope": scope}, headers=env.headers,
    )


def test_allow_for_this_report_is_reused_without_a_lasting_preference(env, test_client, alembic_config):
    r = _answer(env, test_client, alembic_config, "delete_branch", approved=True, scope="report")
    assert r.status_code == 200, r.text
    assert env.tools()["delete_branch"].get("user_policy") in (None, "")  # nothing saved for good
    out = env.call("delete_branch", branch="feature-x")
    assert out.ran and not out.prompted
    # Another tool on the same server is still asked about.
    assert env.call("mystery_action", x="1").prompted


def test_deny_for_this_report_refuses_without_asking(env, test_client, alembic_config):
    r = _answer(env, test_client, alembic_config, "delete_branch", approved=False, scope="report")
    assert r.status_code == 200, r.text
    out = env.call("delete_branch", branch="feature-x")
    assert not out.ran and not out.prompted


def test_answer_for_another_report_does_not_carry_over(env, test_client, alembic_config, create_report):
    other = create_report(title="Other", user_token=env.token, org_id=env.org_id, data_sources=[env.ds_id])["id"]
    r = _answer(env, test_client, alembic_config, "delete_branch", approved=True, scope="report", report_id=other)
    assert r.status_code == 200, r.text
    assert env.call("delete_branch", branch="feature-x").prompted


def test_always_writes_the_personal_preference(env, test_client, alembic_config):
    r = _answer(env, test_client, alembic_config, "delete_branch", approved=True, scope="always")
    assert r.status_code == 200, r.text
    assert env.tools()["delete_branch"]["user_policy"] == "allow"


def test_unknown_scope_is_rejected(env, test_client, alembic_config):
    r = _answer(env, test_client, alembic_config, "delete_branch", approved=True, scope="forever")
    assert r.status_code == 400


# ── Org setting validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    {"default_mode": "allow_all"},              # per report only, never the default
    {"default_mode": "allow_all", "allow_all_enabled": True},
    {"allow_threshold": 0.3, "refuse_threshold": 0.1},  # Auto would allow likely-unsafe calls
    {"default_mode": "yolo"},
    {"unattended": "maybe"},
    {"allow_threshold": 2},
    {"allow_threshold": 0.2, "refuse_threshold": 0.5},
    {"surprise": True},
])
def test_bad_org_setting_is_rejected(env, value):
    assert env.org_setting(**value).status_code == 400


def test_auto_can_be_the_org_default(env, monkeypatch):
    _judge(monkeypatch, approve=True, confidence=0.99)
    assert env.org_setting(default_mode="auto").status_code == 200
    out = env.call("mystery_action", x="1")  # report has no mode of its own
    assert out.ran and not out.prompted
