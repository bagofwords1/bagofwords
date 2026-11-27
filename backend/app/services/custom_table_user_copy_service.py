"""Per-user copies of custom tables: build, serve, invalidate, clean up.

See `app/data_sources/fast/user_copies.py` for why a copy exists and when it
may be served. This is everything that touches the database and the disk.

**Serving never waits long and is never wrong.** `serve()` is called while an
agent's clients are being built. A copy that is good is handed over at once.
One that is missing or expired is built with the asking user's own
credentials; the caller waits a bounded time for it, and if the build is
slower than that it carries on in the background while this question is
answered from the live source — which is the same user, the same credentials
and the same rows, only slower. A copy that cannot be served is simply absent
from the DuckDB session, so there is nothing stale to read by mistake.

**One builder per (table, user).** A file lock, as for shared custom tables:
it holds across workers on the same volume and the kernel releases it if the
process dies, which a `building` status in the database cannot promise.

**Publishing is conditional.** A build that finishes after its table was
deleted, or after the copy was invalidated (the user disconnected, an admin
purged it), discards its file instead of resurrecting what was just removed.
"""

import asyncio
import logging
import threading
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import delete, func, or_, select, update

from app.data_sources.fast import artifacts, extractor, incremental, user_copies
from app.data_sources.fast import limits as fast_limits
from app.models.connection import Connection
from app.models.connection_table import KIND_BOW, ConnectionTable
from app.models.custom_table_user_copy import (
    STATUS_BUILDING,
    STATUS_ERROR,
    STATUS_EVICTED,
    STATUS_OK,
    CustomTableUserCopy,
)

logger = logging.getLogger(__name__)

ERROR_LIMIT = 2000
# A failed build is not retried on every question: each retry would make the
# asker wait for the same failure.
RETRY_FAILED_AFTER = timedelta(minutes=5)
# A `building` row this old with nobody holding its lock was left by a worker
# that stopped; shown as interrupted rather than as still building.
INTERRUPTED_AFTER = timedelta(minutes=45)
# `last_used_at` is for deciding what is idle, in days. Written at most this often.
TOUCH_EVERY = timedelta(minutes=10)
# How long the file a rebuild replaced is kept when it was still being served.
# A chat that started before the rebuild holds that file's path for the rest
# of its run; deleting it at once would take the relation away mid-answer.
# (Found live: a sign-in asked for a rebuild, the next question was served the
# old copy, and the rebuild deleted it five seconds into the run.) If the
# process stops first, the daily orphan sweep removes the file.
RETIRE_REPLACED_AFTER_SECONDS = 3600


@dataclass
class CopyRef:
    """What the DuckDB session needs to attach one user's copy."""

    table_id: str
    artifact_path: str
    artifact_key: str
    columns: list
    row_count: int
    refreshed_at: Optional[datetime]


@dataclass
class _Snapshot:
    """A custom table's definition, read off the ORM row once."""

    table_id: str
    name: str
    sql: str
    target: Optional[dict]
    definition_hash: str
    stale_before: Optional[datetime]


def _session_maker():
    from app.dependencies import async_session_maker

    return async_session_maker


async def _write(work, session_factory=None):
    """`work(db)` in a short session of its own, committed."""
    async with (session_factory or _session_maker())() as db:
        result = await work(db)
        await db.commit()
        return result


def definition_hash_of(cq: ConnectionTable) -> str:
    """What decides which rows a copy holds: the query and its target.

    Refresh settings are left out on purpose. Per-user copies are always full
    loads, so changing the schedule must not throw every copy away.
    """
    meta = cq.metadata_json if isinstance(cq.metadata_json, dict) else {}
    target = meta.get("target") if isinstance(meta.get("target"), dict) else None
    return incremental.definition_hash(cq.definition_sql or "", target, incremental.RefreshConfig())


def _target_of(cq: ConnectionTable) -> Optional[dict]:
    meta = cq.metadata_json if isinstance(cq.metadata_json, dict) else {}
    target = meta.get("target")
    return dict(target) if isinstance(target, dict) else None


def _secret() -> str:
    from app.settings.config import settings

    return str(settings.bow_config.encryption_key or "")


def _zone(name: Optional[str]):
    if not name or name == "UTC":
        return None
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return None


