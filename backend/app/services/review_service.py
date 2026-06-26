"""ReviewService — the admin Review feed.

Items are scoped to an agent (``data_source_id``) or global (null). Visibility is
derived from manage-permission on the agent (full admins see everything). State
is shared: anyone with access reads/dismisses/resolves. Dedup is by
(org, agent, type, group_key) over ACTIVE items — repeats bump ``group_count``.

Resolution actions (à la carte) fire the *existing* agent workflows scoped to the
item's agent: ``run_eval`` (eval-only), ``run_training`` (force-train), plus
``dismiss``. Instruction suggestions are reviewed inline in the explorer (the
feed just deep-links), and auto-resolve when the pending change clears.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review_item import (
    ReviewItem,
    ACTIVE_STATUSES, SEVERITY_RANK,
    STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_RESOLVED, STATUS_DISMISSED, STATUS_SNOOZED,
    SEVERITY_INFO, DISPOSITION_NOTIFY,
    TYPE_INSTRUCTION_SUGGESTION, TYPE_SCHEMA_CHANGED, TYPE_SLOW_QUERY,
    TYPE_LOW_CONFIDENCE, TYPE_QUERY_ERROR,
)
from app.core.permission_resolver import get_ds_ids_with_permission

logger = logging.getLogger(__name__)


# Per-type action menu. Each action: id, label, kind, primary?, optional capability.
# `kind` drives the backend dispatch; `nav` actions are handled by the frontend.
def actions_for(item: ReviewItem) -> List[Dict[str, Any]]:
    t = item.type
    menu: List[Dict[str, Any]] = []
    if t == TYPE_INSTRUCTION_SUGGESTION:
        menu = [
            {"id": "review", "label": "Review", "kind": "nav", "primary": True},
            {"id": "run_eval", "label": "Run eval", "kind": "run_eval"},
        ]
    elif t == TYPE_SCHEMA_CHANGED:
        menu = [
            {"id": "run_training", "label": "Run training", "kind": "run_training", "primary": True},
            {"id": "run_eval", "label": "Run eval", "kind": "run_eval"},
        ]
    elif t == TYPE_LOW_CONFIDENCE:
        menu = [
            {"id": "run_training", "label": "Run training", "kind": "run_training", "primary": True},
            {"id": "run_eval", "label": "Run eval", "kind": "run_eval"},
        ]
    elif t == TYPE_SLOW_QUERY:
        menu = [
            {"id": "run_training", "label": "Add guardrail (train)", "kind": "run_training", "primary": True},
        ]
    elif t == TYPE_QUERY_ERROR:
        menu = [
            {"id": "run_training", "label": "Run training", "kind": "run_training", "primary": True},
            {"id": "run_eval", "label": "Run eval", "kind": "run_eval"},
        ]
    # Dismiss is always available.
    menu.append({"id": "dismiss", "label": "Dismiss", "kind": "dismiss"})
    return menu


def to_dict(item: ReviewItem, *, agent_has_evals: Optional[bool] = None) -> Dict[str, Any]:
    return {
        "id": str(item.id),
        "type": item.type,
        "severity": item.severity,
        "status": item.status,
        "disposition": item.disposition,
        "title": item.title,
        "why": item.why,
        "subject": item.subject_json or {},
        "group_count": item.group_count,
        "agent_id": str(item.data_source_id) if item.data_source_id else None,
        # Whether the item's agent has any active eval to run/train against —
        # drives whether run_eval/run_training are offered. None = N/A (global).
        "agent_has_evals": agent_has_evals,
        "resolution": item.resolution_json,
        "spawned": item.spawned_json or [],
        "source_run_id": item.source_run_id,
        "build_id": item.build_id,
        "read": item.read_at is not None,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "last_seen_at": (item.last_seen_at or item.created_at).isoformat() if (item.last_seen_at or item.created_at) else None,
        "actions": actions_for(item),
    }


async def _agents_with_active_evals(db: AsyncSession, organization_id: str) -> tuple[bool, set[str]]:
    """(any_auto_eval, {agent_id, …}) over active eval cases in the org. An
    Auto/empty-scope case applies to *every* agent (like a global instruction),
    so it's tracked separately."""
    from app.models.eval import TestCase, TestSuite, TEST_CASE_STATUS_ACTIVE
    rows = (await db.execute(
        select(TestCase.data_source_ids_json)
        .join(TestSuite, TestSuite.id == TestCase.suite_id)
        .where(and_(
            TestSuite.organization_id == str(organization_id),
            TestCase.deleted_at.is_(None),
            TestCase.status == TEST_CASE_STATUS_ACTIVE,
        ))
    )).all()
    any_auto = False
    agents: set[str] = set()
    for (ds_ids_json,) in rows:
        ds_list = ds_ids_json or []
        if isinstance(ds_list, list) and len(ds_list) > 0:
            for x in ds_list:
                agents.add(str(x))
        else:
            any_auto = True
    return any_auto, agents


