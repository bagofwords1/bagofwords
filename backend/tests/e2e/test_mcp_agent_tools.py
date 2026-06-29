"""E2E tests for the MCP agent-tool gateway.

These drive the *real* BOW MCP server (JSON-RPC over /api/mcp) as an external
MCP client would, exercising the new gateway end to end:

  * get_context        — advertises each agent's MCP/custom-API tools
  * list_agent_tools   — returns those tools' full input schemas
  * execute_mcp        — invokes a tool through BOW and returns the result
  * policy enforcement — disabled tools and non-'allow' policies are blocked

The provider client is mocked (MockToolProviderClient) so no network is needed.
Run with `-s` to see the full JSON-RPC transcript printed inline.
"""

import contextlib
import json

import pytest
from unittest.mock import patch

from tests.mocks.mock_mcp_server import MockToolProviderClient


# ── helpers ──────────────────────────────────────────────────────────

@contextlib.contextmanager
def _patch_provider(mock=None):
    """Patch ConnectionService.construct_client to return an in-process mock."""
    client = mock or MockToolProviderClient()

    async def _construct(self, db, connection, current_user=None, **kwargs):
        return client

    with patch(
        "app.services.connection_service.ConnectionService.construct_client",
        _construct,
    ):
        yield client


def _make_caller(test_client, api_key, verbose=False):
    def call(method, params=None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        resp = test_client.post("/api/mcp", json=payload, headers={"X-API-Key": api_key})
        if verbose:
            print(f"\n→ {method} {json.dumps(params or {})}")
            print(f"← {resp.status_code} {json.dumps(resp.json())[:600]}")
        return resp
    return call


def _tool_call(call, name, arguments):
    resp = call("tools/call", {"name": name, "arguments": arguments})
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    parsed = json.loads(result["content"][0]["text"])
    return parsed, result.get("isError")


def _full_setup(
    *, create_user, login_user, whoami, create_api_key, enable_mcp,
    create_mcp_connection, create_domain_from_connection, test_client,
    mock=None, verbose=False,
):
    """Stand up a user + org + MCP agent with discovered tools + a report."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    api_key = create_api_key(user_token=token, org_id=org_id)["key"]
    enable_mcp(user_token=token, org_id=org_id)

    conn = create_mcp_connection(
        name="Mock MCP", server_url="http://mock:3000/mcp",
        user_token=token, org_id=org_id,
    )
    with _patch_provider(mock) as client:
        # Persist ConnectionTool rows from the provider's list_tools().
        r = test_client.post(
            f"/api/connections/{conn['id']}/refresh-tools",
            headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
        )
        assert r.status_code == 200, r.text

    ds = create_domain_from_connection(
        name="Mock Agent", connection_id=conn["id"],
        user_token=token, org_id=org_id, is_public=True,
    )

    call = _make_caller(test_client, api_key, verbose=verbose)
    report, _ = _tool_call(call, "create_report", {"title": "Gateway Test"})
    report_id = report["report_id"]
    return {
        "token": token, "org_id": org_id, "api_key": api_key,
        "conn_id": conn["id"], "ds_id": str(ds["id"]),
        "report_id": report_id, "call": call, "client": client,
    }


# ── tests ────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_gateway_discovery_and_execute(
    create_user, login_user, whoami, create_api_key, enable_mcp,
    create_mcp_connection, create_domain_from_connection, test_client, capsys,
):
    """get_context advertises tools, list_agent_tools returns schemas,
    execute_mcp runs them — printed as a JSON-RPC transcript."""
    ctx = _full_setup(
        create_user=create_user, login_user=login_user, whoami=whoami,
        create_api_key=create_api_key, enable_mcp=enable_mcp,
        create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client, verbose=True,
    )
    call, ds_id, report_id = ctx["call"], ctx["ds_id"], ctx["report_id"]

    # 1) get_context advertises the agent's tools (names only).
    context, is_err = _tool_call(call, "get_context", {"report_id": report_id})
    assert is_err is False
    agents = {d["id"]: d for d in context["data_sources"]}
    assert ds_id in agents, f"agent {ds_id} missing from get_context: {context}"
    agent_tools = {t["name"] for t in agents[ds_id]["tools"]}
    print("\n[get_context] advertised tools:", sorted(agent_tools))
    assert {"echo", "get_records", "search_docs"} <= agent_tools
    # Tools are name-only here; the hint points the client to list_agent_tools.
    print("[get_context] tools_hint:", context["tools_hint"])
    assert "list_agent_tools" in (context["tools_hint"] or "")

    # 2) list_agent_tools returns full input schemas.
    listed, is_err = _tool_call(call, "list_agent_tools", {"data_source_ids": [ds_id]})
    assert is_err is False
    by_name = {t["name"]: t for t in listed["tools"]}
    print("[list_agent_tools] get_records schema:",
          json.dumps(by_name["get_records"]["input_schema"]))
    assert by_name["get_records"]["input_schema"]["properties"]["count"]["type"] == "integer"

    # 3) execute_mcp — tabular tool returns rows.
    with _patch_provider(ctx["client"]):
        out, is_err = _tool_call(call, "execute_mcp", {
            "data_source_id": ds_id, "tool_name": "get_records",
            "arguments": {"count": 3},
        })
    assert is_err is False
    print("[execute_mcp get_records] ->", json.dumps(out))
    assert out["success"] is True
    assert out["content_type"] == "tabular"
    assert out["row_count"] == 3
    assert out["result"][0]["name"] == "Record 1"

    # 4) execute_mcp — echo (json).
    with _patch_provider(ctx["client"]):
        out, is_err = _tool_call(call, "execute_mcp", {
            "data_source_id": ds_id, "tool_name": "echo",
            "arguments": {"message": "hello gateway"},
        })
    print("[execute_mcp echo] ->", json.dumps(out))
    assert out["success"] is True
    assert out["result"]["echoed"] == "hello gateway"


@pytest.mark.e2e
def test_gateway_unknown_tool_returns_schema_help(
    create_user, login_user, whoami, create_api_key, enable_mcp,
    create_mcp_connection, create_domain_from_connection, test_client,
):
    """Calling a tool the agent doesn't expose fails gracefully."""
    ctx = _full_setup(
        create_user=create_user, login_user=login_user, whoami=whoami,
        create_api_key=create_api_key, enable_mcp=enable_mcp,
        create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    with _patch_provider(ctx["client"]):
        out, _ = _tool_call(ctx["call"], "execute_mcp", {
            "data_source_id": ctx["ds_id"], "tool_name": "does_not_exist",
            "arguments": {},
        })
    assert out["success"] is False
    assert "not found" in out["error_message"].lower()


@pytest.mark.e2e
def test_gateway_respects_disabled_tool(
    create_user, login_user, whoami, create_api_key, enable_mcp,
    create_mcp_connection, create_domain_from_connection,
    get_connection_tools, update_connection_tool, test_client,
):
    """A disabled ConnectionTool is hidden from discovery and blocked on execute."""
    ctx = _full_setup(
        create_user=create_user, login_user=login_user, whoami=whoami,
        create_api_key=create_api_key, enable_mcp=enable_mcp,
        create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    token, org_id, conn_id, ds_id = ctx["token"], ctx["org_id"], ctx["conn_id"], ctx["ds_id"]

    tools = get_connection_tools(connection_id=conn_id, user_token=token, org_id=org_id)
    echo_tool = next(t for t in tools if t["name"] == "echo")
    update_connection_tool(
        connection_id=conn_id, tool_id=echo_tool["id"],
        payload={"is_enabled": False}, user_token=token, org_id=org_id,
    )

    # Discovery no longer lists echo.
    listed, _ = _tool_call(ctx["call"], "list_agent_tools", {"data_source_ids": [ds_id]})
    assert "echo" not in {t["name"] for t in listed["tools"]}

    # Execute is blocked.
    with _patch_provider(ctx["client"]):
        out, _ = _tool_call(ctx["call"], "execute_mcp", {
            "data_source_id": ds_id, "tool_name": "echo", "arguments": {"message": "x"},
        })
    assert out["success"] is False
    assert "disabled" in out["error_message"].lower()


@pytest.mark.e2e
def test_gateway_blocks_non_allow_policy(
    create_user, login_user, whoami, create_api_key, enable_mcp,
    create_mcp_connection, create_domain_from_connection,
    get_connection_tools, update_connection_tool, test_client,
):
    """A tool with policy='deny' cannot be invoked through the gateway."""
    ctx = _full_setup(
        create_user=create_user, login_user=login_user, whoami=whoami,
        create_api_key=create_api_key, enable_mcp=enable_mcp,
        create_mcp_connection=create_mcp_connection,
        create_domain_from_connection=create_domain_from_connection,
        test_client=test_client,
    )
    token, org_id, conn_id, ds_id = ctx["token"], ctx["org_id"], ctx["conn_id"], ctx["ds_id"]

    tools = get_connection_tools(connection_id=conn_id, user_token=token, org_id=org_id)
    echo_tool = next(t for t in tools if t["name"] == "echo")
    update_connection_tool(
        connection_id=conn_id, tool_id=echo_tool["id"],
        payload={"policy": "deny"}, user_token=token, org_id=org_id,
    )

    with _patch_provider(ctx["client"]):
        out, _ = _tool_call(ctx["call"], "execute_mcp", {
            "data_source_id": ds_id, "tool_name": "echo", "arguments": {"message": "x"},
        })
    assert out["success"] is False
    assert "policy" in out["error_message"].lower()
