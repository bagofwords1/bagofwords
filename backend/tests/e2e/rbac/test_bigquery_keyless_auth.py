"""Keyless BigQuery connections through the real API.

A connection may authenticate as the deployment's own Google identity
(Application Default Credentials) instead of a pasted service-account key.
These tests pin the contract around that:

* it exists only where the operator enabled it — hidden from the form and
  refused on every write path otherwise;
* no key is stored for it, and switching method leaves no trace of the other;
* it is the admin's to configure: a member can neither create such a
  connection nor reach the ambient identity through their own credentials.

Only Google is faked (the identity lookup and the BigQuery driver).
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.connection import Connection

TARGET = "reader@analytics-prod.iam.gserviceaccount.com"
CONFIG = {"project_id": "analytics-prod", "dataset": "sales"}
SERVICE_ACCOUNT_KEY = json.dumps({
    "type": "service_account",
    "project_id": "analytics-prod",
    "private_key_id": "k1",
    "client_email": "legacy@analytics-prod.iam.gserviceaccount.com",
    "client_id": "1",
    "token_uri": "https://oauth2.googleapis.com/token",
})


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


class _Rows:
    def result(self):
        return self

    def to_dataframe(self):
        import pandas as pd

        return pd.DataFrame([{
            "table_name": "orders", "column_name": "id", "data_type": "INT64",
            "column_description": None, "table_description": None,
        }])


class _FakeBigQuery:
    """Stands in for google.cloud.bigquery.Client; records the identity used."""

    identities: list = []

    def __init__(self, project=None, credentials=None, **_):
        self.project = project
        self.credentials = credentials

    def list_datasets(self):
        _FakeBigQuery.identities.append(self.credentials)
        return []

    def query(self, *_, **__):
        return _Rows()


@pytest.fixture
def google(monkeypatch):
    """A server with an ambient Google identity and a fake BigQuery."""
    import google.auth
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials

    from app.data_sources import google_default_credentials as gdc
    from app.data_sources.clients import bigquery_client as bq_module

    ambient = Credentials(token="ambient-token")
    monkeypatch.setattr(google.auth, "default", lambda scopes=None, **_: (ambient, "ambient-project"))
    monkeypatch.setattr(bq_module.bigquery, "Client", _FakeBigQuery)
    # The fixture key carries no private key; parsing it is not under test.
    monkeypatch.setattr(
        service_account.Credentials, "from_service_account_info",
        classmethod(lambda cls, info, **_: ("service-account-key", info["client_email"])),
    )
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    _FakeBigQuery.identities = []
    gdc.reset_cache()
    yield ambient
    gdc.reset_cache()


@pytest.fixture
def keyless(monkeypatch):
    from app.settings.config import settings

    def _set(enabled: bool):
        monkeypatch.setattr(settings.bow_config.security, "allow_google_default_credentials", enabled)

    return _set


async def _stored(connection_id):
    async with async_session_maker() as db:
        row = await db.get(Connection, connection_id)
        return json.loads(row.config), (row.decrypt_credentials() if row.credentials else {})


def _is_ambient(identity, ambient, target=None):
    from google.auth import impersonated_credentials

    if target:
        return isinstance(identity, impersonated_credentials.Credentials) and identity.service_account_email == target
    return identity is ambient


@pytest.mark.e2e
def test_keyless_method_is_offered_only_where_the_operator_enabled_it(
    test_client, bootstrap_admin, keyless,
):
    admin = bootstrap_admin()
    headers = _headers(admin["token"], admin["org_id"])

    keyless(False)
    off = test_client.get("/api/data_sources/bigquery/fields", params={"auth_policy": "system_only"}, headers=headers).json()
    assert "adc" not in off["auth"]["by_auth"]
    assert "adc" not in off["credentials_by_auth"]
    assert "service_account" in off["auth"]["by_auth"]

    keyless(True)
    on = test_client.get("/api/data_sources/bigquery/fields", params={"auth_policy": "system_only"}, headers=headers).json()
    assert on["auth"]["by_auth"]["adc"]["description"]
    fields = on["credentials_by_auth"]["adc"]["properties"]
    assert "credentials_json" not in fields
    assert "target_service_account" in fields
    # Never a per-user option: it is the server's identity, not a member's.
    per_user = test_client.get("/api/data_sources/bigquery/fields", params={"auth_policy": "user_required"}, headers=headers).json()
    assert "adc" not in per_user["auth"]["by_auth"]


@pytest.mark.e2e
@pytest.mark.parametrize("auth_policy", ["system_only", "user_required"])
def test_keyless_connection_is_refused_while_disabled_and_nothing_is_saved(
    test_client, bootstrap_admin, keyless, google, auth_policy,
):
    admin = bootstrap_admin()
    headers = _headers(admin["token"], admin["org_id"])
    keyless(False)

    name = f"bq-{uuid.uuid4().hex[:8]}"
    created = test_client.post("/api/connections", headers=headers, json={
        "name": name, "type": "bigquery", "auth_policy": auth_policy,
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True},
    })
    assert created.status_code == 400, created.text
    listed = test_client.get("/api/connections", headers=headers).json()
    assert all(c["name"] != name for c in listed)

    # The form's pre-save Test button reports the same refusal.
    tested = test_client.post("/api/connections/test-params", headers=headers, json={
        "name": name, "type": "bigquery", "auth_policy": auth_policy,
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True},
    })
    assert tested.status_code == 200, tested.text
    assert tested.json()["success"] is False
    assert _FakeBigQuery.identities == []


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, TARGET])
async def test_keyless_connection_stores_no_key_and_runs_as_the_server_identity(
    test_client, bootstrap_admin, keyless, google, test_connection_connectivity, target,
):
    admin = bootstrap_admin()
    headers = _headers(admin["token"], admin["org_id"])
    keyless(True)

    credentials = {"use_application_default_credentials": True}
    if target:
        credentials["target_service_account"] = target
    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"bq-{uuid.uuid4().hex[:8]}", "type": "bigquery",
        "config": {**CONFIG, "auth_type": "adc"}, "credentials": credentials,
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    config, stored = await _stored(connection_id)
    assert config["auth_type"] == "adc"
    assert stored.get("use_application_default_credentials") is True
    assert stored.get("target_service_account") == target
    assert "credentials_json" not in stored

    _FakeBigQuery.identities = []
    result = test_connection_connectivity(connection_id=connection_id, user_token=admin["token"], org_id=admin["org_id"])
    assert result["success"] is True, result
    assert _FakeBigQuery.identities and all(_is_ambient(i, google, target) for i in _FakeBigQuery.identities)


@pytest.mark.e2e
def test_a_target_that_is_not_a_service_account_is_refused_on_save(
    test_client, bootstrap_admin, keyless, google,
):
    admin = bootstrap_admin()
    keyless(True)
    created = test_client.post("/api/connections", headers=_headers(admin["token"], admin["org_id"]), json={
        "name": f"bq-{uuid.uuid4().hex[:8]}", "type": "bigquery",
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True, "target_service_account": "someone@example.com"},
    })
    assert created.status_code == 400, created.text
    assert _FakeBigQuery.identities == []


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_switching_method_leaves_no_trace_of_the_previous_one(
    test_client, bootstrap_admin, keyless, google, test_connection_connectivity,
):
    admin = bootstrap_admin()
    headers = _headers(admin["token"], admin["org_id"])
    keyless(True)

    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"bq-{uuid.uuid4().hex[:8]}", "type": "bigquery",
        "config": {**CONFIG, "auth_type": "service_account"},
        "credentials": {"credentials_json": SERVICE_ACCOUNT_KEY, "oauth_client_id": "signin-app"},
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    def _identity_used():
        _FakeBigQuery.identities = []
        result = test_connection_connectivity(connection_id=connection_id, user_token=admin["token"], org_id=admin["org_id"])
        assert result["success"] is True, result
        return _FakeBigQuery.identities[-1]

    assert _identity_used()[0] == "service-account-key"

    # Key -> keyless: the form sends only the new method's fields.
    to_keyless = test_client.put(f"/api/connections/{connection_id}", headers=headers, json={
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True, "target_service_account": TARGET},
    })
    assert to_keyless.status_code == 200, to_keyless.text
    _, stored = await _stored(connection_id)
    assert "credentials_json" not in stored
    assert stored["oauth_client_id"] == "signin-app"
    assert _is_ambient(_identity_used(), google, TARGET)

    # Keyless -> key: the marker must not survive, or the pasted key is ignored.
    to_key = test_client.put(f"/api/connections/{connection_id}", headers=headers, json={
        "config": {**CONFIG, "auth_type": "service_account"},
        "credentials": {"credentials_json": SERVICE_ACCOUNT_KEY},
    })
    assert to_key.status_code == 200, to_key.text
    _, stored = await _stored(connection_id)
    assert "use_application_default_credentials" not in stored
    assert "target_service_account" not in stored
    assert stored["oauth_client_id"] == "signin-app"
    assert _identity_used()[0] == "service-account-key"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_testing_a_method_switch_before_saving_tests_the_new_method(
    test_client, bootstrap_admin, keyless, google,
):
    """The edit form's Test button sends the new method on top of saved credentials."""
    admin = bootstrap_admin()
    headers = _headers(admin["token"], admin["org_id"])
    keyless(True)
    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"bq-{uuid.uuid4().hex[:8]}", "type": "bigquery",
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True},
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    _FakeBigQuery.identities = []
    tested = test_client.post(f"/api/connections/{connection_id}/test", headers=headers, json={
        "config": {**CONFIG, "auth_type": "service_account"},
        "credentials": {"credentials_json": SERVICE_ACCOUNT_KEY},
    })
    assert tested.status_code == 200, tested.text
    assert tested.json()["success"] is True, tested.json()
    assert _FakeBigQuery.identities[-1][0] == "service-account-key"
    # Testing changes nothing that is stored.
    config, stored = await _stored(connection_id)
    assert config["auth_type"] == "adc" and "credentials_json" not in stored


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_disabling_the_opt_in_stops_existing_keyless_connections_but_not_edits_elsewhere(
    test_client, bootstrap_admin, keyless, google,
):
    admin = bootstrap_admin()
    headers = _headers(admin["token"], admin["org_id"])
    keyless(True)
    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"bq-{uuid.uuid4().hex[:8]}", "type": "bigquery",
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True},
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    keyless(False)
    _FakeBigQuery.identities = []
    tested = test_client.post(f"/api/connections/{connection_id}/test", headers=headers)
    assert tested.status_code == 200, tested.text
    assert tested.json()["success"] is False
    assert _FakeBigQuery.identities == []

    # The connection can still be renamed, and moved off keyless auth.
    renamed = test_client.put(f"/api/connections/{connection_id}", headers=headers, json={"name": f"renamed-{uuid.uuid4().hex[:6]}"})
    assert renamed.status_code == 200, renamed.text
    moved = test_client.put(f"/api/connections/{connection_id}", headers=headers, json={
        "config": {**CONFIG, "auth_type": "service_account"},
        "credentials": {"credentials_json": SERVICE_ACCOUNT_KEY},
    })
    assert moved.status_code == 200, moved.text
    _, stored = await _stored(connection_id)
    assert "use_application_default_credentials" not in stored


