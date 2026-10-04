"""The startup sweep: index every run the diagnosis explorer cannot see yet.

Runs once per process start, in the background, after the app is already
serving. Newest runs first, 500 per commit, a short pause between batches, and
it stops the moment nothing is pending — so on every deploy after the first it
costs one indexed count and exits. Safe to run on several replicas at once:
refreshing a run twice writes the same row.

The same pass also runs on the scheduler (``scheduled_sweep``) because runs
keep becoming pending after startup: a run whose process died before the
early judge hook indexed it only counts as pending once it turns stale an
hour later — after the startup sweep of the very restart that orphaned it.

Disable with ``BOW_DIAGNOSIS_SWEEP=0``. Never runs under ``TESTING``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

from app.services.diagnosis.rollup import backfill, count_pending

logger = logging.getLogger(__name__)

BATCH_SIZE = 500
PAUSE_SECONDS = 0.05
# Let the process finish warming up before the first batch.
INITIAL_DELAY_SECONDS = 3.0
SCHEDULED_JOB_ID = "diagnosis_rollup_sweep"
SCHEDULE_MINUTES = 5

_task: Optional[asyncio.Task] = None


def enabled() -> bool:
    if os.getenv("BOW_DIAGNOSIS_SWEEP", "1").strip().lower() in ("0", "false", "no", "off"):
        return False
    try:
        from app.settings.config import settings
        if getattr(settings, "TESTING", False):
            return False
    except Exception:  # pragma: no cover
        pass
    return True


async def run_sweep(session_factory, *, batch_size: int = BATCH_SIZE, pause_seconds: float = PAUSE_SECONDS,
                    log_idle: bool = True) -> int:
    """One full pass. Returns the number of runs indexed."""
    started = time.monotonic()
    async with session_factory() as db:
        pending = await count_pending(db)
        if not pending:
            if log_idle:
                logger.info("diagnosis sweep: nothing to index")
            return 0
        logger.info("diagnosis sweep: %d runs to index, newest first", pending)
        last = {"t": started, "n": 0}

        def progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - last["t"] >= 10 or done >= total:
                logger.info("diagnosis sweep: %d/%d runs (%.0f/s)", done, total, done / max(now - started, 1e-6))
                last["t"] = now

        done = await backfill(db, batch_size=batch_size, pause_seconds=pause_seconds, progress=progress)
    logger.info("diagnosis sweep: indexed %d runs in %.1fs", done, time.monotonic() - started)
    return done


async def _guarded(session_factory) -> None:
    try:
        await asyncio.sleep(INITIAL_DELAY_SECONDS)
        await run_sweep(session_factory)
    except asyncio.CancelledError:  # shutdown mid-sweep: the next boot resumes
        raise
    except Exception:  # noqa: BLE001 — never take the worker down
        logger.exception("diagnosis sweep failed; it will retry on the next start")


async def scheduled_sweep(session_factory=None) -> int:
    """Scheduler entrypoint: one pass, claimed so a single worker runs each fire.
    When nothing is pending it costs one indexed count. Never raises."""
    from app.core.scheduler import claim_scheduled_run

    if not await asyncio.to_thread(claim_scheduled_run, SCHEDULED_JOB_ID):
        return 0
    if session_factory is None:
        from app.dependencies import async_session_maker as session_factory
    try:
        return await run_sweep(session_factory, log_idle=False)
    except Exception:  # noqa: BLE001 — the next fire retries
        logger.exception("scheduled diagnosis sweep failed; the next run retries")
        return 0


def start_background_sweep(session_factory) -> Optional[asyncio.Task]:
    """Schedule the sweep on the running loop. Idempotent per process."""
    global _task
    if not enabled():
        return None
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_guarded(session_factory), name="diagnosis-rollup-sweep")
    return _task
