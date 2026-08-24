"""mark_report_viewed must survive commit failures without touching
expired ORM attributes (the MissingGreenlet class: rollback expires the
session's instances, including current_user)."""

from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.report_view import ReportView
from app.services.report_service import ReportService


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    # expire_on_commit=True matches the app's session: commits/rollbacks
    # expire loaded instances, which is the failure precondition here.
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=True)
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
        slug="r1", status="published", report_type="regular",
    )
    db.add(report)
    await db.flush()
    ids = (str(user.id), str(org.id), str(report.id))
    await db.commit()
    return ids


def _fail_once_commit(db, exc):
    real_commit = db.commit
    state = {"failed": False}

    async def commit():
        if not state["failed"]:
            state["failed"] = True
            await db.rollback()
            raise exc
        await real_commit()

    db.commit = commit
    return state


@pytest.mark.asyncio
async def test_locked_commit_drops_watermark_instead_of_500(db):
    user_id, org_id, report_id = await _seed(db)
    # Reload so instances are expire-able session members.
    user = await db.get(User, user_id)
    org = await db.get(Organization, org_id)
    _fail_once_commit(db, OperationalError("stmt", {}, Exception("database is locked")))
    res = await ReportService().mark_report_viewed(db, report_id, user, org)
    assert res["id"] == report_id
    assert "viewed_at" in res


@pytest.mark.asyncio
async def test_insert_race_retries_as_update_with_expired_user(db):
    user_id, org_id, report_id = await _seed(db)
    # The row the "other tab" already inserted.
    db.add(ReportView(report_id=report_id, user_id=user_id,
                      last_viewed_at=datetime(2020, 1, 1)))
    await db.commit()
    user = await db.get(User, user_id)
    org = await db.get(Organization, org_id)
    _fail_once_commit(db, IntegrityError("stmt", {}, Exception("unique")))
    res = await ReportService().mark_report_viewed(db, report_id, user, org)
    assert "viewed_at" in res
    row = (await db.execute(
        ReportView.__table__.select().where(ReportView.report_id == report_id)
    )).mappings().first()
    assert row["last_viewed_at"].year >= 2025
