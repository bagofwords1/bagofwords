"""
Verify backfill_org_connectors seeds an EXISTING (bare) org.

Flow: register admin (org auto-seeded) → strip the seeded mcp connectors via SQL
to simulate a pre-existing org → run backfill_org_connectors → assert the 5
integration agents are recreated.

Run from backend/: uv run python ../tools/sandbox/connector-mocks/verify_backfill.py
"""
import sys, uuid, asyncio
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

def mcp_count(tok, org):
    ds = C.get("/api/data_sources/active", headers=H(tok, org), params={"include_unconnected": "true"}).json()
    return len([d for d in ds if d.get("type") == "mcp"])

async def strip_and_backfill(org_id):
    from app.dependencies import async_session_maker
    from sqlalchemy import text
    from app.services.connector_seed_service import backfill_org_connectors
    # Strip seeded mcp connectors (links, memberships, data_sources, connections)
    # to simulate a pre-existing org that never ran the create-time seeding.
    async with async_session_maker() as db:
        conn_ids = [r[0] for r in (await db.execute(
            text("SELECT id FROM connections WHERE organization_id=:o AND type='mcp'"), {"o": org_id}
        )).fetchall()]
        ds_ids = []
        for cid in conn_ids:
            rows = (await db.execute(
                text("SELECT data_source_id FROM domain_connection WHERE connection_id=:c"), {"c": cid}
            )).fetchall()
            ds_ids += [r[0] for r in rows]
            await db.execute(text("DELETE FROM domain_connection WHERE connection_id=:c"), {"c": cid})
        for dsid in set(ds_ids):
            await db.execute(text("DELETE FROM data_source_memberships WHERE data_source_id=:d"), {"d": dsid})
            await db.execute(text("DELETE FROM data_sources WHERE id=:d"), {"d": dsid})
        for cid in conn_ids:
            await db.execute(text("DELETE FROM connections WHERE id=:c"), {"c": cid})
        await db.commit()
    return await backfill_org_connectors(async_session_maker)

def main():
    import main as _app  # noqa: F401 — registers all SQLAlchemy models/mappers
    print("=== backfill verification ===")
    email = f"admin-{uuid.uuid4().hex[:8]}@example.com"; pw = "Password123!"
    assert C.post("/api/auth/register", json={"name": "admin", "email": email, "password": pw}).status_code == 201
    tok = C.post("/api/auth/jwt/login", data={"username": email, "password": pw}).json()["access_token"]
    org = C.get("/api/users/whoami", headers=H(tok)).json()["organizations"][0]["id"]

    ck("org auto-seeded at create (≥5 mcp)", mcp_count(tok, org) >= 5, f"{mcp_count(tok, org)}")
    seeded = asyncio.run(strip_and_backfill(org))
    ck("strip removed seeded connectors then backfill re-created them", mcp_count(tok, org) >= 5, f"now {mcp_count(tok, org)}, backfill seeded {seeded}")

    print(f"\n  {sum(R)}/{len(R)} checks passed")
    return 0 if all(R) else 1

if __name__ == "__main__":
    sys.path.insert(0, ".")
    try: sys.exit(main())
    except AssertionError as e:
        print(f"ASSERT FAILED: {e}"); sys.exit(2)
