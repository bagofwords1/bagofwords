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


# ---------------------------------------------------------------------------
# Inbound: scope and report reuse follow the number the message arrived on
# ---------------------------------------------------------------------------


def _inbound_event(phone_number_id, wa_id, text):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "waba-1",
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "contacts": [{"wa_id": wa_id, "profile": {"name": "Dana"}}],
                    "messages": [{
                        "from": wa_id,
                        "id": "wamid." + uuid.uuid4().hex,
                        "timestamp": "1700000000",
                        "type": "text",
                        "text": {"body": text},
                    }],
                },
            }],
        }],
    }


def _post_inbound(test_client, event, app_secret="secret"):
    import hashlib
    import hmac
    import json

    body = json.dumps(event).encode()
    sig = "sha256=" + hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return test_client.post(
        "/api/settings/integrations/whatsapp/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )


@pytest.fixture
def inbound_boundaries(monkeypatch):
    """Mock the two boundaries of an inbound message: Meta's send API and the
    agent run. Everything between (routing, identity, scope, reports) is real.
    Returns the list of report ids the agent would have been invoked on."""
    from app.routes import whatsapp_webhook
    from app.services.platform_adapters.whatsapp_adapter import WhatsAppAdapter

    async def _sent(self, payload):
        return True

    monkeypatch.setattr(WhatsAppAdapter, "_post_message", _sent)

    report_ids = []

    async def _agent(**kwargs):
        report_ids.append(kwargs.get("report_id"))
        return None

    monkeypatch.setattr(
        whatsapp_webhook.platform_manager.completion_service,
        "create_completion", _agent,
    )
    return report_ids


def _seed_verified_mapping(org_id, platform_id, wa_id, app_user_id):
    # Direct DB write: verification completes out-of-band (the user clicks a
    # link sent to their BOW email), which the API can't drive from a test.
    import asyncio
    from app.dependencies import async_session_maker
    from app.models.external_user_mapping import ExternalUserMapping

    async def _run():
        async with async_session_maker() as db:
            db.add(ExternalUserMapping(
                organization_id=org_id, platform_id=platform_id,
                platform_type="whatsapp", external_user_id=wa_id,
                app_user_id=app_user_id, is_verified=True,
            ))
            await db.commit()

    asyncio.run(_run())


def _report_agent_ids(test_client, token, org_id, report_id):
    r = test_client.get(f"/api/reports/{report_id}", headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    return {str(ds["id"]) for ds in r.json().get("data_sources") or []}


@pytest.mark.e2e
@pytest.mark.parametrize("first_number", ["sales", "finance"])
def test_inbound_scope_follows_the_number_messaged(
    first_number, mock_whatsapp_probe, inbound_boundaries, create_user,
    login_user, whoami, create_data_source, test_client, tmp_path
):
    """One person messaging two scoped numbers gets each number's own agents
    and a separate conversation per number — regardless of which number they
    first verified on (a user mapping is shared across numbers)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    org_id = me["organizations"][0]["id"]

    numbers = {}
    for label in ("sales", "finance"):
        pnid = "PNID_" + uuid.uuid4().hex[:8]
        r = test_client.post(
            "/api/settings/integrations/whatsapp",
            json={"access_token": "tok", "phone_number_id": pnid, "waba_id": "waba-1",
                  "app_secret": "secret", "verify_token": "verify-" + pnid, "name": label},
            headers=_headers(token, org_id),
        )
        assert r.status_code == 200, r.json()
        agent = _sqlite_agent(create_data_source, token, org_id, tmp_path, label)
        r = test_client.put(f"/api/settings/integrations/{r.json()['id']}/agents",
                            json={"agent_ids": [agent]}, headers=_headers(token, org_id))
        assert r.status_code == 200, r.json()
        numbers[label] = {"pnid": pnid, "id": None, "agent": agent}

    listing = test_client.get("/api/settings/integrations", headers=_headers(token, org_id)).json()
    for p in listing:
        for n in numbers.values():
            if (p.get("platform_config") or {}).get("phone_number_id") == n["pnid"]:
                n["id"] = p["id"]

    wa_id = "1555" + uuid.uuid4().hex[:7]
    _seed_verified_mapping(org_id, numbers[first_number]["id"], wa_id, me["id"])

    seen = {}
    for label in ("sales", "finance", "sales"):
        r = _post_inbound(test_client, _inbound_event(numbers[label]["pnid"], wa_id, "hi"))
        assert r.status_code == 200, r.json()
        assert r.json()["result"].get("success") is True, r.json()
        report_id = inbound_boundaries[-1]
        assert _report_agent_ids(test_client, token, org_id, report_id) == {numbers[label]["agent"]}
        seen.setdefault(label, report_id)
        # Within the reuse window each number keeps continuing its own report.
        assert seen[label] == report_id

    assert seen["sales"] != seen["finance"]
