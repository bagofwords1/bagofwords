"""Live verification against a real *external* BOW instance (eu.bagofwords.com).

Proves the routing fix end-to-end: a local BOW agent (real Haiku) calls the
`create_report` MCP tool on an eu connection. `create_report` is also a BOW
built-in name — before the fix it was intercepted in-process and a report was
created LOCALLY; after the fix the call goes over HTTPS to eu, so the report
lands on eu (its URL host is eu.bagofwords.com) and nothing is created locally.

Requires ANTHROPIC_API_KEY_TEST and EU_MCP_TOKEN (a bow_ key for eu). Creates a
single clearly-labelled report on the eu workspace.

  ANTHROPIC_API_KEY_TEST=$ANTHROPIC_KEY EU_MCP_TOKEN=$EU_TOKEN \
    uv run pytest tests/e2e/test_mcp_forwarding_eu_live.py -m e2e -s
"""

import json
import os
import uuid

import httpx
import pytest

EU_URL = "https://eu.bagofwords.com/api/mcp"


def _eu_up(token: str) -> bool:
    try:
        httpx.get("https://eu.bagofwords.com/api/mcp", timeout=5)
        return True
    except Exception:
        return False


def _blob(obj) -> str:
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return str(obj)


@pytest.mark.e2e
def test_haiku_agent_routes_builtin_named_tool_to_eu(
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
    token_env = os.getenv("EU_MCP_TOKEN")
    if not os.getenv("ANTHROPIC_API_KEY_TEST"):
        pytest.skip("ANTHROPIC_API_KEY_TEST not set")
    if not token_env or not _eu_up(token_env):
        pytest.skip("EU_MCP_TOKEN not set or eu.bagofwords.com unreachable")

    user = create_user()
    tok = login_user(user["email"], user["password"])
    org_id = whoami(tok)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {tok}", "X-Organization-Id": str(org_id)}

    # Haiku-only provider → Haiku plans the turn.
    prov = test_client.post(
        "/api/llm/providers",
        json={
            "name": "anthropic haiku",
            "provider_type": "anthropic",
            "credentials": {"api_key": os.environ["ANTHROPIC_API_KEY_TEST"]},
            "models": [{"model_id": "claude-haiku-4-5-20251001", "name": "Claude 4.5 Haiku", "is_custom": False}],
        },
        headers=headers,
    )
    assert prov.status_code == 200, prov.text
    enable_mcp(tok, org_id)

    # MCP data source → eu, bearer token + identity forwarding.
    marker = f"bow-fwd-verify-{uuid.uuid4().hex[:8]}"
    ds = create_data_source(
        name="EU MCP",
        type="mcp",
        config={
            "server_url": EU_URL,
            "transport": "streamable_http",
            "auth_type": "bearer",
            "header_injection": [{"header": "X-User-Email", "source": "user.email"}],
            "metadata_injection": {
                "argument_key": "custom_metadata",
                "fields": [{"name": "user_email", "source": "user.email", "mode": "locked"}],
            },
        },
        credentials={"token": token_env},
        user_token=tok,
        org_id=org_id,
    )
    ds_id = ds["id"]
    conn_id = (ds.get("connections") or [{}])[0].get("id")
    if not conn_id:
        conns = test_client.get("/api/connections", headers=headers).json()
        conn_id = next((c["id"] for c in conns if c.get("name") == "EU MCP"), None)
    assert conn_id, f"no connection id in {_blob(ds)[:300]}"
    # Discover eu's tools (proves the connection reaches eu).
    refresh_connection_tools(connection_id=conn_id, user_token=tok, org_id=org_id)

    report = create_report(title="verify-host", user_token=tok, org_id=org_id, data_sources=[ds_id])

    completions = create_completion(
        report_id=report["id"],
        prompt=(
            f"Use the create_report MCP tool now to create a report titled '{marker}'. "
            "Call the tool directly; do not ask for confirmation."
        ),
        user_token=tok,
        org_id=org_id,
        background=False,
    )

    blob = _blob(completions)
    print("\n--- completion blob (truncated) ---\n", blob[:1500])

    # Decisive: the created report's URL points at eu, proving the call was routed
    # over HTTPS to eu rather than intercepted by the local in-process builtin.
    assert "eu.bagofwords.com" in blob, (
        "expected the create_report call to be routed to eu (eu.bagofwords.com URL "
        "in the tool result); if absent, the call was intercepted locally"
    )
    # And it must NOT have been created on the local instance.
    local_reports = test_client.get("/api/reports?limit=100", headers=headers).json()
    titles = [r.get("title") for r in (local_reports.get("reports") or local_reports if isinstance(local_reports, dict) else local_reports)]
    assert marker not in (titles or []), f"report '{marker}' was created LOCALLY, not on eu"
