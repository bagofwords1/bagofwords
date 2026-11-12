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
import logging
import sqlite3
import uuid
from contextlib import contextmanager

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


async def _seed_sqlite_agent(org_id, owner_id, db_path, query_timeout_seconds=None, **rate_limit):
    """A connection over a local SQLite file, its agent and an activated
    `orders` table. Direct model writes: no route creates a connection over a
    local file together with an indexed catalog (same as
    test_agent_table_selection_enforced.py)."""
    config = {"database": str(db_path)}
    if query_timeout_seconds is not None:
        config["query_timeout_seconds"] = query_timeout_seconds
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY)")
    con.executemany("INSERT INTO orders VALUES (?)", [(i,) for i in range(1, 4)])
    # In the file, never activated for the agent.
    con.execute("CREATE TABLE staff (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        conn = Connection(organization_id=org_id, name=f"wh-{suffix}", type="sqlite",
                          config=config, auth_policy="system_only", **rate_limit)
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
def api(test_client):
    # Server errors raise in the test: a refusal must never become one.
    return test_client


def _entity(api, token, org_id, ds_id, code=_ENTITY_CODE):
    resp = api.post(
        "/api/entities",
        json={"type": "model", "title": f"e-{uuid.uuid4().hex[:6]}", "code": code, "status": "published",
              "parameters": [_V_SPEC], "data_source_ids": [ds_id]},
        headers=_headers(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _call(api, kind, token, org_id, ds_id, eid, n, code=_ENTITY_CODE):
    """One call of `kind`; `n` varies the parameter value so a viewer run
    never answers from its cache. Runs use the entity's stored code, previews
    send `code`. Returns (served rows?, response)."""
    hdr = _headers(token, org_id)
    if kind == "entity_run":
        resp = api.post(f"/api/entities/{eid}/run", json={}, headers=hdr)
        ok = resp.status_code == 200 and bool(resp.json()["data"].get("rows"))
    elif kind == "entity_viewer_run":
        resp = api.post(f"/api/entities/{eid}/run", json={"params": {"v": n}}, headers=hdr)
        ok = resp.status_code == 200 and bool(resp.json()["data"].get("rows"))
    elif kind == "entity_preview":
        resp = api.post(f"/api/entities/{eid}/preview", json={"code": code}, headers=hdr)
        ok = resp.status_code == 200 and not resp.json().get("error") and bool(resp.json()["data"].get("rows"))
    else:
        resp = api.post("/api/entities/preview",
                        json={"code": code, "data_source_ids": [ds_id]}, headers=hdr)
        ok = resp.status_code == 200 and not resp.json().get("error") and bool(resp.json()["data"].get("rows"))
    return ok, resp


_ENTITY_CALLS = ["entity_run", "entity_viewer_run", "entity_preview", "entity_code_preview"]
_RUNS = ("entity_run", "entity_viewer_run")


def _refusal_text(kind, resp, status):
    """The reason a refused call gives. Runs answer with the refusal status;
    previews keep reporting a failed execution in the body (200, `error`)."""
    if kind in _RUNS:
        assert resp.status_code == status, resp.text
        detail = resp.json()["detail"]
        return detail["message"] if isinstance(detail, dict) else detail
    assert resp.status_code == 200, resp.text
    assert resp.json().get("data") is None, resp.text
    return resp.json().get("error")


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
    assert "rate limit" in (_refusal_text(kind, resp, 429) or ""), resp.text

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


# ── A refused query is answered as a refusal, never a server error ───────

def _code_running(statement):
    """The entity code with its query replaced by the Python expression `statement`."""
    return _ENTITY_CODE.replace('"SELECT COUNT(*) AS n FROM orders"', statement)


def _order_count(db_path):
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    finally:
        con.close()


@pytest.mark.e2e
@pytest.mark.parametrize("kind", _ENTITY_CALLS)
@pytest.mark.parametrize("statement", [
    '"DELETE FROM orders"',         # a write the code scan sees
    '"DEL" + "ETE FROM orders"',    # a write assembled at run time
    '"SELECT id FROM staff"',       # a table not activated for the agent
])
def test_entity_calls_answer_a_guard_refusal_with_403_and_its_reason(
    api, create_user, login_user, whoami, tmp_path, kind, statement,
):
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    db_path = tmp_path / "wh.db"
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, db_path))
    code = _code_running(statement)
    eid = _entity(api, token, org_id, ds_id, code=code)

    served, resp = _call(api, kind, token, org_id, ds_id, eid, 1, code=code)
    assert not served, resp.text
    reason = _refusal_text(kind, resp, 403)
    assert reason and "custom code" not in reason and "Internal server error" not in reason, resp.text
    assert _order_count(db_path) == 3


