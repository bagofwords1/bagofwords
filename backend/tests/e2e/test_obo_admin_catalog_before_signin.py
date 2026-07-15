"""
Fix validation: creating admin/owner on a delegated (OBO, user_required+oauth)
data source sees the CANONICAL catalog before their first sign-in; a plain
member without a token still fails closed to zero.

Reported flow (Power BI / Fabric with Entra OBO): the admin creates the
connection, the background indexer seeds the canonical catalog with the
service principal, the admin then creates an agent — and the Select Tables
step showed ZERO tables ("effective_auth == none" scoped the paginated read to
an empty overlay), while "Reload tables" 403'd on "Connect required" and the
UI swallowed it. Tables only appeared after the admin's OAuth sign-in.

After the fix:
  - `get_data_source_schema_paginated` / `get_data_source_schema` /
    `_refresh_shared_user_overlay` fall back to the canonical catalog for the
    data source owner and org admins (`_admin_catalog_access`) — the same
    audience that already sees the catalog via connection management.
  - `ConnectionService.refresh_schema` indexes with the connection's system
    creds when the caller has no per-user token (same identity as the
    background indexer) instead of raising 403.
  - Members without a token still see nothing (fail-closed unchanged).

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_obo_admin_catalog_before_signin.py -v -s
"""
import uuid
import asyncio

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.connection_table import ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


async def _seed():
    """Seed a powerbi (shared-catalog, user_required+oauth) data source whose
    canonical catalog is populated (SP-seeded at creation), owned by `owner`.
    NEITHER user has a delegated token yet — this is the moment right after
    connection + agent creation, before any sign-in."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"OBO Creator Org {suffix}")
        db.add(org)
        await db.flush()

        owner = User(
            name="Creating Admin",
            email=f"owner-{suffix}@example.com",
            hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True,
        )
        member = User(
            name="Plain Member",
            email=f"member-{suffix}@example.com",
            hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True,
        )
        db.add_all([owner, member])
        await db.flush()

        conn = Connection(
            organization_id=org.id,
            name=f"PowerBI {suffix}",
            type="powerbi",
            config={},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
        )
        conn.encrypt_credentials({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
        db.add(conn)
        await db.flush()

        ds = DataSource(
            name=f"PBI DS {suffix}",
            organization_id=org.id,
            is_active=True,
            owner_user_id=owner.id,
        )
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id,
        ))

        # Canonical catalog: SP-seeded at connection creation.
        for tname in ("SalesPush/Sales", "SalesPush/Customers"):
            ct = ConnectionTable(
                name=tname,
                connection_id=conn.id,
                columns=[{"name": "id", "dtype": "int"}],
                pks=[], fks=[], no_rows=0,
                metadata_json={},
            )
            db.add(ct)
            await db.flush()
            db.add(DataSourceTable(
                name=tname,
                datasource_id=ds.id,
                connection_table_id=ct.id,
                is_active=False,  # nothing selected yet — mid agent creation
                metadata_json={},
                columns=[{"name": "id", "dtype": "int"}],
                pks=[], fks=[], no_rows=0,
            ))

        await db.commit()
        return {
            "org_id": org.id,
            "ds_id": ds.id,
            "conn_id": conn.id,
            "owner_id": owner.id,
            "member_id": member.id,
        }


async def _paginated_total(ids, user_id):
    svc = DataSourceService()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, user_id)
        resp = await svc.get_data_source_schema_paginated(
            db=db,
            data_source_id=ids["ds_id"],
            organization=org,
            page=1,
            page_size=100,
            include_inactive=True,
            current_user=user,
        )
        return resp.total_tables, len(resp.tables)


class _FakePBIClient:
    """Stands in for the live Power BI client on the reload path."""
    async def aget_schemas(self, progress_callback=None, prior_catalog=None):
        return [
            {"name": "SalesPush/Sales", "columns": [{"name": "id", "dtype": "int"}],
             "pks": [], "fks": [], "metadata_json": {}},
            {"name": "SalesPush/Customers", "columns": [{"name": "id", "dtype": "int"}],
             "pks": [], "fks": [], "metadata_json": {}},
        ]


@pytest.mark.e2e
def test_tokenless_owner_sees_canonical_catalog_member_fails_closed():
    """Display scoping: before any sign-in, the owner/admin sees the canonical
    catalog in the tables selector; a plain member sees zero (fail-closed)."""
    ids = _run(_seed())

    owner_total, owner_rows = _run(_paginated_total(ids, ids["owner_id"]))
    member_total, member_rows = _run(_paginated_total(ids, ids["member_id"]))
    print(f"\n[display] token-less owner sees total={owner_total} rows={owner_rows}; "
          f"token-less member sees total={member_total} rows={member_rows}")

    assert owner_total == 2 and owner_rows == 2, (
        "owner/admin must see the canonical catalog before first sign-in "
        "(it is already visible to them via connection management)"
    )
    assert member_total == 0 and member_rows == 0, (
        "plain member without a token must stay fail-closed"
    )


@pytest.mark.e2e
def test_tokenless_owner_reload_uses_system_creds_not_403(monkeypatch):
    """Reload: refresh_data_source_schema as a token-less owner must index with
    the connection's system creds (background-indexer identity) and return the
    canonical catalog — not raise 403 'Connect required'."""
    from app.services.connection_service import ConnectionService

    async def _fake_construct_client(self, db, connection, current_user=None, **kw):
        # The fix routes token-less callers to the system identity; a per-user
        # resolve here would raise 403 for a token-less caller.
        assert current_user is None, (
            "refresh_schema must index with the system identity (current_user=None) "
            "when the caller has no per-user credentials"
        )
        return _FakePBIClient()

    monkeypatch.setattr(ConnectionService, "construct_client", _fake_construct_client)

    ids = _run(_seed())

    async def _reload():
        svc = DataSourceService()
        async with async_session_maker() as db:
            org = await db.get(Organization, ids["org_id"])
            user = await db.get(User, ids["owner_id"])
            return await svc.refresh_data_source_schema(
                db=db,
                data_source_id=ids["ds_id"],
                organization=org,
                current_user=user,
            )

    schemas = _run(_reload())
    names = sorted(s.name for s in (schemas or []))
    print(f"\n[reload] token-less owner reload returned {len(names)} tables: {names}")
    assert names == ["SalesPush/Customers", "SalesPush/Sales"]
