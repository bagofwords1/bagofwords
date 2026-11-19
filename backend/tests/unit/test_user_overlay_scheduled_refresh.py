"""A delegated user's overlay follows the connection's reindex schedule.

The scheduled reindex refreshes only the shared catalog, with no user identity.
A user on a user_required connection reads their own overlay, which only their
own credentials can rebuild, so before this it stayed at whatever the source
looked like when they first connected.

The contract pinned here, for `kick_stale_user_overlays` (called when a user's
overlay is read for a prompt):
  - an overlay never synced for this user, or synced longer ago than the
    connection's schedule, gets one background sync scoped to THAT user;
  - one synced within the schedule does not, and neither does a second read
    while a sync is already pending;
  - neither the shared refresh nor another user's sync counts as this user's;
  - the same gates as the sweep apply: the license feature and the
    connection's auto_reindex_enabled.
"""
import uuid
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.connection import Connection
from app.models.connection_indexing import ConnectionIndexing, ConnectionIndexingStatus
from app.models.organization import Organization
from app.services.connection_indexing_service import ConnectionIndexingService
from app.services.scheduled_reindex import kick_stale_user_overlays


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    url = f"sqlite+aiosqlite:///{tmp_path / 'overlay.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.dependencies.async_session_maker", maker)
    monkeypatch.setattr("app.ee.license.has_feature", lambda feature: True)

    # The runner crawls the upstream source with the user's token — the
    # external boundary. What is under test is whether a run is started.
    async def _no_run(self, indexing_id):
        return None
    monkeypatch.setattr(ConnectionIndexingService, "_run", _no_run)

    async with maker() as db:
        org = Organization(name="o")
        db.add(org)
        await db.flush()
        connection = Connection(
            name="bi", type="postgresql", config={}, is_active=True,
            auth_policy="user_required", organization_id=str(org.id),
            auto_reindex_enabled=True, reindex_interval_hours=6,
        )
        db.add(connection)
        await db.commit()

    yield maker, connection
    await engine.dispose()


async def _user_runs(maker, connection, user_id):
    async with maker() as db:
        return (await db.execute(
            select(ConnectionIndexing).where(
                ConnectionIndexing.connection_id == str(connection.id),
                ConnectionIndexing.user_id == user_id,
            )
        )).scalars().all()


async def _finished_run(maker, connection, user_id, hours_ago):
    # A past sync at a chosen age: no API produces a backdated run.
    started = datetime.utcnow() - timedelta(hours=hours_ago)
    async with maker() as db:
        db.add(ConnectionIndexing(
            connection_id=str(connection.id), user_id=user_id,
            status=ConnectionIndexingStatus.COMPLETED.value,
            started_at=started, finished_at=started + timedelta(minutes=1),
        ))
        await db.commit()


@pytest.mark.asyncio
async def test_never_synced_overlay_gets_one_sync_scoped_to_that_user(env):
    maker, connection = env
    user_id = str(uuid.uuid4())

    await kick_stale_user_overlays([connection], user_id)
    await kick_stale_user_overlays([connection], user_id)

    runs = await _user_runs(maker, connection, user_id)
    assert len(runs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interval_hours", [3, 12])
async def test_sync_follows_the_connection_schedule(env, interval_hours):
    maker, connection = env
    async with maker() as db:
        conn = await db.get(Connection, connection.id)
        conn.reindex_interval_hours = interval_hours
        await db.commit()
        connection = conn

    fresh_user, stale_user = str(uuid.uuid4()), str(uuid.uuid4())
    await _finished_run(maker, connection, fresh_user, hours_ago=interval_hours - 1)
    await _finished_run(maker, connection, stale_user, hours_ago=interval_hours + 1)

    await kick_stale_user_overlays([connection], fresh_user)
    await kick_stale_user_overlays([connection], stale_user)

    assert len(await _user_runs(maker, connection, fresh_user)) == 1
    assert len(await _user_runs(maker, connection, stale_user)) == 2


@pytest.mark.asyncio
async def test_shared_refresh_and_other_users_syncs_do_not_count(env):
    maker, connection = env
    user_id = str(uuid.uuid4())
    await _finished_run(maker, connection, None, hours_ago=0)
    await _finished_run(maker, connection, str(uuid.uuid4()), hours_ago=0)

    await kick_stale_user_overlays([connection], user_id)

    assert len(await _user_runs(maker, connection, user_id)) == 1


@pytest.mark.asyncio
async def test_unlicensed_starts_nothing(env, monkeypatch):
    maker, connection = env
    monkeypatch.setattr("app.ee.license.has_feature", lambda feature: False)
    user_id = str(uuid.uuid4())

    await kick_stale_user_overlays([connection], user_id)

    assert await _user_runs(maker, connection, user_id) == []


@pytest.mark.asyncio
async def test_auto_reindex_disabled_starts_nothing(env):
    maker, connection = env
    async with maker() as db:
        conn = await db.get(Connection, connection.id)
        conn.auto_reindex_enabled = False
        await db.commit()
        connection = conn
    user_id = str(uuid.uuid4())

    await kick_stale_user_overlays([connection], user_id)

    assert await _user_runs(maker, connection, user_id) == []
