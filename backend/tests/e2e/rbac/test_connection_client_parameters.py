"""The destination check compares what a connector client actually receives.

A client is built from ``{**config, **credentials}`` and most clients use any
credential they are handed regardless of ``auth_type``. These tests run through
the routes as a non-admin ``manage_connection`` holder and sweep every connector
in the registry for the rules that do not depend on one connector's fields.
"""
import asyncio
import base64
import json
import uuid
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import update

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.schemas.data_source_registry import REGISTRY
from app.services.connection_destination import NON_DESTINATION_CONFIG_KEYS, secret_field_names
from tests.e2e.rbac.test_connection_destination_binding import (  # noqa: F401
    ADMIN_NEEDED, CREDENTIALS_NEEDED, OTHER_HOST, SAVED_HOST, STORED_SECRET, _hdr, _put, _refused,
    _saved, _test, driver, no_outbound_sockets, world,
)
from tests.fixtures.connection_tool import _skip_network

SECRET_JSON = json.dumps({
    "type": "service_account", "project_id": "p", "client_email": "svc@example.com",
    "private_key": "stored-secret", "token_uri": "https://oauth2.googleapis.com/token",
})
ADMIN_ONLY_VARIANTS = ["default", "aws_default", "arn", "managed_identity", "kerberos", "kerberos_delegated"]


def _sample(name, field, other=False):
    annotation = str(field.annotation)
    if "url" in name or "uri" in name:
        return "https://other.example.com" if other else "https://saved.example.com"
    if "bool" in annotation:
        default = field.default if isinstance(field.default, bool) else False
        return (not default) if other else default
    if "int" in annotation:
        return 9999 if other else 1000
    if "dict" in annotation.lower() or "list" in annotation.lower():
        return {"x": "other"} if other else {}
    if "host" in name or name == "account":
        return "other.example.com" if other else "saved.example.com"
    return "other-value" if other else "saved-value"


def _credential(conn_type, auth_type, name):
    if name in ("credentials_json", "service_account_json"):
        return SECRET_JSON
    return "stored-secret-marker" if name in secret_field_names(conn_type, auth_type) else "identifier-marker"


def _secret_variants(conn_type):
    return [a for a in REGISTRY[conn_type].credentials_auth.by_auth if secret_field_names(conn_type, a)]


def _destination_fields(conn_type):
    fields = REGISTRY[conn_type].config_schema.model_fields
    return [n for n in fields if n not in NON_DESTINATION_CONFIG_KEYS]


def _stored(test_client, w, conn_type, auth_type, extra_config=None, extra_credentials=None):
    entry = REGISTRY[conn_type]
    variant = entry.credentials_auth.by_auth[auth_type]
    config = {n: _sample(n, f) for n, f in entry.config_schema.model_fields.items() if f.is_required()}
    config["auth_type"] = auth_type
    config.update(extra_config or {})
    credentials = {
        n: _credential(conn_type, auth_type, n) for n, f in variant.schema.model_fields.items()
        if f.is_required() or n in secret_field_names(conn_type, auth_type)
    }
    credentials.update(extra_credentials or {})
    with _skip_network():
        created = test_client.post("/api/connections", headers=w["ah"], json={
            "name": f"{conn_type}-{uuid.uuid4().hex[:6]}", "type": conn_type, "config": config,
            "credentials": credentials, "auth_policy": "system_only",
        })
    assert created.status_code == 200, f"{conn_type}/{auth_type}: {created.text}"
    return w["grant"](created.json())


@pytest.fixture
def every_connector_allowed(monkeypatch):
    monkeypatch.setattr("app.ee.license.is_datasource_allowed", lambda _type: True)


# ── 1. Credential keys that decide the destination ───────────────────────

@pytest.mark.e2e
def test_every_connector_refuses_a_destination_field_sent_as_a_credential(test_client, world, every_connector_allowed):
    # One field per connector here; tests/unit sweeps every field without HTTP.
    failures = []
    for conn_type in sorted(REGISTRY):
        variants = _secret_variants(conn_type)
        if not variants:
            continue
        entry = REGISTRY[conn_type]
        conn = _stored(test_client, world, conn_type, variants[0])
        for name in _destination_fields(conn_type)[:1] + ["auth_type"]:
            field = entry.config_schema.model_fields.get(name)
            other_variants = [a for a in entry.credentials_auth.by_auth if a != variants[0]]
            value = _sample(name, field, other=True) if field is not None else (other_variants or ["other-auth"])[0]
            with _skip_network():
                r = _test(test_client, world, {"credentials": {name: value}}, conn_id=conn["id"])
            if r.status_code != 403:
                failures.append(f"{conn_type}.{name}: {r.status_code}")
    assert not failures, failures


