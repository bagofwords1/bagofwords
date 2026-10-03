#!/usr/bin/env python3
"""Seed a realistic spread of audit events for the Audit Logs page.

Most events come from real API actions (API key create/revoke, report
create/update/delete), so they carry real IPs and user agents. A handful of
event *shapes* the API can't produce on demand in a fresh sandbox — tool/agent
events and three-segment actions such as ``artifact.record.created`` or
``connection.custom_query.created`` — are written through ``audit_service.log``
directly, the same function every call site uses.

Run (stack up, org seeded with tools/agent/seed_org.py):

    cd backend
    uv run python ../tools/agent/seed_audit_events.py [--base-url http://localhost:8000]
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, "backend"))


def _login(client: httpx.Client, email: str, password: str) -> str:
    r = client.post("/api/auth/jwt/login", data={"username": email, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


def _api_events(base_url: str, email: str, password: str) -> tuple[str, str]:
    with httpx.Client(base_url=base_url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (seed_audit_events)"}) as c:
        token = _login(c, email, password)
        me = c.get("/api/users/whoami", headers={"Authorization": f"Bearer {token}"}).json()
        org_id = me["organizations"][0]["id"]
        h = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}
        key = c.post("/api/api_keys", json={"name": "SIEM poller"}, headers=h)
        if key.status_code == 200:
            c.delete(f"/api/api_keys/{key.json()['id']}", headers=h)
        c.post("/api/api_keys", json={"name": "Nightly export"}, headers=h)
        rep = c.post("/api/reports", json={"title": "Q3 Revenue"}, headers=h)
        if rep.status_code == 200:
            rid = rep.json()["id"]
            c.put(f"/api/reports/{rid}", json={"title": "Q3 Revenue — final"}, headers=h)
        return org_id, me["id"]


async def _shape_events(org_id: str, user_id: str) -> int:
    os.environ.setdefault("TESTING", "true")
    import main  # noqa: F401  registers every ORM model before the first query
    from app.dependencies import async_session_maker
    from app.ee.audit.service import audit_service

    rows = [
        dict(action="artifact.record.created", resource_type="artifact_resource",
             details={"title": "Churn dashboard", "record_count": 42}),
        dict(action="artifact.resource.created", resource_type="artifact_resource",
             details={"title": "Churn dashboard"}),
        dict(action="connection.custom_query.created", resource_type="custom_query",
             details={"title": "active_customers", "connection": "Demo Postgres"}),
        dict(action="tool.data_queried", resource_type="data_source",
             details={"tool": "create_data", "data_source": "Demo Postgres",
                      "queries": ["SELECT region, SUM(amount) FROM sales GROUP BY 1"],
                      "row_count": 12, "agent_execution_id": "c0ffee00-0000-4000-8000-000000000001",
                      "execution_mode": "chat"}),
        dict(action="tool.access_blocked_by_policy", resource_type="data_source",
             details={"tool": "inspect_data", "reason": "table not granted",
                      "agent_execution_id": "c0ffee00-0000-4000-8000-000000000002"}),
        dict(action="settings.updated", resource_type="organization", user_id=None,
             details={"title": "Nightly retention sweep", "changed": {"step_retention_days": [30, 14]}}),
    ]
    async with async_session_maker() as s:
        for r in rows:
            await audit_service.log(
                db=s, organization_id=org_id, action=r["action"],
                user_id=r.get("user_id", user_id), resource_type=r["resource_type"],
                resource_id=None, details=r["details"],
            )
    return len(rows)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base-url", default="http://localhost:8000")
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--password", default="Password123!")
    a = p.parse_args()
    org_id, user_id = _api_events(a.base_url, a.email, a.password)
    n = asyncio.run(_shape_events(org_id, user_id))
    print(f"seeded API events + {n} shaped events for org {org_id}")


if __name__ == "__main__":
    main()
