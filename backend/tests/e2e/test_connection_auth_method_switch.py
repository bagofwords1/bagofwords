"""Switching a connection's auth method through the real API.

Credential edits are merged over the saved blob, so a change of method used to
leave the previous method's fields stored next to the new ones. Clients that
pick their method from which fields are present then kept using the old one
(a leftover ``use_kerberos`` wins over a SQL login, a leftover Neo4j ``token``
or ``impersonated_user`` wins over a plain user/password).

The contract pinned here: after an admin switches method, the stored
credentials hold only what the chosen method owns, and the connection
authenticates with that method — on the save itself, on the Test button, and
whenever the client is built later.

Only the database drivers are faked: each client's ``test_connection`` records
how the client was set up instead of opening a socket.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.connection import Connection


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _mssql_identity(client):
    return {
        "method": "kerberos" if client.use_kerberos else "sql_login",
        "user": client.user,
    }


def _neo4j_identity(client):
    return {
        "method": client.auth_method,
        "user": client.username,
        "impersonated_user": client.impersonated_user,
    }


@pytest.fixture
def drivers(monkeypatch):
    """Record the identity each client would connect with."""
    from app.data_sources.clients.mssql_client import MSSQLClient
    from app.data_sources.clients.neo4j_client import Neo4jClient

    seen: list[dict] = []

    def recorder(describe):
        def test_connection(self):
            seen.append(describe(self))
            return {"success": True, "message": "ok"}
        return test_connection

    monkeypatch.setattr(MSSQLClient, "test_connection", recorder(_mssql_identity))
    monkeypatch.setattr(Neo4jClient, "test_connection", recorder(_neo4j_identity))
    # The save also lists tables (and indexes them); there is no server to read.
    from app.ai.prompt_formatters import Table

    one_table = [Table(name="orders", columns=[], pks=[], fks=[])]
    for client_class in (MSSQLClient, Neo4jClient):
        monkeypatch.setattr(client_class, "get_schemas", lambda self, *a, **k: one_table)
        if hasattr(client_class, "count_tables"):
            monkeypatch.setattr(client_class, "count_tables", lambda self, *a, **k: 1)
    return seen


MSSQL_CONFIG = {"host": "sql01.corp.example.com", "port": 1433, "database": "sales"}
NEO4J_CONFIG = {"uri": "neo4j://graph.corp.example.com:7687", "database": "neo4j"}

# (type, config, from-method, its credentials, to-method, credentials the
#  form sends for it, what the connection must authenticate as afterwards)
SWITCHES = [
    pytest.param(
        "MSSQL", MSSQL_CONFIG,
        "kerberos", {"use_kerberos": True, "kerberos_principal": "svc-bow@CORP.EXAMPLE.COM"},
        "userpass", {"user": "reader", "password": "s3cret"},
        {"method": "sql_login", "user": "reader"},
        id="mssql-kerberos-to-sql-login",
    ),
    pytest.param(
        "MSSQL", MSSQL_CONFIG,
        "userpass", {"user": "reader", "password": "s3cret"},
        "kerberos", {"use_kerberos": True},
        {"method": "kerberos", "user": None},
        id="mssql-sql-login-to-kerberos",
    ),
    pytest.param(
        "neo4j", NEO4J_CONFIG,
        "kerberos", {"use_kerberos": True},
        "userpass", {"username": "analyst", "password": "s3cret"},
        {"method": "basic", "user": "analyst", "impersonated_user": None},
        id="neo4j-kerberos-to-userpass",
    ),
    pytest.param(
        "neo4j", NEO4J_CONFIG,
        "bearer", {"token": "eyJ.oidc.token"},
        "userpass", {"username": "analyst", "password": "s3cret"},
        {"method": "basic", "user": "analyst", "impersonated_user": None},
        id="neo4j-bearer-to-userpass",
    ),
    pytest.param(
        "neo4j", NEO4J_CONFIG,
        "impersonation", {"username": "svc", "password": "s3cret", "impersonated_user": "alice"},
        "userpass", {"username": "analyst", "password": "other"},
        {"method": "basic", "user": "analyst", "impersonated_user": None},
        id="neo4j-impersonation-to-userpass",
    ),
    pytest.param(
        "neo4j", NEO4J_CONFIG,
        "userpass", {"username": "analyst", "password": "s3cret"},
        "none", {},
        {"method": "none", "user": None, "impersonated_user": None},
        id="neo4j-userpass-to-no-auth",
    ),
]


async def _stored(connection_id):
    async with async_session_maker() as db:
        row = await db.get(Connection, connection_id)
        return json.loads(row.config), (row.decrypt_credentials() if row.credentials else {})


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("ds_type,config,old,old_creds,new,new_creds,expected", SWITCHES)
async def test_switching_auth_method_authenticates_with_the_new_method_only(
    test_client, create_user, login_user, whoami, test_connection_connectivity, drivers,
    ds_type, config, old, old_creds, new, new_creds, expected,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = _headers(token, org_id)

    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"{ds_type}-{uuid.uuid4().hex[:8]}", "type": ds_type,
        "config": {**config, "auth_type": old}, "credentials": old_creds,
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    drivers.clear()
    updated = test_client.put(f"/api/connections/{connection_id}", headers=headers, json={
        "config": {**config, "auth_type": new}, "credentials": new_creds,
    })
    assert updated.status_code == 200, updated.text
    # The save re-tests the connection with what it is about to store.
    assert drivers and drivers[-1] == expected

    stored_config, stored = await _stored(connection_id)
    assert stored_config["auth_type"] == new
    leftovers = set(old_creds) - set(new_creds)
    assert not leftovers & stored.keys(), f"previous method's fields still stored: {leftovers & stored.keys()}"

    drivers.clear()
    result = test_connection_connectivity(connection_id=connection_id, user_token=token, org_id=org_id)
    assert result["success"] is True, result
    assert drivers and all(seen == expected for seen in drivers)


@pytest.fixture
def built_with(monkeypatch):
    """Record the credentials each client is constructed with (its driver is
    never reached: the test and the table listing are answered here)."""
    import functools

    from app.ai.prompt_formatters import Table
    from app.services.connection_service import resolve_client_class

    built: list[dict] = []
    one_table = [Table(name="orders", columns=[], pks=[], fks=[])]

    def fake(ds_type):
        client_class = resolve_client_class(ds_type)
        original_init = client_class.__init__

        @functools.wraps(original_init)
        def recording_init(self, *args, **kwargs):
            built.append(kwargs)
            original_init(self, *args, **kwargs)

        monkeypatch.setattr(client_class, "__init__", recording_init)
        monkeypatch.setattr(client_class, "test_connection", lambda self: {"success": True, "message": "ok"})
        monkeypatch.setattr(client_class, "get_schemas", lambda self, *a, **k: one_table)
        if hasattr(client_class, "count_tables"):
            monkeypatch.setattr(client_class, "count_tables", lambda self, *a, **k: 1)
        return built

    return fake


# Methods whose clients pick a branch by which fields are present, so a field
# left over from the previous method would win over the new one.
FIELD_PRECEDENCE_SWITCHES = [
    pytest.param("trino", {"host": "trino.example.com", "catalog": "hive"},
                 "token", {"user": "svc", "access_token": "jwt-1"},
                 "userpass", {"user": "analyst", "password": "s3cret"}, id="trino-token-to-password"),
    pytest.param("snowflake", {"account": "acme-xy123", "warehouse": "WH", "database": "SALES"},
                 "keypair", {"user": "svc", "private_key_pem": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"},
                 "userpass", {"user": "analyst", "password": "s3cret"}, id="snowflake-keypair-to-password"),
    pytest.param("elasticsearch", {"host": "search.example.com"},
                 "apikey", {"api_key": "a2V5"},
                 "userpass", {"user": "analyst", "password": "s3cret"}, id="elasticsearch-apikey-to-password"),
    pytest.param("aws_redshift", {"host": "wh.abc.us-east-1.redshift.amazonaws.com", "port": 5439, "database": "dev", "schema": "public"},
                 "iam", {"user": "svc", "access_key": "AKIAEXAMPLE", "secret_key": "secret"},
                 "userpass", {"user": "analyst", "password": "s3cret"}, id="redshift-iam-to-password"),
    pytest.param("druid", {"host": "druid.example.com"},
                 "basic_token", {"basic_token": "dXNlcjpwYXNz"},
                 "token", {"token": "jwt-2"}, id="druid-basic-token-to-jwt"),
]


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("ds_type,config,old,old_creds,new,new_creds", FIELD_PRECEDENCE_SWITCHES)
async def test_the_client_is_given_only_the_chosen_methods_credentials(
    test_client, create_user, login_user, whoami, test_connection_connectivity, built_with,
    ds_type, config, old, old_creds, new, new_creds,
):
    built = built_with(ds_type)
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = _headers(token, org_id)

    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"{ds_type}-{uuid.uuid4().hex[:8]}", "type": ds_type,
        "config": {**config, "auth_type": old}, "credentials": old_creds,
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    updated = test_client.put(f"/api/connections/{connection_id}", headers=headers, json={
        "config": {**config, "auth_type": new}, "credentials": new_creds,
    })
    assert updated.status_code == 200, updated.text

    _, stored = await _stored(connection_id)
    leftovers = set(old_creds) - set(new_creds)
    assert leftovers and not leftovers & stored.keys()
    assert {k: stored[k] for k in new_creds} == new_creds

    built.clear()
    result = test_connection_connectivity(connection_id=connection_id, user_token=token, org_id=org_id)
    assert result["success"] is True, result
    # Indexing started by the original save may still be finishing in the
    # background with the credentials of that time; judge the clients built
    # for the new method.
    after_switch = [kw for kw in built if all(kw.get(k) == v for k, v in new_creds.items())]
    assert after_switch
    for kwargs in after_switch:
        assert not leftovers & {k for k, v in kwargs.items() if v not in (None, "")}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_a_connection_without_a_saved_method_keeps_its_credentials(
    test_client, create_user, login_user, whoami, test_connection_connectivity, drivers,
):
    """Connections created before the method picker (or through the API
    without one) carry no auth_type. Their fields are what they connect with;
    they must not be read as the registry's default method and stripped."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = _headers(token, org_id)

    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"neo4j-{uuid.uuid4().hex[:8]}", "type": "neo4j",
        "config": dict(NEO4J_CONFIG), "credentials": {"token": "eyJ.oidc.token"},
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]
    renamed = test_client.put(f"/api/connections/{connection_id}", headers=headers, json={
        "name": f"renamed-{uuid.uuid4().hex[:6]}", "config": dict(NEO4J_CONFIG),
    })
    assert renamed.status_code == 200, renamed.text

    _, stored = await _stored(connection_id)
    assert stored.get("token") == "eyJ.oidc.token"
    drivers.clear()
    result = test_connection_connectivity(connection_id=connection_id, user_token=token, org_id=org_id)
    assert result["success"] is True, result
    assert drivers and all(seen["method"] == "bearer" for seen in drivers)