class CustomTableUserCopyService:
    def __init__(self):
        # Builds in flight on this process, by (loop, table, user), so two
        # questions from the same person share one extraction.
        self._tasks: dict = {}
        self._started: dict = {}
        self._semaphores: dict = {}
        self._guard = threading.Lock()
        self.retire_replaced_after = RETIRE_REPLACED_AFTER_SECONDS

    # -- settings -----------------------------------------------------------

    @staticmethod
    async def _settings_row(db, organization_id: str):
        from app.models.organization_settings import OrganizationSettings

        return (await db.execute(
            select(OrganizationSettings).where(
                OrganizationSettings.organization_id == str(organization_id))
        )).scalars().first()

    async def limits_for(self, db, organization_id: str) -> tuple:
        """(per-user copy limits, extraction limits, timezone) for an organization."""
        row = await self._settings_row(db, organization_id)
        config = getattr(row, "config", None)
        tz = config.get("timezone") if isinstance(config, dict) else None
        if isinstance(tz, dict):
            tz = tz.get("value")
        return fast_limits.resolve_user_copies(row), fast_limits.resolve(row), tz

    # -- reading ------------------------------------------------------------

    @staticmethod
    def _snapshot(cq: ConnectionTable, now: datetime, tz) -> _Snapshot:
        return _Snapshot(
            table_id=str(cq.id), name=cq.name, sql=cq.definition_sql or "",
            target=_target_of(cq), definition_hash=definition_hash_of(cq),
            stale_before=user_copies.schedule_due_at(
                now, cq.refresh_schedule_mode, cq.refresh_interval_minutes,
                cq.refresh_at_time, tz),
        )

    @staticmethod
    def _state(copy: Optional[CustomTableUserCopy], snap: _Snapshot, fingerprint: Optional[str],
               now: datetime, limits: fast_limits.UserCopyLimits) -> user_copies.State:
        if copy is None:
            return user_copies.State(False, True, user_copies.REASON_MISSING)
        return user_copies.evaluate(
            status=copy.status,
            has_artifact=bool(copy.artifact_path and copy.artifact_key_enc),
            refreshed_at=copy.refreshed_at, invalidated_at=copy.invalidated_at,
            copy_definition_hash=copy.definition_hash, definition_hash=snap.definition_hash,
            copy_fingerprint=copy.credential_fingerprint, fingerprint=fingerprint,
            now=now, max_age_seconds=limits.max_age_seconds, stale_before=snap.stale_before,
            refresh_requested_at=copy.refresh_requested_at,
        )

    @staticmethod
    async def _copies(db, user_id: str, table_ids: list) -> dict:
        if not table_ids:
            return {}
        rows = (await db.execute(
            select(CustomTableUserCopy)
            .where(CustomTableUserCopy.user_id == str(user_id),
                   CustomTableUserCopy.connection_table_id.in_([str(t) for t in table_ids]))
            .execution_options(populate_existing=True)
        )).scalars().all()
        return {str(r.connection_table_id): r for r in rows}

    @staticmethod
    def _ref(copy: CustomTableUserCopy) -> Optional[CopyRef]:
        try:
            key = artifacts.decrypt_key(copy.artifact_key_enc)
        except Exception as e:
            logger.error("user_copy.key_decrypt_failed", extra={"copy": str(copy.id), "error": str(e)})
            return None
        return CopyRef(
            table_id=str(copy.connection_table_id), artifact_path=copy.artifact_path,
            artifact_key=key, columns=copy.columns or [], row_count=int(copy.row_count or 0),
            refreshed_at=copy.refreshed_at,
        )

    async def servable(self, db, user_id: Optional[str], tables: list) -> dict:
        """`{table_id: copy row}` for the tables `user_id` has a copy the next
        `serve()` would hand over.

        The schema context asks this, so the prompt names exactly the cached
        relations the DuckDB session will hold. It cannot check the user's
        credentials (it does not resolve them); `serve()` records a changed
        login as an invalidation, which this does see.
        """
        if not user_id or not tables:
            return {}
        by_org: dict = {}
        out: dict = {}
        now = datetime.utcnow()
        copies = await self._copies(db, user_id, [t.id for t in tables])
        if not copies:
            return {}
        for cq in tables:
            copy = copies.get(str(cq.id))
            if copy is None:
                continue
            org_id = str(copy.organization_id)
            if org_id not in by_org:
                by_org[org_id] = await self.limits_for(db, org_id)
            limits, _fast, tz = by_org[org_id]
            snap = self._snapshot(cq, now, _zone(tz))
            if self._state(copy, snap, None, now, limits).servable:
                out[str(cq.id)] = copy
        return out

    # -- serving ------------------------------------------------------------

    async def serve(
        self,
        db,
        *,
        connection: Connection,
        tables: list,
        user,
        credentials: Optional[dict],
        client_factory: Callable[[ConnectionTable], Awaitable[Any]],
        wait: bool = True,
    ) -> dict:
        """The copies of `tables` that `user` may be served right now.

        Builds what is missing, waiting up to the organization's limit when
        `wait` is set. `client_factory(table)` must return a client for the
        connection carrying `user`'s own credentials, ready to extract; it is
        called only for a table that actually needs building.
        """
        if user is None or not tables:
            return {}
        user_id = str(user.id)
        org_id = str(connection.organization_id)
        limits, fast, tz = await self.limits_for(db, org_id)
        now = datetime.utcnow()
        fingerprint = user_copies.credential_fingerprint(credentials, _secret())
        copies = await self._copies(db, user_id, [t.id for t in tables])
        zone = _zone(tz)

        refs: dict = {}
        waiting: list = []
        touched: list = []
        committed = False
        for cq in tables:
            snap = self._snapshot(cq, now, zone)
            copy = copies.get(snap.table_id)
            state = self._state(copy, snap, fingerprint, now, limits)
            if state.reason == user_copies.REASON_CREDENTIALS and copy is not None:
                # Make it visible to readers that cannot check credentials.
                await self.invalidate(db, copy_id=copy.id)
            if state.servable:
                ref = self._ref(copy)
                if ref is not None:
                    refs[snap.table_id] = ref
                    touched.append(copy)
            if not state.rebuild:
                continue
            if self._recently_failed(copy, snap, fingerprint, now):
                continue
            if not committed:
                # A build writes through sessions of its own. Whatever this
                # one has pending must be committed first, or on SQLite (one
                # writer) the build would wait on the very request that is
                # waiting for it.
                await db.commit()
                committed = True
            task, started = await self._ensure_build(
                connection=connection, cq=cq, snap=snap, user_id=user_id, org_id=org_id,
                fingerprint=fingerprint, limits=limits, fast=fast, client_factory=client_factory,
                # Only a copy this call is handing out may outlive its
                # replacement. One that was refused (another login's rows, an
                # older query) is deleted the moment the new one lands.
                keep_replaced=state.servable,
            )
            if task is not None and not state.servable:
                waiting.append((snap.table_id, task, started))

        if waiting and wait and limits.wait_seconds > 0:
            # Bounded from when the build began, not from now: a long build
            # holds up the first question, not every question until it ends.
            deadline = max(
                (started + limits.wait_seconds for _id, _task, started in waiting),
                default=time.monotonic(),
            )
            remaining = deadline - time.monotonic()
            if remaining > 0:
                with suppress(Exception):
                    await asyncio.wait(
                        [asyncio.shield(t) for _id, t, _s in waiting], timeout=remaining)
            built = await self._copies(db, user_id, [i for i, _t, _s in waiting])
            now = datetime.utcnow()
            for cq in tables:
                copy = built.get(str(cq.id))
                if copy is None or str(cq.id) in refs:
                    continue
                snap = self._snapshot(cq, now, zone)
                if self._state(copy, snap, fingerprint, now, limits).servable:
                    ref = self._ref(copy)
                    if ref is not None:
                        refs[str(cq.id)] = ref
                        touched.append(copy)

        await self._touch(db, touched)
        return refs

    @staticmethod
    def _recently_failed(copy, snap: _Snapshot, fingerprint: str, now: datetime) -> bool:
        return bool(
            copy is not None and copy.status == STATUS_ERROR
            and copy.building_since is not None
            and now - copy.building_since < RETRY_FAILED_AFTER
            and copy.failed_definition_hash == snap.definition_hash
            and copy.failed_credential_fingerprint == fingerprint
            and not (copy.invalidated_at and copy.invalidated_at >= copy.building_since)
        )

    @staticmethod
    async def _touch(db, copies: list) -> None:
        now = datetime.utcnow()
        ids = [str(c.id) for c in copies
               if c.last_used_at is None or now - c.last_used_at > TOUCH_EVERY]
        if not ids:
            return
        try:
            await db.execute(
                update(CustomTableUserCopy).where(CustomTableUserCopy.id.in_(ids))
                .values(last_used_at=now).execution_options(synchronize_session=False))
            await db.commit()
        except Exception:
            with suppress(Exception):
                await db.rollback()

    # -- building -----------------------------------------------------------

    def _semaphore(self, connection_id: str, concurrency: int) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        key = (id(loop), str(connection_id), int(concurrency))
        with self._guard:
            sem = self._semaphores.get(key)
            if sem is None:
                sem = self._semaphores[key] = asyncio.Semaphore(max(1, int(concurrency)))
            return sem

    async def _ensure_build(self, *, connection, cq, snap: _Snapshot, user_id: str, org_id: str,
                            fingerprint: str, limits, fast, client_factory,
                            keep_replaced: bool = False) -> tuple:
        """The build task for (table, user) on this loop, started if needed.

        Returns `(task, started_monotonic)`; `(None, 0)` when the client for
        it could not be made (the user has no credentials, say), which is not
        an error here — the live client reports that in its own words.
        """
        loop = asyncio.get_running_loop()
        key = (id(loop), snap.table_id, user_id)
        with self._guard:
            task = self._tasks.get(key)
            if task is not None and not task.done():
                return task, self._started.get(key, time.monotonic())
        try:
            client = await client_factory(cq)
        except Exception as e:
            logger.warning("user_copy.client_unavailable",
                           extra={"table": snap.name, "user": user_id, "error": str(e)})
            return None, 0.0
        if client is None:
            return None, 0.0
        client.extraction_limits = fast
        if snap.target and hasattr(client, "attach_table_metadata"):
            client.extraction_target = dict(snap.target)

        connection_id = str(connection.id)
        started = time.monotonic()
        with self._guard:
            task = self._tasks.get(key)
            if task is not None and not task.done():
                return task, self._started.get(key, started)
            task = loop.create_task(self._build(
                connection_id=connection_id, org_id=org_id, snap=snap, user_id=user_id,
                fingerprint=fingerprint, limits=limits, fast=fast, client=client,
                keep_replaced=keep_replaced,
            ))
            self._tasks[key] = task
            self._started[key] = started

        def _done(t, key=key):
            with self._guard:
                if self._tasks.get(key) is t:
                    self._tasks.pop(key, None)
                    self._started.pop(key, None)
            if not t.cancelled() and t.exception() is not None:
                logger.error("user_copy.build.crashed", exc_info=t.exception())

        task.add_done_callback(_done)
        return task, started

    async def _build(self, *, connection_id: str, org_id: str, snap: _Snapshot, user_id: str,
                     fingerprint: str, limits, fast, client, keep_replaced: bool = False) -> None:
        async with self._semaphore(connection_id, limits.concurrency):
            with artifacts.try_user_copy_lock(connection_id, snap.table_id, user_id) as acquired:
                if not acquired:
                    # Another worker is building this very copy; its result
                    # lands in the same row.
                    await self._await_other_builder(snap.table_id, user_id, limits.wait_seconds)
                    return
                await self._build_locked(
                    connection_id=connection_id, org_id=org_id, snap=snap, user_id=user_id,
                    fingerprint=fingerprint, limits=limits, fast=fast, client=client,
                    keep_replaced=keep_replaced)

    async def _await_other_builder(self, table_id: str, user_id: str, seconds: int) -> None:
        deadline = time.monotonic() + max(0, int(seconds))
        while time.monotonic() < deadline:
            async def read(db):
                return (await db.execute(
                    select(CustomTableUserCopy.status).where(
                        CustomTableUserCopy.connection_table_id == table_id,
                        CustomTableUserCopy.user_id == user_id)
                )).scalar_one_or_none()

            async with _session_maker()() as db:
                status = await read(db)
            if status != STATUS_BUILDING:
                return
            await asyncio.sleep(0.5)

    async def _mark_building(self, *, connection_id: str, org_id: str, snap: _Snapshot,
                             user_id: str, started_at: datetime) -> Optional[tuple]:
        """Create or mark the row; returns `(copy_id, previous_artifact_path)`,
        or None when the table is gone."""

        async def work(db):
            table = (await db.execute(
                select(ConnectionTable.id).where(
                    ConnectionTable.id == snap.table_id, ConnectionTable.deleted_at.is_(None))
            )).scalar_one_or_none()
            if table is None:
                return None
            copy = (await db.execute(
                select(CustomTableUserCopy).where(
                    CustomTableUserCopy.connection_table_id == snap.table_id,
                    CustomTableUserCopy.user_id == user_id)
            )).scalar_one_or_none()
            if copy is None:
                copy = CustomTableUserCopy(
                    connection_table_id=snap.table_id, connection_id=connection_id,
                    organization_id=org_id, user_id=user_id, status=STATUS_BUILDING,
                    building_since=started_at, last_used_at=started_at,
                )
                db.add(copy)
                await db.flush()
                return str(copy.id), None
            copy.status = STATUS_BUILDING
            copy.building_since = started_at
            copy.error = None
            return str(copy.id), copy.artifact_path

        return await _write(work)

    def _retire(self, path: str, keep: bool) -> None:
        """Delete the file a rebuild replaced: now, or after the grace period
        when a running chat may still be reading it."""
        delay = self.retire_replaced_after if keep else 0
        if delay <= 0:
            artifacts.delete_artifact(path)
            return
        try:
            asyncio.get_running_loop().call_later(delay, artifacts.delete_artifact, path)
        except RuntimeError:
            artifacts.delete_artifact(path)

    async def _build_locked(self, *, connection_id: str, org_id: str, snap: _Snapshot,
                            user_id: str, fingerprint: str, limits, fast, client,
                            keep_replaced: bool = False) -> None:
        started_at = datetime.utcnow()
        t0 = time.monotonic()
        try:
            marked = await self._mark_building(
                connection_id=connection_id, org_id=org_id, snap=snap, user_id=user_id,
                started_at=started_at)
        except Exception:
            logger.exception("user_copy.mark_building_failed")
            return
        if marked is None:
            return
        copy_id, previous_path = marked
        abort = threading.Event()

        def work():
            est = extractor.estimate(client, snap.sql)
            extractor.check_budget(est, max_rows=fast.max_rows, max_bytes=fast.max_bytes,
                                   max_scan_bytes=fast.max_scan_bytes)
            return extractor.extract_to_artifact(
                client, snap.sql, snap.name, connection_id,
                max_rows=fast.max_rows, max_bytes=fast.max_bytes,
                should_abort=abort.is_set,
            )

        try:
            res = await asyncio.to_thread(work)
        except asyncio.CancelledError:
            abort.set()
            with suppress(Exception):
                await asyncio.shield(self._record_error(
                    copy_id, "The build was cancelled before it completed.", started_at))
            raise
        except BaseException as e:
            logger.warning("user_copy.build.failed",
                           extra={"table": snap.name, "user": user_id, "error": str(e)[:500]})
            with suppress(Exception):
                await self._record_error(copy_id, str(e), started_at,
                                         definition_hash=snap.definition_hash,
                                         fingerprint=fingerprint)
            return

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        published = False
        try:
            published = await self._publish(
                copy_id=copy_id, snap=snap, fingerprint=fingerprint, res=res,
                started_at=started_at, elapsed_ms=elapsed_ms)
        except Exception:
            logger.exception("user_copy.publish_failed")
        if not published:
            # Deleted, invalidated or superseded while it was building: this
            # file must not come back.
            if not await self._is_referenced(res.artifact_path):
                artifacts.delete_artifact(res.artifact_path)
            return
        if previous_path and previous_path != res.artifact_path:
            self._retire(previous_path, keep_replaced)
        logger.info("user_copy.build.ok", extra={
            "table": snap.name, "user": user_id, "rows": res.row_count,
            "bytes": res.artifact_bytes, "ms": elapsed_ms,
        })
        with suppress(Exception):
            await self.enforce_quota(org_id, limits.max_total_bytes, keep_id=copy_id)

    async def _record_error(self, copy_id: str, message: str, started_at: datetime, *,
                            definition_hash: Optional[str] = None,
                            fingerprint: Optional[str] = None) -> None:
        """A cancelled build passes no hashes: it says nothing about whether
        the next attempt would fail, so it is retried at once."""
        async def work(db):
            await db.execute(
                update(CustomTableUserCopy)
                .where(CustomTableUserCopy.id == copy_id,
                       CustomTableUserCopy.building_since == started_at)
                .values(status=STATUS_ERROR, error=(message or "Build failed")[:ERROR_LIMIT],
                        failed_definition_hash=definition_hash,
                        failed_credential_fingerprint=fingerprint)
                .execution_options(synchronize_session=False))

        await _write(work)

    async def _publish(self, *, copy_id: str, snap: _Snapshot, fingerprint: str, res,
                       started_at: datetime, elapsed_ms: int) -> bool:
        async def work(db):
            live = (await db.execute(
                select(ConnectionTable.id).where(
                    ConnectionTable.id == snap.table_id, ConnectionTable.deleted_at.is_(None))
            )).scalar_one_or_none()
            if live is None:
                return False
            done = await db.execute(
                update(CustomTableUserCopy)
                .where(
                    CustomTableUserCopy.id == copy_id,
                    # Still the build this run started, and nothing asked for
                    # the data to be thrown away while it ran.
                    CustomTableUserCopy.building_since == started_at,
                    or_(CustomTableUserCopy.invalidated_at.is_(None),
                        CustomTableUserCopy.invalidated_at < started_at),
                )
                .values(
                    status=STATUS_OK, error=None,
                    artifact_path=res.artifact_path,
                    artifact_key_enc=artifacts.encrypt_key(res.artifact_key),
                    artifact_bytes=res.artifact_bytes, row_count=res.row_count,
                    columns=res.columns, definition_hash=snap.definition_hash,
                    credential_fingerprint=fingerprint,
                    refreshed_at=datetime.utcnow(), build_ms=elapsed_ms,
                )
                .execution_options(synchronize_session=False))
            return done.rowcount == 1

        return bool(await _write(work))

    @staticmethod
    async def _is_referenced(path: Optional[str]) -> bool:
        if not path:
            return False
        try:
            async with _session_maker()() as db:
                return (await db.execute(
                    select(CustomTableUserCopy.id)
                    .where(CustomTableUserCopy.artifact_path == path).limit(1)
                )).first() is not None
        except Exception:
            return True     # cannot tell: leave the file for the daily sweep

    async def wait_for_builds(self, timeout: float = 60.0) -> None:
        """Wait for this loop's builds in flight (tests, graceful shutdown)."""
        loop = asyncio.get_running_loop()
        with self._guard:
            tasks = [t for k, t in self._tasks.items() if k[0] == id(loop) and not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    # -- invalidation and removal -------------------------------------------

    @staticmethod
    def _where(*, table_id=None, user_id=None, connection_id=None, organization_id=None,
               copy_id=None) -> list:
        clauses = []
        if copy_id is not None:
            clauses.append(CustomTableUserCopy.id == str(copy_id))
        if table_id is not None:
            clauses.append(CustomTableUserCopy.connection_table_id == str(table_id))
        if user_id is not None:
            clauses.append(CustomTableUserCopy.user_id == str(user_id))
        if connection_id is not None:
            clauses.append(CustomTableUserCopy.connection_id == str(connection_id))
        if organization_id is not None:
            clauses.append(CustomTableUserCopy.organization_id == str(organization_id))
        if not clauses:
            raise ValueError("refusing to act on every user copy: name a scope")
        return clauses

    async def request_refresh(self, db, **scope) -> int:
        """Ask for the matching copies to be rebuilt at their next use. They
        stay served meanwhile (a sign-in does this)."""
        done = await db.execute(
            update(CustomTableUserCopy).where(*self._where(**scope))
            .values(refresh_requested_at=datetime.utcnow())
            .execution_options(synchronize_session=False))
        await db.commit()
        return int(done.rowcount or 0)

    async def invalidate(self, db, **scope) -> int:
        """Stop serving the matching copies until they are rebuilt. The files
        stay until the rebuild replaces them or cleanup removes them."""
        done = await db.execute(
            update(CustomTableUserCopy).where(*self._where(**scope))
            .values(invalidated_at=datetime.utcnow())
            .execution_options(synchronize_session=False))
        await db.commit()
        return int(done.rowcount or 0)

    async def purge(self, db, *, forget: bool = False, **scope) -> dict:
        """Delete the matching copies' files now.

        The row stays as `evicted` unless `forget` is set: it is how a build
        still in flight learns it must not publish, and how the next sign-in
        knows what this user works with. `forget` removes the rows too (the
        table or the user is gone).
        """
        rows = (await db.execute(
            select(CustomTableUserCopy).where(*self._where(**scope))
        )).scalars().all()
        paths = [r.artifact_path for r in rows if r.artifact_path]
        reclaimed = sum(int(r.artifact_bytes or 0) for r in rows if r.artifact_path)
        now = datetime.utcnow()
        ids = [str(r.id) for r in rows]
        if ids:
            if forget:
                await db.execute(delete(CustomTableUserCopy).where(CustomTableUserCopy.id.in_(ids))
                                 .execution_options(synchronize_session=False))
            else:
                await db.execute(
                    update(CustomTableUserCopy).where(CustomTableUserCopy.id.in_(ids))
                    .values(status=STATUS_EVICTED, artifact_path=None, artifact_key_enc=None,
                            artifact_bytes=None, invalidated_at=now, error=None)
                    .execution_options(synchronize_session=False))
            await db.commit()
        # Only after the references are gone: a file is never deleted while a
        # row still points at it.
        for path in paths:
            artifacts.delete_artifact(path)
        if ids:
            logger.info("user_copy.purged", extra={"copies": len(ids), "bytes": reclaimed, **{
                k: str(v) for k, v in scope.items()}})
        return {"copies": len(ids), "reclaimed_bytes": reclaimed}

    async def enforce_quota(self, organization_id: str, max_total_bytes: int,
                            keep_id: Optional[str] = None) -> dict:
        """Evict the least recently used copies until the organization's
        copies fit its storage limit. The copy just built is never the one
        to go."""
        async with _session_maker()() as db:
            total = int((await db.execute(
                select(func.coalesce(func.sum(CustomTableUserCopy.artifact_bytes), 0)).where(
                    CustomTableUserCopy.organization_id == str(organization_id),
                    CustomTableUserCopy.artifact_path.is_not(None))
            )).scalar() or 0)
            if total <= max_total_bytes:
                return {"copies": 0, "reclaimed_bytes": 0}
            rows = (await db.execute(
                select(CustomTableUserCopy.id, CustomTableUserCopy.artifact_bytes).where(
                    CustomTableUserCopy.organization_id == str(organization_id),
                    CustomTableUserCopy.artifact_path.is_not(None))
                .order_by(CustomTableUserCopy.last_used_at.asc().nullsfirst(),
                          CustomTableUserCopy.refreshed_at.asc().nullsfirst())
            )).all()
            out = {"copies": 0, "reclaimed_bytes": 0}
            for copy_id, size in rows:
                if total <= max_total_bytes:
                    break
                if keep_id and str(copy_id) == str(keep_id):
                    continue
                done = await self.purge(db, copy_id=copy_id)
                total -= int(size or 0)
                out["copies"] += done["copies"]
                out["reclaimed_bytes"] += done["reclaimed_bytes"]
            if out["copies"]:
                logger.info("user_copy.quota_evicted",
                            extra={"organization_id": str(organization_id), **out})
            return out

    # -- listing ------------------------------------------------------------

    async def list_for_table(self, db, table_id: str) -> list:
        """Every user's copy of one table, for the admin's view of it."""
        from app.models.user import User

        rows = (await db.execute(
            select(CustomTableUserCopy, User.email, User.name)
            .outerjoin(User, User.id == CustomTableUserCopy.user_id)
            .where(CustomTableUserCopy.connection_table_id == str(table_id))
            .order_by(CustomTableUserCopy.last_used_at.desc().nullslast())
        )).all()
        return [self.describe(copy, email=email, name=name) for copy, email, name in rows]

    @staticmethod
    def describe(copy: CustomTableUserCopy, *, email: str = None, name: str = None) -> dict:
        status = copy.status
        if status == STATUS_BUILDING and copy.building_since is not None \
                and datetime.utcnow() - copy.building_since > INTERRUPTED_AFTER:
            status = STATUS_ERROR
        return {
            "id": str(copy.id),
            "user_id": str(copy.user_id),
            "user_email": email,
            "user_name": name,
            "status": status,
            "error": copy.error if status == STATUS_ERROR else None,
            "row_count": copy.row_count if copy.artifact_path else None,
            "artifact_bytes": copy.artifact_bytes if copy.artifact_path else None,
            "refreshed_at": copy.refreshed_at if copy.artifact_path else None,
            "last_used_at": copy.last_used_at,
            "build_ms": copy.build_ms,
        }

    async def summary_for_tables(self, db, table_ids: list) -> dict:
        """`{table_id: {"copies": n, "bytes": b}}` — what the tables list shows."""
        if not table_ids:
            return {}
        rows = (await db.execute(
            select(CustomTableUserCopy.connection_table_id,
                   func.count(CustomTableUserCopy.id),
                   func.coalesce(func.sum(CustomTableUserCopy.artifact_bytes), 0))
            .where(CustomTableUserCopy.connection_table_id.in_([str(t) for t in table_ids]),
                   CustomTableUserCopy.artifact_path.is_not(None))
            .group_by(CustomTableUserCopy.connection_table_id)
        )).all()
        return {str(t): {"copies": int(n or 0), "bytes": int(b or 0)} for t, n, b in rows}

    # -- cleanup ------------------------------------------------------------

    async def cleanup(self, session_factory=None) -> dict:
        """Delete copies nobody should be holding any more.

        Run daily. Removes a copy when its table or connection is gone or the
        connection is no longer per-user; when its user is deactivated or has
        left the organization; when it has not been used for the
        organization's idle period; and when it is past the maximum age (it
        would not be served again without a rebuild, so the file is only
        taking space). Then brings each organization back under its storage
        limit.
        """
        from app.models.membership import Membership
        from app.models.user import User

        factory = session_factory or _session_maker()
        totals = {"copies": 0, "reclaimed_bytes": 0}

        def add(done: dict) -> None:
            totals["copies"] += done["copies"]
            totals["reclaimed_bytes"] += done["reclaimed_bytes"]

        async with factory() as db:
            now = datetime.utcnow()
            rows = (await db.execute(
                select(CustomTableUserCopy, ConnectionTable.deleted_at, ConnectionTable.kind,
                       Connection.auth_policy, Connection.deleted_at, User.is_active)
                .outerjoin(ConnectionTable,
                           ConnectionTable.id == CustomTableUserCopy.connection_table_id)
                .outerjoin(Connection, Connection.id == CustomTableUserCopy.connection_id)
                .outerjoin(User, User.id == CustomTableUserCopy.user_id)
            )).all()
            members = {
                (str(u), str(o)) for u, o in (await db.execute(
                    select(Membership.user_id, Membership.organization_id)
                    .where(Membership.deleted_at.is_(None))
                )).all()
            }
            limits_by_org: dict = {}
            for copy, table_deleted, kind, auth_policy, conn_deleted, user_active in rows:
                org_id = str(copy.organization_id)
                if org_id not in limits_by_org:
                    limits_by_org[org_id] = (await self.limits_for(db, org_id))[0]
                limits = limits_by_org[org_id]
                gone = (
                    kind != KIND_BOW or table_deleted is not None or conn_deleted is not None
                    or (auth_policy or "system_only") != "user_required"
                    or not user_active
                    or (str(copy.user_id), org_id) not in members
                )
                if gone:
                    add(await self.purge(db, copy_id=copy.id, forget=True))
                    continue
                if not copy.artifact_path:
                    # Remembered only so a sign-in can tell what to rebuild.
                    seen = copy.last_used_at or copy.updated_at or copy.created_at
                    if seen is not None and (now - seen).total_seconds() > 4 * limits.idle_seconds:
                        add(await self.purge(db, copy_id=copy.id, forget=True))
                    continue
                last = copy.last_used_at or copy.refreshed_at or copy.created_at
                idle = last is not None and (now - last).total_seconds() > limits.idle_seconds
                expired = copy.refreshed_at is not None and \
                    (now - copy.refreshed_at).total_seconds() > limits.max_age_seconds
                if idle or expired:
                    add(await self.purge(db, copy_id=copy.id))
            for org_id, limits in limits_by_org.items():
                add(await self.enforce_quota(org_id, limits.max_total_bytes))
        if totals["copies"]:
            logger.info("user_copy.cleanup", extra=totals)
        return totals

    async def scheduled_cleanup(self, session_factory=None) -> dict:
        """Shared job stores fire in every worker; one claim owns each sweep."""
        from app.core.scheduler import claim_scheduled_run

        if not await asyncio.to_thread(claim_scheduled_run, "cleanup_custom_table_user_copies"):
            return {"copies": 0, "reclaimed_bytes": 0}
        return await self.cleanup(session_factory)


user_copy_service = CustomTableUserCopyService()


async def scheduled_cleanup_user_copies() -> dict:
    """APScheduler entry point (a module-level name, so the shared job store
    can reference it)."""
    return await user_copy_service.scheduled_cleanup()


async def refresh_on_sign_in(user_id: str) -> None:
    """A sign-in asks for this user's copies to be rebuilt at their next use.

    They are still served meanwhile — they are this user's own rows and still
    inside the maximum age — so the first question after signing in is not
    held up, and the answer after it comes from fresh data. Never raises: a
    sign-in must not fail over a cache.
    """
    try:
        async with _session_maker()() as db:
            await user_copy_service.request_refresh(db, user_id=str(user_id))
    except Exception:
        logger.warning("user_copy.refresh_on_sign_in_failed", exc_info=True)


async def referenced_paths(db) -> list:
    """Every artifact a user copy points at, for the orphan sweep."""
    return list((await db.execute(
        select(CustomTableUserCopy.artifact_path)
        .where(CustomTableUserCopy.artifact_path.is_not(None))
    )).scalars())


__all__ = [
    "CopyRef", "CustomTableUserCopyService", "definition_hash_of", "referenced_paths",
    "refresh_on_sign_in", "scheduled_cleanup_user_copies", "user_copy_service",
]
