"""A member's own Postgres credentials keep only the credentials model's fields.

Contract: the per-user credentials paths of a data source
(`UserDataSourceCredentialsService`: test, save and both patch branches)
keep only the fields of the type's credentials model for a `postgresql`
connection, as they already do for `mssql` and `neo4j`. `PostgreSQLCredentials`
has `user` and `password` only, and the client merges the credentials over
the connection's config, so a member's `schema` (or any other unknown key)
would otherwise override the admin's config at run time. Other types store
what they did before.

The save keys on the data source's first connection, but a stored row
applies to every `user_required` connection of the data source. So the read
(`DataSourceService.resolve_credentials` and
`resolve_credentials_for_connection`) also keeps, for a `postgresql`,
`mssql` or `neo4j` connection, only the field names of that type's
credentials model for the row's auth mode. It drops the other keys without
validating, so rows saved before keep loading; they are not rewritten.
Kerberos-delegated rows are handled as before.

Only the network probe and the egress check are faked; the services, the
models and their encryption run for real.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.models.connection import Connection
from app.models.user_data_source_credentials import UserDataSourceCredentials
from app.services.connection_service import ConnectionService
from app.services.data_source_service import DataSourceService
from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService

ADMIN_CONFIG = {"host": "db.invalid", "port": 5432, "database": "d", "schema": "public"}
MEMBER = {"user": "member", "password": "pw"}
EXTRAS = {"schema": "other_schema", "host": "elsewhere.invalid"}
PROBE_FAILED = "probe-did-not-connect"


def _run(coro):
    return asyncio.run(coro)


async def _allow(*_args, **_kwargs):
    return None


@pytest.fixture(autouse=True)
def no_egress(monkeypatch):
    monkeypatch.setattr("app.core.egress.acheck_connection_params", _allow)
    # The best-effort catalog refresh after a save would try the network.
    monkeypatch.setattr("app.services.data_source_service.DataSourceService.get_user_data_source_schema", _allow)


@pytest.fixture
def probes(monkeypatch):
    calls = []

    def probe(self):
        calls.append({"schema": self.schema, "host": self.host, "user": self.user})
        return {"success": False, "message": PROBE_FAILED}

    monkeypatch.setattr(PostgresqlClient, "test_connection", probe)
    return calls


def _connection(conn_type="postgresql", config=None):
    conn = Connection(id="CONN-1", organization_id="ORG-1", name="pg", type=conn_type,
                      config=dict(config if config is not None else ADMIN_CONFIG),
                      auth_policy="user_required", allowed_user_auth_modes=None)
    conn.encrypt_credentials({"user": "admin", "password": "admin-pw"})
    return conn


def _source(conn):
    return SimpleNamespace(id="DS-1", organization_id="ORG-1", connections=[conn])


class _Rows:
    def __init__(self, row):
        self._row = row

    def scalars(self):
        return self

    def first(self):
        return self._row


class _Db:
    """Answers the paths' lookups with `row` (None: no saved row yet)."""

    def __init__(self, row=None):
        self.row = row

    async def execute(self, _stmt):
        return _Rows(self.row)

    def add(self, row):
        self.row = row

    async def commit(self):
        return None

    async def refresh(self, row):
        # What the database fills in on insert.
        from datetime import datetime

        row.id = row.id or "ROW-1"
        row.created_at = row.created_at or datetime(2026, 1, 1)
        row.updated_at = row.updated_at or datetime(2026, 1, 1)


def _saved_row(auth_mode="userpass", credentials=None):
    row = UserDataSourceCredentials(data_source_id="DS-1", user_id="U-1", organization_id="ORG-1",
                                    auth_mode=auth_mode, is_active=True, is_primary=True)
    row.encrypt_credentials(credentials if credentials is not None else dict(MEMBER))
    return row


async def _client_from(conn, stored):
    """The client the runtime builds for this member from `stored`."""
    service = ConnectionService()

    async def resolved(*_args, **_kwargs):
        return dict(stored)

    service.resolve_credentials = resolved
    return await service.construct_client(None, conn, current_user=SimpleNamespace(id="U-1"))


