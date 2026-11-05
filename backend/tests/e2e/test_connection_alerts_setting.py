"""E2E: the org-level switch for connection down/restored alert emails.

The in-app inbox notification is always delivered; this setting governs only
the email nudge. It defaults ON and must round-trip through the API so an admin
who turns it off actually silences the mail.
"""
import uuid

import pytest


def _unique_email(domain="acme-corp.com"):
    return f"connalerts_{uuid.uuid4().hex[:8]}@{domain}"


@pytest.mark.e2e
def test_connection_alert_email_toggle_defaults_on_and_round_trips(
    create_user, login_user, whoami
):
    email = _unique_email()
    create_user(email=email)
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]

    from main import app
    from fastapi.testclient import TestClient

    client = TestClient(app)
    H = {"Authorization": f"Bearer {token}", "X-Organization-Id": org_id}

    # Default: on, with no stored override.
    g0 = client.get("/api/organization/connection-alerts", headers=H)
    assert g0.status_code == 200, g0.text
    assert g0.json()["email_enabled"] is True

    # Turn it off, and confirm the response reflects the new state.
    p1 = client.put("/api/organization/connection-alerts",
                    json={"email_enabled": False}, headers=H)
    assert p1.status_code == 200, p1.text
    assert p1.json()["email_enabled"] is False

    # It persists across reads.
    g1 = client.get("/api/organization/connection-alerts", headers=H)
    assert g1.json()["email_enabled"] is False

    # And it can be turned back on.
    p2 = client.put("/api/organization/connection-alerts",
                    json={"email_enabled": True}, headers=H)
    assert p2.status_code == 200, p2.text
    assert p2.json()["email_enabled"] is True
    assert client.get("/api/organization/connection-alerts", headers=H).json()["email_enabled"] is True