@pytest.mark.e2e
@pytest.mark.parametrize("kind", _ENTITY_CALLS)
def test_entity_calls_answer_an_exhausted_query_quota_with_429_and_its_reason(
    api, create_user, login_user, whoami, tmp_path, kind,
):
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    policy = api.post(
        f"/api/organizations/{org_id}/usage-policies",
        json={"name": f"cap-{uuid.uuid4().hex[:6]}", "monthly_query_limit": 1,
              "assignments": [{"principal_type": "user", "principal_id": user_id}]},
        headers=_headers(token, org_id),
    )
    assert policy.status_code == 200, policy.text
    eid = _entity(api, token, org_id, ds_id)

    served, resp = _call(api, kind, token, org_id, ds_id, eid, 0)
    assert served, resp.text
    served, resp = _call(api, kind, token, org_id, ds_id, eid, 1)
    assert not served, resp.text
    assert _refusal_text(kind, resp, 429), resp.text
    assert _run(_usage_sources(org_id)) == [kind]


@pytest.mark.e2e
@pytest.mark.parametrize("kind", _ENTITY_CALLS)
def test_entity_calls_tag_their_queries_with_their_own_source(
    api, create_user, login_user, whoami, tmp_path, monkeypatch, kind,
):
    """The warehouse sees the same caller name as the usage events."""
    from app.data_sources.clients.sqlite_client import SqliteClient
    from app.data_sources.query_tag import current_query_tag

    sources = []
    sent = SqliteClient.execute_query

    def recording(self, sql):
        # The driver boundary: what tag the statement would carry.
        sources.append(current_query_tag().get("source"))
        return sent(self, sql)

    monkeypatch.setattr(SqliteClient, "execute_query", recording)
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(api, token, org_id, ds_id)

    served, resp = _call(api, kind, token, org_id, ds_id, eid, 0)
    assert served, resp.text
    assert sources and set(sources) == {kind}, sources


# ── A failed run keeps the status its error carries ──────────────────────

_SWALLOWING_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    client = [c for key, c in ds_clients.items() if ":" in key][0]
    try:
        return client.execute_query("SELECT no_such_column FROM orders")
    except Exception:
        return pd.DataFrame()
