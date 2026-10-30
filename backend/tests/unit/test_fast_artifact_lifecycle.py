"""Artifact lifetime invariants, using real sessions, extraction and files.

Runs on the harness database (``pytestmark = db``: a fresh, migrated schema per
test on whichever ``--db`` CI selects), with the real sqlite extraction source,
real encrypted DuckDB files and the real volume lock. Only boundaries are
replaced: the artifact root (a tmp dir), the clock the claim table buckets on,
one connector I/O call (to hold a refresh mid-flight) and one OS unlink.

Also runnable in a fresh sandbox via tools/agent/custom_table_artifact_loop.py.
"""
import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.data_sources.fast import artifacts, extractor
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.organization import Organization
from app.services.custom_query_service import CustomQueryService, job_id_for

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

BACKEND_DIR = Path(__file__).resolve().parents[2]
TWO_DAYS_AGO = time.time() - 2 * 86400


@dataclass
class Sandbox:
    root: Path          # the patched artifact volume
    source: Path        # the sqlite database the custom table reads from
    connection_id: str
    table_id: str
    postgres: bool
    service: CustomQueryService

    def artifacts(self) -> set[Path]:
        return set(self.root.rglob("*.db"))

    async def load(self, db):
        return await db.get(Connection, self.connection_id), await db.get(ConnectionTable, self.table_id)

    def cascade(self) -> str:
        return " CASCADE" if self.postgres else ""


@pytest_asyncio.fixture
async def sandbox(tmp_path, db_backend):
    root = tmp_path / "fast"
    source = tmp_path / "source.sqlite"
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("CREATE TABLE orders AS SELECT 42 AS value")
    with patch.object(artifacts, "_ARTIFACT_ROOT", root):
        async with async_session_maker() as db:
            # The API cannot produce two preloaded ORM sessions or crash debris.
            # Seed the minimal real model state for those lifecycle invariants.
            org = Organization(name=f"Artifacts {uuid.uuid4().hex[:8]}")
            db.add(org)
            await db.flush()
            connection = Connection(name="Source", type="sqlite", config={"database": str(source)},
                                    organization_id=org.id)
            db.add(connection)
            await db.flush()
            table = ConnectionTable(connection_id=connection.id, name="orders_copy", kind=KIND_BOW,
                                    definition_sql="SELECT * FROM orders", columns=[], pks=[], fks=[])
            db.add(table)
            await db.commit()
            ids = (connection.id, table.id)
        yield Sandbox(root, source, *ids, db_backend != "sqlite", CustomQueryService())


def _age(*paths: Path) -> None:
    for path in paths:
        os.utime(path, (TWO_DAYS_AGO, TWO_DAYS_AGO))


def _orphan(directory: Path, name: str = None, content: bytes = b"orphan") -> Path:
    path = directory / (name or f"{uuid.uuid4().hex}.db")
    path.write_bytes(content)
    _age(path)
    return path


@contextmanager
def held_source(release_after: threading.Event = None):
    """Hold every extraction at the connector I/O boundary until released.

    Yields (entered, release): ``entered`` fires once an extraction is blocked
    inside the source, ``release`` lets it continue. Extraction and DuckDB stay
    real; only the connect call is delayed, so cancellation can be delivered
    deterministically mid-flight without timing sleeps.
    """
    from app.data_sources.clients.sqlite_client import SqliteClient
    entered, release = threading.Event(), release_after or threading.Event()
    original = SqliteClient.extraction_connect

    @contextmanager
    def slow_source(client):
        entered.set()
        if not release.wait(10):
            raise TimeoutError("sandbox source was never released")
        with original(client) as connection:
            yield connection

    with patch.object(SqliteClient, "extraction_connect", slow_source):
        try:
            yield entered, release
        finally:
            release.set()


@contextmanager
def frozen_claim_clock():
    """Pin the clock the shared-job claim buckets on, so two fires share a bucket."""
    from app.core import scheduler as scheduler_module
    now = time.time()
    with patch.object(scheduler_module, "time", SimpleNamespace(time=lambda: now)):
        yield


