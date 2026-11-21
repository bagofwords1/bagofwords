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


def _serve_content():
    """The content MCP server on a free port of 127.0.0.1 (never "localhost":
    it may resolve to ::1 first, and egress pins the first address)."""
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


@pytest.fixture(scope="module")
def content_url():
    yield from _serve_content()


@pytest.fixture(scope="module")
def second_content_url():
    """A second server, so a second agent gets a connection of its own."""
    yield from _serve_content()


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
    me = whoami(token)
    org_id = me["organizations"][0]["id"]
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
    e.user_id = str(me["id"])

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

    def call_as(request_headers, tool_name, **arguments) -> _Outcome:
        _ScriptedAgent.call = {"connection_id": conn_id, "tool_name": tool_name, "arguments": arguments}
        before = len(_ScriptedAgent.runs)
        r = test_client.post(f"/api/reports/{report_id}/completions", headers=request_headers,
                             json={"prompt": {"content": f"run {tool_name}"}})
        assert r.status_code == 200, r.text
        assert len(_ScriptedAgent.runs) == before + 1, "scripted agent did not run"
        return _Outcome(_ScriptedAgent.runs[-1])

    def call(tool_name, **arguments) -> _Outcome:
        return call_as(headers, tool_name, **arguments)

    e.tools, e.set_admin, e.set_mine, e.org_setting, e.set_mode, e.call = tools, set_admin, set_mine, org_setting, set_mode, call
    e.call_as = call_as
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
    out = env.call("list_issues", repo="DROP TABLE users")
    assert not out.ran and not out.prompted and out.blocked_by == "blocklist"


@pytest.mark.parametrize("mode", ["allow_all", "auto", "ask"])
def test_a_tool_missing_from_the_catalog_is_refused_before_anything_else(env, monkeypatch, mode):
    from app.data_sources.clients.mcp_client import McpClient

    env.org_setting(allow_all_enabled=True)
    env.set_mode(mode)
    _judge(monkeypatch, approve=True, confidence=1.0)
    sent = []
    real_call = McpClient.acall_tool

    async def spy(self, tool_name, *args, **kwargs):  # records and delegates; behaviour unchanged
        sent.append(tool_name)
        return await real_call(self, tool_name, *args, **kwargs)

    monkeypatch.setattr(McpClient, "acall_tool", spy)

    # The blocklist would also refuse this argument: the refusal comes first.
    out = env.call("no_such_tool", x="DROP TABLE users")
    assert not out.ran and not out.prompted
    assert out.blocked_by == "unknown_tool"
    assert "Refresh the tool list" in out.output["error_message"]
    assert sent == []

    assert env.call("list_issues", repo="x").ran  # control: the spy sees a real call
    assert sent == ["list_issues"]


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