@pytest.mark.e2e
@pytest.mark.parametrize("conn_type,auth_type,config,credential_override", [
    ("postgresql", "userpass", {"host": SAVED_HOST, "port": 5432, "database": "w"}, {"host": OTHER_HOST}),
    ("custom_api", "api_key", {"base_url": "https://api.example.com"}, {"base_url": "https://other.example.com"}),
    ("mcp", "bearer", {"server_url": "https://mcp.example.com/mcp"}, {"server_url": "https://other.example.com/mcp"}),
])
def test_destination_in_credentials_is_not_saved_without_the_secret(
    test_client, world, every_connector_allowed, conn_type, auth_type, config, credential_override,
):
    conn = _stored(test_client, world, conn_type, auth_type, extra_config=config)
    r = _put(test_client, world, {"credentials": credential_override}, conn_id=conn["id"])
    assert r.status_code == 403, r.text
    assert _saved(test_client, world, conn["id"])["config"].get(next(iter(credential_override))) == config.get(next(iter(credential_override)))


@pytest.mark.e2e
def test_a_row_saved_with_a_destination_credential_compares_where_the_client_goes(test_client, world, driver):
    conn = world["grant"](world["connection"](credentials={"user": "u", "password": STORED_SECRET, "host": "row.example.com"}))
    driver.clear()
    # The client goes to the credential's host, so editing config host does not move it.
    moved = _test(test_client, world, {"config": {"host": "unrelated.example.com"}}, conn_id=conn["id"])
    assert moved.status_code == 200, moved.text
    assert driver and all(d["host"] == "row.example.com" for d in driver)
    driver.clear()
    refused = _test(test_client, world, {"credentials": {"host": OTHER_HOST}}, conn_id=conn["id"])
    _refused(refused, ADMIN_NEEDED)
    assert driver == []


# ── 2. A variant without secrets still receives stored ones ──────────────

@pytest.mark.e2e
def test_every_connector_needs_an_admin_to_switch_to_no_auth_with_a_stored_secret(test_client, world, every_connector_allowed):
    failures = []
    for conn_type in sorted(REGISTRY):
        variants = _secret_variants(conn_type)
        if not variants or "none" not in REGISTRY[conn_type].credentials_auth.by_auth:
            continue
        conn = _stored(test_client, world, conn_type, variants[0])
        for action in ("test", "put"):
            with _skip_network():
                r = (_test(test_client, world, {"config": {"auth_type": "none"}}, conn_id=conn["id"]) if action == "test"
                     else _put(test_client, world, {"config": {**_saved(test_client, world, conn["id"])["config"], "auth_type": "none"}}, conn_id=conn["id"]))
            if r.status_code != 403 or r.json().get("error_code") != ADMIN_NEEDED:
                failures.append(f"{conn_type}/{action}: {r.status_code}")
    assert not failures, failures


@pytest.mark.e2e
def test_saved_no_auth_with_a_stored_secret_is_still_bound(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "custom_api", "none", extra_config={"base_url": "https://api.example.com"},
                   extra_credentials={"token": "stored-secret-marker"})
    r = _test(test_client, world, {"config": {"base_url": "https://other.example.com"}}, conn_id=conn["id"])
    _refused(r, ADMIN_NEEDED)


# ── 3. Stored secrets of other variants ─────────────────────────────────

@pytest.mark.e2e
def test_every_connector_refuses_a_variant_switch_that_leaves_a_stored_secret(test_client, world, every_connector_allowed):
    failures = []
    for conn_type in sorted(REGISTRY):
        variants = _secret_variants(conn_type)
        if len(variants) < 2:
            continue
        saved_auth, new_auth = variants[0], variants[1]
        if set(secret_field_names(conn_type, saved_auth)) <= set(secret_field_names(conn_type, new_auth)):
            continue  # entering the new variant's secrets replaces every stored one
        destination = _destination_fields(conn_type)[:1]
        if not destination:
            continue  # without a host field this would be a same-host switch
        field = REGISTRY[conn_type].config_schema.model_fields[destination[0]]
        conn = _stored(test_client, world, conn_type, saved_auth)
        junk = {n: "typed-value" for n in secret_field_names(conn_type, new_auth)}
        body = {"config": {"auth_type": new_auth, destination[0]: _sample(destination[0], field, other=True)}, "credentials": junk}
        with _skip_network():
            r = _test(test_client, world, body, conn_id=conn["id"])
        if r.status_code != 403:
            failures.append(f"{conn_type}: {saved_auth}->{new_auth}: {r.status_code}")
    assert not failures, failures


class _RecordingClient:
    received: list = []

    def __init__(self, **kwargs):
        _RecordingClient.received.append(kwargs)

    async def atest_connection(self):
        return {"success": True, "message": "ok", "connectivity": True}


@pytest.fixture
def recording_client(monkeypatch):
    _RecordingClient.received = []
    monkeypatch.setattr("app.services.connection_service.resolve_client_class", lambda _type: _RecordingClient)
    return _RecordingClient


