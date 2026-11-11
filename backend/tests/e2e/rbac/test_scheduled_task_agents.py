"""Scheduled tasks run on their own agents.

Reported: a scheduled task ran with the report's agents, not the agents chosen
in the task, and choosing agents for a task silently changed the whole
report's — so two tasks in one report could not use different agents.

Contract:
  * a task with its own agents runs on exactly those: their tools are offered,
    the report's other agents' are not;
  * a task without its own agents runs on the report's, as before;
  * two tasks in one report each run on their own agents;
  * running a task never changes the report's attached agents;
  * a spawned run report gets the task's agents;
  * a task can only name agents its owner can use (member and admin alike),
    and an empty selection goes back to the report's agents.

The LLM boundary is stubbed; the scheduler run, routes, services, the agent
loop and the custom API clients run real.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.e2e


class _Planner:
    def __init__(self):
        self.requests: list[list[str]] = []

    def stream(self):
        from app.ai.llm.types import MessageStopEvent, TextDeltaEvent, UsageEvent
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            names = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])]
            planner.requests.append([n for n in names if n])
            for e in (TextDeltaEvent(text="Done."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn"),
                      UsageEvent(input_tokens=10, output_tokens=5)):
                yield e

        return scripted

    def mcp_tools(self):
        return {n for n in self.requests[0] if n.startswith("mcp__")}


@pytest.fixture
def planner(monkeypatch):
    from app.ai.llm.llm import LLM
    from app.ai.tools.mcp_tool_registry import clear_usage_cache

    clear_usage_cache()
    p = _Planner()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    yield p
    clear_usage_cache()


@pytest.fixture
def api_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def org(test_client, bootstrap_admin, invite_user_to_org, create_llm_provider_and_models, enable_mcp,
        create_custom_api_connection, create_domain_from_connection, wait_for_connection_indexing,
        api_url, monkeypatch):
    """Three custom API agents: two public (usage, billing), one admin-only."""
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("schedagents")
    token, org_id = admin["token"], str(admin["org_id"])
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    enable_mcp(user_token=token, org_id=org_id)
    agents = {}
    for label, public in (("usage", True), ("billing", True), ("private", False)):
        slug = f"{label}{uuid.uuid4().hex[:4]}"
        conn = create_custom_api_connection(
            name=slug, base_url=api_url, user_token=token, org_id=org_id,
            endpoints=[{"name": f"{label}_report", "method": "GET", "path": f"/{label}",
                        "description": f"{label} report", "parameters": []}],
        )
        ds = create_domain_from_connection(name=f"{slug} agent", connection_id=conn["id"], user_token=token,
                                           org_id=org_id, is_public=public)
        # Creation starts a background discovery run that writes the tool
        # catalog with the fixture's mock client; let it finish before the
        # real refresh, or it can land after it (it does on Postgres).
        wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
        assert test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=_h(token, org_id)).status_code == 200
        agents[label] = {"ds_id": str(ds["id"]), "tool": f"mcp__{slug}__{label}_report"}
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    return {"admin": admin, "member": member, "org_id": org_id, "agents": agents}


def _report(test_client, user, org, agent_ids):
    r = test_client.post("/api/reports", headers=_h(user["token"], org["org_id"]),
                         json={"title": "costs", "files": [], "data_sources": agent_ids})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _task(test_client, user, org, report_id, **fields):
    r = test_client.post(f"/api/reports/{report_id}/scheduled-prompts", headers=_h(user["token"], org["org_id"]),
                         json={"prompt": {"content": "Pull the report."}, "cron_schedule": "0 7 * * *", **fields})
    return r


def _run(sp_id):
    from app.services.scheduled_prompt_service import scheduled_prompt_service
    asyncio.run(scheduled_prompt_service.scheduled_run_prompt(sp_id, force=True))


def _attached(test_client, user, org, report_id):
    r = test_client.get(f"/api/reports/{report_id}", headers=_h(user["token"], org["org_id"]))
    assert r.status_code == 200, r.text
    return {d["id"] for d in r.json()["data_sources"]}


@pytest.mark.parametrize("role", ["admin", "member"])
def test_a_task_runs_on_its_own_agents_and_leaves_the_report_alone(test_client, org, planner, role):
    user, a = org[role], org["agents"]
    report_id = _report(test_client, user, org, [a["usage"]["ds_id"]])
    r = _task(test_client, user, org, report_id, data_source_ids=[a["billing"]["ds_id"]])
    assert r.status_code == 200, r.text
    assert r.json()["data_source_ids"] == [a["billing"]["ds_id"]]

    _run(r.json()["id"])

    assert a["billing"]["tool"] in planner.mcp_tools(), planner.requests[0]
    assert a["usage"]["tool"] not in planner.mcp_tools(), planner.requests[0]
    assert _attached(test_client, user, org, report_id) == {a["usage"]["ds_id"]}


def test_a_task_without_its_own_agents_uses_the_reports(test_client, org, planner):
    user, a = org["admin"], org["agents"]
    report_id = _report(test_client, user, org, [a["usage"]["ds_id"]])
    r = _task(test_client, user, org, report_id)
    assert r.status_code == 200, r.text
    assert r.json()["data_source_ids"] is None

    _run(r.json()["id"])

    assert planner.mcp_tools() == {a["usage"]["tool"]}


def test_two_tasks_in_one_report_each_use_their_own_agents(test_client, org, planner):
    user, a = org["admin"], org["agents"]
    report_id = _report(test_client, user, org, [a["usage"]["ds_id"], a["billing"]["ds_id"]])
    usage = _task(test_client, user, org, report_id, data_source_ids=[a["usage"]["ds_id"]]).json()
    billing = _task(test_client, user, org, report_id, data_source_ids=[a["billing"]["ds_id"]]).json()

    _run(usage["id"])
    first = planner.mcp_tools()
    planner.requests.clear()
    _run(billing["id"])
    second = planner.mcp_tools()

    assert first == {a["usage"]["tool"]}
    assert second == {a["billing"]["tool"]}
    assert _attached(test_client, user, org, report_id) == {a["usage"]["ds_id"], a["billing"]["ds_id"]}


def test_a_spawned_run_report_gets_the_tasks_agents(test_client, org, planner):
    user, a = org["admin"], org["agents"]
    report_id = _report(test_client, user, org, [a["usage"]["ds_id"]])
    sp = _task(test_client, user, org, report_id, spawn_new_report=True,
               data_source_ids=[a["billing"]["ds_id"]]).json()

    _run(sp["id"])

    assert planner.mcp_tools() == {a["billing"]["tool"]}
    runs = test_client.get(f"/api/reports/{report_id}/scheduled-prompts/{sp['id']}/runs",
                           headers=_h(user["token"], org["org_id"])).json()["runs"]
    assert runs, runs
    assert _attached(test_client, user, org, runs[0]["report_id"]) == {a["billing"]["ds_id"]}


@pytest.mark.parametrize("role", ["admin", "member"])
def test_a_task_cannot_name_an_agent_its_owner_cannot_use(test_client, org, planner, role):
    user, a = org[role], org["agents"]
    report_id = _report(test_client, user, org, [a["usage"]["ds_id"]])
    bad = [str(uuid.uuid4())] if role == "admin" else [a["private"]["ds_id"]]

    r = _task(test_client, user, org, report_id, data_source_ids=bad)
    assert r.status_code == 400, r.text

    ok = _task(test_client, user, org, report_id, data_source_ids=[a["billing"]["ds_id"]]).json()
    r = test_client.put(f"/api/reports/{report_id}/scheduled-prompts/{ok['id']}",
                        headers=_h(user["token"], org["org_id"]), json={"data_source_ids": bad})
    assert r.status_code == 400, r.text


def test_an_empty_selection_goes_back_to_the_reports_agents(test_client, org, planner):
    user, a = org["admin"], org["agents"]
    report_id = _report(test_client, user, org, [a["usage"]["ds_id"]])
    sp = _task(test_client, user, org, report_id, data_source_ids=[a["billing"]["ds_id"]]).json()

    r = test_client.put(f"/api/reports/{report_id}/scheduled-prompts/{sp['id']}",
                        headers=_h(user["token"], org["org_id"]), json={"data_source_ids": []})
    assert r.status_code == 200, r.text
    assert r.json()["data_source_ids"] is None
    _run(sp["id"])
    assert planner.mcp_tools() == {a["usage"]["tool"]}
