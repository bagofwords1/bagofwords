"""Caller metadata on a completion reaches every MCP tool call of that run.

Contract (see app/utils/mcp_meta.py):
  * ``POST /api/reports/{id}/completions`` accepts ``prompt.metadata`` — a JSON
    object, at most 8KB, without MCP-reserved keys — and stores it with the
    completion;
  * every MCP ``tools/call`` the run makes carries it, unchanged, as ``_meta``
    — also when the prompt was queued and the dispatcher starts it later;
  * a run without metadata sends no caller ``_meta``;
  * the public ``/c/{token}`` share page does not expose it.

Real: routes, services, DB, the queue dispatcher, execute_mcp, McpClient and a
real streamable-HTTP MCP server (tests/mocks/echo_mcp_http_server.py) that
echoes the ``_meta`` it received. Stubbed: only the LLM loop — a scripted
AgentV2 whose "decisions" are execute_mcp calls made through the REAL tool with
the run's own context.
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
from tests.mocks import echo_mcp_http_server as echo

ECHO_TOOL = "query_production_orders"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def echo_url():
    port = _free_port()
    echo.mcp.settings.host = "127.0.0.1"
    echo.mcp.settings.port = port
    server = uvicorn.Server(
        uvicorn.Config(echo.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "echo MCP server did not start"
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def capture(tmp_path, monkeypatch):
    path = tmp_path / "mcp_capture.json"
    monkeypatch.setattr(echo, "_CAPTURE_FILE", str(path))

    class Capture:
        def read(self):
            return json.loads(path.read_text()) if path.exists() else None

    return Capture()


class _ScriptedAgent:
    """Stand-in for AgentV2: makes the scripted execute_mcp calls through the
    REAL tool, with the same runtime context keys the agent passes."""

    calls: list = []
    results: list = []

    def __init__(self, **kw):
        self.kw = kw

    async def main_execution(self):
        from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
        from app.models.agent_execution import AgentExecution
        from app.models.user import User

        db = self.kw["db"]
        head = self.kw["head_completion"]
        system = self.kw["system_completion"]
        report = self.kw["report"]
        org = self.kw["organization"]
        db.add(AgentExecution(
            completion_id=str(system.id), organization_id=str(org.id), user_id=str(head.user_id),
            report_id=str(report.id), status="success",
        ))
        user = await db.get(User, str(head.user_id))
        ctx = {"db": db, "user": user, "organization": org, "report": report,
               "head_completion": head, "system_completion": system}
        for spec in _ScriptedAgent.calls:
            events = [e async for e in ExecuteMCPTool().run_stream(spec, ctx)]
            _ScriptedAgent.results.append(events[-1].payload)
        system.completion = {"content": "done"}
        system.status = "success"
        await db.commit()


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_data_source, create_report,
        refresh_connection_tools, enable_mcp, echo_url, monkeypatch):
    monkeypatch.setattr(completion_mod, "AgentV2", _ScriptedAgent)
    _ScriptedAgent.calls = []
    _ScriptedAgent.results = []

    email = f"mcpmeta_{uuid.uuid4().hex[:8]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]
    headers = _h(token, org_id)

    # A default model so completions can run (no network: the agent is scripted).
    r = test_client.post("/api/llm/providers", headers=headers, json={
        "name": f"openai-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
        "credentials": {"api_key": "sk-test-not-used"},
        "models": [{"model_id": "gpt-6-luna", "name": "GPT-6 Luna", "is_custom": False, "is_default": True}],
    })
    assert r.status_code == 200, r.text
    enable_mcp(token, org_id)

    ds = create_data_source(
        name="Echo MCP", type="mcp",
        config={"server_url": echo_url, "transport": "streamable_http"},
        credentials={}, user_token=token, org_id=org_id,
    )
    conn_id = next(c["id"] for c in ds["connections"])
    refresh_connection_tools(connection_id=conn_id, user_token=token, org_id=org_id)
    report_id = create_report(title="Orders", user_token=token, org_id=org_id, data_sources=[ds["id"]])["id"]
    _ScriptedAgent.calls = [{
        "connection_id": conn_id, "tool_name": ECHO_TOOL,
        "arguments": {"prompt": "weekly production orders", "company": "111"},
    }]

    class E:
        pass

    e = E()
    e.client, e.headers, e.report_id, e.conn_id = test_client, headers, report_id, conn_id

    def complete(prompt: dict, **body):
        return test_client.post(f"/api/reports/{report_id}/completions", headers=headers,
                                json={"prompt": prompt, **body})

    e.complete = complete
    return e


META = {"chat_id": "c-77", "message_id": "m-912", "client_user": {"id": 5, "tags": ["a", "ב"]}}


@pytest.mark.e2e
def test_metadata_reaches_mcp_server_as_meta(env, capture):
    r = env.complete({"content": "weekly orders", "metadata": META})
    assert r.status_code == 200, r.text

    assert _ScriptedAgent.results and _ScriptedAgent.results[-1]["output"]["success"], _ScriptedAgent.results
    got = capture.read()
    assert got is not None, "the MCP server never received the tool call"
    assert got["received_meta"] == META
    # The tool's own arguments are untouched by the caller metadata.
    assert got["received_arguments"]["prompt"] == "weekly production orders"
    assert got["received_arguments"]["custom_metadata"] == {}


@pytest.mark.e2e
def test_run_without_metadata_sends_no_caller_meta(env, capture):
    r = env.complete({"content": "weekly orders"})
    assert r.status_code == 200, r.text

    got = capture.read()
    assert got is not None, "the MCP server never received the tool call"
    assert not set(got["received_meta"]) & set(META)


@pytest.mark.e2e
def test_metadata_is_stored_with_the_completion(env, capture):
    r = env.complete({"content": "weekly orders", "metadata": META})
    assert r.status_code == 200, r.text

    timeline = env.client.get(f"/api/reports/{env.report_id}/completions", headers=env.headers)
    assert timeline.status_code == 200, timeline.text
    prompts = [c["prompt"] for c in timeline.json()["completions"] if c["role"] == "user"]
    assert prompts and prompts[-1]["metadata"] == META


def _drive_dispatcher(report_id):
    """Run the queue dispatcher on a private loop until its dispatched run ends
    (same driver as test_completion_queue_steer.py)."""
    import asyncio

    async def _run():
        await completion_mod.CompletionService().start_next_queued_if_idle(report_id, None)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=30)

    asyncio.run(_run())


@pytest.mark.e2e
def test_queued_prompt_keeps_its_metadata(env, capture, monkeypatch):
    # The route fires the dispatcher as a fire-and-forget task whose fate under
    # the sync TestClient depends on the DB backend. Hold it back so the queued
    # row stays queued, then drive the dispatcher deterministically: it starts
    # the run from the row as stored in the DB.
    real_dispatch = completion_mod.CompletionService.start_next_queued_if_idle

    async def _hold(self, *a, **kw):
        return None

    monkeypatch.setattr(completion_mod.CompletionService, "start_next_queued_if_idle", _hold)
    r = env.complete({"content": "weekly orders", "metadata": META}, queue=True)
    assert r.status_code == 200, r.text
    monkeypatch.setattr(completion_mod.CompletionService, "start_next_queued_if_idle", real_dispatch)

    queued = [c for c in env.client.get(f"/api/reports/{env.report_id}/completions",
                                        headers=env.headers).json()["completions"]
              if c["role"] == "user"]
    assert queued[-1]["status"] == "queued"
    assert capture.read() is None

    _drive_dispatcher(env.report_id)

    got = capture.read()
    assert got is not None, "the queued run never called the MCP server"
    assert got["received_meta"] == META


@pytest.mark.e2e
def test_public_share_page_hides_metadata(env, capture):
    assert env.complete({"content": "weekly orders", "metadata": META}).status_code == 200
    share = env.client.post(f"/api/reports/{env.report_id}/conversation-share", headers=env.headers)
    assert share.status_code == 200, share.text

    page = env.client.get(f"/api/c/{share.json()['token']}")
    assert page.status_code == 200, page.text
    prompts = [c["prompt"] for c in page.json()["completions"] if c["role"] == "user"]
    assert prompts and prompts[-1]["content"] == "weekly orders"
    assert all("metadata" not in p for p in prompts)


@pytest.mark.e2e
@pytest.mark.parametrize(
    "metadata, reason",
    [
        ("c-77", "not an object"),
        (["c-77"], "not an object"),
        ({"blob": "x" * (8 * 1024)}, "over 8KB"),
        ({"progressToken": "t1"}, "MCP protocol key"),
        ({"modelcontextprotocol.io/trace": "t1"}, "MCP-reserved prefix"),
        ({"api.mcp.dev/trace": "t1"}, "MCP-reserved prefix"),
    ],
)
def test_invalid_metadata_is_rejected(env, capture, metadata, reason):
    r = env.complete({"content": "weekly orders", "metadata": metadata})
    assert r.status_code == 422, (reason, r.text)
    assert capture.read() is None, "a rejected request must not run"


@pytest.mark.e2e
@pytest.mark.parametrize(
    "metadata",
    [
        {"acme.com/trace": "t1"},          # a caller's own prefix is fine
        {"mcp_session": "s1"},             # 'mcp' only reserved as a prefix label
        {"blob": "x" * (8 * 1024 - 20)},   # just under the limit
    ],
)
def test_valid_edge_metadata_is_accepted(env, capture, metadata):
    r = env.complete({"content": "weekly orders", "metadata": metadata})
    assert r.status_code == 200, r.text
    assert capture.read()["received_meta"] == metadata
