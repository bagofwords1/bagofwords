"""A Postgres connection's schema setting is checked when it is saved.

Contract: every user-facing save of a `postgresql` connection's config
(create, update with a new config, the legacy data-source create and its
pre-save test; the `/test-params` route is in
tests/e2e/test_pg_schema_setting_at_save.py) applies the one schema rule the
read uses at query time (`postgresql_client.checked_search_path`):

* a schema item the rule refuses gives a 400 `connection.config_invalid`
  that names the setting (`schema`) but never its value;
* an accepted setting goes on to the connection probe;
* an update re-checks the setting only when it changes. The setting is the
  one the client gets (the credentials merged over the config), and the
  edit form always resends the full config, so a saved setting it leaves
  alone keeps working until someone edits it (the pre-release scan lists
  those);
* submitted connection-level credentials may not carry a `schema` (they
  would win over the config unchecked); on an update only a new or changed
  one is refused (the legacy data-source payload drops the key);
* other connection types are not checked.

Only the network probe is faked (`PostgresqlClient.test_connection`); the
services, models and the app database run for real.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.data_sources.clients.sqlite_client import SqliteClient
from app.errors import AppError, ErrorCode
from app.schemas.data_source_schema import DataSourceCreate
from app.services.connection_service import ConnectionService
from app.services.data_source_service import DataSourceService

REFUSED = ["rvw_a;", "rvw_a --x", "a" + chr(92) + "b", "a%b", "1.5", "public, x;y"]
ACCEPTED = ["public", "rvw_a, \"Sales\"", "'Sales'", "123", "\"$user\", public"]
PROBE_FAILED = "probe-did-not-connect"


def _run(coro):
    return asyncio.run(coro)


def _config(schema):
    config = {"host": "db.invalid", "port": 5432, "database": "d"}
    if schema is not None:
        config["schema"] = schema
    return config


CREDENTIALS = {"user": "u", "password": "p"}


@pytest.fixture
def probes(monkeypatch):
    """The network probe, faked: it records that it ran and fails to
    connect, as a server that is not there would."""
    calls = []

    def probe(self):
        calls.append(self.schema)
        return {"success": False, "message": PROBE_FAILED}

    monkeypatch.setattr(PostgresqlClient, "test_connection", probe)
    return calls


def _assert_schema_refusal(raised, value):
    error = raised.value
    assert isinstance(error, AppError)
    assert error.status_code == 400
    assert error.error_code == ErrorCode.CONNECTION_CONFIG_INVALID.value
    text = f"{error.message} {error.params}"
    assert "schema" in text
    assert value not in text
    # The refused item is not named either.
    assert all(item.strip() not in text for item in value.split(",") if item.strip() != "public")


# ------------------------------------------------------------------ create

@pytest.mark.parametrize("schema", REFUSED)
def test_create_refuses_a_schema_setting_the_rule_refuses_before_any_probe(schema, probes):
    with pytest.raises(AppError) as raised:
        _run(ConnectionService().create_connection(
            db=None, organization=None, current_user=None, name="pg", type="postgresql",
            config=_config(schema), credentials=CREDENTIALS))
    _assert_schema_refusal(raised, schema)
    assert probes == []


@pytest.mark.parametrize("schema", ACCEPTED + [None, ""])
def test_create_lets_an_accepted_schema_setting_through_to_the_probe(schema, probes):
    with pytest.raises(HTTPException) as raised:
        _run(ConnectionService().create_connection(
            db=None, organization=None, current_user=None, name="pg", type="postgresql",
            config=_config(schema), credentials=CREDENTIALS))
    assert raised.value.status_code == 400
    assert PROBE_FAILED in str(raised.value.detail)
    assert len(probes) == 1


def test_other_connection_types_are_not_checked(monkeypatch):
    calls = []

    def probe(self):
        calls.append(True)
        return {"success": False, "message": PROBE_FAILED}

    monkeypatch.setattr(SqliteClient, "test_connection", probe)
    result = _run(ConnectionService().test_connection_params(
        data_source_type="sqlite", config={"database": ":memory:", "schema": "rvw_a;"}, credentials={}))
    assert result["success"] is False
    assert calls == [True]


# ------------------------------------------------------------------ test before save

def test_the_service_probe_does_not_recheck_a_saved_setting(probes):
    """The service's probe also tests saved configs (a member's identity, a
    credential change); those keep their setting. The pre-save test route
    applies the rule (tests/e2e/test_pg_schema_setting_at_save.py)."""
    result = _run(ConnectionService().test_connection_params(
        data_source_type="postgresql", config=_config("a%b"), credentials=CREDENTIALS))
    assert result["success"] is False
    assert probes == ["a%b"]


@pytest.mark.parametrize("schema", REFUSED)
def test_the_legacy_test_of_new_params_refuses_a_refused_schema_setting(schema, probes):
    data = DataSourceCreate(name="Agent", type="postgresql", config=_config(schema), credentials=CREDENTIALS)
    with pytest.raises(AppError) as raised:
        _run(DataSourceService().test_new_data_source_connection(
            db=None, data=data, organization=None, current_user=None))
    _assert_schema_refusal(raised, schema)
    assert probes == []


def test_the_legacy_test_of_new_params_probes_an_accepted_schema_setting(probes):
    data = DataSourceCreate(name="Agent", type="postgresql", config=_config("'Sales'"), credentials=CREDENTIALS)
    result = _run(DataSourceService().test_new_data_source_connection(
        db=None, data=data, organization=None, current_user=None))
    assert result["success"] is False
    assert probes == ["'Sales'"]


# ------------------------------------------------------------------ saves that need the app database

async def _seed(schema, *, stored="dict", credentials=CREDENTIALS):
    """An org, an admin and a Postgres connection saved with `schema`. The
    connection row is written directly: the API refuses a refused setting
    now, and connections saved before the check must keep working.
    `stored` is how the config sits in its column: a dict, a JSON string (as
    the services write it) or a JSON string encoded twice."""
    import json
    from app.dependencies import async_session_maker
    from app.models.connection import Connection
    from app.models.organization import Organization
    from app.models.user import User

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Schema Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name="Admin", email=f"admin-{suffix}@example.com", hashed_password="x",
                    is_active=True, is_superuser=False, is_verified=True)
        db.add(user)
        await db.flush()
        config = _config(schema)
        if stored in ("json", "json twice"):
            config = json.dumps(config)
        if stored == "json twice":
            config = json.dumps(config)
        conn = Connection(organization_id=org.id, name=f"pg-{suffix}", type="postgresql",
                          config=config, auth_policy="system_only")
        conn.encrypt_credentials(credentials)
        db.add(conn)
        await db.commit()
        return {"org_id": str(org.id), "user_id": str(user.id), "conn_id": str(conn.id)}


async def _update(ids, **updates):
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User

    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, ids["user_id"])
        return await ConnectionService().update_connection(db, ids["conn_id"], org, user, **updates)


@pytest.mark.db
@pytest.mark.parametrize("schema", ["rvw_a;", "a%b"])
def test_an_update_with_a_new_config_refuses_a_refused_schema_setting(schema, probes):
    ids = _run(_seed("public"))
    with pytest.raises(AppError) as raised:
        _run(_update(ids, config=_config(schema)))
    _assert_schema_refusal(raised, schema)
    assert probes == []


@pytest.mark.db
def test_an_update_with_a_new_config_probes_an_accepted_schema_setting(probes):
    ids = _run(_seed("public"))
    with pytest.raises(HTTPException) as raised:
        _run(_update(ids, config=_config("rvw_a, \"Sales\"")))
    assert raised.value.status_code == 400
    assert PROBE_FAILED in str(raised.value.detail)
    assert probes == ["rvw_a, \"Sales\""]


@pytest.mark.db
def test_an_update_that_leaves_the_config_alone_keeps_a_saved_refused_setting(probes):
    ids = _run(_seed("a%b"))
    updated = _run(_update(ids, name="renamed"))
    assert updated.name == "renamed"
    assert probes == []


@pytest.mark.db
def test_a_credential_change_probes_with_the_saved_setting_unchecked(probes):
    ids = _run(_seed("a%b"))
    with pytest.raises(HTTPException) as raised:
        _run(_update(ids, credentials={"password": "rotated"}))
    assert PROBE_FAILED in str(raised.value.detail)
    assert probes == ["a%b"]


def _assert_probe_ran_not_refused(raised):
    """The update got past the schema check to the faked probe, whose
    failure is the only 400 left."""
    assert not isinstance(raised.value, AppError), raised.value.message
    assert raised.value.status_code == 400
    assert PROBE_FAILED in str(raised.value.detail)


@pytest.mark.db
@pytest.mark.parametrize("stored", ["dict", "json", "json twice"])
@pytest.mark.parametrize("edit", ["new credentials", "rename"])
def test_the_edit_form_resending_a_saved_refused_setting_keeps_it(stored, edit, probes):
    """The edit form always sends the full config back. A saved setting it
    leaves alone is not re-checked; the probe runs with it."""
    ids = _run(_seed("a%b", stored=stored))
    extra = {"credentials": {"password": "rotated"}} if edit == "new credentials" else {"name": "renamed"}
    with pytest.raises(HTTPException) as raised:
        _run(_update(ids, config=_config("a%b"), **extra))
    _assert_probe_ran_not_refused(raised)
    assert probes == ["a%b"]


@pytest.mark.db
def test_a_saved_schema_in_the_credentials_wins_and_is_kept_unchanged(probes):
    """The client merges the credentials over the config, so the saved
    setting is the credentials' one; resending it unchanged is no edit."""
    ids = _run(_seed("public", credentials={**CREDENTIALS, "schema": "a%b"}))
    with pytest.raises(HTTPException) as raised:
        _run(_update(ids, config=_config("public"), credentials={"password": "rotated", "schema": "a%b"}))
    _assert_probe_ran_not_refused(raised)
    assert probes == ["a%b"]


