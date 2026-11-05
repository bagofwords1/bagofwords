"""Audit event contract end to end (docs/design/audit-event-contract.md).

Events are produced by real routes through ``test_client``; assertions read
them back through the audit API, the export endpoint and the exporter with
the mock SIEM intake. The database is touched directly only where the API
cannot produce the state (an old row for retention), with a comment.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.ee.audit import context as audit_context
from app.ee.audit.models import AuditLog
from app.ee.audit.streams import exporter
from tests.e2e.audit.conftest import h

pytestmark = pytest.mark.e2e
BASE = "/api/enterprise/audit"


def _run(coro):
    return asyncio.run(coro)


def _rows(client, who, **params):
    items, page = [], 1
    while True:
        r = client.get(BASE, params={"page": page, "page_size": 100, **params}, headers=h(who["token"], who["org_id"]))
        assert r.status_code == 200, r.text
        body = r.json()
        items += body["items"]
        if page >= max(body["total_pages"], 1):
            return items
        page += 1


def _one(client, who, action, **match):
    rows = [r for r in _rows(client, who, action=action) if all(r.get(k) == v for k, v in match.items())]
    assert rows, f"no {action} row matching {match}"
    return rows[-1]


async def _db_rows(action: str):
    from app.dependencies import async_session_maker
    async with async_session_maker() as db:
        return (await db.execute(select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.created_at))).scalars().all()


# --- session lifecycle ----------------------------------------------------------

def test_password_login_success_and_failure_are_recorded_with_context(test_client, bootstrap_admin):
    admin = bootstrap_admin()
    ua = f"contract-test/{uuid.uuid4().hex[:6]}"
    ok = test_client.post("/api/auth/jwt/login", data={"username": admin["email"], "password": admin["password"]},
                          headers={"User-Agent": ua})
    assert ok.status_code == 200
    bad = test_client.post("/api/auth/jwt/login", data={"username": admin["email"], "password": "wrong-" + uuid.uuid4().hex},
                           headers={"User-Agent": ua})
    assert bad.status_code == 400

    login = _one(test_client, admin, "auth.login", user_agent=ua)
    assert login["outcome"] == "success" and login["user_id"] == admin["user_id"]
    assert login["ip_address"] and login["request_id"] and login["schema_version"] == 2
    assert login["actor_snapshot"]["email"] == admin["email"]
    assert login["details"]["auth_method"] in ("password", "ldap")
    assert admin["password"] not in json.dumps(login["details"])  # the credential never reaches the row

    failed = _one(test_client, admin, "auth.login_failed", user_agent=ua)
    assert failed["outcome"] == "failure" and failed["failure_reason"] == "invalid_credentials"
    assert failed["user_id"] == admin["user_id"]  # known account: evidence belongs to its organizations
    assert failed["details"]["account_known"] is True


def test_unknown_account_failure_has_no_organization_and_is_invisible_to_orgs(test_client, bootstrap_admin):
    admin = bootstrap_admin()
    ghost = f"nobody_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post("/api/auth/jwt/login", data={"username": ghost, "password": "whatever-1"})
    assert r.status_code == 400
    assert not [x for x in _rows(test_client, admin, action="auth.login_failed") if x["details"].get("email") == ghost]
    rows = [x for x in _run(_db_rows("auth.login_failed")) if (x.details or {}).get("email") == ghost]
    assert len(rows) == 1
    assert rows[0].organization_id is None and rows[0].user_id is None and rows[0].actor_type == "unknown"
    assert rows[0].outcome == "failure" and rows[0].details["account_known"] is False


def test_logout_and_password_change_are_recorded(test_client, bootstrap_admin, login_user):
    admin = bootstrap_admin()
    out = test_client.post("/api/auth/jwt/logout", headers={"Authorization": f"Bearer {admin['token']}"})
    assert out.status_code == 204
    token = login_user(admin["email"], admin["password"])
    new_pw = "Changed-" + uuid.uuid4().hex[:8] + "!"
    r = test_client.patch("/api/users/me", json={"password": new_pw}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    admin["token"] = login_user(admin["email"], new_pw)

    logout = _one(test_client, admin, "auth.logout", user_id=admin["user_id"])
    assert logout["outcome"] == "success" and logout["details"]["sessions_revoked"] is True
    changed = _one(test_client, admin, "auth.password_changed", user_id=admin["user_id"])
    assert changed["outcome"] == "success"
    assert new_pw not in json.dumps(changed["details"])


def test_session_id_is_opaque_and_stable_within_a_login(test_client, bootstrap_admin, login_user):
    admin = bootstrap_admin()
    token = login_user(admin["email"], admin["password"])
    admin["token"] = token
    for _ in range(2):
        assert test_client.post("/api/api_keys", json={"name": "sid"}, headers=h(token, admin["org_id"])).status_code == 200
    rows = [r for r in _rows(test_client, admin, action="api_key.created") if r["details"].get("name") == "sid"]
    sids = {r["session_id"] for r in rows}
    assert len(rows) == 2 and len(sids) == 1
    sid = sids.pop()
    assert sid and sid not in token and len(sid) >= 16


# --- correlation and source context -----------------------------------------------

def test_request_id_is_echoed_and_recorded_and_spoofed_forwarded_for_is_ignored(test_client, bootstrap_admin):
    admin = bootstrap_admin()
    rid = "client-" + uuid.uuid4().hex
    r = test_client.post("/api/api_keys", json={"name": "rid"},
                         headers={**h(admin["token"], admin["org_id"]), "X-Request-ID": rid, "X-Forwarded-For": "198.51.100.77"})
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID") == rid
    row = _one(test_client, admin, "api_key.created", request_id=rid)
    # the test client is not a configured proxy: its header must not become the actor's address
    assert row["ip_address"] != "198.51.100.77"
    bad = test_client.post("/api/api_keys", json={"name": "rid2"}, headers={**h(admin["token"], admin["org_id"]), "X-Request-ID": "x y z"})
    assert bad.status_code == 200 and bad.headers.get("X-Request-ID") not in (None, "x y z")


# --- authorization denials -------------------------------------------------------

def test_denial_carries_source_context_reason_and_resource(test_client, bootstrap_admin, invite_user_to_org):
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"], role="member")
    ua = f"denied/{uuid.uuid4().hex[:6]}"
    r = test_client.get("/api/enterprise/audit/streams", headers={**h(member["token"], admin["org_id"]), "User-Agent": ua})
    assert r.status_code == 403
    row = _one(test_client, admin, "access.denied", user_id=member["user_id"], user_agent=ua)
    assert row["outcome"] == "denied" and row["failure_reason"] == "missing_permission"
    assert row["ip_address"] and row["request_id"]
    assert row["details"]["permission"] and row["details"]["endpoint"] and row["details"]["reason"] == "missing_permission"
    assert row["actor_snapshot"]["email"] == member["email"]


# --- data access evidence ----------------------------------------------------------

def test_report_reads_public_reads_and_query_execution_are_recorded(test_client, bootstrap_admin):
    admin = bootstrap_admin()
    H = h(admin["token"], admin["org_id"])
    rep = test_client.post("/api/reports", json={"title": f"Evidence {uuid.uuid4().hex[:6]}"}, headers=H)
    assert rep.status_code == 200, rep.text
    rid = rep.json()["id"]
    assert test_client.get(f"/api/reports/{rid}", headers=H).status_code == 200
    viewed = _one(test_client, admin, "report.viewed", resource_id=rid)
    assert viewed["outcome"] == "success" and viewed["details"]["observation"] == "served"

    assert test_client.post(f"/api/reports/{rid}/publish", headers=H).status_code == 200
    published = [r for r in _rows(test_client, admin, action="report.published") if r["resource_id"] == rid]
    assert len(published) == 1 and published[0]["ip_address"]  # once, with request context
    anon = test_client.get(f"/api/r/{rid}")
    assert anon.status_code == 200
    public = _one(test_client, admin, "report.public_viewed", resource_id=rid)
    assert public["user_id"] is None and public["details"]["access_scope"] == "anonymous"

    q = test_client.post("/api/queries", json={"title": "q", "report_id": rid}, headers=H)
    assert q.status_code == 200, q.text
    run = test_client.post(f"/api/queries/{q.json()['id']}/run", json={}, headers=H)
    assert run.status_code == 400
    executed = _one(test_client, admin, "query.executed", resource_id=q.json()["id"])
    assert executed["outcome"] == "failure" and executed["failure_reason"] == "invalid_request"
    assert executed["details"]["mode"] == "builder"


def test_tool_query_evidence_keeps_every_query_with_an_explicit_marker(test_client, bootstrap_admin, monkeypatch):
    from app.models.organization import Organization
    from app.models.user import User
    from app.ee.audit.tool_audit import log_tool_audit, stop_tool_audit_worker
    from app.settings.config import settings

    admin = bootstrap_admin()
    monkeypatch.setattr(settings.bow_config.audit, "max_query_payload_bytes", 4000)
    queries = [f"SELECT /* q{i} */ " + ", ".join(f"c{j}" for j in range(120)) + " FROM t" for i in range(12)]

    async def produce():
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            user = await db.get(User, admin["user_id"])
            org = await db.get(Organization, admin["org_id"])
        ctx = {"user": user, "organization": org, "agent_execution_id": str(uuid.uuid4()), "mode": "chat"}
        with audit_context.scope(audit_context.for_job("agent")):
            await log_tool_audit(ctx, "tool.data_queried", "report", "rep-x", {"tool": "create_data", "executed_queries": queries})
            # Drain and stop: the worker task belongs to this event loop.
            await stop_tool_audit_worker(timeout=10)
    _run(produce())

    row = _one(test_client, admin, "tool.data_queried", resource_id="rep-x")
    d = row["details"]
    assert len(d["executed_queries"]) == 12
    assert d["executed_queries_truncation"]["truncated"] is True and d["executed_queries_truncation"]["query_count"] == 12
    assert row["actor_type"] == "agent" and row["outcome"] == "success" and row["request_id"]
    assert row["actor_snapshot"]["email"] == admin["email"]


# --- historical identity -----------------------------------------------------------

def test_rename_after_the_event_does_not_rewrite_the_evidence(test_client, bootstrap_admin, invite_user_to_org, login_user):
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"], role="member")
    old_email = member["email"]
    assert test_client.post("/api/api_keys", json={"name": "before-rename"}, headers=h(member["token"], admin["org_id"])).status_code == 200
    new_email = f"renamed_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.patch("/api/users/me", json={"email": new_email}, headers={"Authorization": f"Bearer {member['token']}"})
    assert r.status_code == 200, r.text

    row = _one(test_client, admin, "api_key.created", user_id=member["user_id"])
    assert row["actor_snapshot"]["email"] == old_email
    exp = test_client.get(f"{BASE}/export", params={"format": "json", "action": "api_key.created"}, headers=h(admin["token"], admin["org_id"]))
    assert exp.status_code == 200
    env = [json.loads(l) for l in exp.text.splitlines() if l.strip() and json.loads(l)["actor"]["id"] == member["user_id"]][-1]
    assert env["actor"]["email"] == old_email and env["legacy"] is False and env["outcome"] == "success"
    assert env["version"] == 1  # v1 consumers keep parsing


# --- platform routing, heartbeat, retention, health -----------------------------------

def _siem_stream(client, admin, siem):
    r = client.post("/api/enterprise/audit/streams", json={
        "name": f"s-{uuid.uuid4().hex[:6]}", "destination": "https", "start_from": "beginning",
        "config": {"url": f"{siem.url}/https/hook"}, "secrets": {"hmac_secret": "demo-hmac-secret"},
    }, headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def test_unknown_account_failures_reach_only_the_platform_stream(test_client, bootstrap_admin, siem, monkeypatch):
    from app.ee.audit.maintenance import sync_platform_stream
    from app.settings.config import settings

    admin = bootstrap_admin()
    _siem_stream(test_client, admin, siem)
    ps = settings.bow_config.audit.platform_stream
    monkeypatch.setattr(ps, "enabled", True)
    monkeypatch.setattr(ps, "destination", "splunk")
    monkeypatch.setattr(ps, "config", {"hec_url": f"{siem.url}/splunk"})
    monkeypatch.setattr(ps, "secrets", {"token": "demo-hec-token"})
    assert _run(sync_platform_stream())

    ghost = f"ghost_{uuid.uuid4().hex[:8]}@test.com"
    assert test_client.post("/api/auth/jwt/login", data={"username": ghost, "password": "nope-1"}).status_code == 400
    _run(exporter.run_exporter_tick(now=datetime.utcnow() + timedelta(seconds=90)))

    platform = [e for e in siem.state.envelopes("splunk") if e["action"] == "auth.login_failed" and e["metadata"].get("email") == ghost]
    assert len(platform) == 1 and platform[0]["organization"]["id"] is None and platform[0]["actor"]["type"] == "unknown"
    assert not [e for e in siem.state.envelopes("https") if e["metadata"].get("email") == ghost]
    # disabling the config pauses the platform stream; nothing more is stamped for it
    monkeypatch.setattr(ps, "enabled", False)
    assert _run(sync_platform_stream()) is None


def test_heartbeat_is_written_for_streamed_organizations_only(test_client, bootstrap_admin, siem, monkeypatch):
    from app.ee.audit.maintenance import heartbeat_tick
    from app.settings.config import settings

    with_stream, without = bootstrap_admin("hb"), bootstrap_admin("nohb")
    _siem_stream(test_client, with_stream, siem)
    monkeypatch.setattr(settings.bow_config.audit, "heartbeat_interval_seconds", 60)
    assert _run(heartbeat_tick()) >= 1
    beat = _one(test_client, with_stream, "audit.heartbeat")
    assert beat["outcome"] == "success" and beat["actor_type"] == "system" and "queue_depth" in beat["details"]
    assert not _rows(test_client, without, action="audit.heartbeat")
    monkeypatch.setattr(settings.bow_config.audit, "heartbeat_interval_seconds", 0)
    assert _run(heartbeat_tick()) == 0


def test_retention_purges_delivered_rows_only_and_respects_legal_hold(test_client, bootstrap_admin, siem, monkeypatch):
    from app.ee.audit.maintenance import retention_purge
    from app.settings.config import settings

    admin = bootstrap_admin()
    stream = _siem_stream(test_client, admin, siem)
    for i in range(3):
        assert test_client.post("/api/api_keys", json={"name": f"old-{i}"}, headers=h(admin["token"], admin["org_id"])).status_code == 200
    _run(exporter.run_exporter_tick(now=datetime.utcnow() + timedelta(seconds=90)))
    # Pause the stream, then produce one more row: it stays undelivered.
    r = test_client.patch(f"/api/enterprise/audit/streams/{stream['id']}", json={"state": "inactive"}, headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200
    assert test_client.post("/api/api_keys", json={"name": "undelivered"}, headers=h(admin["token"], admin["org_id"])).status_code == 200

    async def age_everything():
        # The API cannot produce rows from the past; age this org's rows directly.
        from app.dependencies import async_session_maker
        from sqlalchemy import update
        async with async_session_maker() as db:
            await db.execute(update(AuditLog).where(AuditLog.organization_id == admin["org_id"])
                             .values(created_at=datetime.utcnow() - timedelta(days=400)))
            await db.commit()
    _run(age_everything())

    monkeypatch.setattr(settings.bow_config.audit, "retention_days", 30)
    monkeypatch.setattr(settings.bow_config.audit, "legal_hold", True)
    assert _run(retention_purge()) == {}
    monkeypatch.setattr(settings.bow_config.audit, "legal_hold", False)
    removed = _run(retention_purge())
    assert removed.get(admin["org_id"], 0) >= 3
    remaining = _rows(test_client, admin, action="api_key.created")
    assert [r["details"]["name"] for r in remaining] == ["undelivered"]
    assert _one(test_client, admin, "audit.retention_purged")["details"]["removed"] == removed[admin["org_id"]]


def test_health_endpoint_reports_recording_and_streams(test_client, bootstrap_admin, invite_user_to_org, siem):
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"], role="member")
    _siem_stream(test_client, admin, siem)
    r = test_client.get(f"{BASE}/health", headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"queue_depth", "spill_backlog", "recording_failures", "unstamped_rows"} <= set(body["recording"])
    assert body["streams"] and {"pending", "lag_seconds", "consecutive_failures", "degraded_since"} <= set(body["streams"][0])
    assert "last_recorded_at" in body["heartbeat"] and "legal_hold" in body["retention"]
    assert test_client.get(f"{BASE}/health", headers=h(member["token"], admin["org_id"])).status_code == 403


def test_sustained_delivery_failure_is_reported_once_and_recovery_recorded(test_client, bootstrap_admin, siem, monkeypatch):
    from app.settings.config import settings

    admin = bootstrap_admin()
    stream = _siem_stream(test_client, admin, siem)
    monkeypatch.setattr(settings.bow_config.audit, "degraded_after_failures", 2)
    siem.state.faults["https"] = {"mode": "503", "count": 1000, "sleep": None}
    monkeypatch.setattr(exporter, "backoff_seconds", lambda failures: 0.0)
    now = datetime.utcnow() + timedelta(seconds=90)
    for i in range(4):
        _run(exporter.run_exporter_tick(now=now + timedelta(seconds=i * 200)))
    siem.state.faults.clear()
    degraded = [r for r in _rows(test_client, admin, action="audit_stream.degraded") if r["resource_id"] == stream["id"]]
    assert len(degraded) == 1 and degraded[0]["outcome"] == "failure"
    st = test_client.get(f"/api/enterprise/audit/streams/{stream['id']}", headers=h(admin["token"], admin["org_id"])).json()
    assert st["state"] == "active"
    _run(exporter.run_exporter_tick(now=now + timedelta(seconds=2000)))
    recovered = [r for r in _rows(test_client, admin, action="audit_stream.recovered") if r["resource_id"] == stream["id"]]
    assert len(recovered) == 1
