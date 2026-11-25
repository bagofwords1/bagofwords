"""An agent's MCP tools reach the model whenever the run can call them, and
the model is never told a tool is callable when it is not.

Contract:
  * one connection on an agent that cannot be set up for this run (its client
    fails to build) does not take the agent's other connections down with it:
    their MCP tools are still in the tool list, for admins and members, in
    Auto and with a pinned selection;
  * every MCP tool a focus / discovery observation (set_report_agents,
    search_agents) lists as callable is in the next request's tool list, or
    is marked deferred and reachable with search_tools;
  * an agent attached mid-run (training mode) brings its MCP tools into the
    tool list from the next step.

The LLM boundary and the MCP provider are stubbed (the provider's client is
the driver boundary: making it fail stands in for an unreachable server or a
missing sign-in). Routes, services, the agent loop and the tool gateway run
real.
"""
from __future__ import annotations

import contextlib
import json
import re
import uuid
from unittest.mock import patch

import pytest

from tests.mocks.mock_mcp_server import MockToolProviderClient

pytestmark = pytest.mark.e2e

_TOOLS = ("echo", "failing_tool", "get_records", "search_docs")


class _Planner:
    """Scripted planner: replays queued tool calls, one per request, then answers."""

    def __init__(self):
        self.requests: list[list[str]] = []
        self.results: list[list[str]] = []
        self.queue: list = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            names = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])]
            planner.requests.append([n for n in names if n])
            planner.results.append([
                json.dumps(b.get("content"))
                for m in messages or []
                for b in ((m.get("content") if isinstance(m, dict) else getattr(m, "content", None)) or [])
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ])
            if planner.queue:
                name, args = planner.queue.pop(0)
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text="Done."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for e in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield e

        return scripted

    def mcp_tools(self, i=0):
        return {n for n in self.requests[i] if n.startswith("mcp__")}


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
def _provider():
    client = MockToolProviderClient()

    async def _construct(self, db, connection, current_user=None, **kwargs):
        return client

    with patch("app.services.connection_service.ConnectionService.construct_client", _construct):
        yield client


@contextlib.contextmanager
def _unreachable(*hosts):
    """The MCP driver cannot be set up for these servers in this run."""
    from app.data_sources.clients.mcp_client import McpClient

    real_init = McpClient.__init__

    def _init(self, server_url, *args, **kwargs):
        if any(h in (server_url or "") for h in hosts):
            raise ConnectionError(f"cannot reach {server_url}")
        real_init(self, server_url, *args, **kwargs)

    with patch.object(McpClient, "__init__", _init):
        yield


