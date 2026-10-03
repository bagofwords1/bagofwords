"""An indexing run must never stay "in flight" forever.

A run lives only in the process that started it. When that process died
mid-run (restart, deploy, OOM) its row stayed `running` with nothing behind it:
every Reindex handed back the dead row, the scheduled sweeper was blocked, and
the UI showed a job that never finished. Separately, a discovery call that hung
on the source held its concurrency slot indefinitely, so a few hung runs queued
every other connection's indexing behind them.

The contract pinned here:
  - a non-terminal run whose runner stopped heartbeating is failed when read,
    and a new run can start;
  - a live run is never reaped, however long its discovery takes;
  - a stage that exceeds its ceiling fails the run and frees its slot;
  - discovery that stops reporting progress is stopped after the idle window,
    while discovery that keeps reporting runs past it;
  - while a run is stuck, the row shows the stage it is stuck IN and when it
    last made progress — enough to diagnose it from a screenshot.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import main  # noqa: F401 — registers all mappers
from app.models.base import BaseSchema
from app.models.connection import Connection
from app.models.connection_indexing import ConnectionIndexing, ConnectionIndexingStatus
from app.models.organization import Organization
from app.services import connection_indexing_service as cis
from app.services.connection_indexing_service import ConnectionIndexingService
from app.services.connection_service import ConnectionService


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    # A file database: the runner and heartbeat open their own engines, and
    # they must see the same data as the test's session.
    url = f"sqlite+aiosqlite:///{tmp_path / 'indexing.db'}"
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.run_sync(BaseSchema.metadata.create_all)
    monkeypatch.setattr(
        "app.settings.database.create_async_database_engine_for_indexing",
        lambda: create_async_engine(url),
    )
    # The gate is process-global; give each test its own.
    monkeypatch.setattr(cis, "_indexing_gate", None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with maker() as db:
        org = Organization(name="o")
        db.add(org)
        await db.flush()
        connection = Connection(
            name="warehouse", type="postgresql", config={}, is_active=True,
            auth_policy="system_only", organization_id=str(org.id),
        )
        db.add(connection)
        await db.commit()

    yield maker, connection
    await engine.dispose()


async def _row(maker, indexing_id) -> ConnectionIndexing:
    async with maker() as db:
        return await db.get(ConnectionIndexing, indexing_id)


@pytest.mark.asyncio
async def test_orphaned_running_row_is_failed_and_a_new_run_can_start(env):
    maker, connection = env
    svc = ConnectionIndexingService()
    silent_since = datetime.utcnow() - timedelta(seconds=cis._STALE_AFTER_SECONDS + 60)
    async with maker() as db:
        orphan = ConnectionIndexing(
            connection_id=str(connection.id),
            status=ConnectionIndexingStatus.RUNNING.value,
            started_at=silent_since, updated_at=silent_since,
        )
        db.add(orphan)
        await db.commit()

        assert await svc.get_active(db, str(connection.id)) is None

        fresh = await svc.start(db=db, connection=connection, kick_off=False)
        assert str(fresh.id) != str(orphan.id)
        assert fresh.status == ConnectionIndexingStatus.PENDING.value

    reaped = await _row(maker, orphan.id)
    assert reaped.status == ConnectionIndexingStatus.FAILED.value
    assert reaped.finished_at is not None
    assert reaped.error


@pytest.mark.asyncio
async def test_latest_run_reported_to_the_ui_is_not_left_running(env):
    maker, connection = env
    silent_since = datetime.utcnow() - timedelta(seconds=cis._STALE_AFTER_SECONDS + 60)
    async with maker() as db:
        db.add(ConnectionIndexing(
            connection_id=str(connection.id),
            status=ConnectionIndexingStatus.PENDING.value,
            updated_at=silent_since,
        ))
        await db.commit()
        latest = await ConnectionIndexingService().get_latest(db, str(connection.id))
    assert latest.status == ConnectionIndexingStatus.FAILED.value


@pytest.mark.asyncio
async def test_recently_active_run_is_not_reaped(env):
    maker, connection = env
    async with maker() as db:
        row = ConnectionIndexing(
            connection_id=str(connection.id),
            status=ConnectionIndexingStatus.RUNNING.value,
            started_at=datetime.utcnow() - timedelta(hours=3),
            updated_at=datetime.utcnow(),
        )
        db.add(row)
        await db.commit()
        active = await ConnectionIndexingService().get_active(db, str(connection.id))
    assert active is not None and str(active.id) == str(row.id)
    assert active.status == ConnectionIndexingStatus.RUNNING.value


@pytest.mark.asyncio
async def test_live_run_outlasting_the_stale_window_is_never_reaped(env, monkeypatch):
    maker, connection = env
    monkeypatch.setattr(cis, "_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(cis, "_STALE_AFTER_SECONDS", 0.5)

    async def slow_discovery(self, **kwargs):
        await asyncio.sleep(1.5)  # three stale windows
        return []

    monkeypatch.setattr(ConnectionService, "refresh_schema", slow_discovery)
    svc = ConnectionIndexingService()
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)

    run = asyncio.create_task(svc._run(str(row.id)))
    try:
        # Poll on ONE session, the way `wait_for_active` does.
        async with maker() as poll_db:
            while not run.done():
                if await svc.get_active(poll_db, str(connection.id)) is None:
                    # Only acceptable once the run has really finished.
                    break
                await asyncio.sleep(0.1)
    finally:
        await asyncio.wait_for(run, timeout=10)

    assert (await _row(maker, row.id)).status == ConnectionIndexingStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_hung_discovery_times_out_and_frees_its_slot(env, monkeypatch):
    maker, connection = env
    monkeypatch.setattr(cis, "_INDEXING_CONCURRENCY", 1)
    monkeypatch.setattr(cis, "_DISCOVERY_TIMEOUT_SECONDS", 0.3)
    calls = {"n": 0}

    async def discovery(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(3600)  # the source never answers
        return []

    monkeypatch.setattr(ConnectionService, "refresh_schema", discovery)
    monkeypatch.setattr(ConnectionService, "construct_client", _construct_client)
    svc = ConnectionIndexingService()

    async with maker() as db:
        hung = await svc.start(db=db, connection=connection, kick_off=False)
    await asyncio.wait_for(svc._run(str(hung.id)), timeout=10)

    hung_row = await _row(maker, hung.id)
    assert hung_row.status == ConnectionIndexingStatus.FAILED.value
    assert hung_row.error

    # With a single slot, this only completes if the timed-out run released it.
    async with maker() as db:
        nxt = await svc.start(db=db, connection=connection, kick_off=False)
    await asyncio.wait_for(svc._run(str(nxt.id)), timeout=10)
    assert (await _row(maker, nxt.id)).status == ConnectionIndexingStatus.COMPLETED.value


class _Client:
    async def awarm_all(self, **kwargs):
        return None

    def index_stats(self):
        return {}


async def _construct_client(self, *args, **kwargs):
    return _Client()


@pytest.mark.asyncio
async def test_discovery_that_stops_reporting_is_stopped_after_the_idle_window(env, monkeypatch):
    maker, connection = env
    monkeypatch.setattr(cis, "_IDLE_TIMEOUT_SECONDS", 2.0)
    stuck_since = {}

    async def discovery(self, *, progress_callback=None, **kwargs):
        for i in range(5):
            progress_callback("columns", f"dbo.t{i}", i + 1, 5)
            await asyncio.sleep(0.05)
        progress_callback("saving", None, 0, 0)
        stuck_since["t"] = datetime.utcnow()
        await asyncio.sleep(3600)  # stuck, silently

    monkeypatch.setattr(ConnectionService, "refresh_schema", discovery)
    monkeypatch.setattr(ConnectionService, "construct_client", _construct_client)
    svc = ConnectionIndexingService()
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)

    run = asyncio.create_task(svc._run(str(row.id)))
    # While stuck (before the idle window closes) the row already names the
    # stage it is stuck in and when progress stopped.
    async with maker() as poll_db:
        for _ in range(60):  # well inside the idle window
            live = await svc.get_latest(poll_db, str(connection.id))
            if "t" in stuck_since and live.phase == "saving":
                break
            await asyncio.sleep(0.02)
    assert live.status == ConnectionIndexingStatus.RUNNING.value
    assert live.phase == "saving"
    assert live.last_activity_at is not None
    assert live.last_activity_at <= stuck_since["t"]

    await asyncio.wait_for(run, timeout=10)
    failed = await _row(maker, row.id)
    assert failed.status == ConnectionIndexingStatus.FAILED.value
    assert "saving" in failed.error


@pytest.mark.asyncio
async def test_discovery_that_keeps_reporting_outlives_the_idle_window(env, monkeypatch):
    maker, connection = env
    monkeypatch.setattr(cis, "_IDLE_TIMEOUT_SECONDS", 0.4)

    async def slow_but_progressing(self, *, progress_callback=None, **kwargs):
        for i in range(30):  # ~1.5s total, several idle windows
            progress_callback("datasets", f"model-{i}", i + 1, 30)
            await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr(ConnectionService, "refresh_schema", slow_but_progressing)
    monkeypatch.setattr(ConnectionService, "construct_client", _construct_client)
    svc = ConnectionIndexingService()
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)
    await asyncio.wait_for(svc._run(str(row.id)), timeout=10)

    assert (await _row(maker, row.id)).status == ConnectionIndexingStatus.COMPLETED.value
