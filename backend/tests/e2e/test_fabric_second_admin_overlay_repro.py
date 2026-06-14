"""
Reproduction: second admin sees NO tables on a shared-catalog, user-required
(Fabric / PowerBI / OBO) data source — and "Reload tables" does not fix it.

Scenario (mirrors the reported Entra ID / OBO bug):
  - Admin1 creates an ms_fabric connection (auth_policy=user_required,
    allowed_user_auth_modes=["oauth"], catalog_ownership=shared).
  - The canonical catalog (ConnectionTable -> DataSourceTable) is populated.
  - Admin1's per-user overlay (UserDataSourceTable) is populated (this happens
    for the creator via OBO auto-provision / first live fetch).
  - Admin2 ALSO has a valid delegated OBO token (a UserConnectionCredentials
    row, auth_mode="oauth"), so their effective_auth resolves to "user" and they
    CAN run queries — exactly the case the user pointed out.
  - Admin2 has NO per-user overlay rows yet.

Hypothesis being validated:
  1. The tables list (paginated endpoint) scopes a user_required source to the
     caller's UserDataSourceTable overlay when effective_auth == "user". With an
     empty overlay, Admin2 sees ZERO tables even though Admin1 sees all.
  2. "Reload tables" -> refresh_data_source_schema for a SHARED catalog only
     refreshes the canonical catalog (ConnectionTable/DataSourceTable) and never
     writes Admin2's overlay, so Admin2 still sees zero after reloading.

No live Azure/Fabric needed: the reload's connection-level schema fetch is
stubbed (the canonical catalog is already seeded). The bug is purely in the
app's overlay scoping + reload not populating the caller's overlay.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_fabric_second_admin_overlay_repro.py -v -s
"""
import uuid
import asyncio
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.connection import Connection
from app.models.data_source import DataSource
from app.models.connection_table import ConnectionTable
from app.models.datasource_table import DataSourceTable
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_data_source_overlay import UserDataSourceTable
from app.models.domain_connection import domain_connection
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


