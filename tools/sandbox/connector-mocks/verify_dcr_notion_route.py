"""
App-route verification of outbound DCR against REAL Notion: create an MCP
connection via the API, then hit the authorize route — which should run
discovery + DCR and return a Notion consent URL. (Token exchange is the manual
consent step.) Requires a running backend + fresh DB (registers the bootstrap admin).

Run from backend/: uv run python ../tools/sandbox/connector-mocks/verify_dcr_notion_route.py
"""
import sys, uuid
from urllib.parse import urlsplit, parse_qs
import httpx

BASE = "http://localhost:8000"
NOTION = "https://mcp.notion.com/mcp"
C = httpx.Client(base_url=BASE, timeout=60.0)
R = []
def ck(n, ok, d=""):
    ok = bool(ok); R.append(ok); print(f"  [{'PASS ✅' if ok else 'FAIL ❌'}] {n}" + (f" — {d}" if d else ""))

def H(t, o=None):
    h = {"Authorization": f"Bearer {t}"}
    if o: h["X-Organization-Id"] = str(o)
    return h

def main():
    print("=== App-route DCR vs REAL Notion ===")
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"; pw = "Password123!"
    assert C.post("/api/auth/register", json={"name": "admin", "email": email, "password": pw}).status_code == 201
    tok = C.post("/api/auth/jwt/login", data={"username": email, "password": pw}).json()["access_token"]
    org = C.get("/api/users/whoami", headers=H(tok)).json()["organizations"][0]["id"]

    # Create an MCP connection to Notion, user_required + oauth (no client creds — DCR will register).
    r = C.post("/api/connections", headers=H(tok, org), json={
        "name": "Notion MCP", "type": "mcp",
        "config": {"server_url": NOTION, "transport": "streamable_http"},
        "credentials": {}, "auth_policy": "user_required",
        "allowed_user_auth_modes": ["oauth"],
    })
    ck("created Notion MCP connection (user_required/oauth)", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        return 1
    conn_id = r.json()["id"]

    # Authorize route -> triggers discovery + DCR, returns Notion consent URL.
    r = C.get(f"/api/connections/{conn_id}/oauth/authorize", headers=H(tok, org))
    ck("authorize route returns 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    if r.status_code != 200:
        return 1
    url = r.json().get("authorization_url", "")
    q = parse_qs(urlsplit(url).query)
    ck("authorize URL targets mcp.notion.com/authorize", urlsplit(url).netloc == "mcp.notion.com" and urlsplit(url).path == "/authorize", url[:60])
    ck("authorize URL carries a DCR-registered client_id", bool(q.get("client_id", [None])[0]), q.get("client_id", [None])[0])
    ck("authorize URL carries PKCE challenge", "code_challenge" in q)
    ck("authorize URL carries resource (RFC 8707)", q.get("resource", [None])[0] == NOTION, q.get("resource", [None])[0])

    print(f"\n  {sum(R)}/{len(R)} route checks passed")
    print(f"\n  Manual consent URL:\n  {url}\n")
    return 0 if all(R) else 1

if __name__ == "__main__":
    try: sys.exit(main())
    except AssertionError as e:
        print(f"ASSERT FAILED: {e}"); sys.exit(2)
