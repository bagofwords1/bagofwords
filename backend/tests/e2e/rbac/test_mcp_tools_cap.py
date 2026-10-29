"""MCP tools past ``tools_max``: what the model is offered, and how it reaches the rest.

Contract:
  * under the cap every MCP tool on the run's agents is a native tool, and
    neither ``search_tools``, ``call_tool`` nor the ``execute_mcp`` gateway is
    offered;
  * past the cap the most used tools (last 30 days) are native, the rest are
    found with ``search_tools`` (full schema) and run with ``call_tool``, which
    goes through the same execution path;
  * the tool list is identical from one run to the next (prompt caching), and
    sorted by name, never by usage;
  * ``search_tools`` only returns tools the running user can reach, and
    ``call_tool`` refuses any other name — for members as well as admins;
  * custom API connections take the same path: native tools, search past
    the cap, and the call reaches the API through its real client;
  * a tool on two of the run's agents with different settings gets the
    strictest one, in the tool list and when it is called.

The LLM boundary and the MCP provider client are stubbed; routes, services, the
agent loop and the tool gateway run real.
"""
from __future__ import annotations

import contextlib
import json
import uuid
from unittest.mock import patch

import pytest

from tests.mocks.mock_mcp_server import MockToolProviderClient

pytestmark = pytest.mark.e2e


