"""The scheduler keeps running after a job-store write fails.

APScheduler 3.x lets an exception from ``jobstore.update_job`` escape
``wakeup()``; the timer is then never re-armed and every scheduled job in the
process stops silently. Seen live as SQLite "database is locked" under write
contention (docs/feedback-loops/audit-log-streams.md). The job store boundary is
the only thing stubbed; the scheduler runs for real on the test's event loop.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.scheduler import ResilientAsyncIOScheduler


class _FlakyStore(MemoryJobStore):
    def __init__(self, failures: int):
        super().__init__()
        self.failures = failures

    def update_job(self, job):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("database is locked")
        return super().update_job(job)


async def _runs_after_a_store_failure(scheduler_cls) -> int:
    runs = []
    store = _FlakyStore(failures=1)
    sched = scheduler_cls(jobstores={"default": store}, jobstore_retry_interval=0.2)
    sched.add_job(lambda: runs.append(time.monotonic()), "interval", seconds=0.1, id="tick",
                  max_instances=1, coalesce=True, misfire_grace_time=5)
    sched.start()
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and len(runs) < 4:
            await asyncio.sleep(0.05)
    finally:
        sched.shutdown(wait=False)
    return len(runs)


@pytest.mark.asyncio
async def test_scheduler_keeps_firing_after_a_failed_job_store_update():
    assert await _runs_after_a_store_failure(ResilientAsyncIOScheduler) >= 4


@pytest.mark.asyncio
async def test_plain_apscheduler_stops_after_the_same_failure():
    # Documents the upstream behaviour the subclass exists for; if APScheduler
    # starts guarding update_job itself, this flips and the subclass can go.
    assert await _runs_after_a_store_failure(AsyncIOScheduler) <= 1
