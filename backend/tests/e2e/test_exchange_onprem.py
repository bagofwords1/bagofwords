"""Real connection API/DB flow; only the Exchange HTTP server is simulated."""
import uuid

import pytest

from tests.unit.test_exchange_onprem_client import SUPPORT, URL, exchange  # noqa: F401  (fixture)

CONFIG = {"ews_url": URL, "shared_mailboxes": SUPPORT}


def _headers(token, org):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org)}


def _member(test_client, create_user, login_user, admin_headers, org):
    email = f"mail-member-{uuid.uuid4().hex[:8]}@example.com"
    invited = test_client.post(f"/api/organizations/{org}/members",
                               json={"organization_id": org, "email": email, "role": "member"}, headers=admin_headers)
    assert invited.status_code == 200, invited.json()
    member = create_user(email=email)
    return _headers(login_user(member["email"], member["password"]), org)


@pytest.mark.e2e
def test_connection_lifecycle_keeps_secrets_and_the_member_boundary(
    exchange, test_client, create_user, login_user, whoami,
    create_connection, get_connection, test_connection_connectivity,
):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    secret = "unique-windows-secret-never-returned"
    conn = create_connection(name="Exchange QA", type="exchange_onprem", config=CONFIG,
                             credentials={"username": "CORP\\reader", "password": secret},
                             user_token=token, org_id=org)
    assert conn["type"] == "exchange_onprem"
    assert secret not in str(get_connection(connection_id=conn["id"], user_token=token, org_id=org))

    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True
    assert secret not in str(probe)

    member_headers = _member(test_client, create_user, login_user, _headers(token, org), org)
    changed = test_client.put(f"/api/connections/{conn['id']}", json={"name": "Unauthorized change"},
                              headers=member_headers)
    assert changed.status_code == 403 and secret not in changed.text


