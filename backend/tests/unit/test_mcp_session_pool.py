"""MCP tool calls reuse one session per (server, credentials), within a cap.

Runs a real streamable-HTTP MCP server in-process and counts the JSON-RPC
messages it receives, so the assertions are about what crosses the wire:

  * repeated calls to one server open one session (one ``initialize``) and the
    SDK lists tools once, not after every call;
  * different credentials never share a session;
  * the cap closes the least recently used session, and pooling can be off;
  * every call still returns its tool's result.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections import Counter

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def mcp_server():
    import uvicorn
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("pool-test")

    @server.tool()
    def add(a: int, b: int) -> str:
        """Add two numbers."""
        return str(a + b)

    seen: Counter = Counter()
    inner = server.streamable_http_app()

    async def counting(scope, receive, send):
        if scope["type"] == "http" and scope.get("method") == "POST":
            body, more = b"", True
            while more:
                m = await receive()
                body += m.get("body", b"")
                more = m.get("more_body", False)
            try:
                msgs = json.loads(body)
                for m in (msgs if isinstance(msgs, list) else [msgs]):
                    seen[(dict(scope["headers"]).get(b"authorization", b"").decode(), m.get("method"))] += 1
            except Exception:
                pass
            sent = {"done": False}

            async def replay():
                if not sent["done"]:
                    sent["done"] = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            return await inner(scope, replay, send)
        return await inner(scope, receive, send)

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(counting, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline, "MCP test server did not start"
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}/mcp", "seen": seen}
    srv.should_exit = True
    t.join(timeout=10)


def _count(seen, method, token=""):
    return seen[(f"Bearer {token}" if token else "", method)]


def _run(coro):
    async def wrapped():
        from app.data_sources.clients.mcp_session_pool import POOL
        try:
            return await coro
        finally:
            await POOL.aclose_all()
    return asyncio.run(wrapped())


def _client(url, token=None):
    from app.data_sources.clients.mcp_client import McpClient
    return McpClient(server_url=url, transport="streamable_http", token=token)


@pytest.fixture(autouse=True)
def _fresh_counts(mcp_server, monkeypatch):
    monkeypatch.delenv("BOW_MCP_POOL_SIZE", raising=False)
    mcp_server["seen"].clear()


def _data(result):
    assert result.get("success", True) is not False, result
    return json.dumps(result.get("data", result.get("contents")))


@pytest.mark.parametrize("calls", [2, 5])
def test_repeated_calls_share_one_session_and_list_tools_once(mcp_server, calls):
    async def go():
        c = _client(mcp_server["url"])
        return [await c.acall_tool("add", {"a": i, "b": 1}) for i in range(calls)]

    results = _run(go())
    assert [str(i + 1) in _data(r) for i, r in enumerate(results)] == [True] * calls
    assert _count(mcp_server["seen"], "initialize") == 1
    assert _count(mcp_server["seen"], "tools/list") <= 1
    assert _count(mcp_server["seen"], "tools/call") == calls


def test_concurrent_calls_on_one_session_all_return(mcp_server):
    async def go():
        c = _client(mcp_server["url"])
        return await asyncio.gather(*(c.acall_tool("add", {"a": i, "b": 10}) for i in range(4)))

    results = _run(go())
    assert all(str(i + 10) in _data(r) for i, r in enumerate(results))
    assert _count(mcp_server["seen"], "initialize") == 1


def test_different_credentials_never_share_a_session(mcp_server):
    async def go():
        for token in ("alice-token", "bob-token", "alice-token"):
            await _client(mcp_server["url"], token=token).acall_tool("add", {"a": 1, "b": 1})

    _run(go())
    seen = mcp_server["seen"]
    assert _count(seen, "initialize", "alice-token") == 1
    assert _count(seen, "initialize", "bob-token") == 1
    assert _count(seen, "tools/call", "alice-token") == 2


def test_cap_closes_the_least_recently_used_session(mcp_server, monkeypatch):
    monkeypatch.setenv("BOW_MCP_POOL_SIZE", "1")

    async def go():
        for token in ("a", "b", "a"):
            await _client(mcp_server["url"], token=token).acall_tool("add", {"a": 2, "b": 2})

    _run(go())
    # One slot: "b" evicts "a", so "a" opens again.
    assert _count(mcp_server["seen"], "initialize", "a") == 2
    assert _count(mcp_server["seen"], "initialize", "b") == 1


def test_pool_size_zero_opens_a_session_per_call(mcp_server, monkeypatch):
    monkeypatch.setenv("BOW_MCP_POOL_SIZE", "0")

    async def go():
        c = _client(mcp_server["url"])
        for _ in range(3):
            await c.acall_tool("add", {"a": 1, "b": 2})

    _run(go())
    assert _count(mcp_server["seen"], "initialize") == 3
