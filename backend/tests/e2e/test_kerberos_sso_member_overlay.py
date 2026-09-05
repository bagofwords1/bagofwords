"""End-to-end (service layer) validation for per-user Kerberos SSO.

Proves the two things that were "wired but not demoed end-to-end":

  1. Per-user overlay IS happening — a Kerberos member with NO stored credential
     is classified effective_auth="user" from a verified directory binding, which
     makes the tables list scope to THEIR overlay. Two members with disjoint
     overlays (alice→sales, bob→audit) see disjoint catalogs through the real
     service path (`get_data_source_schema_paginated`).
  2. The UI shows "Connected" — `build_user_status_for_connection` returns
     has_user_credentials=True + effective_auth="user" + auth_mode
     ="kerberos_delegated", which is exactly what the status chip renders as
     "Connected" (and what suppresses the "connect required" prompt).

Unlike OBO, the members hold NO UserConnectionCredentials row — access comes
from their verified AD object, not their email. This runs against a real test
DB (no live SQL Server, KDC, or browser); the actual S4U → SQL introspection was
separately proven in a throwaway docker lab (samba AD DC + SQL Server; removed
from the repo — see git history for lab/sql-server-kerberos if it's ever needed).
"""
import asyncio
import uuid

import pytest

from app.settings.config import settings
from app.settings.bow_config import LDAPConfig
from app.ee.ldap.connection import LDAPConnectionManager

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.connection_table import ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.models.user_data_source_overlay import UserDataSourceTable
from app.models.domain_connection import domain_connection
from app.services.data_source_service import DataSourceService
from app.services.connection_service import ConnectionService
from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def directory(monkeypatch):
    """Substitute only AD; service policy, overlay filtering and DB stay real."""
    config = LDAPConfig(enabled=True, url="ldaps://ad.corp.example.com",
                        admission_group_dn="cn=members,dc=corp,dc=example,dc=com",
                        kerberos_realm="CORP.EXAMPLE.COM")
    monkeypatch.setattr(settings.bow_config, "ldap", config)
    objects = {}
    monkeypatch.setattr(LDAPConnectionManager, "read_identity", lambda self, dn: objects[dn].copy())
    return objects


