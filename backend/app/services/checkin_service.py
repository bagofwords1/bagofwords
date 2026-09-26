"""Agent check-ins — invisible one-shot follow-ups the agent plans for itself.

Lifecycle (every step is recorded on an ``agent_checkins`` row and every step
is gated by the ``enable_agent_checkins`` org setting):

    chat turn ends (agent_v2 post-analysis)
      └─ dispatch_after_turn  (background task, own session, no SSE, no blocks)
           eligibility (code) → planner (small model) → limits (code)
           → row 'planned' + APScheduler date job ``checkin:<id>``
    … hours/days later …
    run_checkin_wake(checkin_id)  (module-level; claim_scheduled_run)
      → setting / access / caps (code) → judge (small model): run | skip
      → run_machine_turn(trigger_source='checkin') in the same report
      → 'sent' if the run called notify, else 'ran_quiet'

Modelled on ``wait_service``: the job callable is the MODULE-LEVEL
``run_checkin_wake`` (APScheduler's SQLAlchemyJobStore serializes callables by
import path; a bound method would not survive a restart), and job kwargs carry
only the id — everything else is re-read from the DB at fire time so setting
and permission changes are respected.

Nothing here is user-visible until the judge says run: no ScheduledPrompt row,
no tool card, no completion.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from apscheduler.jobstores.base import JobLookupError
from sqlalchemy import select, update
from sqlalchemy.orm import lazyload

from app.core.scheduler import scheduler, claim_scheduled_run
from app.models.agent_checkin import (
    AgentCheckin,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_NOT_PROPOSED,
    STATUS_PLANNED,
    STATUS_RAN_QUIET,
    STATUS_REJECTED,
    STATUS_RUNNING,
    STATUS_SENT,
    STATUS_SKIPPED,
    REASON_DISABLED,
    REASON_INVALID_JUDGE_OUTPUT,
    REASON_JUDGE_SKIP,
    REASON_OPTED_OUT,
    REASON_REPORT_DELETED,
    REASON_RUN_FAILED,
)
from app.services import checkin_policy as policy

logger = logging.getLogger(__name__)

JOB_PREFIX = "checkin:"
TRIGGER_SOURCE = "checkin"
MESSAGE_TYPE = "checkin_event"
MISFIRE_GRACE_SECONDS = 6 * 3600

# How long the planner waits for the source turn to be finalized in the DB
# (the post-analysis hook runs just before the completion is closed out).
_FINALIZE_WAIT_SECONDS = 60


def job_id_for(checkin_id: str) -> str:
    return f"{JOB_PREFIX}{checkin_id}"


def _utcnow() -> datetime:
    return datetime.utcnow()


# ── text helpers ────────────────────────────────────────────────────────────

_PLANNING_HEADER = re.compile(r"\*\*[^\n*]*Planning[^\n*]*\*\*")


def _answer_text(completion_json: Any, limit: int = 2500) -> str:
    """Clean final-answer prose from a system completion's serialized content."""
    if isinstance(completion_json, dict):
        text = str(completion_json.get("content") or completion_json.get("text") or "")
    else:
        text = str(completion_json or "")
    parts = _PLANNING_HEADER.split(text)
    tail = parts[-1] if parts else text
    if not tail.strip() and len(parts) > 1:
        tail = parts[-2]
    tail = re.sub(r"\s+", " ", tail).strip()
    return tail[:limit] + ("…" if len(tail) > limit else "")


def _prompt_text(prompt_json: Any, limit: int = 500) -> str:
    text = str((prompt_json or {}).get("content") or "") if isinstance(prompt_json, dict) else str(prompt_json or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _fmt_local(dt_utc: Optional[datetime], tz_name: str) -> str:
    if not dt_utc:
        return "unknown"
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name)).strftime("%a %Y-%m-%d %H:%M")