class ReviewService:
    # ---- producer ----------------------------------------------------------
    async def emit(
        self,
        db: AsyncSession,
        *,
        organization_id: str,
        type: str,
        title: str,
        data_source_id: Optional[str] = None,
        severity: str = SEVERITY_INFO,
        why: Optional[str] = None,
        subject: Optional[dict] = None,
        group_key: Optional[str] = None,
        source_run_id: Optional[str] = None,
        build_id: Optional[str] = None,
        caused_by_id: Optional[str] = None,
        disposition: str = DISPOSITION_NOTIFY,
        occurrences: Optional[int] = None,
        respect_dismissal: bool = False,
        resurface_after_hours: Optional[int] = None,
    ) -> Optional[ReviewItem]:
        """Create or bump a review item. At most one ACTIVE item per
        (org, agent, type, group_key) — repeats bump group_count + last_seen.

        ``occurrences`` lets a sweep SET the absolute count (recomputed over a
        window) instead of incrementing by one.

        ``respect_dismissal`` makes a prior dismissal stick: if the dedup slot
        has no active item but a recent DISMISSED one, don't resurrect it (so a
        re-scan/cron doesn't immediately re-create what a human just cleared).
        ``resurface_after_hours`` lets it come back after that long (None =
        suppress until the condition's group_key changes). Returns ``None`` when
        emission is suppressed by a dismissal."""
        now = datetime.utcnow()
        if group_key:
            existing = (
                await db.execute(
                    select(ReviewItem).where(and_(
                        ReviewItem.organization_id == organization_id,
                        ReviewItem.data_source_id == data_source_id,
                        ReviewItem.type == type,
                        ReviewItem.group_key == group_key,
                        ReviewItem.status.in_(list(ACTIVE_STATUSES)),
                        ReviewItem.deleted_at.is_(None),
                    )).limit(1)
                )
            ).scalar_one_or_none()
            if existing is None and respect_dismissal:
                dismissed = (
                    await db.execute(
                        select(ReviewItem).where(and_(
                            ReviewItem.organization_id == organization_id,
                            ReviewItem.data_source_id == data_source_id,
                            ReviewItem.type == type,
                            ReviewItem.group_key == group_key,
                            ReviewItem.status == STATUS_DISMISSED,
                            ReviewItem.deleted_at.is_(None),
                        )).order_by(ReviewItem.updated_at.desc()).limit(1)
                    )
                ).scalar_one_or_none()
                if dismissed is not None:
                    if resurface_after_hours is None:
                        return None
                    last = dismissed.updated_at or dismissed.last_seen_at or dismissed.created_at
                    if last is not None and last >= now - timedelta(hours=resurface_after_hours):
                        return None
            if existing is not None:
                existing.group_count = occurrences if occurrences is not None else (existing.group_count or 1) + 1
                existing.last_seen_at = now
                existing.title = title         # keep title/why fresh
                existing.why = why
                if SEVERITY_RANK.get(severity, 9) < SEVERITY_RANK.get(existing.severity, 9):
                    existing.severity = severity   # escalate, never downgrade
                # a snoozed item that recurs stays snoozed until its timer; an
                # open/in_progress one just ticks up.
                await db.commit()
                await db.refresh(existing)
                await self._fanout_notification(db, existing)
                return existing

        item = ReviewItem(
            organization_id=organization_id,
            data_source_id=data_source_id,
            type=type,
            severity=severity,
            status=STATUS_OPEN,
            disposition=disposition,
            title=title,
            why=why,
            subject_json=subject or {},
            group_key=group_key,
            group_count=occurrences if occurrences is not None else 1,
            source_run_id=source_run_id,
            build_id=build_id,
            caused_by_id=caused_by_id,
            last_seen_at=now,
        )
        db.add(item)
        await db.commit()
        await db.refresh(item)
        await self._fanout_notification(db, item)
        return item

    async def _fanout_notification(self, db, item) -> None:
        """Deliver a per-user notification for a freshly created/bumped item.

        ``ReviewItem`` is the internal dedup/state ledger; the *surface* is the
        per-user inbox. Recipients are the item's agent managers (or full admins
        for a global item) — the same audience the review feed used. Non-fatal:
        a delivery failure must never break emission.
        """
        if item is None or item.disposition != DISPOSITION_NOTIFY:
            return
        try:
            from app.services.inbox_service import inbox_service
            ds_id = str(item.data_source_id) if item.data_source_id else None
            await inbox_service.notify_agent_managers(
                db,
                organization_id=str(item.organization_id),
                data_source_id=ds_id,
                type=item.type,
                title=item.title,
                body=item.why,
                severity=item.severity,
                link=(f"/agents/{ds_id}" if ds_id else None),
                subject={
                    "kind": "review_item",
                    "review_item_id": str(item.id),
                    "review_type": item.type,
                    "data_source_id": ds_id,
                },
                group_key=item.group_key,
                source_id=str(item.id),
                resurface_after_hours=24 * 7,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("review: notification fan-out failed: %s", e)

    async def resolve_open_for(
        self, db: AsyncSession, *, organization_id: str, type: str,
        data_source_id: Optional[str], group_key: str, verified: bool = True,
    ) -> int:
        """Auto-resolve active items matching (type, agent, group_key) — used when
        a producer re-verifies the underlying condition has cleared."""
        rows = (await db.execute(
            select(ReviewItem).where(and_(
                ReviewItem.organization_id == organization_id,
                ReviewItem.data_source_id == data_source_id,
                ReviewItem.type == type,
                ReviewItem.group_key == group_key,
                ReviewItem.status.in_(list(ACTIVE_STATUSES)),
                ReviewItem.deleted_at.is_(None),
            ))
        )).scalars().all()
        for it in rows:
            it.status = STATUS_RESOLVED
            if verified:
                it.verified_at = datetime.utcnow()
        if rows:
            await db.commit()
        return len(rows)

    async def resolve_for_instruction(self, db: AsyncSession, *, organization_id: str, instruction_id: str) -> int:
        """Auto-resolve any active instruction_suggestion items for an instruction
        (across all agents) — called when the admin has handled the change
        (accepted/rejected) so the item doesn't linger."""
        from app.models.review_item import TYPE_INSTRUCTION_SUGGESTION
        rows = (await db.execute(
            select(ReviewItem).where(and_(
                ReviewItem.organization_id == organization_id,
                ReviewItem.type == TYPE_INSTRUCTION_SUGGESTION,
                ReviewItem.group_key == f"instr:{instruction_id}",
                ReviewItem.status.in_(list(ACTIVE_STATUSES)),
                ReviewItem.deleted_at.is_(None),
            ))
        )).scalars().all()
        for it in rows:
            it.status = STATUS_RESOLVED
            it.verified_at = datetime.utcnow()
        if rows:
            await db.commit()
        return len(rows)

    # ---- visibility --------------------------------------------------------
    async def _visible(self, db, organization, user) -> tuple[bool, list[str]]:
        return await get_ds_ids_with_permission(
            db, str(user.id), str(organization.id), "manage"
        )

    async def list_items(
        self, db: AsyncSession, organization, user, *,
        agent_id: Optional[str] = None,
        statuses: Optional[List[str]] = None,
        types: Optional[List[str]] = None,
        severities: Optional[List[str]] = None,
        search: Optional[str] = None,
        limit: int = 200,
    ) -> Dict[str, Any]:
        is_admin, ds_ids = await self._visible(db, organization, user)
        if not is_admin and not ds_ids:
            return {"items": [], "total": 0, "unread": 0}

        clauses = [
            ReviewItem.organization_id == str(organization.id),
            ReviewItem.deleted_at.is_(None),
        ]
        # Visibility: full admins see all (incl. global). Others see only agents
        # they manage (no global items).
        if not is_admin:
            clauses.append(ReviewItem.data_source_id.in_(ds_ids))
        if agent_id:
            if not is_admin and agent_id not in ds_ids:
                return {"items": [], "total": 0, "unread": 0}
            clauses.append(ReviewItem.data_source_id == agent_id)
        clauses.append(ReviewItem.status.in_(statuses or [STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_SNOOZED]))
        if types:
            clauses.append(ReviewItem.type.in_(types))
        if severities:
            clauses.append(ReviewItem.severity.in_(severities))
        if search:
            like = f"%{search}%"
            clauses.append(or_(ReviewItem.title.ilike(like), ReviewItem.why.ilike(like)))

        rows = (await db.execute(
            select(ReviewItem).where(and_(*clauses)).limit(limit)
        )).scalars().all()
        # Sort by severity then recency (in Python — small N).
        rows.sort(key=lambda r: (
            SEVERITY_RANK.get(r.severity, 9),
            -((r.last_seen_at or r.created_at or datetime.min).timestamp()),
        ))
        unread = sum(1 for r in rows if r.read_at is None and r.status in (STATUS_OPEN, STATUS_IN_PROGRESS))
        # Gate run_eval/run_training on whether the agent actually has evals.
        any_auto, agents_with_evals = await _agents_with_active_evals(db, str(organization.id))

        def _has_evals(r: ReviewItem) -> Optional[bool]:
            if r.data_source_id is None:
                return None  # global item — eval actions don't apply
            return any_auto or str(r.data_source_id) in agents_with_evals

        return {
            "items": [to_dict(r, agent_has_evals=_has_evals(r)) for r in rows],
            "total": len(rows), "unread": unread,
        }

    async def count_open(self, db, organization, user) -> Dict[str, int]:
        res = await self.list_items(db, organization, user)
        return {"open": res["total"], "unread": res["unread"]}

    async def _get_visible_item(self, db, organization, user, item_id) -> Optional[ReviewItem]:
        item = (await db.execute(
            select(ReviewItem).where(and_(
                ReviewItem.id == item_id,
                ReviewItem.organization_id == str(organization.id),
                ReviewItem.deleted_at.is_(None),
            ))
        )).scalar_one_or_none()
        if item is None:
            return None
        is_admin, ds_ids = await self._visible(db, organization, user)
        if is_admin:
            return item
        if item.data_source_id and str(item.data_source_id) in ds_ids:
            return item
        return None  # global items + unmanaged agents: hidden from non-admins

    # ---- triage ------------------------------------------------------------
    async def mark_read(self, db, organization, user, item_id, read: bool = True) -> Optional[ReviewItem]:
        item = await self._get_visible_item(db, organization, user, item_id)
        if item is None:
            return None
        item.read_at = datetime.utcnow() if read else None
        item.read_by_user_id = str(user.id) if read else None
        await db.commit(); await db.refresh(item)
        return item

    async def mark_all_read(self, db, organization, user, agent_id: Optional[str] = None) -> int:
        res = await self.list_items(db, organization, user, agent_id=agent_id)
        ids = [i["id"] for i in res["items"] if not i["read"]]
        n = 0
        for iid in ids:
            if await self.mark_read(db, organization, user, iid, True):
                n += 1
        return n

    async def dismiss(self, db, organization, user, item_id) -> Optional[ReviewItem]:
        item = await self._get_visible_item(db, organization, user, item_id)
        if item is None:
            return None
        item.status = STATUS_DISMISSED
        item.dismissed_by_user_id = str(user.id)
        await db.commit(); await db.refresh(item)
        return item

    async def resolve(self, db, organization, user, item_id, action_id: str, params: Optional[dict] = None) -> Dict[str, Any]:
        item = await self._get_visible_item(db, organization, user, item_id)
        if item is None:
            return {"ok": False, "error": "not_found"}

        action = next((a for a in actions_for(item) if a["id"] == action_id), None)
        if action is None:
            return {"ok": False, "error": "unknown_action"}
        kind = action["kind"]

        if kind == "dismiss":
            await self.dismiss(db, organization, user, item_id)
            return {"ok": True, "status": STATUS_DISMISSED}

        if kind == "nav":
            # Frontend handles navigation (e.g. open the diff review). No-op here.
            return {"ok": True, "nav": True}

        if kind in ("run_eval", "run_training"):
            if not item.data_source_id:
                return {"ok": False, "error": "no_agent"}
            # Pre-flight: both run_eval and run_training measure against the
            # agent's eval suite (training is eval-driven), so with no active
            # evals the workflow is a guaranteed no-op. Reject synchronously
            # with a clear reason instead of spinning up a run that resolves to
            # "no_evals" and silently marks the item done.
            from app.services.agent_reliability_service import AgentReliabilityService
            case_ids = await AgentReliabilityService().list_agent_eval_case_ids(
                db, str(organization.id), str(item.data_source_id)
            )
            if not case_ids:
                return {
                    "ok": False, "error": "no_evals",
                    "message": "No active evals are scoped to this agent. Add a test case before running an eval or training.",
                }
            # Fire the existing agent workflow in the background, scoped to THIS
            # agent only (even if the suggestion is shared across agents).
            train_override = "auto" if kind == "run_training" else "off"
            # Per-event focus brief (training only) seeded into the run.
            brief = None
            if kind == "run_training":
                try:
                    from app.services.review_producers import build_training_brief
                    brief = build_training_brief(item)
                except Exception:
                    brief = None
            spawned = list(item.spawned_json or [])
            spawned.append({"kind": kind, "status": "running", "started_at": datetime.utcnow().isoformat()})
            item.spawned_json = spawned
            item.status = STATUS_IN_PROGRESS
            item.read_at = item.read_at or datetime.utcnow()
            item.read_by_user_id = item.read_by_user_id or str(user.id)
            await db.commit()
            self._launch_workflow(
                organization_id=str(organization.id),
                data_source_id=str(item.data_source_id),
                item_id=str(item.id),
                kind=kind,
                train_override=train_override,
                user_id=str(user.id),
                brief=brief,
            )
            return {"ok": True, "status": STATUS_IN_PROGRESS, "spawned": kind}

        return {"ok": False, "error": "unsupported"}

    # ---- background workflow runner ---------------------------------------
    def _launch_workflow(self, *, organization_id, data_source_id, item_id, kind, train_override, user_id, brief=None):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("review: no running loop; cannot launch %s", kind)
            return
        asyncio.create_task(self._run_workflow(organization_id, data_source_id, item_id, kind, train_override, user_id, brief))

    async def _run_workflow(self, organization_id, data_source_id, item_id, kind, train_override, user_id, brief=None):
        from app.settings.database import create_async_session_factory
        from app.models.organization import Organization
        from app.models.data_source import DataSource
        from app.models.user import User
        from app.services.agent_reliability_service import AgentReliabilityService
        from app.models.agent_automation_run import TRIGGER_MANUAL
        factory = create_async_session_factory()
        outcome_status = "done"
        run_id = None
        try:
            async with factory() as db:
                org = await db.get(Organization, str(organization_id))
                ds = await db.get(DataSource, str(data_source_id))
                user = await db.get(User, str(user_id))
                if org and ds:
                    run = await AgentReliabilityService().run_automation(
                        db, org, ds, TRIGGER_MANUAL, user=user, train_override=train_override,
                        brief=brief, changed_hint=brief or f"manual {kind} from Review feed",
                    )
                    run_id = str(run.id) if run else None
                    outcome_status = getattr(run, "status", "done")
        except Exception as e:  # noqa: BLE001
            logger.exception("review workflow %s failed: %s", kind, e)
            outcome_status = "error"
        # Record the outcome back on the item (own fresh session).
        try:
            async with factory() as db:
                item = await db.get(ReviewItem, str(item_id))
                if item is not None:
                    # Rebuild with fresh dicts so the JSON column is marked dirty.
                    spawned = []
                    for s in (item.spawned_json or []):
                        s = dict(s)
                        if s.get("kind") == kind and s.get("status") == "running":
                            s["status"] = outcome_status
                            s["run_id"] = run_id
                            s["finished_at"] = datetime.utcnow().isoformat()
                        spawned.append(s)
                    item.spawned_json = spawned
                    item.source_run_id = run_id or item.source_run_id
                    item.resolution_json = {
                        "action": kind, "ref": run_id, "by": user_id,
                        "at": datetime.utcnow().isoformat(), "outcome": outcome_status,
                    }
                    # Only a productive outcome resolves the item. A no-op or
                    # failure (no_evals / skipped / error / gave_up) reopens it so
                    # it stays actionable instead of showing a misleading green
                    # "Resolved". The loop's own outputs surface as their own items.
                    if outcome_status in ("passed", "passed_pending"):
                        item.status = STATUS_RESOLVED
                        item.resolved_by_user_id = user_id
                    else:
                        item.status = STATUS_OPEN
                    await db.commit()
        except Exception as e:  # noqa: BLE001
            logger.exception("review workflow %s: failed to record outcome: %s", kind, e)


review_service = ReviewService()
