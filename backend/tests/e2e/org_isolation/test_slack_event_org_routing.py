"""Slack inbound events are handled by the organization whose Slack app sent them.

Contract: a Slack workspace may host one Slack app per organization, so the
workspace (team) id alone never decides which organization handles an event.

* Events API (HTTP): an event is accepted only when its signature verifies
  against a connected platform's signing secret, and it is handled as that
  platform. A platform without a signing secret (Socket Mode) is not served
  over HTTP at all.
* Socket Mode: an event is handled as the platform whose socket delivered it.
* A second organization cannot connect a Slack app another organization
  already connected.

Slack's API probes are the only boundary mocked on the connect path. The
hand-off to the agent pipeline (which would call an LLM and Slack) is captured
instead of run: which organization/platform receives the event IS the contract.
"""
import asyncio
import hashlib
import hmac
import json
import time
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.external_platform import ExternalPlatform
from app.services import slack_event_service, slack_socket_service
from app.services.external_platform_service import ExternalPlatformService
from app.settings.config import settings as bow_settings

WEBHOOK = "/api/settings/integrations/slack/webhook"


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
def slack_workspace(monkeypatch):
    """One Slack workspace: every bot token probes to the same team id."""
    team_id = "T" + uuid.uuid4().hex[:8].upper()

    async def _probe(self, bot_token):
        return {"success": True, "workspace": {"id": team_id, "name": "Acme"}}

    async def _open(app_token):
        return "wss://example.invalid/socket"

    monkeypatch.setattr(ExternalPlatformService, "_test_slack_token", _probe)
    monkeypatch.setattr(slack_socket_service, "open_socket_url", _open)
    return team_id


@pytest.fixture
def handoffs(monkeypatch):
    """Record which organization/platform each accepted event is handed to."""
    seen = []

    async def _capture(db, platform_type, organization_id, event_data, platform_id=None):
        seen.append({"organization_id": str(organization_id), "platform_id": str(platform_id)})
        return {"success": True}

    monkeypatch.setattr(slack_event_service.platform_manager, "handle_incoming_message", _capture)
    return seen


@pytest.fixture
def two_orgs(allow_multiple_orgs, create_user, login_user, whoami, create_organization):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_a = whoami(token)["organizations"][0]["id"]
    org_b = create_organization(name=f"Org B {uuid.uuid4().hex[:6]}", user_token=token)
    return {"token": token, "org_a": org_a, "org_b": org_b}


def _app_id():
    return "A" + uuid.uuid4().hex[:9].upper()


def _connect(test_client, token, org_id, *, app_id=None, signing_secret=None, mode=None):
    """Connect a Slack app. With ``app_id`` the app-level token carries it
    (Socket Mode); without, it is an Events API app with a signing secret."""
    suffix = uuid.uuid4().hex[:10]
    body = {"bot_token": f"xoxb-{suffix}"}
    if app_id:
        body["connection_mode"] = mode or "socket_mode"
        body["app_token"] = f"xapp-1-{app_id}-{int(time.time())}-{suffix}"
    else:
        body["connection_mode"] = "events_api"
    if signing_secret:
        body["signing_secret"] = signing_secret
    return test_client.post("/api/settings/integrations/slack", json=body, headers=_h(token, org_id))


def _mention(team_id, api_app_id=None):
    payload = {
        "type": "event_callback",
        "team_id": team_id,
        "event_id": "Ev" + uuid.uuid4().hex[:12],
        "event": {"type": "app_mention", "user": "U1", "channel": "C1", "text": "hello", "ts": "1.0"},
    }
    if api_app_id:
        payload["api_app_id"] = api_app_id
    return payload


def _post(test_client, payload, signing_secret=None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signing_secret:
        ts = str(int(time.time()))
        digest = hmac.new(signing_secret.encode(), f"v0:{ts}:{body.decode()}".encode(), hashlib.sha256)
        headers["x-slack-request-timestamp"] = ts
        headers["x-slack-signature"] = "v0=" + digest.hexdigest()
    return test_client.post(WEBHOOK, content=body, headers=headers)


# ── Events API (HTTP) ────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_http_event_is_handled_by_the_org_whose_app_sent_it(
    test_client, two_orgs, slack_workspace, handoffs
):
    t = two_orgs
    app_a, app_b = _app_id(), _app_id()
    secret_a, secret_b = "sec-" + uuid.uuid4().hex, "sec-" + uuid.uuid4().hex
    plat_a = _connect(test_client, t["token"], t["org_a"], app_id=app_a, signing_secret=secret_a)
    plat_b = _connect(test_client, t["token"], t["org_b"], app_id=app_b, signing_secret=secret_b)
    assert plat_a.status_code == 200, plat_a.text
    assert plat_b.status_code == 200, plat_b.text

    for app, secret, org, plat in (
        (app_b, secret_b, t["org_b"], plat_b.json()["id"]),
        (app_a, secret_a, t["org_a"], plat_a.json()["id"]),
    ):
        handoffs.clear()
        r = _post(test_client, _mention(slack_workspace, app), secret)
        assert r.status_code == 200, r.text
        assert handoffs == [{"organization_id": org, "platform_id": plat}]


