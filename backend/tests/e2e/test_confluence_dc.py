"""Real connection API/DB flow and agent file tools for the Confluence Data Center connector.

Only the Confluence REST API is simulated (tools/confluence mock, an in-process
server on a real socket); the app is not patched:

    POST /api/connections | /api/data_sources (type confluence_dc)
      -> test / my-credentials (member login checked with Confluence first)
      -> GET .../connections/{id}/files (live, as the resolved identity)
      -> SchemaContextBuilder (planner note) and list_files / search_files / read_file run_stream
         -> resolve_file_client -> construct_client -> ConfluenceDcClient -> mock Confluence
"""
import asyncio
import html
import re
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from tests.unit.test_confluence_dc_client import conf, confluence_server  # noqa: F401

pytestmark = pytest.mark.e2e

_REJECTED = "connection.confluence_login_rejected"
_CAPTCHA = "connection.confluence_login_captcha"
# What each identity may see in the mock (pages + the blog post), all spaces.
_BOB = {"page:1001", "page:1002", "page:1003", "page:1005", "page:1006", "blogpost:2001", "page:1101", "page:1102"}
_ALICE = _BOB | {"page:1004", "page:1201", "page:1202"}
_SVC = _BOB | {"page:1004"}


def _token(conf, user):
    return next(t for t, u in conf["mock"].TOKENS.items() if u == user)


def _cfg(conf, auth_type="token", **extra):
    return {"base_url": conf["base_url"], "allow_http": True, "index_mode": "none", "auth_type": auth_type, **extra}


def _admin(create_user, login_user, whoami):
    admin = create_user()
    token = login_user(admin["email"], admin["password"])
    org = whoami(token)["organizations"][0]["id"]
    return token, org, {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def _member(test_client, create_user, login_user, org, admin_headers, login):
    email = f"conf-{login}-{uuid.uuid4().hex[:6]}@example.com"
    r = test_client.post(f"/api/organizations/{org}/members",
                         json={"organization_id": org, "email": email, "role": "member"}, headers=admin_headers)
    assert r.status_code == 200, r.json()
    user = create_user(email=email)
    token = login_user(user["email"], user["password"])
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": org}


def _user_required_source(conf, test_client, create_data_source, token, org, headers):
    """A public user_required Confluence data source with the service account's PAT as system credentials."""
    ds = create_data_source(name="Confluence per-user", type="confluence_dc", config=_cfg(conf),
                            credentials={"personal_access_token": _token(conf, "svc")},
                            auth_policy="user_required", user_token=token, org_id=org)
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=headers).status_code == 200
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    return ds["id"], connections[0]["id"]


def _file_ids(response):
    assert response.status_code == 200, response.json()
    body = response.json()
    assert not body.get("connect_required"), body
    return {f["id"] for f in body["files"]}


def _assert_connect_required(response):
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("connect_required") is True, body
    assert body.get("files", []) == [], body


def _users_seen(conf):
    return {user for user, _, _ in conf["mock"].REQUEST_LOG}


def _save(test_client, conn_id, headers, auth_mode, credentials):
    return test_client.post(f"/api/connections/{conn_id}/my-credentials",
                            json={"auth_mode": auth_mode, "credentials": credentials}, headers=headers)


# ----------------------------------------------------------------------------- connection lifecycle