"""


@pytest.mark.e2e
@pytest.mark.parametrize("kind", _RUNS)
def test_entity_runs_answer_a_query_timeout_with_its_own_408(
    api, create_user, login_user, whoami, tmp_path, monkeypatch, kind,
):
    import time

    from app.data_sources.clients.sqlite_client import SqliteClient

    sent = SqliteClient.execute_query

    def slow(self, sql):
        # The warehouse takes longer than the connection's budget.
        time.sleep(3)
        return sent(self, sql)

    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db", query_timeout_seconds=1))
    eid = _entity(api, token, org_id, ds_id)
    monkeypatch.setattr(SqliteClient, "execute_query", slow)

    served, resp = _call(api, kind, token, org_id, ds_id, eid, 1)
    assert not served, resp.text
    assert resp.status_code == 408, resp.text
    assert resp.json()["error_code"] == "query.timeout", resp.text


@pytest.mark.e2e
def test_a_builder_run_answers_a_swallowed_query_failure_with_its_own_422(
    api, create_user, login_user, whoami, tmp_path,
):
    """The builder sees the query errors of their own code; a viewer run
    answers entity.run_failed instead (see the viewer-run test below)."""
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(api, token, org_id, ds_id, code=_SWALLOWING_CODE)

    served, resp = _call(api, "entity_run", token, org_id, ds_id, eid, 1)
    assert not served, resp.text
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "query.failed_silently", resp.text


@pytest.mark.e2e
@pytest.mark.parametrize("kind", _RUNS)
def test_entity_runs_answer_a_host_without_the_sandbox_with_503_and_the_operator_remedy(
    api, create_user, login_user, whoami, tmp_path, monkeypatch, kind,
):
    from app.ai.code_execution import code_execution
    from app.ai.code_execution.sandbox.runner import CONFINEMENT_REMEDY, SandboxUnavailableError

    def no_confinement(job, *args, **kwargs):
        # The child process refuses before running anything.
        raise SandboxUnavailableError("Landlock TCP confinement is required but unavailable")

    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(api, token, org_id, ds_id)
    monkeypatch.setattr(code_execution, "_run_sandbox_job", no_confinement)

    served, resp = _call(api, kind, token, org_id, ds_id, eid, 1)
    assert not served, resp.text
    assert resp.status_code == 503, resp.text
    assert CONFINEMENT_REMEDY in resp.json()["detail"], resp.text


@pytest.mark.e2e
def test_an_entity_run_whose_code_fails_is_not_answered_as_a_missing_entity(
    api, create_user, login_user, whoami, tmp_path,
):
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(api, token, org_id, ds_id, code=_code_running('"SELECT id FROM orders"[999]'))

    resp = api.post(f"/api/entities/{eid}/run", json={}, headers=_headers(token, org_id))
    assert resp.status_code == 422, resp.text
    assert "IndexError" in resp.json()["detail"], resp.text
    missing = api.post(f"/api/entities/{uuid.uuid4()}/run", json={}, headers=_headers(token, org_id))
    assert missing.status_code == 404, missing.text


@pytest.mark.e2e
def test_an_entity_run_refusal_survives_a_failed_save_of_the_refresh_time(
    api, create_user, login_user, whoami, tmp_path, monkeypatch, caplog,
):
    """After a refused run the service records the refresh time; when that
    save fails too, the caller still gets the refusal, not the save error."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    db_path = tmp_path / "wh.db"
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, db_path))
    code = _code_running('"DELETE FROM orders"')
    eid = _entity(api, token, org_id, ds_id, code=code)

    async def database_down(self):
        raise OperationalError("COMMIT", {}, Exception("database is locked"))

    # alembic's fileConfig disables the loggers that exist when it runs.
    monkeypatch.setattr(logging.getLogger("app.services.entity_service"), "disabled", False)
    monkeypatch.setattr(AsyncSession, "commit", database_down)
    with caplog.at_level(logging.WARNING, logger="app.services.entity_service"):
        served, resp = _call(api, "entity_run", token, org_id, ds_id, eid, 1)
    assert not served, resp.text
    reason = _refusal_text("entity_run", resp, 403)
    assert reason and "database is locked" not in reason, resp.text
    assert _order_count(db_path) == 3
    # The lost save is logged, without the save error's text.
    lost = [r.getMessage() for r in caplog.records
            if r.name == "app.services.entity_service" and "not saved" in r.getMessage()]
    assert len(lost) == 1, caplog.text
    assert eid in lost[0] and "OperationalError" in lost[0], lost
    assert "database is locked" not in lost[0], lost


def _watch_app_logs(monkeypatch):
    """Let caplog see every app logger: alembic's fileConfig disables the
    loggers that exist when it runs."""
    for name, lg in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(lg, logging.Logger) and (name == "main" or name.split(".")[0] == "app"):
            monkeypatch.setattr(lg, "disabled", False)


def _handled_by_the_global_handler(caplog):
    return [r for r in caplog.records if r.name == "app.errors" and r.levelno >= logging.ERROR]


def _plain_client():
    """What a real HTTP client receives: server errors become responses."""
    from fastapi.testclient import TestClient

    from main import app

    return TestClient(app, raise_server_exceptions=False)


# The statement a SQLAlchemy-based warehouse client sends, with one
# placeholder more than the values bound to it.
_REJECTED_TAIL = " WHERE id = ? OR id = ?"