@pytest.mark.e2e
def test_http_event_routes_by_signature_when_the_app_id_is_not_recorded(
    test_client, two_orgs, slack_workspace, handoffs
):
    t = two_orgs
    secret_a, secret_b = "sec-" + uuid.uuid4().hex, "sec-" + uuid.uuid4().hex
    assert _connect(test_client, t["token"], t["org_a"], signing_secret=secret_a).status_code == 200
    plat_b = _connect(test_client, t["token"], t["org_b"], signing_secret=secret_b)
    assert plat_b.status_code == 200, plat_b.text

    r = _post(test_client, _mention(slack_workspace, _app_id()), secret_b)
    assert r.status_code == 200, r.text
    assert handoffs == [{"organization_id": t["org_b"], "platform_id": plat_b.json()["id"]}]


@pytest.mark.e2e
def test_http_event_signed_by_one_app_cannot_be_addressed_to_another(
    test_client, two_orgs, slack_workspace, handoffs
):
    t = two_orgs
    app_a, app_b = _app_id(), _app_id()
    secret_a = "sec-" + uuid.uuid4().hex
    assert _connect(test_client, t["token"], t["org_a"], app_id=app_a, signing_secret=secret_a).status_code == 200
    assert _connect(test_client, t["token"], t["org_b"], app_id=app_b,
                    signing_secret="sec-" + uuid.uuid4().hex).status_code == 200

    # Signed with A's secret but naming B's app (or an app nobody connected).
    for claimed in (app_b, _app_id()):
        r = _post(test_client, _mention(slack_workspace, claimed), secret_a)
        assert r.status_code == 401, r.text
    assert handoffs == []