@pytest.mark.db
def test_editing_a_saved_refused_setting_to_another_refused_value_is_refused(probes):
    ids = _run(_seed("a%b"))
    with pytest.raises(AppError) as raised:
        _run(_update(ids, config=_config("rvw_a;")))
    _assert_schema_refusal(raised, "rvw_a;")
    assert probes == []


@pytest.mark.db
def test_editing_a_saved_refused_setting_to_an_accepted_value_passes(probes):
    ids = _run(_seed("a%b"))
    with pytest.raises(HTTPException) as raised:
        _run(_update(ids, config=_config("public")))
    _assert_probe_ran_not_refused(raised)
    assert probes == ["public"]


# ------------------------------------------------------------------ no schema in the credentials

def _assert_credentials_refusal(raised):
    error = raised.value
    assert isinstance(error, AppError)
    assert error.status_code == 400
    assert error.error_code == ErrorCode.CONNECTION_CONFIG_INVALID.value
    assert "schema: the schema setting belongs in the connection settings, not the credentials" in error.message


@pytest.mark.parametrize("schema", ["public", "a%b"])
def test_create_refuses_a_schema_in_the_credentials(schema, probes):
    with pytest.raises(AppError) as raised:
        _run(ConnectionService().create_connection(
            db=None, organization=None, current_user=None, name="pg", type="postgresql",
            config=_config("public"), credentials={**CREDENTIALS, "schema": schema}))
    _assert_credentials_refusal(raised)
    assert probes == []


