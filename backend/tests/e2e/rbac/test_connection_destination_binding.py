"""A connection's stored credentials stay bound to the destination they were saved for.

Holding a per-connection ``manage_connection`` grant, with no org-level
``manage_connections`` or ``full_admin``, keeps every normal ability on the
connection (see ``test_connection_creator_role.py``). What it does not allow is
making the application use the stored login somewhere new. A changed
destination needs the secret re-entered in the same request, or an org admin.

PostgreSQL stands in for login/password connectors with only its engine stubbed;
Custom API requests go through a stubbed HTTP client. Every host is an
``example.com`` name and no test opens an outbound socket.
"""
import asyncio
import base64
import json
import socket
import uuid

import httpx
import pytest
import sqlalchemy
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool

from app.ee.audit.models import AuditLog
from app.dependencies import async_session_maker
from sqlalchemy import select
from tests.fixtures.connection_tool import _skip_network

STORED_USER = "stored_service_account"
STORED_SECRET = "stored-secret-value"
SAVED_HOST = "db.example.com"
OTHER_HOST = "other-db.example.com"
CREDENTIALS_NEEDED = "connection.credentials_required"
ADMIN_NEEDED = "connection.admin_required"


def _hdr(user, org_id):
    return {"Authorization": f"Bearer {user['token']}", "X-Organization-Id": str(org_id)}


