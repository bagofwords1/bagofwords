# Audit stream exporter
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# Treats audit_logs as an outbox. Each tick, for every active stream that is
# due: claim it with a short lease (a compare-and-set UPDATE of next_attempt_at,
# so several hosts never double-send), read the next batch after its cursor, send it, and
# move the cursor only when the destination accepted the batch. Delivery is
# therefore at-least-once with no gaps; every event carries its audit_logs id
# so the receiver can dedupe.
#
# Rows newer than ``now - lag`` are left for a later tick: a transaction that
# commits late can carry a created_at earlier than rows already delivered, and
# the lag window is what keeps the cursor from stepping over it.

import logging
import os
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.orm import selectinload

from app.ee.audit.models import AuditLog
from app.ee.audit.streams.destinations import build_destination
from app.ee.audit.streams.destinations.base import FATAL, INVALID, OK, RETRYABLE, SendResult
from app.ee.audit.streams.envelope import build_envelope
from app.ee.audit.streams.models import AuditLogStream

logger = logging.getLogger(__name__)

LEASE_SECONDS = 120
MAX_BATCHES_PER_TICK = 10
MAX_BACKOFF_SECONDS = 900
_DEFAULT_LAG_SECONDS = 30


def lag_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("BOW_AUDIT_STREAM_LAG_SECONDS", _DEFAULT_LAG_SECONDS)))
    except ValueError:
        return float(_DEFAULT_LAG_SECONDS)


def backoff_seconds(failures: int) -> float:
    base = min(5 * (2 ** max(0, failures - 1)), MAX_BACKOFF_SECONDS)
    return base * random.uniform(0.8, 1.2)


def matches_filter(action: str, prefixes: Optional[List[str]]) -> bool:
    if not prefixes:
        return True
    return any(action.startswith(p) for p in prefixes)


def _session_maker():
    from app.dependencies import async_session_maker
    return async_session_maker


def after_cursor(cursor_created_at: Optional[datetime], cursor_id: Optional[str]):
    """SQL predicate: rows strictly after the (created_at, id) cursor."""
    if cursor_created_at is None:
        return None
    return or_(
        AuditLog.created_at > cursor_created_at,
        and_(AuditLog.created_at == cursor_created_at, AuditLog.id > (cursor_id or "")),
    )


async def fetch_batch(db, organization_id: str, cursor: Tuple[Optional[datetime], Optional[str]],
                      cutoff: datetime, limit: int):
    from app.models.user import User

    q = (
        select(AuditLog, User.email)
        .outerjoin(User, User.id == AuditLog.user_id)
        .where(AuditLog.organization_id == organization_id, AuditLog.created_at < cutoff)
    )
    pred = after_cursor(*cursor)
    if pred is not None:
        q = q.where(pred)
    q = q.order_by(AuditLog.created_at, AuditLog.id).limit(limit)
    return (await db.execute(q)).all()


