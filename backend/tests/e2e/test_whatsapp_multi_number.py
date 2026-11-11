"""End-to-end tests for connecting several WhatsApp numbers to one org.

The contract: an organization may connect more than one WhatsApp number (one
entry point per agent/purpose). What must stay unique is the ``phone_number_id``
— the webhook's routing key — so the same number cannot be connected twice.
Creating the platform is gated by ``manage_settings``.

Meta's Graph API is the only boundary mocked (the credential-probe call); the
routes, service, ORM and DB all run for real.
"""
import uuid

import pytest

from app.services.external_platform_service import ExternalPlatformService


@pytest.fixture
def mock_whatsapp_probe(monkeypatch):
    """Stub the Meta credential check so no real Graph API call is made."""

    async def _ok(self, access_token, phone_number_id):
        return {
            "success": True,
            "info": {
                "display_phone_number": "+1555" + phone_number_id[-7:].rjust(7, "0"),
                "verified_name": "Acme " + phone_number_id[-3:],
            },
        }

    monkeypatch.setattr(ExternalPlatformService, "_test_whatsapp_token", _ok)


def _connect(test_client, token, org_id, phone_number_id, name=None):
    body = {
        "access_token": "tok-" + phone_number_id,
        "phone_number_id": phone_number_id,
        "waba_id": "waba-1",
        "app_secret": "secret",
        "verify_token": "verify-" + phone_number_id,
    }
    if name is not None:
        body["name"] = name
    return test_client.post(
        "/api/settings/integrations/whatsapp",
        json=body,
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )


@pytest.mark.e2e
def test_connect_multiple_whatsapp_numbers(
    mock_whatsapp_probe, create_user, login_user, whoami, test_client
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    pn_a = "PNID_" + uuid.uuid4().hex[:8]
    pn_b = "PNID_" + uuid.uuid4().hex[:8]

    r_a = _connect(test_client, token, org_id, pn_a, name="Sales")
    r_b = _connect(test_client, token, org_id, pn_b, name="Finance")
    assert r_a.status_code == 200, r_a.json()
    assert r_b.status_code == 200, r_b.json()
    assert r_a.json()["id"] != r_b.json()["id"]

    # Both numbers show up as independent instances, each keeping its own
    # phone_number_id and label.
    listing = test_client.get(
        "/api/settings/integrations",
        headers={"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)},
    )
    assert listing.status_code == 200, listing.json()
    whatsapp = [p for p in listing.json() if p["platform_type"] == "whatsapp"]
    assert len(whatsapp) == 2
    by_pnid = {p["platform_config"]["phone_number_id"]: p for p in whatsapp}
    assert set(by_pnid) == {pn_a, pn_b}
    assert by_pnid[pn_a]["platform_config"]["name"] == "Sales"
    assert by_pnid[pn_b]["platform_config"]["name"] == "Finance"


@pytest.mark.e2e
def test_duplicate_phone_number_id_rejected(
    mock_whatsapp_probe, create_user, login_user, whoami, test_client
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    pn = "PNID_" + uuid.uuid4().hex[:8]
    first = _connect(test_client, token, org_id, pn)
    assert first.status_code == 200, first.json()

    # Same number again -> rejected; a different number would be fine.
    dup = _connect(test_client, token, org_id, pn)
    assert dup.status_code == 400


@pytest.mark.e2e
def test_connect_requires_manage_settings(
    mock_whatsapp_probe, create_user, login_user, whoami, test_client
):
    # Owner is admin of their default org; a plain member of it cannot connect.
    owner_email = f"wa_owner_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=owner_email, password="test123")
    owner_token = login_user(email=owner_email, password="test123")
    org_id = whoami(owner_token)["organizations"][0]["id"]

    member_email = f"wa_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers={"Authorization": f"Bearer {owner_token}", "X-Organization-Id": org_id},
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(email=member_email, password="test123")

    r = _connect(test_client, member_token, org_id, "PNID_" + uuid.uuid4().hex[:8])
    assert r.status_code == 403