@pytest.mark.db
def test_an_update_refuses_a_new_schema_in_the_credentials(probes):
    ids = _run(_seed("public"))
    with pytest.raises(AppError) as raised:
        _run(_update(ids, credentials={"password": "rotated", "schema": "public"}))
    _assert_credentials_refusal(raised)
    assert probes == []


@pytest.mark.db
def test_an_update_refuses_a_changed_schema_in_the_credentials(probes):
    ids = _run(_seed("public", credentials={**CREDENTIALS, "schema": "a%b"}))
    with pytest.raises(AppError) as raised:
        _run(_update(ids, credentials={"schema": "public"}))
    _assert_credentials_refusal(raised)
    assert probes == []


@pytest.mark.parametrize("blank", ["", None])
def test_create_refuses_even_a_blank_schema_in_the_credentials(blank, probes):
    """A stored blank `schema` would win over the config's setting."""
    with pytest.raises(AppError) as raised:
        _run(ConnectionService().create_connection(
            db=None, organization=None, current_user=None, name="pg", type="postgresql",
            config=_config("public"), credentials={**CREDENTIALS, "schema": blank}))
    _assert_credentials_refusal(raised)
    assert probes == []


@pytest.mark.db
@pytest.mark.parametrize("blank", ["", None])
def test_an_update_with_a_blank_schema_in_the_credentials_is_not_refused(blank, probes):
    """On an update a blank credential keeps the saved value, so a blank
    `schema` changes nothing: the probe runs with the config's setting."""
    ids = _run(_seed("public"))
    with pytest.raises(HTTPException) as raised:
        _run(_update(ids, credentials={"password": "rotated", "schema": blank}))
    _assert_probe_ran_not_refused(raised)
    assert probes == ["public"]


