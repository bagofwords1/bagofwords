"""McpClient speaks MCP 2026-07-28 and older servers, and advertises MCP Apps.

Runs the real MCP Apps server (tests/mocks/mcp_apps_server.py) in-process,
once as itself (a 2026-07-28 server that also serves older revisions) and
once behind a shim that answers ``server/discover`` the way a server from
before 2026-07-28 does (method not found), so the client must fall back to
the ``initialize`` handshake.

Contract, in both eras:
  * the client announces ``io.modelcontextprotocol/ui`` with the MCP App MIME
    type, so the server enables its UI tools;
  * discovery keeps each tool's UI binding and visibility;
  * a tool call hands back the result verbatim (``raw_result``) for the app;
  * the ``ui://`` document is read with its declared sandbox metadata.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import pytest

from tests.mocks import mcp_apps_server as sales


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _legacy_only(inner):
    """An ASGI wrapper that makes the server look like a pre-2026-07-28 one."""

    async def app(scope, receive, send):
        if scope["type"] != "http" or scope.get("method") != "POST":
            return await inner(scope, receive, send)
        body, more = b"", True
        while more:
            m = await receive()
            body += m.get("body", b"")
            more = m.get("more_body", False)
        try:
            msg = json.loads(body)
        except ValueError:
            msg = {}
        if isinstance(msg, dict) and msg.get("method") == "server/discover":
            payload = json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
                                  "error": {"code": -32601, "message": "Method not found"}}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": payload})
            return
        sent = {"done": False}

        async def replay():
            if not sent["done"]:
                sent["done"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        return await inner(scope, replay, send)

    return app


def _serve(app):
    import uvicorn

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline, "MCP test server did not start"
        time.sleep(0.05)
    return srv, t, f"http://127.0.0.1:{port}/mcp"


@pytest.fixture(scope="module", params=["modern", "legacy"])
def server(request):
    app = sales.mcp.streamable_http_app()
    if request.param == "legacy":
        app = _legacy_only(app)
    srv, t, url = _serve(app)
    yield {"era": request.param, "url": url}
    srv.should_exit = True
    t.join(timeout=10)


def _run(coro):
    async def wrapped():
        from app.data_sources.clients.mcp_session_pool import POOL
        try:
            return await coro
        finally:
            await POOL.aclose_all()
    return asyncio.run(wrapped())


def _client(url):
    from app.data_sources.clients.mcp_client import McpClient
    return McpClient(server_url=url, transport="streamable_http")


def test_client_negotiates_the_era_and_advertises_mcp_apps(server):
    result = _run(_client(server["url"]).acall_tool("client_handshake", {}, read_only=True))
    assert result["success"], result
    seen = result["data"]
    if isinstance(seen, dict) and "content" in seen:
        seen = seen["content"]
    if isinstance(seen, str):
        seen = json.loads(seen)
    if server["era"] == "modern":
        assert seen["protocol_version"] >= "2026-07-28"
    else:
        assert seen["protocol_version"] < "2026-07-28"
    assert seen["client_supports_apps"] is True
    assert "text/html;profile=mcp-app" in seen["extensions"]["io.modelcontextprotocol/ui"]["mimeTypes"]


def test_discovery_keeps_ui_binding_and_visibility(server):
    tools = {t["name"]: t for t in _run(_client(server["url"]).alist_tools())}
    from app.services.mcp_apps import app_callable, model_visible, tool_resource_uri

    dash = tools["show_sales_dashboard"]["metadata"]
    assert tool_resource_uri(dash) == sales.APP_URI
    assert model_visible(dash) and app_callable(dash)

    for app_only in ("refresh_sales", "create_followup_task"):
        meta = tools[app_only]["metadata"]
        assert not model_visible(meta), app_only
        assert app_callable(meta), app_only

    plain = tools["client_handshake"]["metadata"]
    assert tool_resource_uri(plain) is None and model_visible(plain)


def test_tool_call_returns_the_result_verbatim_for_the_app(server):
    result = _run(_client(server["url"]).acall_tool("show_sales_dashboard", {"quarter": "Q2"}, read_only=True))
    raw = result["raw_result"]
    assert raw["structuredContent"]["quarter"] == "Q2"
    regions = {r["region"] for r in raw["structuredContent"]["rows"]}
    assert regions == {r["region"] for r in sales.sales_rows("Q2")}
    assert any(b.get("type") == "text" for b in raw["content"])


def test_app_document_is_read_with_its_sandbox_metadata(server):
    from app.services.mcp_apps import clear_resource_cache, read_app_resource

    clear_resource_cache()
    res = _run(read_app_resource(_client(server["url"]), f"conn-{server['era']}", sales.APP_URI))
    assert "<html" in res["html"].lower()
    assert res["prefersBorder"] is True
    assert set(res["csp"]) == {"connectDomains", "resourceDomains", "frameDomains", "baseUriDomains"}