async def _drop_tables(sandbox: Sandbox, *tables: str) -> None:
    async with async_session_maker() as db:
        for table in tables:
            await db.execute(text(f"DROP TABLE {table}{sandbox.cascade()}"))
        await db.commit()


# -- refresh / swap ---------------------------------------------------------

async def test_refreshes_with_preloaded_sessions_keep_only_current_artifact(sandbox):
    async with async_session_maker() as first, async_session_maker() as second:
        c1, t1 = await sandbox.load(first)
        c2, t2 = await sandbox.load(second)
        await sandbox.service.refresh(first, c1, t1)
        await sandbox.service.refresh(second, c2, t2)
        await second.refresh(t2)
        assert sandbox.artifacts() == {Path(t2.artifact_path)}
        con = artifacts.connect_encrypted(t2.artifact_path, artifacts.decrypt_key(t2.artifact_key_enc),
                                          read_only=True)
        try:
            assert con.execute("SELECT value FROM orders_copy").fetchall() == [(42,)]
        finally:
            con.close()


async def test_cancelled_refresh_waits_for_source_and_discards_unpublished_file(sandbox):
    async with async_session_maker() as db:
        connection, table = await sandbox.load(db)
        await sandbox.service.refresh(db, connection, table)
        old_path = table.artifact_path
        with held_source() as (entered, release):
            task = asyncio.create_task(sandbox.service.refresh(db, connection, table))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                for _ in range(3):
                    task.cancel()
                    # Deliver each cancellation while the source remains
                    # deterministically blocked, without timing sleeps.
                    await asyncio.to_thread(lambda: None)
                    with artifacts.try_refresh_lock(sandbox.connection_id) as acquired:
                        assert not acquired, "lock released while the extraction thread still ran"
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
        await db.refresh(table)
        assert table.artifact_path == old_path
        assert table.last_refresh_status == "error"
        # A cancelled refresh carries a reason, not a blank error badge.
        assert table.last_refresh_error
        assert sandbox.artifacts() == {Path(old_path)}


async def test_cancelled_request_stops_the_extractor_at_the_next_batch(sandbox):
    """The abort hook ends a scan early instead of waiting out the source."""
    from app.data_sources.clients.sqlite_client import SqliteClient
    client = SqliteClient(database=str(sandbox.source))
    with pytest.raises(extractor.ExtractionAborted):
        extractor.extract_to_artifact(client, "SELECT * FROM orders", "orders_copy",
                                      sandbox.connection_id, should_abort=lambda: True)
    assert not list(sandbox.root.rglob("*.db")) and not list(sandbox.root.rglob("*.tmp"))


async def test_deleted_table_is_not_rebuilt_by_a_preloaded_refresh(sandbox):
    async with async_session_maker() as db, async_session_maker() as queued:
        connection, table = await sandbox.load(db)
        stale_connection, stale_table = await sandbox.load(queued)
        await sandbox.service.refresh(db, connection, table)
        await sandbox.service.delete(db, connection, table)
        await sandbox.service.refresh(queued, stale_connection, stale_table)
        await queued.refresh(stale_table)
        assert stale_table.deleted_at is not None
        assert stale_table.artifact_path is None
        assert sandbox.artifacts() == set()


async def test_delete_during_a_running_refresh_wins_and_leaves_no_file(sandbox):
    """Delete never waits behind a refresh; the refresh's swap loses instead."""
    async with async_session_maker() as db, async_session_maker() as other:
        connection, table = await sandbox.load(db)
        other_connection, other_table = await sandbox.load(other)
        await sandbox.service.refresh(db, connection, table)
        with held_source() as (entered, release):
            task = asyncio.create_task(sandbox.service.refresh(db, connection, table))
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                # The refresh holds the volume lock; delete must not queue on it.
                await asyncio.wait_for(sandbox.service.delete(other, other_connection, other_table), 5)
                release.set()
                result = await task
            finally:
                release.set()
                if not task.done():
                    await asyncio.gather(task, return_exceptions=True)
        await db.refresh(result)
        assert result.deleted_at is not None
        assert result.artifact_path is None
        assert sandbox.artifacts() == set()


