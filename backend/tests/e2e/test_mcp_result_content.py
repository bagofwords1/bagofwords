"""Every MCP tool-result content shape reaches the agent; nothing is dropped silently.

Contract (app/data_sources/clients/mcp_client.py, execute_mcp):
  * an embedded TEXT resource (GitHub get_file_contents) is part of the result
    the agent reads — not just the "successfully downloaded" status line;
  * image blocks and embedded blob resources become session files the agent
    opens with read_file (no base64 in the result data); every file in a
    result is saved, not only the first;
  * a resource link tells the agent where to fetch the content;
  * a file of a type the app cannot open is reported, not skipped silently.

Real: routes, services, DB, execute_mcp, read_file, McpClient and a real
streamable-HTTP MCP server (tests/mocks/content_mcp_http_server.py). Stubbed:
only the LLM loop — a scripted agent that makes one execute_mcp call through
the REAL tool, then read_file on each file it got back.
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
    content.mcp.settings.host = "127.0.0.1"
    content.mcp.settings.port = port
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
    """Stand-in for AgentV2: execute_mcp through the REAL tool, then read_file."""

    call: dict = {}
    results: list = []
    reads: list = []

    def __init__(self, **kw):
        self.kw = kw

    async def main_execution(self):
        from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
        from app.ai.tools.implementations.read_file import ReadFileTool
        from app.models.user import User

        db = self.kw["db"]
        head = self.kw["head_completion"]
        system = self.kw["system_completion"]
        report = self.kw["report"]
        org = self.kw["organization"]
        user = await db.get(User, str(head.user_id))
        ctx = {"db": db, "user": user, "organization": org, "report": report,
               "head_completion": head, "system_completion": system}
        events = [e async for e in ExecuteMCPTool().run_stream(json.loads(json.dumps(_ScriptedAgent.call)), ctx)]
        payload = events[-1].payload
        _ScriptedAgent.results.append(payload)
        for f in (payload["output"].get("attached_files") or []):
            read_events = [e async for e in ReadFileTool().run_stream({"file_id": f["file_id"]}, ctx)]
            _ScriptedAgent.reads.append(read_events[-1].payload)
        system.completion = {"content": "done"}
        system.status = "success"
        await db.commit()


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_data_source, create_report,
        refresh_connection_tools, enable_mcp, content_url, monkeypatch):
    monkeypatch.setattr(completion_mod, "AgentV2", _ScriptedAgent)
    _ScriptedAgent.results, _ScriptedAgent.reads = [], []

    email = f"mcpcontent_{uuid.uuid4().hex[:8]}@test.com"
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

    ds = create_data_source(name="Content MCP", type="mcp",
                            config={"server_url": content_url, "transport": "streamable_http"},
                            credentials={}, user_token=token, org_id=org_id)
    conn_id = next(c["id"] for c in ds["connections"])
    refresh_connection_tools(connection_id=conn_id, user_token=token, org_id=org_id)
    report_id = create_report(title="Content", user_token=token, org_id=org_id, data_sources=[ds["id"]])["id"]

    def call(tool_name, **arguments):
        """Run one turn whose only action is execute_mcp(tool_name); return the tool payload."""
        _ScriptedAgent.call = {"connection_id": conn_id, "tool_name": tool_name, "arguments": arguments}
        before = len(_ScriptedAgent.results)
        r = test_client.post(f"/api/reports/{report_id}/completions", headers=headers,
                             json={"prompt": {"content": f"run {tool_name}"}})
        assert r.status_code == 200, r.text
        assert len(_ScriptedAgent.results) == before + 1, "scripted agent did not run"
        payload = _ScriptedAgent.results[-1]
        assert payload["output"]["success"], payload
        return payload

    return call


def _agent_text(payload) -> str:
    """Everything the agent is shown for a tool call: preview + summary."""
    return json.dumps(payload["output"].get("preview"), default=str) + " " + payload["observation"]["summary"]


@pytest.mark.e2e
def test_github_style_file_body_reaches_the_agent(env):
    payload = env("get_file_contents", owner="bagofwords1", repo="bagofwords", path="README.md")
    assert "MARKER-README-BODY-7f3a" in _agent_text(payload)


@pytest.mark.e2e
def test_image_becomes_a_readable_session_file_not_base64(env):
    payload = env("take_screenshot", url="https://example.com")
    out = payload["output"]
    assert content.PNG_B64 not in json.dumps(out, default=str)
    files = out.get("attached_files") or []
    assert len(files) == 1 and files[0]["mime_type"] == "image/png"
    assert files[0]["file_id"] in payload["observation"]["summary"]
    assert _ScriptedAgent.reads and all(r["output"].get("success", True) is not False for r in _ScriptedAgent.reads)


@pytest.mark.e2e
def test_embedded_pdf_is_saved_next_to_the_text(env):
    payload = env("export_pdf", report="q3")
    out = payload["output"]
    assert "exported q3" in _agent_text(payload)
    files = out.get("attached_files") or []
    assert [f["mime_type"] for f in files] == ["application/pdf"]
    # The text result keeps its own artifact; the PDF is an additional file.
    assert out.get("file_id") and out["file_id"] != files[0]["file_id"]


@pytest.mark.e2e
def test_every_file_in_a_result_is_saved(env):
    payload = env("three_charts", topic="revenue")
    files = payload["output"].get("attached_files") or []
    assert len(files) == 3
    assert len({f["file_id"] for f in files}) == 3
    assert len(_ScriptedAgent.reads) == 3


@pytest.mark.e2e
def test_resource_link_tells_the_agent_where_to_fetch(env):
    payload = env("link_dataset", name="orders")
    text = _agent_text(payload)
    assert "data://datasets/orders.csv" in text
    assert "read_mcp_resource" in text


@pytest.mark.e2e
def test_unopenable_file_is_reported_not_dropped(env):
    payload = env("export_zip", name="backup")
    out = payload["output"]
    assert not out.get("attached_files")
    dropped = out.get("dropped_files") or []
    assert [d["mime_type"] for d in dropped] == ["application/zip"]
    assert "application/zip" in payload["observation"]["summary"]
