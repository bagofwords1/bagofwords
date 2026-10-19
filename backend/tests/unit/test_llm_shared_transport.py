"""LLM provider clients share one connection pool per process (and event loop).

Contract:
  * two SDK clients built for different messages reuse the same TCP
    connection, so the second message pays no TCP + TLS setup;
  * closing one client does not close the shared pool for the others;
  * preconnect opens the connection the next call reuses, without sending
    the API key;
  * the async pool is per event loop: a call on another loop works.

A local keep-alive HTTP server counts the connections it accepts.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest


@pytest.fixture
def server():
    seen = {"peers": set(), "auth": []}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self):
            seen["peers"].add(self.client_address)
            seen["auth"].append(self.headers.get("x-api-key") or self.headers.get("authorization"))
            length = int(self.headers.get("content-length") or 0)
            if length:
                self.rfile.read(length)
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        do_GET = do_POST = do_HEAD = _reply

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{srv.server_address[1]}", "seen": seen}
    srv.shutdown()


def _client():
    from app.ai.llm.clients.openai_client import OpenAi
    return OpenAi(api_key="sk-secret")


def test_clients_for_different_messages_reuse_one_connection(server):
    async def go():
        for _ in range(3):
            c = _client()  # a new SDK client per message
            r = await c.async_client._client.get(server["url"] + "/v1/ping")
            assert r.status_code == 200
            await c.async_client.close()  # one client closing keeps the pool
    asyncio.run(go())
    assert len(server["seen"]["peers"]) == 1, server["seen"]["peers"]


def test_preconnect_opens_the_connection_the_call_reuses_without_the_key(server):
    from app.core.llm_transport import preconnect

    async def go():
        c = _client()
        assert await preconnect(c.async_client._client, server["url"] + "/v1/") is True
        await c.async_client._client.get(server["url"] + "/v1/ping")
    asyncio.run(go())
    assert len(server["seen"]["peers"]) == 1
    assert server["seen"]["auth"][0] is None, "preconnect must not send credentials"


def test_each_event_loop_gets_its_own_pool(server):
    c = _client()

    async def call():
        r = await c.async_client._client.get(server["url"] + "/v1/ping")
        return r.status_code

    assert asyncio.run(call()) == 200
    assert asyncio.run(call()) == 200  # a new loop: no connection from the closed one
