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
from types import SimpleNamespace

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


async def _until(condition, timeout=30):
    """Wait for something that is going to happen. The deadline only ends a
    test whose condition never comes true; no outcome depends on how soon."""
    async def poll():
        while not await condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


class _ServiceClock:
    """The service's clocks, moved only by the test.

    Liveness and idleness are decided by elapsed time. Measured on the real
    clock, a test can't tell a run that went silent from a machine that paused
    the test for a moment, so here only the test makes time pass.

    The two clocks move separately, because each deadline reads one of them:
    the stale-run reaper and the heartbeat read the wall clock, the discovery
    idle watchdog reads the monotonic one. Moving both would let one test's
    time trip the other deadline. The monotonic clock starts at 0, so elapsed
    times are exact sums of the steps, with no float error at a boundary.
    """

    def __init__(self, monkeypatch):
        self._wall = datetime.utcnow()
        self._monotonic = 0.0
        clock = self

        class _Datetime(datetime):
            @classmethod
            def utcnow(cls):
                return clock._wall

        monkeypatch.setattr(cis, "datetime", _Datetime)
        monkeypatch.setattr(cis, "time", SimpleNamespace(perf_counter=lambda: clock._monotonic))

    @property
    def now(self) -> datetime:
        return self._wall

    def pass_wall_time(self, seconds: float) -> None:
        self._wall += timedelta(seconds=seconds)

    def pass_monotonic_time(self, seconds: float) -> None:
        self._monotonic += seconds


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
    clock = _ServiceClock(monkeypatch)
    monkeypatch.setattr(cis, "_HEARTBEAT_SECONDS", 0.01)
    discovering = asyncio.Event()
    finish_discovery = asyncio.Event()

    async def slow_discovery(self, **kwargs):
        discovering.set()
        await finish_discovery.wait()
        return []

    monkeypatch.setattr(ConnectionService, "refresh_schema", slow_discovery)
    monkeypatch.setattr(ConnectionService, "construct_client", _construct_client)
    svc = ConnectionIndexingService()
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)

    run = asyncio.create_task(svc._run(str(row.id)))
    try:
        await asyncio.wait_for(discovering.wait(), 30)

        # Poll on ONE session, the way `wait_for_active` does.
        async with maker() as poll_db:
            polled = None  # held across polls, or the session forgets the row

            async def active():
                nonlocal polled
                polled = await svc.get_active(poll_db, str(connection.id))
                assert polled is not None, "a live run was reaped"
                return polled

            async def stage_written():
                return (await active()).phase is not None

            # The runner's own writes stamp `updated_at` with the real time, so
            # let the last of them land before time moves.
            await _until(stage_written)

            # More than three stale windows in all, each step ending inside
            # the window of the last heartbeat: only heartbeats keep it alive.
            for _ in range(4):
                clock.pass_wall_time(0.9 * cis._STALE_AFTER_SECONDS)
                heartbeat_due = clock.now

                async def heartbeat_landed():
                    return (await active()).updated_at >= heartbeat_due

                await _until(heartbeat_landed)
    finally:
        finish_discovery.set()
        await asyncio.wait_for(run, timeout=30)

    done = await _row(maker, row.id)
    assert done.status == ConnectionIndexingStatus.COMPLETED.value
    assert done.error is None


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
    clock = _ServiceClock(monkeypatch)
    monkeypatch.setattr(cis, "_IDLE_TIMEOUT_SECONDS", 0.4)
    stuck = asyncio.Event()

    async def discovery(self, *, progress_callback=None, **kwargs):
        for i in range(5):
            progress_callback("columns", f"dbo.t{i}", i + 1, 5)
        progress_callback("saving", None, 0, 0)
        stuck.set()
        await asyncio.sleep(3600)  # stuck, silently

    monkeypatch.setattr(ConnectionService, "refresh_schema", discovery)
    monkeypatch.setattr(ConnectionService, "construct_client", _construct_client)
    svc = ConnectionIndexingService()
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)

    run = asyncio.create_task(svc._run(str(row.id)))
    await asyncio.wait_for(stuck.wait(), 30)
    stuck_since = clock.now

    # While stuck (before the idle window closes) the row already names the
    # stage it is stuck in and when progress stopped.
    async with maker() as poll_db:
        async def saving_written():
            return (await svc.get_latest(poll_db, str(connection.id))).phase == "saving"

        await _until(saving_written)
        live = await svc.get_latest(poll_db, str(connection.id))
    assert live.status == ConnectionIndexingStatus.RUNNING.value
    assert live.last_activity_at == stuck_since

    clock.pass_monotonic_time(2 * cis._IDLE_TIMEOUT_SECONDS)  # well past the window
    await asyncio.wait_for(run, timeout=30)
    failed = await _row(maker, row.id)
    assert failed.status == ConnectionIndexingStatus.FAILED.value
    assert "saving" in failed.error


