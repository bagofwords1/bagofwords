"""
Verify catalog endpoint + org-creation auto-seed against a running backend
(seeding ON by default in dev config). Registers the bootstrap admin → org is
created → default DCR integrations seeded → assert they appear as public
connector agents.

Run from backend/: uv run python ../tools/sandbox/connector-mocks/verify_seed_catalog.py
"""
import sys, uuid, httpx

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
    print("=== Catalog + auto-seed verification ===")
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"; pw = "Password123!"
    assert C.post("/api/auth/register", json={"name": "admin", "email": email, "password": pw}).status_code == 201
    tok = C.post("/api/auth/jwt/login", data={"username": email, "password": pw}).json()["access_token"]
    org = C.get("/api/users/whoami", headers=H(tok)).json()["organizations"][0]["id"]

    # 1) Catalog endpoint
    cat = C.get("/api/connectors/catalog", headers=H(tok, org))
    ck("GET /connectors/catalog returns 200", cat.status_code == 200, str(cat.status_code))
    keys = {e["key"] for e in cat.json()} if cat.status_code == 200 else set()
    ck("catalog includes the DCR set", {"monday","notion","atlassian","linear","sentry"} <= keys, str(sorted(keys)))
    auto = {e["key"] for e in cat.json() if e.get("auto_seed")}
    ck("auto_seed flagged for DCR set", {"monday","notion","linear","sentry"} <= auto, str(sorted(auto)))

    # 2) Org auto-seeded the integration agents
    ds = C.get("/api/data_sources/active", headers=H(tok, org), params={"include_unconnected": "true"}).json()
    by_name = {d["name"]: d for d in ds}
    ck("Monday integration agent seeded", "Monday" in by_name)
    ck("Notion integration agent seeded", "Notion" in by_name)
    seeded = [d for d in ds if d.get("type") == "mcp"]
    ck("≥5 mcp integration agents seeded", len(seeded) >= 5, f"got {len(seeded)}")
    if "Monday" in by_name:
        m = by_name["Monday"]
        ck("seeded agent is public", m.get("is_public") is True, f"is_public={m.get('is_public')}")
        ck("seeded agent flagged is_connector", bool(m.get("is_connector")), f"is_connector={m.get('is_connector')}")
        ck("seeded agent auth_policy=user_required", m.get("auth_policy") == "user_required", f"auth_policy={m.get('auth_policy')}")

    print(f"\n  {sum(R)}/{len(R)} checks passed")
    return 0 if all(R) else 1

if __name__ == "__main__":
    try: sys.exit(main())
    except AssertionError as e:
        print(f"ASSERT FAILED: {e}"); sys.exit(2)