async def test_source_failure_keeps_previous_readable_artifact(sandbox):
    async with async_session_maker() as db:
        connection, table = await sandbox.load(db)
        await sandbox.service.refresh(db, connection, table)
        old_path = table.artifact_path
        with closing(sqlite3.connect(sandbox.source)) as source, source:
            source.execute("DROP TABLE orders")
        with pytest.raises(HTTPException):
            await sandbox.service.refresh(db, connection, table)
        await db.refresh(table)
        assert table.artifact_path == old_path
        assert table.last_refresh_status == "error"
        assert sandbox.artifacts() == {Path(old_path)}


async def test_post_commit_sync_failure_still_deletes_previous_artifact(sandbox):
    async with async_session_maker() as db:
        connection, table = await sandbox.load(db)
        await sandbox.service.refresh(db, connection, table)
        old_path = table.artifact_path
        # Metadata-sync storage failure after the reference swap commits. DDL
        # goes through this session: asyncpg invalidates its plan cache for
        # DDL it ran itself, whereas DDL from another connection would surface
        # on this one as an error before the swap even started.
        await db.execute(text(f"DROP TABLE datasource_tables{sandbox.cascade()}"))
        await db.commit()
        with pytest.raises(DBAPIError):
            await sandbox.service.refresh(db, connection, table)
        await db.rollback()
        await db.refresh(table)
        assert table.artifact_path != old_path
        assert not Path(old_path).exists()
        assert sandbox.artifacts() == {Path(table.artifact_path)}


async def test_failed_swap_keeps_old_artifact_and_discards_new_file(sandbox):
    async with async_session_maker() as db:
        connection, table = await sandbox.load(db)
        await sandbox.service.refresh(db, connection, table)
        old_path = table.artifact_path
        # Real storage failure: reject replacement references at the DB
        # boundary rather than mocking the refresh implementation.
        if sandbox.postgres:
            install = ["""CREATE FUNCTION reject_swap() RETURNS trigger AS $$
                          BEGIN RAISE EXCEPTION 'sandbox failed swap'; END $$ LANGUAGE plpgsql""",
                       """CREATE TRIGGER reject_swap BEFORE UPDATE OF artifact_path ON connection_tables
                          FOR EACH ROW WHEN (NEW.artifact_path IS DISTINCT FROM OLD.artifact_path)
                          EXECUTE FUNCTION reject_swap()"""]
            remove = ["DROP TRIGGER IF EXISTS reject_swap ON connection_tables",
                      "DROP FUNCTION IF EXISTS reject_swap()"]
        else:
            install = ["""CREATE TRIGGER reject_swap BEFORE UPDATE OF artifact_path ON connection_tables
                          WHEN NEW.artifact_path IS NOT OLD.artifact_path
                          BEGIN SELECT RAISE(ABORT, 'sandbox failed swap'); END"""]
            remove = ["DROP TRIGGER IF EXISTS reject_swap"]
        for statement in install:
            await db.execute(text(statement))
        await db.commit()
        try:
            with pytest.raises(DBAPIError):
                await sandbox.service.refresh(db, connection, table)
            await db.rollback()
            await db.refresh(table)
            assert table.artifact_path == old_path
            assert sandbox.artifacts() == {Path(old_path)}
            await db.refresh(connection)  # rollback expires all ORM objects
            with pytest.raises(DBAPIError):
                await sandbox.service.delete(db, connection, table)
            await db.rollback()
            await db.refresh(table)
            assert table.artifact_path == old_path
            assert Path(old_path).exists()
        finally:
            # Triggers are outside the harness's schema fingerprint; leave the
            # database exactly as the migration chain built it. Same session,
            # after ending its transaction: DROP TRIGGER needs an exclusive
            # lock on the table this session still had a read lock on.
            await db.rollback()
            for statement in remove:
                await db.execute(text(statement))
            await db.commit()


# -- scheduling -------------------------------------------------------------