PROM_URL = "https://prom.example.com"


@pytest.mark.e2e
def test_changing_the_auth_method_on_the_same_host_removes_the_old_secret(test_client, world, every_connector_allowed, recording_client):
    conn = _stored(test_client, world, "prometheus", "bearer", extra_config={"base_url": PROM_URL})
    with _skip_network():
        saved = _put(test_client, world, {
            "config": {"base_url": PROM_URL, "auth_type": "basic"},
            "credentials": {"username": "u", "password": "typed-pass"},
        }, conn_id=conn["id"])
    assert saved.status_code == 200, saved.text
    r = _test(test_client, world, {}, conn_id=conn["id"])
    assert r.status_code == 200, r.text
    assert recording_client.received, "the client was built"
    built = recording_client.received[-1]
    assert built["password"] == "typed-pass" and "token" not in built


@pytest.mark.e2e
def test_testing_a_same_host_auth_method_change_does_not_carry_the_old_secret(test_client, world, every_connector_allowed, recording_client):
    conn = _stored(test_client, world, "prometheus", "bearer", extra_config={"base_url": PROM_URL})
    r = _test(test_client, world, {
        "config": {"auth_type": "basic"}, "credentials": {"username": "u", "password": "typed-pass"},
    }, conn_id=conn["id"])
    assert r.status_code == 200, r.text
    built = recording_client.received[-1]
    assert built["password"] == "typed-pass" and "token" not in built


@pytest.mark.e2e
@pytest.mark.parametrize("config,credentials", [
    ({"auth_type": "none"}, {}),
    ({"auth_type": "basic"}, {"username": "u", "password": ""}),
    ({"auth_type": "basic"}, {"username": "u", "password": "typed-pass", "use_kerberos": True}),
], ids=["no-auth", "blank-secret", "extra-field"])
def test_a_same_host_auth_method_change_still_needs_the_full_secret(test_client, world, every_connector_allowed, config, credentials):
    conn = _stored(test_client, world, "prometheus", "bearer", extra_config={"base_url": PROM_URL})
    with _skip_network():
        r = _test(test_client, world, {"config": config, "credentials": credentials}, conn_id=conn["id"])
    assert r.status_code == 403, r.text


@pytest.mark.e2e
def test_a_same_host_change_to_a_server_identity_variant_needs_an_admin(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "MSSQL", "userpass", extra_config={"host": "sql.example.com", "port": 1433, "database": "d"})
    with _skip_network():
        r = _test(test_client, world, {"config": {"auth_type": "kerberos"}, "credentials": {"use_kerberos": True}}, conn_id=conn["id"])
    _refused(r, ADMIN_NEEDED)


# ── Declared fields that choose who the server acts as ───────────────────

IDENTITY_CREDENTIALS = [
    "role_arn", "kerberos_principal", "principal", "impersonated_user", "trusted_user", "user_directory", "user_id",
    "documentum_login", "tenant_id", "member_cid", "partition", "domain", "workspace_domain",
]


@pytest.mark.e2e
def test_changing_only_a_field_that_chooses_the_identity_is_refused(test_client, world, every_connector_allowed):
    failures, cases = [], 0
    for conn_type in sorted(REGISTRY):
        for auth_type, variant in REGISTRY[conn_type].credentials_auth.by_auth.items():
            fields = [f for f in IDENTITY_CREDENTIALS if f in variant.schema.model_fields]
            if not fields or auth_type in ("oauth", "otds_impersonation"):
                continue
            conn = _stored(test_client, world, conn_type, auth_type)
            for field in fields:
                cases += 1
                with _skip_network():
                    r = _test(test_client, world, {"credentials": {field: "other-identity"}}, conn_id=conn["id"])
                if r.status_code != 403:
                    failures.append(f"{conn_type}/{auth_type}.{field}: {r.status_code}")
    assert cases, "the registry declares identity fields"
    assert not failures, failures


@pytest.mark.e2e
def test_a_role_on_a_server_identity_variant_is_not_saved_by_a_non_admin(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "s3", "aws_role", extra_config={"bucket": "b"},
                   extra_credentials={"role_arn": "arn:aws:iam::1:role/saved"})
    with _skip_network():
        r = _put(test_client, world, {"credentials": {"role_arn": "arn:aws:iam::2:role/other"}}, conn_id=conn["id"])
    _refused(r, ADMIN_NEEDED)


