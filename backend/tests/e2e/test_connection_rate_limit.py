"""Per-connection request rate limit (enterprise `connection_rate_limit`).

Covers:
  - detail payload exposes the rate-limit fields with off/None defaults,
  - PUT is enterprise-gated (402 unlicensed) and persists when licensed,
    including 0 -> "no limit",
  - the enforcement service hard-blocks past a per-window cap and writes
    exactly one audit-log entry per window (on the under->over transition),
  - a disabled connection is never throttled,
  - entity runs, viewer runs with parameter values, entity previews and
    code previews count against the limit, and their queries are metered
    under their own usage source (persisted usage events), errors included.
"""

import asyncio
import sqlite3
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.ee import license as ee_license
from app.ee.audit.models import AuditLog
from app.models.connection import Connection
from app.models.connection_rate_limit_counter import ConnectionRateLimitCounter
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.usage_policy import UsageEvent
from app.services.connection_rate_limit_service import (
    RateLimitExceeded,
    connection_rate_limit_service,
)
from app.services.usage_policy_service import METRIC_DATA_QUERIES
from tests.fixtures.client import _ReleasingTestClient


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
        features=["connection_rate_limit", "usage_limits"],
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


# ── Entity runs and previews are rate-limited and metered ────────────────

_ENTITY_CODE = """
def generate_df(ds_clients, excel_files, params):
    client = [c for key, c in ds_clients.items() if ":" in key][0]
    return client.execute_query("SELECT COUNT(*) AS n FROM orders")
"""
_V_SPEC = {"name": "v", "type": "number", "label": "V", "default": None, "required": False, "source": "input"}


async def _seed_sqlite_agent(org_id, owner_id, db_path, **rate_limit):
    """A connection over a local SQLite file, its agent and an activated
    `orders` table. Direct model writes: no route creates a connection over a
    local file together with an indexed catalog (same as
    test_agent_table_selection_enforced.py)."""
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    con.executemany("INSERT INTO orders VALUES (?)", [(i,) for i in range(1, 4)])
    con.commit()
    con.close()
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        conn = Connection(organization_id=org_id, name=f"wh-{suffix}", type="sqlite",
                          config={"database": str(db_path)}, auth_policy="system_only", **rate_limit)
        conn.encrypt_credentials({})
        db.add(conn)
        await db.flush()
        ds = DataSource(name=f"agent-{suffix}", organization_id=org_id, is_active=True,
                        is_public=True, owner_user_id=owner_id)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))
        ct = ConnectionTable(name="orders", connection_id=conn.id, columns=[{"name": "id", "dtype": "int"}],
                             pks=[], fks=[], no_rows=3)
        db.add(ct)
        await db.flush()
        db.add(DataSourceTable(name="orders", datasource_id=ds.id, connection_table_id=ct.id, is_active=True,
                               columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=3))
        await db.commit()
        return str(ds.id), str(conn.id)


async def _usage_sources(org_id):
    async with async_session_maker() as db:
        rows = (await db.execute(select(UsageEvent).where(
            UsageEvent.organization_id == org_id, UsageEvent.metric == METRIC_DATA_QUERIES,
        ))).scalars().all()
        return [r.source for r in rows]


@pytest.fixture
def api():
    # A refused query surfaces as a server error on some of these routes
    # (viewer runs); the test reads the status instead of the raised error.
    from main import app

    return _ReleasingTestClient(app, raise_server_exceptions=False)


def _entity(api, token, org_id, ds_id, code=_ENTITY_CODE):
    resp = api.post(
        "/api/entities",
        json={"type": "model", "title": f"e-{uuid.uuid4().hex[:6]}", "code": code, "status": "published",
              "parameters": [_V_SPEC], "data_source_ids": [ds_id]},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _call(api, kind, token, org_id, ds_id, eid, n):
    """One call of `kind`; `n` varies the parameter value so a viewer run
    never answers from its cache. Returns (served rows?, response)."""
    hdr = _headers(token, org_id)
    if kind == "entity_run":
        resp = api.post(f"/api/entities/{eid}/run", json={}, headers=hdr)
        ok = resp.status_code == 200 and bool(resp.json()["data"].get("rows"))
    elif kind == "entity_viewer_run":
        resp = api.post(f"/api/entities/{eid}/run", json={"params": {"v": n}}, headers=hdr)
        ok = resp.status_code == 200 and bool(resp.json()["data"].get("rows"))
    elif kind == "entity_preview":
        resp = api.post(f"/api/entities/{eid}/preview", json={"code": _ENTITY_CODE}, headers=hdr)
        ok = resp.status_code == 200 and not resp.json().get("error") and bool(resp.json()["data"].get("rows"))
    else:
        resp = api.post("/api/entities/preview",
                        json={"code": _ENTITY_CODE, "data_source_ids": [ds_id]}, headers=hdr)
        ok = resp.status_code == 200 and not resp.json().get("error") and bool(resp.json()["data"].get("rows"))
    return ok, resp


_ENTITY_CALLS = ["entity_run", "entity_viewer_run", "entity_preview", "entity_code_preview"]


@pytest.mark.e2e
@pytest.mark.parametrize("kind", _ENTITY_CALLS)
@pytest.mark.parametrize("limit", [1, 2])
def test_entity_calls_count_against_the_connection_rate_limit(
    api, create_user, login_user, whoami, tmp_path, kind, limit,
):
    """limit N per day: N calls are served, the (N+1)th query is refused
    (one rate-limit audit entry), and every query that ran is metered under
    the call's own source."""
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, conn_id = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db",
                                             rate_limit_enabled=True, rate_limit_per_day=limit))
    eid = _entity(api, token, org_id, ds_id)

    served = [_call(api, kind, token, org_id, ds_id, eid, n) for n in range(limit)]
    assert [ok for ok, _ in served] == [True] * limit, [resp.text[:300] for _, resp in served]
    refused, resp = _call(api, kind, token, org_id, ds_id, eid, limit)
    assert not refused, resp.text

    logs = _run(_audit_count(org_id))
    assert [log.resource_id for log in logs] == [conn_id]
    assert _run(_usage_sources(org_id)) == [kind] * limit


@pytest.mark.e2e
def test_entity_preview_whose_code_fails_after_a_query_still_meters_it(
    api, create_user, login_user, whoami, tmp_path,
):
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    failing = _ENTITY_CODE.replace(
        '    return client.execute_query("SELECT COUNT(*) AS n FROM orders")',
        '    client.execute_query("SELECT COUNT(*) AS n FROM orders")\n'
        '    raise ValueError("the code fails after its query")',
    )
    eid = _entity(api, token, org_id, ds_id, code=failing)

    resp = api.post(f"/api/entities/{eid}/preview", json={"code": failing}, headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.text
    assert "fails after its query" in (resp.json().get("error") or "")
    assert _run(_usage_sources(org_id)) == ["entity_preview"]
