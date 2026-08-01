"""Per-worker live feed of report activity for list badges.

One background watcher task per uvicorn worker, running only while at least
one client is subscribed. Each tick it opens its own short-lived DB session,
derives the org-level activity facts for a bounded candidate set (reports
with live completions now or recent last_activity_at churn), diffs against
the previous tick, and fans the changes out to that worker's subscribers.

The DB is the bus: every worker polls the same database, so a run executing
on worker 2 is seen by a subscriber connected to worker 0 — the property the
old report-scoped in-process WebSocket broadcast never had. Cost is a few
cheap indexed queries per tick per active org, independent of how many
clients are connected; zero when nobody is subscribed.

Events carry viewer-independent facts only. The client composes the per-user
view: `unread` (an event for a report you're not currently reading), and
`awaiting_user_id` (a pending tool confirmation counts as "waiting for you"
only for its target user — everyone else renders the running state the
in-progress completion already implies). Snapshot truth on connect/reconnect
comes from GET /reports/activity — deltas here, snapshot there.
"""
import asyncio
import contextlib
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

logger = logging.getLogger(__name__)

TICK_SECONDS = 2.0
# Look-back margin when picking up reports whose last_activity_at moved; wide
# enough to absorb tick jitter and cross-worker clock skew on the DB writes.
CHURN_MARGIN = timedelta(seconds=10)
# Bound per-tick candidate sets; a busier org degrades to snapshot-on-nav for
# the overflow instead of unbounded queries.
MAX_CANDIDATES = 200
QUEUE_MAXSIZE = 256


class _Subscriber:
    __slots__ = ('queue', 'user_id')

    def __init__(self, user_id: str):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.user_id = user_id


class ReportActivityHub:
    def __init__(self):
        # org_id -> list of subscribers on THIS worker
        self._subs: dict[str, list[_Subscriber]] = {}
        # org_id -> report_id -> signature of the last event we emitted
        self._last_sig: dict[str, dict[str, tuple]] = {}
        # org_id -> report ids that were live (running/queued) last tick, so a
        # run finishing (leaving the live set) still emits its terminal event.
        self._prev_live: dict[str, set[str]] = {}
        self._watcher: asyncio.Task | None = None
        self._last_tick_at: datetime = datetime.utcnow()

    # ── subscription ────────────────────────────────────────────────────────
    def subscribe(self, org_id: str, user_id: str) -> _Subscriber:
        sub = _Subscriber(user_id)
        self._subs.setdefault(str(org_id), []).append(sub)
        if self._watcher is None or self._watcher.done():
            self._watcher = asyncio.create_task(self._run())
        return sub

    def unsubscribe(self, org_id: str, sub: _Subscriber) -> None:
        org_id = str(org_id)
        subs = self._subs.get(org_id, [])
        with contextlib.suppress(ValueError):
            subs.remove(sub)
        if not subs:
            self._subs.pop(org_id, None)
            self._last_sig.pop(org_id, None)
            self._prev_live.pop(org_id, None)

    @property
    def has_subscribers(self) -> bool:
        return any(self._subs.values())

    # ── watcher ─────────────────────────────────────────────────────────────
    async def _run(self):
        logger.info("report-activity watcher started")
        try:
            while self.has_subscribers:
                started = datetime.utcnow()
                for org_id in list(self._subs.keys()):
                    try:
                        await self._tick(org_id)
                    except Exception:
                        logger.exception("report-activity tick failed for org %s", org_id)
                self._last_tick_at = started
                await asyncio.sleep(TICK_SECONDS)
        finally:
            logger.info("report-activity watcher stopped")
            self._watcher = None

    async def _tick(self, org_id: str) -> None:
        """One diff pass for one org. Opens its own session (never the pool
        slot of a request — the watcher outlives every request)."""
        from app.settings.database import create_async_session_factory
        from app.models.report import Report
        from app.models.completion import Completion
        from app.services.report_service import ReportService

        session_factory = create_async_session_factory()
        async with session_factory() as db:
            # Candidates: live completions now, live last tick, or recent
            # last_activity_at churn (covers error finalize, new messages,
            # viewed-elsewhere is client-side).
            since = self._last_tick_at - CHURN_MARGIN
            live_rows = await db.execute(
                select(Completion.report_id)
                .join(Report, Report.id == Completion.report_id)
                .filter(
                    Report.organization_id == org_id,
                    Completion.status.in_(['in_progress', 'queued']),
                    Completion.deleted_at.is_(None),
                )
            )
            live_now = {str(r) for (r,) in live_rows.all()}
            churn_rows = await db.execute(
                select(Report.id).filter(
                    Report.organization_id == org_id,
                    Report.last_activity_at > since,
                    Report.deleted_at.is_(None),
                )
            )
            churned = {str(r) for (r,) in churn_rows.all()}
            candidates = list(live_now | churned | self._prev_live.get(org_id, set()))[:MAX_CANDIDATES]
            self._prev_live[org_id] = live_now
            if not candidates:
                return

            svc = ReportService()
            sets = await svc.derive_activity_sets(db, candidates)
            meta_rows = await db.execute(
                select(Report.id, Report.last_activity_at, Report.created_at).filter(
                    Report.id.in_(candidates),
                    Report.organization_id == org_id,
                    Report.deleted_at.is_(None),
                )
            )
            events = []
            sig_cache = self._last_sig.setdefault(org_id, {})
            for rid, last_activity_at, created_at in meta_rows.all():
                rid = str(rid)
                conf_users = sets['confirmation_user_ids'].get(rid, set())
                if rid in sets['clarify_ids']:
                    state = 'awaiting_user'
                elif conf_users:
                    state = 'awaiting_user'
                elif rid in sets['running_ids']:
                    state = 'running'
                elif rid in sets['queued_ids']:
                    state = 'queued'
                else:
                    state = 'idle'
                la = (last_activity_at or created_at)
                sig = (state, rid in sets['error_ids'], la.isoformat() if la else None,
                       tuple(sorted(conf_users)))
                if sig_cache.get(rid) == sig:
                    continue
                sig_cache[rid] = sig
                events.append({
                    'report_id': rid,
                    'state': state,
                    'error': rid in sets['error_ids'],
                    'last_activity_at': sig[2],
                    # Set only when the pause belongs to specific users
                    # (tool confirmation); clarify is answerable by anyone.
                    'awaiting_user_ids': sorted(conf_users) if (conf_users and rid not in sets['clarify_ids']) else None,
                })

        if not events:
            return
        for sub in list(self._subs.get(org_id, [])):
            for ev in events:
                try:
                    sub.queue.put_nowait(ev)
                except asyncio.QueueFull:
                    # Slow consumer: drop — the next snapshot (reconnect or
                    # navigation) restores truth.
                    pass


report_activity_hub = ReportActivityHub()


def format_activity_event(ev: dict) -> str:
    return f"event: report.activity\ndata: {json.dumps(ev)}\n\n"
