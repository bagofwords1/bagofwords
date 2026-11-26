"""The connection form's save and test check a Postgres schema setting.

Contract, through the API: creating a `postgresql` connection, updating its
config and testing new parameters (`/api/connections/test-params`) refuse a
schema setting the query-time rule refuses (`checked_search_path`) with a
400 `connection.config_invalid` that names the setting but not its value,
before anything connects. An accepted setting reaches the connection probe.
A `schema` in the submitted credentials is refused the same way. Testing a
saved connection with the edit form's values (`/api/connections/{id}/test`
with overrides) checks the setting only when the overrides change it.

Only the network probe is faked (`PostgresqlClient.test_connection`).
"""

import pytest

from app.data_sources.clients.postgresql_client import PostgresqlClient

BS = chr(92)
PROBE_FAILED = "probe-did-not-connect"


@pytest.fixture
def probes(monkeypatch):
    calls = []

    def probe(self):
        calls.append(self.schema)
        return {"success": False, "message": PROBE_FAILED}

    monkeypatch.setattr(PostgresqlClient, "test_connection", probe)
    return calls


@pytest.fixture
def admin(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _payload(schema):
    return {
        "name": "Warehouse", "type": "postgresql",
        "config": {"host": "db.invalid", "port": 5432, "database": "d", "schema": schema},
        "credentials": {"user": "u", "password": "p"},
        "auth_policy": "system_only",
    }


def _assert_refused(resp, schema):
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body.get("error_code") == "connection.config_invalid", body
    assert "schema" in resp.text
    assert schema not in resp.text


@pytest.mark.e2e
@pytest.mark.parametrize("schema", ["rvw_a;", "rvw_a --x", "a" + BS + "b", "a%b"])
def test_testing_new_params_refuses_a_refused_schema_setting(test_client, admin, probes, schema):
    resp = test_client.post("/api/connections/test-params", json=_payload(schema), headers=admin)
    _assert_refused(resp, schema)
    assert probes == []


@pytest.mark.e2e
def test_testing_new_params_probes_an_accepted_schema_setting(test_client, admin, probes):
    resp = test_client.post("/api/connections/test-params", json=_payload('rvw_a, "Sales"'), headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is False
    assert probes == ['rvw_a, "Sales"']


@pytest.mark.e2e
@pytest.mark.parametrize("schema", ["rvw_a;", "1.5"])
def test_creating_a_connection_refuses_a_refused_schema_setting(test_client, admin, probes, schema):
    resp = test_client.post("/api/connections", json=_payload(schema), headers=admin)
    _assert_refused(resp, schema)
    assert probes == []


CREDENTIALS_REFUSAL = "schema: the schema setting belongs in the connection settings, not the credentials"


def _assert_credentials_refused(resp):
    assert resp.status_code == 400, resp.text
    assert resp.json().get("error_code") == "connection.config_invalid", resp.text
    assert CREDENTIALS_REFUSAL in resp.text


@pytest.mark.e2e
def test_testing_new_params_refuses_a_schema_in_the_credentials(test_client, admin, probes):
    payload = _payload("public")
    payload["credentials"]["schema"] = "public"
    resp = test_client.post("/api/connections/test-params", json=payload, headers=admin)
    _assert_credentials_refused(resp)
    assert probes == []


# ------------------------------------------------------------------ the edit form's Test

def _saved_connection(admin, schema):
    """A Postgres connection in the admin's org saved with `schema`, written
    directly: the API refuses a refused setting now, and connections saved
    before the check must keep working."""
    import asyncio
    import json
    import uuid

    async def seed():
        from app.dependencies import async_session_maker
        from app.models.connection import Connection

        async with async_session_maker() as db:
            conn = Connection(organization_id=admin["X-Organization-Id"], name=f"pg-{uuid.uuid4().hex[:8]}",
                              type="postgresql", auth_policy="system_only",
                              config=json.dumps(_payload(schema)["config"]))
            conn.encrypt_credentials({"user": "u", "password": "p"})
            db.add(conn)
            await db.commit()
            return str(conn.id)

    return asyncio.run(seed())


@pytest.mark.e2e
def test_the_edit_forms_test_with_an_unchanged_saved_refused_setting_probes(test_client, admin, probes):
    conn_id = _saved_connection(admin, "a%b")
    overrides = {"config": _payload("a%b")["config"], "credentials": {"password": "rotated"}}
    resp = test_client.post(f"/api/connections/{conn_id}/test", json=overrides, headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is False
    assert probes == ["a%b"]


@pytest.mark.e2e
def test_the_edit_forms_test_with_an_edited_refused_setting_is_refused(test_client, admin, probes):
    conn_id = _saved_connection(admin, "a%b")
    resp = test_client.post(f"/api/connections/{conn_id}/test",
                            json={"config": _payload("rvw_a;")["config"]}, headers=admin)
    _assert_refused(resp, "rvw_a;")
    assert probes == []


@pytest.mark.e2e
def test_the_edit_forms_test_with_a_schema_in_the_credentials_is_refused(test_client, admin, probes):
    conn_id = _saved_connection(admin, "public")
    resp = test_client.post(f"/api/connections/{conn_id}/test",
                            json={"credentials": {"password": "p", "schema": "public"}}, headers=admin)
    _assert_credentials_refused(resp)
    assert probes == []
