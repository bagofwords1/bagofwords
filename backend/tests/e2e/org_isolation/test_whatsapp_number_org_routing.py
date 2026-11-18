"""A WhatsApp number belongs to one organization on the deployment.

Contract: ``phone_number_id`` is the webhook's routing key.

* A second organization cannot connect a number another organization already
  connected (the same organization re-connecting its own number is fine).
* If older data already has a number connected twice, inbound messages go to
  the active connection — predictably, not to whichever row is read first.

Meta's credential probe is the only boundary mocked on the connect path. The
hand-off to the agent pipeline is captured instead of run.
"""
import asyncio
import hashlib
import hmac
import json
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.external_platform import ExternalPlatform
from app.routes import whatsapp_webhook
from app.services.external_platform_service import ExternalPlatformService
from app.settings.config import settings as bow_settings

WEBHOOK = "/api/settings/integrations/whatsapp/webhook"


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def allow_multiple_orgs():
    flags = bow_settings.bow_config.features
    saved = flags.allow_multiple_organizations
    flags.allow_multiple_organizations = True
    try:
        yield
    finally:
        flags.allow_multiple_organizations = saved


@pytest.fixture
def meta(monkeypatch):
    async def _ok(self, access_token, phone_number_id):
        return {"success": True, "info": {"display_phone_number": "+15550000000",
                                          "verified_name": "Acme"}}

    monkeypatch.setattr(ExternalPlatformService, "_test_whatsapp_token", _ok)


@pytest.fixture
def handoffs(monkeypatch):
    seen = []

    async def _capture(db, platform_type, organization_id, event_data, platform_id=None):
        seen.append({"organization_id": str(organization_id), "platform_id": str(platform_id)})
        return {"success": True}

    monkeypatch.setattr(whatsapp_webhook.platform_manager, "handle_incoming_message", _capture)
    return seen


@pytest.fixture
def two_orgs(allow_multiple_orgs, create_user, login_user, whoami, create_organization):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_a = whoami(token)["organizations"][0]["id"]
    org_b = create_organization(name=f"Org B {uuid.uuid4().hex[:6]}", user_token=token)
    return {"token": token, "org_a": org_a, "org_b": org_b}


def _connect(test_client, token, org_id, phone_number_id, app_secret="secret"):
    return test_client.post(
        "/api/settings/integrations/whatsapp",
        json={"access_token": "tok-" + uuid.uuid4().hex, "phone_number_id": phone_number_id,
              "waba_id": "waba-1", "app_secret": app_secret, "verify_token": "v-" + uuid.uuid4().hex},
        headers=_h(token, org_id),
    )


def _post_text(test_client, phone_number_id, app_secret):
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": phone_number_id},
            "messages": [{"id": "wamid." + uuid.uuid4().hex, "from": "15551234567",
                          "type": "text", "text": {"body": "hello"}}],
        }}]}],
    }).encode()
    signature = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return test_client.post(WEBHOOK, content=body, headers={
        "Content-Type": "application/json", "x-hub-signature-256": signature})


@pytest.mark.e2e
def test_a_whatsapp_number_cannot_be_connected_to_a_second_organization(
    test_client, two_orgs, meta
):
    t = two_orgs
    number = "PNID_" + uuid.uuid4().hex[:8]
    assert _connect(test_client, t["token"], t["org_a"], number).status_code == 200

    dup = _connect(test_client, t["token"], t["org_b"], number)
    assert dup.status_code == 409, dup.text
    assert dup.json()["error_code"] == "integration.connected_elsewhere"
    listing = test_client.get("/api/settings/integrations", headers=_h(t["token"], t["org_b"]))
    assert [p for p in listing.json() if p["platform_type"] == "whatsapp"] == []

    # A different number is a legitimate connection for the second org.
    other = _connect(test_client, t["token"], t["org_b"], "PNID_" + uuid.uuid4().hex[:8])
    assert other.status_code == 200, other.text


@pytest.mark.e2e
def test_an_org_can_reconnect_its_own_whatsapp_number(
    test_client, create_user, login_user, whoami, meta
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    number = "PNID_" + uuid.uuid4().hex[:8]
    first = _connect(test_client, token, org_id, number)
    assert first.status_code == 200, first.text
    removed = test_client.delete(f"/api/settings/integrations/{first.json()['id']}",
                                 headers=_h(token, org_id))
    assert removed.status_code == 200, removed.text

    assert _connect(test_client, token, org_id, number).status_code == 200


@pytest.mark.e2e
def test_message_reaches_the_org_that_connected_the_number(test_client, two_orgs, meta, handoffs):
    t = two_orgs
    num_a, num_b = "PNID_" + uuid.uuid4().hex[:8], "PNID_" + uuid.uuid4().hex[:8]
    plat_a = _connect(test_client, t["token"], t["org_a"], num_a, app_secret="sa").json()["id"]
    plat_b = _connect(test_client, t["token"], t["org_b"], num_b, app_secret="sb").json()["id"]

    assert _post_text(test_client, num_b, "sb").status_code == 200
    assert _post_text(test_client, num_a, "sa").status_code == 200
    assert handoffs == [
        {"organization_id": t["org_b"], "platform_id": plat_b},
        {"organization_id": t["org_a"], "platform_id": plat_a},
    ]


async def _seed_duplicate_number(org_id, phone_number_id, app_secret):
    async with async_session_maker() as db:
        platform = ExternalPlatform(
            organization_id=org_id, platform_type="whatsapp", is_active=True,
            platform_config={"phone_number_id": phone_number_id, "waba_id": "waba-1"},
        )
        platform.encrypt_credentials({"app_secret": app_secret, "verify_token": "v"})
        db.add(platform)
        await db.commit()
        return str(platform.id)


@pytest.mark.e2e
def test_a_number_connected_twice_in_older_data_routes_to_the_active_connection(
    test_client, two_orgs, meta, handoffs
):
    t = two_orgs
    number = "PNID_" + uuid.uuid4().hex[:8]
    plat_a = _connect(test_client, t["token"], t["org_a"], number, app_secret="shared").json()["id"]
    # Seeded directly: the API no longer lets a second org connect this
    # number, but deployments may already hold such rows.
    plat_b = asyncio.run(_seed_duplicate_number(t["org_b"], number, "shared"))

    r = test_client.put(f"/api/settings/integrations/{plat_a}", json={"is_active": False},
                        headers=_h(t["token"], t["org_a"]))
    assert r.status_code == 200, r.text

    assert _post_text(test_client, number, "shared").status_code == 200
    assert handoffs == [{"organization_id": t["org_b"], "platform_id": plat_b}]
