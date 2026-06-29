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

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    Notification,
    SEVERITY_INFO, SEVERITY_RANK,
    SOURCE_REVIEW, SOURCE_SHARE,
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
        "read": n.read_at is not None,
        "dismissed": n.dismissed_at is not None,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
    }


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
            "read": getattr(n, "read_at", None) is not None,
            "dismissed": getattr(n, "dismissed_at", None) is not None,
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
                existing.updated_at = now
                out.append(existing)
                continue

            if existing is not None and existing.dismissed_at is not None:
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
        return await self.notify_users(
            db,
            organization_id=str(report.organization_id),
            user_ids=user_ids,
            source=SOURCE_SHARE,
            type=ntype,
            title=title,
            body=body,
            actor_user_id=str(actor_user.id),
            link=f"/reports/{report.id}",
            subject={"kind": "report", "report_id": str(report.id), "share_type": share_type},
            group_key=f"share:{share_type}:{report.id}",
        )

    # ---- read --------------------------------------------------------------
    async def list_for_user(
        self, db: AsyncSession, user, *,
        source: Optional[str] = None,
        unread: Optional[bool] = None,
        include_dismissed: bool = False,
        limit: int = 200,
    ) -> Dict[str, Any]:
        clauses = [
            Notification.user_id == str(user.id),
            Notification.deleted_at.is_(None),
        ]
        if not include_dismissed:
            clauses.append(Notification.dismissed_at.is_(None))
        if source:
            clauses.append(Notification.source == source)
        if unread is True:
            clauses.append(Notification.read_at.is_(None))
        elif unread is False:
            clauses.append(Notification.read_at.isnot(None))

        rows = (await db.execute(
            select(Notification).where(and_(*clauses))
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
            rows.sort(key=lambda r: (
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

    async def count_unread(self, db: AsyncSession, user) -> Dict[str, int]:
        n = (await db.execute(
            select(func.count(Notification.id)).where(and_(
                Notification.user_id == str(user.id),
                Notification.deleted_at.is_(None),
                Notification.dismissed_at.is_(None),
                Notification.read_at.is_(None),
            ))
        )).scalar_one()
        return {"unread": int(n or 0)}

    async def _get_own(self, db, user, notification_id) -> Optional[Notification]:
        return (await db.execute(
            select(Notification).where(and_(
                Notification.id == str(notification_id),
                Notification.user_id == str(user.id),
                Notification.deleted_at.is_(None),
            ))
        )).scalar_one_or_none()

    async def mark_read(self, db, user, notification_id, read: bool = True) -> Optional[Notification]:
        n = await self._get_own(db, user, notification_id)
        if n is None:
            return None
        n.read_at = datetime.utcnow() if read else None
        await db.commit(); await db.refresh(n)
        return n

    async def mark_all_read(self, db, user, source: Optional[str] = None) -> int:
        clauses = [
            Notification.user_id == str(user.id),
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

    async def dismiss(self, db, user, notification_id) -> Optional[Notification]:
        n = await self._get_own(db, user, notification_id)
        if n is None:
            return None
        n.dismissed_at = datetime.utcnow()
        if n.read_at is None:
            n.read_at = n.dismissed_at
        await db.commit(); await db.refresh(n)
        return n


inbox_service = InboxService()
