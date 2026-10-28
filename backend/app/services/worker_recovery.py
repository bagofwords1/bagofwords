"""Independent abandonment detection; never replay a partially executed agent."""

from __future__ import annotations
import asyncio
import contextlib
from datetime import datetime, timedelta
from functools import wraps
import inspect
import logging
from sqlalchemy import select, update

logger = logging.getLogger(__name__)
STALE_SECONDS = 900
HEARTBEAT_SECONDS = 30


def factory():
    from app.settings.database import create_async_session_factory

    return create_async_session_factory()


@contextlib.asynccontextmanager
async def monitor(model, row_id, running_status):
    """Keep a row alive; cancel cooperative work if another worker retires it."""
    parent = asyncio.current_task()

    async def beat():
        deadline = asyncio.get_running_loop().time() + STALE_SECONDS
        while True:
            try:
                async with factory()() as db:
                    result = await db.execute(
                        update(model)
                        .where(
                            model.id == str(row_id),
                            model.status.in_(
                                running_status if isinstance(running_status, tuple) else (running_status,)
                            ),
                        )
                        .values(updated_at=datetime.utcnow())
                    )
                    await db.commit()
                if not result.rowcount:
                    # Normal finalization can win while the heartbeat awaits
                    # its connection. Only abandonment cancels the owner.
                    async with factory()() as db:
                        row = await db.get(model, str(row_id))
                        markers = [
                            getattr(row, field, None)
                            for field in ("detail_json", "summary_json", "resolution_json", "error_message")
                        ]
                        if any(
                            value == "worker_lost" or isinstance(value, dict) and value.get("reason") == "worker_lost"
                            for value in markers
                        ):
                            parent.cancel()
                    return
                deadline = asyncio.get_running_loop().time() + STALE_SECONDS
            except Exception:
                logger.warning("Job heartbeat failed: %s/%s", model.__name__, row_id, exc_info=True)
                if asyncio.get_running_loop().time() >= deadline:
                    parent.cancel()
                    return
            await asyncio.sleep(HEARTBEAT_SECONDS)

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def monitored(model, id_arg, status):
    def decorate(fn):
        signature = inspect.signature(fn)

        @wraps(fn)
        async def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            async with monitor(model, bound.arguments[id_arg], status):
                return await fn(*args, **kwargs)

        return wrapped

    return decorate


async def require_running(db, model, row_id, status):
    """Fence finalization/promotion after another process recovered this run."""
    result = await db.execute(
        update(model)
        .where(
            model.id == str(row_id),
            model.status == status,
        )
        .values(updated_at=datetime.utcnow())
    )
    if result.rowcount != 1:
        raise asyncio.CancelledError("Execution was retired by worker recovery")


async def recover_automation(db, data_source_id=None, *, now=None):
    from app.models.agent_automation_run import AgentAutomationRun

    now = now or datetime.utcnow()
    stmt = update(AgentAutomationRun).where(
        AgentAutomationRun.status == "running",
        AgentAutomationRun.updated_at < now - timedelta(seconds=STALE_SECONDS),
    )
    if data_source_id is not None:
        stmt = stmt.where(AgentAutomationRun.data_source_id == str(data_source_id))
    await db.execute(stmt.values(status="error", finished_at=now, detail_json={"reason": "worker_lost"}))


async def recover_evals(db, run_ids=None, *, now=None):
    from app.models.eval import TestRun, TestResult

    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=STALE_SECONDS)
    stmt = select(TestRun.id).where(TestRun.status == "in_progress", TestRun.updated_at < cutoff)
    if run_ids is not None:
        stmt = stmt.where(TestRun.id.in_(run_ids))
    stale_runs = (await db.execute(stmt.limit(200))).scalars().all()
    for run_id in stale_runs:
        retired = await db.execute(
            update(TestRun)
            .where(
                TestRun.id == run_id,
                TestRun.status == "in_progress",
                TestRun.updated_at < cutoff,
            )
            .values(status="error", finished_at=now)
        )
        if retired.rowcount:
            await db.execute(
                update(TestResult)
                .where(
                    TestResult.run_id == run_id,
                    TestResult.status.notin_(("pass", "fail", "error", "stopped")),
                )
                .values(status="error", failure_reason="worker_lost")
            )
            statuses = (await db.execute(select(TestResult.status).where(TestResult.run_id == run_id))).scalars().all()
            await db.execute(
                update(TestRun)
                .where(TestRun.id == run_id)
                .values(
                    summary_json={
                        "reason": "worker_lost",
                        "total": len(statuses),
                        "passed": statuses.count("pass"),
                        "failed": sum(x in ("fail", "error") for x in statuses),
                    }
                )
            )


