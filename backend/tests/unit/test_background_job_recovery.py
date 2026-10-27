"""Delivery receipts survive worker loss without replaying agent side effects."""

from datetime import datetime, timedelta
import asyncio
import uuid
import pytest
from sqlalchemy import select
from app.dependencies import async_session_maker
from app.models.background_job import BackgroundJob
from app.services.background_jobs import BackgroundWorker, enqueue

pytestmark = [pytest.mark.db, pytest.mark.asyncio]


@pytest.mark.parametrize(
    "started_agent,attempts,expected", [(False, 1, "pending"), (False, 3, "failed"), (True, 1, "failed")]
)
async def test_expired_delivery_retries_only_before_agent_execution(started_agent, attempts, expected):
    async with async_session_maker() as db:
        job = await enqueue(db, key=str(uuid.uuid4()), kind="wait", payload={})
        # Worker death cannot be produced through an API; seed its persisted lease.
        job.status = "running"
        job.owner = str(uuid.uuid4())
        job.lease_until = datetime.utcnow() - timedelta(minutes=5)
        job.attempts = attempts
        job.completion_id = str(uuid.uuid4()) if started_agent else None
        await db.commit()
        job_id = job.id
    await BackgroundWorker(async_session_maker).recover()
    async with async_session_maker() as db:
        job = await db.get(BackgroundJob, job_id)
        assert job.status == expected
        assert job.owner is None


async def test_unexpired_delivery_is_not_reassigned():
    async with async_session_maker() as db:
        job = await enqueue(db, key=str(uuid.uuid4()), kind="wait", payload={})
        job.status = "running"
        job.owner = str(uuid.uuid4())
        job.lease_until = datetime.utcnow() + timedelta(minutes=5)
        await db.commit()
        job_id, owner = job.id, job.owner
    await BackgroundWorker(async_session_maker).recover()
    async with async_session_maker() as db:
        job = await db.get(BackgroundJob, job_id)
        assert job.status == "running" and job.owner == owner


async def test_duplicate_enqueue_preserves_original_intent():
    key = str(uuid.uuid4())
    async with async_session_maker() as db:
        first = await enqueue(db, key=key, kind="wait", payload={"reason": "first"})
        await db.commit()
        original_id = first.id
    async with async_session_maker() as db:
        second = await enqueue(db, key=key, kind="wait", payload={"reason": "duplicate"})
        await db.commit()
        assert second.id == original_id
        assert second.payload == {"reason": "first"}


async def test_multiple_consumers_do_not_execute_one_receipt_twice():
    async with async_session_maker() as db:
        job = await enqueue(db, key=str(uuid.uuid4()), kind="invalid-handler", payload={})
        await db.commit()
        job_id = job.id
    # Unsupported handlers fail explicitly; both consumers use the real DB/CAS.
    await asyncio.gather(*(BackgroundWorker(async_session_maker).run_one() for _ in range(2)))
    async with async_session_maker() as db:
        job = await db.get(BackgroundJob, job_id)
        assert job.status == "failed"
        assert job.attempts == 1


async def test_future_work_does_not_run_early():
    async with async_session_maker() as db:
        job = await enqueue(
            db, key=str(uuid.uuid4()), kind="wait", payload={}, available_at=datetime.utcnow() + timedelta(days=1)
        )
        await db.commit()
        job_id = job.id
    assert not await BackgroundWorker(async_session_maker).run_one()
    async with async_session_maker() as db:
        assert (await db.get(BackgroundJob, job_id)).status == "pending"


async def test_rolled_back_intent_is_not_delivered():
    key = str(uuid.uuid4())
    async with async_session_maker() as db:
        await enqueue(db, key=key, kind="wait", payload={})
        await db.rollback()
    async with async_session_maker() as db:
        assert (await db.execute(select(BackgroundJob).where(BackgroundJob.key == key))).scalar_one_or_none() is None


async def test_wait_remains_cancellable_after_scheduler_dispatch():
    from app.services.wait_service import WaitService

    key = f"wait:{uuid.uuid4()}:{uuid.uuid4()}"
    async with async_session_maker() as db:
        await enqueue(
            db,
            key=key,
            kind="wait",
            payload={"report_id": "r", "user_id": "u", "organization_id": "o"},
            available_at=datetime.utcnow() + timedelta(hours=1),
        )
        await db.commit()
    # There is no APScheduler job: its dispatch already removed the timer.
    assert WaitService().cancel_wait(key)
    async with async_session_maker() as db:
        job = (await db.execute(select(BackgroundJob).where(BackgroundJob.key == key))).scalar_one()
        assert job.status == "cancelled"
