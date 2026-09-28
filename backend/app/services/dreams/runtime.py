"""The overnight runtime: the hourly sweep, exactly-once units, budget, run log.

``overnight_sweep`` is registered as a leader-only hourly job. Each tick, for
every org whose local time is inside a dream window, it picks the due units
(users 01:00-03:00, agents 03:00-05:00 local) and runs them with bounded
concurrency. A unit runs at most once per org-local night: a ``dream_runs``
row for the same (kind, target, local_date) that is done / skipped / running
blocks another attempt, and failed units get at most ``MAX_ATTEMPTS`` tries.
``claim_scheduled_run`` additionally guards against two replicas that both
believe they lead.

Watermarks (``Membership.user_dreamed_at`` / ``DataSource.agent_dreamed_at``)
advance only when a run ends done or skipped, so a failure is retried.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.orm import lazyload

from app.models.dream_run import (
    KIND_AGENT,
    KIND_USER,
    REASON_BUDGET,
    REASON_DISABLED,
    REASON_STALE,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SKIPPED,
    DreamRun,
)
from app.services.dreams import common as C

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 2
# Without a watermark, look this far back for "new activity".
FIRST_RUN_LOOKBACK_HOURS = 36


@dataclass
class DreamResult:
    """What a dream implementation returns to the runtime."""

    status: str  # done | skipped | cancelled (a switch turned off mid-run)
    reason: Optional[str] = None
    inputs_summary: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    outputs: Dict[str, Any] = field(default_factory=dict)


DreamFn = Callable[..., Awaitable[DreamResult]]


def _session_maker():
    from app.dependencies import async_session_maker

    return async_session_maker


async def load_org_settings(db, organization_id: str):
    from app.services.checkin_policy import load_org_settings as _load

    return await _load(db, organization_id)


async def nightly_learning_for(db, organization, org_settings, data_source) -> bool:
    """The per-agent switch, resolved like every Self-Learning field."""
    from app.schemas.agent_automation_schema import resolve_policy

    org_defaults = None
    try:
        cfg = org_settings.get_config("agent_automation_defaults") if org_settings else None
        org_defaults = getattr(cfg, "value", cfg)
    except Exception:
        org_defaults = None
    override = getattr(data_source, "automation_settings", None)
    policy = resolve_policy(
        org_defaults if isinstance(org_defaults, dict) else None,
        override if isinstance(override, dict) else None,
    )
    return bool(getattr(policy, "nightly_learning", True))


class DreamRuntime:
    def __init__(
        self,
        *,
        agent_dream: Optional[DreamFn] = None,
        user_dream: Optional[DreamFn] = None,
        session_maker=None,
        global_concurrency: int = C.GLOBAL_CONCURRENCY,
        per_org_concurrency: int = C.PER_ORG_CONCURRENCY,
    ):
        self._agent_dream = agent_dream
        self._user_dream = user_dream
        self._session_maker = session_maker
        self._global = asyncio.Semaphore(global_concurrency)
        self._per_org_limit = per_org_concurrency
        self._per_org: Dict[str, asyncio.Semaphore] = {}

    # ------------------------------------------------------------ plumbing

    def _sessions(self):
        return self._session_maker or _session_maker()

    def _org_sem(self, org_id: str) -> asyncio.Semaphore:
        sem = self._per_org.get(org_id)
        if sem is None:
            sem = asyncio.Semaphore(self._per_org_limit)
            self._per_org[org_id] = sem
        return sem

    def _dream_fn(self, kind: str) -> DreamFn:
        if kind == KIND_AGENT:
            if self._agent_dream is None:
                from app.services.dreams.agent_dream import run_agent_dream

                self._agent_dream = run_agent_dream
            return self._agent_dream
        if self._user_dream is None:
            from app.services.dreams.user_dream import run_user_dream

            self._user_dream = run_user_dream
        return self._user_dream

    # ------------------------------------------------------------ the sweep

    async def sweep(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """One hourly tick. Returns a small summary for logs and tests."""
        from app.models.organization_settings import OrganizationSettings

        now = now or C.utcnow()
        summary: Dict[str, Any] = {"stale": 0, "queued": 0, "ran": 0}
        async with self._sessions()() as db:
            summary["stale"] = await self.fail_stale_runs(db, now)
            rows = (await db.execute(select(OrganizationSettings))).scalars().all()
            plan: List[tuple] = []
            for settings_row in rows:
                org_id = str(settings_row.organization_id)
                tz = C.org_timezone(settings_row)
                if C.user_dreaming_enabled(settings_row) and C.in_window(now, tz, C.USER_WINDOW):
                    for uid in await self.due_users(db, org_id, now):
                        plan.append((KIND_USER, org_id, uid))
                if C.agent_dreaming_enabled(settings_row) and C.in_window(now, tz, C.AGENT_WINDOW):
                    for ds_id in await self.due_agents(db, org_id, settings_row, now):
                        plan.append((KIND_AGENT, org_id, ds_id))
        summary["queued"] = len(plan)
        if not plan:
            return summary
        results = await asyncio.gather(
            *(self._run_bounded(kind, org_id, target, now) for kind, org_id, target in plan),
            return_exceptions=True,
        )
        summary["ran"] = sum(1 for r in results if isinstance(r, DreamRun))
        return summary

    async def _run_bounded(self, kind: str, org_id: str, target_id: str, now: datetime):
        async with self._global:
            async with self._org_sem(org_id):
                return await self.run_unit(kind, org_id, target_id, now=now)

    # ------------------------------------------------------------ due units

    async def due_users(self, db, organization_id: str, now: datetime) -> List[str]:
        """Members with new human-initiated activity since their watermark or a
        memory event in the next days (a follow-up may be worth planning)."""
        from app.models.completion import Completion
        from app.models.membership import Membership
        from app.models.memory_entry import MemoryEntry
        from app.models.report import Report

        members = (
            await db.execute(
                select(Membership.user_id, Membership.user_dreamed_at).where(
                    Membership.organization_id == str(organization_id),
                    Membership.user_id.isnot(None),
                )
            )
        ).all()
        due: List[str] = []
        horizon = now + timedelta(days=C.UPCOMING_EVENT_DAYS)
        for user_id, watermark in members:
            since = watermark or (now - timedelta(hours=FIRST_RUN_LOOKBACK_HOURS))
            active = (
                await db.execute(
                    select(Completion.id)
                    .join(Report, Report.id == Completion.report_id)
                    .where(
                        Report.organization_id == str(organization_id),
                        Completion.user_id == str(user_id),
                        Completion.role == "user",
                        Completion.trigger_source.is_(None),
                        Completion.webhook_id.is_(None),
                        Completion.scheduled_prompt_id.is_(None),
                        Completion.created_at > since,
                    )
                    .limit(1)
                )
            ).first()
            upcoming = None
            if active is None:
                upcoming = (
                    await db.execute(
                        select(MemoryEntry.id).where(
                            MemoryEntry.organization_id == str(organization_id),
                            MemoryEntry.user_id == str(user_id),
                            MemoryEntry.status == "active",
                            MemoryEntry.event_start.isnot(None),
                            MemoryEntry.event_start >= now,
                            MemoryEntry.event_start <= horizon,
                        ).limit(1)
                    )
                ).first()
            if active is not None or upcoming is not None:
                due.append(str(user_id))
            if len(due) >= C.MAX_UNITS_PER_ORG_PER_TICK:
                break
        return due

    async def due_agents(self, db, organization_id: str, org_settings, now: datetime) -> List[str]:
        from app.models.data_source import DataSource
        from app.models.organization import Organization
        from app.services.dreams.agent_dream import has_new_activity

        org = await db.get(Organization, str(organization_id))
        agents = (
            await db.execute(
                select(DataSource).options(lazyload("*")).where(
                    DataSource.organization_id == str(organization_id),
                    DataSource.deleted_at.is_(None),
                )
            )
        ).scalars().all()
        due: List[str] = []
        for ds in agents:
            if not await nightly_learning_for(db, org, org_settings, ds):
                continue
            if await has_new_activity(db, org_settings, ds, now):
                due.append(str(ds.id))
            if len(due) >= C.MAX_UNITS_PER_ORG_PER_TICK:
                break
        return due

    # ------------------------------------------------------------ one unit

    async def _already_ran(self, db, kind: str, target_id: str, day: str) -> bool:
        col = DreamRun.user_id if kind == KIND_USER else DreamRun.data_source_id
        rows = (
            await db.execute(
                select(DreamRun.status).where(
                    DreamRun.kind == kind, col == str(target_id), DreamRun.local_date == day,
                )
            )
        ).scalars().all()
        if any(s in (STATUS_DONE, STATUS_SKIPPED, STATUS_RUNNING, STATUS_QUEUED) for s in rows):
            return True
        return sum(1 for s in rows if s in (STATUS_FAILED, STATUS_CANCELLED)) >= MAX_ATTEMPTS

    async def _tokens_used_today(self, db, organization_id: str, day: str) -> int:
        total = (
            await db.execute(
                select(func.coalesce(func.sum(DreamRun.tokens), 0)).where(
                    DreamRun.organization_id == str(organization_id), DreamRun.local_date == day,
                )
            )
        ).scalar()
        return int(total or 0)

    async def _gate(self, db, kind: str, organization, org_settings, target_id: str) -> Optional[str]:
        """None when allowed; otherwise the reason it isn't."""
        if kind == KIND_AGENT:
            from app.models.data_source import DataSource

            if not C.agent_dreaming_enabled(org_settings):
                return REASON_DISABLED
            ds = await db.get(DataSource, str(target_id))
            if ds is None or ds.deleted_at is not None or str(ds.organization_id) != str(organization.id):
                return "target_missing"
            if not await nightly_learning_for(db, organization, org_settings, ds):
                return REASON_DISABLED
            return None
        from app.models.membership import Membership

        if not C.user_dreaming_enabled(org_settings):
            return REASON_DISABLED
        m = (
            await db.execute(
                select(Membership).where(
                    Membership.organization_id == str(organization.id),
                    Membership.user_id == str(target_id),
                )
            )
        ).scalar_one_or_none()
        if m is None:
            return "target_missing"
        return None

    async def run_unit(
        self,
        kind: str,
        organization_id: str,
        target_id: str,
        *,
        now: Optional[datetime] = None,
        ignore_window: bool = False,
        force: bool = False,
    ) -> Optional[DreamRun]:
        """Run one dream unit end to end. Returns the DreamRun row, or None
        when the unit was deduplicated / claimed elsewhere.

        ``ignore_window`` (debug trigger) skips the night-window check;
        ``force`` also bypasses the once-per-night dedupe. Settings are always
        honoured."""
        from app.core.scheduler import claim_scheduled_run
        from app.models.organization import Organization

        now = now or C.utcnow()
        sessions = self._sessions()
        async with sessions() as db:
            organization = await db.get(Organization, str(organization_id))
            if organization is None:
                return None
            org_settings = await load_org_settings(db, organization_id)
            tz = C.org_timezone(org_settings)
            day = C.local_date(now, tz)
            if not ignore_window and not C.in_window(now, tz, C.window_for(kind)):
                return None
            if not force and await self._already_ran(db, kind, target_id, day):
                return None
            claim_key = f"dream:{kind}:{target_id}:{day}"
            if not force and not await asyncio.to_thread(claim_scheduled_run, claim_key, 3600):
                return None

            run = DreamRun(
                organization_id=str(organization_id),
                kind=kind,
                data_source_id=str(target_id) if kind == KIND_AGENT else None,
                user_id=str(target_id) if kind == KIND_USER else None,
                local_date=day,
                status=STATUS_RUNNING,
                started_at=now,
            )
            denied = await self._gate(db, kind, organization, org_settings, target_id)
            if denied:
                run.status = STATUS_CANCELLED if denied == REASON_DISABLED else STATUS_SKIPPED
                run.status_reason = denied
                run.finished_at = now
                db.add(run)
                await db.commit()
                return run
            if await self._tokens_used_today(db, organization_id, day) >= C.DREAM_ORG_NIGHTLY_TOKENS:
                run.status = STATUS_SKIPPED
                run.status_reason = REASON_BUDGET
                run.finished_at = now
                db.add(run)
                await db.commit()
                return run
            db.add(run)
            await db.commit()
            run_id = str(run.id)

        # The dream itself runs on a fresh session; the runtime owns the row.
        result: Optional[DreamResult] = None
        error: Optional[BaseException] = None
        try:
            async with sessions() as db:
                organization = await db.get(Organization, str(organization_id))
                org_settings = await load_org_settings(db, organization_id)
                result = await self._dream_fn(kind)(
                    db,
                    run_id=run_id,
                    organization=organization,
                    org_settings=org_settings,
                    target_id=str(target_id),
                    now=now,
                )
        except Exception as e:  # never let one unit break the sweep
            logger.exception("dream %s %s/%s failed", kind, organization_id, target_id)
            error = e

        async with sessions() as db:
            run = await db.get(DreamRun, run_id)
            if run is None:
                return None
            run.finished_at = C.utcnow()
            tokens, cost = await self._usage_for(db, run_id)
            # Usage rows are written fire-and-forget after each model call;
            # give them a moment to land so the run is priced (and budgeted).
            for _ in range(3):
                if tokens or error is not None or result is None or not result.tool_calls:
                    break
                await asyncio.sleep(1.0)
                tokens, cost = await self._usage_for(db, run_id)
            run.tokens, run.cost_usd = tokens, cost
            if error is not None or result is None:
                run.status = STATUS_FAILED
                run.status_reason = f"error:{type(error).__name__}"[:64] if error else "error:no_result"
            else:
                allowed = (STATUS_DONE, STATUS_SKIPPED, STATUS_CANCELLED)
                run.status = result.status if result.status in allowed else STATUS_DONE
                run.status_reason = result.reason
                run.inputs_summary = result.inputs_summary or None
                run.tool_calls = result.tool_calls or None
                run.outputs = result.outputs or None
                if run.status in (STATUS_DONE, STATUS_SKIPPED):
                    await self._advance_watermark(db, kind, organization_id, target_id, now)
            db.add(run)
            await db.commit()
            await db.refresh(run)
            logger.info(
                "dream %s %s target=%s status=%s reason=%s tokens=%s",
                kind, organization_id, target_id, run.status, run.status_reason, run.tokens,
            )
            return run

    async def _advance_watermark(self, db, kind, organization_id, target_id, now) -> None:
        if kind == KIND_AGENT:
            from app.models.data_source import DataSource

            ds = await db.get(DataSource, str(target_id))
            if ds is not None:
                ds.agent_dreamed_at = now
                db.add(ds)
            return
        from app.models.membership import Membership

        m = (
            await db.execute(
                select(Membership).where(
                    Membership.organization_id == str(organization_id),
                    Membership.user_id == str(target_id),
                )
            )
        ).scalar_one_or_none()
        if m is not None:
            m.user_dreamed_at = now
            db.add(m)

    async def _usage_for(self, db, run_id: str):
        """Tokens and cost the dream's model calls recorded under this run id."""
        from app.models.llm_usage_record import LLMUsageRecord

        try:
            row = (
                await db.execute(
                    select(
                        func.coalesce(
                            func.sum(LLMUsageRecord.prompt_tokens + LLMUsageRecord.completion_tokens), 0
                        ),
                        func.coalesce(func.sum(LLMUsageRecord.total_cost_usd), 0),
                    ).where(LLMUsageRecord.scope_ref_id == str(run_id))
                )
            ).first()
        except Exception:
            return None, None
        if not row:
            return None, None
        tokens, cost = int(row[0] or 0), float(row[1] or 0)
        return (tokens or None), (cost or None)

    # ------------------------------------------------------------ maintenance

    async def fail_stale_runs(self, db, now: datetime) -> int:
        cutoff = now - timedelta(minutes=C.STALE_RUNNING_MINUTES)
        rows = (
            await db.execute(
                select(DreamRun).where(
                    DreamRun.status.in_((STATUS_RUNNING, STATUS_QUEUED)),
                    DreamRun.started_at < cutoff,
                )
            )
        ).scalars().all()
        for r in rows:
            r.status = STATUS_FAILED
            r.status_reason = REASON_STALE
            r.finished_at = now
            db.add(r)
        if rows:
            await db.commit()
        return len(rows)

    async def cancel_queued_for_org(self, db, organization_id: str, kind: Optional[str] = None) -> int:
        q = select(DreamRun).where(
            DreamRun.organization_id == str(organization_id), DreamRun.status == STATUS_QUEUED,
        )
        if kind:
            q = q.where(DreamRun.kind == kind)
        rows = (await db.execute(q)).scalars().all()
        for r in rows:
            r.status = STATUS_CANCELLED
            r.status_reason = REASON_DISABLED
            r.finished_at = C.utcnow()
            db.add(r)
        if rows:
            await db.commit()
        return len(rows)


dream_runtime = DreamRuntime()


async def overnight_sweep() -> None:
    """Module-level job callable (APScheduler serializes callables by path)."""
    try:
        summary = await dream_runtime.sweep()
        if summary.get("queued") or summary.get("stale"):
            logger.info("overnight_sweep: %s", summary)
    except Exception:
        logger.exception("overnight_sweep failed")