def test_a_failed_report_mode_read_falls_back_to_ask_and_the_run_goes_on(env, test_client):
    # The database rejects the read of the report's mode (a fault injected at
    # the driver: the statement is sent with a bad column, so Postgres aborts
    # the transaction). The mode falls back to Ask, and the reads after it
    # (the agents' caps) still work: the call is asked about, not failed.
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")

    def break_mode_read(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT REPORTS.PERMISSION_MODE"):
            statement = statement.replace("reports.permission_mode", "reports.no_such_column", 1)
        return statement, parameters

    event.listen(Engine, "before_cursor_execute", break_mode_read, retval=True)
    try:
        out = env.call("delete_branch", branch="feature-x")
    finally:
        event.remove(Engine, "before_cursor_execute", break_mode_read)
    assert out.prompted and not out.ran


def test_a_failed_agent_cap_read_falls_back_to_ask_and_the_run_goes_on(env, test_client):
    # As above, for the read of the run agents' caps: the mode falls back to
    # Ask (the strictest cap), and the call is asked about, not failed.
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")

    def break_cap_read(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT DATA_SOURCES.MAX_PERMISSION_MODE"):
            statement = statement.replace("data_sources.max_permission_mode", "data_sources.no_such_column", 1)
        return statement, parameters

    event.listen(Engine, "before_cursor_execute", break_cap_read, retval=True)
    try:
        out = env.call("delete_branch", branch="feature-x")
    finally:
        event.remove(Engine, "before_cursor_execute", break_cap_read)
    assert out.prompted and not out.ran


def test_the_report_shows_its_agents_caps(env, test_client):
    # The prompt box reads them for the banner and the picker.
    url = f"/api/reports/{env.report_id}"
    assert [ds["max_permission_mode"] for ds in test_client.get(url, headers=env.headers).json()["data_sources"]] \
        == [None]
    r = test_client.put(f"/api/data_sources/{env.ds_id}", headers=env.headers, json={"max_permission_mode": "auto"})
    assert r.status_code == 200, r.text
    assert [ds["max_permission_mode"] for ds in test_client.get(url, headers=env.headers).json()["data_sources"]] \
        == ["auto"]
    # The conversations list carries the same agents: its cards say the same.
    cards = test_client.get("/api/reports", headers=env.headers, params={"filter": "my"}).json()["reports"]
    card = next(c for c in cards if c["id"] == env.report_id)
    assert [ds["max_permission_mode"] for ds in card["data_sources"]] == ["auto"]


def test_agent_cap_can_be_cleared(env, test_client):
    # The agent settings page saves "no cap" as "none".
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    url = f"/api/data_sources/{env.ds_id}"
    assert test_client.put(url, headers=env.headers, json={"max_permission_mode": "ask"}).status_code == 200
    r = test_client.put(url, headers=env.headers, json={"max_permission_mode": "none"})
    assert r.status_code == 200, r.text
    assert test_client.get(url, headers=env.headers).json()["max_permission_mode"] is None
    out = env.call("delete_branch", branch="feature-x")
    assert out.ran and not out.prompted  # no longer capped


def test_member_cannot_change_the_agent_cap(env, test_client, create_user, login_user):
    url = f"/api/data_sources/{env.ds_id}"
    r = test_client.put(url, headers=env.headers, json={"max_permission_mode": "ask"})
    assert r.status_code == 200, r.text
    r = test_client.put(url, headers=env.headers, json={"is_public": True})  # members can see it
    assert r.status_code == 200, r.text
    assert test_client.get(url, headers=env.headers).json()["max_permission_mode"] == "ask"
    member, _ = _add_member(env, test_client, create_user, login_user)
    assert test_client.get(url, headers=member).status_code == 200  # the member can see the agent
    r = test_client.put(url, headers=member, json={"max_permission_mode": "none"})
    assert r.status_code == 403, r.text
    assert test_client.get(url, headers=env.headers).json()["max_permission_mode"] == "ask"


@pytest.mark.parametrize("agents_from", ["report", "run"])
def test_a_cap_on_any_agent_in_the_run_limits_every_tool(env, test_client, create_data_source, second_content_url,
                                                          agents_from):
    # A second agent with its own connection caps the mode at Ask. The tool
    # belongs to the first agent, which has no cap: the run's strictest cap
    # still applies to it.
    capped = create_data_source(
        name="Capped agent", type="mcp",
        config={"server_url": second_content_url, "transport": "streamable_http"},
        credentials={}, user_token=env.token, org_id=env.org_id)
    assert env.conn_id not in {c["id"] for c in capped["connections"]}
    r = test_client.put(f"/api/reports/{env.report_id}", headers=env.headers,
                        json={"data_sources": [env.ds_id, capped["id"]]})
    assert r.status_code == 200, r.text
    if agents_from == "run":
        _ScriptedAgent.extra = {"run_agent_ids": [env.ds_id, capped["id"]]}
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    out = env.call("delete_branch", branch="feature-x")
    assert out.ran and not out.prompted  # control: no cap yet

    r = test_client.put(f"/api/data_sources/{capped['id']}", headers=env.headers, json={"max_permission_mode": "ask"})
    assert r.status_code == 200, r.text
    out = env.call("delete_branch", branch="feature-x")
    assert out.prompted and not out.ran


def test_a_parameter_its_schema_calls_sql_gets_the_sql_rules(env, alembic_config):
    from app.models.connection_tool import ConnectionTool

    # list_issues is read-only and on 'allow'; to the key rule its 'repo' text is not SQL.
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    assert env.call("list_issues", repo="DELETE FROM orders").ran  # control

    # The server describes the parameter as SQL (as a refreshed catalog would store it).
    table = ConnectionTool.__table__
    engine = _sync_engine(alembic_config)
    with engine.begin() as conn:
        conn.execute(table.update().where(table.c.id == env.tools()["list_issues"]["id"]).values(input_schema={
            "type": "object", "required": ["repo"],
            "properties": {"repo": {"type": "string", "description": "The SQL query to run"}},
        }))
    engine.dispose()
    out = env.call("list_issues", repo="DELETE FROM orders")
    assert not out.ran and not out.prompted and out.blocked_by == "blocklist"


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


@pytest.mark.parametrize("approve", [True, False])
def test_an_unsure_auto_judge_is_asked_once_in_an_unattended_run(env, monkeypatch, approve):
    # Auto in a run nobody watches, unattended 'settings', a tool on 'auto':
    # the judge's unsure answer decides as the per-tool judge would, without a
    # second judge call.
    import app.ai.tools.implementations.execute_mcp as execute_mcp_mod
    from app.services import tool_permission_mode as pm

    calls = _judge(monkeypatch, approve=approve, confidence=0.6)
    audits = []
    real_audit = execute_mcp_mod.log_tool_audit

    async def audit_spy(runtime_ctx, action, *args, **kwargs):
        audits.append((action, kwargs.get("details") or {}))
        return await real_audit(runtime_ctx, action, *args, **kwargs)

    monkeypatch.setattr(execute_mcp_mod, "log_tool_audit", audit_spy)
    assert env.org_setting(unattended="settings", default_mode="auto").status_code == 200
    env.set_admin("mystery_action", policy="auto")
    _ScriptedAgent.extra = dict(_MACHINE_RUN)
    out = env.call("mystery_action", x="1")
    assert calls == ["mystery_action"]
    assert not out.prompted
    assert out.ran is approve
    # The audit says why the judge decided, as the per-tool judge's entry does.
    decisions = [d for a, d in audits if a == "tool.auto_policy_decision"]
    assert [(d["decided_by"], d.get("asked_because")) for d in decisions] == [(pm.BY_SMALL_MODEL, pm.BY_UNATTENDED)]


def test_unattended_allow_never_runs_a_blocklisted_call(env):
    env.org_setting(unattended="allow")
    _ScriptedAgent.extra = dict(_MACHINE_RUN)
    out = env.call("mystery_action", x="DROP DATABASE prod")
    assert not out.ran and out.blocked_by == "blocklist"


# ── Machine turns in the report (the real run_machine_turn path) ────────────

def _machine_turn(env, trigger_source, tool_name="delete_branch", **arguments) -> _Outcome:
    """A machine turn on the env report, as a wait wake, check-in or
    sub-agent starts one: run_machine_turn → create_completion → the agent."""
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.machine_turn import run_machine_turn

    _ScriptedAgent.call = {"connection_id": env.conn_id, "tool_name": tool_name,
                           "arguments": arguments or {"branch": "feature-x"}}
    before = len(_ScriptedAgent.runs)

    async def _run():
        async with async_session_maker() as db:
            await run_machine_turn(
                db, report=await db.get(Report, env.report_id), user=await db.get(User, env.user_id),
                organization=await db.get(Organization, env.org_id), summary="machine turn",
                trigger_source=trigger_source, message_type=f"{trigger_source}_event", instruction="resume")

    asyncio.run(_run())
    assert len(_ScriptedAgent.runs) == before + 1, f"the {trigger_source} turn did not run"
    return _Outcome(_ScriptedAgent.runs[-1])


@pytest.mark.parametrize("trigger", ["wait", "checkin", "sub_agent_message", "sub_agent_wave"])
def test_a_machine_turn_shown_in_the_report_prompts_like_a_user_turn(env, trigger):
    # As on main: the card shows in the report and the owner can answer it.
    out = _machine_turn(env, trigger)
    assert out.prompted and not out.ran


@pytest.mark.parametrize("trigger", ["wait", "sub_agent"])
def test_allow_all_and_report_grants_apply_to_machine_turns_in_the_report(env, test_client, alembic_config,
                                                                         trigger):
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    out = _machine_turn(env, trigger)
    assert out.ran and not out.prompted
    env.set_mode("ask")
    r = _answer(env, test_client, alembic_config, "mystery_action", approved=True, scope="report")
    assert r.status_code == 200, r.text
    out = _machine_turn(env, trigger, "mystery_action", x="1")
    assert out.ran and not out.prompted


@pytest.mark.parametrize("unattended,ran", [("refuse", False), ("allow", True)])
def test_a_hidden_sub_agent_turn_that_would_prompt_follows_the_unattended_setting(env, unattended, ran):
    # Nobody sees a sub-agent's card, so a prompt there is never raised.
    assert env.org_setting(unattended=unattended).status_code == 200
    out = _machine_turn(env, "sub_agent")
    assert not out.prompted
    assert out.ran is ran


def test_an_eval_turn_gets_no_report_grants(env, test_client, alembic_config):
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    out = _machine_turn(env, "eval_run")
    assert not out.ran and not out.prompted


# ── Scheduled-prompt and webhook turns (the real paths) ─────────────────────

def _fire_scheduled(env, test_client):
    """A scheduled prompt on the env report, fired as "Run now" does."""
    import asyncio

    from app.services.scheduled_prompt_service import scheduled_prompt_service

    r = test_client.post(f"/api/reports/{env.report_id}/scheduled-prompts", headers=env.headers,
                         json={"prompt": {"content": "run delete_branch"}, "cron_schedule": "0 9 * * *"})
    assert r.status_code == 200, r.text
    asyncio.run(scheduled_prompt_service.scheduled_run_prompt(r.json()["id"], force=True))


def _fire_webhook(env, test_client):
    """A standalone trigger on the env agent, and one delivery to it."""
    import asyncio

    from app.services.webhook_service import webhook_service

    r = test_client.post("/api/triggers", headers=env.headers, json={
        "name": "Alert", "source": "generic", "auth_mode": "token", "classify_enabled": False,
        "task_template": "run delete_branch", "mode": "chat", "data_source_ids": [env.ds_id],
    })
    assert r.status_code == 200, r.text
    asyncio.run(webhook_service.process_delivery(
        r.json()["id"], {"alert": "x"}, {"x-bow-delivery": uuid.uuid4().hex}))


_FIRE = {"scheduled": _fire_scheduled, "webhook": _fire_webhook}


def _unattended_turn(env, test_client, path) -> _Outcome:
    _ScriptedAgent.call = {"connection_id": env.conn_id, "tool_name": "delete_branch",
                           "arguments": {"branch": "feature-x"}}
    before = len(_ScriptedAgent.runs)
    _FIRE[path](env, test_client)
    assert len(_ScriptedAgent.runs) == before + 1, f"the {path} turn did not run"
    return _Outcome(_ScriptedAgent.runs[-1])


@pytest.mark.parametrize("path", ["scheduled", "webhook"])
@pytest.mark.parametrize("unattended,ran", [("refuse", False), ("settings", False), ("allow", True)])
def test_scheduled_and_webhook_turns_follow_the_unattended_setting(env, test_client, path, unattended, ran):
    assert env.org_setting(unattended=unattended).status_code == 200
    out = _unattended_turn(env, test_client, path)
    assert not out.prompted
    assert out.ran is ran


@pytest.mark.parametrize("path", ["scheduled", "webhook"])
def test_allow_all_does_not_apply_to_scheduled_or_webhook_turns(env, test_client, path):
    # The webhook spawns its own report, which starts from the org default.
    assert env.org_setting(allow_all_enabled=True, default_mode="allow_all", unattended="refuse").status_code == 200
    env.set_mode("allow_all")
    out = _unattended_turn(env, test_client, path)
    assert not out.ran and not out.prompted


# ── Runs started with an API key ────────────────────────────────────────────

def _api_key_headers(env, create_api_key, sent_as="header"):
    key = create_api_key(env.token, env.org_id, name=f"perm-{uuid.uuid4().hex[:6]}")["key"]
    credential = {"X-API-Key": key} if sent_as == "header" else {"Authorization": f"Bearer {key}"}
    return {**credential, "X-Organization-Id": str(env.org_id)}


# Both ways an API key can be sent are the same kind of run.
@pytest.mark.parametrize("sent_as", ["header", "bearer"])
def test_allow_all_does_not_apply_to_an_api_run(env, create_api_key, sent_as):
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    out = env.call_as(_api_key_headers(env, create_api_key, sent_as), "delete_branch", branch="feature-x")
    assert not out.ran and not out.prompted


@pytest.mark.parametrize("unattended,ran", [("refuse", False), ("settings", False), ("allow", True)])
def test_an_api_run_follows_the_unattended_setting(env, create_api_key, unattended, ran):
    assert env.org_setting(unattended=unattended).status_code == 200
    out = env.call_as(_api_key_headers(env, create_api_key), "delete_branch", branch="feature-x")
    assert not out.prompted
    assert out.ran is ran


def _drive_dispatcher(report_id):
    """Start the next queued prompt the way a finishing run does, and wait for
    the dispatched run. The sync TestClient kills tasks a route spawns, so the
    queue is driven from the test (as test_completion_queue_steer.py does).
    This runs in the test thread, outside any request context."""
    import asyncio

    from app.services.completion_service import CompletionService

    async def _run():
        await CompletionService().start_next_queued_if_idle(report_id)
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and getattr(t.get_coro(), "__qualname__", "").endswith("CompletionService._run_dispatched_agent")
        ]
        if pending:
            await asyncio.wait(pending, timeout=30)

    asyncio.run(_run())


@pytest.fixture
def live_client():
    """A client whose event loop lives for the whole test, as the server's
    does: a task a request starts runs to its end instead of being cut off
    when the response returns."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield client


def _settle_queue_work(portal, timeout=30.0):
    """Wait until no dispatcher or dispatched-run task is left on the live
    loop (as test_completion_queue_steer.py does)."""
    import asyncio

    names = ("CompletionService.start_next_queued_if_idle", "CompletionService._run_dispatched_agent")

    async def _wait():
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task() and not t.done()
                       and getattr(t.get_coro(), "__qualname__", "").endswith(names)]
            if not pending:
                return
            await asyncio.wait(pending, timeout=deadline - asyncio.get_running_loop().time())
        raise AssertionError("queue work did not settle")

    portal.call(_wait)


@pytest.mark.parametrize("credential", ["api_key", "jwt"])
def test_a_queued_api_prompt_stays_unattended_when_dispatched_later(env, test_client, live_client, alembic_config,
                                                                   create_api_key, credential):
    from datetime import datetime

    from sqlalchemy import select

    from app.models.completion import Completion

    env.org_setting(allow_all_enabled=True, unattended="refuse")
    env.set_mode("allow_all")
    _ScriptedAgent.call = {"connection_id": env.conn_id, "tool_name": "delete_branch",
                           "arguments": {"branch": "feature-x"}}
    me = test_client.get("/api/users/whoami", headers=env.headers).json()

    # Hold the queue with a live run. The scripted harness cannot keep a run
    # open through the API, so insert the row a running turn would have (as
    # test_completion_queue_steer.py does); a fresh updated_at counts as live.
    engine = _sync_engine(alembic_config)
    blocking_id = str(uuid.uuid4())
    now = datetime.utcnow()
    with engine.begin() as conn:
        conn.execute(Completion.__table__.insert().values(
            id=blocking_id, report_id=env.report_id, user_id=str(me["id"]), role="system",
            status="in_progress", model="gpt-6-luna", prompt={}, completion={"content": ""},
            turn_index=0, created_at=now, updated_at=now,
        ))

    headers = _api_key_headers(env, create_api_key) if credential == "api_key" else env.headers
    before = len(_ScriptedAgent.runs)
    # Queueing starts a dispatcher of its own (in case nothing was running),
    # and it takes the report's write lock. On the per-request loop of
    # test_client it can be cut off holding that lock, and the rows written
    # below then wait on a lock nobody releases. On a live loop it sees the
    # live run and ends its transaction.
    r = live_client.post(f"/api/reports/{env.report_id}/completions", headers=headers,
                         json={"prompt": {"content": "run delete_branch"}, "queue": True})
    assert r.status_code == 200, r.text
    _settle_queue_work(live_client.portal)
    head = r.json()["completions"][0]
    assert head["status"] == "queued"
    assert len(_ScriptedAgent.runs) == before

    with engine.begin() as conn:
        started_via = conn.execute(
            select(Completion.__table__.c.started_via).where(Completion.__table__.c.id == head["id"])).scalar_one()
        assert started_via == ("api_key" if credential == "api_key" else None)
        conn.execute(Completion.__table__.update().where(Completion.__table__.c.id == blocking_id)
                     .values(status="success", updated_at=datetime.utcnow()))
    engine.dispose()

    _drive_dispatcher(env.report_id)
    assert len(_ScriptedAgent.runs) == before + 1, "the queued prompt was not dispatched"
    out = _Outcome(_ScriptedAgent.runs[-1])
    if credential == "api_key":
        assert not out.ran and not out.prompted  # unattended: Allow all does not apply
    else:
        assert out.ran and not out.prompted  # control: a watched owner run under Allow all


# ── Machine turns started by an unattended run ──────────────────────────────
# A scheduled, webhook or API-key run that delegates, waits or plans a
# check-in starts more turns later. Their head rows carry the origin
# (started_via), so they stay unattended: real create_completion,
# SubAgentService._start_child and run_wait_wake paths.

def _origin(env, test_client, alembic_config, create_api_key, path) -> dict:
    """Run one turn started by ``path`` and return the ids of its report, head
    and system rows: the turn a delegate, wait or check-in comes from."""
    _ScriptedAgent.call = {"connection_id": env.conn_id, "tool_name": "delete_branch",
                           "arguments": {"branch": "feature-x"}}
    if path in _FIRE:
        _FIRE[path](env, test_client)
    elif path == "api_key":
        env.call_as(_api_key_headers(env, create_api_key), "delete_branch", branch="feature-x")
    else:
        env.call("delete_branch", branch="feature-x")
    return _latest_turn(env, alembic_config)


def _latest_turn(env, alembic_config) -> dict:
    """The ids of the report, head and system rows of the user's latest turn."""
    from sqlalchemy import select

    from app.models.completion import Completion

    c = Completion.__table__.c
    engine = _sync_engine(alembic_config)
    with engine.connect() as conn:
        head = conn.execute(select(c.id, c.report_id).where(c.user_id == env.user_id, c.role == "user")
                            .order_by(c.created_at.desc()).limit(1)).one()
        system_id = conn.execute(select(c.id).where(c.parent_id == head.id, c.role == "system")
                                 .order_by(c.created_at.desc()).limit(1)).scalar_one()
    engine.dispose()
    return {"report_id": str(head.report_id), "head_id": str(head.id), "system_id": str(system_id)}


async def _settle():
    """Wait for the runs a machine turn started in the background."""
    import asyncio

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.wait(pending, timeout=30)


def _delegate(env, origin) -> _Outcome:
    """One sub-agent of the origin turn, started as the wave launcher does."""
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.sub_agent import RUN_PENDING, WAVE_RUNNING, SubAgentRun, SubAgentWave
    from app.services.sub_agent_service import SubAgentService

    _ScriptedAgent.call = {"connection_id": env.conn_id, "tool_name": "delete_branch",
                           "arguments": {"branch": "feature-x"}}
    before = len(_ScriptedAgent.runs)

    async def _run():
        async with async_session_maker() as db:
            wave = SubAgentWave(organization_id=str(env.org_id), report_id=origin["report_id"], user_id=env.user_id,
                                parent_completion_id=origin["system_id"], status=WAVE_RUNNING, remaining=1, total=1)
            db.add(wave)
            await db.flush()
            row = SubAgentRun(wave_id=str(wave.id), organization_id=str(env.org_id), report_id=origin["report_id"],
                              wave_index=0, parent_completion_id=origin["system_id"], title="Check", profile="investigate",
                              goal="Delete the branch.", status=RUN_PENDING)
            db.add(row)
            await db.commit()
            run_id = str(row.id)
        await SubAgentService()._start_child(run_id)
        await _settle()

    asyncio.run(_run())
    assert len(_ScriptedAgent.runs) == before + 1, "the sub-agent did not run"
    return _Outcome(_ScriptedAgent.runs[-1])


def _wake(env, origin) -> _Outcome:
    """The wait the origin turn armed, fired as APScheduler fires it."""
    import asyncio
    from unittest.mock import patch

    import app.services.wait_service as ws

    _ScriptedAgent.call = {"connection_id": env.conn_id, "tool_name": "delete_branch",
                           "arguments": {"branch": "feature-x"}}
    before = len(_ScriptedAgent.runs)

    async def _run():
        # The cross-worker claim is the job store's; one worker here.
        with patch.object(ws, "claim_scheduled_run", lambda job_id: True):
            await ws.run_wait_wake(
                job_id=f"wait:{origin['report_id']}:{uuid.uuid4().hex[:8]}", report_id=origin["report_id"],
                user_id=env.user_id, organization_id=str(env.org_id), reason="check again",
                system_completion_id=origin["system_id"])

    asyncio.run(_run())
    assert len(_ScriptedAgent.runs) == before + 1, "the wait wake did not run"
    return _Outcome(_ScriptedAgent.runs[-1])


def _latest_head_started_via(env, alembic_config):
    from sqlalchemy import select

    from app.models.completion import Completion

    c = Completion.__table__.c
    engine = _sync_engine(alembic_config)
    with engine.connect() as conn:
        value = conn.execute(select(c.started_via).where(c.user_id == env.user_id, c.role == "user")
                             .order_by(c.created_at.desc()).limit(1)).scalar_one()
    engine.dispose()
    return value


@pytest.mark.parametrize("grant", ["allow_all", "report_answer"])
@pytest.mark.parametrize("unattended,ran", [("refuse", False), ("allow", True)])
def test_a_scheduled_run_that_delegates_gets_no_report_grants_in_its_child(
        env, test_client, alembic_config, create_api_key, grant, unattended, ran):
    assert env.org_setting(allow_all_enabled=True, unattended=unattended).status_code == 200
    if grant == "allow_all":
        env.set_mode("allow_all")
    else:
        r = _answer(env, test_client, alembic_config, "delete_branch", approved=True, scope="report")
        assert r.status_code == 200, r.text
    origin = _origin(env, test_client, alembic_config, create_api_key, "scheduled")
    out = _delegate(env, origin)
    assert not out.prompted
    assert out.ran is ran  # the unattended setting decides, not the report's grants
    assert _latest_head_started_via(env, alembic_config) == "unattended"


def test_a_watched_run_that_delegates_keeps_the_reports_grants_in_its_child(env, test_client, alembic_config,
                                                                          create_api_key):
    # Control: the child of a turn the owner typed runs under Allow all.
    assert env.org_setting(allow_all_enabled=True, unattended="refuse").status_code == 200
    env.set_mode("allow_all")
    origin = _origin(env, test_client, alembic_config, create_api_key, "watched")
    out = _delegate(env, origin)
    assert _latest_head_started_via(env, alembic_config) is None
    assert out.ran and not out.prompted


@pytest.mark.parametrize("path,stamp", [("api_key", "api_key"), ("webhook", "unattended")])
@pytest.mark.parametrize("unattended,ran", [("refuse", False), ("allow", True)])
def test_the_wait_wake_of_an_unattended_run_follows_the_unattended_setting(
        env, test_client, alembic_config, create_api_key, path, stamp, unattended, ran):
    assert env.org_setting(allow_all_enabled=True, default_mode="allow_all", unattended=unattended).status_code == 200
    env.set_mode("allow_all")
    origin = _origin(env, test_client, alembic_config, create_api_key, path)
    out = _wake(env, origin)
    assert not out.prompted
    assert out.ran is ran
    assert _latest_head_started_via(env, alembic_config) == stamp


def test_the_wait_wake_of_a_watched_run_still_prompts(env, test_client, alembic_config, create_api_key):
    # Control: the owner typed the turn that waited, so its wake shows the card.
    assert env.org_setting(unattended="allow").status_code == 200
    origin = _origin(env, test_client, alembic_config, create_api_key, "watched")
    out = _wake(env, origin)
    assert _latest_head_started_via(env, alembic_config) is None
    assert out.prompted and not out.ran


# ── Turns started by a turn from another channel ────────────────────────────
# A person on Slack, Teams or email cannot answer a card and gets none of the
# report's grants. The turns their turn starts later (a wait wake, a
# sub-agent) are the same kind of run: the head row carries a 'channel' mark.

def _channel_origin(env, alembic_config) -> dict:
    """One turn a person sent from Slack, started as the channel handler
    starts it (create_completion with the channel's fields); its row ids."""
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User
    from app.schemas.completion_v2_schema import CompletionCreate, PromptSchema
    from app.services.completion_service import CompletionService

    _ScriptedAgent.call = {"connection_id": env.conn_id, "tool_name": "delete_branch",
                           "arguments": {"branch": "feature-x"}}
    before = len(_ScriptedAgent.runs)

    async def _run():
        async with async_session_maker() as db:
            await CompletionService().create_completion(
                db=db, report_id=env.report_id,
                completion_data=CompletionCreate(prompt=PromptSchema(content="run delete_branch")),
                current_user=await db.get(User, env.user_id), organization=await db.get(Organization, env.org_id),
                background=False, external_user_id="U1", external_platform="slack", external_thread_ts="1.0",
                external_message_ts="1.0", external_channel_id="C1", external_channel_type="im")

    asyncio.run(_run())
    assert len(_ScriptedAgent.runs) == before + 1, "the channel turn did not run"
    return _latest_turn(env, alembic_config)


@pytest.mark.parametrize("unattended", ["refuse", "allow"])
@pytest.mark.parametrize("start", ["wake", "delegate"])
def test_a_turn_started_by_a_channel_turn_gets_no_report_grants(env, test_client, alembic_config, start, unattended):
    assert env.org_setting(allow_all_enabled=True, default_mode="allow_all", unattended=unattended).status_code == 200
    env.set_mode("allow_all")
    r = _answer(env, test_client, alembic_config, "delete_branch", approved=True, scope="report")
    assert r.status_code == 200, r.text
    origin = _channel_origin(env, alembic_config)
    out = (_wake if start == "wake" else _delegate)(env, origin)
    # Neither Allow all, nor the owner's "for this report" answer, nor the
    # unattended 'allow' (it covers runs no person started) lets it run.
    assert not out.ran and not out.prompted
    assert _latest_head_started_via(env, alembic_config) == "channel"


def test_a_turn_started_by_a_turn_whose_row_is_gone_is_unattended(env, test_client, alembic_config):
    # The wake names the turn that armed it; with that row missing nobody can
    # say a person watched it, so the wake fails closed.
    assert env.org_setting(allow_all_enabled=True, unattended="refuse").status_code == 200
    env.set_mode("allow_all")
    out = _wake(env, {"report_id": env.report_id, "system_id": str(uuid.uuid4())})
    assert not out.ran and not out.prompted
    assert _latest_head_started_via(env, alembic_config) == "unattended"


@pytest.mark.parametrize("path,stamp", [("scheduled", "unattended"), ("watched", None)])
def test_a_sub_agents_question_wakes_its_parent_with_the_origins_mark(env, test_client, alembic_config,
                                                                     create_api_key, path, stamp):
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.sub_agent import RUN_RUNNING, WAVE_RUNNING, SubAgentRun, SubAgentWave
    from app.services.sub_agent_message_service import sub_agent_message_service

    assert env.org_setting(allow_all_enabled=True, unattended="refuse").status_code == 200
    env.set_mode("allow_all")
    origin = _origin(env, test_client, alembic_config, create_api_key, path)

    async def _ask():
        # A live child of the origin turn asks its parent a question: the
        # service queues the parent's reply turn in the report.
        async with async_session_maker() as db:
            wave = SubAgentWave(organization_id=str(env.org_id), report_id=origin["report_id"], user_id=env.user_id,
                                parent_completion_id=origin["system_id"], status=WAVE_RUNNING, remaining=1, total=1)
            db.add(wave)
            await db.flush()
            child = SubAgentRun(wave_id=str(wave.id), organization_id=str(env.org_id), report_id=origin["report_id"],
                                wave_index=0, parent_completion_id=origin["system_id"], title="Check",
                                profile="investigate", goal="Delete the branch.", status=RUN_RUNNING)
            db.add(child)
            await db.commit()
            await sub_agent_message_service.send(
                db, organization_id=str(env.org_id), report_id=origin["report_id"], sender_sub_agent_id=str(child.id),
                recipient="parent", kind="question", body="Which branch?", idempotency_key=uuid.uuid4().hex)

    asyncio.run(_ask())
    assert _latest_head_started_via(env, alembic_config) == stamp
    before = len(_ScriptedAgent.runs)
    _drive_dispatcher(origin["report_id"])
    assert len(_ScriptedAgent.runs) == before + 1, "the parent's reply turn did not run"
    out = _Outcome(_ScriptedAgent.runs[-1])
    assert not out.prompted
    assert out.ran is (stamp is None)  # Allow all applies only to the watched origin's reply


def _event_status(env, alembic_config, trigger):
    from sqlalchemy import select

    from app.models.completion import Completion

    c = Completion.__table__.c
    engine = _sync_engine(alembic_config)
    with engine.connect() as conn:
        status = conn.execute(select(c.status).where(c.report_id == env.report_id, c.role == "external",
                                                     c.trigger_source == trigger)
                              .order_by(c.created_at.desc()).limit(1)).scalar_one()
    engine.dispose()
    return status


def _origin_read_fails(monkeypatch):
    # The origin's row is read from the session (already loaded there), so the
    # fault is injected at the read itself.
    import app.services.machine_turn as machine_turn

    async def failing_read(session, completion_id):
        raise RuntimeError("origin read failed")

    monkeypatch.setattr(machine_turn, "unattended_origin", failing_read)


def test_a_wake_whose_origin_cannot_be_read_ends_as_a_failed_event(env, test_client, alembic_config, create_api_key,
                                                                    monkeypatch):
    origin = _origin(env, test_client, alembic_config, create_api_key, "watched")
    _origin_read_fails(monkeypatch)
    before = len(_ScriptedAgent.runs)
    with pytest.raises(AssertionError, match="the wait wake did not run"):
        _wake(env, origin)
    assert len(_ScriptedAgent.runs) == before
    # The visible event does not stay "in progress" forever.
    assert _event_status(env, alembic_config, "wait") == "error"


def test_a_sub_agent_whose_origin_cannot_be_read_ends_as_failed(env, test_client, alembic_config, create_api_key,
                                                                 monkeypatch):
    from sqlalchemy import select

    from app.models.sub_agent import RUN_FAILED, SubAgentRun

    origin = _origin(env, test_client, alembic_config, create_api_key, "watched")
    _origin_read_fails(monkeypatch)
    with pytest.raises(AssertionError, match="the sub-agent did not run"):
        _delegate(env, origin)
    engine = _sync_engine(alembic_config)
    with engine.connect() as conn:
        status = conn.execute(select(SubAgentRun.__table__.c.status)
                              .where(SubAgentRun.__table__.c.report_id == origin["report_id"])).scalar_one()
    engine.dispose()
    assert status == RUN_FAILED


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
    assert r.json()["error_code"] == "shell_confirmation.invalid_scope"


@pytest.mark.parametrize("scope", [["report"], {"scope": "once"}, 3])
def test_a_scope_that_is_not_text_is_a_typed_error(env, test_client, alembic_config, scope):
    r = _answer(env, test_client, alembic_config, "delete_branch", approved=True, scope=scope)
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == "shell_confirmation.invalid_scope"


def test_for_this_report_without_a_report_is_rejected(env, test_client, alembic_config):
    completion_id, confirmation_id = str(uuid.uuid4()), str(uuid.uuid4())
    _insert_pending(
        alembic_config, id=confirmation_id, organization_id=str(env.org_id), report_id=None,
        system_completion_id=completion_id, user_id=env.user_id,
        connection_tool_id=env.tools()["delete_branch"]["id"], tool_name="delete_branch", arguments={},
    )
    r = test_client.post(f"/api/completions/{completion_id}/mcp_tool_confirmations/{confirmation_id}",
                         json={"approved": True, "permission_scope": "report"}, headers=env.headers)
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == "shell_confirmation.report_required"


@pytest.mark.parametrize("value,code", [
    ({"default_mode": "allow_all"}, "tool_permission_mode.allow_all_default_needs_switch"),
    ({"judge_decision_model_id": "no-such-model"}, "tool_permission_mode.judge_model_unavailable"),
])
def test_settings_page_errors_are_typed(env, value, code):
    r = env.org_setting(**value)
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == code


def test_picking_allow_all_while_it_is_off_is_a_typed_error(env):
    r = env.set_mode("allow_all")
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == "tool_permission_mode.allow_all_off"


# ── Org setting validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    {"default_mode": "allow_all"},              # only while the Allow all switch is on
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


# ── Allow all as the org default ────────────────────────────────────────────

def test_allow_all_can_be_the_org_default_while_its_switch_is_on(env):
    r = env.org_setting(allow_all_enabled=True, default_mode="allow_all")
    assert r.status_code == 200, r.text
    out = env.call("delete_branch", branch="feature-x")  # the report has no mode of its own
    assert out.ran and not out.prompted
    _ScriptedAgent.extra = dict(_MACHINE_RUN)  # nobody watching: acts as Ask, unattended refuse
    out = env.call("delete_branch", branch="feature-x")
    assert not out.ran and not out.prompted


def test_turning_the_switch_off_resets_an_allow_all_default(env, test_client):
    assert env.org_setting(allow_all_enabled=True, default_mode="allow_all").status_code == 200
    assert env.org_setting(allow_all_enabled=False).status_code == 200
    stored = test_client.get("/api/organization/settings", headers=env.headers).json()
    assert stored["config"]["tool_permission_mode"]["value"]["default_mode"] == "ask"
    out = env.call("delete_branch", branch="feature-x")
    assert out.prompted and not out.ran


def test_allow_all_default_alone_is_accepted_while_the_stored_switch_is_on(env):
    assert env.org_setting(allow_all_enabled=True).status_code == 200
    r = env.org_setting(default_mode="allow_all")
    assert r.status_code == 200, r.text


def test_the_judge_can_be_the_small_model(env, test_client):
    r = env.org_setting(judge_decision_model_id="small")
    assert r.status_code == 200, r.text
    stored = test_client.get("/api/organization/settings", headers=env.headers).json()
    assert stored["config"]["tool_permission_mode"]["value"]["judge_decision_model_id"] == "small"
    assert env.org_setting(judge_decision_model_id=str(uuid.uuid4())).status_code == 400


@pytest.fixture
def decision_model(env, test_client, monkeypatch):
    """A decision model behind the fake decision server (the provider boundary),
    set as the Auto router's model. The license cache is patched as in
    tests/e2e/rbac/test_decision_models.py."""
    from app.ai.llm import decisions
    from app.ee import license as ee_license
    from tests.mocks.fake_decision_server import FakeDecisionServer

    monkeypatch.setattr(ee_license, "_cached_license", ee_license.LicenseInfo(
        licensed=True, tier="enterprise", org_name="permission-mode-tests",
        features=["model_routing", "llm_fallback"], license_id="test-permission-modes",
    ))
    monkeypatch.setattr(ee_license, "_cache_initialized", True)
    server = FakeDecisionServer()
    monkeypatch.setattr(decisions, "TRANSPORT_OVERRIDE", server.transport())

    sfx = uuid.uuid4().hex[:6]
    r = test_client.post("/api/llm/providers", headers=env.headers, json={
        "name": f"decide-{sfx}", "provider_type": "custom",
        "credentials": {"base_url": "https://decisions.example.com/v1", "api_key": "dummy-key"},
        "models": [{"model_id": "jev-1.14.0", "name": f"Decider {sfx}", "is_custom": True,
                    "model_kind": "decision", "config": {"decision_format": "systemone"}}],
    })
    assert r.status_code == 200, r.text
    models = test_client.get("/api/llm/models", params={"kind": "decision"}, headers=env.headers).json()
    model_id = models[0]["id"]
    r = test_client.put("/api/organization/settings", headers=env.headers, json={"config": {
        "model_routing_decision": {"value": {"method": "decision", "decision_model_id": model_id}}}})
    assert r.status_code == 200, r.text
    return server


def _judge_requests(server):
    """Requests the Auto judge sent (the router may ask its own questions)."""
    return [q for q in server.requests if "safe_to_run_without_asking" in json.dumps(q["json"])]


def test_automatic_judge_uses_the_decision_model(env, monkeypatch, decision_model):
    small = _judge(monkeypatch, approve=False, confidence=1.0)  # would refuse
    decision_model.pick("yes", confidence=0.99)  # P(safe) = 0.99 → allow
    env.set_mode("auto")
    out = env.call("mystery_action", x="1")
    assert _judge_requests(decision_model)
    assert small == []
    assert out.ran and not out.prompted


def test_small_judge_skips_the_decision_model(env, monkeypatch, decision_model):
    small = _judge(monkeypatch, approve=True, confidence=0.99)
    decision_model.pick("yes", confidence=0.01)  # would refuse if it were asked
    assert env.org_setting(judge_decision_model_id="small").status_code == 200
    env.set_mode("auto")
    out = env.call("mystery_action", x="1")
    assert _judge_requests(decision_model) == []
    assert small == ["mystery_action"]
    assert out.ran and not out.prompted


def _add_member(env, test_client, create_user, login_user):
    email = f"permmode_member_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(f"/api/organizations/{env.org_id}/members", headers=env.headers,
                         json={"organization_id": str(env.org_id), "email": email, "role": "member"})
    assert r.status_code == 200, r.text
    create_user(email=email, password="test123")
    token = login_user(email, "test123")
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(env.org_id)}, token


