"""Slack knowledge source, end to end through the API — the Slack twin of
``test_teams_messages_connector.py``.

The admin flow the UI drives (registry entry → connection with the org's own
Slack app → org-wide agent, each user signing in) and the browse contract the
Files tab depends on: until the caller links their Slack account the listing
asks them to connect; once linked it lists messages grouped by conversation,
worded as live messages. The Slack Web API is simulated by the same fake the
client's unit tests use. Each agent's channel choice (the per-agent Slack
"Channels" picker) is set through its API and checked on what the agent
browses.
"""
import httpx
import pytest

SLACK_APP = {"oauth_client_id": "123.456", "oauth_client_secret": "test-secret"}


@pytest.fixture
def org(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return {"token": token, "id": org_id,
            "headers": {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}}


def _slack_agent(test_client, headers, config=None):
    """What IntegrationConnectionForm + createPublicAgent send."""
    conn = test_client.post("/api/connections", headers=headers, json={
        "name": "Slack", "type": "slack_messages", "credentials": SLACK_APP,
        "config": config or {}, "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
    })
    assert conn.status_code == 200, conn.text
    ds = test_client.post("/api/data_sources", headers=headers, json={
        "name": "Slack", "type": "slack_messages", "connection_ids": [conn.json()["id"]],
        "is_public": True, "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
    })
    assert ds.status_code == 200, ds.text
    return conn.json(), ds.json()


def _signed_in(monkeypatch, fake):
    """As if the caller had signed in with Slack: the client is built from the
    connection's stored config plus the overrides the route passes (the
    agent's channel scope — merged as construct_client merges them), a user
    token and the fake Web API."""
    import json

    from app.data_sources.clients.slack_messages_client import SlackMessagesClient
    from app.services.connection_service import ConnectionService

    async def construct(self, db, connection, current_user=None, config_overrides=None, **kwargs):
        config = connection.config
        config = json.loads(config) if isinstance(config, str) else dict(config or {})
        config.update({k: v for k, v in (config_overrides or {}).items() if v is not None and v != ""})
        return SlackMessagesClient(access_token="xoxp-user", transport=httpx.MockTransport(fake.handler), **config)
    monkeypatch.setattr(ConnectionService, "construct_client", construct)


def _fake_with_history():
    from tests.unit.test_slack_messages_client import FakeSlack, _msg

    fake = FakeSlack()
    fake.routes["conversations.history"] = lambda q: {"ok": True, "has_more": False, "messages": [
        _msg("1700000200.000100", text=f"latest in {q['channel']}"),
        _msg("1700000100.000100", user="U0OMER0001", text=f"earlier in {q['channel']}"),
    ]}
    return fake


@pytest.mark.e2e
def test_registry_offers_slack_as_a_live_per_user_message_source(test_client, org):
    entries = test_client.get("/api/available_data_sources", headers=org["headers"]).json()
    slack = next(e for e in entries if e["type"] == "slack_messages")
    assert slack["title"] == "Slack"
    assert slack["data_shape"] == "files"
    assert slack["catalog_ownership"] == "per_user"
    assert slack["ui_form"] == "integration"
    assert list(slack["catalog_nouns"]) == ["message", "messages"]


@pytest.mark.e2e
def test_browse_asks_to_connect_until_the_user_signs_in(test_client, org):
    conn, ds = _slack_agent(test_client, org["headers"])
    r = test_client.get(f"/api/data_sources/{ds['id']}/connections/{conn['id']}/files", headers=org["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connect_required"] is True and body["files"] == []
    assert body["catalog_nouns"] == ["message", "messages"] and body["live"] is True


@pytest.mark.e2e
def test_browse_lists_messages_grouped_by_conversation_once_signed_in(test_client, org, monkeypatch):
    conn, ds = _slack_agent(test_client, org["headers"])
    _signed_in(monkeypatch, _fake_with_history())

    body = test_client.get(
        f"/api/data_sources/{ds['id']}/connections/{conn['id']}/files", headers=org["headers"],
    ).json()
    folders = {f["path"].split("/", 1)[0] for f in body["files"]}
    assert folders == {"#support", "#sales", "#leadership", "DM with Dana", "Group DM: dana, omer, noa"}
    assert body["live"] is True and body["catalog_nouns"] == ["message", "messages"]
    # A message has no original bytes to preview.
    assert body["preview_supported"] is False
    assert all(f["web_url"].startswith("https://acme.slack.com/archives/") for f in body["files"])


def _scope_url(ds, conn):
    return f"/api/data_sources/{ds['id']}/connections/{conn['id']}/channel-scope"


@pytest.mark.e2e
def test_an_agent_reads_only_its_chosen_channels(test_client, org, monkeypatch):
    conn, ds = _slack_agent(test_client, org["headers"])
    # Nothing chosen yet: every channel, DMs included.
    assert test_client.get(_scope_url(ds, conn), headers=org["headers"]).json() == {
        "allowed_channels": [], "include_direct_messages": True}

    r = test_client.put(_scope_url(ds, conn), headers=org["headers"], json={
        "allowed_channels": [{"id": "C0SUPPORT1", "name": "support"}], "include_direct_messages": False})
    assert r.status_code == 200, r.text
    assert test_client.get(_scope_url(ds, conn), headers=org["headers"]).json() == {
        "allowed_channels": [{"id": "C0SUPPORT1", "name": "support"}], "include_direct_messages": False}

    fake = _fake_with_history()
    _signed_in(monkeypatch, fake)
    body = test_client.get(
        f"/api/data_sources/{ds['id']}/connections/{conn['id']}/files", headers=org["headers"],
    ).json()
    assert {f["path"].split("/", 1)[0] for f in body["files"]} == {"#support"}
    # Other channels are never even requested from Slack, and DMs aren't listed.
    assert {q["channel"] for m, q in fake.calls if m == "conversations.history"} == {"C0SUPPORT1"}
    listed_types = next(q["types"] for m, q in fake.calls if m == "users.conversations")
    assert "im" not in listed_types.split(",")


@pytest.mark.e2e
def test_chosen_channels_plus_dms(test_client, org, monkeypatch):
    conn, ds = _slack_agent(test_client, org["headers"])
    test_client.put(_scope_url(ds, conn), headers=org["headers"], json={
        "allowed_channels": [{"id": "C0SUPPORT1", "name": "support"}], "include_direct_messages": True})
    _signed_in(monkeypatch, _fake_with_history())
    body = test_client.get(
        f"/api/data_sources/{ds['id']}/connections/{conn['id']}/files", headers=org["headers"],
    ).json()
    assert {f["path"].split("/", 1)[0] for f in body["files"]} == {
        "#support", "DM with Dana", "Group DM: dana, omer, noa"}


@pytest.mark.e2e
def test_the_picker_lists_the_users_channels_without_dms(test_client, org, monkeypatch):
    conn, ds = _slack_agent(test_client, org["headers"])
    url = f"/api/data_sources/{ds['id']}/connections/{conn['id']}/conversations"
    # Not signed in with Slack yet: ask to connect.
    body = test_client.get(url, headers=org["headers"]).json()
    assert body["connect_required"] is True and body["channels"] == []

    # Signed in: every channel the user is in — even ones outside the agent's
    # current choice, so the choice can be widened again.
    test_client.put(_scope_url(ds, conn), headers=org["headers"], json={
        "allowed_channels": [{"id": "C0SUPPORT1", "name": "support"}]})
    _signed_in(monkeypatch, _fake_with_history())
    body = test_client.get(url, headers=org["headers"]).json()
    assert body["connect_required"] is False
    assert body["channels"] == [
        {"id": "G0PRIVATE1", "name": "leadership", "is_private": True},
        {"id": "C0SALES001", "name": "sales", "is_private": False},
        {"id": "C0SUPPORT1", "name": "support", "is_private": False},
    ]


@pytest.mark.e2e
def test_only_an_agent_manager_can_change_its_channels(test_client, org, create_user, login_user):
    import uuid
    conn, ds = _slack_agent(test_client, org["headers"])
    email = f"slack_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(f"/api/organizations/{org['id']}/members", headers=org["headers"],
                     json={"organization_id": org["id"], "email": email, "role": "member"})
    create_user(email=email, password="test123")
    member = {"Authorization": f"Bearer {login_user(email=email, password='test123')}",
              "X-Organization-Id": str(org["id"])}
    assert test_client.get(_scope_url(ds, conn), headers=member).status_code == 200
    r = test_client.put(_scope_url(ds, conn), headers=member, json={
        "allowed_channels": [{"id": "C0SALES001", "name": "sales"}]})
    assert r.status_code == 403, r.text


@pytest.mark.e2e
def test_channel_choice_is_slack_only_and_goes_away_with_the_connection(test_client, org):
    conn, ds = _slack_agent(test_client, org["headers"])
    test_client.put(_scope_url(ds, conn), headers=org["headers"], json={
        "allowed_channels": [{"id": "C0SUPPORT1", "name": "support"}]})
    # An agent keeps at least one connection, so give it a second one first.
    other = test_client.post("/api/connections", headers=org["headers"], json={
        "name": "Slack 2", "type": "slack_messages", "credentials": SLACK_APP, "config": {},
        "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"]}).json()
    assert test_client.post(f"/api/data_sources/{ds['id']}/connections/{other['id']}",
                            headers=org["headers"]).status_code == 200
    r = test_client.delete(f"/api/data_sources/{ds['id']}/connections/{conn['id']}", headers=org["headers"])
    assert r.status_code == 200, r.text
    # Relinking starts from "every channel" — the old choice doesn't come back.
    r = test_client.post(f"/api/data_sources/{ds['id']}/connections/{conn['id']}", headers=org["headers"])
    assert r.status_code == 200, r.text
    assert test_client.get(_scope_url(ds, conn), headers=org["headers"]).json()["allowed_channels"] == []


