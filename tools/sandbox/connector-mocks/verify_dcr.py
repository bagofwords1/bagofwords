"""
Verify the DCR mock end-to-end at the protocol level (RFC 9728/8414/7591 + OAuth
code flow), then call its MCP tools with the issued token via the real McpClient.

This is the flow the backend outbound-DCR work (design Phase 4) will implement;
the mock + this script prove the handshake before that lands.

Run (with mock_mcp_dcr running on :9302):
  uv run python tools/sandbox/connector-mocks/verify_dcr.py
"""
import os, sys, httpx

BASE = os.environ.get("DCR_URL", "http://localhost:9302")
CB = "http://localhost:8000/api/connections/oauth/callback"  # BOW callback (placeholder)
R = []
def ck(name, ok, d=""):
    ok = bool(ok); R.append(ok); print(f"  [{'PASS ✅' if ok else 'FAIL ❌'}] {name}" + (f" — {d}" if d else ""))

def main():
    print("=== DCR handshake verification ===")
    h = httpx.Client(timeout=30.0, follow_redirects=False)

    # 1) Protected-resource discovery (RFC 9728)
    pr = h.get(f"{BASE}/.well-known/oauth-protected-resource").json()
    ck("protected-resource advertises authorization_servers", bool(pr.get("authorization_servers")), str(pr.get("authorization_servers")))
    as_url = pr["authorization_servers"][0]

    # 2) AS metadata (RFC 8414) incl registration_endpoint
    md = h.get(f"{as_url}/.well-known/oauth-authorization-server").json()
    ck("AS metadata exposes registration_endpoint", bool(md.get("registration_endpoint")), md.get("registration_endpoint"))
    for k in ("authorization_endpoint", "token_endpoint"):
        ck(f"AS metadata has {k}", bool(md.get(k)))

    # 3) Dynamic Client Registration (RFC 7591) — no preconfigured client
    reg = h.post(md["registration_endpoint"], json={
        "client_name": "BagOfWords (test)",
        "redirect_uris": [CB],
        "grant_types": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_method": "client_secret_post",
    })
    ck("DCR returns 201 with client_id", reg.status_code == 201 and "client_id" in reg.json(), f"status={reg.status_code}")
    client = reg.json(); client_id = client["client_id"]; client_secret = client.get("client_secret")

    # 4) Authorize -> code
    az = h.get(md["authorization_endpoint"], params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CB,
        "state": "xyz", "resource": pr["resource"],
    })
    loc = az.headers.get("location", "")
    code = None
    if "code=" in loc:
        from urllib.parse import urlparse, parse_qs
        code = parse_qs(urlparse(loc).query).get("code", [None])[0]
    ck("authorize issued an auth code", bool(code), loc[:80])

    # 5) Token exchange
    tok = h.post(md["token_endpoint"], data={
        "grant_type": "authorization_code", "code": code,
        "redirect_uri": CB, "client_id": client_id, "client_secret": client_secret,
    }).json()
    access = tok.get("access_token")
    ck("token endpoint issued access_token", bool(access))

    # 6) Use the token with the REAL McpClient against the gated /mcp endpoint
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))
    from app.data_sources.clients.mcp_client import McpClient
    # Unauthed should fail
    bad = McpClient(server_url=f"{BASE}/mcp", transport="streamable_http")
    ck("unauthenticated MCP call rejected", bad.test_connection().get("success") is False)
    # Authed with the DCR-issued token should work
    good = McpClient(server_url=f"{BASE}/mcp", transport="streamable_http", access_token=access)
    tc = good.test_connection()
    ck("authenticated MCP connects", tc.get("success") is True, tc.get("message"))
    tools = good.list_tools() if tc.get("success") else []
    ck("tools discovered over DCR-authed session", {"search_emails", "send_email"} <= {t["name"] for t in tools}, str([t["name"] for t in tools]))
    if tools:
        res = good.call_tool("search_emails", {"query": "Q2"})
        ck("tool call returns data", res.get("success") and res.get("data"), str(res.get("data"))[:80])

    print(f"\n  {sum(R)}/{len(R)} DCR checks passed")
    return 0 if all(R) else 1

if __name__ == "__main__":
    sys.exit(main())
