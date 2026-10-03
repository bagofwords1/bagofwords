"""Audit stream API contract + RBAC (docs/design/audit-log-streams.md, Loop A4)."""
from __future__ import annotations

import uuid

import pytest

from tests.e2e.audit.conftest import h

pytestmark = pytest.mark.e2e

BASE = "/api/enterprise/audit/streams"


def _payload(siem, **over):
    p = {
        "name": f"Splunk {uuid.uuid4().hex[:4]}",
        "destination": "splunk",
        "config": {"hec_url": f"{siem.url}/splunk", "index": "audit"},
        "secrets": {"token": "demo-hec-token-0123456789"},
    }
    p.update(over)
    return p


def _audit_actions(client, admin, prefix):
    r = client.get("/api/enterprise/audit", params={"page_size": 100}, headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200
    return [i for i in r.json()["items"] if i["action"].startswith(prefix)]


def test_admin_full_lifecycle_never_exposes_secrets(test_client, bootstrap_admin, siem):
    admin = bootstrap_admin()
    hd = h(admin["token"], admin["org_id"])
    secret = "demo-hec-token-0123456789"

    created = test_client.post(BASE, json=_payload(siem), headers=hd)
    assert created.status_code == 200, created.text
    s = created.json()
    assert s["state"] == "active" and s["destination"] == "splunk"
    assert secret not in created.text
    assert s["secrets"]["token"].startswith("••••") and s["secrets"]["token"].endswith("6789")

    listed = test_client.get(BASE, headers=hd)
    assert listed.status_code == 200 and [x["id"] for x in listed.json()] == [s["id"]]
    assert secret not in listed.text

    # Rename keeps the stored secret when the masked value is sent back.
    upd = test_client.patch(f"{BASE}/{s['id']}", json={"name": "Renamed", "config": s["config"], "secrets": s["secrets"]}, headers=hd)
    assert upd.status_code == 200 and upd.json()["name"] == "Renamed"
    # Testing with the masked value must use the stored token: the mock only
    # accepts the real one.
    siem.state.config["hec_token"] = secret
    t = test_client.post(f"{BASE}/test", json={"destination": "splunk", "config": s["config"], "secrets": s["secrets"], "stream_id": s["id"]}, headers=hd)
    assert t.status_code == 200 and t.json()["ok"] is True, t.text

    paused = test_client.patch(f"{BASE}/{s['id']}", json={"state": "inactive"}, headers=hd)
    assert paused.json()["state"] == "inactive"
    resumed = test_client.patch(f"{BASE}/{s['id']}", json={"state": "active"}, headers=hd)
    assert resumed.json()["state"] == "active"

    assert test_client.delete(f"{BASE}/{s['id']}", headers=hd).status_code == 204
    assert test_client.get(f"{BASE}/{s['id']}", headers=hd).status_code == 404

    actions = {a["action"] for a in _audit_actions(test_client, admin, "audit_stream.")}
    assert {"audit_stream.created", "audit_stream.updated", "audit_stream.paused",
            "audit_stream.activated", "audit_stream.deleted"} <= actions
    assert all(secret not in str(a["details"]) for a in _audit_actions(test_client, admin, "audit_stream."))


def test_secrets_are_encrypted_at_rest(test_client, bootstrap_admin, siem):
    import asyncio

    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.ee.audit.streams.models import AuditLogStream

    admin = bootstrap_admin()
    s = test_client.post(BASE, json=_payload(siem), headers=h(admin["token"], admin["org_id"])).json()

    async def raw():
        async with async_session_maker() as db:
            return (await db.execute(select(AuditLogStream.secrets).where(AuditLogStream.id == s["id"]))).scalar_one()

    stored = asyncio.run(raw())
    assert stored and "demo-hec-token" not in stored


def test_test_endpoint_reports_rejected_credentials(test_client, bootstrap_admin, siem):
    admin = bootstrap_admin()
    r = test_client.post(f"{BASE}/test", json={"destination": "splunk", "config": {"hec_url": f"{siem.url}/splunk"},
                                               "secrets": {"token": "nope"}}, headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200
    assert r.json()["ok"] is False and r.json()["kind"] == "invalid"
    ok = test_client.post(f"{BASE}/test", json={"destination": "https", "config": {"url": f"{siem.url}/https/x"},
                                                "secrets": {"hmac_secret": "demo-hmac-secret"}}, headers=h(admin["token"], admin["org_id"]))
    assert ok.json()["ok"] is True
    assert siem.state.stats("https")["test_events"] == 1


@pytest.mark.parametrize("bad,code", [
    ({"destination": "carrier-pigeon"}, "audit_stream.invalid_destination"),
    ({"config": {}}, "audit_stream.missing_field"),
    ({"secrets": {}}, "audit_stream.missing_field"),
    ({"config": {"hec_url": "ftp://nope"}}, "audit_stream.invalid_field"),
])
def test_validation_errors_are_typed(test_client, bootstrap_admin, siem, bad, code):
    admin = bootstrap_admin()
    r = test_client.post(BASE, json=_payload(siem, **bad), headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 400, r.text
    assert r.json()["error_code"] == code


def test_destinations_schema_marks_secret_fields(test_client, bootstrap_admin):
    admin = bootstrap_admin()
    r = test_client.get(f"{BASE}/destinations", headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 200
    specs = {d["type"]: {f["key"]: f for f in d["fields"]} for d in r.json()}
    assert set(specs) == {"datadog", "splunk", "sentinel", "s3", "gcs", "https", "syslog"}
    assert specs["datadog"]["api_key"]["secret"] and specs["splunk"]["token"]["secret"]
    assert not specs["splunk"]["hec_url"]["secret"]


def test_member_cannot_view_or_manage_streams(test_client, bootstrap_admin, invite_user_to_org, siem):
    admin = bootstrap_admin()
    s = test_client.post(BASE, json=_payload(siem), headers=h(admin["token"], admin["org_id"])).json()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    mh = h(member["token"], admin["org_id"])
    calls = [
        ("get", BASE, None), ("get", f"{BASE}/destinations", None), ("get", f"{BASE}/{s['id']}", None),
        ("post", BASE, _payload(siem)), ("patch", f"{BASE}/{s['id']}", {"state": "inactive"}),
        ("delete", f"{BASE}/{s['id']}", None),
        ("post", f"{BASE}/test", {"destination": "https", "config": {"url": f"{siem.url}/https/x"}, "secrets": {}}),
    ]
    for method, url, body in calls:
        kw = {"headers": mh} | ({"json": body} if body is not None else {})
        r = getattr(test_client, method)(url, **kw)
        assert r.status_code == 403, (method, url, r.status_code)


def test_view_audit_logs_role_reads_but_cannot_manage(
    test_client, bootstrap_admin, invite_user_to_org, create_role, assign_role, siem, enterprise_license
):
    admin = bootstrap_admin()
    s = test_client.post(BASE, json=_payload(siem), headers=h(admin["token"], admin["org_id"])).json()
    auditor = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    role = create_role(name=f"auditor-{uuid.uuid4().hex[:4]}", permissions=["view_audit_logs"],
                       user_token=admin["token"], org_id=admin["org_id"])
    assert role.status_code in (200, 201), role.text
    a = assign_role(role_id=role.json()["id"], principal_type="user", principal_id=auditor["user_id"],
                    user_token=admin["token"], org_id=admin["org_id"])
    assert a.status_code in (200, 201), a.text
    ah = h(auditor["token"], admin["org_id"])

    assert test_client.get(BASE, headers=ah).status_code == 200
    assert test_client.get(f"{BASE}/{s['id']}", headers=ah).status_code == 200
    assert "demo-hec-token" not in test_client.get(BASE, headers=ah).text
    assert test_client.post(BASE, json=_payload(siem), headers=ah).status_code == 403
    assert test_client.patch(f"{BASE}/{s['id']}", json={"state": "inactive"}, headers=ah).status_code == 403
    assert test_client.delete(f"{BASE}/{s['id']}", headers=ah).status_code == 403


def test_streams_require_the_license_feature(test_client, bootstrap_admin, monkeypatch):
    from app.ee import license as ee_license

    admin = bootstrap_admin()
    monkeypatch.setattr(ee_license, "_cached_license", ee_license.LicenseInfo(licensed=True, tier="enterprise", features=["audit_logs"]))
    r = test_client.get(BASE, headers=h(admin["token"], admin["org_id"]))
    assert r.status_code == 402
    monkeypatch.setattr(ee_license, "_cached_license", ee_license.LicenseInfo(licensed=False, tier="community"))
    assert test_client.get(BASE, headers=h(admin["token"], admin["org_id"])).status_code == 402
