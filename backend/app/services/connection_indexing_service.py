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
import threading
import time
from datetime import datetime
from typing import Optional

from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.models.connection_indexing import (
    ConnectionIndexing,
    ConnectionIndexingStatus,
    TERMINAL_INDEXING_STATUSES,
)


logger = logging.getLogger(__name__)


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


def _get_background_loop() -> asyncio.AbstractEventLoop:
    global _background_loop
    with _background_loop_lock:
        if _background_loop is None or _background_loop.is_closed():
            _background_loop = _start_background_loop()
        return _background_loop


# How often we flush progress updates to the DB. Progress callbacks from the
# client loop can fire thousands of times; we coalesce into one write per
# `_PROGRESS_FLUSH_SECONDS` (plus one final flush at end-of-phase).
_PROGRESS_FLUSH_SECONDS = 0.25

# Per-run event log cap. Keep enough to be useful, drop oldest beyond.
_EVENT_LOG_MAX = 200


class ConnectionIndexingService:
    """Create, poll, and (internally) run `ConnectionIndexing` rows."""

    async def get_latest(
        self,
        db: AsyncSession,
        connection_id: str,
    ) -> Optional[ConnectionIndexing]:
        """Return the most recent indexing row for a connection (any status)."""
        result = await db.execute(
            select(ConnectionIndexing)
            .where(ConnectionIndexing.connection_id == str(connection_id))
            .order_by(desc(ConnectionIndexing.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_active(
        self,
        db: AsyncSession,
        connection_id: str,
    ) -> Optional[ConnectionIndexing]:
        """Return the current pending/running indexing row for a connection, if any."""
        result = await db.execute(
            select(ConnectionIndexing)
            .where(
                ConnectionIndexing.connection_id == str(connection_id),
                ConnectionIndexing.status.in_([
                    ConnectionIndexingStatus.PENDING.value,
                    ConnectionIndexingStatus.RUNNING.value,
                ]),
            )
            .order_by(desc(ConnectionIndexing.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()

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
        kick_off: bool = True,
    ) -> ConnectionIndexing:
        """Create a pending indexing row and (unless already in-flight) kick off
        the background runner. Idempotent — returns the active row if one
        already exists.
        """
        existing = await self.get_active(db, str(connection.id))
        if existing is not None:
            return existing

        row = ConnectionIndexing(
            connection_id=str(connection.id),
            status=ConnectionIndexingStatus.PENDING.value,
            phase=None,
            progress_done=0,
            progress_total=0,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        if kick_off:
            # Fire-and-forget on the shared background loop. The runner opens
            # its own DB session; we don't await the future here.
            loop = _get_background_loop()
            asyncio.run_coroutine_threadsafe(self._run(row.id), loop)

        return row

    async def _run(self, indexing_id: str) -> None:
        """Runner that opens a fresh session and executes `refresh_schema`.

        Exceptions are captured onto the row — never re-raised. The task must
        not let its wrapping session outlive work, so we open/close a session
        for each significant phase (mark-running, progress flush, finalize).
        """
        # Avoid circular import at module load.
        from app.services.connection_service import ConnectionService

        # The loop this coroutine is currently executing on. Progress callbacks
        # fire from worker threads (via `asyncio.to_thread` inside aget_schemas)
        # and must post their flush coroutine BACK to this loop.
        runner_loop = asyncio.get_running_loop()
        start = time.perf_counter()

        async def _append_event(level: str, phase: str | None, message: str,
                                done: int = 0, total: int = 0) -> None:
            """Append a single entry to the indexing row's events_json.

            Best-effort: a failure to log must never affect the run. Events
            are capped at `_EVENT_LOG_MAX` (oldest dropped).
            """
            try:
                async with async_session_maker() as ev_db:
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
            async with async_session_maker() as db:
                row = await db.get(ConnectionIndexing, indexing_id)
                if row is None:
                    logger.warning("indexing.run.missing", extra={"indexing_id": indexing_id})
                    return
                row.status = ConnectionIndexingStatus.RUNNING.value
                row.started_at = datetime.utcnow()
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
                pending_state: dict = {"phase": None, "item": None, "done": 0, "total": 0}
                state_lock = threading.Lock()
                last_phase: dict = {"name": None}
                flush_lock = asyncio.Lock()

                def _state_snapshot() -> dict:
                    with state_lock:
                        return dict(pending_state)

                async def _maybe_log_phase_event(phase: str | None, total: int) -> None:
                    if phase != last_phase["name"]:
                        last_phase["name"] = phase
                        if phase:
                            label = (
                                f"Phase: {phase} ({total} items)"
                                if total > 0
                                else f"Phase: {phase}"
                            )
                            await _append_event("info", phase, label, done=0, total=total)

                async def _flush(force: bool = False) -> None:
                    nonlocal last_flush_at
                    now = time.perf_counter()
                    if not force and (now - last_flush_at) < _PROGRESS_FLUSH_SECONDS:
                        return
                    # Take a single consistent snapshot of progress state.
                    snap = _state_snapshot()
                    async with flush_lock:
                        try:
                            async with async_session_maker() as flush_db:
                                fresh = await flush_db.get(ConnectionIndexing, indexing_id)
                                if fresh is None:
                                    return
                                fresh.phase = snap["phase"]
                                fresh.current_item = snap["item"]
                                fresh.progress_done = snap["done"]
                                fresh.progress_total = snap["total"]
                                await flush_db.commit()
                        except Exception:
                            # Never let a failed flush kill the indexing run.
                            logger.debug("indexing.flush_failed", exc_info=True)
                        last_flush_at = now
                    # Phase-transition events live on the same flush schedule —
                    # one row write only, no extra DB traffic per item.
                    await _maybe_log_phase_event(snap["phase"], snap["total"])

                def progress_cb(phase, current_item, done, total):
                    # Called from inside `asyncio.to_thread(get_schemas)` — a
                    # worker thread. Update state under a lock so the runner
                    # loop never reads a torn (phase, item, done, total).
                    with state_lock:
                        pending_state["phase"] = phase
                        pending_state["item"] = current_item
                        pending_state["done"] = done
                        pending_state["total"] = total
                    if runner_loop.is_closed():
                        return
                    try:
                        asyncio.run_coroutine_threadsafe(_flush(), runner_loop)
                    except RuntimeError:
                        pass

                svc = ConnectionService()

                try:
                    tables = await svc.refresh_schema(
                        db=db,
                        connection=connection,
                        current_user=None,
                        progress_callback=progress_cb,
                    )
                except Exception as exc:  # pragma: no cover — surface via row
                    logger.exception("indexing.run.failed", extra={"indexing_id": indexing_id})
                    # Use a fresh session — the service may have rolled back.
                    async with async_session_maker() as err_db:
                        fresh = await err_db.get(ConnectionIndexing, indexing_id)
                        if fresh is not None:
                            fresh.status = ConnectionIndexingStatus.FAILED.value
                            fresh.error = str(exc)[:4000]
                            fresh.finished_at = datetime.utcnow()
                            await err_db.commit()
                    await _append_event("error", _state_snapshot()["phase"], f"Indexing failed: {exc}")
                    return

                # Force one final flush so progress ends at the true total.
                await _flush(force=True)

                # Fan schema out to every DataSource linked to this connection so
                # the domain-level view (DataSourceTable) reflects the new schema.
                synced_domains = await self._sync_linked_data_sources(
                    db, connection_id=row.connection_id
                )

                fresh = await db.get(ConnectionIndexing, indexing_id)
                if fresh is None:
                    return
                fresh.status = ConnectionIndexingStatus.COMPLETED.value
                fresh.finished_at = datetime.utcnow()
                fresh.error = None
                table_count = len(tables) if tables else 0
                elapsed_s = round(time.perf_counter() - start, 3)
                fresh.stats_json = {
                    "table_count": table_count,
                    "synced_domains": synced_domains,
                    "elapsed_s": elapsed_s,
                }
                # Ensure progress_done == progress_total so the UI settles at 100%.
                if fresh.progress_total and fresh.progress_done < fresh.progress_total:
                    fresh.progress_done = fresh.progress_total
                await db.commit()

                await _append_event(
                    "info", _state_snapshot()["phase"],
                    f"Completed: {table_count} table(s) in {elapsed_s}s",
                    done=table_count, total=table_count,
                )

        except Exception as exc:  # pragma: no cover — last-ditch guard
            logger.exception("indexing.run.crash", extra={"indexing_id": indexing_id})
            try:
                async with async_session_maker() as err_db:
                    fresh = await err_db.get(ConnectionIndexing, indexing_id)
                    if fresh is not None and not fresh.is_terminal():
                        fresh.status = ConnectionIndexingStatus.FAILED.value
                        fresh.error = str(exc)[:4000]
                        fresh.finished_at = datetime.utcnow()
                        await err_db.commit()
            except Exception:
                pass

    async def _sync_linked_data_sources(
        self,
        db: AsyncSession,
        *,
        connection_id: str,
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
                async with async_session_maker() as per_db:
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