class _Planner:
    """Scripted planner: replays queued tool calls, one per request, then answers."""

    def __init__(self):
        self.requests: list[list[str]] = []
        self.texts: list[str] = []
        self.queue: list = []
        self.results: list[str] = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            names = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])]
            planner.requests.append([n for n in names if n])
            planner.texts.append(json.dumps([system, messages], default=str))
            planner.results = [
                json.dumps(b.get("content"))
                for m in messages or []
                for b in ((m.get("content") if isinstance(m, dict) else getattr(m, "content", None)) or [])
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if planner.queue:
                step = planner.queue.pop(0)
                name, args = step(planner) if callable(step) else step
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text="Done."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for e in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield e

        return scripted

    def mcp_tools(self, i=0):
        return [n for n in self.requests[i] if n.startswith("mcp__")]


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


@contextlib.contextmanager
def _provider(constructed: list | None = None):
    client = MockToolProviderClient()

    async def _construct(self, db, connection, current_user=None, **kwargs):
        if constructed is not None:
            constructed.append(str(connection.id))
        return client

    with patch("app.services.connection_service.ConnectionService.construct_client", _construct):
        yield client


@pytest.fixture
def org(test_client, bootstrap_admin, invite_user_to_org, create_llm_provider_and_models,
        enable_mcp, create_mcp_connection, create_domain_from_connection, monkeypatch):
    """Admin + member; a public agent and an admin-only agent, each on its own
    MCP server with the mock's four tools (echo, failing_tool, get_records, search_docs)."""
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("mcpcap")
    token, org_id = admin["token"], admin["org_id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    enable_mcp(user_token=token, org_id=org_id)
    h = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    agents = {}
    for label, public in (("shared", True), ("private", False)):
        slug = f"{label}{uuid.uuid4().hex[:4]}"
        conn = create_mcp_connection(name=slug, server_url=f"http://{slug}:3000/mcp", user_token=token, org_id=org_id)
        with _provider():
            assert test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=h).status_code == 200
        ds = create_domain_from_connection(name=f"{slug} agent", connection_id=conn["id"],
                                           user_token=token, org_id=org_id, is_public=public)
        agents[label] = {"slug": slug, "conn_id": str(conn["id"]), "ds_id": str(ds["id"])}
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    return {"admin": admin, "member": member, "org_id": org_id, "agents": agents, "h": h}


def _set_report_type(report_id, report_type):
    """Direct write: artifact_chat reports are made by ArtifactChatService from a
    published artifact; only the report_type matters to these contracts."""
    import asyncio
    import os
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from app.models.report import Report

    url = (os.environ["TEST_DATABASE_URL"]
           .replace("sqlite://", "sqlite+aiosqlite://", 1)
           .replace("postgresql://", "postgresql+asyncpg://", 1))

    async def _run():
        engine = create_async_engine(url)
        try:
            async with async_sessionmaker(engine)() as db:
                await db.execute(update(Report).where(Report.id == report_id).values(report_type=report_type))
                await db.commit()
        finally:
            await engine.dispose()
    asyncio.run(_run())


def _ask(test_client, user, org_id, data_sources, constructed=None, report_type=None):
    h = {"Authorization": f"Bearer {user['token']}", "X-Organization-Id": str(org_id)}
    r = test_client.post("/api/reports", headers=h, json={"title": "cap", "files": [], "data_sources": data_sources})
    assert r.status_code == 200, r.text
    if report_type:
        _set_report_type(r.json()["id"], report_type)
    with _provider(constructed) as client:
        r = test_client.post(f"/api/reports/{r.json()['id']}/completions", headers=h,
                             params={"background": False}, json={"prompt": {"content": "Use the tools."}})
    assert r.status_code == 200, r.text
    return client


def _name(slug, tool):
    return f"mcp__{slug}__{tool}"


def test_under_the_cap_every_tool_is_native_and_no_search_is_offered(test_client, org, planner):
    shared = org["agents"]["shared"]
    _ask(test_client, org["admin"], org["org_id"], [shared["ds_id"]])
    offered = planner.requests[0]
    assert planner.mcp_tools() == [_name(shared["slug"], t) for t in ("echo", "failing_tool", "get_records", "search_docs")]
    assert not {"search_tools", "call_tool", "execute_mcp", "search_mcps"} & set(offered), offered


def test_past_the_cap_the_rest_is_found_and_called_and_usage_decides_what_is_native(
        test_client, org, planner, monkeypatch):
    monkeypatch.setenv("BOW_TOOLS_MAX", "2")
    shared = org["agents"]["shared"]
    target = _name(shared["slug"], "get_records")

    # Nothing used yet: the first two by name are native, the rest is searchable.
    planner.queue = [
        ("search_tools", {"query": "records"}),
        ("call_tool", {"name": target, "arguments": {"count": 3}}),
    ]
    client = _ask(test_client, org["admin"], org["org_id"], [shared["ds_id"]])
    assert planner.mcp_tools(0) == [_name(shared["slug"], "echo"), _name(shared["slug"], "failing_tool")]
    assert {"search_tools", "call_tool"} <= set(planner.requests[0])
    assert "execute_mcp" not in planner.requests[0]
    # The context index marks exactly the tools that are not in the tool list.
    context = planner.texts[0].replace('\\"', '"')
    for tool in ("get_records", "search_docs"):
        assert f'<tool name="{tool}" deferred="true"' in context, tool
    for tool in ("echo", "failing_tool"):
        assert f'<tool name="{tool}" deferred' not in context, tool

    found = planner.results[0]
    assert target in found, found
    assert "Number of records to return" in found, "search_tools returns the tool's full input schema"
    assert client._call_log == [{"tool_name": "get_records", "arguments": {"count": 3}}]

    # Next day's ranking: get_records was used, so it is native now; the list
    # is still sorted by name, not by usage.
    from app.ai.tools.mcp_tool_registry import clear_usage_cache
    clear_usage_cache()
    planner.requests.clear()
    _ask(test_client, org["admin"], org["org_id"], [shared["ds_id"]])
    assert planner.mcp_tools(0) == [_name(shared["slug"], "echo"), target]


def test_the_tool_list_is_identical_across_runs(test_client, org, planner, monkeypatch):
    monkeypatch.setenv("BOW_TOOLS_MAX", "3")
    data_sources = [org["agents"]["shared"]["ds_id"], org["agents"]["private"]["ds_id"]]
    _ask(test_client, org["admin"], org["org_id"], data_sources)
    _ask(test_client, org["admin"], org["org_id"], data_sources)
    assert planner.requests[0] == planner.requests[1]


@pytest.mark.parametrize("who", ["admin", "member"])
def test_search_tools_only_returns_tools_the_user_can_reach(test_client, org, planner, monkeypatch, who):
    """Auto, everything behind search (tools_max=0)."""
    monkeypatch.setenv("BOW_TOOLS_MAX", "0")
    shared, private = org["agents"]["shared"], org["agents"]["private"]
    planner.queue = [
        ("search_tools", {}),
        ("call_tool", {"name": _name(private["slug"], "echo"), "arguments": {"message": "hi"}}),
    ]
    constructed: list = []
    _ask(test_client, org[who], org["org_id"], [], constructed)

    assert planner.mcp_tools(0) == []
    found = planner.results[0]
    assert _name(shared["slug"], "echo") in found
    if who == "admin":
        assert _name(private["slug"], "echo") in found
        assert private["conn_id"] in constructed
    else:
        assert private["slug"] not in found, found
        assert private["conn_id"] not in constructed
        assert "Unknown tool" in planner.results[-1]


@pytest.fixture
def two_agents_one_connection(test_client, org, create_domain_from_connection):
    """A second agent on the shared agent's connection."""
    shared = org["agents"]["shared"]
    token, org_id = org["admin"]["token"], org["org_id"]
    other = create_domain_from_connection(name=f"{shared['slug']} second", connection_id=shared["conn_id"],
                                          user_token=token, org_id=org_id, is_public=True)
    tools = test_client.get(f"/api/data_sources/{shared['ds_id']}/tools", headers=org["h"]).json()
    ids = {t["name"]: t["id"] for t in tools}
    return {"a": shared["ds_id"], "b": str(other["id"]), "tool_ids": ids}


def _set(test_client, org, ds_id, tool_id, **body):
    r = test_client.put(f"/api/data_sources/{ds_id}/tools/{tool_id}", headers=org["h"], json=body)
    assert r.status_code == 200, r.text


def test_a_shared_tool_takes_the_strictest_setting_across_the_runs_agents(
        test_client, org, planner, two_agents_one_connection):
    agents = two_agents_one_connection
    slug, conn_id = org["agents"]["shared"]["slug"], org["agents"]["shared"]["conn_id"]
    # Written allow-first, so "the first setting found" is the permissive one.
    _set(test_client, org, agents["b"], agents["tool_ids"]["echo"], policy="allow")
    _set(test_client, org, agents["a"], agents["tool_ids"]["echo"], policy="deny")
    _set(test_client, org, agents["b"], agents["tool_ids"]["get_records"], is_enabled=True)
    _set(test_client, org, agents["a"], agents["tool_ids"]["get_records"], is_enabled=False)

    planner.queue = [("execute_mcp", {"connection_id": conn_id, "tool_name": "echo",
                                      "arguments": {"message": "hi"}, "title": "Echo"})]
    client = _ask(test_client, org["admin"], org["org_id"], [agents["a"], agents["b"]])

    offered = planner.mcp_tools(0)
    assert _name(slug, "echo") not in offered and _name(slug, "get_records") not in offered, offered
    assert _name(slug, "search_docs") in offered
    assert client._call_log == [], "a call to the denied tool must not reach the server"
    assert "denied" in planner.results[-1].lower(), planner.results
    # <mcp_tools> agrees with the tool list: each agent's own view would list
    # get_records (agent b enables it) and echo, which the run cannot call.
    context = planner.texts[0].replace('\\"', '"')
    assert '<tool name="search_docs"' in context
    assert '<tool name="get_records"' not in context and '<tool name="echo"' not in context, context


def test_strictest_setting_unit_rules():
    from types import SimpleNamespace as N
    from app.services.tool_policy_service import strictest_admin_setting

    tool = N(is_enabled=True, policy="allow")
    assert strictest_admin_setting(tool, []) == (True, "allow")
    assert strictest_admin_setting(tool, [N(is_enabled=True, policy="ask")], 1) == (True, "ask")
    # One agent overrides, the other uses the tool default.
    assert strictest_admin_setting(N(is_enabled=True, policy="deny"), [N(is_enabled=True, policy="allow")], 2) == (True, "deny")
    assert strictest_admin_setting(tool, [N(is_enabled=True, policy="auto"), N(is_enabled=True, policy="ask")], 2) == (True, "ask")
    assert strictest_admin_setting(tool, [N(is_enabled=False, policy="allow"), N(is_enabled=True, policy="allow")], 2)[0] is False


def test_no_mcp_tool_is_offered_when_the_org_switches_mcp_tools_off(
        test_client, org, planner, update_organization_settings):
    update_organization_settings(config={"enable_mcp_tools": {"value": False}},
                                 user_token=org["admin"]["token"], org_id=org["org_id"])
    _ask(test_client, org["admin"], org["org_id"], [org["agents"]["shared"]["ds_id"]])
    offered = planner.requests[0]
    assert planner.mcp_tools() == [] and not {"search_tools", "call_tool", "execute_mcp"} & set(offered), offered


def test_a_shared_artifact_viewer_chat_gets_no_mcp_tool_under_any_name(test_client, org, planner):
    """The viewer chat's allowlist has no MCP tool: none may be registered
    natively, listed in <mcp_tools>, or run when the model calls one anyway."""
    shared = org["agents"]["shared"]
    planner.queue = [
        (_name(shared["slug"], "echo"), {"message": "hi"}),
        ("execute_mcp", {"connection_id": shared["conn_id"], "tool_name": "echo", "arguments": {"message": "hi"}}),
    ]
    client = _ask(test_client, org["admin"], org["org_id"], [shared["ds_id"]], report_type="artifact_chat")
    offered = planner.requests[0]
    assert planner.mcp_tools() == [] and not {"search_tools", "call_tool", "execute_mcp"} & set(offered), offered
    assert "<mcp_tools>" not in planner.texts[0]
    assert client._call_log == [], "no MCP call may reach the server from a viewer chat"


def test_when_the_catalog_cannot_be_built_the_run_falls_back_to_execute_mcp(
        test_client, org, planner, monkeypatch):
    from app.ai.tools import mcp_tool_registry

    async def failed(*a, **k):
        return mcp_tool_registry.McpToolCatalog(failed=True)

    monkeypatch.setattr(mcp_tool_registry, "build_mcp_tool_catalog", failed)
    shared = org["agents"]["shared"]
    planner.queue = [("execute_mcp", {"connection_id": shared["conn_id"], "tool_name": "echo",
                                      "arguments": {"message": "hi"}, "title": "Echo"})]
    client = _ask(test_client, org["admin"], org["org_id"], [shared["ds_id"]])
    offered = planner.requests[0]
    assert "execute_mcp" in offered and planner.mcp_tools() == [], offered
    context = planner.texts[0]
    assert "Call these tools with execute_mcp" in context
    assert "in your tool list as mcp__" not in context
    assert client._call_log == [{"tool_name": "echo", "arguments": {"message": "hi"}}]


# ── Custom API connections take the same path ────────────────────────────

@pytest.fixture
def items_api():
    """A real HTTP server for a custom API connection; records each request."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps({"items": [{"id": 1, "name": "widget"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield {"url": f"http://127.0.0.1:{srv.server_address[1]}", "seen": seen}
    srv.shutdown()


@pytest.fixture
def custom_api_agent(test_client, org, items_api, create_custom_api_connection, create_domain_from_connection):
    token, org_id = org["admin"]["token"], org["org_id"]
    slug = f"items{uuid.uuid4().hex[:4]}"
    conn = create_custom_api_connection(name=slug, base_url=items_api["url"], user_token=token, org_id=org_id)
    r = test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=org["h"])
    assert r.status_code == 200, r.text
    ds = create_domain_from_connection(name=f"{slug} agent", connection_id=conn["id"], user_token=token, org_id=org_id)
    return {"slug": slug, "ds_id": str(ds["id"])}


def _ask_real(test_client, user, org_id, data_sources):
    """Like _ask, but the connection's real client makes the HTTP call."""
    h = {"Authorization": f"Bearer {user['token']}", "X-Organization-Id": str(org_id)}
    r = test_client.post("/api/reports", headers=h, json={"title": "api", "files": [], "data_sources": data_sources})
    assert r.status_code == 200, r.text
    r = test_client.post(f"/api/reports/{r.json()['id']}/completions", headers=h,
                         params={"background": False}, json={"prompt": {"content": "Use the tools."}})
    assert r.status_code == 200, r.text


def test_a_custom_api_tool_is_native_and_its_call_reaches_the_api(test_client, org, planner, items_api, custom_api_agent):
    name = _name(custom_api_agent["slug"], "get_items")
    planner.queue = [(name, {"limit": 3})]
    _ask_real(test_client, org["admin"], org["org_id"], [custom_api_agent["ds_id"]])
    assert planner.mcp_tools(0) == [name], planner.requests[0]
    assert "execute_mcp" not in planner.requests[0]
    assert items_api["seen"] == ["/items?limit=3"], items_api["seen"]
    assert "widget" in planner.results[-1], planner.results


def test_a_custom_api_tool_past_the_cap_is_found_and_called(test_client, org, planner, monkeypatch, items_api, custom_api_agent):
    monkeypatch.setenv("BOW_TOOLS_MAX", "0")
    name = _name(custom_api_agent["slug"], "get_items")
    planner.queue = [("search_tools", {"query": "items"}),
                     ("call_tool", {"name": name, "arguments": {"limit": 2}})]
    _ask_real(test_client, org["admin"], org["org_id"], [custom_api_agent["ds_id"]])
    assert planner.mcp_tools(0) == [] and {"search_tools", "call_tool"} <= set(planner.requests[0])
    assert name in planner.results[0], planner.results
    assert items_api["seen"] == ["/items?limit=2"], items_api["seen"]