def _warehouse_rejecting(bound):
    """SqliteClient.execute_query run the way the SQLAlchemy-based warehouse
    clients (postgres, mysql, mssql, ...) run a statement. The driver refuses
    it, and SQLAlchemy raises its ProgrammingError, whose text quotes the
    statement and the bound value."""
    import pandas as pd
    from sqlalchemy import create_engine

    def execute_query(self, sql):
        engine = create_engine(f"sqlite:///{self.database}")
        try:
            with engine.connect() as conn:
                rows = conn.exec_driver_sql(sql + _REJECTED_TAIL, (bound,)).fetchall()
        finally:
            engine.dispose()
        return pd.DataFrame(rows)

    return execute_query


@pytest.mark.e2e
def test_a_builder_run_whose_warehouse_rejects_the_query_answers_422_with_the_reason(
    create_user, login_user, whoami, tmp_path, monkeypatch, caplog,
):
    """A warehouse client raises SQLAlchemy's own errors: that is the
    builder's query failing, answered 422 with the driver's reason, not an
    app-database failure. Neither the answer nor the log quotes the
    statement's bound values."""
    from sqlalchemy.exc import ProgrammingError

    from app.data_sources.clients.sqlite_client import SqliteClient

    client = _plain_client()
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(client, token, org_id, ds_id)
    bound = f"bound-{uuid.uuid4().hex}"
    monkeypatch.setattr(SqliteClient, "execute_query", _warehouse_rejecting(bound))
    _watch_app_logs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        resp = client.post(f"/api/entities/{eid}/run", json={}, headers=_headers(token, org_id))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert ProgrammingError.__name__ in detail and "bindings" in detail, resp.text
    assert bound not in resp.text and "[parameters:" not in resp.text, resp.text
    assert not _handled_by_the_global_handler(caplog), caplog.text
    assert bound not in caplog.text and _REJECTED_TAIL not in caplog.text, caplog.text


@pytest.mark.e2e
@pytest.mark.parametrize("kind", _RUNS)
def test_an_entity_run_whose_save_fails_never_answers_or_logs_the_database_error(
    create_user, login_user, whoami, tmp_path, monkeypatch, caplog, kind,
):
    """The run succeeds but saving its result (the shared snapshot, or the
    viewer's cached slice) fails: the caller gets a 500 without the app
    database's error, and the log names the failure without its statement
    and bound values."""
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import AsyncSession

    client = _plain_client()
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(client, token, org_id, ds_id)
    statement = f"UPDATE entities SET data=? WHERE entities.id = ? -- {uuid.uuid4().hex}"
    bound = f"bound-{uuid.uuid4().hex}"

    async def save_fails(self):
        raise OperationalError(statement, (bound, eid), Exception("disk I/O error"))

    _watch_app_logs(monkeypatch)
    monkeypatch.setattr(AsyncSession, "commit", save_fails)
    with caplog.at_level(logging.WARNING):
        _, resp = _call(client, kind, token, org_id, ds_id, eid, 1)
    assert resp.status_code == 500, resp.text
    assert resp.json()["error_code"] == "generic", resp.text
    for text in (resp.text, caplog.text):
        assert statement not in text and "UPDATE entities" not in text, text
        assert bound not in text and "disk I/O error" not in text, text
    assert not _handled_by_the_global_handler(caplog), caplog.text
    lost = [r.getMessage() for r in caplog.records
            if r.name == "app.services.entity_service" and "not saved" in r.getMessage()]
    assert len(lost) == 1, caplog.text
    assert eid in lost[0] and "OperationalError" in lost[0], lost
    assert "failed run" not in lost[0], lost


_SPINNING_CODE = """
def generate_df(ds_clients, excel_files, params):
    while True:
        pass
"""


