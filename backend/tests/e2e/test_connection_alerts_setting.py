"""E2E: the two switches for connection down/restored alert emails.

The in-app inbox notification is always delivered; these govern only the email
nudge. The org switch (Settings → SMTP) defaults ON, is admin-only, and must
round-trip; the per-connection switch must persist and read back on the
connection, alongside the org switch it sits under.
"""
import uuid
from pathlib import Path

import pytest

ALERTS = "/api/organization/connection-alerts"
SQLITE_DB = str(Path(__file__).resolve().parents[1] / "config" / "chinook.sqlite")


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}


@pytest.fixture
def org(test_client, create_user, login_user, whoami):
    admin = create_user()
    admin_token = login_user(admin["email"], admin["password"])
    org_id = whoami(admin_token)["organizations"][0]["id"]
    return {"id": org_id, "admin": admin_token}


@pytest.mark.e2e
def test_org_alert_email_toggle_defaults_on_and_round_trips(test_client, org):
    H = _h(org["admin"], org["id"])

    g0 = test_client.get(ALERTS, headers=H)
    assert g0.status_code == 200, g0.text
    assert g0.json()["email_enabled"] is True

    p1 = test_client.put(ALERTS, json={"email_enabled": False}, headers=H)
    assert p1.status_code == 200, p1.text
    assert p1.json()["email_enabled"] is False
    assert test_client.get(ALERTS, headers=H).json()["email_enabled"] is False

    p2 = test_client.put(ALERTS, json={"email_enabled": True}, headers=H)
    assert p2.status_code == 200, p2.text
    assert test_client.get(ALERTS, headers=H).json()["email_enabled"] is True


@pytest.mark.e2e
def test_org_alert_email_toggle_rejects_a_non_boolean(test_client, org):
    H = _h(org["admin"], org["id"])
    r = test_client.put(ALERTS, json={"email_enabled": "nope"}, headers=H)
    assert r.status_code == 422, r.text
    r = test_client.put(ALERTS, json={}, headers=H)
    assert r.status_code == 422, r.text
    assert test_client.get(ALERTS, headers=H).json()["email_enabled"] is True


@pytest.mark.e2e
def test_org_alert_email_toggle_is_admin_only(test_client, org, create_user, login_user):
    email = f"mem_{uuid.uuid4().hex[:8]}@test.com"
    r = test_client.post(
        f"/api/organizations/{org['id']}/members",
        json={"organization_id": org["id"], "email": email, "role": "member"},
        headers=_h(org["admin"], org["id"]),
    )
    assert r.status_code == 200, r.json()
    create_user(email=email, password="test123")
    member = _h(login_user(email, "test123"), org["id"])

    assert test_client.get(ALERTS, headers=member).status_code == 403
    assert test_client.put(ALERTS, json={"email_enabled": False}, headers=member).status_code == 403


@pytest.mark.e2e
def test_per_connection_alert_switch_persists_on_the_connection(
    test_client, org, create_connection, get_connection, update_connection,
):
    conn = create_connection(
        name="Alerts", type="sqlite", config={"database": SQLITE_DB}, credentials={},
        user_token=org["admin"], org_id=org["id"],
    )
    detail = get_connection(connection_id=conn["id"], user_token=org["admin"], org_id=org["id"])
    assert detail["alert_emails_enabled"] is True
    assert detail["org_alert_emails_enabled"] is True

    update_connection(connection_id=conn["id"], payload={"alert_emails_enabled": False},
                      user_token=org["admin"], org_id=org["id"])
    test_client.put(ALERTS, json={"email_enabled": False}, headers=_h(org["admin"], org["id"]))

    detail = get_connection(connection_id=conn["id"], user_token=org["admin"], org_id=org["id"])
    assert detail["alert_emails_enabled"] is False
    assert detail["org_alert_emails_enabled"] is False
