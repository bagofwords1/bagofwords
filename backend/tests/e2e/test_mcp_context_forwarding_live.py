"""Live end-to-end: a real Haiku agent turn forwards user context to an MCP server.

Unlike the unit tests (resolver logic) and the wire smoke test (McpClient), this
drives the *whole* stack — a real Anthropic **Haiku** model plans the turn,
decides to call the MCP tool, and BOW injects the signed-in user's identity into
the outbound headers + `custom_metadata` before the call reaches a real
streamable-HTTP MCP server.

Requires:
  * ANTHROPIC_API_KEY_TEST  — a working Anthropic key
  * the echo MCP server running on :3333 with MOCK_MCP_CAPTURE_FILE set
    (tests/mocks/echo_mcp_http_server.py). The test reads that capture file.

Run:
  MOCK_MCP_CAPTURE_FILE=/tmp/bow-agent/mcp_capture.json \
  ANTHROPIC_API_KEY_TEST=$ANTHROPIC_KEY \
    uv run pytest tests/e2e/test_mcp_context_forwarding_live.py -m e2e -s
"""

import json
import os

import httpx
import pytest

ECHO_URL = os.environ.get("ECHO_MCP_URL", "http://127.0.0.1:3333/mcp")
CAPTURE = os.environ.get("MOCK_MCP_CAPTURE_FILE", "/tmp/bow-agent/mcp_capture.json")


def _echo_up() -> bool:
    try:
        httpx.get(ECHO_URL, timeout=3)
        return True
    except Exception:
        return False


@pytest.mark.e2e
def test_haiku_agent_forwards_user_context_to_mcp(
    test_client,
    create_user,
    login_user,
    whoami,
    create_data_source,
    create_report,
    create_completion,
    refresh_connection_tools,
    enable_mcp,
):
    if not os.getenv("ANTHROPIC_API_KEY_TEST"):
        pytest.skip("ANTHROPIC_API_KEY_TEST is not set")
    if not _echo_up():
        pytest.skip(f"echo MCP server not reachable at {ECHO_URL}")

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    user_email = user["email"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    # Anthropic provider with Haiku only → Haiku is the default planner model.
    prov = test_client.post(
        "/api/llm/providers",
        json={
            "name": "anthropic haiku",
            "provider_type": "anthropic",
            "credentials": {"api_key": os.environ["ANTHROPIC_API_KEY_TEST"]},
            "models": [
                {"model_id": "claude-haiku-4-5-20251001", "name": "Claude 4.5 Haiku", "is_custom": False},
            ],
        },
        headers=headers,
    )
    assert prov.status_code == 200, prov.text

    enable_mcp(token, org_id)

    # MCP data source → echo server, with user-context forwarding configured.
    forwarding = {
        "server_url": ECHO_URL,
        "transport": "streamable_http",
        "header_injection": [
            {"header": "X-User-Email", "source": "user.email"},
        ],
        "metadata_injection": {
            "argument_key": "custom_metadata",
            "fields": [
                {"name": "_client_userId", "source": "membership.role", "mode": "locked"},
                {"name": "user_email", "source": "user.email", "mode": "locked"},
                {"name": "application_name", "source": "static:BagOfWords", "mode": "locked"},
            ],
        },
    }
    ds = create_data_source(
        name="Echo LN MCP",
        type="mcp",
        config=forwarding,
        credentials={},
        user_token=token,
        org_id=org_id,
    )
    ds_id = ds["id"]
    # The MCP data source owns a connection; discover its tools against the echo server.
    conn_id = None
    for c in (ds.get("connections") or []):
        conn_id = c.get("id")
    if conn_id is None:
        # Fall back to listing connections for this org.
        conns = test_client.get("/api/connections", headers=headers).json()
        conn_id = next((c["id"] for c in conns if c.get("name") == "Echo LN MCP"), None)
    assert conn_id, f"could not resolve MCP connection id from {json.dumps(ds)[:400]}"
    refresh_connection_tools(connection_id=conn_id, user_token=token, org_id=org_id)

    report = create_report(title="LN Orders", user_token=token, org_id=org_id, data_sources=[ds_id])

    # Wipe any stale capture, then let Haiku drive the tool call.
    try:
        os.remove(CAPTURE)
    except FileNotFoundError:
        pass

    create_completion(
        report_id=report["id"],
        prompt=(
            "Call the query_production_orders MCP tool now. "
            "Pass company='111' and prompt='weekly production orders'. "
            "Do not ask for confirmation; just call it."
        ),
        user_token=token,
        org_id=org_id,
        background=False,
    )

    # The echo server recorded exactly what it received over the wire.
    assert os.path.exists(CAPTURE), "echo server never received a tool call — Haiku did not invoke it"
    captured = json.load(open(CAPTURE))
    cm = captured["received_arguments"]["custom_metadata"]
    hdrs = captured["received_headers"]

    # Locked identity fields injected by the server (model never set these).
    assert cm.get("user_email") == user_email, cm
    assert cm.get("application_name") == "BagOfWords", cm
    assert cm.get("_client_userId"), cm  # resolved from membership.role (non-empty)
    # Identity header forwarded on the wire.
    assert hdrs.get("x-user-email") == user_email, hdrs