@pytest.mark.e2e
def test_a_member_cannot_create_a_keyless_connection(
    test_client, bootstrap_admin, invite_user_to_org, keyless, google,
):
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    keyless(True)

    name = f"bq-{uuid.uuid4().hex[:8]}"
    created = test_client.post("/api/connections", headers=_headers(member["token"], admin["org_id"]), json={
        "name": name, "type": "bigquery",
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True},
    })
    assert created.status_code == 403, created.text
    assert _FakeBigQuery.identities == []
    listed = test_client.get("/api/connections", headers=_headers(admin["token"], admin["org_id"])).json()
    assert all(c["name"] != name for c in listed)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_the_impersonated_account_is_shown_to_admins_and_can_be_changed_or_removed(
    test_client, bootstrap_admin, keyless, google, get_connection, test_connection_connectivity,
):
    admin = bootstrap_admin()
    headers = _headers(admin["token"], admin["org_id"])
    keyless(True)
    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"bq-{uuid.uuid4().hex[:8]}", "type": "bigquery",
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True, "target_service_account": TARGET},
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    # The edit form is pre-filled from this: an identity, not a secret.
    detail = get_connection(connection_id=connection_id, user_token=admin["token"], org_id=admin["org_id"])
    assert detail["credentials_meta"]["target_service_account"] == TARGET

    def _save(credentials):
        response = test_client.put(f"/api/connections/{connection_id}", headers=headers, json={
            "config": {**CONFIG, "auth_type": "adc"}, "credentials": credentials,
        })
        assert response.status_code == 200, response.text

    # A blank value means "unchanged", as for every credential field.
    _save({"use_application_default_credentials": True, "target_service_account": ""})
    assert (await _stored(connection_id))[1].get("target_service_account") == TARGET

    other = "auditor@analytics-prod.iam.gserviceaccount.com"
    _save({"target_service_account": other})
    _, stored = await _stored(connection_id)
    assert stored == {"use_application_default_credentials": True, "target_service_account": other}

    # An explicit null removes it: queries then run as the server identity itself.
    _save({"target_service_account": None})
    _, stored = await _stored(connection_id)
    assert stored == {"use_application_default_credentials": True}
    _FakeBigQuery.identities = []
    result = test_connection_connectivity(connection_id=connection_id, user_token=admin["token"], org_id=admin["org_id"])
    assert result["success"] is True, result
    assert _is_ambient(_FakeBigQuery.identities[-1], google)


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["connection_save", "connection_test", "agent_save"])
async def test_removing_the_impersonated_account_needs_an_org_admin(
    test_client, bootstrap_admin, invite_user_to_org, grant_resource, keyless, google, via,
):
    """A null removes the account the server acts as, so queries run as the
    server's own identity: that is a change of who the server is, like setting
    another account, and a connection manager who is not an org admin may not
    make it on any path that sends credentials."""
    from tests.fixtures.connection_tool import _skip_network

    admin = bootstrap_admin()
    org_id = admin["org_id"]
    ah = _headers(admin["token"], org_id)
    keyless(True)
    created = test_client.post("/api/connections", headers=ah, json={
        "name": f"bq-{uuid.uuid4().hex[:8]}", "type": "bigquery",
        "config": {**CONFIG, "auth_type": "adc"},
        "credentials": {"use_application_default_credentials": True, "target_service_account": TARGET},
    })
    assert created.status_code == 200, created.text
    connection_id = created.json()["id"]

    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    granted = grant_resource(resource_type="connection", resource_id=connection_id, principal_type="user",
                             principal_id=manager["user_id"], permissions=["manage_connection"],
                             user_token=admin["token"], org_id=org_id)
    assert granted.status_code == 200, granted.text
    agent_id = None
    if via == "agent_save":
        with _skip_network():
            agent = test_client.post("/api/data_sources", headers=ah, json={
                "name": f"agent-{uuid.uuid4().hex[:6]}", "connection_ids": [connection_id],
                "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
            })
        assert agent.status_code == 200, agent.text
        agent_id = agent.json()["id"]
        granted = grant_resource(resource_type="data_source", resource_id=agent_id, principal_type="user",
                                 principal_id=manager["user_id"], permissions=["manage"],
                                 user_token=admin["token"], org_id=org_id)
        assert granted.status_code == 200, granted.text

    removal = {"target_service_account": None}

    def _send(headers):
        _FakeBigQuery.identities = []
        if via == "connection_save":
            return test_client.put(f"/api/connections/{connection_id}", headers=headers, json={"credentials": removal})
        if via == "connection_test":
            return test_client.post(f"/api/connections/{connection_id}/test", headers=headers, json={"credentials": removal})
        return test_client.put(f"/api/data_sources/{agent_id}", headers=headers, json={"credentials": removal})

    refused = _send(_headers(manager["token"], org_id))
    assert refused.status_code == 403, refused.text
    assert refused.json()["error_code"] == "connection.admin_required", refused.text
    assert _FakeBigQuery.identities == []
    assert (await _stored(connection_id))[1].get("target_service_account") == TARGET

    allowed = _send(ah)
    assert allowed.status_code == 200, allowed.text
    if via == "connection_test":
        assert allowed.json()["success"] is True, allowed.json()
        assert _FakeBigQuery.identities and all(_is_ambient(i, google) for i in _FakeBigQuery.identities)
        assert (await _stored(connection_id))[1].get("target_service_account") == TARGET
    else:
        assert "target_service_account" not in (await _stored(connection_id))[1]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_a_saved_key_is_never_returned_and_a_null_does_not_erase_other_connectors_fields(
    test_client, bootstrap_admin, keyless, google, get_connection, create_connection, update_connection,
):
    from pathlib import Path

    admin = bootstrap_admin()
    headers = _headers(admin["token"], admin["org_id"])
    keyless(True)
    created = test_client.post("/api/connections", headers=headers, json={
        "name": f"bq-{uuid.uuid4().hex[:8]}", "type": "bigquery",
        "config": {**CONFIG, "auth_type": "service_account"},
        "credentials": {"credentials_json": SERVICE_ACCOUNT_KEY},
    })
    assert created.status_code == 200, created.text
    detail = get_connection(connection_id=created.json()["id"], user_token=admin["token"], org_id=admin["org_id"])
    assert "credentials_json" not in (detail.get("credentials_meta") or {})
    assert "private_key_id" not in json.dumps(detail)

    # Null-clears are scoped to connectors that asked for them. SQLite is a
    # real local driver; credential storage is independent of the source type.
    saved = {"user": "reader", "password": "saved-password"}
    other = create_connection(
        name=f"sqlite-{uuid.uuid4().hex[:8]}", type="sqlite",
        config={"database": str(Path(__file__).resolve().parents[2] / "config" / "chinook.sqlite")},
        credentials=saved, user_token=admin["token"], org_id=admin["org_id"],
    )
    update_connection(connection_id=other["id"], payload={"credentials": {"user": None, "password": None}},
                      user_token=admin["token"], org_id=admin["org_id"])
    assert (await _stored(other["id"]))[1] == saved