@pytest.fixture(autouse=True)
def no_outbound_sockets(monkeypatch):
    real_connect = socket.socket.connect

    def connect(self, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise AssertionError(f"outbound connection attempted: {address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)


@pytest.fixture
def driver(monkeypatch):
    """What reached the PostgreSQL driver: one dict per engine built."""
    seen = []

    def engine_for(uri, **_):
        parsed = make_url(uri)
        seen.append({"host": parsed.host, "user": parsed.username, "password": parsed.password})
        return sqlalchemy.create_engine("sqlite://", poolclass=StaticPool)

    monkeypatch.setattr("app.data_sources.clients.postgresql_client.get_engine", engine_for)
    return seen


@pytest.fixture
def world(driver, test_client, bootstrap_admin, invite_user_to_org, create_connection, grant_resource):
    """A saved Postgres connection, an org admin, and a plain member holding only a grant on it."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]

    def connection(auth_policy="user_required", **overrides):
        spec = {
            "name": f"conn-{uuid.uuid4().hex[:8]}",
            "type": "postgresql",
            "config": {"host": SAVED_HOST, "port": 5432, "database": "warehouse", "sslmode": "require"},
            "credentials": {"user": STORED_USER, "password": STORED_SECRET},
            "auth_policy": auth_policy,
        }
        spec.update(overrides)
        # Saving a system_only connection validates it; the stub engine has no catalog to read.
        with _skip_network():
            return create_connection(**spec, user_token=admin["token"], org_id=org_id)

    holder = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    def grant(conn):
        r = grant_resource(resource_type="connection", resource_id=conn["id"], principal_type="user",
                           principal_id=holder["user_id"], permissions=["manage_connection"],
                           user_token=admin["token"], org_id=org_id)
        assert r.status_code == 200, r.text
        return conn

    conn = grant(connection())
    driver.clear()
    return {
        "admin": admin, "holder": holder, "org_id": org_id, "conn": conn,
        "h": _hdr(holder, org_id), "ah": _hdr(admin, org_id),
        "connection": connection, "grant": grant,
    }


def _saved(test_client, w, conn_id=None):
    r = test_client.get(f"/api/connections/{conn_id or w['conn']['id']}", headers=w["ah"])
    assert r.status_code == 200, r.text
    return r.json()


def _refused(response, code):
    assert response.status_code == 403, response.text
    assert response.json()["error_code"] == code, response.text


def _test(test_client, w, body=None, headers=None, conn_id=None):
    return test_client.post(f"/api/connections/{conn_id or w['conn']['id']}/test", json=body,
                            headers=headers or w["h"])


def _put(test_client, w, body, headers=None, conn_id=None):
    return test_client.put(f"/api/connections/{conn_id or w['conn']['id']}", json=body,
                           headers=headers or w["h"])


async def _store_member_login(connection_id, user_id, org_id):
    from app.models.user_connection_credentials import UserConnectionCredentials
    async with async_session_maker() as db:
        row = UserConnectionCredentials(
            connection_id=connection_id, user_id=user_id, organization_id=org_id,
            auth_mode="userpass", is_active=True, is_primary=True,
        )
        row.encrypt_credentials({"user": "member", "password": "member-pass"})
        db.add(row)
        await db.commit()


def _audit_rows(action):
    async def _load():
        async with async_session_maker() as db:
            rows = await db.execute(select(AuditLog).where(AuditLog.action == action))
            return [(r.details or {}) for r in rows.scalars().all()]
    return asyncio.run(_load())


# ── Test as saved stays open ─────────────────────────────────────────────

@pytest.mark.e2e
def test_grant_holder_can_test_the_saved_destination(test_client, world, driver):
    r = _test(test_client, world)
    assert r.status_code == 200 and r.json()["success"] is True, r.text
    assert [d["host"] for d in driver] == [SAVED_HOST]


# ── Changed destination on /test ─────────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("override", [
    {"host": OTHER_HOST},
    {"port": 6543},
    {"sslmode": "disable"},
    {"database": f"warehouse?host={OTHER_HOST}"},
])
def test_grant_holder_cannot_test_a_changed_destination_with_the_stored_login(test_client, world, driver, override):
    r = _test(test_client, world, {"config": override})
    _refused(r, CREDENTIALS_NEEDED)
    assert driver == []


@pytest.mark.e2e
@pytest.mark.parametrize("credentials", [
    None,
    {},
    {"user": "someone_else"},
    {"user": "someone_else", "password": ""},
    {"user": "someone_else", "password": "   "},
    {"password": ""},
])
def test_partial_reentry_does_not_unlock_a_changed_destination(test_client, world, driver, credentials):
    r = _test(test_client, world, {"config": {"host": OTHER_HOST}, "credentials": credentials})
    _refused(r, CREDENTIALS_NEEDED)
    assert driver == []


@pytest.mark.e2e
def test_changed_destination_with_the_secret_reentered_never_sends_the_stored_one(test_client, world, driver):
    r = _test(test_client, world, {"config": {"host": OTHER_HOST},
                                   "credentials": {"user": "typed_user", "password": "typed-secret"}})
    assert r.status_code == 200, r.text
    assert driver and all(d["host"] == OTHER_HOST and d["password"] == "typed-secret" for d in driver)


@pytest.mark.e2e
def test_org_admin_can_test_a_changed_destination(test_client, world, driver):
    r = _test(test_client, world, {"config": {"host": OTHER_HOST}}, headers=world["ah"])
    assert r.status_code == 200, r.text
    assert driver and driver[0]["host"] == OTHER_HOST


@pytest.mark.e2e
def test_org_connection_admin_can_test_a_changed_destination(test_client, world, driver, create_role, assign_role, invite_user_to_org):
    member = invite_user_to_org(org_id=world["org_id"], admin_token=world["admin"]["token"])
    role = create_role(name=f"mc-{uuid.uuid4().hex[:6]}", permissions=["manage_connections"],
                       user_token=world["admin"]["token"], org_id=world["org_id"])
    assert role.status_code == 200, role.text
    assign_role(role_id=role.json()["id"], principal_type="user", principal_id=member["user_id"],
                user_token=world["admin"]["token"], org_id=world["org_id"])
    r = _test(test_client, world, {"config": {"host": OTHER_HOST}}, headers=_hdr(member, world["org_id"]))
    assert r.status_code == 200, r.text
    assert driver and driver[0]["host"] == OTHER_HOST


@pytest.mark.e2e
@pytest.mark.parametrize("override", [
    {"host": SAVED_HOST.upper()},
    {"host": f"{SAVED_HOST}/"},
    {"port": "5432"},
    {"host": SAVED_HOST, "port": 5432, "database": "warehouse", "sslmode": "require", "schema": "", "index_routines": False},
])
def test_normalized_or_unchanged_form_values_are_not_a_change(test_client, world, driver, override):
    r = _test(test_client, world, {"config": override})
    assert r.status_code == 200, r.text
    assert driver and driver[0]["password"] == STORED_SECRET


# ── Saving ───────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_grant_holder_cannot_save_a_changed_host_without_the_secret(test_client, world, driver):
    r = _put(test_client, world, {"config": {"host": OTHER_HOST, "port": 5432, "database": "warehouse", "sslmode": "require"}})
    _refused(r, CREDENTIALS_NEEDED)
    assert _saved(test_client, world)["config"]["host"] == SAVED_HOST
    assert driver == []


@pytest.mark.e2e
def test_save_validation_never_runs_for_a_refused_system_only_change(test_client, world, driver):
    conn = world["grant"](world["connection"](auth_policy="system_only"))
    driver.clear()
    r = _put(test_client, world, {"config": {"host": OTHER_HOST, "port": 5432, "database": "warehouse"}}, conn_id=conn["id"])
    _refused(r, CREDENTIALS_NEEDED)
    assert driver == []
    assert _saved(test_client, world, conn["id"])["config"]["host"] == SAVED_HOST


@pytest.mark.e2e
def test_the_whole_form_as_loaded_saves_for_a_grant_holder(test_client, world, driver):
    """The edit form sends every config field back, including defaults and blanks."""
    saved = _saved(test_client, world)
    body = {
        "name": "renamed",
        "config": {**saved["config"], "auth_type": "userpass", "schema": "", "index_routines": False},
        "auth_policy": saved["auth_policy"],
    }
    r = _put(test_client, world, body)
    assert r.status_code == 200, r.text
    assert _saved(test_client, world)["name"] == "renamed"


@pytest.mark.e2e
def test_grant_holder_keeps_changing_non_destination_settings(test_client, world):
    r = _put(test_client, world, {"config": {"host": SAVED_HOST, "port": 5432, "database": "analytics", "schema": "reporting", "sslmode": "require"}})
    assert r.status_code == 200, r.text
    saved = _saved(test_client, world)["config"]
    assert saved["database"] == "analytics" and saved["schema"] == "reporting"


@pytest.mark.e2e
def test_grant_holder_can_save_a_new_host_with_the_secret_reentered(test_client, world, driver):
    driver.clear()
    r = _put(test_client, world, {"config": {"host": OTHER_HOST, "port": 5432, "database": "warehouse", "sslmode": "require"},
                                  "credentials": {"password": "typed-secret"}})
    assert r.status_code == 200, r.text
    assert _saved(test_client, world)["config"]["host"] == OTHER_HOST
    assert all(d["password"] != STORED_SECRET for d in driver)


@pytest.mark.e2e
def test_mixed_save_with_one_secret_of_two_is_refused(test_client, world, create_connection):
    conn = world["grant"](create_connection(
        name=f"ds-{uuid.uuid4().hex[:6]}", type="snowflake",
        config={"account": "acct", "warehouse": "wh", "database": "db", "auth_type": "keypair"},
        credentials={"user": "u", "private_key_pem": "stored-key", "private_key_passphrase": "stored-pass"},
        auth_policy="user_required", user_token=world["admin"]["token"], org_id=world["org_id"],
    ))
    r = _put(test_client, world, {"config": {"account": "other-acct", "warehouse": "wh", "database": "db", "auth_type": "keypair"},
                                  "credentials": {"private_key_pem": "typed-key"}}, conn_id=conn["id"])
    _refused(r, CREDENTIALS_NEEDED)
    assert r.json()["params"]["fields"] == ["private_key_passphrase"]


@pytest.mark.e2e
def test_org_admin_can_save_a_changed_host_without_the_secret(test_client, world):
    r = _put(test_client, world, {"config": {"host": OTHER_HOST, "port": 5432, "database": "warehouse", "sslmode": "require"}}, headers=world["ah"])
    assert r.status_code == 200, r.text
    assert _saved(test_client, world)["config"]["host"] == OTHER_HOST


@pytest.mark.e2e
def test_members_stored_logins_keep_a_new_host_with_admins(test_client, world, invite_user_to_org):
    """Other members' saved logins would follow the connection; re-entering the shared one cannot cover them."""
    member = invite_user_to_org(org_id=world["org_id"], admin_token=world["admin"]["token"])
    # Direct DB write: the personal-login route needs an agent membership that has nothing to do with this rule.
    asyncio.run(_store_member_login(world["conn"]["id"], member["user_id"], world["org_id"]))
    r = _put(test_client, world, {"config": {"host": OTHER_HOST, "port": 5432, "database": "warehouse", "sslmode": "require"},
                                  "credentials": {"password": "typed-secret"}})
    _refused(r, ADMIN_NEEDED)
    assert _saved(test_client, world)["config"]["host"] == SAVED_HOST


# ── What counts as a destination, per connector ──────────────────────────

CHANGED_DESTINATIONS = [
    ("mcp", {"server_url": "https://mcp.example.com/mcp", "auth_type": "bearer"}, {"token": "s3cr3t-token"},
     {"server_url": "https://other.example.com/mcp"}, None),
    ("custom_api", {"base_url": "https://api.example.com", "auth_type": "api_key", "endpoints": []}, {"api_key": "s3cr3t-key", "api_key_header": "X-Api-Key"},
     {"base_url": "https://other.example.com"}, None),
    ("custom_api", {"base_url": "https://api.example.com", "auth_type": "api_key", "endpoints": []}, {"api_key": "s3cr3t-key", "api_key_header": "X-Api-Key"},
     {"endpoints": [{"name": "x", "method": "GET", "path": ".other.example.com/x"}]}, None),
    ("custom_api", {"base_url": "https://api.example.com", "auth_type": "oauth_app"},
     {"authorize_url": "https://idp.example.com/a", "token_url": "https://idp.example.com/t", "client_id": "cid", "client_secret": "s3cr3t-client"},
     None, {"token_url": "https://other.example.com/t"}),
    ("netapp_ontap", {"url": "https://ontap.example.com", "allow_http": False}, {"username": "u", "password": "s3cr3t-pw"},
     {"url": "http://ontap.example.com", "allow_http": True}, None),
    ("remote_ssh", {"host": "ssh.example.com", "port": 22, "host_public_key": "ssh-ed25519 AAAA saved"}, {"username": "u", "password": "s3cr3t-pw"},
     {"host_public_key": "ssh-ed25519 AAAA other"}, None),
    ("snowflake", {"account": "acct", "warehouse": "w", "database": "d"}, {"user": "u", "password": "s3cr3t-pw"},
     {"account": "other-acct"}, None),
    ("servicenow", {"instance_url": "https://acme.service-now.example.com"}, {"username": "u", "password": "s3cr3t-pw"},
     {"instance_url": "https://other.service-now.example.com"}, None),
    ("documentum", {"rest_url": "https://dctm.example.com/rest", "repository": "r"}, {"username": "u", "password": "s3cr3t-pw"},
     {"rest_url": "https://other.example.com/rest"}, None),
    ("custom_api", {"base_url": "https://api.example.com", "auth_type": "bearer", "endpoints": []}, {"token": "s3cr3t-token"},
     {"auth_type": "api_key"}, None),
]


@pytest.mark.e2e
@pytest.mark.parametrize("conn_type,config,credentials,config_change,cred_change", CHANGED_DESTINATIONS)
def test_changed_destination_is_refused_without_the_secret(test_client, world, conn_type, config, credentials, config_change, cred_change):
    with _skip_network():
        created = test_client.post("/api/connections", headers=world["ah"], json={
            "name": f"{conn_type}-{uuid.uuid4().hex[:6]}", "type": conn_type, "config": config,
            "credentials": credentials, "auth_policy": "system_only",
        })
    assert created.status_code == 200, created.text
    conn = world["grant"](created.json())
    body = {}
    if config_change is not None:
        body["config"] = config_change
    if cred_change is not None:
        body["credentials"] = cred_change
    r = _test(test_client, world, body, conn_id=conn["id"])
    assert r.status_code == 403, r.text
    assert r.json()["error_code"] in (CREDENTIALS_NEEDED, ADMIN_NEEDED), r.text


@pytest.mark.e2e
def test_ambient_identity_destination_needs_an_admin(test_client, world):
    with _skip_network():
        created = test_client.post("/api/connections", headers=world["ah"], json={
            "name": f"s3-{uuid.uuid4().hex[:6]}", "type": "s3",
            "config": {"bucket": "b", "auth_type": "aws_default"}, "credentials": {}, "auth_policy": "system_only",
        })
    assert created.status_code == 200, created.text
    conn = world["grant"](created.json())
    r = _put(test_client, world, {"config": {"bucket": "b", "auth_type": "aws_default", "endpoint_url": "https://s3.other.example.com"}}, conn_id=conn["id"])
    _refused(r, ADMIN_NEEDED)


@pytest.mark.e2e
def test_credential_less_connections_are_not_gated(test_client, world):
    with _skip_network():
        created = test_client.post("/api/connections", headers=world["ah"], json={
            "name": f"sqlite-{uuid.uuid4().hex[:6]}", "type": "sqlite", "config": {"database": "/tmp/a.db"},
            "credentials": {}, "auth_policy": "system_only",
        })
        conn = world["grant"](created.json())
        r = _put(test_client, world, {"config": {"database": "/tmp/b.db"}}, conn_id=conn["id"])
    assert r.status_code == 200, r.text


@pytest.mark.e2e
def test_grant_holder_reindexes_the_saved_destination(test_client, world, driver):
    r = test_client.post(f"/api/connections/{world['conn']['id']}/reindex", headers=world["h"])
    assert r.status_code in (200, 202), r.text


# ── Per-user auth ────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_turning_per_user_auth_off_needs_an_admin(test_client, world):
    r = _put(test_client, world, {"auth_policy": "system_only"})
    _refused(r, ADMIN_NEEDED)
    assert _saved(test_client, world)["auth_policy"] == "user_required"

    ok = _put(test_client, world, {"auth_policy": "system_only"}, headers=world["ah"])
    assert ok.status_code == 200, ok.text
    assert _saved(test_client, world)["auth_policy"] == "system_only"


@pytest.mark.e2e
def test_turning_per_user_auth_on_stays_open(test_client, world, create_custom_api_connection):
    conn = world["grant"](create_custom_api_connection(user_token=world["admin"]["token"], org_id=world["org_id"]))
    r = _put(test_client, world, {"auth_policy": "user_required"}, conn_id=conn["id"])
    assert r.status_code == 200, r.text
    assert _saved(test_client, world, conn["id"])["auth_policy"] == "user_required"


# ── The data source route is the same door ───────────────────────────────

@pytest.fixture
def agent(test_client, world):
    with _skip_network():
        r = test_client.post("/api/data_sources", headers=world["ah"], json={
            "name": f"agent-{uuid.uuid4().hex[:6]}", "connection_ids": [world["conn"]["id"]],
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
        })
    assert r.status_code == 200, r.text
    return r.json()["id"]


@pytest.mark.e2e
def test_data_source_update_cannot_change_destination_or_auth_policy(test_client, world, driver, agent, grant_resource):
    r = grant_resource(resource_type="data_source", resource_id=agent, principal_type="user",
                       principal_id=world["holder"]["user_id"], permissions=["manage"],
                       user_token=world["admin"]["token"], org_id=world["org_id"])
    assert r.status_code == 200, r.text
    h = world["h"]
    moved = test_client.put(f"/api/data_sources/{agent}", headers=h, json={
        "config": {"host": OTHER_HOST, "port": 5432, "database": "warehouse", "sslmode": "require"},
    })
    _refused(moved, CREDENTIALS_NEEDED)
    assert driver == []
    assert _saved(test_client, world)["config"]["host"] == SAVED_HOST

    flipped = test_client.put(f"/api/data_sources/{agent}", headers=h, json={"auth_policy": "system_only"})
    _refused(flipped, ADMIN_NEEDED)
    assert _saved(test_client, world)["auth_policy"] == "user_required"


# ── test-tool ────────────────────────────────────────────────────────────

ECHO_SECRET = "s3cr3t-api-key-value"
ECHO_PASSWORD = "s3cr3t-basic-password"


@pytest.fixture
def http_calls(monkeypatch):
    """Requests the Custom API client made; the stub echoes the auth headers back."""
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append({"host": request.url.host, "headers": dict(request.headers)})
        return httpx.Response(200, json={"echo": dict(request.headers)})

    monkeypatch.setattr("app.core.llm_transport.shared_transports",
                        lambda **_: (httpx.MockTransport(respond),))
    return calls


@pytest.fixture
def custom_api(test_client, world, http_calls):
    def make(auth_type, credentials):
        with _skip_network():
            created = test_client.post("/api/connections", headers=world["ah"], json={
                "name": f"api-{uuid.uuid4().hex[:6]}", "type": "custom_api",
                "config": {"base_url": "https://api.example.com", "auth_type": auth_type,
                           "endpoints": [{"name": "ping", "method": "GET", "path": "/ping"}]},
                "credentials": credentials, "auth_policy": "system_only",
            })
        assert created.status_code == 200, created.text
        return world["grant"](created.json())
    return make


def _tool(test_client, w, conn_id, **body):
    payload = {"type": "custom_api", "config": {}, "credentials": {}, "tool_name": "ping", "arguments": {},
               "connection_id": conn_id}
    payload.update(body)
    return test_client.post("/api/connections/test-tool", json=payload, headers=w["h"])


@pytest.mark.e2e
def test_tool_test_refuses_a_type_other_than_the_saved_one(test_client, world, custom_api, http_calls):
    conn = custom_api("api_key", {"api_key": ECHO_SECRET, "api_key_header": "X-Api-Key"})
    r = _tool(test_client, world, conn["id"], type="mcp")
    assert r.status_code == 400, r.text
    assert http_calls == []


@pytest.mark.e2e
def test_tool_test_refuses_a_saved_connection_of_another_type(test_client, world, http_calls):
    r = _tool(test_client, world, world["conn"]["id"])
    assert r.status_code == 400, r.text
    assert http_calls == []


@pytest.mark.e2e
def test_tool_test_refuses_a_changed_destination_with_the_stored_key(test_client, world, custom_api, http_calls):
    conn = custom_api("api_key", {"api_key": ECHO_SECRET, "api_key_header": "X-Api-Key"})
    r = _tool(test_client, world, conn["id"], config={"base_url": "https://other.example.com"})
    _refused(r, CREDENTIALS_NEEDED)
    assert http_calls == []


@pytest.mark.e2e
def test_tool_test_refuses_a_path_that_leaves_the_saved_host(test_client, world, custom_api, http_calls):
    conn = custom_api("api_key", {"api_key": ECHO_SECRET, "api_key_header": "X-Api-Key"})
    r = _tool(test_client, world, conn["id"], config={
        "endpoints": [{"name": "ping", "method": "GET", "path": ".other.example.com/ping"}]})
    _refused(r, CREDENTIALS_NEEDED)
    assert http_calls == []


@pytest.mark.e2e
def test_tool_test_with_a_typed_secret_uses_only_that_secret(test_client, world, custom_api, http_calls):
    conn = custom_api("api_key", {"api_key": ECHO_SECRET, "api_key_header": "X-Api-Key"})
    r = _tool(test_client, world, conn["id"], config={"base_url": "https://other.example.com", "auth_type": "api_key", "endpoints": [{"name": "ping", "method": "GET", "path": "/ping"}]},
              credentials={"api_key": "typed-key", "api_key_header": "X-Api-Key"})
    assert r.status_code == 200, r.text
    assert all(ECHO_SECRET not in json.dumps(c["headers"]) for c in http_calls)


@pytest.mark.e2e
def test_tool_test_preview_never_returns_a_stored_secret(test_client, world, custom_api):
    conn = custom_api("api_key", {"api_key": ECHO_SECRET, "api_key_header": "X-Api-Key"})
    r = _tool(test_client, world, conn["id"])
    assert r.status_code == 200 and r.json()["success"] is True, r.text
    assert ECHO_SECRET not in r.text


@pytest.mark.e2e
def test_tool_test_preview_scrubs_a_basic_auth_header(test_client, world, custom_api):
    conn = custom_api("basic", {"username": "svc", "password": ECHO_PASSWORD})
    r = _tool(test_client, world, conn["id"])
    assert r.status_code == 200, r.text
    assert ECHO_PASSWORD not in r.text
    assert base64.b64encode(f"svc:{ECHO_PASSWORD}".encode()).decode() not in r.text


# ── Audit ────────────────────────────────────────────────────────────────

@pytest.mark.e2e
def test_probe_of_saved_destination_is_audited_without_secrets(test_client, world, driver):
    assert _test(test_client, world).status_code == 200
    rows = _audit_rows("connection.tested")
    assert rows, "the test route writes an audit event"
    assert any(SAVED_HOST in json.dumps(r) for r in rows)
    assert not any(STORED_SECRET in json.dumps(r) or STORED_USER in json.dumps(r) for r in rows)


@pytest.mark.e2e
def test_refused_probe_is_audited(test_client, world, driver):
    _refused(_test(test_client, world, {"config": {"host": OTHER_HOST}}), CREDENTIALS_NEEDED)
    rows = _audit_rows("connection.tested")
    assert any(OTHER_HOST in json.dumps(r) for r in rows)


@pytest.mark.e2e
def test_tool_test_is_audited_without_secrets(test_client, world, custom_api):
    conn = custom_api("api_key", {"api_key": ECHO_SECRET, "api_key_header": "X-Api-Key"})
    assert _tool(test_client, world, conn["id"]).status_code == 200
    rows = _audit_rows("connection.tool_tested")
    assert rows and not any(ECHO_SECRET in json.dumps(r) for r in rows)


@pytest.mark.e2e
def test_connection_edit_records_old_and_new_destination_and_auth_policy(test_client, world, driver):
    r = _put(test_client, world, {"config": {"host": OTHER_HOST, "port": 5432, "database": "warehouse", "sslmode": "require"},
                                  "credentials": {"password": "typed-secret"}})
    assert r.status_code == 200, r.text
    r2 = _put(test_client, world, {"auth_policy": "system_only"}, headers=world["ah"])
    assert r2.status_code == 200, r2.text
    rows = [d for d in _audit_rows("connection.updated") if "before" in d]
    host_row = next(d for d in rows if OTHER_HOST in json.dumps(d.get("after", {})))
    assert SAVED_HOST in json.dumps(host_row["before"])
    assert any(d["before"]["auth_policy"] == "user_required" and d["after"]["auth_policy"] == "system_only" for d in rows)
    assert not any("typed-secret" in json.dumps(d) or STORED_SECRET in json.dumps(d) for d in rows)