def test_connection_lifecycle_and_secrets(
    conf, test_client, create_user, login_user, whoami, create_connection, get_connection,
    test_connection_connectivity,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    secret = _token(conf, "svc")
    conn = create_connection(name="Confluence QA", type="confluence_dc", config=_cfg(conf),
                             credentials={"personal_access_token": secret}, user_token=token, org_id=org)
    assert conn["type"] == "confluence_dc"
    assert secret not in str(get_connection(connection_id=conn["id"], user_token=token, org_id=org))
    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True, probe

    wrong = "wrong-conf-pass-e2e-41c7"
    conf["mock"].REQUEST_LOG.clear()
    r = test_client.post("/api/connections/test-params", headers=headers, json={
        "name": "Bad password", "type": "confluence_dc", "config": _cfg(conf, auth_type="userpass"),
        "credentials": {"username": "svc", "password": wrong}})
    assert r.status_code == 200 and r.json()["success"] is False, r.json()
    assert wrong not in r.text
    # The failure is Confluence refusing the login, not an egress or config error before it.
    assert conf["mock"].REQUEST_LOG, "the login was sent to Confluence"

    fields = test_client.get("/api/data_sources/confluence_dc/fields", params={"auth_policy": "user_required"},
                             headers=headers).json()
    assert set(fields["credentials_by_auth"]) == {"token", "userpass"}


def test_switching_auth_variant_uses_the_new_one(
    conf, test_client, create_user, login_user, whoami, create_connection, test_connection_connectivity,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    conn = create_connection(name="Confluence token", type="confluence_dc", config=_cfg(conf),
                             credentials={"personal_access_token": _token(conf, "alice")}, user_token=token, org_id=org)
    r = test_client.put(f"/api/connections/{conn['id']}", headers=headers, json={
        "config": _cfg(conf, auth_type="userpass"),
        "credentials": {"username": "svc", "password": conf["mock"].USERS["svc"]["password"]}})
    assert r.status_code == 200, r.json()
    conf["mock"].REQUEST_LOG.clear()
    probe = test_connection_connectivity(connection_id=conn["id"], user_token=token, org_id=org)
    assert probe["success"] is True, probe
    assert _users_seen(conf) == {"svc"}, "no call may run with the stale token"


# ----------------------------------------------------------------------------- per-user access

def test_user_required_members_read_as_themselves(
    conf, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(conf, test_client, create_data_source, token, org, headers)
    file_endpoint = f"/api/data_sources/{ds_id}/connections/{conn_id}/files"
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    alice_h = _member(test_client, create_user, login_user, org, headers, "alice")

    conf["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(file_endpoint, headers=bob_h))
    assert conf["mock"].REQUEST_LOG == [], "no Confluence call may run before the member connects"

    bob_pass = conf["mock"].USERS["bob"]["password"]
    r = _save(test_client, conn_id, bob_h, "userpass", {"username": "bob", "password": bob_pass})
    assert r.status_code == 200, r.json()
    assert r.json().get("verified") is True and r.json().get("identity") == "bob", r.json()
    assert bob_pass not in r.text
    alice_pat = _token(conf, "alice")
    r = _save(test_client, conn_id, alice_h, "token", {"personal_access_token": alice_pat})
    assert r.status_code == 200, r.json()
    assert alice_pat not in r.text

    conf["mock"].REQUEST_LOG.clear()
    assert _file_ids(test_client.get(file_endpoint, headers=bob_h)) == _BOB
    assert _users_seen(conf) == {"bob"}, "every call must run as bob"
    conf["mock"].REQUEST_LOG.clear()
    assert _file_ids(test_client.get(file_endpoint, headers=alice_h)) == _ALICE
    assert _users_seen(conf) == {"alice"}, "every call must run as alice"

    r = test_client.delete(f"/api/connections/{conn_id}/my-credentials", headers=bob_h)
    assert r.status_code in (200, 204), r.text
    conf["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(file_endpoint, headers=bob_h))
    assert conf["mock"].REQUEST_LOG == []


@pytest.mark.parametrize("auth_mode,credentials,secret", [
    ("userpass", {"username": "bob", "password": "wrong-conf-pass-save-5d1"}, "wrong-conf-pass-save-5d1"),
    ("token", {"personal_access_token": "NotAConfluencePat8c2e"}, "NotAConfluencePat8c2e"),
], ids=["userpass", "token"])
def test_a_login_confluence_rejects_is_refused_and_nothing_is_stored(
    conf, test_client, create_user, login_user, whoami, create_data_source, auth_mode, credentials, secret,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(conf, test_client, create_data_source, token, org, headers)
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    conf["mock"].REQUEST_LOG.clear()

    r = _save(test_client, conn_id, bob_h, auth_mode, credentials)

    assert r.status_code == 422, r.text
    assert r.json()["error_code"] == _REJECTED, r.json()
    assert secret not in r.text
    assert conf["mock"].REQUEST_LOG, "the login was checked with Confluence"
    conf["mock"].REQUEST_LOG.clear()
    _assert_connect_required(test_client.get(f"/api/data_sources/{ds_id}/connections/{conn_id}/files", headers=bob_h))
    assert conf["mock"].REQUEST_LOG == [], "a refused save leaves the member unconnected"


def test_a_captcha_locked_account_is_refused_with_its_own_code(
    conf, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(conf, test_client, create_data_source, token, org, headers)
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    conf["mock"].STATE["captcha_users"] = {"bob"}

    r = _save(test_client, conn_id, bob_h, "userpass", {"username": "bob", "password": conf["mock"].USERS["bob"]["password"]})

    assert r.status_code == 422 and r.json()["error_code"] == _CAPTCHA, r.text
    # The same member's PAT is not behind the CAPTCHA: it saves at once.
    assert _save(test_client, conn_id, bob_h, "token", {"personal_access_token": _token(conf, "bob")}).status_code == 200


def test_an_unreachable_confluence_still_saves_the_login_unchecked(
    conf, test_client, create_user, login_user, whoami, create_data_source,
):
    """Confluence being down is not the member's fault: the login is saved, unverified, as before."""
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(conf, test_client, create_data_source, token, org, headers)
    bob_h = _member(test_client, create_user, login_user, org, headers, "bob")
    conf["mock"].STATE["fail_status"] = {"/rest/api/user/current": 503}

    r = _save(test_client, conn_id, bob_h, "token", {"personal_access_token": _token(conf, "bob")})

    assert r.status_code == 200, r.text
    assert r.json().get("verified") is False


def test_the_admin_owner_runs_as_the_connection_identity(
    conf, test_client, create_user, login_user, whoami, create_data_source,
):
    token, org, headers = _admin(create_user, login_user, whoami)
    ds_id, conn_id = _user_required_source(conf, test_client, create_data_source, token, org, headers)
    conf["mock"].REQUEST_LOG.clear()
    assert _file_ids(test_client.get(f"/api/data_sources/{ds_id}/connections/{conn_id}/files", headers=headers)) == _SVC
    assert _users_seen(conf) == {"svc"}


# ----------------------------------------------------------------------------- agent

def _setup_agent(conf, test_client, create_user, login_user, whoami, create_data_source, create_report, **cfg):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org = whoami(token)["organizations"][0]["id"]
    ds = create_data_source(name="Engineering wiki", type="confluence_dc", config=_cfg(conf, **cfg),
                            credentials={"personal_access_token": _token(conf, "alice")},
                            auth_policy="system_only", user_token=token, org_id=org)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": org}
    connections = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=headers).json()
    assert [c["type"] for c in connections] == ["confluence_dc"], connections
    report = create_report(title="Confluence agent", user_token=token, org_id=org, data_sources=[ds["id"]])
    return ds["id"], connections[0]["id"], report["id"]


async def _loaded(db, ds_id, report_id):
    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    ds = (await db.execute(
        select(DataSource).options(selectinload(DataSource.connections)).where(DataSource.id == ds_id)
    )).scalar_one()
    org = await db.get(Organization, ds.organization_id)
    report = (await db.execute(
        select(Report).where(Report.id == report_id)
        .options(selectinload(Report.data_sources).selectinload(DataSource.connections))
    )).scalar_one()
    return ds, org, await db.get(User, report.user_id), report


def _agent(ds_id, conn_id, report_id, calls):
    """The planner context, then each (tool, input) run as the planner runs it; returns their outputs."""
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.ai.tools.implementations.list_files import ListFilesTool
    from app.ai.tools.implementations.read_file import ReadFileTool
    from app.ai.tools.implementations.search_files import SearchFilesTool
    from app.dependencies import async_session_maker

    tools = {"list": ListFilesTool, "search": SearchFilesTool, "read": ReadFileTool}

    async def run():
        async with async_session_maker() as db:
            ds, org, user, report = await _loaded(db, ds_id, report_id)
            built = await SchemaContextBuilder(db=db, data_sources=[ds], organization=org, report=None,
                                               user=user).build(with_stats=False)
            out = {"context": built.render() if built else ""}
            ctx = {"db": db, "organization": org, "user": user, "report": report}
            for name, (tool, tool_input) in calls.items():
                events = [e async for e in tools[tool]().run_stream({"connection_id": conn_id, **tool_input}, ctx)]
                out[name] = events[-1].payload["output"]
            return out

    return asyncio.run(run())


def test_agent_is_told_the_scope_then_searches_browses_and_reads_inside_it(
    conf, test_client, create_user, login_user, whoami, create_data_source, create_report,
):
    ds_id, conn_id, report_id = _setup_agent(conf, test_client, create_user, login_user, whoami,
                                             create_data_source, create_report, spaces="ENG")
    out = _agent(ds_id, conn_id, report_id, {
        "search": ("search", {"query": "failover"}),
        "list": ("list", {"folder_id": "ENG/Runbooks"}),
        "read": ("read", {"file_id": "page:1003"}),
        "outside": ("read", {"file_id": "page:1202"}),
    })

    element = re.search(r'<connection [^>]*id="' + re.escape(conn_id) + r'"[^>]*>(.*?)</connection>',
                        out["context"], re.S)
    assert element, out["context"]
    note = html.unescape(element.group(1))
    assert "limited to the spaces ENG" in note
    assert "page:<id>" in note

    assert out["search"]["success"] is True, out["search"]
    hits = {f["id"]: f for f in out["search"]["files"]}
    # alice may see SEC's Incident Response Plan, but this connection is limited to ENG.
    assert set(hits) == {"page:1003", "page:1006", "blogpost:2001"}, set(hits)

    assert out["list"]["success"] is True, out["list"]
    assert {f["id"] for f in out["list"]["files"]} == {"page:1003", "page:1004"}

    assert out["read"]["success"] is True, out["read"]
    assert "| 1 | Freeze writes on `orders-primary` | DBA |" in out["read"]["text"]

    assert out["outside"]["success"] is False, out["outside"]
    assert "outside the spaces" in out["outside"]["error"]


def test_agent_gets_a_fixed_reconnect_text_when_the_token_is_revoked(
    conf, test_client, create_user, login_user, whoami, create_data_source, create_report,
):
    ds_id, conn_id, report_id = _setup_agent(conf, test_client, create_user, login_user, whoami,
                                             create_data_source, create_report)
    conf["mock"].STATE["pats_disabled"] = True
    out = _agent(ds_id, conn_id, report_id, {"search": ("search", {"query": "failover"})})
    assert out["search"]["success"] is False, out["search"]
    assert "Do NOT retry" in out["search"]["error"]
    assert _token(conf, "alice") not in str(out["search"])
