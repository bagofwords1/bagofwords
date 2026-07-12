"""Per-connection request rate limit (enterprise `connection_rate_limit`).

Covers:
  - detail payload exposes the rate-limit fields with off/None defaults,
  - PUT is enterprise-gated (402 unlicensed) and persists when licensed,
    including 0 -> "no limit",
  - the enforcement service hard-blocks past a per-window cap and writes
    exactly one audit-log entry per window (on the under->over transition),
  - a disabled connection is never throttled.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.ee import license as ee_license
from app.ee.audit.models import AuditLog
from app.models.connection import Connection
from app.models.connection_rate_limit_counter import ConnectionRateLimitCounter
from app.services.connection_rate_limit_service import (
    RateLimitExceeded,
    connection_rate_limit_service,
)


def _run(coro):
    return asyncio.run(coro)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


@pytest.fixture(autouse=True)
def _enable_rate_limit_license():
    """Every test in this module sees connection_rate_limit as licensed."""
    saved_cached = ee_license._cached_license
    saved_initialized = ee_license._cache_initialized
    ee_license._cached_license = ee_license.LicenseInfo(
        licensed=True,
        tier="enterprise",
        org_name="tests",
        features=["connection_rate_limit"],
        license_id="test-rate-limit",
    )
    ee_license._cache_initialized = True
    try:
        yield
    finally:
        ee_license._cached_license = saved_cached
        ee_license._cache_initialized = saved_initialized


async def _create_connection(org_id, name, **rate_limit):
    async with async_session_maker() as db:
        conn = Connection(
            organization_id=org_id,
            name=name,
            type="sqlite",
            config={},
            credentials=None,
            **rate_limit,
        )
        db.add(conn)
        await db.commit()
        await db.refresh(conn)
        return str(conn.id)


async def _consume(conn_id, user_id=None):
    async with async_session_maker() as db:
        await connection_rate_limit_service.check_and_consume_by_id(
            db,
            connection_id=conn_id,
            user_id=user_id,
            metadata={"sql": "SELECT 1", "data_source_name": "ds"},
        )


async def _audit_count(org_id):
    async with async_session_maker() as db:
        result = await db.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org_id,
                AuditLog.action == "connection.rate_limit_exceeded",
            )
        )
        return list(result.scalars().all())


async def _counter_rows(conn_id):
    async with async_session_maker() as db:
        result = await db.execute(
            select(ConnectionRateLimitCounter).where(
                ConnectionRateLimitCounter.connection_id == conn_id,
            )
        )
        return list(result.scalars().all())


def _bootstrap_admin(create_user, login_user, whoami):
    user = create_user(email=f"ratelimit_{uuid.uuid4().hex[:8]}@test.com")
    token = login_user(user["email"], user["password"])
    profile = whoami(token)
    return token, profile["organizations"][0]["id"], profile["id"]


def _get_detail(test_client, cid, token, org_id):
    resp = test_client.get(f"/api/connections/{cid}", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


@pytest.mark.e2e
def test_rate_limit_detail_defaults_and_ee_gate(
    test_client, create_user, login_user, whoami, monkeypatch
):
    """Detail exposes the fields; PUT is EE-gated and persists (0 => no limit)."""
    token, org_id, _ = _bootstrap_admin(create_user, login_user, whoami)
    # Create directly in the DB — the connectivity probe the create endpoint runs
    # is irrelevant to this test and would reject an empty scratch sqlite.
    cid = _run(_create_connection(org_id, f"RL Conn {uuid.uuid4().hex[:6]}"))

    detail = _get_detail(test_client, cid, token, org_id)
    assert detail["rate_limit_enabled"] is False
    assert detail["rate_limit_per_minute"] is None
    assert detail["rate_limit_per_hour"] is None
    assert detail["rate_limit_per_day"] is None

    import app.ee.license as lic
    headers = _headers(token, org_id)

    # Unlicensed: rejected with 402.
    monkeypatch.setattr(lic, "has_feature", lambda feature: False)
    resp = test_client.put(
        f"/api/connections/{cid}",
        json={"rate_limit_enabled": True, "rate_limit_per_minute": 60},
        headers=headers,
    )
    assert resp.status_code == 402, resp.json()

    # Licensed: persists, and 0 stores as "no limit".
    monkeypatch.setattr(lic, "has_feature", lambda feature: True)
    resp = test_client.put(
        f"/api/connections/{cid}",
        json={
            "rate_limit_enabled": True,
            "rate_limit_per_minute": 60,
            "rate_limit_per_hour": 0,
            "rate_limit_per_day": 5000,
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    detail = _get_detail(test_client, cid, token, org_id)
    assert detail["rate_limit_enabled"] is True
    assert detail["rate_limit_per_minute"] == 60
    assert detail["rate_limit_per_hour"] == 0
    assert detail["rate_limit_per_day"] == 5000

    # Negative value => 400.
    resp = test_client.put(
        f"/api/connections/{cid}",
        json={"rate_limit_per_minute": -1},
        headers=headers,
    )
    assert resp.status_code in (400, 422), resp.json()


@pytest.mark.e2e
def test_rate_limit_enforcement_and_audit(create_user, login_user, whoami):
    """Blocks past the cap; audit logs exactly once per window."""
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(
        _create_connection(
            org_id,
            f"RL Enforce {uuid.uuid4().hex[:6]}",
            rate_limit_enabled=True,
            rate_limit_per_minute=3,
        )
    )

    # First 3 pass.
    for _ in range(3):
        _run(_consume(conn_id, user_id))

    # 4th and 5th are blocked.
    with pytest.raises(RateLimitExceeded) as exc_info:
        _run(_consume(conn_id, user_id))
    assert exc_info.value.window == "minute"
    assert exc_info.value.limit == 3

    with pytest.raises(RateLimitExceeded):
        _run(_consume(conn_id, user_id))

    # Audit: one entry only (logged on the first breach / transition).
    logs = _run(_audit_count(org_id))
    assert len(logs) == 1, [l.details for l in logs]
    assert logs[0].details["window"] == "minute"
    assert logs[0].details["limit"] == 3
    assert logs[0].resource_id == conn_id
    assert logs[0].user_id == user_id


@pytest.mark.e2e
def test_rate_limit_disabled_never_blocks(create_user, login_user, whoami):
    """A connection with the toggle off is never throttled and writes no counters."""
    _, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    conn_id = _run(
        _create_connection(
            org_id,
            f"RL Off {uuid.uuid4().hex[:6]}",
            rate_limit_enabled=False,
            rate_limit_per_minute=1,  # cap set, but feature toggled off
        )
    )

    for _ in range(10):
        _run(_consume(conn_id, user_id))  # no raise

    assert _run(_counter_rows(conn_id)) == []
