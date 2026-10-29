"""How often a run rebuilds its warm context (history, queries, mentions, entities).

Each rebuild re-runs every warm builder serially on the run's DB session, so it
sits on the critical path between the model's decision and the next model
call. This pins the rebuild count as a performance contract — the count IS the
behavior here, which is why the test observes the hub's public refresh entry:

  * a step that calls a tool which never reads the turn's context (an MCP
    call) adds exactly ONE rebuild: the one after the tool. Before, it added
    three — before the tool, after it, and again at the next loop start with
    nothing changed in between;
  * a step whose tool reads the context (here describe_tables) still gets its
    pre-tool rebuild, so it adds two;
  * the end-of-turn snapshot reuses the last view instead of rebuilding, so a
    turn with no tools rebuilds once (it used to rebuild twice).

The LLM boundary and the MCP provider client are stubbed; routes, services and
the agent loop run real.
"""
from __future__ import annotations

import contextlib
import uuid
from unittest.mock import patch

import pytest

from tests.mocks.mock_mcp_server import MockToolProviderClient

pytestmark = pytest.mark.e2e


class _Planner:
    def __init__(self):
        self.force_tool: tuple | None = None

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            has_result = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for m in messages or []
                for b in ((m.get("content") if isinstance(m, dict) else getattr(m, "content", None)) or [])
                if isinstance(b, dict)
            )
            if not has_result and planner.force_tool:
                name, args = planner.force_tool
                planner.force_tool = None
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
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


@pytest.fixture
def rebuilds(monkeypatch):
    from app.ai.context.context_hub import ContextHub

    count = {"n": 0}
    real = ContextHub.refresh_warm

    async def counting(self, *a, **k):
        count["n"] += 1
        return await real(self, *a, **k)

    monkeypatch.setattr(ContextHub, "refresh_warm", counting)
    return count


@contextlib.contextmanager
def _provider():
    client = MockToolProviderClient()

    async def _construct(self, db, connection, current_user=None, **kwargs):
        return client

    with patch("app.services.connection_service.ConnectionService.construct_client", _construct):
        yield client


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_llm_provider_and_models, enable_mcp,
        create_mcp_connection, create_domain_from_connection, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    enable_mcp(user_token=token, org_id=org_id)
    h = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    slug = f"music{uuid.uuid4().hex[:4]}"
    conn = create_mcp_connection(name=slug, server_url=f"http://{slug}:3000/mcp", user_token=token, org_id=org_id)
    with _provider():
        assert test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=h).status_code == 200
    ds = create_domain_from_connection(name=f"{slug} agent", connection_id=conn["id"], user_token=token, org_id=org_id)
    return {"h": h, "ds_id": str(ds["id"]), "conn_id": str(conn["id"]), "slug": slug}


def _ask(test_client, env):
    r = test_client.post("/api/reports", headers=env["h"],
                         json={"title": "refresh", "files": [], "data_sources": [env["ds_id"]]})
    assert r.status_code == 200, r.text
    with _provider():
        r = test_client.post(f"/api/reports/{r.json()['id']}/completions", headers=env["h"],
                             params={"background": False},
                             json={"prompt": {"content": "Echo hello with the tools."}})
    assert r.status_code == 200, r.text


def _rebuilds_for(test_client, env, planner, rebuilds, tool=None):
    planner.force_tool = tool
    before = rebuilds["n"]
    _ask(test_client, env)
    return rebuilds["n"] - before


@pytest.mark.parametrize("tool", [
    ("execute_mcp", {"tool_name": "echo", "arguments": {"message": "hi"}, "title": "Echo"}),
    ("native", {"message": "hi"}),
], ids=["gateway", "native"])
def test_a_tool_that_ignores_the_context_adds_one_rebuild(test_client, env, planner, rebuilds, tool):
    name, args = tool
    if name == "execute_mcp":
        args = {**args, "connection_id": env["conn_id"]}
    else:
        name = f"mcp__{env['slug']}__echo"
    baseline = _rebuilds_for(test_client, env, planner, rebuilds)
    with_tool = _rebuilds_for(test_client, env, planner, rebuilds, (name, args))
    assert with_tool - baseline == 1, (baseline, with_tool)


def test_a_tool_that_reads_the_context_keeps_its_pre_tool_rebuild(test_client, env, planner, rebuilds):
    baseline = _rebuilds_for(test_client, env, planner, rebuilds)
    with_tool = _rebuilds_for(test_client, env, planner, rebuilds, ("describe_tables", {"query": "anything"}))
    assert with_tool - baseline == 2, (baseline, with_tool)


def test_a_turn_without_tools_rebuilds_once(test_client, env, planner, rebuilds):
    assert _rebuilds_for(test_client, env, planner, rebuilds) == 1


def test_a_rescued_iteration_rebuilds_instead_of_reusing_the_carried_view(test_client, env, planner, rebuilds, monkeypatch):
    # The step after a tool reuses the post-tool view. When that iteration
    # crashes and is rescued (the session may be rolled back), the retry must
    # rebuild from persisted context, not reuse the view from before the crash.
    import app.ai.agent_v2 as agent_v2

    tool = (f"mcp__{env['slug']}__echo", {"message": "hi"})
    without_crash = _rebuilds_for(test_client, env, planner, rebuilds, tool)
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_BUDGET", 1)
    monkeypatch.setattr(agent_v2, "_LOOP_FAULT_MIN_INDEX", 1)
    with_crash = _rebuilds_for(test_client, env, planner, rebuilds, tool)
    assert agent_v2._LOOP_FAULT_BUDGET == 0, "the fault did not fire"
    assert with_crash == without_crash + 1, (without_crash, with_crash)
