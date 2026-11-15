"""MCP Apps host API: who may see an app, who may act through it, and what it may do.

Routes: app/routes/mcp_apps.py. The MCP server is real
(tests/mocks/mcp_apps_server.py, run in-process); the connection, its tool
catalog, the agent and the report are created through the API. Only the tool
call that rendered the app is seeded directly — producing one through the API
needs an LLM in the loop.

Contract:
  * any member who can view the report gets the app document and its sandbox
    metadata; only the report's owner may act through it (``can_interact``);
  * an app may call tools whose visibility includes "app" on its OWN server,
    and nothing else; a write-capable tool needs ``confirmed: true``;
  * a policy "deny" on the tool blocks app calls too;
  * ``ui/update-model-context`` is stored on the tool call's history entry
    and replaced by the next update;
  * the sandbox page carries the declared CSP and may only be framed by BOW;
  * none of it exists for a tool call without an app, or with MCP Apps off.
"""
from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid

import pytest

from tests.mocks import mcp_apps_server as sales


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sales_server():
    import uvicorn

    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(sales.mcp.streamable_http_app(), host="127.0.0.1",
                                        port=port, log_level="error", lifespan="on"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    deadline = time.monotonic() + 20
    while not srv.started:
        assert time.monotonic() < deadline
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    srv.should_exit = True
    t.join(timeout=10)


async def _seed_tool_call(report_id: str, owner_id: str, org_id: str, result_json: dict) -> str:
    # The only direct write: a finished tool call in the report's history.
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution
    from app.models.completion import Completion
    from app.models.tool_execution import ToolExecution

    async with async_session_maker() as db:
        completion = Completion(prompt={"content": "show sales"}, completion={"content": ""},
                                status="success", role="system", report_id=report_id,
                                user_id=owner_id, turn_index=0)
        db.add(completion)
        await db.flush()
        ae = AgentExecution(completion_id=completion.id, organization_id=org_id, user_id=owner_id,
                            report_id=report_id, status="completed")
        db.add(ae)
        await db.flush()
        te = ToolExecution(agent_execution_id=ae.id, tool_name="execute_mcp", status="success",
                           success=True, arguments_json={}, result_json=result_json)
        db.add(te)
        await db.commit()
        return str(te.id)


@pytest.fixture
def world(test_client, sales_server, bootstrap_admin, invite_user_to_org):
    owner = bootstrap_admin("apps_owner")
    org = owner["org_id"]
    conn = test_client.post("/api/connections", headers=_h(owner["token"], org), json={
        "name": f"Regional Sales {uuid.uuid4().hex[:6]}", "type": "mcp",
        "config": {"server_url": sales_server, "transport": "streamable_http"},
        "credentials": {}, "auth_policy": "system_only",
    })
    assert conn.status_code == 200, conn.text
    conn_id = conn.json()["id"]
    tools = test_client.post(f"/api/connections/{conn_id}/refresh-tools", headers=_h(owner["token"], org))
    assert tools.status_code == 200, tools.text
    ds = test_client.post("/api/data_sources", headers=_h(owner["token"], org), json={
        "name": f"Sales Ops {uuid.uuid4().hex[:6]}", "connection_ids": [conn_id],
        "generate_summary": False, "generate_conversation_starters": False,
        "generate_ai_rules": False, "use_llm_sync": False, "is_public": True,
    })
    assert ds.status_code == 200, ds.text
    report = test_client.post("/api/reports", headers=_h(owner["token"], org), json={
        "title": "Sales review", "data_sources": [ds.json()["id"]],
    })
    assert report.status_code == 200, report.text
    report_id = report.json()["id"]
    app_result = {
        "success": True, "connection_name": "Regional Sales",
        "mcp_app": {
            "resource_uri": sales.APP_URI, "connection_id": conn_id,
            "tool_name": "show_sales_dashboard", "tool_input": {"quarter": "Q3"},
            "tool_result": {"content": [{"type": "text", "text": "ok"}],
                            "structuredContent": {"quarter": "Q3", "rows": sales.sales_rows("Q3")}},
        },
    }
    te_app = asyncio.run(_seed_tool_call(report_id, owner["user_id"], org, app_result))
    te_plain = asyncio.run(_seed_tool_call(report_id, owner["user_id"], org, {"success": True}))
    member = invite_user_to_org(org_id=org, admin_token=owner["token"])
    tools_by_name = {t["name"]: t for t in tools.json()}
    return {"org": org, "owner": owner, "member": member, "te": te_app, "te_plain": te_plain,
            "conn_id": conn_id, "tools": tools_by_name}


def _rpc(test_client, w, who, method, params, confirmed=False):
    user = w[who]
    return test_client.post(f"/api/mcp_apps/{w['te']}/rpc", headers=_h(user["token"], w["org"]),
                            json={"method": method, "params": params, "confirmed": confirmed})


@pytest.mark.e2e
@pytest.mark.parametrize("who, can_interact", [("owner", True), ("member", False)])
def test_viewers_get_the_app_document_owner_alone_may_interact(test_client, world, who, can_interact):
    user = world[who]
    r = test_client.get(f"/api/mcp_apps/{world['te']}/resource", headers=_h(user["token"], world["org"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "<html" in body["html"].lower()
    assert body["prefers_border"] is True
    assert body["tool_result"]["structuredContent"]["quarter"] == "Q3"
    assert body["sandbox_url"].endswith("/api/mcp_apps/sandbox")
    assert body["can_interact"] is can_interact


@pytest.mark.e2e
def test_a_tool_call_without_an_app_has_no_app_routes(test_client, world):
    h = _h(world["owner"]["token"], world["org"])
    assert test_client.get(f"/api/mcp_apps/{world['te_plain']}/resource", headers=h).status_code == 404
    assert test_client.get(f"/api/mcp_apps/{uuid.uuid4()}/resource", headers=h).status_code == 404


@pytest.mark.e2e
def test_an_outsider_cannot_reach_the_app(test_client, world, bootstrap_admin):
    other = bootstrap_admin("apps_outsider")
    r = test_client.get(f"/api/mcp_apps/{world['te']}/resource", headers=_h(other["token"], other["org_id"]))
    assert r.status_code == 404


@pytest.mark.e2e
def test_only_the_owner_may_act_through_the_app(test_client, world):
    r = _rpc(test_client, world, "member", "tools/call", {"name": "refresh_sales", "arguments": {"quarter": "Q1"}})
    assert r.status_code == 403
    r = test_client.post(f"/api/mcp_apps/{world['te']}/model_context",
                         headers=_h(world["member"]["token"], world["org"]),
                         json={"content": [{"type": "text", "text": "x"}]})
    assert r.status_code == 403


@pytest.mark.e2e
@pytest.mark.parametrize("quarter", ["Q1", "Q4"])
def test_app_calls_an_app_only_read_tool_on_its_server(test_client, world, quarter):
    r = _rpc(test_client, world, "owner", "tools/call", {"name": "refresh_sales", "arguments": {"quarter": quarter}})
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["structuredContent"]["quarter"] == quarter
    assert result["structuredContent"]["rows"] == sales.sales_rows(quarter)


@pytest.mark.e2e
def test_app_cannot_call_a_model_only_tool(test_client, world):
    r = _rpc(test_client, world, "owner", "tools/call", {"name": "client_handshake", "arguments": {}})
    # client_handshake has no UI binding → default visibility includes "app".
    assert "result" in r.json()
    # A tool the model may see but the app may not: hide it from apps and retry.
    from app.dependencies import async_session_maker
    from app.models.connection_tool import ConnectionTool

    async def make_model_only():
        async with async_session_maker() as db:
            t = await db.get(ConnectionTool, world["tools"]["client_handshake"]["id"])
            t.metadata_json = {**(t.metadata_json or {}), "ui": {"visibility": ["model"]}}
            await db.commit()

    asyncio.run(make_model_only())
    r = _rpc(test_client, world, "owner", "tools/call", {"name": "client_handshake", "arguments": {}})
    assert r.json()["error"]["code"] == -32602


@pytest.mark.e2e
def test_app_cannot_reach_tools_the_server_does_not_have(test_client, world):
    r = _rpc(test_client, world, "owner", "tools/call", {"name": "execute_shell", "arguments": {"command": "id"}})
    assert "error" in r.json() and "result" not in r.json()
    r = _rpc(test_client, world, "owner", "sampling/createMessage", {})
    assert r.json()["error"]["code"] == -32601


@pytest.mark.e2e
def test_write_capable_app_call_needs_confirmation(test_client, world, tmp_path, monkeypatch):
    args = {"region": "APAC", "note": "check pipeline"}
    first = _rpc(test_client, world, "owner", "tools/call", {"name": "create_followup_task", "arguments": args})
    err = first.json()["error"]
    assert err["message"] == "confirmation_required"
    assert err["data"]["tool"] == "create_followup_task" and err["data"]["arguments"] == args
    confirmed = _rpc(test_client, world, "owner", "tools/call",
                     {"name": "create_followup_task", "arguments": args}, confirmed=True)
    result = confirmed.json()["result"]
    assert not result.get("isError")
    assert "APAC" in result["content"][0]["text"]


@pytest.mark.e2e
def test_policy_deny_blocks_app_calls(test_client, world):
    tool_id = world["tools"]["refresh_sales"]["id"]
    from app.dependencies import async_session_maker
    from app.models.connection_tool import ConnectionTool

    async def deny():
        async with async_session_maker() as db:
            t = await db.get(ConnectionTool, tool_id)
            t.policy = "deny"
            await db.commit()

    asyncio.run(deny())
    r = _rpc(test_client, world, "owner", "tools/call", {"name": "refresh_sales", "arguments": {}})
    assert "error" in r.json() and "result" not in r.json()


@pytest.mark.e2e
def test_app_reads_resources_of_its_own_server(test_client, world):
    r = _rpc(test_client, world, "owner", "resources/read", {"uri": sales.APP_URI})
    contents = r.json()["result"]["contents"]
    assert contents and contents[0]["mimeType"].startswith("text/html")


@pytest.mark.e2e
def test_model_context_is_stored_on_the_tool_call_and_replaced(test_client, world):
    h = _h(world["owner"]["token"], world["org"])
    for region in ("APAC", "EMEA"):
        r = test_client.post(f"/api/mcp_apps/{world['te']}/model_context", headers=h, json={
            "content": [{"type": "text", "text": f"User selected {region}"}],
            "structuredContent": {"selectedRegion": region},
        })
        assert r.status_code == 200 and r.json() == {"result": {}}

    from app.dependencies import async_session_maker
    from app.models.tool_execution import ToolExecution
    from app.ai.persisted_summary import tool_context_for_replay

    async def read():
        async with async_session_maker() as db:
            return (await db.get(ToolExecution, world["te"])).context_summary_json

    body = tool_context_for_replay(asyncio.run(read()))
    rendered = json.dumps(body)
    assert "EMEA" in rendered and "APAC" not in rendered  # the latest update replaced the first


@pytest.mark.e2e
def test_model_context_is_bounded(test_client, world):
    r = test_client.post(f"/api/mcp_apps/{world['te']}/model_context",
                         headers=_h(world["owner"]["token"], world["org"]),
                         json={"content": [{"type": "text", "text": "x" * 20_000}]})
    assert "error" in r.json()


@pytest.mark.e2e
def test_mcp_apps_off_hides_the_app(test_client, world):
    h = _h(world["owner"]["token"], world["org"])
    r = test_client.put("/api/organization/settings", headers=h,
                        json={"config": {"enable_mcp_apps": {"value": False}}})
    assert r.status_code == 200, r.text
    assert test_client.get(f"/api/mcp_apps/{world['te']}/resource", headers=h).status_code == 404
    assert _rpc(test_client, world, "owner", "tools/call", {"name": "refresh_sales", "arguments": {}}).status_code == 404


@pytest.mark.e2e
def test_sandbox_page_carries_declared_csp_and_only_bow_may_frame_it(test_client):
    declared = {"connectDomains": ["https://api.example.com", "https://x.example; script-src *"]}
    r = test_client.get("/api/mcp_apps/sandbox", params={"csp": json.dumps(declared)})
    assert r.status_code == 200
    csp = r.headers["content-security-policy"]
    directives = {p.strip().split(" ", 1)[0]: p.strip() for p in csp.split(";")}
    assert "https://api.example.com" in directives["connect-src"]
    assert "script-src *" not in csp
    assert directives["frame-ancestors"].startswith("frame-ancestors 'self'")
    assert "*" not in directives["frame-ancestors"].split()
    # Unparseable input falls back to the restrictive default, not an error.
    r = test_client.get("/api/mcp_apps/sandbox", params={"csp": "{not json"})
    assert r.status_code == 200
    assert "connect-src 'none'" in r.headers["content-security-policy"]


@pytest.mark.e2e
@pytest.mark.parametrize("tools_max", ["50", "0"])
def test_app_only_tools_never_reach_the_agent(test_client, world, monkeypatch, tools_max):
    """Native or behind search_tools, the model's catalog has no app-only tool
    (spec MUST), while the app's model-visible tool stays."""
    monkeypatch.setenv("BOW_TOOLS_MAX", tools_max)
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution
    from app.models.report import Report
    from app.models.tool_execution import ToolExecution
    from app.models.user import User
    from app.ai.tools.mcp_tool_registry import build_mcp_tool_catalog, clear_usage_cache

    async def catalog():
        clear_usage_cache()
        async with async_session_maker() as db:
            te = await db.get(ToolExecution, world["te"])
            ae = await db.get(AgentExecution, te.agent_execution_id)
            report = (await db.execute(select(Report).options(selectinload(Report.data_sources))
                                       .where(Report.id == ae.report_id))).scalar_one()
            user = await db.get(User, world["owner"]["user_id"])
            return await build_mcp_tool_catalog(db, report, user=user)

    cat = asyncio.run(catalog())
    assert not cat.failed
    reachable = {r["tool_name"] for r in cat.routing.values()}
    assert "show_sales_dashboard" in reachable
    assert "client_handshake" in reachable
    assert not {"refresh_sales", "create_followup_task"} & reachable