async def test_deleted_table_cannot_regain_schedule_from_stale_update(sandbox):
    from apscheduler.jobstores.base import JobLookupError

    from app.core.scheduler import scheduler
    try:
        async with async_session_maker() as db, async_session_maker() as queued:
            connection, table = await sandbox.load(db)
            stale_connection, stale_table = await sandbox.load(queued)
            await sandbox.service.delete(db, connection, table)
            with pytest.raises(HTTPException) as error:
                await sandbox.service.update(queued, stale_connection, stale_table,
                                             definition_sql="SELECT value FROM orders WHERE value > 0")
            assert error.value.status_code == 404
            assert scheduler.get_job(job_id_for(sandbox.table_id)) is None
    finally:
        with suppress(JobLookupError):
            scheduler.remove_job(job_id_for(sandbox.table_id))


async def test_scheduled_refresh_runs_once_per_tick_across_workers(sandbox):
    """Every worker fires the shared job; only the claim winner scans the source."""
    with frozen_claim_clock():
        await sandbox.service.scheduled_refresh(sandbox.connection_id, sandbox.table_id)
        async with async_session_maker() as db:
            table = await db.get(ConnectionTable, sandbox.table_id)
            first_path = table.artifact_path
        assert first_path and Path(first_path).exists()
        await sandbox.service.scheduled_refresh(sandbox.connection_id, sandbox.table_id)
        async with async_session_maker() as db:
            table = await db.get(ConnectionTable, sandbox.table_id)
        assert table.artifact_path == first_path, "a second worker re-ran the same tick"
        assert sandbox.artifacts() == {Path(first_path)}


# -- volume lock ------------------------------------------------------------

async def test_queued_refresh_waits_for_the_lock_instead_of_failing(sandbox):
    """A sibling table's refresh runs after the current one, it is not dropped."""
    entered = asyncio.Event()

    async def queued():
        async with artifacts.refresh_lock(sandbox.connection_id):
            entered.set()

    with artifacts.try_refresh_lock(sandbox.connection_id) as acquired:
        assert acquired
        task = asyncio.create_task(queued())
        for _ in range(5):
            await asyncio.sleep(0)
        assert not entered.is_set()
    await asyncio.wait_for(task, 10)
    assert entered.is_set()


async def test_bounded_lock_wait_returns_conflict_and_can_be_reused(sandbox):
    from app.errors import AppError
    with artifacts.try_refresh_lock(sandbox.connection_id) as acquired:
        assert acquired
        with pytest.raises(AppError) as error:
            async with artifacts.refresh_lock(sandbox.connection_id, timeout=0):
                pytest.fail("entered a contended volume")
        assert error.value.status_code == 409
    async with artifacts.refresh_lock(sandbox.connection_id, timeout=0):
        assert (sandbox.root / sandbox.connection_id / ".refresh.lock").exists()