def _upsert(db, conn, credentials, auth_mode="userpass"):
    payload = SimpleNamespace(auth_mode=auth_mode, credentials=dict(credentials), is_primary=True,
                              expires_at=None, metadata_json=None)
    return UserDataSourceCredentialsService().upsert_my_credentials(
        db, data_source=_source(conn), user=SimpleNamespace(id="U-1"), payload=payload)


def _patch(db, conn, credentials, auth_mode=None):
    payload = SimpleNamespace(auth_mode=auth_mode, credentials=dict(credentials), is_active=None,
                              is_primary=None, expires_at=None, metadata_json=None)
    return UserDataSourceCredentialsService().patch_my_credentials(
        db, data_source=_source(conn), user=SimpleNamespace(id="U-1"), payload=payload)


# ------------------------------------------------------------------ test

def test_a_member_test_probes_with_the_admins_schema_and_host(probes):
    conn = _connection()
    payload = SimpleNamespace(auth_mode="userpass", credentials={**MEMBER, **EXTRAS})
    result = _run(UserDataSourceCredentialsService().test_my_credentials(
        None, data_source=_source(conn), user=SimpleNamespace(id="U-1"), payload=payload))
    assert result["success"] is False
    assert probes == [{"schema": "public", "host": "db.invalid", "user": "member"}]
    assert payload.credentials == MEMBER


# ------------------------------------------------------------------ save

def test_a_member_save_stores_only_the_models_fields():
    conn = _connection()
    db = _Db()
    _run(_upsert(db, conn, {**MEMBER, **EXTRAS}))
    stored = db.row.decrypt_credentials()
    assert stored == MEMBER
    client = _run(_client_from(conn, stored))
    assert (client.schema, client.host, client.user) == ("public", "db.invalid", "member")


def test_a_plain_member_save_still_works():
    conn = _connection()
    db = _Db()
    _run(_upsert(db, conn, MEMBER))
    assert db.row.decrypt_credentials() == MEMBER


def test_a_member_save_with_a_blank_password_keeps_it_blank():
    conn = _connection()
    db = _Db()
    _run(_upsert(db, conn, {"user": "member"}))
    assert db.row.decrypt_credentials() == {"user": "member", "password": ""}


# ------------------------------------------------------------------ patch

@pytest.mark.parametrize("names_the_mode", [True, False], ids=["changes_auth_mode", "keeps_auth_mode"])
def test_a_member_patch_stores_only_the_models_fields(names_the_mode):
    conn = _connection()
    db = _Db(_saved_row())
    _run(_patch(db, conn, {"user": "member2", "password": "pw2", **EXTRAS},
                auth_mode="userpass" if names_the_mode else None))
    stored = db.row.decrypt_credentials()
    assert stored == {"user": "member2", "password": "pw2"}
    client = _run(_client_from(conn, stored))
    assert (client.schema, client.host, client.user) == ("public", "db.invalid", "member2")


@pytest.mark.parametrize(
    "conn_type, config, mode_and_credentials, stored",
    [
        ("MSSQL", {"host": "db.invalid", "port": 1433, "database": "d"},
         ("userpass", {"user": "member2", "password": "pw2", "host": "elsewhere.invalid"}),
         {"user": "member2", "password": "pw2"}),
        ("neo4j", {"uri": "bolt://db.invalid:7687", "database": "neo4j"},
         ("userpass", {"username": "member2", "password": "pw2", "uri": "bolt://elsewhere.invalid:7687"}),
         {"username": "member2", "password": "pw2"}),
    ],
    ids=["mssql", "neo4j"],
)
@pytest.mark.parametrize("names_the_mode", [True, False], ids=["changes_auth_mode", "keeps_auth_mode"])
def test_mssql_and_neo4j_member_patches_store_only_the_models_fields(
        conn_type, config, mode_and_credentials, stored, names_the_mode):
    auth_mode, credentials = mode_and_credentials
    conn = _connection(conn_type, config)
    db = _Db(_saved_row(credentials={k: v for k, v in stored.items()}))
    _run(_patch(db, conn, credentials, auth_mode=auth_mode if names_the_mode else None))
    assert db.row.decrypt_credentials() == stored


