"""
Live e2e: outbound MCP Dynamic Client Registration against the REAL Notion MCP.

Opt-in (hits the network) — set BOW_TEST_NOTION_DCR=1 to run:
    BOW_TEST_NOTION_DCR=1 uv run pytest tests/e2e/test_dcr_notion.py -q

Verifies the authorize route discovers Notion's authorization server and
dynamically registers a client (RFC 9728/8414/7591), returning a Notion consent
URL with the DCR client_id + PKCE + RFC 8707 resource. The interactive consent
/ token exchange is manual and not covered here. The enterprise license needed
for user_required connections is force-enabled by tests/e2e/conftest.py.
"""
import os
import uuid
import pytest

NOTION = "https://mcp.notion.com/mcp"

pytestmark = pytest.mark.skipif(
    os.getenv("BOW_TEST_NOTION_DCR") != "1",
    reason="live Notion DCR test; set BOW_TEST_NOTION_DCR=1 to run",
)


@pytest.mark.e2e
def test_authorize_route_runs_dcr_against_notion(test_client, create_user, login_user, whoami):
    from urllib.parse import urlsplit, parse_qs

    user = create_user(email=f"dcr-{uuid.uuid4().hex[:8]}@example.com")
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    H = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    # Create an MCP connection to Notion with NO preconfigured OAuth client.
    r = test_client.post("/api/connections", json={
        "name": "Notion MCP", "type": "mcp",
        "config": {"server_url": NOTION, "transport": "streamable_http"},
        "credentials": {}, "auth_policy": "user_required",
        "allowed_user_auth_modes": ["oauth"],
    }, headers=H)
    assert r.status_code == 200, r.text
    conn_id = r.json()["id"]

    # Authorize route -> triggers discovery + DCR -> Notion consent URL.
    r = test_client.get(f"/api/connections/{conn_id}/oauth/authorize", headers=H)
    assert r.status_code == 200, r.text
    url = r.json()["authorization_url"]
    parts = urlsplit(url)
    q = parse_qs(parts.query)

    assert parts.netloc == "mcp.notion.com" and parts.path == "/authorize", url
    assert q.get("client_id", [None])[0], "missing DCR client_id"
    assert "code_challenge" in q and q.get("code_challenge_method", [None])[0] == "S256"
    assert q.get("resource", [None])[0] == NOTION, "missing RFC 8707 resource binding"

    # The connection now persists the DCR-registered client + endpoints.
    from app.models.connection import Connection
    # (sanity) the route stored client_id on the connection credentials
    # via ensure_mcp_oauth_config — re-authorize is idempotent.
    r2 = test_client.get(f"/api/connections/{conn_id}/oauth/authorize", headers=H)
    assert r2.status_code == 200, r2.text
    q2 = parse_qs(urlsplit(r2.json()["authorization_url"]).query)
    assert q2.get("client_id", [None])[0] == q.get("client_id", [None])[0], "client_id should be stable (idempotent DCR)"