@pytest.mark.e2e
@pytest.mark.parametrize("signed_with", [None, "a-secret-the-platform-never-had"])
def test_http_event_is_refused_for_a_platform_without_a_signing_secret(
    test_client, create_user, login_user, whoami, slack_workspace, handoffs, signed_with
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    app = _app_id()
    assert _connect(test_client, token, org_id, app_id=app).status_code == 200  # Socket Mode, no secret

    r = _post(test_client, _mention(slack_workspace, app), signed_with)
    assert r.status_code == 401, r.text
    assert handoffs == []


@pytest.mark.e2e
def test_http_event_with_a_bad_signature_is_refused(
    test_client, create_user, login_user, whoami, slack_workspace, handoffs
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    assert _connect(test_client, token, org_id, signing_secret="right-secret").status_code == 200

    assert _post(test_client, _mention(slack_workspace), "wrong-secret").status_code == 401
    assert _post(test_client, _mention(slack_workspace)).status_code == 401
    assert handoffs == []


@pytest.mark.e2e
@pytest.mark.parametrize("with_app_id", [True, False])
def test_single_org_events_api_platform_still_receives_signed_events(
    test_client, create_user, login_user, whoami, slack_workspace, handoffs, with_app_id
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    secret = "sec-" + uuid.uuid4().hex
    plat = _connect(test_client, token, org_id, signing_secret=secret)
    assert plat.status_code == 200, plat.text

    r = _post(test_client, _mention(slack_workspace, _app_id() if with_app_id else None), secret)
    assert r.status_code == 200, r.text
    assert handoffs == [{"organization_id": org_id, "platform_id": plat.json()["id"]}]


async def _seed_events_api_platform_without_secret(org_id, team_id):
    async with async_session_maker() as db:
        platform = ExternalPlatform(
            organization_id=org_id, platform_type="slack", is_active=True,
            platform_config={"team_id": team_id, "team_name": "Acme"},
        )
        platform.encrypt_credentials({"bot_token": "xoxb-" + uuid.uuid4().hex})
        db.add(platform)
        await db.commit()
        return str(platform.id)


@pytest.fixture
def require_signed_events():
    security = bow_settings.bow_config.security
    saved = security.slack_require_signed_events
    security.slack_require_signed_events = True
    try:
        yield
    finally:
        security.slack_require_signed_events = saved


@pytest.fixture
def legacy_unsigned_platform(create_user, login_user, whoami):
    """An Events API platform stored without a signing secret. Seeded
    directly: the connect API requires the secret in Events API mode, but
    deployments upgraded from older versions may hold such rows."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    team_id = "T" + uuid.uuid4().hex[:8].upper()
    platform_id = asyncio.run(_seed_events_api_platform_without_secret(org_id, team_id))
    return {"token": token, "org_id": org_id, "team_id": team_id, "platform_id": platform_id}


@pytest.mark.e2e
def test_sole_events_api_platform_without_a_secret_keeps_receiving_events(
    test_client, legacy_unsigned_platform, handoffs
):
    p = legacy_unsigned_platform
    r = _post(test_client, _mention(p["team_id"], _app_id()))
    assert r.status_code == 200, r.text
    assert handoffs == [{"organization_id": p["org_id"], "platform_id": p["platform_id"]}]


@pytest.mark.e2e
def test_unsigned_events_are_refused_when_signatures_are_required(
    test_client, legacy_unsigned_platform, require_signed_events, handoffs
):
    r = _post(test_client, _mention(legacy_unsigned_platform["team_id"]))
    assert r.status_code == 401, r.text
    assert handoffs == []


@pytest.mark.e2e
@pytest.mark.parametrize("other_has_secret", [True, False])
def test_unsigned_events_are_refused_when_two_platforms_share_the_workspace(
    test_client, two_orgs, slack_workspace, handoffs, other_has_secret
):
    t = two_orgs
    if other_has_secret:
        assert _connect(test_client, t["token"], t["org_a"], signing_secret="s-a").status_code == 200
    else:
        asyncio.run(_seed_events_api_platform_without_secret(t["org_a"], slack_workspace))
    asyncio.run(_seed_events_api_platform_without_secret(t["org_b"], slack_workspace))

    r = _post(test_client, _mention(slack_workspace))
    assert r.status_code == 401, r.text
    assert handoffs == []


@pytest.mark.e2e
def test_url_verification_challenge_needs_no_platform(test_client):
    challenge = uuid.uuid4().hex
    r = test_client.post(WEBHOOK, json={"type": "url_verification", "challenge": challenge})
    assert r.status_code == 200
    assert r.json() == {"challenge": challenge}


@pytest.mark.e2e
def test_event_for_an_unconnected_workspace_is_acknowledged_and_dropped(test_client, handoffs):
    r = _post(test_client, _mention("T" + uuid.uuid4().hex[:8].upper()))
    assert r.status_code == 200
    assert handoffs == []


# ── Socket Mode ──────────────────────────────────────────────────────────────

async def _deliver_on_socket(payload, platform_id):
    async with async_session_maker() as db:
        return await slack_event_service.handle_slack_event(db, payload, platform_id=platform_id)


@pytest.mark.e2e
def test_socket_event_is_handled_by_the_platform_whose_socket_delivered_it(
    test_client, two_orgs, slack_workspace, handoffs
):
    t = two_orgs
    app_a, app_b = _app_id(), _app_id()
    plat_a = _connect(test_client, t["token"], t["org_a"], app_id=app_a).json()["id"]
    plat_b = _connect(test_client, t["token"], t["org_b"], app_id=app_b).json()["id"]

    for app, org, plat in ((app_b, t["org_b"], plat_b), (app_a, t["org_a"], plat_a)):
        handoffs.clear()
        res = asyncio.run(_deliver_on_socket(_mention(slack_workspace, app), plat))
        assert res["ok"] is True
        assert handoffs == [{"organization_id": org, "platform_id": plat}]


@pytest.mark.e2e
def test_socket_event_for_a_deactivated_platform_is_not_handled_by_another_org(
    test_client, two_orgs, slack_workspace, handoffs
):
    t = two_orgs
    _connect(test_client, t["token"], t["org_a"], app_id=_app_id())
    app_b = _app_id()
    plat_b = _connect(test_client, t["token"], t["org_b"], app_id=app_b).json()["id"]
    r = test_client.put(f"/api/settings/integrations/{plat_b}", json={"is_active": False},
                        headers=_h(t["token"], t["org_b"]))
    assert r.status_code == 200, r.text

    asyncio.run(_deliver_on_socket(_mention(slack_workspace, app_b), plat_b))
    assert handoffs == []


# ── Connect-time uniqueness ──────────────────────────────────────────────────

@pytest.mark.e2e
def test_a_slack_app_cannot_be_connected_to_a_second_organization(
    test_client, two_orgs, slack_workspace
):
    t = two_orgs
    app = _app_id()
    assert _connect(test_client, t["token"], t["org_a"], app_id=app).status_code == 200

    dup = _connect(test_client, t["token"], t["org_b"], app_id=app)
    assert dup.status_code == 409, dup.text
    assert dup.json()["error_code"] == "integration.connected_elsewhere"
    listing = test_client.get("/api/settings/integrations", headers=_h(t["token"], t["org_b"]))
    assert [p for p in listing.json() if p["platform_type"] == "slack"] == []

    # A different app in the same workspace is a legitimate second connection.
    assert _connect(test_client, t["token"], t["org_b"], app_id=_app_id()).status_code == 200


@pytest.mark.e2e
def test_an_org_can_reconnect_its_own_slack_app(
    test_client, create_user, login_user, whoami, slack_workspace
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    app = _app_id()
    first = _connect(test_client, token, org_id, app_id=app)
    assert first.status_code == 200, first.text
    removed = test_client.delete(f"/api/settings/integrations/{first.json()['id']}",
                                 headers=_h(token, org_id))
    assert removed.status_code == 200, removed.text

    again = _connect(test_client, token, org_id, app_id=app)
    assert again.status_code == 200, again.text
    assert again.json()["platform_config"]["app_id"] == app
