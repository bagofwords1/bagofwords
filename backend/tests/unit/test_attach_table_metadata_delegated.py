"""Query-target metadata (table name → Power BI dataset GUID) must reach the
client even when the canonical catalog row is inactive, and must include the
executing user's overlay.

Confirmed against a live Power BI verify-rls dataset: a shared dashboard's
per-viewer run failed with "Could not resolve Power BI dataset for table
'rls_sales/Sales'". The canonical DataSourceTable carried the correct datasetId
but was is_active=False (the service principal that indexes the catalog has no
access to a delegated dataset), and _attach_stored_table_metadata filtered on
is_active==True — so the resolution map was empty and every viewer run failed.

The map only resolves a query target; access is still enforced by the delegated
token at executeQueries time. So it must include inactive canonical rows and the
executing user's overlay rows.
"""
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.models.datasource_table import DataSourceTable
from app.models.user_data_source_overlay import UserDataSourceTable
from app.services.data_source_service import DataSourceService


class _FakeClient:
    def __init__(self):
        self.attached = None

    def attach_table_metadata(self, tables):
        self.attached = tables


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed(db, *, canonical_active):
    org = Organization(name="o")
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add_all([org, user])
    await db.flush()
    ds = DataSource(name="PowerBI verify-rls", organization_id=str(org.id), is_active=True)
    db.add(ds)
    await db.flush()
    conn = Connection(name="c", type="powerbi", config={}, is_active=True,
                      auth_policy="user_required", organization_id=str(org.id))
    conn.data_sources.append(ds)
    db.add(conn)
    canon = DataSourceTable(
        name="rls_sales/Sales", datasource_id=str(ds.id), is_active=canonical_active,
        columns=[{"name": "id", "dtype": "Integer"}], pks=[], fks=[],
        metadata_json={"powerbi": {"datasetId": "443cb0b4", "workspaceId": "ws-1"}},
    )
    db.add(canon)
    ot = UserDataSourceTable(
        data_source_id=str(ds.id), user_id=str(user.id),
        table_name="rls_sales/Sales", is_accessible=True, status="accessible",
        metadata_json={"powerbi": {"datasetId": "443cb0b4", "workspaceId": "ws-1"}},
    )
    db.add(ot)
    await db.flush()
    return org, user, ds, conn


@pytest.mark.asyncio
async def test_inactive_canonical_metadata_still_attached(db):
    org, user, ds, conn = await _seed(db, canonical_active=False)
    client = _FakeClient()
    await DataSourceService()._attach_stored_table_metadata(
        db, client, ds, conn, current_user=user)
    names = {t["name"] for t in (client.attached or [])}
    assert "rls_sales/Sales" in names, (
        "delegated table's dataset-GUID metadata dropped because canonical is inactive")
    meta = next(t["metadata_json"] for t in client.attached if t["name"] == "rls_sales/Sales")
    assert (meta or {}).get("powerbi", {}).get("datasetId") == "443cb0b4"


@pytest.mark.asyncio
async def test_overlay_only_table_is_attached(db):
    """A delegated dataset with no canonical row at all — only the user's
    overlay carries the GUID — must still resolve."""
    org, user, ds, conn = await _seed(db, canonical_active=False)
    # Remove the canonical row entirely; overlay is the sole source.
    canon = (await db.execute(
        DataSourceTable.__table__.select().where(
            DataSourceTable.datasource_id == str(ds.id))
    )).first()
    await db.execute(DataSourceTable.__table__.delete().where(
        DataSourceTable.datasource_id == str(ds.id)))
    await db.flush()
    client = _FakeClient()
    await DataSourceService()._attach_stored_table_metadata(
        db, client, ds, conn, current_user=user)
    names = {t["name"] for t in (client.attached or [])}
    assert "rls_sales/Sales" in names