@pytest.mark.e2e
def test_a_role_with_stored_keys_needs_the_keys_reentered(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "aws_athena", "key", extra_config={"region": "us-east-1", "database": "d"},
                   extra_credentials={"role_arn": "arn:aws:iam::1:role/saved"})
    with _skip_network():
        refused = _test(test_client, world, {"credentials": {"role_arn": "arn:aws:iam::2:role/other"}}, conn_id=conn["id"])
        _refused(refused, CREDENTIALS_NEEDED)
        ok = _test(test_client, world, {"credentials": {"role_arn": "arn:aws:iam::2:role/other", "access_key": "typed", "secret_key": "typed-secret"}}, conn_id=conn["id"])
    assert ok.status_code == 200, ok.text


@pytest.mark.e2e
@pytest.mark.parametrize("conn_type,auth_type,config,change", [
    ("snowflake", "userpass", {"account": "a", "warehouse": "w", "database": "d", "role": "saved_role"}, {"role": "other_role"}),
    ("prometheus", "bearer", {"base_url": PROM_URL, "org_id": "saved"}, {"org_id": "other"}),
    ("bigquery", "service_account", {"project_id": "saved"}, {"project_id": "other"}),
], ids=["role", "org", "project"])
def test_config_fields_that_choose_the_identity_or_tenant_are_refused(test_client, world, every_connector_allowed, conn_type, auth_type, config, change):
    conn = _stored(test_client, world, conn_type, auth_type, extra_config=config)
    with _skip_network():
        r = _test(test_client, world, {"config": change}, conn_id=conn["id"])
    assert r.status_code == 403, r.text


# ── Personal rows saved before these fields were filtered ────────────────

async def _store_old_personal_rows(connection_id, data_source_id, user_id, org_id, legacy):
    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.models.user_data_source_credentials import UserDataSourceCredentials

    payload = {"user": "member", "password": "member-pass", "host": "row.example.com"}
    async with async_session_maker() as db:
        if legacy:
            row = UserDataSourceCredentials(data_source_id=data_source_id, user_id=user_id, organization_id=org_id,
                                            auth_mode="userpass", is_active=True, is_primary=True)
        else:
            row = UserConnectionCredentials(connection_id=connection_id, user_id=user_id, organization_id=org_id,
                                            auth_mode="userpass", is_active=True, is_primary=True)
        row.encrypt_credentials(payload)
        db.add(row)
        await db.commit()


@pytest.mark.e2e
@pytest.mark.parametrize("legacy", [False, True], ids=["connection-row", "data-source-row"])
def test_an_older_personal_row_cannot_move_the_connection(test_client, world, driver, invite_user_to_org, legacy):
    with _skip_network():
        ds = test_client.post("/api/data_sources", headers=world["ah"], json={
            "name": f"agent-{uuid.uuid4().hex[:6]}", "connection_ids": [world["conn"]["id"]], "is_public": True,
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
        })
    assert ds.status_code == 200, ds.text
    ds_id = ds.json()["id"]
    member = invite_user_to_org(org_id=world["org_id"], admin_token=world["admin"]["token"])
    # Direct DB write: the routes no longer store undeclared fields, only older rows can hold them.
    asyncio.run(_store_old_personal_rows(world["conn"]["id"], ds_id, member["user_id"], world["org_id"], legacy))
    driver.clear()
    if legacy:
        code = "def generate_df(ds_clients, excel_files):\n    return list(ds_clients.values())[0].execute_query('SELECT 1 AS v')\n"
        r = test_client.post("/api/entities/preview", headers=_hdr(member, world["org_id"]), json={"code": code, "data_source_ids": [ds_id]})
    else:
        r = test_client.post(f"/api/connections/{world['conn']['id']}/test-my-credentials", headers=_hdr(member, world["org_id"]))
    assert r.status_code == 200, r.text
    assert driver, "the personal login reached the driver"
    assert all(d["host"] == SAVED_HOST for d in driver), driver


@pytest.mark.e2e
def test_changing_the_host_with_every_stored_secret_reentered_saves(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "prometheus", "bearer", extra_config={"base_url": "https://prom.example.com"})
    with _skip_network():
        r = _put(test_client, world, {
            "config": {"base_url": "https://other.example.com", "auth_type": "bearer"},
            "credentials": {"token": "typed-token"},
        }, conn_id=conn["id"])
    assert r.status_code == 200, r.text


# ── Fields that select the server's own identity ─────────────────────────

_FLAG_VALUE = {"role_arn": "arn:aws:iam::1:role/r"}


def _identity_flags(conn_type, auth_type):
    """Fields of the connector's server-identity variants the given variant does not declare."""
    by_auth = REGISTRY[conn_type].credentials_auth.by_auth
    declared = set(by_auth[auth_type].schema.model_fields)
    flags = set()
    for name, variant in by_auth.items():
        if name in ADMIN_ONLY_VARIANTS:
            flags.update(n for n in variant.schema.model_fields if n not in declared)
    return sorted(flags)


def _identity_sweep_cases():
    cases = []
    for conn_type in sorted(REGISTRY):
        variants = _secret_variants(conn_type)
        if not variants or not _destination_fields(conn_type):
            continue
        cases.extend((conn_type, variants[0], flag) for flag in _identity_flags(conn_type, variants[0]))
    return cases