@pytest.mark.e2e
@pytest.mark.parametrize("failure", ["warehouse_error", "swallowed_query", "sandbox_timeout", "sandbox_crash"])
def test_a_viewer_run_that_fails_outside_the_code_answers_422_without_the_error_text(
    create_user, login_user, whoami, tmp_path, monkeypatch, caplog, failure,
):
    """A run with parameter values answers every failure of the saved code's
    execution with 422 entity.run_failed: a data-source error forwarded from
    the client, the sandbox's own time limit and a killed sandbox process
    included. Neither the answer nor the log carries the error's text."""
    from app.data_sources.clients.sqlite_client import SqliteClient

    client = _plain_client()
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    bound = f"bound-{uuid.uuid4().hex}"
    if failure == "warehouse_error":
        eid = _entity(client, token, org_id, ds_id)
        monkeypatch.setattr(SqliteClient, "execute_query", _warehouse_rejecting(bound))
        hidden, error_type = [bound, _REJECTED_TAIL, "bindings"], "ProgrammingError"
    elif failure == "swallowed_query":
        # The code hides the data source's error and returns no rows.
        eid = _entity(client, token, org_id, ds_id, code=_SWALLOWING_CODE)
        hidden, error_type = ["no_such_column", "no such column"], "SwallowedQueryError"
    elif failure == "sandbox_timeout":
        # The wall-clock budget runs out before the CPU budget.
        eid = _entity(client, token, org_id, ds_id, code=_SPINNING_CODE)
        monkeypatch.setenv("BOW_SANDBOX_TIMEOUT_SECONDS", "2")
        monkeypatch.setenv("BOW_SANDBOX_CPU_SECONDS", "60")
        hidden, error_type = ["time limit"], "SandboxTimeoutError"
    else:
        # The CPU budget runs out first: the kernel kills the process.
        eid = _entity(client, token, org_id, ds_id, code=_SPINNING_CODE)
        monkeypatch.setenv("BOW_SANDBOX_TIMEOUT_SECONDS", "60")
        monkeypatch.setenv("BOW_SANDBOX_CPU_SECONDS", "1")
        hidden, error_type = ["ended unexpectedly", "memory limit"], "SandboxCrashError"
    _watch_app_logs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        resp = client.post(f"/api/entities/{eid}/run", json={"params": {"v": 1}}, headers=_headers(token, org_id))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "entity.run_failed", resp.text
    assert error_type not in resp.text, resp.text
    for text in hidden:
        assert text not in resp.text and text not in caplog.text, (text, resp.text, caplog.text)
    assert not _handled_by_the_global_handler(caplog), caplog.text
    logged = [r.getMessage() for r in caplog.records if eid in r.getMessage()]
    assert logged and all(error_type in m for m in logged), caplog.text


@pytest.mark.e2e
def test_a_viewer_run_whose_saved_code_fails_answers_422_without_the_code_error(
    test_client, create_user, login_user, whoami, tmp_path, monkeypatch, caplog,
):
    """A run with parameter values executes the saved code; when that code
    fails the caller gets a 422 that does not quote the code's error (a viewer
    may not be allowed to see the code), and the server logs it without the
    error's text."""
    client = _plain_client()
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    marker = f"marker-{uuid.uuid4().hex[:8]}"
    eid = _entity(test_client, token, org_id, ds_id,
                  code=_code_running(f'"SELECT id FROM orders" if {{}}["{marker}"] else ""'))

    _watch_app_logs(monkeypatch)
    with caplog.at_level(logging.WARNING):
        resp = client.post(f"/api/entities/{eid}/run", json={"params": {"v": 1}}, headers=_headers(token, org_id))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "entity.run_failed", resp.text
    assert marker not in resp.text and "KeyError" not in resp.text, resp.text
    assert marker not in caplog.text, caplog.text
    # The log names the code's error type, never its message.
    logged = [r.getMessage() for r in caplog.records if eid in r.getMessage()]
    assert logged and any("KeyError" in m for m in logged), caplog.text

    # A builder run of the same code still reports the code's error.
    builder = client.post(f"/api/entities/{eid}/run", json={}, headers=_headers(token, org_id))
    assert builder.status_code == 422, builder.text
    assert "KeyError" in builder.json()["detail"], builder.text


# ── A slice the database refuses to save leaves no trace of the database ─

