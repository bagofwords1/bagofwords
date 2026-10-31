"""MCP tools follow the run's agents, in Auto and in a pinned selection.

Contract:
  * every MCP tool on the agents a run executes against is offered to the model
    as its own native tool (``mcp__<connection>__<tool>``), with no org switch;
  * "the run's agents" is the working set: the pinned agents, or in Auto every
    agent the RUNNING user can access. An Auto report has no attachments, which
    used to leave it with no native tools at all;
  * a tool on an agent outside that set is neither offered nor callable through
    ``execute_mcp`` by connection id, for admins and members alike.

The LLM boundary (``LLM.inference_stream_v2`` / ``LLM.inference``) and the MCP
provider client are stubbed; routes, services, the agent loop and the tool
gateway run real. Each planner request is recorded with the tool names it
offered, and each provider client construction with its connection.
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
    def __init__(self):
        self.tool_names: list[set] = []
        self.schemas: dict = {}
        self.force_tool: tuple | None = None
        self.tool_results: list[str] = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            names = {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])}
            names.discard(None)
            results = [
                json.dumps(b.get("content"))
                for m in messages or []
                for b in ((m.get("content") if isinstance(m, dict) else getattr(m, "content", None)) or [])
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            planner.tool_names.append(names)
            for t in tools or []:
                name = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                schema = getattr(t, "input_schema", None) or getattr(t, "schema", None) or (
                    (t.get("input_schema") or t.get("schema")) if isinstance(t, dict) else None)
                planner.schemas[name] = schema or {}
            if not results and planner.force_tool:
                name, args = planner.force_tool
                planner.force_tool = None
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                planner.tool_results += results
                events = [TextDeltaEvent(text="Done."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for e in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield e

        return scripted


@pytest.fixture
def planner(monkeypatch):
    from app.ai.llm.llm import LLM

    p = _Planner()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    return p


@contextlib.contextmanager
def _provider(constructed: list):
    client = MockToolProviderClient()

    async def _construct(self, db, connection, current_user=None, **kwargs):
        constructed.append(str(connection.id))
        return client

    with patch("app.services.connection_service.ConnectionService.construct_client", _construct):
        yield client


@pytest.fixture
def org(test_client, bootstrap_admin, invite_user_to_org, create_llm_provider_and_models,
        enable_mcp, create_mcp_connection, create_domain_from_connection, monkeypatch):
    """Admin + member; a public agent and an admin-only agent, each on its own MCP server."""
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("mcpscope")
    token, org_id = admin["token"], admin["org_id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    enable_mcp(user_token=token, org_id=org_id)
    h = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    agents = {}
    for label, public in (("shared", True), ("private", False)):
        slug = f"{label}{uuid.uuid4().hex[:4]}"
        conn = create_mcp_connection(name=slug, server_url=f"http://{slug}:3000/mcp",
                                     user_token=token, org_id=org_id)
        with _provider([]):
            r = test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=h)
            assert r.status_code == 200, r.text
        ds = create_domain_from_connection(name=f"{slug} agent", connection_id=conn["id"],
                                           user_token=token, org_id=org_id, is_public=public)
        agents[label] = {"slug": slug, "conn_id": str(conn["id"]), "ds_id": str(ds["id"])}

    member = invite_user_to_org(org_id=org_id, admin_token=token)
    return {"admin": admin, "member": member, "org_id": org_id, "agents": agents}


def _ask(test_client, token, org_id, data_sources, constructed):
    h = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    r = test_client.post("/api/reports", headers=h,
                         json={"title": "scope", "files": [], "data_sources": data_sources})
    assert r.status_code == 200, r.text
    with _provider(constructed):
        r = test_client.post(f"/api/reports/{r.json()['id']}/completions", headers=h,
                             params={"background": False},
                             json={"prompt": {"content": "Use the tools to echo hello."}})
    assert r.status_code == 200, r.text


def _native(names, slug):
    return {n for n in names if n.startswith("mcp__") and slug.lower() in n.lower()}


@pytest.mark.parametrize("who", ["admin", "member"])
def test_auto_offers_native_tools_for_exactly_the_agents_the_user_can_access(test_client, org, planner, who):
    user = org[who]
    _ask(test_client, user["token"], org["org_id"], [], [])
    offered = planner.tool_names[0]

    assert _native(offered, org["agents"]["shared"]["slug"]), sorted(offered)
    private = _native(offered, org["agents"]["private"]["slug"])
    if who == "admin":
        assert private, "the admin can reach the private agent, so Auto includes its tools"
    else:
        assert not private, "a member must not be offered tools on an agent they cannot access"


def test_pinned_selection_offers_only_the_pinned_agents_tools(test_client, org, planner):
    admin = org["admin"]
    _ask(test_client, admin["token"], org["org_id"], [org["agents"]["shared"]["ds_id"]], [])
    offered = planner.tool_names[0]
    assert _native(offered, org["agents"]["shared"]["slug"])
    assert not _native(offered, org["agents"]["private"]["slug"])


@pytest.mark.parametrize("target, reachable", [("shared", True), ("private", False)])
def test_execute_mcp_only_reaches_connections_on_the_runs_agents(test_client, org, planner, target, reachable):
    """By connection id, from a member's Auto report: an agent outside the
    member's working set stays unreachable even though the id is valid."""
    member = org["member"]
    conn_id = org["agents"][target]["conn_id"]
    planner.force_tool = ("execute_mcp", {"connection_id": conn_id, "tool_name": "echo",
                                          "arguments": {"message": "hello"}, "title": "Echo"})
    constructed: list = []
    _ask(test_client, member["token"], org["org_id"], [], constructed)

    assert (conn_id in constructed) is reachable, constructed
    if not reachable:
        assert any("not found" in r.lower() for r in planner.tool_results), planner.tool_results