@pytest.mark.e2e
@pytest.mark.parametrize("retained", [False, True], ids=["supplied", "retained"])
def test_every_server_identity_field_needs_an_admin_on_a_changed_destination(test_client, world, every_connector_allowed, retained):
    cases = _identity_sweep_cases()
    assert cases, "the registry has connectors with server-identity fields"
    failures = []
    for conn_type, auth_type, flag in cases:
        field = _destination_fields(conn_type)[0]
        model = REGISTRY[conn_type].config_schema.model_fields[field]
        flag_value = _FLAG_VALUE.get(flag, True)
        extra = {flag: flag_value} if retained else None
        conn = _stored(test_client, world, conn_type, auth_type, extra_credentials=extra)
        typed = {n: "typed-value" for n in secret_field_names(conn_type, auth_type)}
        credentials = typed if retained else {**typed, flag: flag_value}
        with _skip_network():
            r = _test(test_client, world, {"config": {field: _sample(field, model, other=True)}, "credentials": credentials}, conn_id=conn["id"])
        if r.status_code != 403 or r.json().get("error_code") != ADMIN_NEEDED:
            failures.append(f"{conn_type}.{flag}: {r.status_code}")
    assert not failures, failures


@pytest.mark.e2e
def test_saving_a_server_identity_field_needs_an_admin(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "aws_redshift", "userpass", extra_config={"host": "rs.example.com"})
    with _skip_network():
        r = _put(test_client, world, {
            "config": {"host": OTHER_HOST, "auth_type": "userpass"},
            "credentials": {"user": "u", "password": "typed-value", "role_arn": "arn:aws:iam::1:role/r"},
        }, conn_id=conn["id"])
    _refused(r, ADMIN_NEEDED)
    assert _saved(test_client, world, conn["id"])["config"]["host"] == "rs.example.com"


@pytest.mark.e2e
def test_server_identity_on_a_connection_with_nothing_stored_needs_an_admin(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "neo4j", "none", extra_config={"uri": "bolt://neo.example.com:7687"})
    with _skip_network():
        r = _test(test_client, world, {"config": {"uri": "bolt://other.example.com:7687"}, "credentials": {"use_kerberos": True}}, conn_id=conn["id"])
    _refused(r, ADMIN_NEEDED)


@pytest.mark.e2e
def test_switching_to_a_server_identity_variant_with_nothing_stored_needs_an_admin(test_client, world, every_connector_allowed):
    with _skip_network():
        created = test_client.post("/api/connections", headers=world["ah"], json={
            "name": f"mssql-{uuid.uuid4().hex[:6]}", "type": "MSSQL",
            "config": {"host": "sql.example.com", "port": 1433, "database": "d", "auth_type": "userpass"},
            "credentials": {"user": "u"}, "auth_policy": "system_only",
        })
    assert created.status_code == 200, created.text
    conn = world["grant"](created.json())
    with _skip_network():
        r = _test(test_client, world, {"config": {"host": OTHER_HOST, "auth_type": "kerberos"}, "credentials": {"use_kerberos": True}}, conn_id=conn["id"])
    _refused(r, ADMIN_NEEDED)


@pytest.mark.e2e
def test_a_server_identity_field_in_config_needs_an_admin(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "neo4j", "userpass", extra_config={"uri": "bolt://neo.example.com:7687"})
    with _skip_network():
        r = _test(test_client, world, {
            "config": {"uri": "bolt://other.example.com:7687", "use_kerberos": True},
            "credentials": {"password": "typed-value", "username": "u"},
        }, conn_id=conn["id"])
    _refused(r, ADMIN_NEEDED)


# ── Malformed values and client defaults ─────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("who", ["h", "ah"])
@pytest.mark.parametrize("body", [
    {"config": {"port": "²"}},
    {"config": {"auth_type": ["x"]}},
    {"credentials": {"auth_type": {"a": 1}}},
    {"config": {"port": "9" * 5000}},
], ids=["superscript-port", "list-auth-type", "dict-credential-auth-type", "huge-port"])
def test_malformed_values_do_not_fail_the_request(test_client, world, who, body):
    with _skip_network():
        r = _test(test_client, world, body, headers=world[who])
    assert r.status_code < 500, r.text


@pytest.mark.e2e
@pytest.mark.parametrize("port,refused", [(8080, True), (443, False)])
def test_a_row_without_a_port_compares_against_the_client_default(test_client, world, every_connector_allowed, port, refused):
    conn = _stored(test_client, world, "trino", "userpass", extra_config={"host": "trino.example.com", "catalog": "c"})
    with _skip_network():
        r = _test(test_client, world, {"config": {"port": port}}, conn_id=conn["id"])
    assert (r.status_code == 403) is refused, r.text


