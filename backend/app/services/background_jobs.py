"""Bounded, leased delivery of accepted work, independent of APScheduler.

Pending/pre-agent deliveries may be retried. Once a completion is committed,
its side effects are not replayed: the completion reaper reports interruption.
Every worker runs a small consumer; CAS leases coordinate across processes.
"""

from __future__ import annotations
import asyncio
import contextlib
from contextvars import ContextVar
from datetime import datetime, timedelta
import logging
import uuid
from sqlalchemy import select, update, and_
from app.models.background_job import BackgroundJob

logger = logging.getLogger(__name__)
LEASE_SECONDS = 300
HEARTBEAT_SECONDS = 30
MAX_ATTEMPTS = 3
_current: ContextVar[tuple[str, str] | None] = ContextVar("background_delivery", default=None)


def utcnow():
    return datetime.utcnow()


def session_factory():
    from app.settings.database import create_async_session_factory

    return create_async_session_factory()


def _insert(bind, *, key, kind, payload, available_at=None):
    # Native conflict handling keeps the intent in the caller transaction even
    # on SQLite's legacy transaction mode (a SAVEPOINT can commit by itself).
    if bind.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    return (
        insert(BackgroundJob)
        .values(
            id=str(uuid.uuid4()),
            key=key,
            kind=kind,
            payload=payload,
            status="pending",
            available_at=available_at or utcnow(),
            attempts=0,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["key"])
    )


async def enqueue(db, *, key, kind, payload, available_at=None):
    """Participates in the caller transaction; HTTP acknowledgement follows commit."""
    await db.execute(_insert(db.bind, key=key, kind=kind, payload=payload, available_at=available_at))
    return (await db.execute(select(BackgroundJob).where(BackgroundJob.key == key))).scalar_one()


def enqueue_sync(*, key, kind, payload, available_at=None):
    """For synchronous scheduling APIs; uses the existing scheduler engine."""
    from app.core.scheduler import _engine

    with _engine.begin() as conn:
        conn.execute(_insert(_engine, key=key, kind=kind, payload=payload, available_at=available_at))


def cancel_pending(key):
    from sqlalchemy.orm import Session
    from app.core.scheduler import _engine

    with Session(_engine) as db:
        result = db.execute(
            update(BackgroundJob)
            .where(BackgroundJob.key == key, BackgroundJob.status == "pending")
            .values(status="cancelled")
        )
        db.commit()
        return bool(result.rowcount)


@contextlib.contextmanager
def delivery_context(job_id, owner):
    token = _current.set((job_id, owner))
    try:
        yield
    finally:
        _current.reset(token)


async def delivery_event(db):
    from app.models.completion import Completion

    current = _current.get()
    if not current:
        return None
    row = await db.get(BackgroundJob, current[0])
    return await db.get(Completion, row.event_id) if row and row.event_id else None


async def checkpoint_event(db, event):
    current = _current.get()
    if not current:
        return
    await db.flush()
    result = await db.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.id == current[0],
            BackgroundJob.owner == current[1],
            BackgroundJob.status == "running",
            BackgroundJob.lease_until > utcnow(),
        )
        .values(event_id=str(event.id))
    )
    if result.rowcount != 1:
        await db.rollback()
        raise asyncio.CancelledError("Background delivery lease lost")


async def checkpoint_completion(db, completion):
    """Fence a recovered owner and atomically record the no-replay boundary."""
    current = _current.get()
    if current is None:
        return
    await db.flush()
    job_id, owner = current
    result = await db.execute(
        update(BackgroundJob)
        .where(
            BackgroundJob.id == job_id,
            BackgroundJob.owner == owner,
            BackgroundJob.status == "running",
            BackgroundJob.lease_until > utcnow(),
            BackgroundJob.completion_id.is_(None),
        )
        .values(completion_id=str(completion.id))
    )
    if result.rowcount != 1:
        await db.rollback()
        raise asyncio.CancelledError("Background delivery no longer owns its lease")
    # Descendant turns (evals/sub-agents) belong to the running agent, not to
    # another delivery. Do not inherit this receipt's single-start fence.
    _current.set(None)


async def deliver(kind, payload):
    """Closed handler registry: persisted data never chooses an import/callable."""
    if kind == "wait":
        from app.services.wait_service import deliver_wait_wake

        await deliver_wait_wake(**payload)
    elif kind == "eval_wake":
        from app.services.test_run_service import TestRunService

        await TestRunService()._fire_eval_wake(payload["run_id"])
    elif kind == "wave_deadline":
        from app.services.sub_agent_service import deliver_wave_deadline

        await deliver_wave_deadline(**payload)
    elif kind == "fan_in":
        from app.services.sub_agent_service import run_fan_in

        await run_fan_in(payload["wave_id"])
    elif kind == "webhook":
        from app.services.webhook_service import webhook_service

        await webhook_service.process_delivery(**payload)
    elif kind == "scheduled_prompt":
        from app.services.scheduled_prompt_service import scheduled_prompt_service

        await scheduled_prompt_service.scheduled_run_prompt(**payload, recovered_delivery=True)
    else:
        raise ValueError(f"Unknown background job kind: {kind}")


