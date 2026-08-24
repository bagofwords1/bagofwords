"""refresh_on_view_rerun must not run under an active agent run."""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.organization import Organization
from app.models.user import User
from app.models.report import Report
from app.models.completion import Completion
from app.services.report_service import ReportService


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed(db, *, last_run_age_minutes=60):
    org = Organization(name="o")
    user = User(name="u", email="u@x.com", hashed_password="x")
    db.add_all([org, user])
    await db.flush()
    report = Report(
        title="r", user_id=str(user.id), organization_id=str(org.id),
        slug="r1", status="published", refresh_on_view=True,
        last_run_at=datetime.utcnow() - timedelta(minutes=last_run_age_minutes),
    )
    db.add(report)
    await db.flush()
    return user, report


def _completion(report, status):
    return Completion(
        prompt={"content": "x"}, completion={"content": ""}, model="m",
        report_id=str(report.id), role="system", status=status, turn_index=0,
        # Model metadata drifts from live DDL here (sigkill is nullable in real
        # DBs); create_all enforces NOT NULL, so supply a datetime.
        sigkill=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_skips_while_agent_run_in_progress(db):
    user, report = await _seed(db)
    db.add(_completion(report, "in_progress"))
    await db.flush()
    res = await ReportService().refresh_on_view_rerun(db, str(report.id), user=user)
    assert res["skipped"] is True
    assert "agent run in progress" in res["message"]


@pytest.mark.asyncio
async def test_finished_run_does_not_block(db):
    """A completed run must not trip the guard — the next gate (staleness,
    with a fresh last_run_at) answers instead."""
    user, report = await _seed(db, last_run_age_minutes=0)
    db.add(_completion(report, "success"))
    await db.flush()
    res = await ReportService().refresh_on_view_rerun(db, str(report.id), user=user)
    assert res["skipped"] is True
    assert "data is fresh" in res["message"]


@pytest.mark.asyncio
async def test_stale_orphaned_run_does_not_block_forever(db):
    """An in_progress row older than the age bound is treated as orphaned."""
    user, report = await _seed(db, last_run_age_minutes=0)
    c = _completion(report, "in_progress")
    db.add(c)
    await db.flush()
    c.created_at = datetime.utcnow() - timedelta(hours=2)
    db.add(c)
    await db.flush()
    res = await ReportService().refresh_on_view_rerun(db, str(report.id), user=user)
    assert res["skipped"] is True
    assert "data is fresh" in res["message"]  # passed the agent guard
