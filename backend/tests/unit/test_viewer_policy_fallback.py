"""DS-less reports must not slip past snapshot withholding.

Chat-created reports carry no report<->data-source association — execution
falls back to the org's data sources — so the withholding policy has to
evaluate that same fallback, filtered to the sources the step's code
addresses by client key.
"""

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.services.viewer_data_policy import (
    snapshot_withheld_for_viewers,
    sources_credential_scoped,
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed(db):
    org = Organization(name="o")
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add_all([org, user])
    await db.flush()
    report = Report(
        title="r", user_id=str(user.id), organization_id=str(org.id),
        slug="r1", status="published", shared_run_identity="viewer",
    )
    db.add(report)
    await db.flush()

    def make_ds(name, conn_name, auth_policy):
        conn = Connection(
            name=conn_name, type="powerbi" if auth_policy != "system_only" else "sqlite",
            config="{}", organization_id=str(org.id), is_active=True,
            auth_policy=auth_policy,
        )
        ds = DataSource(name=name, organization_id=str(org.id), owner_user_id=str(user.id))
        ds.connections.append(conn)
        db.add_all([conn, ds])
        return ds

    delegated = make_ds("PowerBI RLS", "powerbi-1", "user_required")
    plain = make_ds("Membership DB", "sqlite-1", "system_only")
    await db.flush()
    return org, report, delegated, plain


@pytest.mark.asyncio
async def test_dsless_report_code_naming_delegated_source_withholds(db):
    org, report, delegated, plain = await _seed(db)
    code = "def generate_df(ds_clients, excel_files):\n    ds_clients['PowerBI RLS:powerbi-1'].execute_query('EVALUATE sales')"
    assert await snapshot_withheld_for_viewers(
        db, str(report.id), "viewer",
        fallback_org_id=str(org.id), code=code,
    ) is True


@pytest.mark.asyncio
async def test_dsless_report_code_naming_system_source_is_served(db):
    org, report, delegated, plain = await _seed(db)
    code = "def generate_df(ds_clients, excel_files):\n    ds_clients['Membership DB:sqlite-1'].execute_query('select 1')"
    assert await snapshot_withheld_for_viewers(
        db, str(report.id), "viewer",
        fallback_org_id=str(org.id), code=code,
    ) is False


@pytest.mark.asyncio
async def test_dsless_report_no_code_withholds_conservatively(db):
    org, report, delegated, plain = await _seed(db)
    assert await snapshot_withheld_for_viewers(
        db, str(report.id), "viewer",
        fallback_org_id=str(org.id), code=None,
    ) is True


@pytest.mark.asyncio
async def test_no_fallback_org_keeps_legacy_behavior(db):
    org, report, delegated, plain = await _seed(db)
    assert await snapshot_withheld_for_viewers(db, str(report.id), "viewer") is False


@pytest.mark.asyncio
async def test_credential_scoped_follows_code_named_source(db):
    org, report, delegated, plain = await _seed(db)
    memo = {}
    assert await sources_credential_scoped(
        db, str(report.id), fallback_org_id=str(org.id),
        code="ds_clients['PowerBI RLS:powerbi-1']", memo=memo,
    ) is True
    assert await sources_credential_scoped(
        db, str(report.id), fallback_org_id=str(org.id),
        code="ds_clients['Membership DB:sqlite-1']", memo=memo,
    ) is False
    # memo keyed per (report, code) — both answers cached
    assert len([k for k in memo if isinstance(k, tuple)]) == 2