# ── Personal credentials hold only declared fields ───────────────────────

@pytest.fixture
def athena_for_members(test_client, world, every_connector_allowed, monkeypatch, create_data_source, invite_user_to_org):
    monkeypatch.setattr("app.ee.license.is_enterprise_licensed", lambda: True)
    with _skip_network():
        ds = create_data_source(
            name=f"athena-{uuid.uuid4().hex[:6]}", type="aws_athena",
            config={"region": "us-east-1", "database": "d", "s3_output_location": "s3://saved/out", "auth_type": "default"},
            credentials={}, auth_policy="user_required", user_token=world["admin"]["token"], org_id=world["org_id"],
        )
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=world["ah"]).status_code == 200
    member = invite_user_to_org(org_id=world["org_id"], admin_token=world["admin"]["token"])
    return {"ds_id": ds["id"], "h": _hdr(member, world["org_id"]), "user_id": member["user_id"]}


ATHENA_EXTRAS = {"region": "eu-west-3", "role_arn": "arn:aws:iam::1:role/r", "s3_output_location": "s3://other/out"}


def _stored_personal_login(ds_id, user_id):
    # Direct DB read: the API never returns the stored payload.
    from sqlalchemy import select
    from app.models.user_data_source_credentials import UserDataSourceCredentials

    async def _read():
        async with async_session_maker() as db:
            rows = await db.execute(select(UserDataSourceCredentials).where(
                UserDataSourceCredentials.data_source_id == ds_id, UserDataSourceCredentials.user_id == user_id,
            ))
            return rows.scalars().first().decrypt_credentials()
    return asyncio.run(_read())


@pytest.mark.e2e
def test_a_personal_login_stores_only_the_fields_its_variant_declares(test_client, athena_for_members):
    w = athena_for_members
    saved = test_client.post(f"/api/data_sources/{w['ds_id']}/my-credentials", headers=w["h"],
                             json={"auth_mode": "default", "credentials": ATHENA_EXTRAS})
    assert saved.status_code == 200, saved.text
    assert not set(ATHENA_EXTRAS) & set(_stored_personal_login(w["ds_id"], w["user_id"]))

    patched = test_client.patch(f"/api/data_sources/{w['ds_id']}/my-credentials", headers=w["h"],
                                json={"auth_mode": "default", "credentials": ATHENA_EXTRAS})
    assert patched.status_code == 200, patched.text
    assert not set(ATHENA_EXTRAS) & set(_stored_personal_login(w["ds_id"], w["user_id"]))


@pytest.mark.e2e
def test_a_personal_login_test_passes_only_declared_fields_to_the_client(test_client, athena_for_members, monkeypatch):
    w = athena_for_members
    received = {}

    class RecordingClient:
        def __init__(self, **kwargs):
            received.update(kwargs)

        async def atest_connection(self):
            return {"success": True}

    monkeypatch.setattr("app.services.user_data_source_credentials_service.resolve_client_class", lambda _type: RecordingClient)
    r = test_client.post(f"/api/data_sources/{w['ds_id']}/my-credentials/test", headers=w["h"],
                         json={"auth_mode": "default", "credentials": ATHENA_EXTRAS})
    assert r.status_code == 200, r.text
    assert received, "the client was built"
    assert received.get("region") == "us-east-1" and "role_arn" not in received
    assert received.get("s3_output_location") == "s3://saved/out"


# ── In-process engines must not expose stored keys to queries ────────────

DUCKDB_SECRET = "duckdb-stored-secret-marker"


@pytest.mark.e2e
@pytest.mark.parametrize("sql", [
    "SELECT current_setting('s3_secret_access_key') AS v",
    "SELECT secret_string AS v FROM duckdb_secrets()",
], ids=["current_setting", "duckdb_secrets"])
def test_a_query_cannot_read_a_stored_duckdb_key(test_client, world, grant_resource, tmp_path, sql):
    csv_file = tmp_path / "t.csv"
    csv_file.write_text("a\n1\n")
    with _skip_network():
        created = test_client.post("/api/data_sources", headers=world["ah"], json={
            "name": f"duck-{uuid.uuid4().hex[:6]}", "type": "duckdb",
            "config": {"uris": str(csv_file), "auth_type": "aws"},
            "credentials": {"access_key": "AKIDEXAMPLE", "secret_key": DUCKDB_SECRET},
            "auth_policy": "system_only", "generate_summary": False,
            "generate_conversation_starters": False, "generate_ai_rules": False,
        })
    assert created.status_code == 200, created.text
    ds_id = created.json()["id"]
    g = grant_resource(resource_type="data_source", resource_id=ds_id, principal_type="user",
                       principal_id=world["holder"]["user_id"], permissions=["manage"],
                       user_token=world["admin"]["token"], org_id=world["org_id"])
    assert g.status_code == 200, g.text
    code = (
        "def generate_df(ds_clients, excel_files):\n"
        f"    return list(ds_clients.values())[0].execute_query({sql!r})\n"
    )
    r = test_client.post("/api/entities/preview", headers=world["h"], json={"code": code, "data_source_ids": [ds_id]})
    assert r.status_code == 200, r.text
    assert DUCKDB_SECRET not in r.text