@pytest.mark.asyncio
async def test_discovery_that_keeps_reporting_outlives_the_idle_window(env, monkeypatch):
    maker, connection = env
    clock = _ServiceClock(monkeypatch)
    monkeypatch.setattr(cis, "_IDLE_TIMEOUT_SECONDS", 0.4)

    async def slow_but_progressing(self, *, progress_callback=None, **kwargs):
        for i in range(4):  # more than three idle windows in all
            # Time passes and the report lands in one step, so the idle check
            # never sees a full window since the last report, however late it runs.
            clock.pass_monotonic_time(0.9 * cis._IDLE_TIMEOUT_SECONDS)
            progress_callback("datasets", f"model-{i}", i + 1, 4)
            await asyncio.sleep(0.15)  # gives the idle check (every 0.1s) its turn
        return []

    monkeypatch.setattr(ConnectionService, "refresh_schema", slow_but_progressing)
    monkeypatch.setattr(ConnectionService, "construct_client", _construct_client)
    svc = ConnectionIndexingService()
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)
    await asyncio.wait_for(svc._run(str(row.id)), timeout=30)

    assert (await _row(maker, row.id)).status == ConnectionIndexingStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_cancel_recorded_by_another_worker_is_not_overwritten(env, monkeypatch):
    """`request_cancel` handled by another worker only changes the row (its
    in-process cancel event is not ours). Finalize must not flip it back."""
    maker, connection = env
    svc = ConnectionIndexingService()

    async def discovery(self, **kwargs):
        # Raw write: the cancel route would also set THIS process's event,
        # which is exactly what another worker cannot do.
        async with maker() as db:
            row = await svc.get_active(db, str(connection.id))
            row.status = ConnectionIndexingStatus.CANCELLED.value
            row.finished_at = datetime.utcnow()
            row.error = "Cancelled by user"
            await db.commit()
        return []

    monkeypatch.setattr(ConnectionService, "refresh_schema", discovery)
    monkeypatch.setattr(ConnectionService, "construct_client", _construct_client)
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)
    await asyncio.wait_for(svc._run(str(row.id)), timeout=10)

    assert (await _row(maker, row.id)).status == ConnectionIndexingStatus.CANCELLED.value


class _CatalogClient(_Client):
    def __init__(self, tables):
        self._tables = tables

    async def aget_schemas(self, **kwargs):
        return self._tables


@pytest.mark.asyncio
@pytest.mark.parametrize("table_count", [3, 1201])
async def test_run_log_records_discovery_and_save_before_writing_the_catalog(
    env, monkeypatch, table_count,
):
    """A worker killed while saving (out of memory) leaves no error behind, so
    the run's own event log must already say it reached discovery and saving,
    with the worker's memory, before the catalog write starts."""
    maker, connection = env
    tables = [
        {"name": f"dbo.t{i}", "columns": [{"name": "id", "dtype": "int"}, {"name": "v", "dtype": "text"}]}
        for i in range(table_count)
    ]

    async def construct_client(self, *args, **kwargs):
        return _CatalogClient(tables)

    monkeypatch.setattr(ConnectionService, "construct_client", construct_client)
    svc = ConnectionIndexingService()
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)
    await asyncio.wait_for(svc._run(str(row.id)), timeout=30)

    done = await _row(maker, row.id)
    assert done.status == ConnectionIndexingStatus.COMPLETED.value
    assert done.stats_json["table_count"] == table_count
    with_memory = [e["phase"] for e in done.events_json if "rss" in e["message"]]
    assert "discovered" in with_memory and "saving" in with_memory
    assert with_memory.index("discovered") < with_memory.index("saving")


@pytest.mark.asyncio
@pytest.mark.parametrize("rounds", [5, 40])
async def test_event_log_keeps_every_entry_when_progress_and_run_events_interleave(
    env, monkeypatch, rounds,
):
    """Progress reports and the runner's own events are written to the same
    log by different writers. Neither may overwrite the other's entries."""
    maker, connection = env

    async def discovery(self, *, progress_callback=None, diagnostics_hook=None, **kwargs):
        for i in range(rounds):
            progress_callback(f"stage-{i}", f"dbo.t{i}", i + 1, rounds)
            await diagnostics_hook("discovered", {"tables": i, "columns": i})
        return []

    monkeypatch.setattr(ConnectionService, "refresh_schema", discovery)
    monkeypatch.setattr(ConnectionService, "construct_client", _construct_client)
    svc = ConnectionIndexingService()
    async with maker() as db:
        row = await svc.start(db=db, connection=connection, kick_off=False)
    await asyncio.wait_for(svc._run(str(row.id)), timeout=30)

    events = (await _row(maker, row.id)).events_json
    phases = [e["phase"] for e in events]
    assert all(f"stage-{i}" in phases for i in range(rounds)), phases
    assert phases.count("discovered") == rounds, phases
    stage_positions = [phases.index(f"stage-{i}") for i in range(rounds)]
    assert stage_positions == sorted(stage_positions)