async def _seed(populate_admin2_overlay: bool = False):
    """Seed an ms_fabric (shared-catalog, user_required) data source with two
    token-holding admins. Admin1 has an overlay; Admin2 does not."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"OBO Org {suffix}")
        db.add(org)
        await db.flush()

        admin1 = User(
            name="Admin One",
            email=f"admin1-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        admin2 = User(
            name="Admin Two",
            email=f"admin2-{suffix}@example.com",
            hashed_password="x",
            is_active=True,
            is_superuser=False,
            is_verified=True,
        )
        db.add_all([admin1, admin2])
        await db.flush()

        # Fabric connection — delegated/OBO (user_required + oauth) => supports_user_token
        conn = Connection(
            organization_id=org.id,
            name=f"Fabric {suffix}",
            type="ms_fabric",
            config={"server_hostname": "example.datawarehouse.fabric.microsoft.com", "database": "demo_db"},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
        )
        conn.encrypt_credentials({
            "tenant_id": "t", "client_id": "c", "client_secret": "s",
        })
        db.add(conn)
        await db.flush()

        ds = DataSource(
            name=f"Fabric DS {suffix}",
            organization_id=org.id,
            is_active=True,
            owner_user_id=admin1.id,
        )
        db.add(ds)
        await db.flush()
        # link DS <-> Connection (M:N)
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id,
        ))

        # Canonical catalog: two tables (sales, finance)
        canonical_ds_table_ids = {}
        for tname in ("sales", "finance"):
            ct = ConnectionTable(
                name=tname,
                connection_id=conn.id,
                columns=[{"name": "id", "dtype": "int"}],
                pks=[], fks=[], no_rows=0,
                metadata_json={"schema": "dbo"},
            )
            db.add(ct)
            await db.flush()
            dst = DataSourceTable(
                name=tname,
                datasource_id=ds.id,
                connection_table_id=ct.id,
                is_active=True,
                metadata_json={"schema": "dbo"},
                # legacy non-null-on-downgrade columns (kept for sqlite teardown)
                columns=[{"name": "id", "dtype": "int"}],
                pks=[], fks=[], no_rows=0,
            )
            db.add(dst)
            await db.flush()
            canonical_ds_table_ids[tname] = dst.id

        # Both admins hold a valid delegated OBO token => effective_auth == "user"
        for u in (admin1, admin2):
            cred = UserConnectionCredentials(
                connection_id=conn.id,
                user_id=u.id,
                organization_id=org.id,
                auth_mode="oauth",
                is_active=True,
                is_primary=True,
                last_used_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(hours=1),
            )
            cred.encrypt_credentials({
                "access_token": "tok", "refresh_token": "r",
                "token_type": "Bearer",
            })
            db.add(cred)

        # Admin1 overlay: both tables accessible. (Creator got this via OBO
        # auto-provision / first live fetch.)
        for tname, dst_id in canonical_ds_table_ids.items():
            db.add(UserDataSourceTable(
                data_source_id=ds.id,
                user_id=admin1.id,
                table_name=tname,
                data_source_table_id=dst_id,
                is_accessible=True,
                status="accessible",
            ))

        # Admin2 overlay: optionally populate (used to show the fix's effect).
        if populate_admin2_overlay:
            for tname, dst_id in canonical_ds_table_ids.items():
                db.add(UserDataSourceTable(
                    data_source_id=ds.id,
                    user_id=admin2.id,
                    table_name=tname,
                    data_source_table_id=dst_id,
                    is_accessible=True,
                    status="accessible",
                ))

        await db.commit()
        return {
            "org_id": org.id,
            "ds_id": ds.id,
            "conn_id": conn.id,
            "admin1_id": admin1.id,
            "admin2_id": admin2.id,
        }


async def _get_user(db, user_id):
    return await db.get(User, user_id)


async def _get_org(db, org_id):
    return await db.get(Organization, org_id)


async def _paginated_total(ids, user_id):
    """Return total tables the paginated tables-selector endpoint shows `user_id`."""
    svc = DataSourceService()
    async with async_session_maker() as db:
        org = await _get_org(db, ids["org_id"])
        user = await _get_user(db, user_id)
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


async def _count_overlay(ids, user_id):
    from sqlalchemy import select, func
    async with async_session_maker() as db:
        res = await db.execute(
            select(func.count(UserDataSourceTable.id)).where(
                UserDataSourceTable.data_source_id == ids["ds_id"],
                UserDataSourceTable.user_id == user_id,
            )
        )
        return res.scalar() or 0


@pytest.mark.e2e
def test_second_admin_sees_no_tables_and_reload_does_not_help(monkeypatch):
    ids = _run(_seed())

    # CLAIM 1 — display scoping: admin1 sees both tables, admin2 sees none.
    a1_total, a1_len = _run(_paginated_total(ids, ids["admin1_id"]))
    a2_total, a2_len = _run(_paginated_total(ids, ids["admin2_id"]))
    print(f"\n[display] admin1 sees total={a1_total} rows={a1_len}; "
          f"admin2 sees total={a2_total} rows={a2_len}")

    assert a1_total == 2 and a1_len == 2, "admin1 (with overlay) should see both tables"
    assert a2_total == 0 and a2_len == 0, (
        "BUG REPRODUCED: admin2 has a valid OBO token (can run queries) but an "
        "empty overlay, so the tables selector shows ZERO tables"
    )

    # CLAIM 2 — reload doesn't populate admin2's overlay. Stub the connection
    # schema refresh (canonical catalog already seeded) and confirm the shared
    # catalog reload path never writes admin2's UserDataSourceTable rows.
    assert _run(_count_overlay(ids, ids["admin2_id"])) == 0

    from app.services.connection_service import ConnectionService

    async def _noop_refresh_schema(self, db, connection, current_user=None):
        return []  # simulate a successful canonical refresh, no overlay writes

    monkeypatch.setattr(ConnectionService, "refresh_schema", _noop_refresh_schema)

    async def _reload_as_admin2():
        svc = DataSourceService()
        async with async_session_maker() as db:
            org = await _get_org(db, ids["org_id"])
            user = await _get_user(db, ids["admin2_id"])
            return await svc.refresh_data_source_schema(
                db=db,
                data_source_id=ids["ds_id"],
                organization=org,
                current_user=user,
            )

    _run(_reload_as_admin2())

    overlay_after = _run(_count_overlay(ids, ids["admin2_id"]))
    a2_total_after, _ = _run(_paginated_total(ids, ids["admin2_id"]))
    print(f"[reload]  admin2 overlay rows after reload={overlay_after}; "
          f"admin2 tables after reload={a2_total_after}")

    assert overlay_after == 0, "reload must NOT have populated admin2's overlay"
    assert a2_total_after == 0, (
        "BUG REPRODUCED: after 'Reload tables', admin2 STILL sees zero tables — "
        "the shared-catalog reload refreshes only the canonical catalog"
    )


@pytest.mark.e2e
def test_control_admin2_with_overlay_sees_tables():
    """Control: if admin2 HAD an overlay (e.g. via OBO auto-provision), they'd
    see the tables. Confirms the scoping — not some unrelated failure — is what
    hides them in the bug case."""
    ids = _run(_seed(populate_admin2_overlay=True))
    a2_total, a2_len = _run(_paginated_total(ids, ids["admin2_id"]))
    print(f"\n[control] admin2 WITH overlay sees total={a2_total} rows={a2_len}")
    assert a2_total == 2 and a2_len == 2