# ── 4. Safe-listed fields that a client can read as a location ───────────

@pytest.mark.e2e
@pytest.mark.parametrize("field,value", [
    ("repo", "https://other.example.com/o/n"),
    ("repo", "//other.example.com/o/n"),
    ("branch", "https://other.example.com/x"),
])
def test_scope_field_holding_a_url_counts_as_a_changed_destination(test_client, world, every_connector_allowed, field, value):
    conn = _stored(test_client, world, "github_repo", "token", extra_config={"repo": "owner/name"})
    r = _test(test_client, world, {"config": {field: value}}, conn_id=conn["id"])
    assert r.status_code == 403, r.text


@pytest.mark.e2e
def test_changing_a_scope_field_within_the_destination_stays_open(test_client, world, every_connector_allowed):
    conn = _stored(test_client, world, "github_repo", "token", extra_config={"repo": "owner/name"})
    with _skip_network():
        r = _test(test_client, world, {"config": {"repo": "owner/other", "branch": "main"}}, conn_id=conn["id"])
    assert r.status_code == 200, r.text


# ── 5. auth_policy values ────────────────────────────────────────────────

@pytest.fixture
def managed_agent(test_client, world, grant_resource):
    with _skip_network():
        r = test_client.post("/api/data_sources", headers=world["ah"], json={
            "name": f"agent-{uuid.uuid4().hex[:6]}", "connection_ids": [world["conn"]["id"]],
            "generate_summary": False, "generate_conversation_starters": False, "generate_ai_rules": False,
        })
    assert r.status_code == 200, r.text
    ds_id = r.json()["id"]
    g = grant_resource(resource_type="data_source", resource_id=ds_id, principal_type="user",
                       principal_id=world["holder"]["user_id"], permissions=["manage"],
                       user_token=world["admin"]["token"], org_id=world["org_id"])
    assert g.status_code == 200, g.text
    return ds_id


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["h", "ah"])
def test_auth_policy_outside_the_known_values_is_rejected(test_client, world, managed_agent, who):
    assert _put(test_client, world, {"auth_policy": "pending"}, headers=world[who]).status_code == 422
    r = test_client.put(f"/api/data_sources/{managed_agent}", json={"auth_policy": "pending"}, headers=world[who])
    assert r.status_code == 422, r.text
    assert _saved(test_client, world)["auth_policy"] == "user_required"


def _store_policy(connection_id, policy):
    # Direct DB write: the API no longer accepts this value, only older rows can hold it.
    async def _write():
        async with async_session_maker() as db:
            await db.execute(update(Connection).where(Connection.id == connection_id).values(auth_policy=policy))
            await db.commit()
    asyncio.run(_write())


@pytest.mark.e2e
@pytest.mark.parametrize("route", ["connection", "data_source"])
def test_an_existing_unknown_policy_row_needs_an_admin_to_land_on_system_only(test_client, world, managed_agent, route):
    _store_policy(world["conn"]["id"], "pending")
    url = f"/api/connections/{world['conn']['id']}" if route == "connection" else f"/api/data_sources/{managed_agent}"
    _refused(test_client.put(url, json={"auth_policy": "system_only"}, headers=world["h"]), ADMIN_NEEDED)
    ok = test_client.put(url, json={"auth_policy": "system_only"}, headers=world["ah"])
    assert ok.status_code == 200, ok.text
    assert _saved(test_client, world)["auth_policy"] == "system_only"


# ── 6. Server identity with nothing stored ───────────────────────────────

@pytest.mark.e2e
def test_every_server_identity_variant_needs_an_admin_for_a_changed_destination(test_client, world, every_connector_allowed):
    failures = []
    for conn_type in sorted(REGISTRY):
        for auth_type, variant in REGISTRY[conn_type].credentials_auth.by_auth.items():
            if auth_type not in ADMIN_ONLY_VARIANTS:
                continue
            conn = _stored(test_client, world, conn_type, auth_type)
            fields = _destination_fields(conn_type)
            if fields:
                field = REGISTRY[conn_type].config_schema.model_fields[fields[0]]
                body = {"config": {fields[0]: _sample(fields[0], field, other=True)}}
            else:
                body = {"config": {"auth_type": "service_principal"}}
            with _skip_network():
                r = _test(test_client, world, body, conn_id=conn["id"])
            if r.status_code != 403:
                failures.append(f"{conn_type}/{auth_type}: {r.status_code}")
    assert not failures, failures


