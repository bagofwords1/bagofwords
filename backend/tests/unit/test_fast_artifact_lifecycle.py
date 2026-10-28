"""Artifact lifetime invariants, using real sessions, extraction and files.

Also runnable in a fresh sandbox via tools/agent/custom_table_artifact_loop.py.
"""
import asyncio
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.data_sources.fast import artifacts
from app.models.base import Base
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.organization import Organization
from app.services.custom_query_service import CustomQueryService

pytestmark = pytest.mark.db


class ArtifactLifecycle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bow-artifact-test-")
        self.path = Path(self.tmp.name)
        self.patch = patch.object(artifacts, "_ARTIFACT_ROOT", self.path / "fast")
        self.patch.start()
        self.admin = None
        # Follow pytest's --db selection, including xdist's per-worker URL.
        # Destructive fault injection uses a private database on that same
        # server so DROP/trigger failures cannot damage the harness template.
        harness_url = make_url(os.environ["TEST_DATABASE_URL"])
        if harness_url.get_backend_name() == "postgresql":
            url = harness_url
            self.database = "artifact_loop_" + uuid.uuid4().hex
            self.admin = create_engine(url.set(drivername="postgresql+psycopg2"), isolation_level="AUTOCOMMIT")
            with self.admin.connect() as conn:
                conn.execute(text(f'CREATE DATABASE "{self.database}"'))
            self.engine = create_async_engine(url.set(drivername="postgresql+asyncpg", database=self.database))
        else:
            self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.path / 'db.sqlite'}")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        source = self.path / "source.sqlite"
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("CREATE TABLE orders AS SELECT 42 AS value")
        async with self.sessions() as db:
            # The API cannot produce two preloaded ORM sessions or crash debris.
            # Seed the minimal real model state for those lifecycle invariants.
            org = Organization(name=self.tmp.name)
            db.add(org)
            await db.flush()
            connection = Connection(name="Source", type="sqlite", config={"database": str(source)}, organization_id=org.id)
            db.add(connection)
            await db.flush()
            table = ConnectionTable(connection_id=connection.id, name="orders_copy", kind=KIND_BOW,
                                    definition_sql="SELECT * FROM orders", columns=[], pks=[], fks=[])
            db.add(table)
            await db.commit()
            self.connection_id, self.table_id = connection.id, table.id
        self.service = CustomQueryService()

    async def asyncTearDown(self):
        await self.engine.dispose()
        if self.admin:
            with self.admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE "{self.database}" WITH (FORCE)'))
            self.admin.dispose()
        self.patch.stop()
        self.tmp.cleanup()

    async def load(self, db):
        return await db.get(Connection, self.connection_id), await db.get(ConnectionTable, self.table_id)

    async def test_refreshes_with_preloaded_sessions_keep_only_current_artifact(self):
        async with self.sessions() as first, self.sessions() as second:
            c1, t1 = await self.load(first)
            c2, t2 = await self.load(second)
            await self.service.refresh(first, c1, t1)
            await self.service.refresh(second, c2, t2)
            await second.refresh(t2)
            files = set((self.path / "fast").rglob("*.db"))
            self.assertEqual(files, {Path(t2.artifact_path)})
            con = artifacts.connect_encrypted(t2.artifact_path, artifacts.decrypt_key(t2.artifact_key_enc), read_only=True)
            try:
                self.assertEqual(con.execute('SELECT value FROM orders_copy').fetchall(), [(42,)])
            finally:
                con.close()


    async def test_cancelled_refresh_waits_for_source_and_discards_unpublished_file(self):
        from app.data_sources.clients.sqlite_client import SqliteClient
        entered, release = threading.Event(), threading.Event()
        original = SqliteClient.extraction_connect

        @contextmanager
        def slow_source(client):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("sandbox source was never released")
            with original(client) as connection:
                yield connection

        async with self.sessions() as db:
            connection, table = await self.load(db)
            await self.service.refresh(db, connection, table)
            old_path = table.artifact_path
            # Delay the source I/O boundary; extraction and DuckDB stay real.
            with patch.object(SqliteClient, "extraction_connect", slow_source):
                task = asyncio.create_task(self.service.refresh(db, connection, table))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 10))
                    for _ in range(3):
                        task.cancel()
                        # Deliver each cancellation while the source remains
                        # deterministically blocked, without timing sleeps.
                        await asyncio.to_thread(lambda: None)
                        with artifacts.try_refresh_lock(self.connection_id) as acquired:
                            self.assertFalse(acquired)
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()
                    if not task.done():
                        await asyncio.gather(task, return_exceptions=True)
            await db.refresh(table)
            self.assertEqual(table.artifact_path, old_path)
            self.assertEqual(table.last_refresh_status, "error")
            self.assertEqual(set((self.path / "fast").rglob("*.db")), {Path(old_path)})

    async def test_deleted_table_is_not_rebuilt_by_a_preloaded_refresh(self):
        async with self.sessions() as db, self.sessions() as queued:
            connection, table = await self.load(db)
            stale_connection, stale_table = await self.load(queued)
            await self.service.refresh(db, connection, table)
            await self.service.delete(db, connection, table)
            await self.service.refresh(queued, stale_connection, stale_table)
            await queued.refresh(stale_table)
            self.assertIsNotNone(stale_table.deleted_at)
            self.assertIsNone(stale_table.artifact_path)
            self.assertEqual(list((self.path / "fast").rglob("*.db")), [])

    async def test_source_failure_keeps_previous_readable_artifact(self):
        async with self.sessions() as db:
            connection, table = await self.load(db)
            await self.service.refresh(db, connection, table)
            old_path = table.artifact_path
            with closing(sqlite3.connect(self.path / "source.sqlite")) as source, source:
                source.execute("DROP TABLE orders")
            with self.assertRaises(HTTPException):
                await self.service.refresh(db, connection, table)
            await db.refresh(table)
            self.assertEqual(table.artifact_path, old_path)
            self.assertEqual(table.last_refresh_status, "error")
            self.assertEqual(set((self.path / "fast").rglob("*.db")), {Path(old_path)})

    async def test_post_commit_sync_failure_still_deletes_previous_artifact(self):
        async with self.sessions() as db:
            connection, table = await self.load(db)
            await self.service.refresh(db, connection, table)
            old_path = table.artifact_path
            # Metadata-sync storage failure after the reference swap commits.
            await db.execute(text("DROP TABLE datasource_tables" + (" CASCADE" if self.admin else "")))
            await db.commit()
            with self.assertRaises(DBAPIError):
                await self.service.refresh(db, connection, table)
            await db.rollback()
            await db.refresh(table)
            self.assertNotEqual(table.artifact_path, old_path)
            self.assertFalse(Path(old_path).exists())
            self.assertEqual(set((self.path / "fast").rglob("*.db")), {Path(table.artifact_path)})

    async def test_failed_swap_keeps_old_artifact_and_discards_new_file(self):
        async with self.sessions() as db:
            connection, table = await self.load(db)
            await self.service.refresh(db, connection, table)
            old_path = table.artifact_path
            # Real storage failure: reject replacement references at the DB
            # boundary rather than mocking the refresh implementation.
            if self.admin:
                await db.execute(text("""CREATE FUNCTION reject_swap() RETURNS trigger AS $$
                    BEGIN RAISE EXCEPTION 'sandbox failed swap'; END $$ LANGUAGE plpgsql"""))
                await db.execute(text("""CREATE TRIGGER reject_swap BEFORE UPDATE OF artifact_path
                    ON connection_tables FOR EACH ROW WHEN (NEW.artifact_path IS DISTINCT FROM OLD.artifact_path)
                    EXECUTE FUNCTION reject_swap()"""))
            else:
                await db.execute(text("""CREATE TRIGGER reject_swap BEFORE UPDATE OF artifact_path
                    ON connection_tables WHEN NEW.artifact_path IS NOT OLD.artifact_path
                    BEGIN SELECT RAISE(ABORT, 'sandbox failed swap'); END"""))
            await db.commit()
            with self.assertRaises(DBAPIError):
                await self.service.refresh(db, connection, table)
            await db.rollback()
            await db.refresh(table)
            self.assertEqual(table.artifact_path, old_path)
            self.assertEqual(set((self.path / "fast").rglob("*.db")), {Path(old_path)})
            await db.refresh(connection)  # rollback expires all ORM objects
            with self.assertRaises(DBAPIError):
                await self.service.delete(db, connection, table)
            await db.rollback()
            await db.refresh(table)
            self.assertEqual(table.artifact_path, old_path)
            self.assertTrue(Path(old_path).exists())

    async def test_contended_volume_lock_returns_conflict_and_can_be_reused(self):
        from app.errors import AppError
        with artifacts.try_refresh_lock(self.connection_id) as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(AppError) as error:
                async with artifacts.refresh_lock(self.connection_id, timeout=0):
                    self.fail("entered a contended volume")
            self.assertEqual(error.exception.status_code, 409)
        async with artifacts.refresh_lock(self.connection_id, timeout=0):
            self.assertTrue((self.path / "fast" / self.connection_id / ".refresh.lock").exists())

    async def test_scheduled_sweep_claims_one_run_across_workers(self):
        from app.core import scheduler as scheduler_module
        from app.services.fast_artifact_maintenance import scheduled_purge_orphaned_fast_artifacts
        sync_driver = "postgresql+psycopg2" if self.admin else "sqlite"
        claim_engine = create_engine(self.engine.url.set(drivername=sync_driver))
        # The claim table is migration-only (no ORM model), so install its
        # actual schema in this private fault-injection database.
        import importlib.util
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        migration_path = Path(__file__).resolve().parents[2] / "alembic/versions/sc1d2e3f4a5b_add_scheduled_job_runs.py"
        spec = importlib.util.spec_from_file_location("claim_schema", migration_path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        with claim_engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
        orphan = artifacts.artifact_dir(self.connection_id) / "orphan.db"
        old = time.time() - 2 * 86400
        try:
            # Replace only the datastore handle. The claim primitive, unique
            # constraint and sweep all run against the real private database.
            with patch.object(scheduler_module, "_engine", claim_engine), patch("time.time", return_value=time.time()):
                orphan.write_bytes(b"orphan")
                os.utime(orphan, (old, old))
                first = await scheduled_purge_orphaned_fast_artifacts(self.sessions)
                self.assertEqual(first["deleted"], 1)
                orphan.write_bytes(b"next worker must not run the same sweep")
                os.utime(orphan, (old, old))
                second = await scheduled_purge_orphaned_fast_artifacts(self.sessions)
                self.assertEqual(second["deleted"], 0)
                self.assertTrue(orphan.exists())
        finally:
            claim_engine.dispose()

    async def test_cleanup_continues_after_one_file_cannot_be_removed(self):
        from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts
        directory = artifacts.artifact_dir(self.connection_id)
        blocked, removable = directory / "blocked.db", directory / "removable.db"
        old = time.time() - 2 * 86400
        for path in (blocked, removable):
            path.write_bytes(b"orphan")
            os.utime(path, (old, old))
        original = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("sandbox protected inode")
            return original(path, *args, **kwargs)

        # Inject an OS unlink failure at the filesystem boundary, not the
        # collector. It must not skip other files or lose their totals.
        with patch.object(Path, "unlink", unlink):
            result = await purge_orphaned_fast_artifacts(self.sessions)
        self.assertEqual(result["deleted"], 1)
        self.assertEqual(result["reclaimed_bytes"], len(b"orphan"))
        self.assertTrue(blocked.exists())
        self.assertFalse(removable.exists())

    async def test_deleted_table_cannot_regain_schedule_from_stale_update(self):
        from app.core.scheduler import scheduler
        from app.services.custom_query_service import job_id_for
        from apscheduler.jobstores.base import JobLookupError
        try:
            async with self.sessions() as db, self.sessions() as queued:
                connection, table = await self.load(db)
                stale_connection, stale_table = await self.load(queued)
                await self.service.delete(db, connection, table)
                with self.assertRaises(HTTPException) as error:
                    await self.service.update(queued, stale_connection, stale_table,
                                              definition_sql="SELECT value FROM orders WHERE value > 0")
                self.assertEqual(error.exception.status_code, 404)
                self.assertIsNone(scheduler.get_job(job_id_for(self.table_id)))
        finally:
            try:
                scheduler.remove_job(job_id_for(self.table_id))
            except JobLookupError:
                pass

    async def test_cleanup_preserves_references_recent_files_and_active_refreshes(self):
        from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts
        async with self.sessions() as db:
            connection, table = await self.load(db)
            await self.service.refresh(db, connection, table)
            current = Path(table.artifact_path)
            # Even a soft-deleted row still protects its reference.
            from datetime import datetime
            table.deleted_at = datetime.utcnow()
            await db.commit()
        old = time.time() - 2 * 86400
        os.utime(current, (old, old))
        current_wal = Path(str(current) + ".wal")
        current_wal.write_bytes(b"current sidecar")
        os.utime(current_wal, (old, old))
        orphan = current.parent / f"{uuid.uuid4().hex}.db"
        orphan.write_bytes(b"abandoned refresh")
        os.utime(orphan, (old, old))
        debris = current.parent / f"{uuid.uuid4().hex}.tmp.wal"
        debris.write_bytes(b"crash debris")
        os.utime(debris, (old, old))
        recent = current.parent / f"{uuid.uuid4().hex}.tmp"
        recent.write_bytes(b"in progress")
        unknown = current.parent / "README.txt"
        unknown.write_bytes(b"leave alone")
        outside = self.path / "outside.db"
        outside.write_bytes(b"outside the artifact volume")
        os.utime(outside, (old, old))
        link = current.parent / "outside.db"
        link.symlink_to(outside)
        removed_connection = artifacts.artifact_dir(str(uuid.uuid4())) / f"{uuid.uuid4().hex}.db"
        removed_connection.write_bytes(b"no connection row")
        os.utime(removed_connection, (old, old))
        with artifacts.try_refresh_lock(self.connection_id) as acquired:
            self.assertTrue(acquired)
            await purge_orphaned_fast_artifacts(self.sessions)
            self.assertTrue(orphan.exists())
            self.assertTrue(debris.exists())
        result = await purge_orphaned_fast_artifacts(self.sessions)
        self.assertEqual(result["deleted"], 2)
        self.assertFalse(orphan.exists())
        self.assertFalse(debris.exists())
        self.assertFalse(removed_connection.exists())
        for path in (current, current_wal, recent, unknown, outside, link):
            self.assertTrue(path.exists(), str(path))
        again = await purge_orphaned_fast_artifacts(self.sessions)
        self.assertEqual(again["deleted"], 0)

    async def test_cleanup_does_not_delete_when_reference_query_fails(self):
        from app.services.fast_artifact_maintenance import purge_orphaned_fast_artifacts
        directory = artifacts.artifact_dir(self.connection_id)
        orphan = directory / f"{uuid.uuid4().hex}.db"
        orphan.write_bytes(b"do not infer unreferenced from a database failure")
        old = time.time() - 2 * 86400
        os.utime(orphan, (old, old))
        async with self.engine.begin() as conn:
            await conn.execute(text("DROP TABLE connection_tables" + (" CASCADE" if self.admin else "")))
        result = await purge_orphaned_fast_artifacts(self.sessions)
        self.assertEqual(result["deleted"], 0)
        self.assertTrue(orphan.exists())

    async def test_volume_lock_excludes_other_process_and_releases_on_crash(self):
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
        child = subprocess.Popen([sys.executable, "-c", code, str(self.path / "fast"), self.connection_id],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, cwd=Path(__file__).resolve().parents[2])
        try:
            while True:
                line = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), 30)
                if line.strip() == "LOCKED":
                    break
                self.assertTrue(line, "lock worker exited unexpectedly")
            with artifacts.try_refresh_lock(self.connection_id) as acquired:
                self.assertFalse(acquired)
            child.kill()
            await asyncio.to_thread(child.wait, 10)
            with artifacts.try_refresh_lock(self.connection_id) as acquired:
                self.assertTrue(acquired)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()