@pytest.mark.parametrize("blank", ["", None])
def test_a_test_with_a_blank_schema_in_the_credential_overrides_is_not_refused(blank):
    from app.services.connection_service import _check_pg_test_overrides

    saved = SimpleNamespace(type="postgresql", config=_config("public"),
                            decrypt_credentials=lambda: dict(CREDENTIALS))
    _check_pg_test_overrides(saved, None, {"password": "", "schema": blank})
    with pytest.raises(AppError) as raised:
        _check_pg_test_overrides(saved, None, {"schema": "public"})
    _assert_credentials_refusal(raised)


# ------------------------------------------------------------------ the client's merge

def test_construct_client_overrides_win_only_when_not_blank(monkeypatch):
    """The merge the save-time check mirrors: a non-blank override wins,
    `""` and None keep the saved value."""
    from app.models.connection import Connection

    async def allow(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.core.egress.acheck_connection_params", allow)
    conn = Connection(id="CONN-1", organization_id="ORG-1", name="pg", type="postgresql",
                      config=_config("public"), auth_policy="system_only")
    conn.encrypt_credentials({"user": "u", "password": "p"})
    client = _run(ConnectionService().construct_client(
        None, conn,
        config_overrides={"schema": "", "database": None, "host": "other.invalid"},
        credential_overrides={"user": "", "password": "rotated", "sslmode": None}))
    assert (client.schema, client.database, client.host) == ("public", "d", "other.invalid")
    assert (client.user, client.password, client.sslmode) == ("u", "rotated", "prefer")


def test_the_legacy_payload_drops_a_schema_in_the_credentials(probes):
    """The legacy data-source payload validates its credentials against the
    type's credentials model, which has no `schema`: the key never reaches
    the legacy create or its pre-save test, so the config's setting is the
    one the client gets. (Both still call the check, as the other paths do.)"""
    data = DataSourceCreate(name="Agent", type="postgresql", config=_config("public"),
                            credentials={**CREDENTIALS, "schema": "a%b"})
    assert "schema" not in data.credentials
    result = _run(DataSourceService().test_new_data_source_connection(
        db=None, data=data, organization=None, current_user=None))
    assert result["success"] is False
    assert probes == ["public"]


async def _legacy_create(ids, schema, auth_policy):
    from app.dependencies import async_session_maker
    from app.models.organization import Organization
    from app.models.user import User

    data = DataSourceCreate(name=f"Agent {uuid.uuid4().hex[:6]}", type="postgresql", config=_config(schema),
                            credentials=CREDENTIALS, auth_policy=auth_policy)
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, ids["user_id"])
        return await DataSourceService().create_data_source(db, org, user, data)


