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

from fastapi import HTTPException
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.data_sources.clients.progress import IndexingCancelled
from app.models.connection import Connection
from app.models.connection_indexing import (
    ConnectionIndexing,
    ConnectionIndexingStatus,
    TERMINAL_INDEXING_STATUSES,
)


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
    global _background_loop
    with _background_loop_lock:
        loop = _background_loop
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

    async def request_cancel(
        self,
        db: AsyncSession,
        connection_id: str,
    ) -> Optional[ConnectionIndexing]:
        """Request cancellation of the active indexing run for a connection.

        Signals the running task to stop cooperatively (kills an in-flight QVD
        convert subprocess mid-stream) and optimistically marks the row
        `cancelled` so polling reflects it immediately. The runner re-checks the
        cancel event before finalizing and will not flip a cancelled row back to
        completed. Returns the row, or None if nothing was active.
        """
        row = await self.get_active(db, connection_id)
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
            loop = _get_background_loop()
            asyncio.run_coroutine_threadsafe(self._run(row.id), loop)

        return row

    async def _run(self, indexing_id: str) -> None:
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
                            async with _new_session() as flush_db:
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
                    # Called from inside `asyncio.to_thread(...)` — a worker
                    # thread (schema discovery or warm/convert). Raising here
                    # aborts that loop promptly on cancel.
                    if cancel_event.is_set():
                        raise IndexingCancelled()
                    # Update state under a lock so the runner loop never reads a
                    # torn (phase, item, done, total).
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

                try:
                    if is_tool_provider:
                        items = await svc.refresh_tools(
                            db=db,
                            connection=connection,
                            current_user=None,
                        )
                    else:
                        items = await svc.refresh_schema(
                            db=db,
                            connection=connection,
                            current_user=None,
                            progress_callback=progress_cb,
                        )
                except IndexingCancelled:
                    await self._finalize_cancelled(_new_session, indexing_id, _append_event, _state_snapshot)
                    return
                except Exception as exc:  # pragma: no cover — surface via row
                    logger.exception("indexing.run.failed", extra={"indexing_id": indexing_id})
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
                    try:
                        client = await svc.construct_client(db, connection)
                        await client.awarm_all(
                            progress_callback=progress_cb,
                            cancel_check=cancel_event.is_set,
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
            try:
                await engine.dispose()
            except Exception:
                logger.debug("indexing.engine_dispose_failed", exc_info=True)

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
