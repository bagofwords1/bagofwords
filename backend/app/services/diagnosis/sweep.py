"""The startup sweep: index every run the diagnosis explorer cannot see yet.

Runs once per process start, in the background, after the app is already
serving. Newest runs first, 500 per commit, a short pause between batches, and
it stops the moment nothing is pending — so on every deploy after the first it
costs one indexed count and exits. Safe to run on several replicas at once:
refreshing a run twice writes the same row.

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


async def run_sweep(session_factory, *, batch_size: int = BATCH_SIZE, pause_seconds: float = PAUSE_SECONDS) -> int:
    """One full pass. Returns the number of runs indexed."""
    started = time.monotonic()
    async with session_factory() as db:
        pending = await count_pending(db)
        if not pending:
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


def start_background_sweep(session_factory) -> Optional[asyncio.Task]:
    """Schedule the sweep on the running loop. Idempotent per process."""
    global _task
    if not enabled():
        return None
    if _task is not None and not _task.done():
        return _task
    _task = asyncio.create_task(_guarded(session_factory), name="diagnosis-rollup-sweep")
    return _task
