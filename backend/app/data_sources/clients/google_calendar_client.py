"""Google Calendar integration client.

Per-user OAuth (delegated) on the shared Google app — admin configures the OAuth
client once, each user signs in. Declares typed tools (`TOOLS`) surfaced as
`ConnectionTool` rows and invoked through the same agent path as MCP/custom_api.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from app.ai.prompt_formatters import Table
from app.data_sources.clients.base import Capability, DataSourceClient


CAL_BASE = "https://www.googleapis.com/calendar/v3"


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "list_events",
        "description": "List upcoming events on a calendar within an optional time window (RFC3339).",
        "input_schema": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "default": "primary"},
                "time_min": {"type": "string", "description": "RFC3339 lower bound, e.g. 2026-06-01T00:00:00Z"},
                "time_max": {"type": "string"},
                "max_results": {"type": "integer", "default": 25, "minimum": 1, "maximum": 250},
            },
        },
    },
    {
        "name": "list_calendars",
        "description": "List the calendars the user can access.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_event",
        "description": "Get one event by id from a calendar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "calendar_id": {"type": "string", "default": "primary"},
                "event_id": {"type": "string"},
            },
            "required": ["event_id"],
        },
    },
]


class GoogleCalendarClient(DataSourceClient):
    capabilities = {Capability.LIST_FILES}  # tool provider
    tools = TOOLS

    @property
    def description(self) -> str:
        return "Google Calendar"

    @property
    def is_document_based(self) -> bool:
        return False

    def __init__(self, access_token: Optional[str] = None, **_ignored):
        super().__init__()
        self.access_token = access_token

    def _headers(self) -> Dict[str, str]:
        if not self.access_token:
            raise ValueError("Google Calendar client has no access token; user must connect via OAuth.")
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        resp = httpx.get(f"{CAL_BASE}{path}", headers=self._headers(), params=params, timeout=30)
        if resp.status_code >= 400:
            raise ValueError(f"Calendar {path} → {resp.status_code} {resp.text[:300]}")
        return resp.json()

    # tools
    def list_calendars(self) -> List[dict]:
        return self._get("/users/me/calendarList").get("items", []) or []

    def list_events(self, calendar_id: str = "primary", time_min: Optional[str] = None,
                    time_max: Optional[str] = None, max_results: int = 25) -> List[dict]:
        params: Dict[str, Any] = {"maxResults": max_results, "singleEvents": "true", "orderBy": "startTime"}
        if time_min:
            params["timeMin"] = time_min
        if time_max:
            params["timeMax"] = time_max
        from urllib.parse import quote
        return self._get(f"/calendars/{quote(calendar_id)}/events", params=params).get("items", []) or []

    def get_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        from urllib.parse import quote
        return self._get(f"/calendars/{quote(calendar_id)}/events/{quote(event_id)}")

    async def acall_tool(self, tool_name: str, arguments: Optional[dict] = None) -> dict:
        arguments = arguments or {}
        if tool_name not in {t["name"] for t in TOOLS}:
            return {"success": False, "error": f"Unknown Calendar tool '{tool_name}'."}
        method = getattr(self, tool_name, None)
        if not callable(method):
            return {"success": False, "error": f"Calendar tool '{tool_name}' not implemented."}
        try:
            import asyncio
            data = await asyncio.to_thread(lambda: method(**arguments))
            return {"success": True, "content_type": "json", "data": data}
        except Exception as e:  # pragma: no cover - network path
            return {"success": False, "error": str(e)}

    # DataSourceClient compatibility
    def test_connection(self) -> dict:
        if not self.access_token:
            return {"success": True, "message": "OAuth client saved. Have a user sign in with Google."}
        try:
            self._get("/users/me/calendarList", params={"maxResults": 1})
            return {"success": True, "message": "Connected"}
        except Exception as e:  # pragma: no cover
            return {"success": False, "message": str(e)}

    def get_schemas(self) -> List[Table]:
        return []

    def get_schema(self, table_name: str) -> Optional[Table]:
        return None

    def prompt_schema(self) -> str:
        return "Google Calendar — tools: " + ", ".join(t["name"] for t in TOOLS)

    def execute_query(self, query: Optional[str] = None, **kwargs):
        raise NotImplementedError("Google Calendar is a tool integration; invoke its tools.")
