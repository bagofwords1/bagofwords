"""Gmail integration client.

Per-user OAuth (delegated). The admin configures a Google OAuth client once
(admin-first); each user signs in (user-required) and the resulting access token
is what this client uses on behalf of that user.

This is an *integration* (tools), not a data-source catalog: it declares a set of
typed actions (`TOOLS`) — search, read, send, … — surfaced as `ConnectionTool`
rows and invoked at runtime. `get_schemas()` is intentionally empty (no tabular
catalog); the agent reaches Gmail through the tool path, resolved by current user.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from app.ai.prompt_formatters import Table
from app.data_sources.clients.base import Capability, DataSourceClient


GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"


# Declared tool catalog. Discovery (`ConnectionService`) materializes one
# `ConnectionTool` row per entry; per-user enable/disable is the
# `UserConnectionTool` overlay. Kept declarative so the catalog is the single
# source of truth for both discovery and the Integrations UI "Actions" list.
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "search_messages",
        "description": "Search the user's mailbox with a Gmail query (e.g. 'from:boss is:unread newer_than:7d'). Returns matching message ids and snippets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Gmail search query."},
                "max_results": {"type": "integer", "default": 25, "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_message",
        "description": "Read a single email by message id — returns headers, body text, and metadata.",
        "input_schema": {
            "type": "object",
            "properties": {"message_id": {"type": "string"}},
            "required": ["message_id"],
        },
    },
    {
        "name": "list_labels",
        "description": "List the labels (folders/categories) in the user's mailbox.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message",
        "description": "Send an email on behalf of the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]


class GmailClient(DataSourceClient):
    capabilities = {Capability.LIST_FILES}  # tool-provider; QUERY catalog not used

    # Surfaced for discovery / Integrations "Actions" list.
    tools = TOOLS

    @property
    def description(self) -> str:
        return "Gmail"

    @property
    def is_document_based(self) -> bool:
        return False

    def __init__(
        self,
        access_token: Optional[str] = None,
        oauth_client_id: Optional[str] = None,
        oauth_client_secret: Optional[str] = None,
        **_ignored,
    ):
        super().__init__()
        self.access_token = access_token
        self.oauth_client_id = oauth_client_id
        self.oauth_client_secret = oauth_client_secret

    # ----------------------------------------------------------------- helpers

    def _headers(self) -> Dict[str, str]:
        if not self.access_token:
            raise ValueError(
                "Gmail client has no access token. The user must connect via OAuth "
                "before the connection can be used."
            )
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = httpx.get(f"{GMAIL_BASE}{path}", headers=self._headers(), params=params, timeout=30)
        if resp.status_code >= 400:
            raise ValueError(f"Gmail {path} → {resp.status_code} {resp.text[:300]}")
        return resp.json()

    # ------------------------------------------------------------------- tools

    def list_tools(self) -> List[Dict[str, Any]]:
        return TOOLS

    def search_messages(self, query: str, max_results: int = 25) -> List[dict]:
        data = self._get("/users/me/messages", params={"q": query, "maxResults": max_results})
        return data.get("messages", []) or []

    def read_message(self, message_id: str) -> dict:
        return self._get(f"/users/me/messages/{message_id}", params={"format": "full"})

    def list_labels(self) -> List[dict]:
        return self._get("/users/me/labels").get("labels", []) or []

    # Generic dispatch so native integrations invoke through the same agent path
    # (search_mcps / execute_mcp) as MCP/custom_api connections. Returns the
    # {success, content_type, data} envelope execute_mcp expects.
    async def acall_tool(self, tool_name: str, arguments: Optional[dict] = None) -> dict:
        arguments = arguments or {}
        allowed = {t["name"] for t in TOOLS}
        if tool_name not in allowed:
            return {"success": False, "error": f"Unknown Gmail tool '{tool_name}'."}
        method = getattr(self, tool_name, None)
        if not callable(method):
            return {"success": False, "error": f"Gmail tool '{tool_name}' not implemented."}
        try:
            import asyncio
            data = await asyncio.to_thread(lambda: method(**arguments))
            return {"success": True, "content_type": "json", "data": data}
        except Exception as e:  # pragma: no cover - network/auth path
            return {"success": False, "error": str(e)}

    # ----------------------------------------- DataSourceClient compatibility

    def test_connection(self) -> dict:
        if not self.access_token:
            return {
                "success": True,
                "message": "OAuth client saved. Have a user sign in with Google to use Gmail.",
            }
        try:
            self._get("/users/me/profile")
            return {"success": True, "message": "Connected"}
        except Exception as e:  # pragma: no cover - network path
            return {"success": False, "message": str(e)}

    def get_schemas(self) -> List[Table]:
        return []

    def get_schema(self, table_name: str) -> Optional[Table]:
        return None

    def prompt_schema(self) -> str:
        return "Gmail integration — tools: " + ", ".join(t["name"] for t in TOOLS)

    def execute_query(self, query: Optional[str] = None, **kwargs):
        raise NotImplementedError("Gmail is a tool integration; invoke its tools, not execute_query.")