@pytest.fixture
def org(test_client, bootstrap_admin, invite_user_to_org, create_llm_provider_and_models,
        enable_mcp, create_mcp_connection, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("mcpreach")
    token, org_id = admin["token"], admin["org_id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    enable_mcp(user_token=token, org_id=org_id)
    h = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    def connection():
        slug = f"c{uuid.uuid4().hex[:6]}"
        conn = create_mcp_connection(name=slug, server_url=f"http://{slug}:3000/mcp", user_token=token, org_id=org_id)
        with _provider():
            assert test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=h).status_code == 200
        return {"slug": slug, "id": str(conn["id"])}

    member = invite_user_to_org(org_id=org_id, admin_token=token)
    return {"admin": admin, "member": member, "org_id": org_id, "h": h, "connection": connection}


def _agent(test_client, org, connections, *, public=True):
    r = test_client.post("/api/data_sources", headers=org["h"], json={
        "name": f"agent {uuid.uuid4().hex[:6]}",
        "connection_ids": [c["id"] for c in connections],
        # An analysis agent with MCP tools attached, not a personal connector.
        "kind": "agent",
        "generate_summary": False, "generate_conversation_starters": False,
        "generate_ai_rules": False, "use_llm_sync": False, "is_public": public,
    })
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


def _report(test_client, user, org_id, data_sources, **extra):
    h = {"Authorization": f"Bearer {user['token']}", "X-Organization-Id": str(org_id)}
    r = test_client.post("/api/reports", headers=h, json={"title": "reach", "files": [], "data_sources": data_sources, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"], h


def _ask(test_client, report_id, h, mode="chat"):
    with _provider():
        r = test_client.post(f"/api/reports/{report_id}/completions", headers=h, params={"background": False},
                             json={"prompt": {"content": "Use the tools.", "mode": mode}})
    assert r.status_code == 200, r.text


def _names(slug):
    return {f"mcp__{slug}__{t}" for t in _TOOLS}


_CONN_RE = re.compile(r'<connection name="([^"]+)"[^>]*>(.*?)</connection>', re.S)
_TOOL_RE = re.compile(r'<tool name="([^"]+)"([^>]*)>')


def _listed_as_callable(observation: str) -> set[str]:
    """mcp__<connection>__<tool> for every tool an observation's <mcp_tools>
    lists without deferred="true" — the ones the model is told to call directly."""
    out = set()
    # Tool results reach the planner JSON-encoded (possibly twice).
    observation = observation.replace("\\", "")
    for conn, body in _CONN_RE.findall(observation):
        for tool, attrs in _TOOL_RE.findall(body):
            if "deferred" not in attrs:
                out.add(f"mcp__{conn}__{tool}")
    return out


@pytest.mark.parametrize("role", ["admin", "member"])
@pytest.mark.parametrize("selection", ["auto", "pinned"])
def test_a_failing_connection_does_not_hide_the_agents_other_tools(test_client, org, planner, role, selection):
    healthy, broken, sibling = org["connection"](), org["connection"](), org["connection"]()
    ds_id = _agent(test_client, org, [healthy, broken])
    # A second agent in the run, as in any workspace with more than one agent.
    other = _agent(test_client, org, [sibling])
    report_id, h = _report(test_client, org[role], org["org_id"], [ds_id, other] if selection == "pinned" else [])

    with _unreachable(broken["slug"]):
        _ask(test_client, report_id, h)

    offered = planner.mcp_tools(0)
    assert _names(healthy["slug"]) <= offered, offered
    assert _names(sibling["slug"]) <= offered, offered


def test_healthy_agents_are_unaffected_by_another_agents_failing_connection(test_client, org, planner):
    """Bounded scope: the fix keeps sibling agents exactly as they were."""
    a, b = org["connection"](), org["connection"]()
    _agent(test_client, org, [a])
    _agent(test_client, org, [b])
    report_id, h = _report(test_client, org["admin"], org["org_id"], [])

    with _unreachable(b["slug"]):
        _ask(test_client, report_id, h)

    offered = planner.mcp_tools(0)
    assert _names(a["slug"]) <= offered
    assert not _names(b["slug"]) & offered, "an agent with no usable connection stays out of the run"


@pytest.mark.parametrize("tool", ["set_report_agents", "search_agents"])
def test_an_observation_never_lists_a_tool_the_next_step_cannot_call(test_client, org, planner, tool):
    """An agent whose only connection fails is not in the run. Focusing it, or
    finding it with search_agents, must not present its tools as callable."""
    healthy, broken = org["connection"](), org["connection"]()
    _agent(test_client, org, [healthy])
    dropped = _agent(test_client, org, [broken])
    report_id, h = _report(test_client, org["admin"], org["org_id"], [])
    planner.queue = [
        ("set_report_agents", {"agent_ids": [dropped], "reason": "need it"}) if tool == "set_report_agents"
        else ("search_agents", {"query": ["agent"]}),
    ]

    with _unreachable(broken["slug"]):
        _ask(test_client, report_id, h)

    assert len(planner.requests) >= 2, "the scripted call should have produced a follow-up request"
    observation = "\n".join(planner.results[1])
    assert observation and "[failed]" not in observation, observation[:400]
    listed = _listed_as_callable(observation)
    next_tools = planner.mcp_tools(1)
    assert listed <= next_tools, f"listed as callable but not offered: {sorted(listed - next_tools)}"
    assert not _names(broken["slug"]) & next_tools


def test_an_agent_created_mid_run_brings_its_tools_into_the_next_step(test_client, org, planner):
    """create_agent attaches a new agent to the report during the run."""
    first, second = org["connection"](), org["connection"]()
    a = _agent(test_client, org, [first])
    report_id, h = _report(test_client, org["admin"], org["org_id"], [a], mode="training")
    planner.queue = [("create_agent", {"name": f"new {uuid.uuid4().hex[:6]}", "connection_ids": [second["id"]]})]

    _ask(test_client, report_id, h, mode="training")

    assert _names(first["slug"]) <= planner.mcp_tools(0)
    assert not _names(second["slug"]) & planner.mcp_tools(0)
    assert len(planner.requests) >= 2
    created = "".join(planner.results[1]).replace("\\", "")
    assert planner.results[1] and '"success": false' not in created and "[denied]" not in created, created[:400]
    assert _names(second["slug"]) <= planner.mcp_tools(1), planner.mcp_tools(1)
    assert _names(first["slug"]) <= planner.mcp_tools(1)




def test_a_failed_rebuild_keeps_the_tools_the_run_already_had(test_client, org, planner):
    """The catalog rebuild for an agent attached mid-run can fail (its server
    times out while tools are listed). The agents already in the run keep
    their tools, under the same names, for the following steps."""
    from app.ai.tools import mcp_tool_registry

    first, second = org["connection"](), org["connection"]()
    a = _agent(test_client, org, [first])
    report_id, h = _report(test_client, org["admin"], org["org_id"], [a], mode="training")
    planner.queue = [("create_agent", {"name": f"new {uuid.uuid4().hex[:6]}", "connection_ids": [second["id"]]})]

    real, builds = mcp_tool_registry.build_mcp_tool_catalog, []

    async def first_build_only(*args, **kwargs):
        builds.append(1)
        if len(builds) > 1:
            raise RuntimeError("tool listing timed out")
        return await real(*args, **kwargs)

    with patch.object(mcp_tool_registry, "build_mcp_tool_catalog", first_build_only):
        _ask(test_client, report_id, h, mode="training")

    assert len(builds) >= 2, "the mid-run attach should have asked for a rebuild"
    assert len(planner.requests) >= 2
    assert _names(first["slug"]) <= planner.mcp_tools(0)
    assert _names(first["slug"]) <= planner.mcp_tools(1), planner.mcp_tools(1)
