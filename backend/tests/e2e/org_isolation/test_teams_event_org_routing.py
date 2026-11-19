"""Teams inbound activities are handled by the organization whose bot they address.

Contract: one Entra tenant may host a bot per organization, so the tenant id
alone never decides which organization handles an activity.

* With several Teams platforms on the deployment, an activity is handled by
  the platform whose bot app id the (verified) token is issued for. There is
  no "any Teams platform" fallback.
* With a single Teams platform, activities without a tenant id (Web Chat,
  Emulator) keep reaching it, as before.
* A second organization cannot connect a bot (tenant + app id) another
  organization already connected.

Mocked boundaries: Microsoft's credential probe and its signing-key endpoint
(replaced by a local key so real, verifiable tokens can be minted). The
hand-off to the agent pipeline is captured instead of run: which
organization/platform receives the activity IS the contract.
"""
import json
import time
import uuid

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.routes import teams_webhook
from app.services.external_platform_service import ExternalPlatformService
from app.services.platform_adapters.teams_adapter import TeamsAdapter
from app.settings.config import settings as bow_settings

WEBHOOK = "/api/settings/integrations/teams/webhook"
SERVICE_URL = "https://smba.example.invalid/teams/"


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


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def microsoft(monkeypatch, signing_key):
    """Stand in for Microsoft: accept any bot credentials, publish our key."""
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key()))
    jwk["kid"] = "test-key"

    async def _probe(self, app_id, client_secret, tenant_id):
        return {"success": True, "app_id": app_id}

    async def _keys(self, force_refresh=False):
        return {"keys": [jwk]}

    monkeypatch.setattr(ExternalPlatformService, "_test_teams_token", _probe)
    monkeypatch.setattr(TeamsAdapter, "_get_signing_keys", _keys)

    def _token(app_id):
        now = int(time.time())
        return jwt.encode(
            {"aud": app_id, "iss": TeamsAdapter.EXPECTED_ISSUER, "serviceurl": SERVICE_URL,
             "iat": now, "exp": now + 600},
            signing_key, algorithm="RS256", headers={"kid": "test-key"},
        )

    return _token


@pytest.fixture
def handoffs(monkeypatch):
    seen = []

    async def _capture(db, platform_type, organization_id, event_data, platform_id=None):
        seen.append({"organization_id": str(organization_id), "platform_id": str(platform_id)})
        return {"success": True}

    monkeypatch.setattr(teams_webhook.platform_manager, "handle_incoming_message", _capture)
    return seen


@pytest.fixture
def two_orgs(allow_multiple_orgs, create_user, login_user, whoami, create_organization):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_a = whoami(token)["organizations"][0]["id"]
    org_b = create_organization(name=f"Org B {uuid.uuid4().hex[:6]}", user_token=token)
    return {"token": token, "org_a": org_a, "org_b": org_b}