@contextmanager
def _slice_saved_first_by_another_request(monkeypatch, org_id):
    """Another request saves the same reader's slice, for the same values,
    after this run looked for it and before it inserts its own: the database
    refuses the second insert, by its unique constraint
    (uq_entity_user_results_entity_user_params), or, on SQLite, because the
    run's read snapshot is no longer the latest ("database is locked").
    Yields what the refused insert binds that must never be shown or logged:
    the fingerprint of the values."""
    import os

    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session

    from app.data_sources.clients.sqlite_client import SqliteClient
    from app.models.entity_user_result import EntityUserResult

    state = {"queried": False, "fingerprints": []}
    sent = SqliteClient.execute_query

    def querying(self, sql):
        result = sent(self, sql)
        state["queried"] = True
        return result

    other = create_engine(os.environ["TEST_DATABASE_URL"])

    def after_lookup(conn, cursor, statement, parameters, context, executemany):
        if not state["queried"] or state["fingerprints"]:
            return
        if not statement.lstrip().upper().startswith("SELECT") or "FROM entity_user_results" not in statement:
            return
        entity_id, user_id, fingerprint = list(parameters)[:3]
        state["fingerprints"].append(fingerprint)
        with Session(other) as s:
            s.add(EntityUserResult(entity_id=entity_id, user_id=user_id, organization_id=org_id,
                                   params_fingerprint=fingerprint, status="success", data={}))
            s.commit()

    app_engine = async_session_maker.kw["bind"].sync_engine
    monkeypatch.setattr(SqliteClient, "execute_query", querying)
    event.listen(app_engine, "after_cursor_execute", after_lookup)
    try:
        yield state["fingerprints"]
    finally:
        event.remove(app_engine, "after_cursor_execute", after_lookup)
        other.dispose()


def _assert_no_database_text(fingerprints, value, *texts):
    assert fingerprints, "the conflicting slice was never saved"
    for text in texts:
        assert "INSERT INTO entity_user_results" not in text, text
        assert "[parameters:" not in text and "[SQL:" not in text, text
        assert "UNIQUE constraint" not in text and "duplicate key" not in text, text
        assert "database is locked" not in text, text
        assert fingerprints[0] not in text and str(value) not in text, text


_REFUSED_VALUE = 7310459


@pytest.mark.e2e
def test_a_viewer_run_whose_slice_the_database_refuses_answers_500_without_its_text(
    create_user, login_user, whoami, tmp_path, monkeypatch, caplog,
):
    client = _plain_client()
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(client, token, org_id, ds_id)
    _watch_app_logs(monkeypatch)

    with caplog.at_level(logging.WARNING), _slice_saved_first_by_another_request(monkeypatch, org_id) as fps:
        resp = client.post(f"/api/entities/{eid}/run", json={"params": {"v": _REFUSED_VALUE}},
                           headers=_headers(token, org_id))
    assert resp.status_code == 500, resp.text
    assert resp.json()["error_code"] == "generic", resp.text
    _assert_no_database_text(fps, _REFUSED_VALUE, resp.text, caplog.text)
    assert not _handled_by_the_global_handler(caplog), caplog.text
    lost = [r.getMessage() for r in caplog.records
            if r.name == "app.services.entity_service" and "not saved" in r.getMessage()]
    assert len(lost) == 1 and eid in lost[0], caplog.text
    assert "IntegrityError" in lost[0] or "OperationalError" in lost[0], lost


async def _describe_in_one_session(org_id, user_id, eid, params):
    """describe_entity on the agent's session, then the agent's next read on
    that same session."""
    from app.ai.tools.implementations.describe_entity import DescribeEntityTool
    from app.models.entity import Entity
    from app.models.organization import Organization
    from app.models.user import User

    async with async_session_maker() as db:
        org = await db.get(Organization, org_id)
        user = await db.get(User, user_id)
        settings = await org.get_settings(db)
        events = [evt async for evt in DescribeEntityTool().run_stream(
            {"name_or_id": eid, "params": params},
            {"db": db, "organization": org, "user": user, "settings": settings},
        )]
        title = (await db.execute(select(Entity.title).where(Entity.id == eid))).scalar_one()
        return events[-1].payload, title