async def recover_rows(db, *, now=None):
    """Public, transaction-scoped sweep; CAS predicates protect refreshed rows."""
    from app.models.metadata_indexing_job import MetadataIndexingJob
    from app.models.completion import Completion
    from app.models.review_item import ReviewItem

    now = now or datetime.utcnow()
    cutoff = now - timedelta(seconds=STALE_SECONDS)
    await recover_automation(db, now=now)
    await db.execute(
        update(MetadataIndexingJob)
        .where(
            MetadataIndexingJob.status.in_(("pending", "running")),
            MetadataIndexingJob.updated_at < cutoff,
        )
        .values(status="failed", completed_at=now, error_message="worker_lost")
    )
    await recover_evals(db, now=now)
    # Review wrappers have their own heartbeat, separate from the nested run.
    reviews = (
        (
            await db.execute(
                select(ReviewItem).where(ReviewItem.status == "in_progress", ReviewItem.updated_at < cutoff).limit(200)
            )
        )
        .scalars()
        .all()
    )
    for item in reviews:
        spawned = [
            {**entry, "status": "error", "finished_at": now.isoformat()} if entry.get("status") == "running" else entry
            for entry in (item.spawned_json or [])
        ]
        await db.execute(
            update(ReviewItem)
            .where(
                ReviewItem.id == item.id,
                ReviewItem.status == "in_progress",
                ReviewItem.updated_at < cutoff,
            )
            .values(status="open", spawned_json=spawned, resolution_json={"outcome": "error", "reason": "worker_lost"})
        )
    from app.services.completion_service import _COMPLETION_STALE_AFTER_SECONDS

    completion_cutoff = now - timedelta(seconds=_COMPLETION_STALE_AFTER_SECONDS)
    orphan_reports = (
        (
            await db.execute(
                select(Completion.report_id)
                .where(
                    Completion.role == "system",
                    Completion.status == "in_progress",
                    Completion.updated_at < completion_cutoff,
                    (Completion.trigger_source.is_(None)) | (Completion.trigger_source != "sub_agent"),
                )
                .distinct()
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    # Recovery/queue start must share the report lock with ordinary submits.
    from app.services.completion_service import CompletionService, lock_report

    for report_id in orphan_reports:
        await lock_report(db, report_id)
        await CompletionService()._get_running_system_completion(db, report_id)
    return orphan_reports


async def sweep():
    """Runs on every worker. Row claims, not a process-local leader, arbitrate."""
    from app.models.eval import TestRun
    from app.models.completion import Completion
    from app.models.sub_agent import SubAgentWave
    from app.services.background_jobs import enqueue
    from app.services.completion_service import CompletionService

    async with factory()() as db:
        recovered_reports = await recover_rows(db)
        # Durable intent for result collection; covers a crash between the
        # domain commit and scheduling, including rows created before upgrade.
        overdue = (
            (
                await db.execute(
                    select(SubAgentWave)
                    .where(
                        SubAgentWave.status.in_(("running", "timed_out")),
                        SubAgentWave.deadline_at < datetime.utcnow(),
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        for wave in overdue:
            key = wave.deadline_job_id or f"sub_agent_deadline:{wave.id}"
            await enqueue(db, key=key, kind="wave_deadline", payload={"job_id": key, "wave_id": str(wave.id)})
        waves = (
            (
                await db.execute(
                    select(SubAgentWave.id)
                    .where(
                        SubAgentWave.status == "firing",
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        for wave in waves:
            await enqueue(db, key=f"fan_in:{wave}", kind="fan_in", payload={"wave_id": str(wave)})
        runs = (
            (
                await db.execute(
                    select(TestRun.id)
                    .where(
                        TestRun.wake_on_finish.is_(True),
                        TestRun.status.in_(("success", "error", "completed", "failed")),
                    )
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        for run in runs:
            await enqueue(db, key=f"eval_wake:{run}", kind="eval_wake", payload={"run_id": str(run)})
        # Only drain idle queues after success or worker loss. Explicit user
        # stops and ordinary runtime errors retain the existing paused policy.
        queued_reports = (
            (
                await db.execute(
                    select(Completion.report_id)
                    .where(
                        Completion.role == "user",
                        Completion.status == "queued",
                    )
                    .distinct()
                    .limit(200)
                )
            )
            .scalars()
            .all()
        )
        await db.commit()
    for report_id in set(queued_reports) | set(recovered_reports):
        async with factory()() as db:
            last = (
                await db.execute(
                    select(Completion)
                    .where(
                        Completion.report_id == report_id,
                        Completion.role == "system",
                        (Completion.trigger_source.is_(None)) | (Completion.trigger_source != "sub_agent"),
                    )
                    .order_by(Completion.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if (
                last
                and last.status != "success"
                and not (
                    last.status == "error"
                    and isinstance(last.completion, dict)
                    and last.completion.get("error_type") == "stale_execution"
                )
            ):
                continue
        await CompletionService().start_next_queued_if_idle(str(report_id))


async def run_recovery():
    while True:
        try:
            await sweep()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Worker recovery sweep failed")
        await asyncio.sleep(30)