def _kind_of(test_client, headers, ds_id):
    rows = test_client.get("/api/data_sources/active", headers=headers, params={"include_unconnected": True}).json()
    row = next(r for r in rows if r["id"] == ds_id)
    return row["kind"], row.get("kind_explicit"), row["is_connector"]


@pytest.mark.e2e
def test_a_slack_source_is_a_connector_and_an_admin_can_turn_it_into_an_agent(test_client, org):
    _, ds = _slack_agent(test_client, org["headers"])
    assert _kind_of(test_client, org["headers"], ds["id"]) == ("connector", None, True)

    r = test_client.put(f"/api/data_sources/{ds['id']}", headers=org["headers"], json={"kind": "agent"})
    assert r.status_code == 200, r.text
    assert _kind_of(test_client, org["headers"], ds["id"]) == ("agent", "agent", False)


@pytest.mark.e2e
def test_attaching_through_the_agent_honours_its_channels(test_client, org, monkeypatch):
    """Copying a message into a chat THROUGH an agent is a read on that agent,
    so it gets the agent's channel scope like every other agent read."""
    from app.services.connection_service import ConnectionService

    conn, ds = _slack_agent(test_client, org["headers"])
    test_client.put(_scope_url(ds, conn), headers=org["headers"], json={
        "allowed_channels": [{"id": "C0SUPPORT1", "name": "support"}], "include_direct_messages": False})
    seen = {}

    async def construct(self, db, connection, current_user=None, config_overrides=None, **kwargs):
        seen["overrides"] = config_overrides
        raise RuntimeError("stop here")  # the scope is what's under test
    monkeypatch.setattr(ConnectionService, "construct_client", construct)

    test_client.post("/api/files/attach", headers=org["headers"], json={
        "data_source_id": ds["id"], "connection_id": conn["id"], "ref": "slack:C0SALES001/1700000100.000100"})
    assert seen["overrides"] == {"allowed_channels": ["C0SUPPORT1"], "include_direct_messages": False}