async def _seed(directory):
    """MSSQL (shared-catalog, user_required) connection with Kerberos SSO and
    two members who have NO stored credentials. alice's overlay = [sales],
    bob's = [audit] — disjoint, as their real SQL grants would produce."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Kerb Org {suffix}")
        db.add(org)
        await db.flush()
        settings.bow_config.ldap.organization_id = str(org.id)
        provider = LDAPConnectionManager(settings.bow_config.ldap).provider_id

        # Login emails deliberately differ from authoritative AD principals.
        alice = User(name="Alice", email=f"alice-{suffix}@corp.example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        bob = User(name="Bob", email=f"bob-{suffix}@corp.example.com",
                   hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        # Service-layer fixture: verified directory bindings and SQL overlays
        # are seeded directly because this test has no live AD/SQL server.
        # Login/admission behavior is covered separately through the HTTP API.
        for number, user in enumerate((alice, bob), start=1):
            guid = str(uuid.uuid4())
            dn = f"cn={guid},dc=corp,dc=example,dc=com"
            identity = {"provider": provider, "guid": guid, "dn": dn,
                        "sid_hex": "0101000000000005" + number.to_bytes(4, "little").hex(),
                        "principal": f"member-{number}@CORP.EXAMPLE.COM",
                        "organization_id": str(org.id)}
            user.ldap_identity = identity
            user.ldap_subject = provider + ":" + guid
            directory[dn] = identity
        # A separate owner so alice/bob are plain members (owner would fall back
        # to system creds and mask the per-user path).
        owner = User(name="Owner", email=f"owner-{suffix}@corp.example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        db.add_all([alice, bob, owner])
        await db.flush()

        conn = Connection(
            organization_id=org.id,
            name=f"MSSQL {suffix}",
            type="MSSQL",
            config={"host": "sql.corp.example.com", "port": 1433, "database": "dwh", "auth_type": "kerberos"},
            auth_policy="user_required",
            allowed_user_auth_modes=["kerberos_delegated"],
        )
        # System identity = the service account's Kerberos identity (no secret).
        conn.encrypt_credentials({"use_kerberos": True})
        db.add(conn)
        await db.flush()

        ds = DataSource(name=f"DWH {suffix}", organization_id=org.id, is_active=True, owner_user_id=owner.id)
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(data_source_id=ds.id, connection_id=conn.id))

        # Canonical catalog (built by the service account): both tables.
        ds_table_ids = {}
        for tname in ("sales", "audit"):
            ct = ConnectionTable(name=tname, connection_id=conn.id,
                                 columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=0,
                                 metadata_json={"schema": "dbo"})
            db.add(ct)
            await db.flush()
            dst = DataSourceTable(name=tname, datasource_id=ds.id, connection_table_id=ct.id,
                                  is_active=True, metadata_json={"schema": "dbo"},
                                  columns=[{"name": "id", "dtype": "int"}], pks=[], fks=[], no_rows=0)
            db.add(dst)
            await db.flush()
            ds_table_ids[tname] = dst.id

        # Per-user overlays (what introspecting AS each member yields): disjoint.
        db.add(UserDataSourceTable(data_source_id=ds.id, user_id=alice.id, table_name="sales",
                                   data_source_table_id=ds_table_ids["sales"], is_accessible=True, status="accessible"))
        db.add(UserDataSourceTable(data_source_id=ds.id, user_id=bob.id, table_name="audit",
                                   data_source_table_id=ds_table_ids["audit"], is_accessible=True, status="accessible"))

        await db.commit()
        return {"org_id": org.id, "ds_id": ds.id, "conn_id": conn.id,
                "alice_id": alice.id, "bob_id": bob.id,
                "alice_principal": alice.ldap_identity["principal"],
                "alice_sid": alice.ldap_identity["sid_hex"]}


async def _status(ids, user_id):
    async with async_session_maker() as db:
        conn = await db.get(Connection, ids["conn_id"])
        ds = await db.get(DataSource, ids["ds_id"])
        user = await db.get(User, user_id)
        return await UserDataSourceCredentialsService().build_user_status_for_connection(db, conn, user, data_source=ds)


async def _resolved_creds(ids, user_id):
    async with async_session_maker() as db:
        conn = await db.get(Connection, ids["conn_id"])
        user = await db.get(User, user_id)
        return await ConnectionService().resolve_credentials(db, conn, user)


async def _paginated_table_names(ids, user_id):
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, user_id)
        resp = await DataSourceService().get_data_source_schema_paginated(
            db=db, data_source_id=ids["ds_id"], organization=org,
            page=1, page_size=100, include_inactive=True, current_user=user,
        )
        return resp.total_tables, {t.name.split(".")[-1].lower() for t in resp.tables}


@pytest.mark.e2e
def test_kerberos_member_status_shows_connected(directory):
    """Claim 2: the status the chip reads → 'Connected' for a Kerberos member
    with NO stored credential."""
    ids = _run(_seed(directory))
    st = _run(_status(ids, ids["alice_id"]))
    assert st.has_user_credentials is True          # chip renders "Connected"
    assert st.effective_auth == "user"              # runs as the member, not service acct
    assert st.auth_mode == "kerberos_delegated"
    assert st.connection in ("unknown", "success")  # not "offline"/"not_connected"


@pytest.mark.e2e
def test_kerberos_member_resolves_own_upn(directory):
    """Claim 1 (wiring): resolve_credentials passes the MEMBER's UPN to the
    client, so queries/introspection impersonate them."""
    ids = _run(_seed(directory))
    creds = _run(_resolved_creds(ids, ids["alice_id"]))
    assert creds.get("use_kerberos") is True
    assert creds.get("kerberos_impersonate") == ids["alice_principal"]
    assert creds.get("kerberos_expected_sid") == ids["alice_sid"]


@pytest.mark.e2e
def test_kerberos_per_user_overlay_is_scoped(directory):
    """Claim 1: per-user overlay IS happening — through the real service path,
    alice and bob see disjoint catalogs (their own tables only)."""
    ids = _run(_seed(directory))
    alice_total, alice_tables = _run(_paginated_table_names(ids, ids["alice_id"]))
    bob_total, bob_tables = _run(_paginated_table_names(ids, ids["bob_id"]))
    assert alice_tables == {"sales"}, f"alice should see only sales, saw {alice_tables}"
    assert bob_tables == {"audit"}, f"bob should see only audit, saw {bob_tables}"
    assert alice_tables.isdisjoint(bob_tables)


@pytest.mark.e2e
def test_kerberos_member_without_upn_is_not_connected(directory):
    """A member without a verified AD binding cannot acquire delegated access."""
    ids = _run(_seed(directory))
    async def _make_no_upn_member():
        async with async_session_maker() as db:
            u = User(name="Unlinked", email=f"unlinked-{uuid.uuid4().hex[:6]}@corp.example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
            db.add(u)
            await db.commit()
            return u.id
    uid = _run(_make_no_upn_member())
    st = _run(_status(ids, uid))
    assert st.has_user_credentials is False
    assert st.effective_auth == "none"
    assert st.connection == "not_connected"


@pytest.mark.e2e
def test_empty_successful_user_schema_refresh_revokes_old_access(directory, monkeypatch):
    from app.data_sources.clients.mssql_client import MSSQLClient
    ids = _run(_seed(directory))
    async def empty_schema(self):
        return []
    monkeypatch.setattr(MSSQLClient, "aget_schemas", empty_schema)
    async def refresh():
        async with async_session_maker() as db:
            ds = (await db.execute(select(DataSource).options(selectinload(DataSource.connections))
                                  .where(DataSource.id == ids["ds_id"]))).scalar_one()
            user = await db.get(User, ids["alice_id"])
            await DataSourceService().get_user_data_source_schema(db, ds, user)
    _run(refresh())
    assert _run(_paginated_table_names(ids, ids["alice_id"]))[1] == set()
    assert _run(_paginated_table_names(ids, ids["bob_id"]))[1] == {"audit"}


@pytest.mark.e2e
def test_first_kerberos_prompt_discovers_only_callers_schema(directory, monkeypatch):
    from app.ai.context.builders.schema_context_builder import SchemaContextBuilder
    from app.data_sources.clients.mssql_client import MSSQLClient
    ids = _run(_seed(directory))
    async def own_schema(self):
        assert self.kerberos_impersonate == ids["alice_principal"]
        return [{"name": "sales", "columns": [{"name": "id", "dtype": "int"}]}]
    monkeypatch.setattr(MSSQLClient, "aget_schemas", own_schema)
    async def build():
        async with async_session_maker() as db:
            await db.execute(delete(UserDataSourceTable).where(
                UserDataSourceTable.data_source_id == ids["ds_id"],
                UserDataSourceTable.user_id == ids["alice_id"]))
            await db.commit()
            ds = (await db.execute(select(DataSource).options(selectinload(DataSource.connections))
                                  .where(DataSource.id == ids["ds_id"]))).scalar_one()
            user = await db.get(User, ids["alice_id"])
            org = await db.get(Organization, ids["org_id"])
            context = await SchemaContextBuilder(db, [ds], org, None, user=user).build(with_stats=False)
            return {t.name for source in context.data_sources for t in source.tables}
    assert _run(build()) == {"sales"}