async def test_volume_lock_excludes_other_process_and_releases_on_crash(sandbox):
    # A second interpreter models another uvicorn worker/container sharing
    # this volume. Pipe handshakes replace sleeps/timing assumptions.
    code = """
import sys
from pathlib import Path
from app.data_sources.fast import artifacts
artifacts._ARTIFACT_ROOT = Path(sys.argv[1])
with artifacts.try_refresh_lock(sys.argv[2]) as acquired:
    assert acquired
    print('LOCKED', flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen([sys.executable, "-c", code, str(sandbox.root), sandbox.connection_id],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, cwd=BACKEND_DIR)
    try:
        while True:
            line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 30)
            if line.strip() == "LOCKED":
                break
            assert line, "lock worker exited unexpectedly"
        with artifacts.try_refresh_lock(sandbox.connection_id) as acquired:
            assert not acquired
        child.kill()
        await asyncio.to_thread(child.wait, 10)
        with artifacts.try_refresh_lock(sandbox.connection_id) as acquired:
            assert acquired
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()


# -- orphan sweep -----------------------------------------------------------

async def test_scheduled_sweep_claims_one_run_across_workers(sandbox):
    from app.services.fast_artifact_maintenance import scheduled_purge_orphaned_fast_artifacts
    directory = artifacts.artifact_dir(sandbox.connection_id)
    orphan = _orphan(directory, "orphan.db")
    with frozen_claim_clock():
        first = await scheduled_purge_orphaned_fast_artifacts()
        assert first["deleted"] == 1
        _orphan(directory, "orphan.db", b"next worker must not run the same sweep")
        second = await scheduled_purge_orphaned_fast_artifacts()
        assert second["deleted"] == 0
        assert orphan.exists()


async def test_cleanup_continues_after_one_file_cannot_be_removed(sandbox):
    from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts
    directory = artifacts.artifact_dir(sandbox.connection_id)
    blocked, removable = _orphan(directory, "blocked.db"), _orphan(directory, "removable.db")
    original = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("sandbox protected inode")
        return original(path, *args, **kwargs)

    # Inject an OS unlink failure at the filesystem boundary, not the
    # collector. It must not skip other files or lose their totals.
    with patch.object(Path, "unlink", unlink):
        result = await purge_orphaned_fast_artifacts()
    assert result["deleted"] == 1
    assert result["reclaimed_bytes"] == len(b"orphan")
    assert blocked.exists()
    assert not removable.exists()


async def test_cleanup_preserves_references_recent_files_and_active_refreshes(sandbox):
    from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts
    async with async_session_maker() as db:
        connection, table = await sandbox.load(db)
        await sandbox.service.refresh(db, connection, table)
        current = Path(table.artifact_path)
        # Even a soft-deleted row still protects its reference.
        table.deleted_at = datetime.utcnow()
        await db.commit()
    _age(current)
    current_wal = Path(str(current) + ".wal")
    current_wal.write_bytes(b"current sidecar")
    _age(current_wal)
    orphan = _orphan(current.parent, content=b"abandoned refresh")
    debris = _orphan(current.parent, f"{uuid.uuid4().hex}.tmp.wal", b"crash debris")
    recent = current.parent / f"{uuid.uuid4().hex}.tmp"
    recent.write_bytes(b"in progress")
    unknown = current.parent / "README.txt"
    unknown.write_bytes(b"leave alone")
    outside = sandbox.root.parent / "outside.db"
    outside.write_bytes(b"outside the artifact volume")
    _age(outside)
    link = current.parent / "outside.db"
    link.symlink_to(outside)
    removed_connection = _orphan(artifacts.artifact_dir(str(uuid.uuid4())), content=b"no connection row")
    with artifacts.try_refresh_lock(sandbox.connection_id) as acquired:
        assert acquired
        await purge_orphaned_fast_artifacts()
        assert orphan.exists()
        assert debris.exists()
    result = await purge_orphaned_fast_artifacts()
    assert result["deleted"] == 2
    assert not orphan.exists()
    assert not debris.exists()
    assert not removed_connection.exists()
    for path in (current, current_wal, recent, unknown, outside, link):
        assert path.exists(), str(path)
    again = await purge_orphaned_fast_artifacts()
    assert again["deleted"] == 0


async def test_cleanup_protects_references_recorded_under_another_mount_prefix(sandbox):
    """Liveness is the (connection, file) identity, not the absolute path.

    A reference written while the volume was mounted elsewhere (a relocated
    release, a bind mount vs a symlink) must still protect its file; resolving
    paths would have treated every served artifact as unreferenced.
    """
    from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts
    async with async_session_maker() as db:
        connection, table = await sandbox.load(db)
        await sandbox.service.refresh(db, connection, table)
        current = Path(table.artifact_path)
        table.artifact_path = str(Path("/srv/previous-release/uploads/fast") / current.parent.name / current.name)
        await db.commit()
    _age(current)
    orphan = _orphan(current.parent)
    result = await purge_orphaned_fast_artifacts()
    assert result["deleted"] == 1
    assert current.exists()
    assert not orphan.exists()


async def test_cleanup_does_not_delete_when_reference_query_fails(sandbox):
    from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts
    orphan = _orphan(artifacts.artifact_dir(sandbox.connection_id),
                     content=b"do not infer unreferenced from a database failure")
    await _drop_tables(sandbox, "connection_tables")
    result = await purge_orphaned_fast_artifacts()
    assert result["deleted"] == 0
    assert orphan.exists()
