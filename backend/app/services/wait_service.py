"""Persist a one-shot agent resume before registering its scheduler wake hint.

Consumers claim durable receipts across processes. A timer may disappear during
worker death without losing the pending resume; cancellation and listing also
consult receipts. Once an agent completion exists, recovery reports interruption
instead of replaying potentially irreversible tool effects.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from apscheduler.jobstores.base import JobLookupError

from app.core.scheduler import scheduler

logger = logging.getLogger(__name__)

_JOB_PREFIX = "wait:"


def _job_id(report_id: str, token: str) -> str:
    return f"{_JOB_PREFIX}{report_id}:{token}"


async def deliver_wait_wake(
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
    from app.dependencies import async_session_maker
    from app.services.machine_turn import run_machine_turn
    from app.models.user import User
    from app.models.report import Report
    from app.models.organization import Organization

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
        # The job was armed while the user could act. Re-check at fire time:
        # a deactivated or removed user must not keep driving the agent.
        from app.core.permission_resolver import principal_can_act_in_org
        if not await principal_can_act_in_org(db, user, str(organization.id)):
            logger.warning("wait wake %s: user %s deactivated or left org; dropping", job_id, user_id)
            return

        wake_prompt = (
            f"[Automatic resume after a scheduled wait] The wait you requested has "
            f"elapsed. Resume the task now: {reason}\n"
            f"If conversation history shows this was already handled or superseded, "
            f"acknowledge briefly and stop — do not redo the work."
        )
        try:
            # Machine turn: visible "wait elapsed" event strip + hidden
            # trigger prompt, instead of a synthetic user bubble.
            await run_machine_turn(
                db,
                report=report,
                user=user,
                organization=organization,
                summary=f"Wait elapsed — resuming: {reason}"[:300],
                trigger_source="wait",
                message_type="wait_resume_event",
                instruction=wake_prompt,
                # Structured fields for locale-aware frontend rendering.
                meta={"reason": reason[:200]},
            )
        except Exception as e:
            logger.error("wait wake %s: resume failed: %s", job_id, e)
            raise


async def run_wait_wake(job_id: str, report_id: str, user_id: str, organization_id: str,
                        reason: str, attempt: int = 1):
    from app.services.background_jobs import enqueue, session_factory
    async with session_factory()() as db:
        await enqueue(db, key=job_id, kind='wait', payload=dict(
            job_id=job_id, report_id=report_id, user_id=user_id,
            organization_id=organization_id, reason=reason, attempt=attempt))
        await db.commit()


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

        from app.services.background_jobs import enqueue_sync
        enqueue_sync(key=job_id, kind='wait', available_at=wake_at.replace(tzinfo=None),
                     payload=dict(job_id=job_id, report_id=report_id, user_id=user_id,
                                  organization_id=organization_id, reason=reason, attempt=int(attempt)))
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
        from app.services.background_jobs import cancel_pending
        cancelled = cancel_pending(job_id)
        try:
            scheduler.remove_job(job_id=job_id)
            logger.info("Cancelled wait %s", job_id)
            return True
        except JobLookupError:
            return cancelled

    def list_waits(self, report_id: str) -> list[dict]:
        """Pending (not yet fired) waits for a report.

        Job ids are namespaced ``wait:{report_id}:{token}``, so this is a
        prefix scan over the shared job store. Returns
        [{job_id, wake_at, reason}, ...] sorted by wake time.
        """
        prefix = f"{_JOB_PREFIX}{report_id}:"
        out: list[dict] = []
        try:
            for job in scheduler.get_jobs():
                if not str(job.id).startswith(prefix):
                    continue
                kwargs = getattr(job, "kwargs", {}) or {}
                wake_at = getattr(job, "next_run_time", None)
                out.append({
                    "job_id": str(job.id),
                    "wake_at": wake_at.isoformat() if wake_at else None,
                    "reason": kwargs.get("reason"),
                })
        except Exception as e:
            logger.warning("list_waits(%s) failed: %s", report_id, e)
        from sqlalchemy.orm import Session
        from sqlalchemy import select
        from app.core.scheduler import _engine
        from app.models.background_job import BackgroundJob
        with Session(_engine) as db:
            rows = db.execute(select(BackgroundJob).where(
                BackgroundJob.kind == 'wait', BackgroundJob.status == 'pending',
            )).scalars().all()
            for row in rows:
                if row.payload.get('report_id') == str(report_id) and not any(x['job_id'] == row.key for x in out):
                    out.append(dict(job_id=row.key, wake_at=row.available_at.isoformat(), reason=row.payload.get('reason')))
        out.sort(key=lambda j: j.get("wake_at") or "")
        return out

    def cancel_waits_for_user(self, user_id: str, organization_id: str | None = None) -> list[str]:
        """Cancel every pending wait armed by ``user_id``. With ``organization_id``,
        only waits in that org. Called on deprovisioning. ``run_wait_wake``
        re-checks at fire time anyway, so this just clears dead jobs early.
        """
        cancelled: list[str] = []
        try:
            jobs = scheduler.get_jobs()
        except Exception as e:
            logger.warning("cancel_waits_for_user(%s) failed: %s", user_id, e)
            return cancelled
        from sqlalchemy.orm import Session
        from sqlalchemy import select
        from types import SimpleNamespace
        from app.core.scheduler import _engine
        from app.models.background_job import BackgroundJob
        with Session(_engine) as db:
            pending = db.execute(select(BackgroundJob).where(
                BackgroundJob.kind == 'wait', BackgroundJob.status == 'pending',
            )).scalars().all()
            jobs = list(jobs)
            known = {str(j.id) for j in jobs}
            jobs.extend(SimpleNamespace(id=row.key, kwargs=row.payload) for row in pending if row.key not in known)
        for job in jobs:
            if not str(job.id).startswith(_JOB_PREFIX):
                continue
            kwargs = getattr(job, "kwargs", {}) or {}
            if str(kwargs.get("user_id")) != str(user_id):
                continue
            if organization_id is not None and str(kwargs.get("organization_id")) != str(organization_id):
                continue
            if self.cancel_wait(str(job.id)):
                cancelled.append(str(job.id))
        return cancelled

    def cancel_waits_for_report(self, report_id: str) -> list[str]:
        """Cancel every pending wait on a report. Returns the cancelled job ids."""
        cancelled: list[str] = []
        for j in self.list_waits(report_id):
            if self.cancel_wait(j["job_id"]):
                cancelled.append(j["job_id"])
        return cancelled


wait_service = WaitService()