def test_allow_all_default_for_members(env, test_client, create_user, login_user, create_report):
    assert env.org_setting(allow_all_enabled=True, default_mode="allow_all").status_code == 200
    member, member_token = _add_member(env, test_client, create_user, login_user)

    # A member can't change the org setting.
    r = test_client.put("/api/organization/settings", headers=member,
                        json={"config": {"tool_permission_mode": {"value": {"default_mode": "allow_all"}}}})
    assert r.status_code == 403, r.text

    # Nor run on the owner's report.
    before = len(_ScriptedAgent.runs)
    _ScriptedAgent.call = {"connection_id": env.conn_id, "tool_name": "delete_branch",
                           "arguments": {"branch": "feature-x"}}
    r = test_client.post(f"/api/reports/{env.report_id}/completions", headers=member,
                         json={"prompt": {"content": "run delete_branch"}})
    assert r.status_code in (403, 404), r.text
    assert len(_ScriptedAgent.runs) == before

    # On their own new report, the org default applies.
    r = test_client.put(f"/api/data_sources/{env.ds_id}", headers=env.headers, json={"is_public": True})
    assert r.status_code == 200, r.text
    own = create_report(title="Mine", user_token=member_token, org_id=env.org_id, data_sources=[env.ds_id])["id"]
    r = test_client.post(f"/api/reports/{own}/completions", headers=member,
                         json={"prompt": {"content": "run delete_branch"}})
    assert r.status_code == 200, r.text
    assert len(_ScriptedAgent.runs) == before + 1, "scripted agent did not run"
    out = _Outcome(_ScriptedAgent.runs[-1])
    assert out.ran and not out.prompted


