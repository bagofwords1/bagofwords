"""Fix validation: file agents don't list their documents under "Tables".

The tables selector asks `get_data_source_schema_paginated` to drop file-source
rows (`exclude_file_source_types=True`). That exclusion recognises a file row by
following its `connection_table_id` to the connection type — and deliberately
KEEPS rows with no link, because legacy name-keyed rows from the old
`save_or_update_tables` path are genuine tables.

Per-user file catalogs (OneDrive, Outlook, personal Drive) have no shared
`ConnectionTable` at all, so every one of their rows is unlinked and slipped
through: a OneDrive agent listed its 14 documents under **Tables**, while
SharePoint (whose rows ARE linked) was correctly hidden.

Now, when every connection on the data source is a file source, unlinked rows
are excluded too — mixed agents keep the legacy allowance.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      python -m pytest tests/e2e/test_file_agent_tables_view.py -v -s
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
from app.models.organization import Organization
from app.models.user import User
from app.services.data_source_service import DataSourceService


def _run(coro):
    return asyncio.run(coro)


async def _seed(conn_type, *, linked: bool, extra_conn_type=None):
    """A data source with one file connection whose rows are linked to a shared
    catalog (SharePoint-style) or unlinked (OneDrive-style)."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Files Org {suffix}")
        db.add(org)
        await db.flush()
        owner = User(name="Owner", email=f"owner-{suffix}@example.com", hashed_password="x",
                     is_active=True, is_superuser=False, is_verified=True)
        db.add(owner)
        await db.flush()

        ds = DataSource(name=f"Agent {suffix}", organization_id=org.id, is_active=True,
                        owner_user_id=owner.id)
        db.add(ds)
        await db.flush()

        for ctype in filter(None, [conn_type, extra_conn_type]):
            conn = Connection(organization_id=org.id, name=f"{ctype} {suffix}", type=ctype,
                              config={}, auth_policy="system_only")
            db.add(conn)
            await db.flush()
            await db.execute(domain_connection.insert().values(
                data_source_id=ds.id, connection_id=conn.id))

            for i in range(3):
                ct_id = None
                if linked and ctype == conn_type:
                    ct = ConnectionTable(name=f"{ctype}-doc-{i}", connection_id=conn.id,
                                         columns=[], pks=[], fks=[], no_rows=0)
                    db.add(ct)
                    await db.flush()
                    ct_id = ct.id
                db.add(DataSourceTable(
                    name=f"{ctype}-doc-{i}", datasource_id=ds.id, connection_table_id=ct_id,
                    is_active=True, columns=[], pks=[], fks=[], no_rows=0))
        await db.commit()
        return {"org_id": org.id, "ds_id": ds.id, "owner_id": owner.id}


async def _tables_view_total(ids):
    """What the Tables selector shows (the route always excludes file sources)."""
    svc = DataSourceService()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org_id"])
        user = await db.get(User, ids["owner_id"])
        resp = await svc.get_data_source_schema_paginated(
            db=db, data_source_id=ids["ds_id"], organization=org, page=1, page_size=50,
            include_inactive=True, current_user=user, exclude_file_source_types=True,
        )
        return resp.total_tables


@pytest.mark.e2e
def test_per_user_file_agent_shows_no_tables():
    """OneDrive-style: unlinked per-user file rows must not appear as tables."""
    ids = _run(_seed("onedrive", linked=False))
    total = _run(_tables_view_total(ids))
    print(f"\n[onedrive] tables view total={total}")
    assert total == 0, "per-user file rows belong in Files, not Tables"


@pytest.mark.e2e
def test_shared_catalog_file_agent_shows_no_tables():
    """SharePoint-style (linked rows) — unchanged, still hidden."""
    ids = _run(_seed("sharepoint", linked=True))
    total = _run(_tables_view_total(ids))
    print(f"[sharepoint] tables view total={total}")
    assert total == 0


@pytest.mark.e2e
def test_outlook_agent_shows_no_tables():
    ids = _run(_seed("outlook_mail", linked=False))
    assert _run(_tables_view_total(ids)) == 0


@pytest.mark.e2e
def test_real_tables_are_untouched():
    """A non-file source keeps showing its tables (no over-filtering)."""
    ids = _run(_seed("postgresql", linked=True))
    total = _run(_tables_view_total(ids))
    print(f"[postgresql] tables view total={total}")
    assert total == 3


@pytest.mark.e2e
def test_mixed_agent_keeps_unlinked_legacy_rows():
    """File connection + a real DB connection: the legacy unlinked-row allowance
    must survive, or genuinely table-shaped legacy rows would vanish."""
    ids = _run(_seed("postgresql", linked=False, extra_conn_type="onedrive"))
    total = _run(_tables_view_total(ids))
    print(f"[mixed] tables view total={total}")
    assert total == 6, "mixed agents keep unlinked rows (legacy behavior)"