# ------------------------------------------------------------------ read

def _resolved_for(conn, row, connections=None):
    """What the run time resolves for this member on `conn` from `row`."""
    source = SimpleNamespace(id="DS-1", organization_id="ORG-1", connections=connections or [conn])
    return _run(DataSourceService().resolve_credentials_for_connection(
        _Db(row), connection=conn, data_source=source, current_user=SimpleNamespace(id="U-1")))


def test_a_row_saved_on_another_first_connection_reaches_postgres_as_user_and_password_only(monkeypatch):
    """The save keys on the data source's first connection (MySQL here keeps
    the payload as sent), but the row applies to every `user_required`
    connection: the Postgres one gets only its model's fields."""
    monkeypatch.setattr("app.services.data_source_service._acheck_connection_destination", _allow)
    mysql = _connection("mysql", {"host": "mysql.invalid", "port": 3306, "database": "m"})
    pg = _connection()
    pg.id, pg.name = "CONN-2", "pg2"
    db = _Db()
    payload = SimpleNamespace(auth_mode="userpass", credentials={**MEMBER, **EXTRAS}, is_primary=True,
                              expires_at=None, metadata_json=None)
    source = SimpleNamespace(id="DS-1", organization_id="ORG-1", name="ds", connections=[mysql, pg])
    _run(UserDataSourceCredentialsService().upsert_my_credentials(
        db, data_source=source, user=SimpleNamespace(id="U-1"), payload=payload))
    assert db.row.decrypt_credentials() == {**MEMBER, **EXTRAS}

    assert _resolved_for(pg, db.row, connections=[mysql, pg]) == MEMBER
    client = _run(DataSourceService()._construct_user_catalog_client(
        db, data_source=source, connection=pg, user=SimpleNamespace(id="U-1")))
    assert isinstance(client, PostgresqlClient)
    assert (client.user, client.password, client.schema, client.host) == ("member", "pw", "public", "db.invalid")


def test_an_old_row_with_extra_keys_is_filtered_when_read_and_still_loads(monkeypatch):
    """A row saved before the save-time filter keeps what it holds; the read
    drops its extra keys, and the client still builds from it."""
    monkeypatch.setattr("app.services.data_source_service._acheck_connection_destination", _allow)
    conn = _connection()
    old = {**MEMBER, **EXTRAS, "pasted_here": "x"}
    row = _saved_row(credentials=old)
    assert row.decrypt_credentials() == old
    assert _resolved_for(conn, row) == MEMBER

    source = SimpleNamespace(id="DS-1", organization_id="ORG-1", name="ds", connections=[conn])
    client = _run(DataSourceService().construct_client(_Db(row), source, SimpleNamespace(id="U-1")))
    assert (client.user, client.password, client.schema, client.host) == ("member", "pw", "public", "db.invalid")
    assert row.decrypt_credentials() == old


@pytest.mark.parametrize(
    "conn_type, config, auth_mode, row_credentials, applied",
    [
        ("MSSQL", {"host": "db.invalid", "port": 1433, "database": "d"}, "userpass",
         {"user": "member", "password": "pw", "schema": "other", "host": "elsewhere.invalid"},
         {"user": "member", "password": "pw"}),
        ("neo4j", {"uri": "bolt://db.invalid:7687", "database": "neo4j"}, "userpass",
         {"username": "member", "password": "pw", "uri": "bolt://elsewhere.invalid:7687", "database": "x"},
         {"username": "member", "password": "pw"}),
        ("neo4j", {"uri": "bolt://db.invalid:7687", "database": "neo4j"}, "bearer",
         {"token": "t0k", "uri": "bolt://elsewhere.invalid:7687"}, {"token": "t0k"}),
    ],
    ids=["mssql", "neo4j", "neo4j_bearer"],
)
def test_mssql_and_neo4j_rows_are_filtered_when_read(conn_type, config, auth_mode, row_credentials, applied):
    conn = _connection(conn_type, config)
    assert _resolved_for(conn, _saved_row(auth_mode, row_credentials)) == applied


