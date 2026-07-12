"""Live HTTP + service verification for the per-connection request rate limit
(enterprise `connection_rate_limit`).

Drives a running server (python main.py) over real HTTP:
  * license is active and carries the feature,
  * PUT /connections/{id} persists the rate-limit config (0 => "no limit"),
  * the enforcement service hard-blocks past a per-window cap and audit-logs
    exactly once per window.

Run from backend/ with the server up on :8000 and an enterprise license loaded.
"""
import asyncio
import os
import sys
import uuid

import httpx

BASE = os.environ.get("BOW_BASE_URL", "http://localhost:8000")
ADMIN = {"name": "RL Admin", "email": f"rladmin_{uuid.uuid4().hex[:6]}@acme.com", "password": "supersecret123"}

_passed = 0
_failed = 0


def check(label, cond, extra=""):
    global _passed, _failed
    mark = "PASS" if cond else "FAIL"
    if cond:
        _passed += 1
    else:
        _failed += 1
    print(f"{mark} - {label}" + (f"   >> {extra}" if extra and not cond else ""))


def main():
    c = httpx.Client(base_url=BASE, timeout=30)

    # ---- auth ----
    c.post("/api/auth/register", json=ADMIN)
    r = c.post("/api/auth/jwt/login", data={"username": ADMIN["email"], "password": ADMIN["password"]})
    token = r.json()["access_token"]
    admin_h = {"Authorization": f"Bearer {token}"}
    orgs = c.get("/api/organizations", headers=admin_h).json()
    if not orgs:
        c.post("/api/organizations", headers=admin_h, json={"name": f"RL Org {uuid.uuid4().hex[:6]}"})
        orgs = c.get("/api/organizations", headers=admin_h).json()
    org_id = orgs[0]["id"]
    h = {**admin_h, "X-Organization-Id": org_id}

    # 1. license carries the feature
    lic = c.get("/api/license", headers=h).json()
    check("license active, enterprise tier", lic.get("licensed") and lic.get("tier") == "enterprise", lic)

    # ---- create a connection directly through the service layer (no live DB
    #      needed to probe) so we can drive the config + enforcement paths ----
    conn_id = asyncio.run(_seed_connection(org_id))
    check("connection created", bool(conn_id), conn_id)

    # 2. detail exposes the rate-limit fields, defaulted off
    d = c.get(f"/api/connections/{conn_id}", headers=h).json()
    check("detail: rate_limit_enabled defaults False", d.get("rate_limit_enabled") is False, d)
    check("detail: per-window defaults None",
          d.get("rate_limit_per_minute") is None and d.get("rate_limit_per_hour") is None, d)

    # 3. PUT persists caps; 0 stored as "no limit"
    r = c.put(f"/api/connections/{conn_id}", headers=h, json={
        "rate_limit_enabled": True,
        "rate_limit_per_minute": 3,
        "rate_limit_per_hour": 0,
        "rate_limit_per_day": 5000,
    })
    check("PUT rate-limit config -> 200", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    d = c.get(f"/api/connections/{conn_id}", headers=h).json()
    check("persisted: enabled + per_minute=3", d.get("rate_limit_enabled") and d.get("rate_limit_per_minute") == 3, d)
    check("persisted: per_hour 0 = no limit", d.get("rate_limit_per_hour") == 0, d)
    check("persisted: per_day=5000", d.get("rate_limit_per_day") == 5000, d)

    # 4. negative value rejected
    r = c.put(f"/api/connections/{conn_id}", headers=h, json={"rate_limit_per_minute": -5})
    check("negative cap rejected (400/422)", r.status_code in (400, 422), r.status_code)

    # 5. enforcement: first 3 pass, 4th+ blocked; audit logged once
    blocked, audit_count, window = asyncio.run(_drive_enforcement(conn_id))
    check("4th request blocked (RateLimitExceeded)", blocked, blocked)
    check("blocked window is 'minute'", window == "minute", window)
    check("audit logged exactly once per window", audit_count == 1, f"audit rows={audit_count}")

    print("\n==== SUMMARY ====")
    print(f"{_passed}/{_passed + _failed} passed")
    print("ALL PASSED" if _failed == 0 else "SOME FAILED")
    sys.exit(0 if _failed == 0 else 1)


async def _seed_connection(org_id):
    import main  # noqa: F401 - loads the full ORM registry so mappers configure
    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    async with async_session_maker() as db:
        conn = Connection(organization_id=org_id, name=f"RL Verify {uuid.uuid4().hex[:6]}",
                          type="sqlite", config={}, credentials=None)
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        return str(conn.id)


async def _drive_enforcement(conn_id):
    import main  # noqa: F401 - loads the full ORM registry so mappers configure
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.ee.audit.models import AuditLog
    from app.services.connection_rate_limit_service import (
        RateLimitExceeded, connection_rate_limit_service,
    )

    async def consume():
        async with async_session_maker() as db:
            await connection_rate_limit_service.check_and_consume_by_id(
                db, connection_id=conn_id, user_id=None,
                metadata={"sql": "SELECT 1", "data_source_name": "verify"},
            )

    for _ in range(3):
        await consume()  # under the cap of 3
    blocked = False
    window = None
    for _ in range(2):
        try:
            await consume()
        except RateLimitExceeded as e:
            blocked = True
            window = e.window

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(AuditLog).where(AuditLog.action == "connection.rate_limit_exceeded",
                                   AuditLog.resource_id == conn_id)
        )).scalars().all()
    return blocked, len(rows), window


if __name__ == "__main__":
    main()