@pytest.mark.e2e
def test_describe_entity_whose_slice_the_database_refuses_reports_a_fixed_message_and_keeps_the_session(
    api, create_user, login_user, whoami, tmp_path, monkeypatch, caplog,
):
    import json

    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(api, token, org_id, ds_id)
    _watch_app_logs(monkeypatch)

    with caplog.at_level(logging.WARNING), _slice_saved_first_by_another_request(monkeypatch, org_id) as fps:
        payload, title = _run(_describe_in_one_session(org_id, user_id, eid, {"v": _REFUSED_VALUE}))
    out, obs = payload["output"], payload["observation"]
    assert not out["success"]
    assert out["errors"] == ["Code execution failed: The run's result could not be saved."], out["errors"]
    assert obs["errors"] == out["errors"] and obs["title"] == title, obs
    _assert_no_database_text(fps, _REFUSED_VALUE, json.dumps(payload, default=str), caplog.text)
    assert not _handled_by_the_global_handler(caplog), caplog.text


@pytest.mark.e2e
def test_mcp_describe_entity_whose_slice_the_database_refuses_reports_a_fixed_message(
    create_user, login_user, whoami, enable_mcp, create_api_key, tmp_path, monkeypatch, caplog,
):
    import json

    client = _plain_client()
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    enable_mcp(user_token=token, org_id=org_id)
    api_key = create_api_key(user_token=token, org_id=org_id)["key"]
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(client, token, org_id, ds_id)
    _watch_app_logs(monkeypatch)

    with caplog.at_level(logging.WARNING), _slice_saved_first_by_another_request(monkeypatch, org_id) as fps:
        resp = client.post("/api/mcp", headers={"X-API-Key": api_key}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "describe_entity", "arguments": {"name_or_id": eid, "params": {"v": _REFUSED_VALUE}}},
        })
    assert resp.status_code == 200, resp.text
    out = json.loads(resp.json()["result"]["content"][0]["text"])
    assert out["error_message"] == "Running the saved query failed: The run's result could not be saved.", out
    assert out["entity_id"] == eid and not out["rows"], out
    _assert_no_database_text(fps, _REFUSED_VALUE, resp.text, caplog.text)
    assert not _handled_by_the_global_handler(caplog), caplog.text


# ── A viewer run answers the reader's own mistakes and a busy source honestly ─

@pytest.mark.e2e
def test_a_builder_run_whose_saved_result_cannot_be_read_back_says_it_was_saved(
    create_user, login_user, whoami, tmp_path, monkeypatch, caplog,
):
    """The run's result is committed, then the row is gone before it is read
    back (another request deleted the entity): the answer and the log say the
    result was saved, not that saving it failed."""
    import os

    from sqlalchemy import create_engine, event, text
    from sqlalchemy.orm import Session

    from app.models.entity import Entity

    client = _plain_client()
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(client, token, org_id, ds_id)
    other = create_engine(os.environ["TEST_DATABASE_URL"])
    deleted = []

    def delete_after_commit(session):
        if deleted or not any(isinstance(o, Entity) and o.id == eid for o in session.identity_map.values()):
            return
        with other.begin() as conn:
            conn.execute(text("DELETE FROM entity_data_source_association WHERE entity_id = :e"), {"e": eid})
            conn.execute(text("DELETE FROM entities WHERE id = :e"), {"e": eid})
        deleted.append(eid)

    _watch_app_logs(monkeypatch)
    event.listen(Session, "after_commit", delete_after_commit)
    try:
        with caplog.at_level(logging.WARNING):
            resp = client.post(f"/api/entities/{eid}/run", json={}, headers=_headers(token, org_id))
    finally:
        event.remove(Session, "after_commit", delete_after_commit)
        other.dispose()
    assert deleted, "the entity was never deleted after the commit"
    assert resp.status_code == 500, resp.text
    assert resp.json()["detail"] == "The run's result was saved but could not be read back.", resp.text
    assert not _handled_by_the_global_handler(caplog), caplog.text
    logged = [r.getMessage() for r in caplog.records if r.name == "app.services.entity_service"]
    assert any(eid in m and "saved but not read back" in m for m in logged), caplog.text
    assert not any("not saved" in m for m in logged), caplog.text


