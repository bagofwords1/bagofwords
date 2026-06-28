"""
Definitive end-to-end: org auto-seeds the Notion integration → user clicks
"Connect" on the seeded agent → authorize route runs DCR vs REAL Notion →
returns a Notion consent URL. (Token exchange is the manual consent step.)

Needs a running backend + fresh DB (registers the bootstrap admin).
Run from backend/: uv run python ../tools/sandbox/connector-mocks/verify_seeded_connect.py
"""
import sys, uuid
from urllib.parse import urlsplit, parse_qs
import httpx

BASE = "http://localhost:8000"
C = httpx.Client(base_url=BASE, timeout=60.0)
R = []
def ck(n, ok, d=""):
    ok = bool(ok); R.append(ok); print(f"  [{'PASS ✅' if ok else 'FAIL ❌'}] {n}" + (f" — {d}" if d else ""))
def H(t, o=None):
    h = {"Authorization": f"Bearer {t}"}
    if o: h["X-Organization-Id"] = str(o)
    return h

def main():
    print("=== Seeded integration → Connect (DCR) end-to-end ===")
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"; pw = "Password123!"
    assert C.post("/api/auth/register", json={"name": "admin", "email": email, "password": pw}).status_code == 201
    tok = C.post("/api/auth/jwt/login", data={"username": email, "password": pw}).json()["access_token"]
    org = C.get("/api/users/whoami", headers=H(tok)).json()["organizations"][0]["id"]

    # Find the seeded Notion integration agent + its connection
    ds = C.get("/api/data_sources/active", headers=H(tok, org), params={"include_unconnected": "true"}).json()
    notion = next((d for d in ds if d["name"] == "Notion"), None)
    ck("Notion integration was auto-seeded", notion is not None)
    if not notion:
        return 1
    conns = notion.get("connections") or []
    ck("seeded Notion agent has a connection", len(conns) == 1, str([c.get("type") for c in conns]))
    conn_id = conns[0]["id"]
    ck("connection is user_required + oauth", conns[0].get("auth_policy") == "user_required")

    # Click "Connect" → authorize route runs discovery + DCR vs real Notion
    r = C.get(f"/api/connections/{conn_id}/oauth/authorize", headers=H(tok, org))
    ck("authorize route returns 200 (ran DCR)", r.status_code == 200, f"{r.status_code} {r.text[:160]}")
    if r.status_code == 200:
        url = r.json()["authorization_url"]; q = parse_qs(urlsplit(url).query)
        ck("consent URL targets mcp.notion.com", urlsplit(url).netloc == "mcp.notion.com", url[:50])
        ck("consent URL has DCR client_id", bool(q.get("client_id", [None])[0]), q.get("client_id", [None])[0])
        ck("consent URL has PKCE + resource", "code_challenge" in q and "resource" in q)
        print(f"\n  Manual consent URL (sign into Notion to finish):\n  {url[:120]}...")

    print(f"\n  {sum(R)}/{len(R)} checks passed")
    return 0 if all(R) else 1

if __name__ == "__main__":
    try: sys.exit(main())
    except AssertionError as e:
        print(f"ASSERT FAILED: {e}"); sys.exit(2)
