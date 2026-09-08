"""Query-target metadata (table name → Power BI dataset GUID) reaches the
client ONLY for activated canonical rows, and the executing user's overlay can
enrich an activated row but never make a non-activated one resolvable.

Activation is the agent manager's table selection and it has to hold at query
time, not only in the prompt: with an unfiltered map a generated DAX query
naming a table the manager left out still resolved its dataset GUID and ran.
A user-discovered delegated dataset gets its canonical row on overlay sync
(`_upsert_user_overlay`), so the manager activates it in the tables wizard
exactly like a service-principal-indexed table.

The NOT-activated rows are handed to clients exposing
`attach_blocked_table_metadata` so a DAX body cannot reach a sibling table of
the same dataset either (Power BI addresses a whole dataset per query).
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
        self.blocked = None

    def attach_table_metadata(self, tables):
        self.attached = tables

    def attach_blocked_table_metadata(self, tables):
        self.blocked = tables


class _LegacyClient:
    """A client without the blocked-table hook (Analysis Services)."""
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


META = {"powerbi": {"datasetId": "443cb0b4", "workspaceId": "ws-1", "tableName": "Sales"}}
OVERLAY_META = {"powerbi": {"datasetId": "443cb0b4", "workspaceId": "ws-user", "tableName": "Sales"}}


async def _seed(db, *, canonical_active, overlay_meta=OVERLAY_META):
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
        metadata_json=META,
    )
    db.add(canon)
    ot = UserDataSourceTable(
        data_source_id=str(ds.id), user_id=str(user.id),
        table_name="rls_sales/Sales", is_accessible=True, status="accessible",
        metadata_json=overlay_meta,
    )
    db.add(ot)
    await db.flush()
    return org, user, ds, conn


@pytest.mark.asyncio
async def test_inactive_canonical_is_not_a_query_target(db):
    org, user, ds, conn = await _seed(db, canonical_active=False)
    client = _FakeClient()
    await DataSourceService()._attach_stored_table_metadata(
        db, client, ds, conn, current_user=user)
    assert {t["name"] for t in (client.attached or [])} == set(), (
        "non-activated table (accessible to the user upstream) must not resolve")
    assert {t["name"] for t in (client.blocked or [])} == {"rls_sales/Sales"}


@pytest.mark.asyncio
async def test_active_canonical_is_attached_and_overlay_enriches_it(db):
    org, user, ds, conn = await _seed(db, canonical_active=True)
    client = _FakeClient()
    await DataSourceService()._attach_stored_table_metadata(
        db, client, ds, conn, current_user=user)
    names = {t["name"] for t in (client.attached or [])}
    assert names == {"rls_sales/Sales"}
    meta = next(t["metadata_json"] for t in client.attached if t["name"] == "rls_sales/Sales")
    # Overlay values win for an activated row: discovered under this user's creds.
    assert meta["powerbi"]["workspaceId"] == "ws-user"
    assert meta["powerbi"]["datasetId"] == "443cb0b4"
    assert client.blocked == []


@pytest.mark.asyncio
async def test_overlay_only_table_without_canonical_row_is_not_a_query_target(db):
    """No canonical row at all → nothing was activated → not resolvable.
    (The next overlay sync creates the row; the wizard activates it.)"""
    org, user, ds, conn = await _seed(db, canonical_active=False)
    await db.execute(DataSourceTable.__table__.delete().where(
        DataSourceTable.datasource_id == str(ds.id)))
    await db.flush()
    client = _FakeClient()
    await DataSourceService()._attach_stored_table_metadata(
        db, client, ds, conn, current_user=user)
    assert {t["name"] for t in (client.attached or [])} == set()


@pytest.mark.asyncio
async def test_client_without_blocked_hook_still_gets_active_map(db):
    org, user, ds, conn = await _seed(db, canonical_active=True)
    client = _LegacyClient()
    await DataSourceService()._attach_stored_table_metadata(
        db, client, ds, conn, current_user=user)
    assert {t["name"] for t in (client.attached or [])} == {"rls_sales/Sales"}
