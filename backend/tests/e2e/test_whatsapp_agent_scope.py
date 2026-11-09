"""End-to-end tests for per-instance agent scoping.

Contract: an admin can scope a connected WhatsApp number to a chosen set of
agents. The scope round-trips, ignores ids that are not agents of the org, and
can be cleared. Setting it is gated by ``manage_settings``.

Meta's Graph API (the credential probe) is the only boundary mocked.
"""
import sqlite3
import uuid

import pytest

from app.services.external_platform_service import ExternalPlatformService


@pytest.fixture
def mock_whatsapp_probe(monkeypatch):
    async def _ok(self, access_token, phone_number_id):
        return {"success": True, "info": {"display_phone_number": "+15550000000",
                                          "verified_name": "Acme"}}

    monkeypatch.setattr(ExternalPlatformService, "_test_whatsapp_token", _ok)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _connect_number(test_client, token, org_id):
    body = {
        "access_token": "tok",
        "phone_number_id": "PNID_" + uuid.uuid4().hex[:8],
        "waba_id": "waba-1",
        "app_secret": "secret",
        "verify_token": "verify",
    }
    r = test_client.post("/api/settings/integrations/whatsapp", json=body,
                         headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    return r.json()["id"]


def _sqlite_agent(create_data_source, token, org_id, tmp_path, label):
    path = tmp_path / f"{label}_{uuid.uuid4().hex[:6]}.db"
    sqlite3.connect(str(path)).close()
    ds = create_data_source(name=f"{label}-{uuid.uuid4().hex[:4]}", type="sqlite",
                            config={"database": str(path)}, user_token=token, org_id=org_id)
    return ds["id"]


@pytest.mark.e2e
def test_agent_scope_roundtrip_and_clear(
    mock_whatsapp_probe, create_user, login_user, whoami,
    create_data_source, test_client, tmp_path
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    platform_id = _connect_number(test_client, token, org_id)
    ds1 = _sqlite_agent(create_data_source, token, org_id, tmp_path, "sales")
    ds2 = _sqlite_agent(create_data_source, token, org_id, tmp_path, "finance")

    # A fresh instance is unscoped.
    r = test_client.get(f"/api/settings/integrations/{platform_id}/agents",
                        headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    assert r.json()["agent_ids"] == []

    # Scope it to one agent; it round-trips.
    r = test_client.put(f"/api/settings/integrations/{platform_id}/agents",
                        json={"agent_ids": [ds1]}, headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    assert r.json()["agent_ids"] == [ds1]
    r = test_client.get(f"/api/settings/integrations/{platform_id}/agents",
                        headers=_headers(token, org_id))
    assert set(r.json()["agent_ids"]) == {ds1}

    # Replace with both; clearing to empty removes the scope.
    r = test_client.put(f"/api/settings/integrations/{platform_id}/agents",
                        json={"agent_ids": [ds1, ds2]}, headers=_headers(token, org_id))
    assert set(r.json()["agent_ids"]) == {ds1, ds2}
    r = test_client.put(f"/api/settings/integrations/{platform_id}/agents",
                        json={"agent_ids": []}, headers=_headers(token, org_id))
    assert r.json()["agent_ids"] == []


@pytest.mark.e2e
def test_agent_scope_drops_unknown_ids(
    mock_whatsapp_probe, create_user, login_user, whoami,
    create_data_source, test_client, tmp_path
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    platform_id = _connect_number(test_client, token, org_id)
    ds1 = _sqlite_agent(create_data_source, token, org_id, tmp_path, "sales")

    # A non-existent / cross-org id is silently dropped; the real one stays.
    r = test_client.put(
        f"/api/settings/integrations/{platform_id}/agents",
        json={"agent_ids": [ds1, "not-a-real-agent-" + uuid.uuid4().hex]},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.json()
    assert r.json()["agent_ids"] == [ds1]


@pytest.mark.e2e
def test_agent_scope_requires_manage_settings(
    mock_whatsapp_probe, create_user, login_user, whoami, test_client
):
    owner_email = f"scope_owner_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=owner_email, password="test123")
    owner_token = login_user(email=owner_email, password="test123")
    org_id = whoami(owner_token)["organizations"][0]["id"]
    platform_id = _connect_number(test_client, owner_token, org_id)

    member_email = f"scope_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(owner_token, org_id),
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(email=member_email, password="test123")

    r = test_client.put(f"/api/settings/integrations/{platform_id}/agents",
                        json={"agent_ids": []}, headers=_headers(member_token, org_id))
    assert r.status_code == 403