# ── 7. Saved rows with no auth_type ──────────────────────────────────────

@pytest.mark.e2e
@pytest.mark.parametrize("conn_type,config,credentials", [
    ("custom_api", {"base_url": "https://api.example.com", "endpoints": []}, {"api_key": "stored-secret-marker", "api_key_header": "X-Api-Key"}),
    ("mcp", {"server_url": "https://mcp.example.com/mcp", "transport": "sse"}, {"token": "stored-secret-marker"}),
])
def test_form_save_of_a_row_without_an_auth_type_passes(test_client, world, conn_type, config, credentials):
    with _skip_network():
        created = test_client.post("/api/connections", headers=world["ah"], json={
            "name": f"{conn_type}-{uuid.uuid4().hex[:6]}", "type": conn_type, "config": config,
            "credentials": credentials, "auth_policy": "system_only",
        })
    assert created.status_code == 200, created.text
    conn = world["grant"](created.json())
    # The form omits auth_type when none was saved and the selector was untouched.
    with _skip_network():
        r = _put(test_client, world, {"name": "renamed", "config": config}, conn_id=conn["id"])
    assert r.status_code == 200, r.text


# ── 8. Personal login rows and the shared OAuth client ───────────────────

@pytest.mark.e2e
def test_a_personal_login_keeps_the_shared_oauth_client_on_its_saved_endpoint(
    test_client, world, monkeypatch, create_data_source, invite_user_to_org, every_connector_allowed,
):
    calls = []

    def recording_request(method, url, **_):
        calls.append(url)
        raise RuntimeError("stop before any network use")

    monkeypatch.setattr("app.data_sources.clients.documentum_client.pinned_request", recording_request)
    with _skip_network():
        ds = create_data_source(
            name=f"dctm-{uuid.uuid4().hex[:6]}", type="documentum",
            config={"rest_url": "https://dctm.example.com/rest", "repository": "r", "auth_type": "otds_client"},
            credentials={"otds_url": "https://otds.example.com", "client_id": "bow", "client_secret": "stored-client-secret"},
            auth_policy="user_required", user_token=world["admin"]["token"], org_id=world["org_id"],
        )
    assert test_client.put(f"/api/data_sources/{ds['id']}", json={"is_public": True}, headers=world["ah"]).status_code == 200
    connection_id = test_client.get(f"/api/data_sources/{ds['id']}/connections", headers=world["ah"]).json()[0]["id"]
    member = invite_user_to_org(org_id=world["org_id"], admin_token=world["admin"]["token"])
    h = _hdr(member, world["org_id"])
    saved = test_client.post(f"/api/data_sources/{ds['id']}/my-credentials", headers=h, json={
        "auth_mode": "otds_impersonation",
        "credentials": {"documentum_login": "bob", "otds_url": "https://other.example.com", "client_id": "other"},
    })
    assert saved.status_code == 200, saved.text
    test_client.post(f"/api/connections/{connection_id}/test-my-credentials", headers=h)
    assert calls, "the login is resolved through the shared client"
    assert not any("other.example.com" in url for url in calls), calls


# ── 9. Encoded echoes in a tool test preview ─────────────────────────────

ENCODED_SECRET = "s3cr3t+/key=value?"


@pytest.mark.e2e
@pytest.mark.parametrize("encoded", [
    base64.urlsafe_b64encode(ENCODED_SECRET.encode()).decode(),
    base64.urlsafe_b64encode(ENCODED_SECRET.encode()).decode().rstrip("="),
    ENCODED_SECRET.encode().hex(),
    quote(ENCODED_SECRET, safe=""),
], ids=["base64url", "base64url-unpadded", "hex", "percent"])
def test_tool_test_preview_removes_encoded_echoes(test_client, world, monkeypatch, encoded):
    monkeypatch.setattr("app.core.llm_transport.shared_transports",
                        lambda **_: (httpx.MockTransport(lambda request: httpx.Response(200, json={"echo": encoded})),))
    with _skip_network():
        created = test_client.post("/api/connections", headers=world["ah"], json={
            "name": f"api-{uuid.uuid4().hex[:6]}", "type": "custom_api",
            "config": {"base_url": "https://api.example.com", "auth_type": "bearer",
                       "endpoints": [{"name": "ping", "method": "GET", "path": "/ping"}]},
            "credentials": {"token": ENCODED_SECRET}, "auth_policy": "system_only",
        })
    assert created.status_code == 200, created.text
    conn = world["grant"](created.json())
    r = test_client.post("/api/connections/test-tool", headers=world["h"], json={
        "type": "custom_api", "config": {}, "credentials": {}, "tool_name": "ping", "arguments": {},
        "connection_id": conn["id"],
    })
    assert r.status_code == 200 and r.json()["success"] is True, r.text
    assert encoded not in r.text