def test_create_data_does_not_ask_the_model_to_restate_the_question(test_client, org, planner):
    """The tool fills ``user_prompt`` from the user's message; the model is not
    asked to write it (it cost ~25 output tokens on every data step)."""
    _ask(test_client, org["admin"]["token"], org["org_id"], [], [])
    schema = planner.schemas.get("create_data")
    assert schema, sorted(planner.schemas)
    assert "interpreted_prompt" in schema.get("properties", {})
    assert "user_prompt" not in schema.get("properties", {})
    assert "user_prompt" not in (schema.get("required") or [])


def _app_conversation(test_client, org, who):
    from tests.e2e.dashboards.dashboard_setup import publish

    headers = {"Authorization": f"Bearer {org['admin']['token']}", "X-Organization-Id": org['org_id']}
    report = test_client.post('/api/reports', headers=headers, json={
        'title': 'Support app', 'files': [], 'data_sources': [org['agents']['shared']['ds_id']]}).json()
    created = test_client.post('/api/artifacts', headers=headers, json={
        'report_id': report['id'], 'title': 'Support',
        'content': {'code': '<div>Support</div>', 'visualization_ids': []}})
    assert created.status_code == 200, created.text
    aid = created.json()['artifact_id']
    publish(aid)
    shared = test_client.put(f'/api/artifacts/{aid}/sharing', headers=headers, json={'visibility': 'internal'})
    assert shared.status_code == 200, shared.text
    viewer = {"Authorization": f"Bearer {org[who]['token']}", "X-Organization-Id": org['org_id']}
    base = f'/api/artifacts/{aid}/runtime/conversations'
    thread = test_client.post(base, headers=viewer, json={})
    assert thread.status_code == 201, thread.text
    return f"{base}/{thread.json()['id']}/messages", viewer


@pytest.mark.parametrize('who', ['admin', 'member'])
@pytest.mark.parametrize('target', ['shared', 'private'])
def test_app_conversations_offer_and_execute_only_selected_agent_tools(test_client, org, planner, who, target):
    user = org[who]
    _ask(test_client, user["token"], org["org_id"], [org["agents"]["shared"]["ds_id"]], [])
    regular_catalog = planner.tool_names[0]
    planner.tool_names.clear()
    path, headers = _app_conversation(test_client, org, who)
    connection = org['agents'][target]['conn_id']
    planner.force_tool = ('execute_mcp', {'connection_id': connection, 'tool_name': 'echo',
                                         'arguments': {'message': 'Support lookup'}, 'title': 'Read source'})
    constructed = []
    with _provider(constructed):
        response = test_client.post(path, headers=headers, json={'text': 'Read the support source.'})
    assert response.status_code == 200, response.text
    offered = planner.tool_names[0]
    assert _native(offered, org['agents']['shared']['slug']), sorted(offered)
    assert not _native(offered, org['agents']['private']['slug'])
    assert offered == regular_catalog, {"missing": regular_catalog - offered, "extra": offered - regular_catalog}
    assert (connection in constructed) is (target == 'shared'), planner.tool_results
    if target == 'shared':
        assert any('Support lookup' in result for result in planner.tool_results), planner.tool_results
    else:
        assert any('not found' in result.lower() for result in planner.tool_results), planner.tool_results
