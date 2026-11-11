"""Microsoft Teams knowledge source, end to end through the API.

The admin flow the UI drives (registry entry → connection with the org's Entra
app → org-wide agent, each user signing in), and the browse contract the Files
tab depends on: until the caller links their Microsoft account the listing
asks them to connect; once linked it lists messages grouped by conversation,
worded as live messages rather than an indexed file scope. Microsoft Graph is
simulated by the same fake the client's unit tests use; everything inside the
app runs for real.
"""
from urllib.parse import parse_qs, urlparse

import pytest

ENTRA_APP = {
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "client_id": "22222222-2222-2222-2222-222222222222",
    "client_secret": "test-secret",
}


@pytest.fixture
def org(create_user, login_user, whoami):
    def member(role_user=None):
        user = role_user or create_user()
        token = login_user(user["email"], user["password"])
        return token
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return {"token": token, "id": org_id,
            "headers": {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}}


def _teams_agent(test_client, headers):
    """What IntegrationConnectionForm + createPublicAgent send."""
    conn = test_client.post("/api/connections", headers=headers, json={
        "name": "Microsoft Teams", "type": "teams_messages", "credentials": ENTRA_APP,
        "config": {}, "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
    })
    assert conn.status_code == 200, conn.text
    ds = test_client.post("/api/data_sources", headers=headers, json={
        "name": "Microsoft Teams", "type": "teams_messages", "connection_ids": [conn.json()["id"]],
        "is_public": True, "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
    })
    assert ds.status_code == 200, ds.text
    return conn.json(), ds.json()


@pytest.mark.e2e
def test_registry_offers_teams_as_a_live_per_user_message_source(test_client, org):
    entries = test_client.get("/api/available_data_sources", headers=org["headers"]).json()
    teams = next(e for e in entries if e["type"] == "teams_messages")
    assert teams["title"] == "Microsoft Teams"
    assert teams["data_shape"] == "files"
    assert teams["catalog_ownership"] == "per_user"
    assert teams["ui_form"] == "integration"
    assert list(teams["catalog_nouns"]) == ["message", "messages"]


@pytest.mark.e2e
def test_sign_in_with_microsoft_requests_the_teams_read_scopes(test_client, org):
    conn, _ = _teams_agent(test_client, org["headers"])
    r = test_client.get(f"/api/connections/{conn['id']}/oauth/authorize", headers=org["headers"])
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["authorization_url"])
    assert url.netloc == "login.microsoftonline.com" and ENTRA_APP["tenant_id"] in url.path
    q = parse_qs(url.query)
    assert q["client_id"] == [ENTRA_APP["client_id"]]
    assert {"Chat.Read", "ChannelMessage.Read.All", "offline_access"} <= set(q["scope"][0].split())


@pytest.mark.e2e
def test_browse_asks_to_connect_until_the_user_signs_in(test_client, org):
    conn, ds = _teams_agent(test_client, org["headers"])
    r = test_client.get(f"/api/data_sources/{ds['id']}/connections/{conn['id']}/files", headers=org["headers"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connect_required"] is True and body["files"] == []
    assert body["catalog_nouns"] == ["message", "messages"] and body["live"] is True


@pytest.mark.e2e
def test_browse_lists_messages_grouped_by_conversation_once_signed_in(test_client, org, monkeypatch):
    import httpx
    from app.data_sources.clients.graph_teams_client import GraphTeamsClient
    from app.services.connection_service import ConnectionService
    from tests.unit.test_graph_teams_client import FakeTeams

    conn, ds = _teams_agent(test_client, org["headers"])

    async def signed_in(self, *args, **kwargs):
        client = GraphTeamsClient(access_token="delegated-test-token")
        client._http = httpx.Client(transport=httpx.MockTransport(FakeTeams()))
        return client
    monkeypatch.setattr(ConnectionService, "construct_client", signed_in)

    body = test_client.get(
        f"/api/data_sources/{ds['id']}/connections/{conn['id']}/files", headers=org["headers"],
    ).json()
    folders = {f["path"].split("/", 1)[0] for f in body["files"]}
    assert folders == {"Finance › General", "Finance › Month-end close", "Chat with Omer Finance", "Forecast 2027"}
    assert body["live"] is True and body["catalog_nouns"] == ["message", "messages"]
    # A message has no original bytes to preview.
    assert body["preview_supported"] is False
    assert all(f["web_url"] for f in body["files"])


def _kind_of(test_client, headers, ds_id):
    rows = test_client.get("/api/data_sources/active", headers=headers, params={"include_unconnected": True}).json()
    row = next(r for r in rows if r["id"] == ds_id)
    return row["kind"], row.get("kind_explicit"), row["is_connector"]


@pytest.mark.e2e
def test_a_teams_source_is_a_connector_and_an_admin_can_turn_it_into_an_agent(test_client, org):
    _, ds = _teams_agent(test_client, org["headers"])
    # Derived: each person's own Teams → a connector.
    assert _kind_of(test_client, org["headers"], ds["id"]) == ("connector", None, True)

    r = test_client.put(f"/api/data_sources/{ds['id']}", headers=org["headers"], json={"kind": "agent"})
    assert r.status_code == 200, r.text
    assert _kind_of(test_client, org["headers"], ds["id"]) == ("agent", "agent", False)

    r = test_client.put(f"/api/data_sources/{ds['id']}", headers=org["headers"], json={"kind": "auto"})
    assert r.status_code == 200, r.text
    assert _kind_of(test_client, org["headers"], ds["id"]) == ("connector", None, True)


@pytest.mark.e2e
def test_the_create_flow_records_the_kind_it_was_created_as(test_client, org):
    conn = test_client.post("/api/connections", headers=org["headers"], json={
        "name": "Teams for analysis", "type": "teams_messages", "credentials": ENTRA_APP,
        "config": {}, "auth_policy": "user_required", "allowed_user_auth_modes": ["oauth"],
    }).json()
    ds = test_client.post("/api/data_sources", headers=org["headers"], json={
        "name": "Teams analysis agent", "type": "teams_messages", "connection_ids": [conn["id"]],
        "is_public": True, "kind": "agent",
    })
    assert ds.status_code == 200, ds.text
    assert _kind_of(test_client, org["headers"], ds.json()["id"])[0] == "agent"
    bad = test_client.post("/api/data_sources", headers=org["headers"], json={
        "name": "x", "type": "teams_messages", "connection_ids": [conn["id"]], "kind": "robot",
    })
    assert bad.status_code == 422
