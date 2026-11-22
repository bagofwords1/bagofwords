#!/usr/bin/env python3
"""Serve the fake Microsoft 365 (Graph + identity platform) over HTTP for sandbox runs.

The fake itself lives in ``backend/tests/mocks/fake_microsoft365.py`` (the same
one the connector-action tests use). This wraps it in a tiny ASGI app so a
running BOW backend can sign users in and call Graph against it:

    cd backend && uv run python ../tools/agent/mock_graph_server.py --port 8765

then start the backend with

    BOW_GRAPH_BASE_URL=http://localhost:8765/v1.0
    BOW_GRAPH_TOKEN_BASE_URL=http://localhost:8765

"Sign in with Microsoft" then completes against the fake (it auto-approves as
the fake user and grants exactly the scopes requested), and every write the
agent makes is visible at ``GET http://localhost:8765/_fake/writes``.
"""
import argparse
import os
import sys

import httpx
import uvicorn

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "backend"))

from tests.mocks.fake_microsoft365 import FakeM365  # noqa: E402

FAKE = FakeM365()


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    body = b""
    while True:
        msg = await receive()
        body += msg.get("body", b"")
        if not msg.get("more_body"):
            break
    qs = scope.get("query_string", b"").decode()
    url = f"http://{dict(scope['headers']).get(b'host', b'localhost').decode()}{scope['path']}" + (f"?{qs}" if qs else "")
    request = httpx.Request(scope["method"], url,
                            headers=[(k.decode(), v.decode()) for k, v in scope["headers"]], content=body)
    resp = FAKE(request)
    content = resp.content
    headers = [(k.encode(), v.encode()) for k, v in resp.headers.items()
               if k.lower() not in ("content-length", "transfer-encoding")]
    headers.append((b"content-length", str(len(content)).encode()))
    await send({"type": "http.response.start", "status": resp.status_code, "headers": headers})
    await send({"type": "http.response.body", "body": content})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