@pytest.mark.parametrize("conn_type", ["mssql", "MSSQL"])
def test_the_read_filter_finds_the_types_model_in_any_case(conn_type):
    conn = _connection(conn_type, {"host": "db.invalid", "port": 1433, "database": "d"})
    row = _saved_row("userpass", {"user": "member", "password": "pw", "host": "elsewhere.invalid"})
    assert _resolved_for(conn, row) == {"user": "member", "password": "pw"}
    source = SimpleNamespace(id="DS-1", organization_id="ORG-1", connections=[conn])
    assert _run(DataSourceService().resolve_credentials(
        _Db(row), source, SimpleNamespace(id="U-1"))) == {"user": "member", "password": "pw"}


KERBEROS_TYPES = pytest.mark.parametrize("conn_type, config", [
    ("MSSQL", {"host": "db.invalid", "port": 1433, "database": "d"}),
    ("neo4j", {"uri": "bolt://db.invalid:7687", "database": "neo4j"}),
], ids=["mssql", "neo4j"])
# A key the Kerberos models don't have: the model dump would drop it, so it
# shows the row is stored as sent.
PATCHED_ROW = {"use_kerberos": True, "note": "as sent"}
STORED_ROW = {"use_kerberos": True, "note": "stored"}


@KERBEROS_TYPES
@pytest.mark.parametrize("names_the_mode", [True, False], ids=["changes_auth_mode", "keeps_auth_mode"])
def test_kerberos_delegated_rows_are_handled_as_before(conn_type, config, names_the_mode):
    """A patch of a kerberos_delegated row stores the payload as sent."""
    conn = _connection(conn_type, config)
    saved = _saved_row("kerberos_delegated" if not names_the_mode else "userpass",
                       {"use_kerberos": True} if not names_the_mode else {"user": "m", "password": "p"})
    db = _Db(saved)
    _run(_patch(db, conn, PATCHED_ROW, auth_mode="kerberos_delegated" if names_the_mode else None))
    assert db.row.auth_mode == "kerberos_delegated"
    assert db.row.decrypt_credentials() == PATCHED_ROW


@KERBEROS_TYPES
def test_a_stored_kerberos_delegated_row_is_applied_as_before(conn_type, config):
    conn = _connection(conn_type, config)
    assert _resolved_for(conn, _saved_row("kerberos_delegated", STORED_ROW)) == STORED_ROW


def test_a_kerberos_delegated_row_applies_nothing_to_a_postgres_connection():
    """Postgres has no Kerberos-delegated mode, so a row saved in that mode
    against another type's first connection gives Postgres none of its keys."""
    conn = _connection()
    row = {"use_kerberos": True, "schema": "other", "host": "elsewhere.invalid"}
    assert _resolved_for(conn, _saved_row("kerberos_delegated", row)) == {}


def test_a_row_in_a_mode_the_connection_type_lacks_applies_nothing():
    """A row saved in a mode of the first connection's type (Neo4j's token,
    say) that Postgres has no model for: none of its keys reach Postgres."""
    conn = _connection()
    assert _resolved_for(conn, _saved_row("bearer", {"token": "t0k", "schema": "other"})) == {}


def test_other_types_are_applied_as_stored():
    conn = _connection("mysql", {"host": "db.invalid", "port": 3306, "database": "m"})
    stored = {**MEMBER, **EXTRAS}
    assert _resolved_for(conn, _saved_row(credentials=stored)) == stored


# ------------------------------------------------------------------ other types

def test_other_types_store_the_payload_as_before():
    """Oracle's member credentials keep the raw payload, unknown keys included."""
    conn = _connection("oracledb", {"host": "db.invalid", "port": 1521, "service_name": "s"})
    db = _Db()
    _run(_upsert(db, conn, {**MEMBER, "pasted_here": "x"}))
    assert db.row.decrypt_credentials() == {**MEMBER, "pasted_here": "x"}

    db = _Db(_saved_row())
    _run(_patch(db, conn, {**MEMBER, "pasted_here": "y"}))
    assert db.row.decrypt_credentials() == {**MEMBER, "pasted_here": "y"}
