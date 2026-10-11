"""Request-shape hardening shared by every route.

Two cheap, app-wide guards that turn malformed identifiers into ordinary
4xx responses before they reach the database (where, e.g., Postgres rejects
NUL bytes with a driver error that used to surface as a 500 + traceback):

* ``ControlCharGuardMiddleware`` — rejects a request whose URL path carries a
  control character (C0 or DEL) or whose query string carries a NUL byte.
  Runs as raw ASGI so it also covers non-FastAPI handlers (SPA fallback,
  webhooks, artifact runtime).
* ``validate_id_path_params`` — a global dependency: every path parameter
  whose name is in ``UUID_PATH_PARAMS`` must parse as a UUID, otherwise the
  request gets the same 404 an unknown id would. Adding a name here is the
  single place to opt a new ``{x_id}`` route param into the check.
"""
from __future__ import annotations

import json
import re
import uuid
from urllib.parse import unquote_to_bytes

from fastapi import HTTPException, Request

# Path params that always hold one of our String(36) uuid4 primary keys.
# Deliberately excludes params that may carry provider/external ids
# (user_id on SCIM, external_id, tool_call_id, client_id, demo_id, ...).
UUID_PATH_PARAMS = frozenset({
    "report_id",
    "data_source_id",
    "organization_id",
    "connection_id",
    "instruction_id",
    "completion_id",
    "model_id",
    "artifact_id",
    "platform_id",
    "list_id",
    "file_id",
    "suite_id",
    "query_id",
    "prompt_id",
    "sa_id",
    "cq_id",
    "version_id",
    "policy_id",
})

_PATH_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def traceback_pages_enabled(settings) -> bool:
    """Starlette's HTML traceback page (``FastAPI(debug=True)``) is served
    only on a DEBUG deployment that *also* opts in via DEBUG_TRACEBACKS."""
    return bool(getattr(settings, "DEBUG", False) and getattr(settings, "DEBUG_TRACEBACKS", False))


def is_uuid(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 36:
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


async def validate_id_path_params(request: Request) -> None:
    for name, value in request.path_params.items():
        if name in UUID_PATH_PARAMS and not is_uuid(value):
            raise HTTPException(status_code=404, detail="Not found")


class ControlCharGuardMiddleware:
    """Reject control characters in the URL before routing (400, JSON)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") in ("http", "websocket"):
            path = scope.get("path") or ""
            query = scope.get("query_string") or b""
            bad = bool(_PATH_CONTROL.search(path))
            if not bad and query:
                bad = b"\x00" in query or b"\x00" in unquote_to_bytes(query)
            if bad:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps({"detail": "Invalid request"}).encode()
                await send({
                    "type": "http.response.start",
                    "status": 400,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)
