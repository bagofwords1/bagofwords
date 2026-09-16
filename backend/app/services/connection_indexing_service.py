"""Connection indexing service — runs `refresh_schema` in the background
and tracks progress in the `connection_indexings` table.

Jobs execute on a dedicated daemon-thread event loop (`_get_background_loop`).
The request thread calls `asyncio.run_coroutine_threadsafe` to submit and
returns immediately — so the HTTP POST completes in milliseconds even if
the job takes minutes. A persistent loop (rather than the request's loop)
means the runner survives request completion in every deployment mode we
support, including FastAPI's sync TestClient used in e2e tests.

Multi-worker safety (per-pod election / APScheduler-backed runner) is a
follow-up hardening step.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, desc, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.data_sources.clients.progress import IndexingCancelled
from app.models.connection import Connection
from app.models.connection_indexing import (
    ConnectionIndexing,
    ConnectionIndexingStatus,
    TERMINAL_INDEXING_STATUSES,
)
from app.services.connection_identity import catalog_requires_user_sign_in


logger = logging.getLogger(__name__)


# Cooperative cancellation. Keyed by indexing_id. The request thread sets the
# event via `request_cancel`; the runner thread (and the clients it drives via
# progress/cancel checks) poll it and abort at the next checkpoint — between
# schema items and, crucially, mid QVD→Parquet convert so a 40-minute job can
# actually be stopped. Guarded by a lock because setter and reader live on
# different threads.
_cancel_events: dict[str, threading.Event] = {}
_cancel_lock = threading.Lock()


def _get_cancel_event(indexing_id: str) -> threading.Event:
    with _cancel_lock:
        ev = _cancel_events.get(indexing_id)
        if ev is None:
            ev = threading.Event()
            _cancel_events[indexing_id] = ev
        return ev


def _clear_cancel_event(indexing_id: str) -> None:
    with _cancel_lock:
        _cancel_events.pop(indexing_id, None)


# A single daemon thread runs an event loop for the whole process — any thread
# can submit coroutines via `asyncio.run_coroutine_threadsafe(..., loop)`. This
# is what makes the runner survive the request that spawned it.
_background_loop: "asyncio.AbstractEventLoop | None" = None
_background_loop_lock = threading.Lock()


def _start_background_loop() -> asyncio.AbstractEventLoop:
    loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            loop.close()

    t = threading.Thread(target=_run, name="connection-indexing-loop", daemon=True)
    t.start()
    return loop


#: How many indexing runs may crawl a source at once.
#: Creating connections in bulk (an import, a scripted rollout) dispatched one
#: job per connection with nothing holding them back, so 100 new SharePoint
#: connections opened 100 simultaneous Graph crawls — enough to saturate the
#: loop and the outbound pool, and to slow down unrelated requests (including
#: an in-flight login) for minutes. Jobs still start immediately; they just
#: queue on this gate instead of all crawling at once.
#: Override with BOW_INDEXING_CONCURRENCY.
_INDEXING_CONCURRENCY = max(1, int(os.environ.get("BOW_INDEXING_CONCURRENCY", "4")))
_indexing_gate: "asyncio.Semaphore | None" = None
_indexing_gate_lock = threading.Lock()


def _get_indexing_gate() -> asyncio.Semaphore:
    """The concurrency gate, created on first use.

    Only ever awaited from the background loop, so a single instance is safe.
    """
    global _indexing_gate
    with _indexing_gate_lock:
        if _indexing_gate is None:
            _indexing_gate = asyncio.Semaphore(_INDEXING_CONCURRENCY)
        return _indexing_gate


def _get_background_loop() -> asyncio.AbstractEventLoop:
    global _background_loop
    with _background_loop_lock:
        if _background_loop is None or _background_loop.is_closed():
            _background_loop = _start_background_loop()
        return _background_loop


def shutdown_background_loop(timeout: float = 5.0) -> None:
    """Cancel pending tasks on the bg loop and stop it. Used by tests to keep
    a leaked indexing job from holding a Postgres `idle in transaction` lock
    across test boundaries (which blocks the per-test schema reset).
    """
    global _background_loop, _indexing_gate
    with _background_loop_lock:
        loop = _background_loop
        # The gate belongs to the loop it was created on; drop it alongside so
        # the next loop builds a fresh one.
        with _indexing_gate_lock:
            _indexing_gate = None
        if loop is None or loop.is_closed():
            _background_loop = None
            return

    async def _cancel_all() -> None:
        tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await asyncio.wait_for(asyncio.shield(t), timeout=timeout)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass

    try:
        fut = asyncio.run_coroutine_threadsafe(_cancel_all(), loop)
        fut.result(timeout=timeout + 1.0)
    except Exception:
        pass
    loop.call_soon_threadsafe(loop.stop)
    with _background_loop_lock:
        _background_loop = None


# How often we flush progress updates to the DB. Progress callbacks from the
# client loop can fire thousands of times; we coalesce into one write per
# `_PROGRESS_FLUSH_SECONDS` (plus one final flush at end-of-phase).
_PROGRESS_FLUSH_SECONDS = 0.25

# Per-run event log cap. Keep enough to be useful, drop oldest beyond.
_EVENT_LOG_MAX = 200


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Liveness. A run lives only in the process that started it, so a restart,
# deploy or OOM kill mid-run left its row `running` forever: every reindex
# returned that dead row as "in flight", the scheduled sweeper was blocked, and
# the UI showed a job that would never finish. The runner now touches
# `updated_at` every `_HEARTBEAT_SECONDS` (while queued on the gate, too); a
# non-terminal row silent for `_STALE_AFTER_SECONDS` has no runner anywhere and
# is failed the next time anyone reads it. Staleness is judged by the heartbeat
# rather than "everything active at boot", so one pod restarting never fails a
# run that is alive on another pod.
_HEARTBEAT_SECONDS = 30.0
_STALE_AFTER_SECONDS = _env_seconds("BOW_INDEXING_STALE_AFTER_S", 600)

# Stage timeouts. Discovery runs blocking client calls in a worker thread with
# no timeout of their own, so one hung catalog query held its gate slot — and,
# with `_INDEXING_CONCURRENCY` of them, every other connection's indexing —
# indefinitely.
#
# Discovery is stopped when the source reports NO PROGRESS for
# `_IDLE_TIMEOUT_SECONDS`, not when it has merely run long: a fixed total
# ceiling is simultaneously too long for a 2-minute SQL crawl that hung (the
# user waits the whole ceiling) and too short for a healthy multi-hour Power BI
# or SharePoint tenant walk (killed mid-way, and again on every retry). The
# total ceiling stays only as a last-resort bound.
#
# Warm keeps a total ceiling only — a single large QVD→Parquet convert can be
# legitimately quiet for a long time — and a warm timeout is non-fatal (the
# catalog is already indexed; the scheduled warmup retries).
_IDLE_TIMEOUT_SECONDS = _env_seconds("BOW_INDEXING_IDLE_TIMEOUT_S", 900)
_DISCOVERY_TIMEOUT_SECONDS = _env_seconds("BOW_INDEXING_TIMEOUT_S", 14400)
_WARM_TIMEOUT_SECONDS = _env_seconds("BOW_INDEXING_WARM_TIMEOUT_S", 7200)


class IndexingStageTimeout(Exception):
    """A stage of an indexing run ran out of time — in total, or (``idle``)
    without reporting any progress."""

    def __init__(self, stage: str, seconds: float, *, idle: bool = False,
                 last_phase: str | None = None):
        self.stage = stage
        self.seconds = seconds
        self.idle = idle
        minutes = max(1, round(seconds / 60))
        if idle:
            where = f" (last stage: {last_phase})" if last_phase else ""
            message = f"{stage} made no progress for {minutes} minute(s){where} and was stopped"
        else:
            message = f"{stage} did not finish within {minutes} minute(s) and was stopped"
        super().__init__(message)


async def _with_stage_timeout(
    coro,
    seconds: float,
    stage: str,
    *,
    idle_seconds: float | None = None,
    last_activity=None,
    last_phase=None,
):
    """Await `coro`, stopping it after `seconds` in total or — when
    `idle_seconds` is given — once `last_activity()` (a `time.perf_counter()`
    reading) is older than `idle_seconds`.

    Only these deadlines become `IndexingStageTimeout`; a TimeoutError raised by
    the work itself (a driver's own socket timeout) propagates unchanged.
    """
    task = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    poll = min(1.0, idle_seconds / 4) if idle_seconds else None
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise IndexingStageTimeout(stage, seconds)
            done, _ = await asyncio.wait(
                {task}, timeout=min(remaining, poll) if poll else remaining
            )
            if done:
                return task.result()
            if idle_seconds and time.perf_counter() - last_activity() >= idle_seconds:
                raise IndexingStageTimeout(
                    stage, idle_seconds, idle=True,
                    last_phase=last_phase() if last_phase else None,
                )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.wait({task})


def _human_bytes(n: int) -> str:
    """Compact human-readable size (e.g. 1.5 GB) for indexing log lines."""
    step = 1024.0
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < step or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= step
    return f"{size:.1f} PB"


class ConnectionIndexingService:
    """Create, poll, and (internally) run `ConnectionIndexing` rows."""

    @staticmethod
    def _scope_clause(user_id: Optional[str]):
        """Restrict a query to one indexing scope.

        `user_id=None` means the ORG-shared run (`user_id IS NULL`), not "any
        scope" — a user's own catalog sync must never be mistaken for the shared
        one, in either direction: the shared run would otherwise refuse to start
        while somebody's OneDrive sync was in flight.
        """
        if user_id is None:
            return ConnectionIndexing.user_id.is_(None)
        return ConnectionIndexing.user_id == str(user_id)

    async def get_latest(
        self,
        db: AsyncSession,
        connection_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[ConnectionIndexing]:
        """Return the most recent indexing row for a connection scope (any status)."""
        result = await db.execute(
            select(ConnectionIndexing)
            .where(
                ConnectionIndexing.connection_id == str(connection_id),
                self._scope_clause(user_id),
            )
            .order_by(desc(ConnectionIndexing.created_at))
            .limit(1)
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        await self._reap_if_stale(db, row)
        return row

    async def get_active(
        self,
        db: AsyncSession,
        connection_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[ConnectionIndexing]:
        """Return the current pending/running indexing row for a connection scope."""
        result = await db.execute(
            select(ConnectionIndexing)
            .where(
                ConnectionIndexing.connection_id == str(connection_id),
                self._scope_clause(user_id),
                ConnectionIndexing.status.in_([
                    ConnectionIndexingStatus.PENDING.value,
                    ConnectionIndexingStatus.RUNNING.value,
                ]),
            )
            .order_by(desc(ConnectionIndexing.created_at))
            .limit(1)
            # Re-read the heartbeat: callers poll this on one session
            # (`wait_for_active`), and an identity-map copy of `updated_at`
            # would make a live run look stale.
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        if await self._reap_if_stale(db, row):
            return None
        return row

    async def _reap_if_stale(
        self, db: AsyncSession, row: Optional[ConnectionIndexing]
    ) -> bool:
        """Fail a non-terminal row whose runner stopped heartbeating.

        Returns True when the row was reaped. See `_STALE_AFTER_SECONDS`.
        """
        if row is None or row.is_terminal():
            return False
        last_seen = row.updated_at or row.created_at
        if last_seen is None:
            return False
        silent_s = (datetime.utcnow() - last_seen).total_seconds()
        if silent_s < _STALE_AFTER_SECONDS:
            return False
        row.status = ConnectionIndexingStatus.FAILED.value
        row.finished_at = datetime.utcnow()
        row.error = (
            f"Indexing stopped responding (no progress for {round(silent_s / 60)} "
            "minute(s)) — the server most likely restarted during the run. "
            "Reindex to start it again."
        )
        await db.commit()
        # Should a slow-but-alive runner exist in this process after all, stop
        # it at its next checkpoint rather than let it race a fresh run.
        with _cancel_lock:
            ev = _cancel_events.get(str(row.id))
        if ev is not None:
            ev.set()
        logger.warning(
            "indexing.reaped_stale",
            extra={
                "connection_id": str(row.connection_id),
                "indexing_id": str(row.id),
                "silent_s": round(silent_s),
            },
        )
        return True

    async def request_cancel(
        self,
        db: AsyncSession,
        connection_id: str,
        user_id: Optional[str] = None,
    ) -> Optional[ConnectionIndexing]:
        """Request cancellation of the active indexing run for a connection scope.

        Signals the running task to stop cooperatively (kills an in-flight QVD
        convert subprocess mid-stream) and optimistically marks the row
        `cancelled` so polling reflects it immediately. The runner re-checks the
        cancel event before finalizing and will not flip a cancelled row back to
        completed. Returns the row, or None if nothing was active.
        """
        row = await self.get_active(db, connection_id, user_id=user_id)
        if row is None:
            return None
        _get_cancel_event(str(row.id)).set()
        row.status = ConnectionIndexingStatus.CANCELLED.value
        row.finished_at = datetime.utcnow()
        row.error = "Cancelled by user"
        await db.commit()
        await db.refresh(row)
        logger.info(
            "indexing.cancel.requested",
            extra={"connection_id": str(connection_id), "indexing_id": str(row.id)},
        )
        return row

    async def wait_for_active(
        self,
        db: AsyncSession,
        connection_id: str,
        *,
        poll_interval_s: float = 0.05,
        timeout_s: float = 600.0,
    ) -> None:
        """Block until any pending/running indexing for this connection reaches a
        terminal state. Used by sync paths (e.g. data-source-level refresh) that
        need a deterministic post-condition. Polls the row's status — runs on
        the request thread. Raises on timeout so callers can surface a clear
        error rather than proceeding on stale state.
        """
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            active = await self.get_active(db, connection_id)
            if active is None:
                return
            await asyncio.sleep(poll_interval_s)
        logger.warning(
            "indexing.wait_for_active.timeout",
            extra={"connection_id": str(connection_id), "timeout_s": timeout_s},
        )
        raise TimeoutError(
            f"Indexing for connection {connection_id} did not finish within {timeout_s}s"
        )

    async def start(
        self,
        db: AsyncSession,
        connection: Connection,
        *,
        user_id: Optional[str] = None,
        kick_off: bool = True,
    ) -> ConnectionIndexing:
        """Create a pending indexing row and (unless already in-flight) kick off
        the background runner. Idempotent — returns the active row if one
        already exists.

        `user_id` selects the scope: omit it for the org-shared catalog run, pass
        a user for a per-user catalog sync (OneDrive / personal Drive after that
        user signs in). The two scopes are independent, so a user signing in
        never blocks — or is blocked by — the shared run.
        """
        existing = await self.get_active(db, str(connection.id), user_id=user_id)
        if existing is not None:
            return existing

        row = ConnectionIndexing(
            connection_id=str(connection.id),
            user_id=str(user_id) if user_id else None,
            status=ConnectionIndexingStatus.PENDING.value,
            phase=None,
            progress_done=0,
            progress_total=0,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        if kick_off:
            loop = _get_background_loop()
            asyncio.run_coroutine_threadsafe(self._run(row.id), loop)

        return row

    async def _run(self, indexing_id: str) -> None:
        """Gate wrapper around `_run_inner`.

        Runs queue on `_get_indexing_gate()` so a bulk dispatch (N connections
        created at once) crawls a few sources at a time instead of all N. The
        row stays PENDING while queued, which is what the UI already renders.
        """
        heartbeat = asyncio.create_task(self._heartbeat(indexing_id))
        try:
            async with _get_indexing_gate():
                await self._run_inner(indexing_id)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _heartbeat(self, indexing_id: str) -> None:
        """Touch the row's `updated_at` while the run is queued or running, so
        `_reap_if_stale` can tell a live run from one whose process died. Runs
        on the loop, not the worker thread, so a blocking client call cannot
        starve it. Stops on its own once the row turns terminal."""
        from app.settings.database import create_async_database_engine_for_indexing

        engine = create_async_database_engine_for_indexing()
        try:
            while True:
                await asyncio.sleep(_HEARTBEAT_SECONDS)
                try:
                    async with AsyncSession(engine) as hb_db:
                        result = await hb_db.execute(
                            update(ConnectionIndexing)
                            .where(
                                ConnectionIndexing.id == indexing_id,
                                ConnectionIndexing.status.in_([
                                    ConnectionIndexingStatus.PENDING.value,
                                    ConnectionIndexingStatus.RUNNING.value,
                                ]),
                            )
                            .values(updated_at=datetime.utcnow())
                        )
                        await hb_db.commit()
                    if result.rowcount == 0:
                        return
                except Exception:
                    logger.debug("indexing.heartbeat_failed", exc_info=True)
        finally:
            await engine.dispose()

    async def _run_inner(self, indexing_id: str) -> None:
        """Runner that opens a fresh session and executes `refresh_schema` (SQL connections)
        or `refresh_tools` (MCP/custom_api connections).

        Exceptions are captured onto the row — never re-raised. The task must
        not let its wrapping session outlive work, so we open/close a session
        for each significant phase (mark-running, progress flush, finalize).
        """
        # Avoid circular import at module load.
        from app.services.connection_service import ConnectionService
        from app.settings.database import create_async_database_engine_for_indexing

        # The loop this coroutine is currently executing on. Progress callbacks
        # fire from worker threads (via `asyncio.to_thread` inside aget_schemas)
        # and must post their flush coroutine BACK to this loop.
        runner_loop = asyncio.get_running_loop()
        start = time.perf_counter()

        # Cancellation flag for this run — set by `request_cancel` from a request
        # thread, polled here and inside the client's schema/warm loops.
        cancel_event = _get_cancel_event(indexing_id)

        # Dedicated NullPool engine for this run — see
        # `create_async_database_engine_for_indexing` for the rationale.
        engine = create_async_database_engine_for_indexing()
        session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False,
        )

        def _new_session():
            return session_factory()

        async def _append_event(level: str, phase: str | None, message: str,
                                done: int = 0, total: int = 0) -> None:
            """Append a single entry to the indexing row's events_json.

            Best-effort: a failure to log must never affect the run. Events
            are capped at `_EVENT_LOG_MAX` (oldest dropped).
            """
            try:
                async with _new_session() as ev_db:
                    fresh = await ev_db.get(ConnectionIndexing, indexing_id)
                    if fresh is None:
                        return
                    events = list(fresh.events_json or [])
                    events.append({
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "level": level,
                        "phase": phase,
                        "message": message,
                        "done": done,
                        "total": total,
                    })
                    if len(events) > _EVENT_LOG_MAX:
                        events = events[-_EVENT_LOG_MAX:]
                    fresh.events_json = events
                    await ev_db.commit()
            except Exception:
                logger.debug("indexing.event_append_failed", exc_info=True)

        try:
            async with _new_session() as db:
                row = await db.get(ConnectionIndexing, indexing_id)
                if row is None:
                    logger.warning("indexing.run.missing", extra={"indexing_id": indexing_id})
                    return
                if row.is_terminal():
                    # Cancelled (or reaped) while queued on the gate — flipping
                    # it back to running would start work nobody wants.
                    return
                row.status = ConnectionIndexingStatus.RUNNING.value
                row.started_at = datetime.utcnow()
                row.last_activity_at = row.started_at
                await db.commit()

                await _append_event("info", None, "Indexing started")

                conn_result = await db.execute(
                    select(Connection).where(Connection.id == row.connection_id)
                )
                connection = conn_result.scalar_one_or_none()
                if connection is None:
                    row.status = ConnectionIndexingStatus.FAILED.value
                    row.error = "Connection not found"
                    row.finished_at = datetime.utcnow()
                    await db.commit()
                    return

                # Progress state shared across the runner loop and worker
                # thread (where the client's `get_schemas` runs). Reads/writes
                # are guarded by a `threading.Lock` so we always observe a
                # consistent (phase, item, done, total) tuple even when the
                # callback fires mid-flush.
                last_flush_at = 0.0
                pending_state: dict = {
                    "phase": None, "item": None, "done": 0, "total": 0,
                    # Last progress report: wall clock for the UI, monotonic
                    # for the inactivity timeout.
                    "activity_at": datetime.utcnow(),
                    "activity_mono": time.perf_counter(),
                }
                state_lock = threading.Lock()
                activity_events: list[tuple] = []
                last_activity = {"phase": None, "bucket": -1, "time": 0.0}
                flush_lock = asyncio.Lock()
                flush_scheduled = False
                last_scheduled_at = float("-inf")

                def _state_snapshot() -> dict:
                    with state_lock:
                        return dict(pending_state)

                def _last_activity() -> float:
                    with state_lock:
                        return pending_state["activity_mono"]

                def _last_phase() -> str | None:
                    with state_lock:
                        return pending_state["phase"]

                async def _flush(force: bool = False) -> None:
                    nonlocal last_flush_at
                    async with flush_lock:
                        now = time.perf_counter()
                        if not force and (now - last_flush_at) < _PROGRESS_FLUSH_SECONDS:
                            return
                        # Snapshot after acquiring the write lock: queued flushes
                        # must never write an older snapshot over newer progress.
                        with state_lock:
                            snap = dict(pending_state)
                            events = list(activity_events)
                            activity_events.clear()
                        try:
                            async with _new_session() as flush_db:
                                fresh = await flush_db.get(ConnectionIndexing, indexing_id)
                                if fresh is None:
                                    return
                                fresh.phase = snap["phase"]
                                fresh.current_item = snap["item"]
                                fresh.progress_done = snap["done"]
                                fresh.progress_total = snap["total"]
                                fresh.last_activity_at = snap["activity_at"]
                                history = list(fresh.events_json or [])
                                for ts, phase, item, done, total, transition in events:
                                    message = f"Phase: {phase}" if transition else str(item or phase)
                                    if total > 0:
                                        message += f" ({done}/{total})"
                                    history.append({"ts": ts, "level": "info", "phase": phase,
                                                    "message": message, "done": done, "total": total})
                                fresh.events_json = history[-_EVENT_LOG_MAX:]
                                await flush_db.commit()
                        except Exception:
                            with state_lock:
                                activity_events[:0] = events
                                del activity_events[:-_EVENT_LOG_MAX]
                            logger.debug("indexing.flush_failed", exc_info=True)
                        last_flush_at = now

                async def _scheduled_flush(delay: float):
                    nonlocal flush_scheduled
                    try:
                        if delay > 0:
                            await asyncio.sleep(delay)
                    finally:
                        # Re-arm BEFORE snapshotting: a report landing while this
                        # write is in progress must schedule its own flush, not
                        # be absorbed by one that already took its snapshot.
                        with state_lock:
                            flush_scheduled = False
                    await _flush(force=True)

                def progress_cb(phase, current_item, done, total):
                    nonlocal flush_scheduled, last_scheduled_at
                    # Called from inside `asyncio.to_thread(...)` — a worker
                    # thread (schema discovery or warm/convert). Raising here
                    # aborts that loop promptly on cancel.
                    if cancel_event.is_set():
                        raise IndexingCancelled()
                    # Update state under a lock so the runner loop never reads a
                    # torn (phase, item, done, total).
                    with state_lock:
                        # None counts (stage markers) keep the current ones, so
                        # a finished run still reports what discovery found.
                        if done is None:
                            done = pending_state["done"]
                        if total is None:
                            total = pending_state["total"]
                        pending_state["phase"] = phase
                        pending_state["item"] = current_item
                        pending_state["done"] = done
                        pending_state["total"] = total
                        now = time.perf_counter()
                        pending_state["activity_at"] = datetime.utcnow()
                        pending_state["activity_mono"] = now
                        transition = phase != last_activity["phase"]
                        bucket = int(10 * done / total) if total > 0 else -1
                        if phase and (transition or bucket > last_activity["bucket"] or
                                      (current_item and now - last_activity["time"] >= 5)):
                            activity_events.append((datetime.utcnow().isoformat() + "Z", phase,
                                                    current_item, done, total, transition))
                            del activity_events[:-_EVENT_LOG_MAX]
                            last_activity.update(phase=phase, bucket=bucket, time=now)

                        # Bound scheduled work as well as DB writes. A 35k-table
                        # crawl must not enqueue a coroutine for every column:
                        # at most one flush is pending at a time. It is a
                        # TRAILING flush (delayed, snapshotting when it runs), so
                        # the last report before a hang still reaches the row —
                        # dropping it would show the stage before the stuck one.
                        if flush_scheduled:
                            return
                        flush_scheduled = True
                        delay = max(0.0, _PROGRESS_FLUSH_SECONDS - (now - last_scheduled_at))
                        last_scheduled_at = now + delay

                    if runner_loop.is_closed():
                        with state_lock:
                            flush_scheduled = False
                        return
                    task = _scheduled_flush(delay)
                    try:
                        asyncio.run_coroutine_threadsafe(task, runner_loop)
                    except RuntimeError:
                        task.close()
                        with state_lock:
                            flush_scheduled = False

                def _stage(phase: str) -> None:
                    """Record entry into a stage that reports no progress of its
                    own (connecting, saving, syncing), so a run stuck there
                    shows WHERE — instead of the last stage that did report."""
                    progress_cb(phase, None, None, None)

                svc = ConnectionService()
                from app.schemas.data_source_registry import (
                    tool_provider_types,
                    data_shape_for,
                    catalog_nouns_for,
                    REGISTRY,
                )
                is_tool_provider = connection.type in tool_provider_types()
                # Copy is driven by the registry's data_shape (files / objects /
                # tools / tables), not the binary tool-provider split — a OneDrive
                # or Power BI run must not report "N table(s)".
                data_shape = data_shape_for(connection.type)
                noun_sing, noun_plural = catalog_nouns_for(connection.type)
                _entry = REGISTRY.get(connection.type)
                catalog_ownership = _entry.catalog_ownership if _entry else "shared"

                # ── Per-user catalog scope ──────────────────────────────────
                # A row carrying a user_id syncs THAT user's catalog with THAT
                # user's own credentials. This is the work the OAuth callback
                # used to do inline — a full drive walk on the redirect, with
                # the browser waiting on it — moved here so sign-in returns at
                # once and the progress is pollable.
                # Tool providers are the exception: their catalog is a flat tool
                # list, not a per-user schema overlay, so a user-scoped run just
                # means "discover with THIS user's token" — which is the only way
                # a per-user OAuth connector (DCR / OAuth app) can be indexed at
                # all. Fall through to the shared tool path with that identity.
                index_user = None
                if row.user_id and is_tool_provider:
                    from app.models.user import User

                    index_user = await db.get(User, str(row.user_id))

                if row.user_id and not is_tool_provider:
                    try:
                        _stage("connecting")
                        await _with_stage_timeout(
                            self._run_user_catalog_sync(
                                db=db,
                                new_session=_new_session,
                                indexing_id=indexing_id,
                                connection_id=str(row.connection_id),
                                user_id=str(row.user_id),
                                progress_cb=progress_cb,
                                flush=_flush,
                                append_event=_append_event,
                                state_snapshot=_state_snapshot,
                                started=start,
                                data_shape=data_shape,
                                nouns=(noun_sing, noun_plural),
                            ),
                            _DISCOVERY_TIMEOUT_SECONDS,
                            "Catalog sync",
                            idle_seconds=_IDLE_TIMEOUT_SECONDS,
                            last_activity=_last_activity,
                            last_phase=_last_phase,
                        )
                    except IndexingCancelled:
                        await self._finalize_cancelled(_new_session, indexing_id, _append_event, _state_snapshot)
                    except IndexingStageTimeout as exc:
                        cancel_event.set()
                        logger.warning(
                            "indexing.user_catalog.timeout",
                            extra={"indexing_id": indexing_id, "timeout_s": exc.seconds, "idle": exc.idle},
                        )
                        await _flush(force=True)
                        async with _new_session() as err_db:
                            fresh = await err_db.get(ConnectionIndexing, indexing_id)
                            if fresh is not None and not fresh.is_terminal():
                                fresh.status = ConnectionIndexingStatus.FAILED.value
                                fresh.error = str(exc)
                                fresh.finished_at = datetime.utcnow()
                                await err_db.commit()
                        await _append_event("error", _state_snapshot()["phase"], f"Catalog sync failed: {exc}")
                    return

                # Per-user-owned catalogs (OneDrive, personal Drive, mail) have
                # nothing to index admin-side — each user's catalog is fetched
                # when they sign in. Complete the run with an honest explanation
                # instead of a "Discovered 0 tables" that reads as broken.
                if not is_tool_provider and catalog_ownership == "per_user":
                    fresh = await db.get(ConnectionIndexing, indexing_id)
                    if fresh is None:
                        return
                    elapsed_s = round(time.perf_counter() - start, 3)
                    fresh.status = ConnectionIndexingStatus.COMPLETED.value
                    fresh.finished_at = datetime.utcnow()
                    fresh.error = None
                    fresh.stats_json = {
                        "table_count": 0,
                        "per_user_catalog": True,
                        "data_shape": data_shape,
                        "item_noun": noun_sing,
                        "item_noun_plural": noun_plural,
                        "elapsed_s": elapsed_s,
                    }
                    await db.commit()
                    await _append_event(
                        "info", None,
                        f"Per-user catalog — nothing to index admin-side; each "
                        f"user's {noun_plural} are indexed when they sign in",
                    )
                    return

                # Per-user OAuth connectors (an MCP/Custom-API tile connected via
                # DCR or an admin OAuth app) hold an OAuth client, never a token.
                # A run with no user in scope would go out unauthenticated and
                # come back 401 — on every scheduled sweep, with a red "Indexing
                # failed" the admin can do nothing about. Say what's actually
                # true instead: this catalog is discovered when a user signs in.
                if index_user is None and catalog_requires_user_sign_in(connection):
                    fresh = await db.get(ConnectionIndexing, indexing_id)
                    if fresh is None:
                        return
                    elapsed_s = round(time.perf_counter() - start, 3)
                    fresh.status = ConnectionIndexingStatus.COMPLETED.value
                    fresh.finished_at = datetime.utcnow()
                    fresh.error = None
                    fresh.stats_json = {
                        "table_count": 0,
                        "awaiting_user_sign_in": True,
                        "data_shape": data_shape,
                        "item_noun": noun_sing,
                        "item_noun_plural": noun_plural,
                        "elapsed_s": elapsed_s,
                    }
                    await db.commit()
                    await _append_event(
                        "info", None,
                        f"This connection signs in per user — {noun_plural} are "
                        f"discovered with each user's own credentials, so there is "
                        f"nothing to index with the connection's own.",
                    )
                    return

                try:
                    _stage("connecting")
                    if is_tool_provider:
                        discovery = svc.refresh_tools(
                            db=db,
                            connection=connection,
                            current_user=index_user,
                        )
                    else:
                        discovery = svc.refresh_schema(
                            db=db,
                            connection=connection,
                            current_user=None,
                            progress_callback=progress_cb,
                        )
                    items = await _with_stage_timeout(
                        discovery, _DISCOVERY_TIMEOUT_SECONDS, "Schema discovery",
                        idle_seconds=_IDLE_TIMEOUT_SECONDS,
                        last_activity=_last_activity,
                        last_phase=_last_phase,
                    )
                except IndexingCancelled:
                    await _flush(force=True)
                    await self._finalize_cancelled(_new_session, indexing_id, _append_event, _state_snapshot)
                    return
                except Exception as exc:  # pragma: no cover — surface via row
                    if isinstance(exc, IndexingStageTimeout):
                        # The worker thread cannot be killed; this makes its
                        # next progress checkpoint raise so it stops working.
                        cancel_event.set()
                        logger.warning(
                            "indexing.run.timeout",
                            extra={"indexing_id": indexing_id, "timeout_s": exc.seconds, "idle": exc.idle},
                        )
                    else:
                        logger.exception("indexing.run.failed", extra={"indexing_id": indexing_id})
                    await _flush(force=True)
                    # Use a fresh session — the service may have rolled back.
                    async with _new_session() as err_db:
                        fresh = await err_db.get(ConnectionIndexing, indexing_id)
                        if fresh is not None:
                            fresh.status = ConnectionIndexingStatus.FAILED.value
                            fresh.error = str(exc)[:4000]
                            fresh.finished_at = datetime.utcnow()
                            await err_db.commit()
                        # Record the failure on the connection for the scheduled
                        # auto-reindex sweeper's diagnostics. next_retry_at was
                        # already stamped by the sweeper before kicking, so the
                        # connection won't be re-kicked until its interval elapses
                        # (user_required catalogs heal on user login meanwhile).
                        conn_row = await err_db.get(Connection, row.connection_id)
                        if conn_row is not None:
                            conn_row.last_reindex_error = str(exc)[:4000]
                            await err_db.commit()
                    await _append_event("error", _state_snapshot()["phase"], f"Indexing failed: {exc}")
                    return

                # Force one final flush so schema-phase progress ends at its total.
                await _flush(force=True)

                # ── Warm phase — the expensive part for file-based sources. ──
                # For QVD this is the 40-minute QVD→Parquet convert. We now run it
                # *inside* the tracked run (awaited, progress-reported, cancellable)
                # so the bar reflects real work and doesn't settle at 100% while a
                # long convert is still churning in the background. Warm failures
                # are non-fatal: the catalog is already indexed and the scheduled
                # warmup retries the convert.
                extra_stats: dict = {}
                if not is_tool_provider:
                    warm_timed_out = threading.Event()
                    try:
                        _stage("warming")
                        client = await svc.construct_client(db, connection)
                        await _with_stage_timeout(
                            client.awarm_all(
                                progress_callback=progress_cb,
                                cancel_check=lambda: cancel_event.is_set() or warm_timed_out.is_set(),
                            ),
                            _WARM_TIMEOUT_SECONDS,
                            "Cache warm",
                        )
                        extra_stats = client.index_stats() or {}
                        await _flush(force=True)
                    except IndexingCancelled:
                        await self._finalize_cancelled(
                            _new_session, indexing_id, _append_event, _state_snapshot
                        )
                        return
                    except HTTPException as exc:
                        # Expected for user_required connections: warming runs in a
                        # background context with no current_user, so credential
                        # resolution returns 403. The first user-initiated query warms
                        # the cache instead. Log cleanly without a scary traceback.
                        logger.debug("indexing.warm.skipped status=%s detail=%s", exc.status_code, exc.detail)
                    except IndexingStageTimeout as warm_exc:
                        # Non-fatal like any warm failure; stop the convert at
                        # its next cancel check without cancelling the run.
                        warm_timed_out.set()
                        logger.warning(
                            "indexing.warm.timeout",
                            extra={"indexing_id": indexing_id, "timeout_s": warm_exc.seconds},
                        )
                        await _append_event(
                            "warn", _state_snapshot()["phase"],
                            f"{warm_exc} (catalog still indexed)",
                        )
                    except Exception as warm_exc:
                        logger.warning("indexing.warm.failed", exc_info=True)
                        await _append_event(
                            "warn", _state_snapshot()["phase"],
                            f"Cache warm failed (catalog still indexed): {warm_exc}",
                        )

                # A cancel that arrived during sync/finalize still wins.
                if cancel_event.is_set():
                    await self._finalize_cancelled(
                        _new_session, indexing_id, _append_event, _state_snapshot
                    )
                    return

                synced_domains = 0
                if not is_tool_provider:
                    _stage("syncing_agents")
                    await _flush(force=True)
                    # Fan schema out to every DataSource linked to this connection so
                    # the domain-level view (DataSourceTable) reflects the new schema.
                    synced_domains = await self._sync_linked_data_sources(
                        db, connection_id=row.connection_id,
                        session_factory=session_factory,
                    )

                fresh = await db.get(ConnectionIndexing, indexing_id)
                if fresh is None:
                    return
                fresh.status = ConnectionIndexingStatus.COMPLETED.value
                fresh.finished_at = datetime.utcnow()
                fresh.error = None
                item_count = len(items) if items else 0
                elapsed_s = round(time.perf_counter() - start, 3)
                count_key = "tool_count" if is_tool_provider else "table_count"
                # Datasets/tables that were found but could not be introspected
                # (e.g. Power BI models with no Build permission / RLS). Captured
                # by refresh_schema on the service instance; report them on the
                # job so admins can see what was skipped and why, rather than the
                # models silently disappearing from the catalog.
                unreadable = list(getattr(svc, "last_discovery_diagnostics", []) or [])
                stats_json = {
                    count_key: item_count,
                    # Shape-aware copy for the UI ("Discovered N files", not
                    # "N tables"). Older runs lack these keys; consumers fall
                    # back to the tables/tools binary.
                    "data_shape": data_shape,
                    "item_noun": noun_sing,
                    "item_noun_plural": noun_plural,
                    "synced_domains": synced_domains,
                    "elapsed_s": elapsed_s,
                    **extra_stats,  # source_bytes / file_count / row_count for file sources
                }
                if unreadable:
                    stats_json["unreadable_datasets"] = unreadable
                    stats_json["unreadable_dataset_count"] = len(unreadable)
                fresh.stats_json = stats_json
                # Ensure progress_done == progress_total so the UI settles at 100%.
                if fresh.progress_total and fresh.progress_done < fresh.progress_total:
                    fresh.progress_done = fresh.progress_total
                await db.commit()

                item_label = noun_sing if item_count == 1 else noun_plural
                size_note = ""
                if extra_stats.get("source_bytes"):
                    size_note = f" ({_human_bytes(extra_stats['source_bytes'])})"
                await _append_event(
                    "info", _state_snapshot()["phase"],
                    f"Completed: {item_count} {item_label} in {elapsed_s}s{size_note}",
                    done=item_count, total=item_count,
                )
                if unreadable:
                    _names = ", ".join(
                        str(d.get("datasetName") or d.get("name") or d.get("datasetId"))
                        for d in unreadable[:5]
                    )
                    _more = "" if len(unreadable) <= 5 else f" (+{len(unreadable) - 5} more)"
                    await _append_event(
                        "warn", _state_snapshot()["phase"],
                        f"{len(unreadable)} semantic model(s) found but not readable "
                        f"(check permissions): {_names}{_more}",
                    )

        except IndexingCancelled:
            # A cancel that surfaced outside the inner handlers — treat as a
            # clean stop, never a failure.
            try:
                await self._finalize_cancelled(_new_session, indexing_id, _append_event, _state_snapshot)
            except Exception:
                pass
        except Exception as exc:  # pragma: no cover — last-ditch guard
            logger.exception("indexing.run.crash", extra={"indexing_id": indexing_id})
            try:
                async with _new_session() as err_db:
                    fresh = await err_db.get(ConnectionIndexing, indexing_id)
                    if fresh is not None and not fresh.is_terminal():
                        fresh.status = ConnectionIndexingStatus.FAILED.value
                        fresh.error = str(exc)[:4000]
                        fresh.finished_at = datetime.utcnow()
                        await err_db.commit()
            except Exception:
                pass
        finally:
            _clear_cancel_event(indexing_id)
            # Telemetry: schema/catalog sync finished, covers every completion
            # branch above via this single finally. Runs before engine.dispose()
            # so _new_session() is still backed by a live engine. Best-effort and
            # fully isolated from the run's own outcome — this is already a
            # background job, so nothing here adds request-path latency.
            try:
                from app.core.telemetry import telemetry
                async with _new_session() as tel_db:
                    fresh = await tel_db.get(ConnectionIndexing, indexing_id)
                    if fresh is not None and fresh.status == ConnectionIndexingStatus.COMPLETED.value:
                        conn = await tel_db.get(Connection, fresh.connection_id)
                        stats = fresh.stats_json or {}
                        await telemetry.capture(
                            "data_source_schema_synced",
                            {
                                "connection_id": fresh.connection_id,
                                "connection_type": conn.type if conn else None,
                                "table_count": stats.get("table_count"),
                                "item_noun": stats.get("item_noun"),
                            },
                            user_id=fresh.user_id,
                            org_id=conn.organization_id if conn else None,
                        )
            except Exception:
                logger.debug("indexing.telemetry_failed", exc_info=True)
            try:
                await engine.dispose()
            except Exception:
                logger.debug("indexing.engine_dispose_failed", exc_info=True)

    async def _run_user_catalog_sync(
        self,
        *,
        db: AsyncSession,
        new_session,
        indexing_id: str,
        connection_id: str,
        user_id: str,
        progress_cb,
        flush,
        append_event,
        state_snapshot,
        started: float,
        data_shape: str,
        nouns: tuple,
    ) -> None:
        """Build ONE user's catalog for this connection, in the background.

        Runs the per-user overlay sync (`get_user_data_source_schema`) for every
        data source linked to the connection, using that user's credentials. Each
        data source gets its own session so one failure can't poison the others,
        and progress flows through the same callback the shared run uses — so a
        signing-in user sees "listing folders 34/120" instead of a blank wait.

        Never raises: the outcome lands on the indexing row.
        """
        from sqlalchemy.orm import selectinload

        from app.models.data_source import DataSource
        from app.models.user import User
        from app.services.data_source_service import DataSourceService

        noun_sing, noun_plural = nouns

        conn_row = await db.execute(
            select(Connection)
            .options(selectinload(Connection.data_sources))
            .where(Connection.id == connection_id)
        )
        connection = conn_row.scalar_one_or_none()
        ds_ids = [
            str(ds.id)
            for ds in (getattr(connection, "data_sources", None) or [])
            if getattr(ds, "deleted_at", None) is None
        ]

        await append_event(
            "info", None,
            f"Building your {noun_plural} catalog"
            + (f" across {len(ds_ids)} data source(s)" if len(ds_ids) > 1 else ""),
        )

        ds_service = DataSourceService()
        item_count = 0
        synced = 0
        try:
            for ds_id in ds_ids:
                async with new_session() as per_db:
                    ds = (await per_db.execute(
                        select(DataSource).where(
                            DataSource.id == ds_id,
                            DataSource.deleted_at.is_(None),
                        )
                    )).scalar_one_or_none()
                    if ds is None:
                        continue
                    user = await per_db.get(User, user_id)
                    if user is None:
                        break
                    tables = await ds_service.get_user_data_source_schema(
                        db=per_db,
                        data_source=ds,
                        user=user,
                        progress_callback=progress_cb,
                    )
                    await per_db.commit()
                    item_count += len(tables or [])
                    synced += 1
        except IndexingCancelled:
            await self._finalize_cancelled(new_session, indexing_id, append_event, state_snapshot)
            return
        except Exception as exc:
            logger.exception(
                "indexing.user_catalog.failed",
                extra={"indexing_id": indexing_id, "user_id": user_id},
            )
            async with new_session() as err_db:
                fresh = await err_db.get(ConnectionIndexing, indexing_id)
                if fresh is not None:
                    fresh.status = ConnectionIndexingStatus.FAILED.value
                    fresh.error = str(exc)[:4000]
                    fresh.finished_at = datetime.utcnow()
                    await err_db.commit()
            await append_event(
                "error", state_snapshot()["phase"], f"Catalog sync failed: {exc}"
            )
            return

        await flush(force=True)
        elapsed_s = round(time.perf_counter() - started, 3)
        async with new_session() as fin_db:
            fresh = await fin_db.get(ConnectionIndexing, indexing_id)
            if fresh is None:
                return
            fresh.status = ConnectionIndexingStatus.COMPLETED.value
            fresh.finished_at = datetime.utcnow()
            fresh.error = None
            fresh.stats_json = {
                "table_count": item_count,
                "user_catalog": True,
                "synced_domains": synced,
                "data_shape": data_shape,
                "item_noun": noun_sing,
                "item_noun_plural": noun_plural,
                "elapsed_s": elapsed_s,
            }
            if fresh.progress_total and fresh.progress_done < fresh.progress_total:
                fresh.progress_done = fresh.progress_total
            await fin_db.commit()

        item_label = noun_sing if item_count == 1 else noun_plural
        await append_event(
            "info", state_snapshot()["phase"],
            f"Completed: {item_count} {item_label} in {elapsed_s}s",
            done=item_count, total=item_count,
        )
        logger.info(
            "indexing.user_catalog.completed",
            extra={
                "indexing_id": indexing_id,
                "user_id": user_id,
                "item_count": item_count,
                "elapsed_s": elapsed_s,
            },
        )

    async def _finalize_cancelled(
        self,
        new_session,
        indexing_id: str,
        append_event,
        state_snapshot,
    ) -> None:
        """Mark a run cancelled from inside the runner. Idempotent: if
        `request_cancel` already flipped the row (the common path), we only
        stamp finished_at/error when still missing and log the stop event once.
        """
        try:
            async with new_session() as db:
                fresh = await db.get(ConnectionIndexing, indexing_id)
                if fresh is None:
                    return
                already_cancelled = fresh.status == ConnectionIndexingStatus.CANCELLED.value
                fresh.status = ConnectionIndexingStatus.CANCELLED.value
                if fresh.finished_at is None:
                    fresh.finished_at = datetime.utcnow()
                if not fresh.error:
                    fresh.error = "Cancelled by user"
                await db.commit()
        except Exception:
            logger.debug("indexing.finalize_cancelled_failed", exc_info=True)
        try:
            await append_event("warn", state_snapshot()["phase"], "Indexing cancelled")
        except Exception:
            pass
        logger.info("indexing.cancelled", extra={"indexing_id": indexing_id})

    async def _sync_linked_data_sources(
        self,
        db: AsyncSession,
        *,
        connection_id: str,
        session_factory,
    ) -> int:
        """After a successful refresh_schema, mirror the new ConnectionTable set
        onto every DataSource that links this connection.

        Each per-DS sync runs in its own session/transaction so that:
          - a data source deleted mid-flight (concurrent test or user delete)
            doesn't FK-violate the whole runner — we re-check existence per DS
            and skip if gone, and catch any leftover IntegrityError on commit;
          - one corrupt DS doesn't abort the sync for its peers.

        Returns the number of data sources synced successfully.
        """
        from sqlalchemy.exc import IntegrityError
        from sqlalchemy.orm import selectinload

        from app.models.data_source import DataSource
        from app.services.data_source_service import DataSourceService

        # Snapshot the connection's linked DS IDs from the runner's session,
        # then close that scope — per-DS work happens in its own session.
        result = await db.execute(
            select(Connection)
            .options(selectinload(Connection.data_sources))
            .where(Connection.id == str(connection_id))
        )
        connection_snapshot = result.scalar_one_or_none()
        if connection_snapshot is None or not connection_snapshot.data_sources:
            return 0
        ds_ids = [str(ds.id) for ds in connection_snapshot.data_sources]

        ds_service = DataSourceService()
        synced = 0
        for ds_id in ds_ids:
            try:
                async with session_factory() as per_db:
                    # Re-fetch the DS in the per-DS session. If it was deleted
                    # (hard or soft) between snapshot and now, skip cleanly.
                    ds_row = await per_db.execute(
                        select(DataSource).where(
                            DataSource.id == ds_id,
                            DataSource.deleted_at.is_(None),
                        )
                    )
                    ds = ds_row.scalar_one_or_none()
                    if ds is None:
                        continue
                    # Same for the connection — guard against a delete here too.
                    conn_row = await per_db.execute(
                        select(Connection).where(
                            Connection.id == str(connection_id),
                            Connection.deleted_at.is_(None),
                        )
                    )
                    connection = conn_row.scalar_one_or_none()
                    if connection is None:
                        continue
                    try:
                        await ds_service.sync_domain_tables_from_connection(
                            per_db,
                            ds,
                            connection,
                            max_auto_select=ds_service.ONBOARDING_MAX_TABLES,
                        )
                        await per_db.commit()
                        synced += 1
                    except IntegrityError:
                        # Most likely cause: the DS was deleted between our
                        # existence check and the INSERT (FK violation on
                        # datasource_tables.datasource_id). Roll back this
                        # DS and move on.
                        await per_db.rollback()
                        logger.info(
                            "indexing.sync_domain_skipped_fk",
                            extra={
                                "connection_id": str(connection_id),
                                "data_source_id": ds_id,
                            },
                        )
            except Exception:
                logger.exception(
                    "indexing.sync_domain_failed",
                    extra={"connection_id": str(connection_id), "data_source_id": ds_id},
                )
        return synced
