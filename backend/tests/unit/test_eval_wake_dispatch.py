"""Unit tests for the durable eval run-finished wake dispatch.

The wake is delivered as a one-shot APScheduler job (durable jobstore) rather
than an in-process asyncio task, so a worker crash before it runs recovers on
restart. These tests cover the dispatch/claim wiring deterministically; the
create-then-clear ordering and the actual completion are exercised in the
sandbox loop.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.test_run_service import (
    TestRunService,
    _eval_wake_job_id,
    run_eval_wake,
)


def test_job_id_is_deterministic_per_run():
    assert _eval_wake_job_id("run-1") == "eval_wake:run-1"
    assert _eval_wake_job_id("run-1") == _eval_wake_job_id("run-1")
    assert _eval_wake_job_id("run-2") != _eval_wake_job_id("run-1")


def test_schedule_registers_a_durable_oneshot_job():
    with patch("app.core.scheduler.scheduler") as sched:
        TestRunService()._schedule_eval_wake("run-1")
    sched.add_job.assert_called_once()
    kwargs = sched.add_job.call_args.kwargs
    assert kwargs["func"] is run_eval_wake
    assert kwargs["trigger"] == "date"
    assert kwargs["id"] == "eval_wake:run-1"
    # Deterministic id + replace_existing collapses duplicate schedules to one.
    assert kwargs["replace_existing"] is True
    # Survives a worker restart within the hour.
    assert kwargs["misfire_grace_time"] == 3600
    assert kwargs["kwargs"] == {"job_id": "eval_wake:run-1", "run_id": "run-1"}


def test_schedule_never_raises_on_scheduler_error():
    # A scheduler hiccup must not block run finalization.
    with patch("app.core.scheduler.scheduler") as sched:
        sched.add_job.side_effect = RuntimeError("scheduler down")
        TestRunService()._schedule_eval_wake("run-1")  # should swallow


@pytest.mark.asyncio
async def test_run_eval_wake_claims_then_delivers():
    with patch("app.core.scheduler.claim_scheduled_run", return_value=True) as claim, \
         patch.object(TestRunService, "_fire_eval_wake", new=AsyncMock()) as fire:
        await run_eval_wake("eval_wake:run-1", "run-1")
    claim.assert_called_once_with("eval_wake:run-1")
    fire.assert_awaited_once_with("run-1")


@pytest.mark.asyncio
async def test_run_eval_wake_skips_when_claim_lost():
    # Another worker already claimed this fire — we must not deliver twice.
    with patch("app.core.scheduler.claim_scheduled_run", return_value=False), \
         patch.object(TestRunService, "_fire_eval_wake", new=AsyncMock()) as fire:
        await run_eval_wake("eval_wake:run-1", "run-1")
    fire.assert_not_awaited()
