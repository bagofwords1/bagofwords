"""End-to-end (service layer) validation for per-user Kerberos SSO.

Proves the two things that were "wired but not demoed end-to-end":

  1. Per-user overlay IS happening — a Kerberos member with NO stored credential
     is classified effective_auth="user" (purely from a resolvable UPN), which
     makes the tables list scope to THEIR overlay. Two members with disjoint
     overlays (alice→sales, bob→audit) see disjoint catalogs through the real
     service path (`get_data_source_schema_paginated`).
  2. The UI shows "Connected" — `build_user_status_for_connection` returns
     has_user_credentials=True + effective_auth="user" + auth_mode
     ="kerberos_delegated", which is exactly what the status chip renders as
     "Connected" (and what suppresses the "connect required" prompt).

Unlike OBO, the members hold NO UserConnectionCredentials row — access comes
from their AD principal (login UPN). This runs in-process against a real sqlite
DB (no live SQL Server, KDC, or browser); the actual S4U → SQL introspection was
separately proven in a throwaway docker lab (samba AD DC + SQL Server; removed
from the repo — see git history for lab/sql-server-kerberos if it's ever needed).
"""
import asyncio
import uuid

import pytest

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


def _run(coro):
    return asyncio.run(coro)


async def _seed():
    """MSSQL (shared-catalog, user_required) connection with Kerberos SSO and
    two members who have NO stored credentials. alice's overlay = [sales],
    bob's = [audit] — disjoint, as their real SQL grants would produce."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Kerb Org {suffix}")
        db.add(org)
        await db.flush()

        # Members whose login email IS their AD UPN (so it resolves).
        alice = User(name="Alice", email=f"alice-{suffix}@corp.example.com",
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
        bob = User(name="Bob", email=f"bob-{suffix}@corp.example.com",
                   hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
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
                "alice_id": alice.id, "bob_id": bob.id, "alice_email": alice.email}


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
def test_kerberos_member_status_shows_connected():
    """Claim 2: the status the chip reads → 'Connected' for a Kerberos member
    with NO stored credential."""
    ids = _run(_seed())
    st = _run(_status(ids, ids["alice_id"]))
    assert st.has_user_credentials is True          # chip renders "Connected"
    assert st.effective_auth == "user"              # runs as the member, not service acct
    assert st.auth_mode == "kerberos_delegated"
    assert st.connection in ("unknown", "success")  # not "offline"/"not_connected"


@pytest.mark.e2e
def test_kerberos_member_resolves_own_upn():
    """Claim 1 (wiring): resolve_credentials passes the MEMBER's UPN to the
    client, so queries/introspection impersonate them."""
    ids = _run(_seed())
    creds = _run(_resolved_creds(ids, ids["alice_id"]))
    assert creds.get("use_kerberos") is True
    assert creds.get("kerberos_impersonate") == ids["alice_email"]


@pytest.mark.e2e
def test_kerberos_per_user_overlay_is_scoped():
    """Claim 1: per-user overlay IS happening — through the real service path,
    alice and bob see disjoint catalogs (their own tables only)."""
    ids = _run(_seed())
    alice_total, alice_tables = _run(_paginated_table_names(ids, ids["alice_id"]))
    bob_total, bob_tables = _run(_paginated_table_names(ids, ids["bob_id"]))
    assert alice_tables == {"sales"}, f"alice should see only sales, saw {alice_tables}"
    assert bob_tables == {"audit"}, f"bob should see only audit, saw {bob_tables}"
    assert alice_tables.isdisjoint(bob_tables)


@pytest.mark.e2e
def test_kerberos_member_without_upn_is_not_connected():
    """A member whose login isn't a UPN and who set no principal → not connected
    (fail closed; the UI would prompt them to set their AD principal)."""
    ids = _run(_seed())
    async def _make_no_upn_member():
        async with async_session_maker() as db:
            u = User(name="NoUPN", email=f"nouser-{uuid.uuid4().hex[:6]}",  # no '@'
                     hashed_password="x", is_active=True, is_superuser=False, is_verified=True)
            db.add(u)
            await db.commit()
            return u.id
    uid = _run(_make_no_upn_member())
    st = _run(_status(ids, uid))
    assert st.has_user_credentials is False
    assert st.effective_auth == "none"
    assert st.connection == "not_connected"