@pytest.mark.e2e
def test_the_channel_choice_takes_ids_only(test_client, org):
    conn, ds = _slack_agent(test_client, org["headers"])
    r = test_client.put(_scope_url(ds, conn), headers=org["headers"], json={
        "allowed_channels": [{"id": "general", "name": "general"}]})
    assert r.status_code == 400 and "general" in r.text
    assert test_client.get(_scope_url(ds, conn), headers=org["headers"]).json()["allowed_channels"] == []


@pytest.mark.e2e
def test_deleting_the_connection_removes_agents_channel_choices(test_client, org):
    conn, ds = _slack_agent(test_client, org["headers"])
    test_client.put(_scope_url(ds, conn), headers=org["headers"], json={
        "allowed_channels": [{"id": "C0SUPPORT1", "name": "support"}]})
    r = test_client.delete(f"/api/connections/{conn['id']}", headers=org["headers"])
    assert r.status_code in (200, 204), r.text

    import asyncio
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.data_source_connection_setting import DataSourceConnectionSetting

    async def rows():
        async with async_session_maker() as db:
            return (await db.execute(select(DataSourceConnectionSetting).where(
                DataSourceConnectionSetting.connection_id == conn["id"]))).scalars().all()
    assert asyncio.run(rows()) == []


@pytest.mark.e2e
def test_concurrent_first_saves_of_a_scope_do_not_fail(test_client, org):
    """Two managers (or a double click) saving the first scope at once: the
    second write updates instead of hitting the unique constraint (500)."""
    import asyncio
    from app.dependencies import async_session_maker
    from app.services.slack_channel_scope import get_scope, set_scope

    conn, ds = _slack_agent(test_client, org["headers"])

    async def both():
        async with async_session_maker() as a, async_session_maker() as b:
            await asyncio.gather(
                set_scope(a, ds["id"], conn["id"], {"allowed_channels": [{"id": "C0SUPPORT1", "name": "support"}]}),
                set_scope(b, ds["id"], conn["id"], {"allowed_channels": [{"id": "C0SALES001", "name": "sales"}]}),
            )
        async with async_session_maker() as c:
            return await get_scope(c, ds["id"], conn["id"])
    scope = asyncio.run(both())
    assert len(scope["allowed_channels"]) == 1
