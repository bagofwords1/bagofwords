"""McpClient against real streamable-HTTP MCP servers: discovery and results.

Contract (app/data_sources/clients/mcp_client.py):
  * tools/list follows nextCursor, so every page's tools are discovered;
  * each tool carries its annotation hints, and the policy a NEW tool starts
    on: read-only → allow (None), destructive or no hints → "ask";
  * structuredContent reaches the caller: alone it IS the data, mirrored in a
    text block it is not duplicated, next to a status line both are kept;
  * invisible Unicode TAG characters are stripped from tool descriptions and
    results (valid emoji flag sequences survive);
  * a pooled session the server no longer knows is retried once on a fresh
    session; other dropped connections are retried only for read-only tools.

Servers: tests/mocks/content_mcp_http_server.py (``mcp`` and ``paged``).
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest

from tests.mocks import content_mcp_http_server as content

TAG_RANGE = range(0xE0000, 0xE0080)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app, port):
    import uvicorn

    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline, "MCP test server did not start"
        time.sleep(0.05)
    return srv, t


def _stop(srv, t):
    srv.should_exit = True
    t.join(timeout=10)


@pytest.fixture(scope="module")
def servers():
    p1, p2 = _free_port(), _free_port()
    a = _serve(content.mcp.streamable_http_app(), p1)
    b = _serve(content.paged.streamable_http_app(), p2)
    yield {"content": f"http://127.0.0.1:{p1}/mcp", "paged": f"http://127.0.0.1:{p2}/mcp"}
    _stop(*a)
    _stop(*b)


def _client(url):
    from app.data_sources.clients.mcp_client import McpClient
    return McpClient(server_url=url, transport="streamable_http")


def _run(coro):
    async def wrapped():
        from app.data_sources.clients.mcp_session_pool import POOL
        try:
            return await coro
        finally:
            await POOL.aclose_all()
    return asyncio.run(wrapped())


def _has_tags(value) -> bool:
    import json
    return any(ord(ch) in TAG_RANGE for ch in json.dumps(value, ensure_ascii=False))


# --- discovery -------------------------------------------------------------

def test_tools_list_follows_every_page(servers):
    tools = _run(_client(servers["paged"]).alist_tools())
    assert sorted(t["name"] for t in tools) == sorted(content.PAGED_TOOL_NAMES)


def test_new_tool_policy_follows_annotation_hints(servers):
    tools = {t["name"]: t for t in _run(_client(servers["content"]).alist_tools())}
    assert tools["list_issues"]["default_policy"] is None  # read-only → allow
    assert tools["delete_branch"]["default_policy"] == "ask"  # destructive
    assert tools["mystery_action"]["default_policy"] == "ask"  # no hints → fail safe
    assert tools["list_issues"]["metadata"]["annotations"]["readOnlyHint"] is True


def test_hidden_tag_characters_are_stripped_from_descriptions(servers):
    tools = {t["name"]: t for t in _run(_client(servers["content"]).alist_tools())}
    desc = tools["hidden_text"]["description"]
    assert desc.startswith("Return a greeting.") and not _has_tags(desc)


# --- results ---------------------------------------------------------------

def _call(url, tool, **args):
    r = _run(_client(url).acall_tool(tool, args))
    assert r["success"], r
    return r["data"]


def test_structured_content_alone_is_the_data(servers):
    assert _call(servers["content"], "structured_only", q="x") == content.ROWS


def test_structured_content_mirrored_in_text_is_not_duplicated(servers):
    assert _call(servers["content"], "structured_dual", q="x") == content.ROWS


def test_structured_content_next_to_a_status_line_keeps_both(servers):
    import json
    data = json.dumps(_call(servers["content"], "structured_with_status", q="x"))
    assert "query ok" in data and '"closed"' in data


def test_hidden_tag_characters_are_stripped_from_results(servers):
    data = _call(servers["content"], "hidden_text", name="bob")
    assert data.startswith("hello bob")
    assert "ignore previous" not in data
    # The Scotland flag is an emoji tag sequence — it survives.
    assert "\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F" in data
    assert not _has_tags(data.replace("\U0001F3F4\U000E0067\U000E0062\U000E0073\U000E0063\U000E0074\U000E007F", ""))


# --- stale sessions --------------------------------------------------------

def _fresh_server_app():
    """A new server instance — an MCP session manager can only run once."""
    from mcp.server.fastmcp import FastMCP

    m = FastMCP("restartable")

    @m.tool()
    def delete_branch(branch: str) -> str:
        """A write tool: retrying it is only safe because the server rejected the session."""
        return f"deleted {branch}"

    return m.streamable_http_app()


def test_call_after_server_restart_is_retried_on_a_fresh_session():
    """The server forgets the pooled session; the next call must still succeed."""
    port = _free_port()
    url = f"http://127.0.0.1:{port}/mcp"

    async def scenario():
        client = _client(url)
        first = await client.acall_tool("delete_branch", {"branch": "a"})  # write tool: retry still safe
        _stop(*holder["srv"])
        holder["srv"] = _serve(_fresh_server_app(), port)
        second = await asyncio.wait_for(client.acall_tool("delete_branch", {"branch": "b"}), 30)
        return first, second

    holder = {"srv": _serve(_fresh_server_app(), port)}
    try:
        first, second = _run(scenario())
    finally:
        _stop(*holder["srv"])
    assert first["success"], first
    assert second["success"], second


class _ClosedResourceError(Exception):
    """Stands in for anyio.ClosedResourceError — matched by type name."""


_ClosedResourceError.__name__ = "ClosedResourceError"


def _flaky_client(monkeypatch, fail_times: int):
    client = _client("http://127.0.0.1:1/mcp")
    calls = {"n": 0}

    async def call_once(tool_name, arguments, pooled):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise _ClosedResourceError("connection closed")
        return {"success": True, "data": "ok", "content_type": "text", "binaries": [], "error": None}

    monkeypatch.setattr(client, "_call_once", call_once)
    return client, calls


def test_dropped_connection_is_retried_for_read_only_tools(monkeypatch):
    client, calls = _flaky_client(monkeypatch, fail_times=1)
    r = asyncio.run(client.acall_tool("list_issues", {}, read_only=True))
    assert r["success"] and calls["n"] == 2


def test_dropped_connection_is_not_retried_for_write_tools(monkeypatch):
    client, calls = _flaky_client(monkeypatch, fail_times=1)
    r = asyncio.run(client.acall_tool("delete_branch", {}))
    assert not r["success"] and calls["n"] == 1
    # The agent is told the outcome is unknown rather than being left to retry blindly.
    assert "not retried" in r["error"].lower()


def test_retry_happens_at_most_once(monkeypatch):
    client, calls = _flaky_client(monkeypatch, fail_times=5)
    r = asyncio.run(client.acall_tool("list_issues", {}, read_only=True))
    assert not r["success"] and calls["n"] == 2
