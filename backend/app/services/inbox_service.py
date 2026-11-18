"""InboxService — the per-user notification inbox.

A ``Notification`` is the *delivery* of something to one recipient; it owns that
user's ``read_at`` / ``dismissed_at``. This replaces the review feed as the
surfaced layer: producers that used to ``review_service.emit(...)`` now call
``notify_agent_managers(...)`` (fan out to the agent's managers) or
``notify_users(...)`` (explicit recipients, e.g. a share).

Reads are scoped purely by ``user_id == current_user`` — delivery already
decided the audience, so there is no agent-permission resolution on read.

Per-user dedup: at most one ACTIVE (``dismissed_at IS NULL``) row per
(user, source, group_key). A repeat refreshes that row (re-surfaces it as
unread, escalates severity, freshens title/body) instead of inserting a new one.
A recently *dismissed* row suppresses re-notification until ``resurface_after_hours``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.notification import (
    Notification,
    SEVERITY_INFO, SEVERITY_RANK,
    SOURCE_REVIEW, SOURCE_SHARE, RESOLUTIONS, RESOLVED_VISIBLE_DAYS,
)
from app.core.permission_resolver import get_user_ids_with_permission

logger = logging.getLogger(__name__)

# The permission that scopes "agent managers" — mirrors review_service visibility.
AGENT_MANAGE_PERMISSION = "manage"


def to_dict(n: Notification) -> Dict[str, Any]:
    return {
        "id": str(n.id),
        "source": n.source,
        "type": n.type,
        "severity": n.severity,
        "title": n.title,
        "body": n.body,
        "link": n.link,
        "subject": n.subject_json or {},
        "actor_user_id": str(n.actor_user_id) if n.actor_user_id else None,
        "actor": _actor(n),
        "read": n.read_at is not None,
        "dismissed": n.dismissed_at is not None,
        "resolved": n.resolved_at is not None,
        "resolution": n.resolution if n.resolved_at is not None else None,
        "resolved_at": n.resolved_at.isoformat() if n.resolved_at else None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


def _actor(n: Notification) -> Optional[Dict[str, Any]]:
    """Who caused it, when the read path loaded the relationship (``actor`` is
    lazy="noload", so an unloaded row just reports no actor)."""
    a = n.__dict__.get("actor")
    if a is None:
        return None
    return {"id": str(a.id), "name": getattr(a, "name", None) or getattr(a, "email", None)}


def _safe_to_dict(n: Notification) -> Dict[str, Any]:
    """Serialize a row, never raising. A single malformed notification must not
    500 the whole inbox (which would strand the user on an empty list under a
    non-zero unread badge). On failure, fall back to a minimal placeholder so the
    row still surfaces and the list stays consistent with count_unread."""
    try:
        return to_dict(n)
    except Exception:  # noqa: BLE001
        logger.exception("inbox: failed to serialize notification %s", getattr(n, "id", "?"))
        nid = getattr(n, "id", None)
        return {
            "id": str(nid) if nid is not None else "",
            "source": getattr(n, "source", "") or "",
            "type": getattr(n, "type", "") or "",
            "severity": getattr(n, "severity", None) or SEVERITY_INFO,
            "title": getattr(n, "title", None) or "Notification",
            "body": None,
            "link": getattr(n, "link", None),
            "subject": {},
            "actor_user_id": None,
            "actor": None,
            "read": getattr(n, "read_at", None) is not None,
            "dismissed": getattr(n, "dismissed_at", None) is not None,
            "resolved": getattr(n, "resolved_at", None) is not None,
            "resolution": None,
            "resolved_at": None,
            "created_at": None,
            "updated_at": None,
        }


class InboxService:
    # ---- write -------------------------------------------------------------
    async def notify_users(
        self,
        db: AsyncSession,
        *,
        organization_id: str,
        user_ids: List[str],
        source: str,
        type: str,
        title: str,
        body: Optional[str] = None,
        severity: str = SEVERITY_INFO,
        link: Optional[str] = None,
        subject: Optional[dict] = None,
        actor_user_id: Optional[str] = None,
        source_id: Optional[str] = None,
        group_key: Optional[str] = None,
        dedup: bool = True,
        resurface_after_hours: Optional[int] = None,
    ) -> List[Notification]:
        """Deliver one notification to each of ``user_ids`` (deduped per user).

        Returns the rows created or refreshed. The actor is never notified about
        their own action. Non-fatal by contract — callers wrap this so a delivery
        failure can't break the event that triggered it.
        """
        now = datetime.utcnow()
        targets = [u for u in {str(u) for u in (user_ids or [])} if u and u != str(actor_user_id or "")]
        out: List[Notification] = []
        for uid in targets:
            existing = None
            if dedup and group_key:
                existing = (await db.execute(
                    select(Notification).where(and_(
                        Notification.user_id == uid,
                        Notification.source == source,
                        Notification.group_key == group_key,
                        Notification.deleted_at.is_(None),
                    )).order_by(Notification.updated_at.desc()).limit(1)
                )).scalar_one_or_none()

            if existing is not None and existing.dismissed_at is None:
                # Active row → refresh in place (re-surface as unread).
                existing.title = title
                existing.body = body
                existing.link = link or existing.link
                existing.subject_json = subject or existing.subject_json
                existing.source_id = source_id or existing.source_id
                if SEVERITY_RANK.get(severity, 9) < SEVERITY_RANK.get(existing.severity, 9):
                    existing.severity = severity  # escalate, never downgrade
                existing.read_at = None
                # The condition is back: a row the team had closed is open again.
                existing.resolved_at = None
                existing.resolution = None
                if isinstance(existing.subject_json, dict) and "handled" in existing.subject_json:
                    existing.subject_json = {k: v for k, v in existing.subject_json.items() if k != "handled"}
                existing.updated_at = now
                out.append(existing)
                continue

            # A row the team closed (resolved_at) that the user then cleared is
            # the end of that event; a notification for a different source (a
            # new review item, e.g. the next schema change) is a new event.
            new_event = (existing is not None and existing.resolved_at is not None
                         and source_id is not None and existing.source_id != str(source_id))
            if existing is not None and existing.dismissed_at is not None and not new_event:
                # Dismissed → respect it unless the resurface window has elapsed.
                if resurface_after_hours is None:
                    continue
                last = existing.updated_at or existing.dismissed_at or existing.created_at
                if last is not None and last >= now - timedelta(hours=resurface_after_hours):
                    continue
                # else fall through and insert a fresh row

            n = Notification(
                organization_id=str(organization_id),
                user_id=uid,
                actor_user_id=str(actor_user_id) if actor_user_id else None,
                source=source,
                type=type,
                severity=severity,
                title=title,
                body=body,
                link=link,
                subject_json=subject or {},
                source_id=source_id,
                group_key=group_key,
            )
            db.add(n)
            out.append(n)

        if out:
            await db.commit()
            for n in out:
                await db.refresh(n)
        return out

    async def notify_agent_managers(
        self,
        db: AsyncSession,
        *,
        organization_id: str,
        data_source_id: Optional[str],
        type: str,
        title: str,
        body: Optional[str] = None,
        severity: str = SEVERITY_INFO,
        link: Optional[str] = None,
        subject: Optional[dict] = None,
        group_key: Optional[str] = None,
        source_id: Optional[str] = None,
        resurface_after_hours: Optional[int] = None,
    ) -> List[Notification]:
        """Fan an agent/ops signal out to the users who manage that agent (or, for
        a global signal with ``data_source_id=None``, the full admins). This is
        the notification analogue of a ``review_item`` — same audience as the old
        review feed, delivered per-user."""
        try:
            user_ids = await get_user_ids_with_permission(
                db, str(organization_id), AGENT_MANAGE_PERMISSION, data_source_id
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("inbox: failed to resolve recipients: %s", e)
            return []
        if not user_ids:
            return []
        return await self.notify_users(
            db, organization_id=str(organization_id), user_ids=user_ids,
            source=SOURCE_REVIEW, type=type, title=title, body=body,
            severity=severity, link=link, subject=subject,
            group_key=group_key, source_id=source_id,
            resurface_after_hours=resurface_after_hours,
        )

    async def notify_share(
        self, db: AsyncSession, *, report, share_type: str,
        user_ids: List[str], actor_user,
    ) -> List[Notification]:
        """Notify-first share delivery: create the durable in-app notification
        for the users a report was shared with. Email (when sent) is a downstream
        channel layered on top — it does not create this record.

        Centralises the share copy so both the share-grant path
        (``set_visibility``) and the explicit notify action call it; the shared
        ``group_key`` dedups across the two so a user is notified once per
        (report, share_type)."""
        if not user_ids:
            return []
        is_conv = share_type == "conversation"
        ntype = "share_conversation" if is_conv else "share_artifact"
        sender = getattr(actor_user, "name", None) or getattr(actor_user, "email", None) or "Someone"
        rtitle = getattr(report, "title", None) or "Untitled"
        if is_conv:
            title = f"{sender} shared a conversation with you"
            body = f'{sender} shared "{rtitle}" with you.'
        else:
            title = f"{sender} shared a dashboard with you"
            body = f'{sender} shared the dashboard "{rtitle}" with you.'
        # Land the recipient on the surface that was actually shared — the same
        # URLs the share modals copy and the share emails carry:
        #   artifact     -> /r/{id}      read-only dashboard viewer
        #   conversation -> /c/{token}   read-only transcript
        # Never /reports/{id}: that is the authoring workspace, which no share
        # grants. set_visibility mints conversation_share_token whenever
        # conversation visibility leaves 'none', so the token is present for
        # every conversation share; the workspace stays a last-resort fallback
        # for a caller that somehow notifies without one (still permission-gated).
        if is_conv:
            token = getattr(report, "conversation_share_token", None)
            link = f"/c/{token}" if token else f"/reports/{report.id}"
        else:
            link = f"/r/{report.id}"
        return await self.notify_users(
            db,
            organization_id=str(report.organization_id),
            user_ids=user_ids,
            source=SOURCE_SHARE,
            type=ntype,
            title=title,
            body=body,
            actor_user_id=str(actor_user.id),
            link=link,
            # `i18n` lets the inbox re-render the copy in the viewer's locale;
            # the English title/body above stay as the fallback.
            subject={"kind": "report", "report_id": str(report.id), "share_type": share_type,
                     "i18n": {"actor": sender, "report": rtitle}},
            group_key=f"share:{share_type}:{report.id}",
        )

    async def notify_dashboard_share(
        self, db: AsyncSession, *, artifact, user_ids: List[str], actor_user,
    ) -> List[Notification]:
        """The same in-app 'shared with you' as notify_share, for one dashboard
        shared on its own (dashboards project, phase 4): it links to the
        dashboard's /d page and dedups per dashboard."""
        if not user_ids:
            return []
        sender = getattr(actor_user, "name", None) or getattr(actor_user, "email", None) or "Someone"
        title_text = getattr(artifact, "title", None) or "Untitled"
        return await self.notify_users(
            db,
            organization_id=str(artifact.organization_id),
            user_ids=user_ids,
            source=SOURCE_SHARE,
            type="share_artifact",
            title=f"{sender} shared a dashboard with you",
            body=f'{sender} shared the dashboard "{title_text}" with you.',
            actor_user_id=str(actor_user.id),
            link=f"/d/{artifact.id}",
            subject={"kind": "dashboard", "artifact_id": str(artifact.id), "share_type": "artifact",
                     "i18n": {"actor": sender, "report": title_text}},
            group_key=f"share:dashboard:{artifact.id}",
        )

    # ---- read --------------------------------------------------------------
    async def list_for_user(
        self, db: AsyncSession, user, organization, *,
        source: Optional[str] = None,
        unread: Optional[bool] = None,
        include_dismissed: bool = False,
        limit: int = 200,
    ) -> Dict[str, Any]:
        clauses = [
            Notification.user_id == str(user.id),
            Notification.organization_id == str(organization.id),
            Notification.deleted_at.is_(None),
        ]
        if not include_dismissed:
            clauses.append(Notification.dismissed_at.is_(None))
            # A handled notification stays (greyed) for a week, then drops out
            # of the list on its own; the row stays for dedup.
            cutoff = datetime.utcnow() - timedelta(days=RESOLVED_VISIBLE_DAYS)
            clauses.append(or_(Notification.resolved_at.is_(None), Notification.resolved_at >= cutoff))
        if source:
            clauses.append(Notification.source == source)
        if unread is True:
            clauses.append(Notification.read_at.is_(None))
        elif unread is False:
            clauses.append(Notification.read_at.isnot(None))

        rows = (await db.execute(
            select(Notification).where(and_(*clauses))
            .options(selectinload(Notification.actor))
            .order_by(Notification.created_at.desc()).limit(limit)
        )).scalars().all()
        # Newest-first (recency DESC), with severity only as a tiebreaker for
        # rows sharing a timestamp. A notification inbox reads chronologically;
        # ranking by severity first surfaced a stale warning above a fresh run.
        #
        # This whole read path must never 500: the unread badge comes from
        # count_unread (a pure func.count that can't fail), so if listing throws
        # on one malformed row the user is left with a red badge over an empty
        # "all caught up" inbox (the frontend swallows the error). Keep the two
        # consistent by being defensive end-to-end:
        #   * _ts treats a missing/invalid timestamp as the epoch — datetime.min
        #     and out-of-range/aware datetimes can otherwise raise on .timestamp()
        #     ("year 0 is out of range").
        #   * the sort itself is wrapped so a surprise key error falls back to the
        #     DB's created_at ordering (already DESC) rather than failing.
        #   * each row is serialized independently so one poison row degrades to a
        #     safe placeholder instead of blanking the entire list.
        def _ts(dt):
            try:
                return dt.timestamp() if dt else 0.0
            except Exception:  # noqa: BLE001 — any unrepresentable datetime
                return 0.0
        try:
            # Handled (resolved) rows stay visible but sink below open ones.
            rows.sort(key=lambda r: (
                r.resolved_at is not None,
                -_ts(r.created_at),
                SEVERITY_RANK.get(r.severity, 9),
            ))
        except Exception:  # noqa: BLE001
            logger.exception("inbox: failed to sort notifications; using DB order")
        unread_n = sum(1 for r in rows if r.read_at is None)
        return {
            "items": [_safe_to_dict(r) for r in rows],
            "total": len(rows),
            "unread": unread_n,
        }

    # ---- source sync -------------------------------------------------------
    async def resolve_for_sources(
        self, db: AsyncSession, source_ids: List[str], resolution: str, *,
        how: Optional[str] = None, by_user_id: Optional[str] = None,
    ) -> int:
        """Mark every recipient's notification for these source rows (e.g. the
        ReviewItems that just closed) as resolved and read.

        Joins the caller's transaction (flush, no commit) so the source's state
        change and its notifications land together. A row already resolved
        keeps its first resolution.
        """
        if resolution not in RESOLUTIONS:
            raise ValueError(f"unknown resolution: {resolution}")
        ids = [str(s) for s in (source_ids or []) if s]
        if not ids:
            return 0
        rows = (await db.execute(
            select(Notification).where(and_(
                Notification.source_id.in_(ids),
                Notification.deleted_at.is_(None),
                Notification.resolved_at.is_(None),
            ))
        )).scalars().all()
        now = datetime.utcnow()
        # Who handled it and how — what the inbox shows next to "Handled".
        by_name = None
        if by_user_id:
            from app.models.user import User
            u = await db.get(User, str(by_user_id))
            by_name = (getattr(u, "name", None) or getattr(u, "email", None)) if u else None
        info = {"how": how, "by_user_id": str(by_user_id) if by_user_id else None,
                "by_name": by_name, "at": now.isoformat()}
        for n in rows:
            n.resolved_at = now
            n.resolution = resolution
            n.read_at = n.read_at or now
            # Fresh dict so the JSON column is marked dirty.
            n.subject_json = {**(n.subject_json or {}), "handled": info}
        if rows:
            await db.flush()
        return len(rows)

    async def dismiss_resolved(self, db: AsyncSession, user, organization) -> int:
        """Clear every handled notification from this user's inbox at once
        ("Clear handled"). Personal, like the per-row ✕: other recipients and
        the Review item are untouched. Scoped to one organization: a person in
        several has a separate inbox in each."""
        rows = (await db.execute(
            select(Notification).where(and_(
                Notification.user_id == str(user.id),
                Notification.organization_id == str(organization.id),
                Notification.deleted_at.is_(None),
                Notification.dismissed_at.is_(None),
                Notification.resolved_at.isnot(None),
            ))
        )).scalars().all()
        now = datetime.utcnow()
        for n in rows:
            n.dismissed_at = now
            n.read_at = n.read_at or now
        if rows:
            await db.commit()
        return len(rows)

    async def count_unread(self, db: AsyncSession, user, organization) -> Dict[str, int]:
        n = (await db.execute(
            select(func.count(Notification.id)).where(and_(
                Notification.user_id == str(user.id),
                Notification.organization_id == str(organization.id),
                Notification.deleted_at.is_(None),
                Notification.dismissed_at.is_(None),
                Notification.read_at.is_(None),
            ))
        )).scalar_one()
        return {"unread": int(n or 0)}

    async def _get_own(self, db, user, organization, notification_id) -> Optional[Notification]:
        # A notification belongs to one recipient in one organization; a
        # person in several organizations has a separate inbox in each.
        return (await db.execute(
            select(Notification).where(and_(
                Notification.id == str(notification_id),
                Notification.user_id == str(user.id),
                Notification.organization_id == str(organization.id),
                Notification.deleted_at.is_(None),
            ))
        )).scalar_one_or_none()

    async def mark_read(self, db, user, organization, notification_id, read: bool = True) -> Optional[Notification]:
        n = await self._get_own(db, user, organization, notification_id)
        if n is None:
            return None
        n.read_at = datetime.utcnow() if read else None
        await db.commit(); await db.refresh(n)
        return n

    async def mark_all_read(self, db, user, organization, source: Optional[str] = None) -> int:
        clauses = [
            Notification.user_id == str(user.id),
            Notification.organization_id == str(organization.id),
            Notification.deleted_at.is_(None),
            Notification.dismissed_at.is_(None),
            Notification.read_at.is_(None),
        ]
        if source:
            clauses.append(Notification.source == source)
        rows = (await db.execute(select(Notification).where(and_(*clauses)))).scalars().all()
        now = datetime.utcnow()
        for n in rows:
            n.read_at = now
        if rows:
            await db.commit()
        return len(rows)

    async def dismiss(self, db, user, organization, notification_id) -> Optional[Notification]:
        n = await self._get_own(db, user, organization, notification_id)
        if n is None:
            return None
        n.dismissed_at = datetime.utcnow()
        if n.read_at is None:
            n.read_at = n.dismissed_at
        await db.commit(); await db.refresh(n)
        return n


inbox_service = InboxService()
