"""Custom API tools stay callable when a file-source agent shares the report.

Reported: a report scoped to a OneDrive agent AND a custom API agent could not
call the custom API tools — the call came back ``resolve_error: tool/plan_type
mismatch`` and the fallback ``execute_mcp`` call failed with ``arguments: Input
should be a valid dictionary``. Each agent worked on its own.

Contract:
  * every custom API tool on the run's agents is offered natively, whatever
    other agents (file sources included) are in the same scope;
  * calling it reaches the API through its real client;
  * ``execute_mcp`` accepts ``arguments`` sent as a JSON-encoded object;
  * a call to an ``mcp__`` name the run does not have is refused as an unknown
    tool that names what the run can call, not as a plan-type mismatch.

The LLM boundary is stubbed; routes, services, the agent loop and the tool
gateway run real.
"""
from __future__ import annotations

import asyncio
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.e2e

FILE_SOURCE_TYPES = ("onedrive", "sharepoint", "google_drive")


class _Planner:
    """Scripted planner: replays queued tool calls, one per request, then answers."""

    def __init__(self):
        self.requests: list[list[str]] = []
        self.results: list[str] = []
        self.queue: list = []

    def stream(self):
        from app.ai.llm.types import (
            MessageStopEvent, TextDeltaEvent, ToolUseCompleteEvent, ToolUseStartEvent, UsageEvent,
        )
        planner = self

        async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
            names = [getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None) for t in (tools or [])]
            planner.requests.append([n for n in names if n])
            planner.results = [
                json.dumps(b.get("content"))
                for m in messages or []
                for b in ((m.get("content") if isinstance(m, dict) else getattr(m, "content", None)) or [])
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if planner.queue:
                name, args = planner.queue.pop(0)
                tid = f"tu_{uuid.uuid4().hex[:8]}"
                events = [ToolUseStartEvent(id=tid, name=name), ToolUseCompleteEvent(id=tid, name=name, input=args),
                          MessageStopEvent(stop_reason="tool_use", raw_stop_reason="tool_use")]
            else:
                events = [TextDeltaEvent(text="Done."), MessageStopEvent(stop_reason="end_turn", raw_stop_reason="end_turn")]
            for e in events + [UsageEvent(input_tokens=10, output_tokens=5)]:
                yield e

        return scripted


@pytest.fixture
def planner(monkeypatch):
    from app.ai.llm.llm import LLM
    from app.ai.tools.mcp_tool_registry import clear_usage_cache

    clear_usage_cache()
    p = _Planner()

    async def _stream(self, prompt, **kwargs):
        yield "ok"

    monkeypatch.setattr(LLM, "inference_stream_v2", p.stream())
    monkeypatch.setattr(LLM, "inference_stream", _stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **k: "[]")
    yield p
    clear_usage_cache()


@pytest.fixture
def items_api():
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = json.dumps({"items": [{"id": 1, "cost": 12.5}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"url": f"http://127.0.0.1:{srv.server_address[1]}", "seen": seen}
    srv.shutdown()


@pytest.fixture
def org(test_client, bootstrap_admin, create_llm_provider_and_models, enable_mcp,
        create_custom_api_connection, create_domain_from_connection, wait_for_connection_indexing,
        items_api, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    admin = bootstrap_admin("apifiles")
    token, org_id = admin["token"], admin["org_id"]
    create_llm_provider_and_models(user_token=token, org_id=org_id)
    enable_mcp(user_token=token, org_id=org_id)
    h = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    slug = f"costs{uuid.uuid4().hex[:4]}"
    conn = create_custom_api_connection(
        name=slug, base_url=items_api["url"], user_token=token, org_id=org_id,
        endpoints=[{"name": "get_cost_report", "method": "GET", "path": "/cost",
                    "description": "Cost report", "parameters": [
                        {"name": "days", "in": "query", "type": "integer", "description": "Window"}]}],
    )
    ds = create_domain_from_connection(name=f"{slug} agent", connection_id=conn["id"], user_token=token, org_id=org_id)
    # Creation starts a background discovery run that writes the tool catalog
    # with the fixture's mock client; let it finish before the real refresh.
    wait_for_connection_indexing(connection_id=conn["id"], user_token=token, org_id=org_id)
    r = test_client.post(f"/api/connections/{conn['id']}/refresh-tools", headers=h)
    assert r.status_code == 200, r.text
    return {"admin": admin, "org_id": str(org_id), "h": h, "slug": slug,
            "api_ds": str(ds["id"]), "api_conn": str(conn["id"])}


def _seed_file_agent(org_id: str, owner_id: str, conn_type: str) -> str:
    """Direct write: a file-source agent (OneDrive and friends) needs a real
    OAuth consent to be created through the API. Shaped like a connected one:
    a per-user connection with unlinked document rows."""
    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.data_source import DataSource
    from app.models.datasource_table import DataSourceTable
    from app.models.domain_connection import domain_connection

    async def _run():
        async with async_session_maker() as db:
            ds = DataSource(name=f"{conn_type} agent {uuid.uuid4().hex[:4]}", organization_id=org_id,
                            is_active=True, is_public=True, owner_user_id=owner_id)
            db.add(ds)
            conn = Connection(organization_id=org_id, name=f"{conn_type} {uuid.uuid4().hex[:4]}",
                              type=conn_type, config={}, auth_policy="user_required")
            db.add(conn)
            await db.flush()
            await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
            for i in range(3):
                db.add(DataSourceTable(name=f"Reports/usage-{i}.xlsx", datasource_id=ds.id, is_active=True,
                                       columns=[], pks=[], fks=[], no_rows=0))
            await db.commit()
            return str(ds.id)

    return asyncio.run(_run())


def _ask(test_client, org, data_sources, focus=None):
    h = org["h"]
    r = test_client.post("/api/reports", headers=h, json={"title": "costs", "files": [], "data_sources": data_sources})
    assert r.status_code == 200, r.text
    if focus is not None:
        # The state a report is left in after a run used a file tool:
        # focus-on-use pins the report to the file agent it read.
        u = test_client.put(f"/api/reports/{r.json()['id']}", headers=h, json={"focused_data_source_ids": focus})
        assert u.status_code == 200, u.text
    r = test_client.post(f"/api/reports/{r.json()['id']}/completions", headers=h,
                         params={"background": False}, json={"prompt": {"content": "Pull the cost report."}})
    assert r.status_code == 200, r.text


def _admin_id(test_client, org) -> str:
    r = test_client.get("/api/users/whoami", headers=org["h"])
    assert r.status_code == 200, r.text
    return str(r.json()["id"])


@pytest.mark.parametrize("focused_on_file_agent", [False, True], ids=["no-focus", "focused-on-file-agent"])
@pytest.mark.parametrize("file_type", FILE_SOURCE_TYPES)
def test_custom_api_tool_is_native_and_callable_beside_a_file_agent(
        test_client, org, planner, items_api, file_type, focused_on_file_agent):
    file_ds = _seed_file_agent(org["org_id"], _admin_id(test_client, org), file_type)
    name = f"mcp__{org['slug']}__get_cost_report"
    planner.queue = [(name, {"days": 7})]

    _ask(test_client, org, [org["api_ds"], file_ds], focus=[file_ds] if focused_on_file_agent else None)

    assert name in planner.requests[0], planner.requests[0]
    assert items_api["seen"] and items_api["seen"][0].startswith("/cost"), items_api["seen"]
    assert "mismatch" not in " ".join(planner.results)


def test_execute_mcp_accepts_json_encoded_arguments(test_client, org, planner, items_api):
    from app.ai.tools.schemas.execute_mcp import ExecuteMCPInput

    parsed = ExecuteMCPInput(connection_id=org["api_conn"], tool_name="get_cost_report",
                             arguments=json.dumps({"days": 30}))
    assert parsed.arguments == {"days": 30}
    assert ExecuteMCPInput(connection_id="c", tool_name="t", arguments=None).arguments == {}
    with pytest.raises(Exception):
        ExecuteMCPInput(connection_id="c", tool_name="t", arguments="[1, 2]")


def test_an_unknown_mcp_name_is_refused_as_unknown_and_lists_the_callable_tools(test_client, org, planner, items_api):
    planner.queue = [(f"mcp__{org['slug']}__no_such_tool", {})]

    _ask(test_client, org, [org["api_ds"]])

    result = " ".join(planner.results)
    assert "mismatch" not in result, result
    assert "unknown_tool" in result, result
    assert f"mcp__{org['slug']}__get_cost_report" in result, result
    assert not items_api["seen"]