# ── The real agent loop ─────────────────────────────────────────────────────
# The tests above drive one execute_mcp call through a scripted stand-in for
# AgentV2. These run the real AgentV2 (its runtime context, its org settings
# loaded at run start, its run agents) with only the model stubbed:
# LLM.inference_stream_v2 replays scripted tool calls, as in
# tests/e2e/rbac/test_file_tools_run_scope.py.

_REAL_AGENT = completion_mod.AgentV2


class _Planner:
    """One scripted tool call per planner request, then a plain answer.

    A script step is (tool suffix, arguments, before): the call goes to the
    offered tool whose name ends with ``__<suffix>``, and ``before`` (an async
    callable, optional) runs first, between two steps of the same run."""

    def __init__(self):
        self.script: list = []
        self.called: list = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            names = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in tools or []]
            if planner.script:
                suffix, args, before = planner.script.pop(0)
                if before is not None:
                    await before()
                name = next(n for n in names if n and n.endswith(f"__{suffix}"))
                planner.called.append(name)
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text="Done."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for e in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield e

        return scripted


@pytest.fixture
def real_agent(env, monkeypatch):
    from app.ai.llm.llm import LLM
    from app.ai.tools.mcp_tool_registry import clear_usage_cache

    monkeypatch.setattr(completion_mod, "AgentV2", _REAL_AGENT)
    clear_usage_cache()
    p = _Planner()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    yield p
    clear_usage_cache()