class BackgroundWorker:
    def __init__(self, factory=None):
        self.factory = factory or session_factory()

    async def recover(self):
        """Requeue only deliveries that have not started an agent turn."""
        async with self.factory() as db:
            expired = and_(BackgroundJob.status == "running", BackgroundJob.lease_until < utcnow())
            lost = (await db.execute(select(BackgroundJob).where(expired).limit(200))).scalars().all()
            for job in lost:
                retry = job.completion_id is None and job.attempts < MAX_ATTEMPTS
                claimed = await db.execute(
                    update(BackgroundJob)
                    .where(BackgroundJob.id == job.id, expired)
                    .values(
                        status="pending" if retry else "failed",
                        owner=None,
                        lease_until=None,
                        error=None if retry else "worker_lost",
                    )
                )
                if not claimed.rowcount or retry:
                    continue
                await self._close_failed_delivery(db, job)
            await db.commit()

    async def _close_failed_delivery(self, db, job):
        from app.models.completion import Completion

        if job.event_id:
            await db.execute(
                update(Completion)
                .where(
                    Completion.id == job.event_id,
                    Completion.status == "in_progress",
                )
                .values(status="error", completion={"error": True, "error_type": "stale_execution"})
            )
        if job.kind == "eval_wake":
            from app.models.eval import TestRun

            await db.execute(
                update(TestRun).where(TestRun.id == job.payload.get("run_id")).values(wake_on_finish=False)
            )
        if job.kind == "fan_in":
            from app.models.sub_agent import SubAgentWave

            await db.execute(
                update(SubAgentWave)
                .where(
                    SubAgentWave.id == job.payload.get("wave_id"),
                    SubAgentWave.status.in_(("firing", "timed_out")),
                )
                .values(status="done")
            )
        if job.kind == "scheduled_prompt" and job.payload.get("expected_once_at"):
            from app.models.scheduled_prompt import ScheduledPrompt

            await db.execute(
                update(ScheduledPrompt)
                .where(
                    ScheduledPrompt.id == job.payload["scheduled_prompt_id"],
                    ScheduledPrompt.run_once_at == datetime.fromisoformat(job.payload["expected_once_at"]),
                )
                .values(is_active=False)
            )

    async def run_one(self):
        await self.recover()
        owner = str(uuid.uuid4())
        async with self.factory() as db:
            job_id = (
                await db.execute(
                    select(BackgroundJob.id)
                    .where(
                        BackgroundJob.status == "pending",
                        BackgroundJob.available_at <= utcnow(),
                    )
                    .order_by(BackgroundJob.available_at, BackgroundJob.id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if job_id is None:
                return False
            claimed = await db.execute(
                update(BackgroundJob)
                .where(
                    BackgroundJob.id == job_id,
                    BackgroundJob.status == "pending",
                )
                .values(
                    status="running",
                    owner=owner,
                    attempts=BackgroundJob.attempts + 1,
                    lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS),
                )
            )
            await db.commit()
            if claimed.rowcount != 1:
                return True
            job = await db.get(BackgroundJob, job_id)
            kind, payload = job.kind, dict(job.payload)
        token = _current.set((job_id, owner))
        delivery_task = asyncio.create_task(deliver(kind, payload))
        heartbeat = asyncio.create_task(self._heartbeat(job_id, owner, delivery_task))
        try:
            async with asyncio.timeout(7200):
                await delivery_task
            async with self.factory() as db:
                await db.execute(
                    update(BackgroundJob)
                    .where(
                        BackgroundJob.id == job_id,
                        BackgroundJob.owner == owner,
                        BackgroundJob.status == "running",
                    )
                    .values(status="succeeded", lease_until=None, owner=None, payload={})
                )
                await db.commit()
        except asyncio.CancelledError:
            # Shutdown cancels the consumer; a lost lease cancels only this
            # delivery. Keep the consumer alive to serve unrelated work.
            if asyncio.current_task().cancelling():
                raise
        except Exception:
            logger.exception("Background delivery failed: %s", job_id)
            async with self.factory() as db:
                retired = await db.execute(
                    update(BackgroundJob)
                    .where(
                        BackgroundJob.id == job_id,
                        BackgroundJob.owner == owner,
                        BackgroundJob.status == "running",
                    )
                    .values(status="failed", lease_until=None, owner=None, error="delivery_failed")
                )
                if retired.rowcount:
                    job = await db.get(BackgroundJob, job_id)
                    await self._close_failed_delivery(db, job)
                await db.commit()
        finally:
            _current.reset(token)
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
        return True

    async def _heartbeat(self, job_id, owner, task):
        deadline = asyncio.get_running_loop().time() + LEASE_SECONDS
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            try:
                async with self.factory() as db:
                    result = await db.execute(
                        update(BackgroundJob)
                        .where(
                            BackgroundJob.id == job_id,
                            BackgroundJob.owner == owner,
                            BackgroundJob.status == "running",
                            BackgroundJob.lease_until > utcnow(),
                        )
                        .values(lease_until=utcnow() + timedelta(seconds=LEASE_SECONDS))
                    )
                    await db.commit()
                if not result.rowcount:
                    task.cancel()
                    return
                deadline = asyncio.get_running_loop().time() + LEASE_SECONDS
            except Exception:
                logger.warning("Background delivery heartbeat failed", exc_info=True)
                if asyncio.get_running_loop().time() >= deadline:
                    task.cancel()
                    return

    async def run(self):
        while True:
            try:
                worked = await self.run_one()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background delivery consumer failed")
                worked = False
            if not worked:
                await asyncio.sleep(2)