@pytest.mark.db
@pytest.mark.parametrize("auth_policy", ["system_only", "user_required"])
def test_the_legacy_create_refuses_a_refused_schema_setting(auth_policy, probes):
    """`system_only` is refused by the pre-save test; `user_required`
    skips that test and is refused by the save itself."""
    ids = _run(_seed("public"))
    with pytest.raises(AppError) as raised:
        _run(_legacy_create(ids, "rvw_a --x", auth_policy))
    _assert_schema_refusal(raised, "rvw_a --x")
    assert probes == []


@pytest.mark.db
def test_the_legacy_create_lets_an_accepted_schema_setting_through(probes):
    ids = _run(_seed("public"))
    try:
        _run(_legacy_create(ids, "\"Sales\", public", "user_required"))
    except AppError as e:
        assert e.error_code != ErrorCode.CONNECTION_CONFIG_INVALID.value, e.message
    except HTTPException as e:
        # Later rules (the license, for one) may still refuse the save.
        assert e.status_code != 400 or "schema" not in str(e.detail)


# ------------------------------------------------------------------ the check's own edges

def _saved(schema, credentials=None):
    """A saved connection as the check reads it: its config and a
    `decrypt_credentials` that returns `credentials`, or raises when given
    an exception."""
    def decrypt():
        if isinstance(credentials, Exception):
            raise credentials
        return dict(credentials if credentials is not None else CREDENTIALS)

    return SimpleNamespace(type="postgresql", config=_config(schema), decrypt_credentials=decrypt)


def test_saved_credentials_that_cannot_be_decrypted_read_as_none():
    """A config-only edit of such a connection is checked as if it had no
    saved credentials, not turned into a server error."""
    from app.services.connection_service import _check_pg_schemas

    saved = _saved("public", ValueError("cannot decrypt"))
    _check_pg_schemas("postgresql", _config("public"), None, saved=saved)
    _check_pg_schemas("postgresql", _config("rvw_a, \"Sales\""), None, saved=saved)
    with pytest.raises(AppError) as raised:
        _check_pg_schemas("postgresql", _config("a%b"), None, saved=saved)
    _assert_schema_refusal(raised, "a%b")


def _assert_not_text_refusal(raised):
    error = raised.value
    assert isinstance(error, AppError)
    assert error.status_code == 400
    assert error.error_code == ErrorCode.CONNECTION_CONFIG_INVALID.value
    assert "schema: the schema setting must be text" in error.message


@pytest.mark.parametrize("schema", [5, True, ["public"], {"a": 1}])
def test_a_schema_setting_that_is_not_text_is_refused(schema):
    from app.services.connection_service import _check_pg_schemas

    with pytest.raises(AppError) as raised:
        _check_pg_schemas("postgresql", _config(schema), CREDENTIALS)
    _assert_not_text_refusal(raised)
    with pytest.raises(AppError) as raised:
        _check_pg_schemas("postgresql", _config(schema), None, saved=_saved("public"))
    _assert_not_text_refusal(raised)
    with pytest.raises(AppError) as raised:
        _check_pg_schemas("postgresql", _config(schema), None, saved=_saved(None))
    _assert_not_text_refusal(raised)


def test_a_saved_schema_setting_that_is_not_text_keeps_working_until_edited():
    from app.services.connection_service import _check_pg_schemas

    _check_pg_schemas("postgresql", _config(5), None, saved=_saved(5))
    _check_pg_schemas("postgresql", _config("public"), None, saved=_saved(5))
    _check_pg_schemas("postgresql", _config(None), None, saved=_saved(5))
    with pytest.raises(AppError) as raised:
        _check_pg_schemas("postgresql", _config(6), None, saved=_saved(5))
    _assert_not_text_refusal(raised)