_CALENDAR_CODE = """
def generate_df(ds_clients, excel_files, params, calendar_date_bounds):
    client = [c for key, c in ds_clients.items() if ":" in key][0]
    start, stop = calendar_date_bounds(params["d"])
    return client.execute_query("SELECT COUNT(*) AS n FROM orders")
"""
_D_SPEC = {"name": "d", "type": "date_range", "label": "D", "default": None, "required": False, "source": "input"}


@pytest.mark.e2e
def test_a_viewer_run_with_values_the_saved_code_cannot_use_answers_400_with_the_reason(
    api, create_user, login_user, whoami, tmp_path,
):
    """calendar_date_bounds refuses timestamps while the saved code runs: the
    reader's own values are wrong, so the answer says why, as for any other
    bad value."""
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    created = api.post(
        "/api/entities",
        json={"type": "model", "title": f"e-{uuid.uuid4().hex[:6]}", "code": _CALENDAR_CODE, "status": "published",
              "parameters": [_D_SPEC], "data_source_ids": [ds_id]},
        headers=_headers(token, org_id),
    )
    assert created.status_code == 200, created.text

    resp = api.post(f"/api/entities/{created.json()['id']}/run",
                    json={"params": {"d": {"from": "2024-01-01T10:00:00", "to": "2024-01-02"}}},
                    headers=_headers(token, org_id))
    assert resp.status_code == 400, resp.text
    assert "calendar_date_bounds accepts calendar dates only" in resp.json()["detail"], resp.text


@pytest.mark.e2e
@pytest.mark.parametrize("kind", _RUNS)
def test_entity_runs_answer_a_connection_without_a_free_slot_with_429_and_try_again(
    api, create_user, login_user, whoami, tmp_path, kind,
):
    """Every concurrency slot of the connection is held by other queries: the
    run is refused for now, not failed."""
    from contextlib import ExitStack

    from app.data_sources import query_concurrency

    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, conn_id = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db", query_timeout_seconds=1))
    eid = _entity(api, token, org_id, ds_id)

    with ExitStack() as running:
        for _ in range(query_concurrency.DEFAULT_MAX_CONCURRENT_QUERIES):
            running.enter_context(query_concurrency.slot(
                conn_id, query_concurrency.DEFAULT_MAX_CONCURRENT_QUERIES, wait_seconds=1))
        served, resp = _call(api, kind, token, org_id, ds_id, eid, 1)
    assert not served, resp.text
    reason = _refusal_text(kind, resp, 429)
    assert "try again" in (reason or "").lower(), resp.text
    assert "wh-" not in reason, resp.text


_DUPLICATE_COLUMNS_CODE = """
def generate_df(ds_clients, excel_files, params):
    import pandas as pd
    client = [c for key, c in ds_clients.items() if ":" in key][0]
    n = int(client.execute_query("SELECT COUNT(*) AS n FROM orders")["n"][0])
    return pd.DataFrame([[n, n]], columns=["n", "n"])
"""


@pytest.mark.e2e
def test_a_viewer_run_whose_result_cannot_be_shown_answers_422_like_a_failed_run(
    create_user, login_user, whoami, tmp_path, monkeypatch, caplog,
):
    """Turning the saved code's result into rows is part of the run: a result
    that cannot be shown is a failed run (422 entity.run_failed), not a server
    error."""
    client = _plain_client()
    token, org_id, user_id = _bootstrap_admin(create_user, login_user, whoami)
    ds_id, _ = _run(_seed_sqlite_agent(org_id, user_id, tmp_path / "wh.db"))
    eid = _entity(client, token, org_id, ds_id, code=_DUPLICATE_COLUMNS_CODE)
    _watch_app_logs(monkeypatch)

    with caplog.at_level(logging.WARNING):
        resp = client.post(f"/api/entities/{eid}/run", json={"params": {"v": 1}}, headers=_headers(token, org_id))
    assert resp.status_code == 422, resp.text
    assert resp.json()["error_code"] == "entity.run_failed", resp.text
    assert "unique" not in resp.text, resp.text
    assert not _handled_by_the_global_handler(caplog), caplog.text