async def _claim(stream_id: str, now: datetime) -> Optional[dict]:
    maker = _session_maker()
    async with maker() as db:
        # Compare-and-set lease: only the process whose UPDATE matches the
        # "due" predicate wins. Atomic on every backend (SQLite ignores
        # SELECT ... FOR UPDATE, so a row lock alone would not serialize
        # two hosts sharing one database file).
        claimed = await db.execute(
            update(AuditLogStream)
            .where(
                AuditLogStream.id == stream_id,
                AuditLogStream.state == "active",
                AuditLogStream.deleted_at.is_(None),
                or_(AuditLogStream.next_attempt_at.is_(None), AuditLogStream.next_attempt_at <= now),
            )
            .values(next_attempt_at=now + timedelta(seconds=LEASE_SECONDS), last_attempt_at=now)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        st = (await db.execute(
            select(AuditLogStream).options(selectinload(AuditLogStream.organization)).where(AuditLogStream.id == stream_id)
        )).scalar_one()
        try:
            secrets = st.get_secrets()
        except Exception:
            secrets = None
        snap = {
            "id": st.id,
            "organization_id": st.organization_id,
            "organization_name": st.organization.name if st.organization else None,
            "name": st.name,
            "destination": st.destination,
            "config": dict(st.config or {}),
            "secrets": secrets,
            "action_filter": list(st.action_filter or []) or None,
            "cursor": (st.cursor_created_at, st.cursor_id),
            "failures": st.consecutive_failures or 0,
        }
        return snap


async def _save_progress(stream_id: str, cursor: Tuple[datetime, str], delivered: int, now: datetime) -> None:
    maker = _session_maker()
    async with maker() as db:
        st = await db.get(AuditLogStream, stream_id)
        if st is None:
            return
        st.cursor_created_at, st.cursor_id = cursor
        st.delivered_count = (st.delivered_count or 0) + delivered
        if delivered:
            st.last_delivered_at = now
        await db.commit()


async def _finish(snap: dict, result: SendResult, now: datetime) -> Optional[str]:
    """Record the outcome; return the new terminal state if it changed."""
    maker = _session_maker()
    transitioned = None
    async with maker() as db:
        st = await db.get(AuditLogStream, snap["id"])
        if st is None:
            return None
        if result.kind == OK:
            st.consecutive_failures = 0
            st.next_attempt_at = None
            st.last_error = None
        elif result.kind == RETRYABLE:
            st.consecutive_failures = (st.consecutive_failures or 0) + 1
            st.next_attempt_at = now + timedelta(seconds=backoff_seconds(st.consecutive_failures))
            st.last_error = result.error
        else:
            st.consecutive_failures = (st.consecutive_failures or 0) + 1
            st.next_attempt_at = None
            st.last_error = result.error
            new_state = "invalid" if result.kind == INVALID else "error"
            # A user may have paused or edited the stream mid-send; only an
            # active stream is moved to a failure state.
            if st.state == "active":
                st.state = new_state
                transitioned = new_state
        await db.commit()
    return transitioned


async def deliver_stream(stream_id: str, now: Optional[datetime] = None) -> int:
    """Deliver what is due for one stream; return the number of events sent."""
    now = now or datetime.utcnow()
    snap = await _claim(stream_id, now)
    if snap is None:
        return 0
    if snap["secrets"] is None:
        await _finish(snap, SendResult(INVALID, "stored secrets could not be decrypted (encryption key changed?)"), now)
        await _on_transition(snap, "invalid", "stored secrets could not be decrypted")
        return 0

    dest = build_destination(snap["destination"], snap["config"], snap["secrets"])
    cutoff = now - timedelta(seconds=lag_seconds())
    cursor = snap["cursor"]
    delivered = 0
    result = SendResult.success()
    maker = _session_maker()
    try:
        for _ in range(MAX_BATCHES_PER_TICK):
            async with maker() as db:
                rows = await fetch_batch(db, snap["organization_id"], cursor, cutoff, dest.max_batch)
            if not rows:
                break
            envelopes = [
                build_envelope(log, user_email=email, organization_name=snap["organization_name"])
                for log, email in rows
                if matches_filter(log.action, snap["action_filter"])
            ]
            if envelopes:
                try:
                    result = await dest.send(envelopes)
                except Exception as e:  # a destination bug must not wedge the exporter
                    logger.exception("audit stream %s: send raised", stream_id)
                    result = SendResult(RETRYABLE, f"{type(e).__name__}: {e}"[:500])
                if not result.ok:
                    break
            last = rows[-1][0]
            cursor = (last.created_at, last.id)
            await _save_progress(stream_id, cursor, len(envelopes), now)
            delivered += len(envelopes)
            if len(rows) < dest.max_batch:
                break
    finally:
        await dest.close()

    transitioned = await _finish(snap, result, now)
    if result.kind == RETRYABLE:
        logger.warning("audit stream %s (%s) send failed, backing off: %s", snap["name"], snap["destination"], result.error)
    if transitioned:
        await _on_transition(snap, transitioned, result.error)
    return delivered


async def run_exporter_tick(now: Optional[datetime] = None) -> Dict[str, int]:
    """One scheduler tick over every due stream (all organizations)."""
    now = now or datetime.utcnow()
    maker = _session_maker()
    async with maker() as db:
        ids = (await db.execute(
            select(AuditLogStream.id).where(
                AuditLogStream.state == "active",
                AuditLogStream.deleted_at.is_(None),
                or_(AuditLogStream.next_attempt_at.is_(None), AuditLogStream.next_attempt_at <= now),
            )
        )).scalars().all()
    out: Dict[str, int] = {}
    for sid in ids:
        try:
            out[sid] = await deliver_stream(sid, now)
        except Exception:
            logger.exception("audit stream %s: delivery tick failed", sid)
    return out


async def scheduled_export_tick() -> None:
    """APScheduler entry point; enterprise-gated so unlicensed installs no-op."""
    from app.ee.license import has_feature

    if not has_feature("audit_log_streams"):
        return
    await run_exporter_tick()


async def stream_status(db, st: AuditLogStream, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    q = select(func.count(AuditLog.id), func.min(AuditLog.created_at)).where(
        AuditLog.organization_id == st.organization_id
    )
    pred = after_cursor(st.cursor_created_at, st.cursor_id)
    if pred is not None:
        q = q.where(pred)
    pending, oldest = (await db.execute(q)).one()
    lag = (now - oldest).total_seconds() if pending and oldest else 0.0
    return {"pending": int(pending or 0), "lag_seconds": max(0.0, lag)}


async def _on_transition(snap: dict, new_state: str, error: Optional[str]) -> None:
    """Audit the transition and email the org's admins once."""
    maker = _session_maker()
    try:
        from app.ee.audit.service import audit_service

        async with maker() as db:
            await audit_service.log(
                db=db, organization_id=snap["organization_id"], action="audit_stream.state_changed",
                user_id=None, resource_type="audit_log_stream", resource_id=snap["id"],
                details={"title": snap["name"], "state": new_state, "destination": snap["destination"], "error": error},
            )
    except Exception:
        logger.debug("audit_stream.state_changed audit failed", exc_info=True)
    try:
        from app.models.membership import Membership
        from app.models.user import User
        from app.services.notification_service import notification_service
        from app.settings.config import settings

        async with maker() as db:
            emails = (await db.execute(
                select(User.email).join(Membership, Membership.user_id == User.id).where(
                    Membership.organization_id == snap["organization_id"], Membership.role == "admin",
                )
            )).scalars().all()
            if not emails:
                return
            link = f"{settings.bow_config.base_url}/settings/audit?tab=streams"
            subject = f"Audit log stream \"{snap['name']}\" stopped ({new_state})"
            body = (
                f"The audit log stream \"{snap['name']}\" ({snap['destination']}) stopped delivering events "
                f"and is now {new_state}.\n\nLast error: {error or 'n/a'}\n\n"
                f"Events are kept and will be delivered from where the stream stopped once it is fixed "
                f"and resumed:\n{link}\n"
            )
            await notification_service.send_custom_email(
                recipients=list(emails), subject=subject, body=body,
                db=db, organization_id=snap["organization_id"], purpose="system",
            )
    except Exception:
        logger.warning("audit stream %s: admin notification failed", snap["id"], exc_info=True)