def _fmt_ago(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{int(delta.total_seconds() // 60)} minutes"
    if hours < 48:
        return f"{hours:.0f} hours"
    return f"{hours / 24:.1f} days"


# ── the scheduler callback ──────────────────────────────────────────────────

async def run_checkin_wake(checkin_id: str) -> None:
    """APScheduler callback. Every worker/replica runs its own scheduler
    against the shared job store, so claim the fire before doing anything."""
    job_id = job_id_for(checkin_id)
    if not await asyncio.to_thread(claim_scheduled_run, job_id):
        return
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        try:
            await checkin_service.fire(db, checkin_id)
        except Exception:
            logger.exception("checkin wake %s failed", checkin_id)


class CheckinService:
    """Plans, arms, cancels and fires agent check-ins."""

    # ── scheduling ──────────────────────────────────────────────────────────

    def arm(self, checkin: AgentCheckin) -> str:
        job_id = job_id_for(checkin.id)
        run_date = checkin.due_at.replace(tzinfo=timezone.utc)
        scheduler.add_job(
            func=run_checkin_wake,
            trigger="date",
            run_date=run_date,
            id=job_id,
            kwargs={"checkin_id": str(checkin.id)},
            replace_existing=True,
            misfire_grace_time=MISFIRE_GRACE_SECONDS,
        )
        logger.info("Armed checkin %s report=%s due_at=%s", checkin.id, checkin.report_id, run_date.isoformat())
        return job_id

    def _remove_job(self, job_id: Optional[str]) -> bool:
        if not job_id or not str(job_id).startswith(JOB_PREFIX):
            return False
        try:
            scheduler.remove_job(job_id=job_id)
            return True
        except JobLookupError:
            return False  # already fired / already removed — idempotent
        except Exception:
            logger.warning("checkin: failed to remove job %s", job_id, exc_info=True)
            return False

    async def cancel(self, db, checkin_id: str, reason: str) -> bool:
        """Cancel one pending check-in: remove its job, mark the row."""
        row = await db.get(AgentCheckin, str(checkin_id))
        if row is None or row.status != STATUS_PLANNED:
            return False
        self._remove_job(row.job_id or job_id_for(row.id))
        row.status = STATUS_CANCELLED
        row.status_reason = reason
        await db.commit()
        return True

    async def _cancel_where(self, db, reason: str, *where) -> int:
        rows = (
            await db.execute(select(AgentCheckin).where(AgentCheckin.status == STATUS_PLANNED, *where))
        ).scalars().all()
        for row in rows:
            self._remove_job(row.job_id or job_id_for(row.id))
            row.status = STATUS_CANCELLED
            row.status_reason = reason
        if rows:
            await db.commit()
        return len(rows)

    async def cancel_for_report(self, db, report_id: str) -> int:
        return await self.cancel_for_reports(db, [report_id])

    async def cancel_for_reports(self, db, report_ids: list) -> int:
        ids = [str(r) for r in report_ids or []]
        if not ids:
            return 0
        n = await self._cancel_where(db, REASON_REPORT_DELETED, AgentCheckin.report_id.in_(ids))
        if n:
            logger.info("Cancelled %s checkin(s) for archived report(s) %s", n, ids)
        return n

    async def cancel_all_for_org(self, db, organization_id: str, reason: str = REASON_DISABLED) -> int:
        """Settings-off hook: no dormant jobs are left behind."""
        n = await self._cancel_where(db, reason, AgentCheckin.organization_id == str(organization_id))
        if n:
            logger.info("Cancelled %s pending checkin(s) for org %s (%s)", n, organization_id, reason)
        return n

    # ── planning ────────────────────────────────────────────────────────────

    async def dispatch_after_turn(
        self,
        *,
        organization_id: str,
        user_id: Optional[str],
        report_id: str,
        head_completion_id: Optional[str],
        system_completion_id: Optional[str],
        small_model_id: Optional[str],
        planner: Optional[Callable[..., Awaitable[Any]]] = None,
        now: Optional[datetime] = None,
        rng=None,
        wait_for_finalize: bool = True,
    ) -> Optional[AgentCheckin]:
        """Background entry point from the agent's post-analysis block.

        Never raises. Returns the recorded row (planned / not_proposed /
        rejected) or None when nothing was recorded.
        """
        try:
            return await self._dispatch_after_turn(
                organization_id=organization_id, user_id=user_id, report_id=report_id,
                head_completion_id=head_completion_id, system_completion_id=system_completion_id,
                small_model_id=small_model_id, planner=planner, now=now, rng=rng,
                wait_for_finalize=wait_for_finalize,
            )
        except Exception:
            logger.exception("checkin planning failed for report %s", report_id)
            return None

    async def _dispatch_after_turn(
        self, *, organization_id, user_id, report_id, head_completion_id, system_completion_id,
        small_model_id, planner, now, rng, wait_for_finalize,
    ) -> Optional[AgentCheckin]:
        from app.dependencies import async_session_maker
        from app.models.completion import Completion
        from app.models.llm_model import LLMModel
        from app.models.report import Report

        if not user_id or not head_completion_id or not system_completion_id:
            return None

        async with async_session_maker() as db:
            settings_row = await policy.load_org_settings(db, organization_id)
            if not policy.feature_enabled(settings_row):
                return None
            # The user opted out of check-ins for themselves: same as off.
            if await policy.user_opted_out(db, organization_id, user_id):
                return None

            # Eligibility (code, no LLM): a human-initiated turn only. This is
            # the guard that stops check-ins spawning check-ins, and stops wait
            # wakes, scheduled runs, evals and webhooks from planning.
            head = (
                await db.execute(select(Completion).options(lazyload("*")).where(Completion.id == str(head_completion_id)))
            ).scalar_one_or_none()
            if head is None or head.trigger_source is not None or head.webhook_id is not None:
                return None
            if getattr(head, "scheduled_prompt_id", None):
                return None
            report = (
                await db.execute(select(Report).options(lazyload("*")).where(Report.id == str(report_id)))
            ).scalar_one_or_none()
            if report is None or getattr(report, "report_type", "regular") != "regular":
                return None

            # Let the turn finish closing out so the planner sees the answer.
            system = None
            deadline = asyncio.get_event_loop().time() + (_FINALIZE_WAIT_SECONDS if wait_for_finalize else 0)
            while True:
                system = (
                    await db.execute(
                        select(Completion).options(lazyload("*"))
                        .where(Completion.id == str(system_completion_id))
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                if system is None or system.status != "in_progress":
                    break
                if asyncio.get_event_loop().time() >= deadline:
                    break
                await asyncio.sleep(1.0)
                await db.rollback()  # fresh snapshot on the next read
            if system is None or system.status == "error":
                return None

            now = now or _utcnow()
            checkin_id = str(uuid.uuid4())

            def _row(**kw) -> AgentCheckin:
                return AgentCheckin(
                    id=checkin_id, organization_id=str(organization_id), user_id=str(user_id),
                    report_id=str(report_id), source_completion_id=str(system_completion_id), **kw,
                )

            # Limits pre-check: don't pay for an LLM call we would reject.
            denied = await policy.check_plan_limits(
                db, organization_id=organization_id, user_id=user_id, report_id=report_id,
                org_settings=settings_row, now=now,
            )
            if denied:
                row = _row(status=STATUS_REJECTED, status_reason=denied)
                db.add(row)
                await db.commit()
                logger.info("checkin %s rejected before planning: %s", checkin_id, denied)
                return row

            model = await db.get(LLMModel, str(small_model_id)) if small_model_id else None
            if model is None and planner is None:
                return None

            tz_name = policy.org_timezone(settings_row)
            ctx = await self._planner_context(db, report=report, user_id=user_id, system=system, now=now, tz_name=tz_name)

            if planner is None:
                from app.ai.agents.checkins.planner import run_planner as planner  # noqa: N806
            out = await planner(model, ctx, checkin_id=checkin_id)
            if out is None:
                return None  # invalid planner output: no check-in, no row

            if not out.propose:
                row = _row(status=STATUS_NOT_PROPOSED, plan_reason=out.reason)
                db.add(row)
                await db.commit()
                return row

            # Re-check: the setting may have flipped during the planner call.
            await db.rollback()
            settings_row = await policy.load_org_settings(db, organization_id)
            if not policy.feature_enabled(settings_row):
                return None
            denied = await policy.check_plan_limits(
                db, organization_id=organization_id, user_id=user_id, report_id=report_id,
                org_settings=settings_row, now=now,
            )
            if denied:
                row = _row(status=STATUS_REJECTED, status_reason=denied, note=out.note, plan_reason=out.reason)
                db.add(row)
                await db.commit()
                return row

            due_at = policy.plan_due_at(now, out.due_in_hours, tz_name, rng=rng)
            row = _row(
                status=STATUS_PLANNED, note=out.note, plan_reason=out.reason,
                due_at=due_at, job_id=job_id_for(checkin_id),
            )
            db.add(row)
            await db.commit()
            try:
                self.arm(row)
            except Exception:
                logger.exception("checkin %s: arming failed", checkin_id)
                row.status = STATUS_FAILED
                row.status_reason = "arm_failed"
                await db.commit()
                return row
            logger.info("checkin planned id=%s report=%s due_at=%s", checkin_id, report_id, due_at.isoformat())
            return row

    async def _planner_context(self, db, *, report, user_id, system, now, tz_name) -> dict:
        from app.models.completion import Completion
        from app.models.scheduled_prompt import ScheduledPrompt
        from app.models.step import Step
        from app.models.user import User
        from app.models.widget import Widget

        user = await db.get(User, str(user_id))
        prompts = (
            await db.execute(
                select(Completion.prompt).where(
                    Completion.report_id == str(report.id),
                    Completion.role == "user",
                    Completion.trigger_source.is_(None),
                    Completion.webhook_id.is_(None),
                ).order_by(Completion.created_at.desc()).limit(3)
            )
        ).scalars().all()
        steps = (
            await db.execute(
                select(Step.title, Step.updated_at, Step.status)
                .join(Widget, Widget.id == Step.widget_id)
                .where(Widget.report_id == str(report.id), Step.deleted_at.is_(None))
                .order_by(Step.updated_at.desc()).limit(8)
            )
        ).all()
        schedules = (
            await db.execute(
                select(ScheduledPrompt).options(lazyload("*")).where(
                    ScheduledPrompt.report_id == str(report.id),
                    ScheduledPrompt.deleted_at.is_(None),
                    ScheduledPrompt.is_active.is_(True),
                )
            )
        ).scalars().all()
        cadence = [
            f"scheduled prompt '{sp.title or _prompt_text(sp.prompt, 80)}' cron '{sp.cron_schedule}', last ran {sp.last_run_at or 'never'}"
            for sp in schedules
        ]
        try:
            from app.services.wait_service import wait_service
            for w in wait_service.list_waits(str(report.id)):
                cadence.append(f"pending wait until {w.get('wake_at')}: {_prompt_text(w.get('reason'), 160)}")
        except Exception:
            pass
        return {
            "now_local": _fmt_local(now, tz_name),
            "timezone": tz_name,
            "user_name": getattr(user, "name", None) or getattr(user, "email", None),
            "report_title": report.title,
            "user_prompts": [_prompt_text(p) for p in reversed(prompts)],
            "final_answer": _answer_text(system.completion),
            "data_steps": [f"{t or '(untitled)'} — {u.strftime('%Y-%m-%d %H:%M') if u else 'unknown'} ({s})" for t, u, s in steps],
            "refresh_cadence": cadence,
        }

    # ── firing ──────────────────────────────────────────────────────────────

    async def fire(
        self,
        db,
        checkin_id: str,
        *,
        judge: Optional[Callable[..., Awaitable[Any]]] = None,
        now: Optional[datetime] = None,
        ignore_working_window: bool = False,
    ) -> Optional[str]:
        """Run one due check-in through guardrails → judge → machine turn.
        Returns the final status (or None if the row wasn't claimable).

        ``ignore_working_window`` is for the debug path only
        (tools/agent/fire_checkin.py); every other guardrail still applies."""
        now = now or _utcnow()

        # Atomic claim: only a 'planned' row proceeds. A duplicate fire (or a
        # fire racing a cancel) finds nothing to claim and returns.
        claimed = await db.execute(
            update(AgentCheckin)
            .where(AgentCheckin.id == str(checkin_id), AgentCheckin.status == STATUS_PLANNED)
            .values(status=STATUS_RUNNING, updated_at=now)
        )
        await db.commit()
        if (claimed.rowcount or 0) != 1:
            return None
        row = await db.get(AgentCheckin, str(checkin_id))
        await db.refresh(row)

        async def _finish(status: str, reason: Optional[str] = None) -> str:
            row.status = status
            row.status_reason = reason
            await db.commit()
            logger.info("checkin %s -> %s%s", row.id, status, f":{reason}" if reason else "")
            return status

        # 1) Setting.
        settings_row = await policy.load_org_settings(db, row.organization_id)
        if not policy.feature_enabled(settings_row):
            return await _finish(STATUS_CANCELLED, REASON_DISABLED)

        # 1b) The user opted out since this was planned.
        if await policy.user_opted_out(db, row.organization_id, row.user_id):
            return await _finish(STATUS_CANCELLED, REASON_OPTED_OUT)

        # 2) Report + access.
        reason = await policy.check_access(db, row)
        if reason:
            return await _finish(STATUS_CANCELLED, reason)

        # 3) Caps.
        reason = await policy.check_fire_limits(db, row, settings_row, now=now)
        if reason:
            return await _finish(STATUS_CANCELLED, reason)

        # 4) Working window (a re-armed or misfired job can land outside it).
        tz_name = policy.org_timezone(settings_row)
        if not ignore_working_window and not policy.in_working_window(now, tz_name):
            return await self._rearm(db, row, policy.shift_into_working_window(now, tz_name))

        # 5) Don't collide with a live run in this report.
        if await policy.live_run_in_report(db, row.report_id):
            return await self._rearm(db, row, now + policy.LIVE_RUN_REARM)

        # 6) Judge.
        from app.models.organization import Organization
        from app.models.report import Report
        from app.models.user import User

        organization = await db.get(Organization, row.organization_id)
        user = await db.get(User, row.user_id)
        report = (
            await db.execute(select(Report).where(Report.id == row.report_id))
        ).scalar_one_or_none()

        small_model = None
        try:
            from app.services.llm_service import LLMService
            small_model = await LLMService().get_default_model(db, organization, user, is_small=True)
        except Exception:
            logger.warning("checkin %s: no small model", row.id, exc_info=True)

        ctx = await self._judge_context(db, row=row, report=report, now=now, tz_name=tz_name)
        if judge is None:
            from app.ai.agents.checkins.judge import run_judge as judge  # noqa: N806
        verdict = await judge(small_model, ctx, checkin_id=row.id)
        row.judge_decision = verdict.decision
        row.judge_reason = verdict.reason
        row.judge_focus = verdict.focus
        row.judged_at = now
        await db.commit()
        if verdict.decision != "run":
            code = REASON_INVALID_JUDGE_OUTPUT if getattr(verdict, "invalid", False) else REASON_JUDGE_SKIP
            return await _finish(STATUS_SKIPPED, code)

        # 7) Setting again, just before the run.
        settings_row = await policy.load_org_settings(db, row.organization_id)
        if not policy.feature_enabled(settings_row):
            return await _finish(STATUS_CANCELLED, REASON_DISABLED)

        # 8) The machine turn, as the user, in the same report.
        return await self._run(db, row=row, report=report, user=user, organization=organization)

    async def _rearm(self, db, row: AgentCheckin, due_at: datetime) -> str:
        row.status = STATUS_PLANNED
        row.due_at = due_at
        row.job_id = job_id_for(row.id)
        await db.commit()
        self.arm(row)
        logger.info("checkin %s re-armed for %s", row.id, due_at.isoformat())
        return STATUS_PLANNED

    async def _run(self, db, *, row: AgentCheckin, report, user, organization) -> str:
        from app.ai.agents.checkins.prompts import render_checkin_prompt
        from app.models.completion import Completion
        from app.services.machine_turn import run_machine_turn

        instruction = render_checkin_prompt(
            user_name=getattr(user, "name", None) or getattr(user, "email", "the user"),
            note=row.note or "",
            plan_reason=row.plan_reason or "",
            judge_reason=row.judge_reason or "",
            judge_focus=row.judge_focus or "",
        )
        checkin_id = str(row.id)
        event_ref: dict = {}
        failed = False
        try:
            await run_machine_turn(
                db,
                report=report,
                user=user,
                organization=organization,
                trigger_source=TRIGGER_SOURCE,
                message_type=MESSAGE_TYPE,
                summary="Follow-up",  # fallback text; meta drives the localized label
                meta={"checkin_id": checkin_id},
                instruction=instruction,
                on_event=lambda ev: event_ref.update(id=str(ev.id), turn_index=ev.turn_index),
            )
        except Exception as e:
            failed = True
            logger.error("checkin %s: run failed: %s", checkin_id, e)

        try:
            await db.rollback()
        except Exception:
            pass
        row = await db.get(AgentCheckin, checkin_id)
        await db.refresh(row)

        # The run's reply: the first check-in system completion after our strip.
        run_completion = None
        if event_ref:
            run_completion = (
                await db.execute(
                    select(Completion).options(lazyload("*")).where(
                        Completion.report_id == row.report_id,
                        Completion.role == "system",
                        Completion.trigger_source == TRIGGER_SOURCE,
                        Completion.turn_index > event_ref["turn_index"],
                    ).order_by(Completion.turn_index.asc()).limit(1)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
        if run_completion is not None:
            row.run_completion_id = str(run_completion.id)
            if run_completion.status == "error":
                failed = True

        if failed:
            status, reason = STATUS_FAILED, REASON_RUN_FAILED
        elif row.notified:
            status, reason = STATUS_SENT, None
        else:
            status, reason = STATUS_RAN_QUIET, None
        row.status = status
        row.status_reason = reason
        await db.commit()
        await self._stamp_strip(db, row, event_ref.get("id"))
        logger.info("checkin %s -> %s (run_completion=%s)", checkin_id, status, row.run_completion_id)
        return status

    async def _stamp_strip(self, db, row: AgentCheckin, event_id: Optional[str]) -> None:
        """Expose the outcome on the visible event strip (prompt.meta) so the
        report UI can collapse a quiet run without another endpoint."""
        from app.models.completion import Completion

        if not event_id:
            return
        strip = (
            await db.execute(
                select(Completion).options(lazyload("*")).where(Completion.id == event_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if strip is None:
            return
        prompt = dict(strip.prompt or {})
        meta = dict(prompt.get("meta") or {})
        meta.update({
            "outcome": row.status,
            "run_completion_id": row.run_completion_id,
            "notify_subject": row.notify_subject,
        })
        prompt["meta"] = meta
        strip.prompt = prompt
        await db.commit()

    async def _judge_context(self, db, *, row: AgentCheckin, report, now: datetime, tz_name: str) -> dict:
        from app.models.completion import Completion
        from app.models.report_view import ReportView
        from app.models.scheduled_prompt import ScheduledPrompt
        from app.models.step import Step
        from app.models.widget import Widget

        source = await db.get(Completion, row.source_completion_id) if row.source_completion_id else None
        since = getattr(source, "created_at", None) or row.created_at or now

        last_answer = (
            await db.execute(
                select(Completion.completion).where(
                    Completion.report_id == row.report_id, Completion.role == "system",
                ).order_by(Completion.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        new_prompts = (
            await db.execute(
                select(Completion.prompt, Completion.created_at).where(
                    Completion.report_id == row.report_id,
                    Completion.role == "user",
                    Completion.trigger_source.is_(None),
                    Completion.webhook_id.is_(None),
                    Completion.created_at > since,
                ).order_by(Completion.created_at.asc()).limit(5)
            )
        ).all()

        refreshes: list[str] = []
        steps = (
            await db.execute(
                select(Step.title, Step.updated_at)
                .join(Widget, Widget.id == Step.widget_id)
                .where(Widget.report_id == row.report_id, Step.deleted_at.is_(None))
                .order_by(Step.updated_at.desc()).limit(5)
            )
        ).all()
        for title, updated in steps:
            refreshes.append(f"step '{title or '(untitled)'}' last ran {_fmt_local(updated, tz_name)}")
        schedules = (
            await db.execute(
                select(ScheduledPrompt.title, ScheduledPrompt.cron_schedule, ScheduledPrompt.last_run_at).where(
                    ScheduledPrompt.report_id == row.report_id, ScheduledPrompt.deleted_at.is_(None),
                )
            )
        ).all()
        for title, cron, last_run in schedules:
            refreshes.append(f"scheduled prompt '{title or 'untitled'}' ({cron}) last ran {_fmt_local(last_run, tz_name)}")
        for ds in getattr(report, "data_sources", None) or []:
            refreshes.append(f"data source '{getattr(ds, 'name', '?')}': refresh time unknown")

        history_rows = (
            await db.execute(
                select(AgentCheckin).where(
                    AgentCheckin.organization_id == row.organization_id,
                    AgentCheckin.user_id == row.user_id,
                    AgentCheckin.id != row.id,
                    AgentCheckin.judge_decision.isnot(None),
                ).order_by(AgentCheckin.created_at.desc()).limit(5)
            )
        ).scalars().all()
        history: list[str] = []
        for h in history_rows:
            opened = "n/a"
            if h.notified and h.sent_at:
                lv = (
                    await db.execute(
                        select(ReportView.last_viewed_at).where(
                            ReportView.report_id == h.report_id, ReportView.user_id == h.user_id,
                        )
                    )
                ).scalar_one_or_none()
                opened = "yes" if (lv and lv > h.sent_at) else "no"
            history.append(
                f"{_fmt_local(h.judged_at, tz_name)}: judge={h.judge_decision}, outcome={h.status}, "
                f"notified={'yes' if h.notified else 'no'}, opened={opened}"
            )

        return {
            "now_local": _fmt_local(now, tz_name),
            "timezone": tz_name,
            "report_title": getattr(report, "title", None),
            "note": row.note,
            "plan_reason": row.plan_reason,
            "planned_ago": _fmt_ago(now - (row.created_at or now)),
            "due_at": row.due_at.strftime("%Y-%m-%d %H:%M") if row.due_at else "unknown",
            "last_answer": _answer_text(last_answer, 1500),
            "new_user_prompts": [f"{_fmt_local(ts, tz_name)}: {_prompt_text(p, 300)}" for p, ts in new_prompts],
            "refreshes": refreshes,
            "history": history,
        }

    # ── notify-tool bridge ──────────────────────────────────────────────────

    async def running_checkin_for_report(self, db, report_id: str) -> Optional[AgentCheckin]:
        return (
            await db.execute(
                select(AgentCheckin).where(
                    AgentCheckin.report_id == str(report_id), AgentCheckin.status == STATUS_RUNNING,
                ).order_by(AgentCheckin.updated_at.desc()).limit(1)
            )
        ).scalars().first()


checkin_service = CheckinService()