@pytest.mark.e2e
def test_bad_input_is_refused_before_or_at_the_server(exchange, test_client, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    headers = _headers(token, whoami(token)["organizations"][0]["id"])
    good_credentials = {"username": "CORP\\reader", "password": "test-only"}

    def create(config, credentials):
        return test_client.post("/api/connections", headers=headers, json={
            "name": f"Exchange {uuid.uuid4().hex[:6]}", "type": "exchange_onprem",
            "config": config, "credentials": credentials, "auth_policy": "system_only"})

    assert create(CONFIG, {}).status_code in (400, 422)
    for bad in ({"ews_url": "http://mail.example.com"},                          # plain HTTP
                {"ews_url": URL, "shared_mailboxes": "not-an-address"},
                {"ews_url": URL, "include_own_mailbox": False}):                 # no mailbox at all
        before = exchange.state["posts"]
        response = create(bad, good_credentials)
        assert response.status_code in (400, 422), (bad, response.text)
        assert exchange.state["posts"] == before, "an invalid config must not reach the server"

    exchange.state["status"] = 401
    rejected = test_client.post("/api/connections/test-params", headers=headers, json={
        "name": "Bad credentials", "type": "exchange_onprem", "config": CONFIG,
        "credentials": {"username": "CORP\\wrong", "password": "incorrect-password"}})
    assert rejected.status_code == 200 and rejected.json()["success"] is False, rejected.json()
    assert "incorrect-password" not in rejected.text


@pytest.mark.e2e
def test_mailbox_is_browsed_live_as_messages(exchange, test_client, create_user, login_user, whoami, create_data_source):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = _headers(token, org)
    ds = create_data_source(name="Team mail", type="exchange_onprem", config=CONFIG,
                            credentials={"username": "CORP\\reader", "password": "system-secret"},
                            user_token=token, org_id=org)
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    listing = test_client.get(f"/api/data_sources/{ds['id']}/connections/{connections[0]['id']}/files", headers=headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    listed = {f["name"] for f in body["files"]}
    assert {m.subject for m in exchange.own.by_role("inbox").messages} <= listed
    assert "Apollo launch checklist" in listed, "mail filed into a subfolder is part of the mailbox"
    assert not listed & {m.subject for m in exchange.own.by_role("deleteditems").messages}
    # Worded as mail and marked live: nothing was indexed or stored.
    assert body.get("live") is True and (body.get("catalog_nouns") or [None, None])[1] == "messages"
    assert "system-secret" not in listing.text
    tables = test_client.get(f"/api/connections/{connections[0]['id']}/tables", headers=headers)
    assert tables.status_code == 200 and not (tables.json() or []), "a mailbox must not create catalog rows"


@pytest.mark.e2e
def test_user_required_connection_reads_as_the_member_not_the_system_identity(
    exchange, test_client, create_user, login_user, whoami, create_data_source,
):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    headers = _headers(token, org)
    ds = create_data_source(name="Personal mail", type="exchange_onprem", config=CONFIG,
                            credentials={"username": "CORP\\service", "password": "system-secret"},
                            auth_policy="user_required", user_token=token, org_id=org)
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=headers).status_code == 200
    member_headers = _member(test_client, create_user, login_user, headers, org)
    connection_id = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()[0]["id"]
    files = f"/api/data_sources/{ds['id']}/connections/{connection_id}/files"
    mine = f"/api/connections/{connection_id}/my-credentials"

    # Before signing in the member reads nothing — and nothing is sent as the service account.
    exchange.state["identities"].clear()
    blocked = test_client.get(files, headers=member_headers)
    assert blocked.status_code == 403 or blocked.json().get("connect_required") is True, blocked.json()
    assert not exchange.state["identities"]

    assert test_client.post(mine, json={"auth_mode": "kerberos", "credentials": {}}, headers=member_headers).status_code == 400
    assert test_client.post(mine, json={"auth_mode": "ntlm", "credentials": {}}, headers=member_headers).status_code == 422
    saved = test_client.post(mine, headers=member_headers, json={
        "auth_mode": "ntlm", "credentials": {"username": "CORP\\member", "password": "member-secret"}})
    assert saved.status_code == 200 and "member-secret" not in saved.text, saved.text

    exchange.state["identities"].clear()
    listed = test_client.get(files, headers=member_headers)
    assert listed.status_code == 200 and listed.json()["files"], listed.json()
    assert set(exchange.state["identities"]) == {"CORP\\member"}

    assert test_client.delete(mine, headers=member_headers).status_code == 200
    exchange.state["identities"].clear()
    again = test_client.get(files, headers=member_headers)
    assert again.status_code == 403 or again.json().get("connect_required") is True, again.json()
    assert not exchange.state["identities"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_agent_mail_tools_work_over_the_connection(exchange, monkeypatch):
    """The planner only reads tool observations: they must name the scope,
    carry every row, and let it open what it found."""
    from app.ai.tools.implementations import _file_tool_common
    from app.ai.tools.implementations.email_tools import ListEmailsTool, SearchEmailsTool
    from app.data_sources.clients.exchange_onprem_client import ExchangeOnpremClient

    client = ExchangeOnpremClient(ews_url=URL, username="CORP\\reader", password="pw", shared_mailboxes=SUPPORT)

    # Connection resolution needs a report; the subject here is the tool contract.
    async def resolve(runtime_ctx, connection_id, capability):
        assert capability in client.capabilities
        return client, None
    monkeypatch.setattr(_file_tool_common, "resolve_file_client", resolve)

    async def run(tool, **kw):
        events = [e async for e in tool.run_stream({"connection_id": "c1", **kw}, {})]
        return events[-1].payload

    folders = await run(ListEmailsTool(), list_folders=True)
    assert f"{SUPPORT}/Inbox/Escalations" in folders["observation"]["details"]
    assert "NOT reachable" not in folders["observation"]["summary"]

    # A mailbox this identity cannot open is named, never silently skipped.
    exchange.denied.add(SUPPORT)
    full, client = client, ExchangeOnpremClient(ews_url=URL, username="CORP\\reader", password="pw",
                                                shared_mailboxes=SUPPORT)
    narrowed = await run(ListEmailsTool(), list_folders=True)
    assert "NOT reachable" in narrowed["observation"]["summary"] and SUPPORT in narrowed["observation"]["summary"]
    refused_mailbox = await run(ListEmailsTool(), folder_id=SUPPORT)
    assert refused_mailbox["output"]["success"] is False
    assert "cannot be opened with this account" in refused_mailbox["output"]["error"]
    exchange.denied.clear()
    client = full

    listed = await run(ListEmailsTool(), unread_only=True)
    assert listed["output"]["success"] and "Complete" in listed["observation"]["summary"]
    assert [f["name"] for f in listed["output"]["files"]] == ["Quarterly revenue report", "Apollo launch checklist"]
    # The observation says what was covered and that another mailbox exists.
    assert "every folder" in listed["observation"]["summary"] and SUPPORT in listed["observation"]["summary"]

    inbox = await run(ListEmailsTool(), unread_only=True, folder_id="Inbox")
    assert "in Inbox" in inbox["observation"]["summary"]
    assert [f["name"] for f in inbox["output"]["files"]] == ["Quarterly revenue report"]

    found = await run(SearchEmailsTool(), query="revenue", max_results=1)
    assert found["output"]["truncated"] and found["output"]["next_cursor"] in found["observation"]["summary"]
    rest = await run(SearchEmailsTool(), query="revenue", cursor=found["output"]["next_cursor"])
    names = [f["name"] for f in found["output"]["files"] + rest["output"]["files"]]
    assert sorted(names) == sorted(["Quarterly revenue report", "Customer cannot log in", "Revenue forecast 2025"])

    missing = await run(ListEmailsTool(), folder_id="No Such Folder")
    assert missing["output"]["success"] is False and "Inbox/Projects/Apollo" in missing["output"]["error"]

    exchange.state["status"] = 401
    refused = await run(SearchEmailsTool(), query="anything else")
    assert refused["output"]["success"] is False
    assert "Do NOT retry" in refused["output"]["error"] and "pw" not in refused["output"]["error"].split()
