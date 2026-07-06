"""Wait service — one-shot "pause then resume" for the agent loop.

The ``wait`` tool ends the current agent turn (like clarify) and arms a *single*
APScheduler ``date`` job through this service. When the job fires, the agent is
resumed on the SAME report by creating a fresh completion whose prompt is the
``reason`` the agent gave when it paused. Full conversation history reloads
automatically, so it can retry exactly where it left off.

This is deliberately NOT a scheduled task:
  - one-shot (``trigger='date'``), never recurring;
  - no user-visible ``ScheduledPrompt`` row — the only record is the ``wait``
    tool execution, which the UI already renders;
  - it self-deletes after firing once.

It reuses the shared scheduler (``app.core.scheduler``) and the cross-worker
run-claim so a fire executes exactly once across uvicorn workers / replicas.

NOTE: the job callable is the MODULE-LEVEL ``run_wait_wake`` function, not a
bound method. APScheduler's ``SQLAlchemyJobStore`` serializes the callable by
import path; a bound method deserializes without ``self`` and would crash on
fire after a restart. A module-level function round-trips cleanly.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from apscheduler.jobstores.base import JobLookupError

from app.core.scheduler import scheduler, claim_scheduled_run

logger = logging.getLogger(__name__)

_JOB_PREFIX = "wait:"


def _job_id(report_id: str, token: str) -> str:
    return f"{_JOB_PREFIX}{report_id}:{token}"


async def run_wait_wake(
    job_id: str,
    report_id: str,
    user_id: str,
    organization_id: str,
    reason: str,
    attempt: int = 1,
) -> None:
    """APScheduler callback: resume the agent on ``report_id``.

    Mirrors ``ScheduledPromptService.scheduled_run_prompt`` but injects the pause
    ``reason`` as a synthesized continuation prompt instead of reading a stored
    ScheduledPrompt.
    """
    # Every worker/replica runs its own scheduler against the shared job store,
    # so this may fire N times. Claim it so exactly one proceeds.
    if not await asyncio.to_thread(claim_scheduled_run, job_id):
        return

    from app.dependencies import async_session_maker
    from app.services.completion_service import CompletionService
    from app.schemas.completion_v2_schema import CompletionCreate
    from app.schemas.completion_schema import PromptSchema
    from app.models.user import User
    from app.models.report import Report
    from app.models.organization import Organization

    completion_service = CompletionService()

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        if not report or report.deleted_at:
            logger.warning("wait wake %s: report gone", job_id)
            return
        user = await db.get(User, user_id)
        organization = await db.get(Organization, organization_id)
        if not user or not organization:
            logger.warning("wait wake %s: user/org gone", job_id)
            return

        wake_prompt = (
            f"[Automatic resume after a scheduled wait] The wait you requested has "
            f"elapsed. Resume the task now: {reason}"
        )
        try:
            await completion_service.create_completion(
                db=db,
                report_id=report.id,
                completion_data=CompletionCreate(prompt=PromptSchema(content=wake_prompt)),
                current_user=user,
                organization=organization,
                background=False,
            )
        except Exception as e:
            logger.error("wait wake %s: resume failed: %s", job_id, e)


class WaitService:
    """Arms and cancels one-shot agent-resume jobs."""

    def schedule_wait(
        self,
        *,
        report_id: str,
        user_id: str,
        organization_id: str,
        reason: str,
        delay_minutes: int,
        attempt: int = 1,
    ) -> dict:
        """Register a one-shot resume job. Returns {job_id, wake_at (ISO UTC)}."""
        token = uuid.uuid4().hex[:12]
        job_id = _job_id(report_id, token)
        wake_at = datetime.now(timezone.utc) + timedelta(minutes=int(delay_minutes))

        scheduler.add_job(
            func=run_wait_wake,
            trigger="date",
            run_date=wake_at,
            id=job_id,
            kwargs={
                "job_id": job_id,
                "report_id": report_id,
                "user_id": user_id,
                "organization_id": organization_id,
                "reason": reason,
                "attempt": int(attempt),
            },
            replace_existing=True,
            misfire_grace_time=3600,  # if the worker was down, still resume within an hour
        )
        logger.info("Armed wait %s -> resume at %s (attempt %s)", job_id, wake_at.isoformat(), attempt)
        return {"job_id": job_id, "wake_at": wake_at.isoformat()}

    def cancel_wait(self, job_id: str) -> bool:
        """Remove a pending resume job. Returns True if a job was removed."""
        if not job_id or not job_id.startswith(_JOB_PREFIX):
            return False
        try:
            scheduler.remove_job(job_id=job_id)
            logger.info("Cancelled wait %s", job_id)
            return True
        except JobLookupError:
            # Already fired or already cancelled — idempotent success.
            return False


wait_service = WaitService()
