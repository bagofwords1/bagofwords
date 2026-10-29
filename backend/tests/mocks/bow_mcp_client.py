"""A small, transport-agnostic MCP client for the Bag of Words ``/api/mcp`` endpoint.

It speaks the same JSON-RPC 2.0 the real clients (Claude Code, Cursor, Claude
Web) send, so tests and sandbox scripts exercise the exact wire contract:

    client = BowMCPClient(http, api_key="bow_...")
    client.initialize()
    names = client.tool_names()
    result = client.call("edit_instruction", {"instruction_id": "...", "text": "..."})
    if result.refused: ...

``http`` is anything with ``post(url, json=..., headers=...)`` returning an
object with ``status_code``, ``headers``, ``text`` and ``json()`` — FastAPI's
``TestClient`` in e2e tests and ``httpx.Client`` against a live stack both fit.

The client never decides what is *allowed*; it only reports what the server
did. ``ToolResult.refused`` folds the three shapes a refusal can take on this
server into one bit: a JSON-RPC error (unknown tool), an MCP tool error
(``isError: true``), or a structured failure payload (``success: false`` /
``error``).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from typing import Any

MCP_PATH = "/api/mcp"


@dataclass
class ToolResult:
    name: str
    status_code: int
    is_error: bool = False
    payload: Any = None
    text: str = ""
    rpc_error: dict | None = None
    raw: dict = field(default_factory=dict)

    @property
    def refused(self) -> bool:
        if self.status_code != 200 or self.rpc_error is not None or self.is_error:
            return True
        if isinstance(self.payload, dict):
            if self.payload.get("success") is False:
                return True
            if self.payload.get("error") or self.payload.get("error_message"):
                return True
        return False

    @property
    def ok(self) -> bool:
        return not self.refused

    def summary(self) -> str:
        if self.rpc_error:
            return f"rpc_error {self.rpc_error.get('code')}: {self.rpc_error.get('message')}"
        if self.is_error:
            return f"isError: {self.text[:200]}"
        if isinstance(self.payload, dict):
            msg = self.payload.get("error_message") or self.payload.get("error") or self.payload.get("message")
            return f"{'refused' if self.refused else 'ok'}: {str(msg)[:200] if msg else ''}"
        return "ok"


class MCPHTTPError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(f"MCP HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body


class BowMCPClient:
    def __init__(
        self,
        http,
        *,
        api_key: str | None = None,
        bearer: str | None = None,
        org_id: str | None = None,
        path: str = MCP_PATH,
        stream: bool = False,
    ):
        if not api_key and not bearer:
            raise ValueError("api_key or bearer is required")
        self._http = http
        self._path = path
        self._stream = stream
        self._ids = itertools.count(1)
        self.headers: dict[str, str] = {}
        if api_key:
            self.headers["X-API-Key"] = api_key
        if bearer:
            self.headers["Authorization"] = f"Bearer {bearer}"
        if org_id:
            self.headers["X-Organization-Id"] = str(org_id)
        self._tools: list[dict] | None = None

    # ── wire ────────────────────────────────────────────────────────────
    def _rpc(self, method: str, params: dict | None = None, *, sse: bool = False):
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            body["params"] = params
        headers = dict(self.headers)
        headers["Accept"] = "application/json, text/event-stream" if sse else "application/json"
        resp = self._http.post(self._path, json=body, headers=headers)
        if resp.status_code != 200:
            return resp.status_code, None
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            return 200, _last_sse_message(resp.text)
        return 200, resp.json()

    def initialize(self) -> dict:
        status, body = self._rpc("initialize", {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "bow-mock-mcp-client", "version": "1.0"},
        })
        if status != 200:
            raise MCPHTTPError(status, "initialize failed")
        return body["result"]

    def list_tools(self, refresh: bool = False) -> list[dict]:
        if self._tools is None or refresh:
            status, body = self._rpc("tools/list")
            if status != 200:
                raise MCPHTTPError(status, "tools/list failed")
            self._tools = body["result"]["tools"]
        return self._tools

    def tool_names(self, refresh: bool = False) -> set[str]:
        return {t["name"] for t in self.list_tools(refresh=refresh)}

    def call(self, name: str, arguments: dict | None = None) -> ToolResult:
        status, body = self._rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}, sse=self._stream,
        )
        if status != 200 or body is None:
            return ToolResult(name=name, status_code=status)
        if "error" in body:
            return ToolResult(name=name, status_code=200, rpc_error=body["error"], raw=body)
        result = body.get("result") or {}
        text = ""
        content = result.get("content") or []
        if content and isinstance(content[0], dict):
            text = content[0].get("text", "") or ""
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            payload = None
        return ToolResult(
            name=name,
            status_code=200,
            is_error=bool(result.get("isError")),
            payload=payload,
            text=text,
            raw=body,
        )

    # ── adapters ────────────────────────────────────────────────────────
    def anthropic_tools(self, include: set[str] | None = None) -> list[dict]:
        """Tools in the Anthropic Messages API ``tools`` shape."""
        out = []
        for t in self.list_tools():
            if include is not None and t["name"] not in include:
                continue
            out.append({
                "name": t["name"],
                "description": t.get("description", "")[:4000],
                "input_schema": t.get("inputSchema") or {"type": "object", "properties": {}},
            })
        return out


def _last_sse_message(text: str) -> dict | None:
    last = None
    for block in text.split("\n\n"):
        data_lines = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
        if data_lines:
            try:
                last = json.loads("\n".join(data_lines))
            except ValueError:
                continue
    return last