def _ask_real(env, test_client):
    r = test_client.post(f"/api/reports/{env.report_id}/completions", headers=env.headers,
                         params={"background": False}, json={"prompt": {"content": "Use the tools."}})
    assert r.status_code == 200, r.text


def _cards(alembic_config, env) -> list:
    """The tool names this report raised an approval card for, in order."""
    from sqlalchemy import select

    from app.models.tool_confirmation import ToolConfirmation

    t = ToolConfirmation.__table__
    engine = _sync_engine(alembic_config)
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.tool_name).where(t.c.report_id == env.report_id)
                            .order_by(t.c.created_at)).scalars().all()
    engine.dispose()
    return list(rows)


async def _turn_allow_all_off(org_id):
    """An admin turns Allow all off, as the settings page saves it."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.organization_settings import OrganizationSettings

    async with async_session_maker() as db:
        row = (await db.execute(select(OrganizationSettings)
                                .where(OrganizationSettings.organization_id == str(org_id)))).scalar_one()
        config = json.loads(json.dumps(row.config))
        config["tool_permission_mode"]["value"]["allow_all_enabled"] = False
        row.config = config
        await db.commit()


def test_real_run_sees_allow_all_turned_off_mid_run(env, test_client, alembic_config, real_agent, monkeypatch):
    sent = _sent_to_server(monkeypatch)
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    off = lambda: _turn_allow_all_off(env.org_id)  # noqa: E731
    real_agent.script = [("delete_branch", {"branch": "a"}, None), ("delete_branch", {"branch": "b"}, off)]
    _ask_real(env, test_client)
    assert len(real_agent.called) == 2
    # The first call ran under Allow all; the second, after the switch went
    # off, asked (the run's org settings were loaded before it).
    assert sent == ["delete_branch"]
    assert _cards(alembic_config, env) == ["delete_branch"]


def _sent_to_server(monkeypatch) -> list:
    """Tool names the MCP client sends to the server (a spy that delegates)."""
    from app.data_sources.clients.mcp_client import McpClient

    sent = []
    real_call = McpClient.acall_tool

    async def spy(self, tool_name, *args, **kwargs):
        sent.append(tool_name)
        return await real_call(self, tool_name, *args, **kwargs)

    monkeypatch.setattr(McpClient, "acall_tool", spy)
    return sent


def test_real_machine_turns_prompt_and_keep_the_reports_grants(env, test_client, alembic_config, real_agent,
                                                               monkeypatch):
    sent = _sent_to_server(monkeypatch)
    # A wait wake prompts on an ask tool, as a turn the user typed.
    real_agent.script = [("delete_branch", {"branch": "a"}, None)]
    _machine_turn_real(env, "wait")
    assert _cards(alembic_config, env) == ["delete_branch"]
    assert sent == []
    # A "for this report" allow applies to a hidden sub-agent turn: it runs
    # without a card.
    r = _answer(env, test_client, alembic_config, "mystery_action", approved=True, scope="report")
    assert r.status_code == 200, r.text
    before = len(_cards(alembic_config, env))
    real_agent.script = [("mystery_action", {"x": "1"}, None)]
    _machine_turn_real(env, "sub_agent")
    assert real_agent.called[-1].endswith("__mystery_action")
    assert len(_cards(alembic_config, env)) == before
    assert sent == ["mystery_action"]


def test_real_run_applies_a_cap_from_its_agents(env, test_client, alembic_config, real_agent, create_data_source,
                                                second_content_url, monkeypatch):
    sent = _sent_to_server(monkeypatch)
    capped = create_data_source(
        name="Capped agent", type="mcp", config={"server_url": second_content_url, "transport": "streamable_http"},
        credentials={}, user_token=env.token, org_id=env.org_id)
    r = test_client.put(f"/api/reports/{env.report_id}", headers=env.headers,
                        json={"data_sources": [env.ds_id, capped["id"]]})
    assert r.status_code == 200, r.text
    env.org_setting(allow_all_enabled=True)
    env.set_mode("allow_all")
    real_agent.script = [("delete_branch", {"branch": "a"}, None)]
    _ask_real(env, test_client)
    assert _cards(alembic_config, env) == [] and sent == ["delete_branch"]  # control: Allow all, no cap

    r = test_client.put(f"/api/data_sources/{capped['id']}", headers=env.headers, json={"max_permission_mode": "ask"})
    assert r.status_code == 200, r.text
    real_agent.script = [("delete_branch", {"branch": "b"}, None)]
    _ask_real(env, test_client)
    assert _cards(alembic_config, env) == ["delete_branch"]


def _machine_turn_real(env, trigger_source):
    import asyncio

    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.machine_turn import run_machine_turn

    async def _run():
        async with async_session_maker() as db:
            await run_machine_turn(
                db, report=await db.get(Report, env.report_id), user=await db.get(User, env.user_id),
                organization=await db.get(Organization, env.org_id), summary="machine turn",
                trigger_source=trigger_source, message_type=f"{trigger_source}_event", instruction="resume")

    asyncio.run(_run())
