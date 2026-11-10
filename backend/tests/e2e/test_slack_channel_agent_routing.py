"""End-to-end tests for Slack channel -> agent routing configuration.

Contract: a Slack workspace is a single connection, so an admin scopes
*individual channels* to agents rather than the whole instance. The config
surface must let them:

* set a channel's agent scope, independently of the connection default and of
  other channels;
* see channels that were hand-added (and, in production, discovered on traffic)
  in a picker, each with its current scope and label;
* clear any key back to "unscoped".

Setting a scope is gated by ``manage_settings``. Slack's auth probe is the only
boundary mocked. The routing *behavior* (channel scope narrows the answering
agents) is covered by the unit tests for ``resolve_scope_agent_ids`` /
``_apply_agent_scope``; here we lock the configuration API and the discovery
registry that drive it.
"""
import sqlite3
import uuid

import pytest

from app.services.external_platform_service import ExternalPlatformService


@pytest.fixture
def mock_slack_probe(monkeypatch):
    async def _ok(self, bot_token):
        return {
            "success": True,
            "workspace": {"id": "T" + uuid.uuid4().hex[:8], "name": "Acme"},
        }

    monkeypatch.setattr(ExternalPlatformService, "_test_slack_token", _ok)


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _connect_slack(test_client, token, org_id):
    # events_api mode avoids the socket-open probe; only the token probe (mocked)
    # runs. A signing secret is required for events_api.
    body = {
        "bot_token": "xoxb-test",
        "connection_mode": "events_api",
        "signing_secret": "shhh",
    }
    r = test_client.post("/api/settings/integrations/slack", json=body,
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
def test_channel_scope_is_independent_of_default_and_other_channels(
    mock_slack_probe, create_user, login_user, whoami,
    create_data_source, test_client, tmp_path
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    platform_id = _connect_slack(test_client, token, org_id)
    sales = _sqlite_agent(create_data_source, token, org_id, tmp_path, "sales")
    finance = _sqlite_agent(create_data_source, token, org_id, tmp_path, "finance")
    other = _sqlite_agent(create_data_source, token, org_id, tmp_path, "other")

    chan_sales = "C_SALES_" + uuid.uuid4().hex[:6]
    chan_fin = "C_FIN_" + uuid.uuid4().hex[:6]

    # Nothing is scoped on a fresh connection.
    r = test_client.get(f"/api/settings/integrations/{platform_id}/agents",
                        headers=_headers(token, org_id))
    assert r.status_code == 200 and r.json()["agent_ids"] == []

    # Map #sales -> sales agent (with a label), #finance -> finance agent.
    r = test_client.put(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan_sales}",
        json={"agent_ids": [sales], "channel_name": "#sales"},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200 and r.json()["agent_ids"] == [sales]
    test_client.put(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan_fin}",
        json={"agent_ids": [finance], "channel_name": "#finance"},
        headers=_headers(token, org_id),
    )

    # Set a different connection default.
    r = test_client.put(f"/api/settings/integrations/{platform_id}/agents",
                        json={"agent_ids": [other]}, headers=_headers(token, org_id))
    assert set(r.json()["agent_ids"]) == {other}

    # Each key reads back its own set, independently.
    assert test_client.get(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan_sales}",
        headers=_headers(token, org_id)).json()["agent_ids"] == [sales]
    assert test_client.get(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan_fin}",
        headers=_headers(token, org_id)).json()["agent_ids"] == [finance]
    assert test_client.get(
        f"/api/settings/integrations/{platform_id}/agents",
        headers=_headers(token, org_id)).json()["agent_ids"] == [other]


@pytest.mark.e2e
def test_channels_listing_reflects_mappings_and_labels(
    mock_slack_probe, create_user, login_user, whoami,
    create_data_source, test_client, tmp_path
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    platform_id = _connect_slack(test_client, token, org_id)
    sales = _sqlite_agent(create_data_source, token, org_id, tmp_path, "sales")
    default_ds = _sqlite_agent(create_data_source, token, org_id, tmp_path, "def")
    chan_sales = "C_SALES_" + uuid.uuid4().hex[:6]

    # No channels discovered yet.
    r = test_client.get(f"/api/settings/integrations/{platform_id}/channels",
                        headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    assert r.json()["channels"] == []
    assert r.json()["default_agent_ids"] == []

    # Hand-add a channel by mapping it; it shows in the picker with its label
    # and scope. Also set a connection default.
    test_client.put(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan_sales}",
        json={"agent_ids": [sales], "channel_name": "#sales"},
        headers=_headers(token, org_id),
    )
    test_client.put(f"/api/settings/integrations/{platform_id}/agents",
                    json={"agent_ids": [default_ds]}, headers=_headers(token, org_id))

    r = test_client.get(f"/api/settings/integrations/{platform_id}/channels",
                        headers=_headers(token, org_id))
    body = r.json()
    assert body["default_agent_ids"] == [default_ds]
    assert len(body["channels"]) == 1
    ch = body["channels"][0]
    assert ch["external_channel_id"] == chan_sales
    assert ch["name"] == "#sales"
    assert ch["agent_ids"] == [sales]


@pytest.mark.e2e
def test_clearing_a_channel_scope_leaves_default_and_listing(
    mock_slack_probe, create_user, login_user, whoami,
    create_data_source, test_client, tmp_path
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    platform_id = _connect_slack(test_client, token, org_id)
    sales = _sqlite_agent(create_data_source, token, org_id, tmp_path, "sales")
    default_ds = _sqlite_agent(create_data_source, token, org_id, tmp_path, "def")
    chan = "C_X_" + uuid.uuid4().hex[:6]

    test_client.put(f"/api/settings/integrations/{platform_id}/agents",
                    json={"agent_ids": [default_ds]}, headers=_headers(token, org_id))
    test_client.put(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan}",
        json={"agent_ids": [sales], "channel_name": "#x"},
        headers=_headers(token, org_id),
    )

    # Clear only the channel scope.
    r = test_client.put(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan}",
        json={"agent_ids": []}, headers=_headers(token, org_id),
    )
    assert r.json()["agent_ids"] == []

    # Default is untouched; the channel still shows in the picker (now unscoped).
    body = test_client.get(f"/api/settings/integrations/{platform_id}/channels",
                           headers=_headers(token, org_id)).json()
    assert body["default_agent_ids"] == [default_ds]
    assert len(body["channels"]) == 1
    assert body["channels"][0]["agent_ids"] == []