@pytest.fixture
def one_org(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    return {"token": token, "org": whoami(token)["organizations"][0]["id"]}


def _connect(test_client, token, org_id, tenant_id, app_id):
    return test_client.post(
        "/api/settings/integrations/teams",
        json={"app_id": app_id, "client_secret": "s-" + uuid.uuid4().hex, "tenant_id": tenant_id},
        headers=_h(token, org_id),
    )


def _activity(app_id, tenant_id=None):
    activity = {
        "type": "message",
        "id": "act-" + uuid.uuid4().hex,
        "text": "hello",
        "serviceUrl": SERVICE_URL,
        "from": {"id": "29:" + uuid.uuid4().hex, "name": "Dana"},
        "recipient": {"id": f"28:{app_id}", "name": "Bot"},
        "conversation": {"id": "conv-" + uuid.uuid4().hex},
    }
    if tenant_id:
        activity["channelData"] = {"tenant": {"id": tenant_id}}
    return activity


def _post(test_client, activity, bearer=None):
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    return test_client.post(WEBHOOK, json=activity, headers=headers)


@pytest.mark.e2e
@pytest.mark.parametrize("send_tenant", [True, False])
def test_two_bots_in_one_tenant_each_reach_their_own_org(
    test_client, two_orgs, microsoft, handoffs, send_tenant
):
    t = two_orgs
    tenant = str(uuid.uuid4())
    app_a, app_b = str(uuid.uuid4()), str(uuid.uuid4())
    plat_a = _connect(test_client, t["token"], t["org_a"], tenant, app_a)
    plat_b = _connect(test_client, t["token"], t["org_b"], tenant, app_b)
    assert plat_a.status_code == 200, plat_a.text
    assert plat_b.status_code == 200, plat_b.text

    for app, org, plat in (
        (app_b, t["org_b"], plat_b.json()["id"]),
        (app_a, t["org_a"], plat_a.json()["id"]),
    ):
        handoffs.clear()
        r = _post(test_client, _activity(app, tenant if send_tenant else None), microsoft(app))
        assert r.status_code == 200, r.text
        assert handoffs == [{"organization_id": org, "platform_id": plat}]


@pytest.mark.e2e
def test_activity_cannot_be_addressed_to_a_bot_its_token_was_not_issued_for(
    test_client, two_orgs, microsoft, handoffs
):
    t = two_orgs
    tenant = str(uuid.uuid4())
    app_a, app_b = str(uuid.uuid4()), str(uuid.uuid4())
    assert _connect(test_client, t["token"], t["org_a"], tenant, app_a).status_code == 200
    assert _connect(test_client, t["token"], t["org_b"], tenant, app_b).status_code == 200

    # Token issued for A's bot, activity body names B's bot: handled as A or
    # not at all, never as B.
    _post(test_client, _activity(app_b, tenant), microsoft(app_a))
    assert all(h["organization_id"] == t["org_a"] for h in handoffs)

    # No token at all is refused.
    handoffs.clear()
    assert _post(test_client, _activity(app_b, tenant)).status_code == 401
    assert handoffs == []


@pytest.mark.e2e
def test_with_several_platforms_an_unknown_bot_reaches_no_org(
    test_client, two_orgs, microsoft, handoffs
):
    t = two_orgs
    tenant = str(uuid.uuid4())
    assert _connect(test_client, t["token"], t["org_a"], tenant, str(uuid.uuid4())).status_code == 200
    assert _connect(test_client, t["token"], t["org_b"], tenant, str(uuid.uuid4())).status_code == 200

    stranger = str(uuid.uuid4())
    for tenant_in_activity in (tenant, None):
        r = _post(test_client, _activity(stranger, tenant_in_activity), microsoft(stranger))
        assert r.status_code in (200, 401), r.text
    assert handoffs == []


@pytest.mark.e2e
@pytest.mark.parametrize("tenant_in_activity", ["same", "other", None])
def test_single_teams_platform_keeps_receiving_activities(
    test_client, one_org, microsoft, handoffs, tenant_in_activity
):
    tenant, app = str(uuid.uuid4()), str(uuid.uuid4())
    plat = _connect(test_client, one_org["token"], one_org["org"], tenant, app)
    assert plat.status_code == 200, plat.text

    sent = {"same": tenant, "other": str(uuid.uuid4()), None: None}[tenant_in_activity]
    r = _post(test_client, _activity(app, sent), microsoft(app))
    assert r.status_code == 200, r.text
    assert handoffs == [{"organization_id": one_org["org"], "platform_id": plat.json()["id"]}]


@pytest.mark.e2e
def test_single_teams_platform_still_requires_a_token_for_its_bot(
    test_client, one_org, microsoft, handoffs
):
    tenant, app = str(uuid.uuid4()), str(uuid.uuid4())
    assert _connect(test_client, one_org["token"], one_org["org"], tenant, app).status_code == 200

    assert _post(test_client, _activity(app, tenant), microsoft(str(uuid.uuid4()))).status_code == 401
    assert handoffs == []


@pytest.mark.e2e
def test_a_teams_bot_cannot_be_connected_to_a_second_organization(
    test_client, two_orgs, microsoft
):
    t = two_orgs
    tenant, app = str(uuid.uuid4()), str(uuid.uuid4())
    assert _connect(test_client, t["token"], t["org_a"], tenant, app).status_code == 200

    dup = _connect(test_client, t["token"], t["org_b"], tenant, app)
    assert dup.status_code == 409, dup.text
    assert dup.json()["error_code"] == "integration.connected_elsewhere"
    listing = test_client.get("/api/settings/integrations", headers=_h(t["token"], t["org_b"]))
    assert [p for p in listing.json() if p["platform_type"] == "teams"] == []

    # Another bot in the same tenant is a legitimate second connection.
    assert _connect(test_client, t["token"], t["org_b"], tenant, str(uuid.uuid4())).status_code == 200


@pytest.mark.e2e
def test_an_org_can_reconnect_its_own_teams_bot(test_client, one_org, microsoft):
    tenant, app = str(uuid.uuid4()), str(uuid.uuid4())
    first = _connect(test_client, one_org["token"], one_org["org"], tenant, app)
    assert first.status_code == 200, first.text
    removed = test_client.delete(f"/api/settings/integrations/{first.json()['id']}",
                                 headers=_h(one_org["token"], one_org["org"]))
    assert removed.status_code == 200, removed.text

    assert _connect(test_client, one_org["token"], one_org["org"], tenant, app).status_code == 200
