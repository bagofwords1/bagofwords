"""Rotating a report webhook's credentials must revoke what it replaces.

In ``url_token`` mode the delivery URL *is* the credential, so a rotation has
to mint a new path token: the old URL stops working and the rotate response
carries the new one. Signing modes (token/hmac) keep a stable URL — the sender
is configured against it — and only get a fresh secret. Mirrors the trigger
rotation contract in test_triggers.py.
"""
import pytest

from app.services.webhook_service import WebhookService


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def owner(create_user, login_user, whoami, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Hooked", user_token=token, org_id=org_id)
    return {"token": token, "org_id": org_id, "report_id": report["id"]}


@pytest.fixture(autouse=True)
def _no_agent_run(monkeypatch):
    """Accepted deliveries are processed in the background by the agent; the
    contract here is the receiver's auth decision, not the run."""
    async def _noop(self, *a, **k):
        return None
    monkeypatch.setattr(WebhookService, "process_delivery", _noop)


def _create(test_client, o, auth_mode):
    r = test_client.post(f"/api/reports/{o['report_id']}/webhooks",
                         json={"name": f"hook-{auth_mode}", "auth_mode": auth_mode},
                         headers=_h(o["token"], o["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


def _rotate(test_client, o, wh):
    r = test_client.post(f"/api/reports/{o['report_id']}/webhooks/{wh['id']}/rotate",
                         headers=_h(o["token"], o["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
def test_rotating_url_token_webhook_revokes_old_delivery_url(test_client, owner):
    wh = _create(test_client, owner, "url_token")
    old_token = wh["token"]
    assert test_client.post(f"/webhooks/{old_token}", json={"type": "x"}).status_code == 200

    rotated = _rotate(test_client, owner, wh)
    assert rotated["token"] != old_token
    assert rotated["secret"]
    # The response hands back the NEW delivery URL so the UI can show it.
    assert rotated["delivery_url"].endswith(rotated["token"])
    assert old_token not in rotated["delivery_url"]

    assert test_client.post(f"/webhooks/{old_token}", json={"type": "x"}).status_code == 404
    assert test_client.post(f"/webhooks/{rotated['token']}", json={"type": "x"}).status_code == 200

    # The listing agrees with the rotate response.
    listed = test_client.get(f"/api/reports/{owner['report_id']}/webhooks",
                             headers=_h(owner["token"], owner["org_id"])).json()
    assert {w["token"] for w in listed if w["id"] == wh["id"]} == {rotated["token"]}


@pytest.mark.e2e
@pytest.mark.parametrize("auth_mode", ["token", "hmac"])
def test_rotating_signing_mode_webhook_keeps_url_and_replaces_secret(test_client, owner, auth_mode):
    wh = _create(test_client, owner, auth_mode)
    rotated = _rotate(test_client, owner, wh)
    assert rotated["token"] == wh["token"]
    assert rotated["delivery_url"] == wh["delivery_url"]
    assert rotated["secret"] and rotated["secret"] != wh["secret"]
