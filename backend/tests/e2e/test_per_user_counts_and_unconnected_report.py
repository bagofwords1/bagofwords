"""Two fixes found in the Entra sso_only / OBO e2e re-run (2026-07-26).

1. ``selected_count`` in the paginated tables response was computed over the
   whole data source while ``total_tables`` was scoped to the caller's per-user
   overlay, so a restricted user's tables panel rendered "2/1 active" — an
   org-wide numerator over a per-user denominator.

2. ``create_report`` correctly refuses to attach a ``user_required`` agent the
   caller cannot use, but discarded the list of what it dropped. The user got a
   report with no data source, a permanently disabled send button, and nothing
   explaining why. The dropped names now come back on the schema.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_per_user_counts_and_unconnected_report.py -v -s
"""
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.domain_connection import domain_connection
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_data_source_overlay import UserDataSourceTable
from app.schemas.report_schema import ReportCreate
from app.services.data_source_service import DataSourceService
from app.services.report_service import ReportService


def _run(coro):
    return asyncio.run(coro)


async def _seed():
    """One user_required+oauth agent with two canonical tables, a member whose
    overlay only covers one of them, and a second member with no credentials."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Counts Org {suffix}")
        db.add(org)
        await db.flush()

        restricted = User(
            name="Restricted Member",
            email=f"restricted-{suffix}@example.com",
            hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True,
        )
        unconnected = User(
            name="Unconnected Member",
            email=f"unconnected-{suffix}@example.com",
            hashed_password="x",
            is_active=True, is_superuser=False, is_verified=True,
        )
        db.add_all([restricted, unconnected])
        await db.flush()
        db.add_all([
            Membership(organization_id=org.id, user_id=restricted.id, role="member"),
            Membership(organization_id=org.id, user_id=unconnected.id, role="member"),
        ])

        conn = Connection(
            organization_id=org.id,
            name=f"Fabric {suffix}",
            type="ms_fabric",
            config={},
            auth_policy="user_required",
            allowed_user_auth_modes=["oauth"],
        )
        conn.encrypt_credentials({"tenant_id": "t", "client_id": "c", "client_secret": "s"})
        db.add(conn)
        await db.flush()

        ds = DataSource(
            name=f"Fabric Agent {suffix}",
            organization_id=org.id,
            is_active=True,
            is_public=True,
        )
        db.add(ds)
        await db.flush()
        await db.execute(domain_connection.insert().values(
            data_source_id=ds.id, connection_id=conn.id,
        ))

        ds_table_ids = {}
        for tname in ("dbo.sales", "dbo.finance"):
            ct = ConnectionTable(
                name=tname, connection_id=conn.id,
                columns=[{"name": "id", "dtype": "int"}],
                pks=[], fks=[], no_rows=0, metadata_json={},
            )
            db.add(ct)
            await db.flush()
            dst = DataSourceTable(
                name=tname, datasource_id=ds.id, connection_table_id=ct.id,
                is_active=True, metadata_json={},
                columns=[{"name": "id", "dtype": "int"}],
                pks=[], fks=[], no_rows=0,
            )
            db.add(dst)
            await db.flush()
            ds_table_ids[tname] = dst.id

        # The restricted member has connected their own identity (so the
        # per-user overlay is the authority for them) but that identity only
        # reaches dbo.sales — exactly the DENY-on-finance shape the live tenant
        # produced for demo2.
        ucc = UserConnectionCredentials(
            connection_id=conn.id,
            user_id=restricted.id,
            organization_id=org.id,
            auth_mode="oauth",
            is_active=True,
            is_primary=True,
        )
        ucc.encrypt_credentials({"access_token": "fake"})
        db.add(ucc)

        db.add(UserDataSourceTable(
            user_id=restricted.id,
            data_source_id=ds.id,
            data_source_table_id=ds_table_ids["dbo.sales"],
            table_name="dbo.sales",
            is_accessible=True,
        ))

        await db.commit()
        return {
            "org_id": org.id,
            "ds_id": ds.id,
            "restricted_id": restricted.id,
            "unconnected_id": unconnected.id,
        }


@pytest.mark.e2e
def test_selected_count_shares_the_user_overlay_scope_with_total():
    """"N/M active" must not mix an org-wide N with a per-user M."""
    ids = _run(_seed())

    async def _counts():
        async with async_session_maker() as db:
            org = await db.get(Organization, ids["org_id"])
            user = await db.get(User, ids["restricted_id"])
            return await DataSourceService().get_data_source_schema_paginated(
                db=db,
                data_source_id=ids["ds_id"],
                organization=org,
                page=1,
                page_size=100,
                include_inactive=True,
                current_user=user,
            )

    resp = _run(_counts())
    print(f"\n[counts] restricted member: selected_count={resp.selected_count} "
          f"total_tables={resp.total_tables} total={resp.total}")

    assert resp.total_tables == 1, "the restricted member sees one table"
    assert resp.selected_count <= resp.total_tables, (
        f"selected_count ({resp.selected_count}) must be scoped to the same "
        f"overlay as total_tables ({resp.total_tables}) — otherwise the UI "
        f"renders an impossible ratio like '2/1 active'"
    )
    assert resp.selected_count == 1


@pytest.mark.e2e
def test_create_report_reports_the_agents_it_could_not_attach():
    """A user_required agent the caller can't use is dropped AND named."""
    ids = _run(_seed())

    async def _create():
        async with async_session_maker() as db:
            org = await db.get(Organization, ids["org_id"])
            user = await db.get(User, ids["unconnected_id"])
            return await ReportService().create_report(
                db=db,
                report_data=ReportCreate(title="New report", data_sources=[ids["ds_id"]]),
                current_user=user,
                organization=org,
            )

    report = _run(_create())
    print(f"\n[report] attached={len(report.data_sources)} "
          f"unconnected={report.unconnected_data_sources}")

    assert len(report.data_sources) == 0, (
        "an agent the user has no credentials for must not be attached"
    )
    assert report.unconnected_data_sources, (
        "…but the caller must be told which agent was dropped, or the report is "
        "an unusable dead end with no explanation"
    )
    assert any("Fabric Agent" in n for n in report.unconnected_data_sources)