@pytest.mark.e2e
def test_adding_the_same_channel_twice_is_idempotent_and_relabels(
    mock_slack_probe, create_user, login_user, whoami,
    create_data_source, test_client, tmp_path
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    platform_id = _connect_slack(test_client, token, org_id)
    sales = _sqlite_agent(create_data_source, token, org_id, tmp_path, "sales")
    chan = "C_DUP_" + uuid.uuid4().hex[:6]

    # First mapping with a label.
    test_client.put(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan}",
        json={"agent_ids": [sales], "channel_name": "#old"},
        headers=_headers(token, org_id),
    )
    # Second write to the same channel with a new label.
    test_client.put(
        f"/api/settings/integrations/{platform_id}/agents?channel={chan}",
        json={"agent_ids": [sales], "channel_name": "#new"},
        headers=_headers(token, org_id),
    )

    body = test_client.get(f"/api/settings/integrations/{platform_id}/channels",
                           headers=_headers(token, org_id)).json()
    # Exactly one row for the channel (upsert, not duplicate), relabeled.
    matching = [c for c in body["channels"] if c["external_channel_id"] == chan]
    assert len(matching) == 1
    assert matching[0]["name"] == "#new"
    assert matching[0]["agent_ids"] == [sales]


@pytest.mark.e2e
def test_channel_scope_requires_manage_settings(
    mock_slack_probe, create_user, login_user, whoami, test_client
):
    owner_email = f"chan_owner_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=owner_email, password="test123")
    owner_token = login_user(email=owner_email, password="test123")
    org_id = whoami(owner_token)["organizations"][0]["id"]
    platform_id = _connect_slack(test_client, owner_token, org_id)

    member_email = f"chan_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(owner_token, org_id),
    )
    create_user(email=member_email, password="test123")
    member_token = login_user(email=member_email, password="test123")

    r = test_client.put(
        f"/api/settings/integrations/{platform_id}/agents?channel=C_NOPE",
        json={"agent_ids": []}, headers=_headers(member_token, org_id),
    )
    assert r.status_code == 403


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_discovered_channel_name_is_resolved_once(
    mock_slack_probe, create_user, login_user, whoami, test_client
):
    """A channel's human name is resolved from the adapter exactly once.

    Slack events carry only the channel id, so discovery labels a channel by
    asking the adapter (``conversations.info``) on first sighting. That lookup
    must fire only when no name is known yet, and must not repeat on every
    later message — otherwise each channel mention would cost an extra Slack
    call. Here we drive ``record_seen_channel`` directly with a counting
    fetcher and lock both halves of that contract.
    """
    from app.services.external_platform_service import ExternalPlatformService
    from app.dependencies import async_session_maker

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    platform_id = _connect_slack(test_client, token, org_id)

    svc = ExternalPlatformService()
    calls = []

    async def fetcher(cid):
        calls.append(cid)
        return "#sales"

    chan = "C_NAME_" + uuid.uuid4().hex[:6]

    # First sighting: no name passed and none stored -> resolve it once.
    async with async_session_maker() as db:
        await svc.record_seen_channel(
            db, org_id, platform_id, chan,
            channel_type="channel", name_fetcher=fetcher,
        )
    assert calls == [chan]

    async with async_session_maker() as db:
        rows = await svc.list_channels(db, platform_id)
    row = next(c for c in rows if c.external_channel_id == chan)
    assert row.name == "#sales"

    # Second sighting: a name is already on record -> fetcher is NOT consulted.
    async with async_session_maker() as db:
        await svc.record_seen_channel(
            db, org_id, platform_id, chan,
            channel_type="channel", name_fetcher=fetcher,
        )
    assert calls == [chan]
