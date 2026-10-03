# Audit stream exporter
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details
#
# Treats audit_logs as an outbox, in two steps per tick:
#
# 1. Stamp. One process at a time (a lease on audit_export_state) gives every
#    newly *visible* row of an organization that has a stream the next value of
#    a global sequence, audit_logs.export_seq. A row whose transaction commits
#    late (lock contention, a slow request, a replayed spill) simply becomes
#    visible later and is stamped later, with a higher number. Ordering by
#    visibility instead of created_at is what makes "no gaps" hold without a
#    lag window: no cursor can be past a row that was not yet visible.
# 2. Deliver. For every active stream that is due: claim it with a short lease
#    (a compare-and-set UPDATE of next_attempt_at, so several workers or hosts
#    never double-send), read the next batch after its export_seq cursor, send
#    it, and move the cursor only when the destination accepted the batch.
#
# Delivery is therefore at-least-once with no gaps; every event carries its
# audit_logs id so the receiver can dedupe.

import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import selectinload

from app.ee.audit.models import AuditLog
from app.ee.audit.streams.destinations import build_destination
from app.ee.audit.streams.destinations.base import FATAL, INVALID, OK, RETRYABLE, SendResult
from app.ee.audit.streams.envelope import build_envelope
from app.ee.audit.streams.models import AuditExportState, AuditLogStream

logger = logging.getLogger(__name__)

LEASE_SECONDS = 120
# Per-stream work per tick is bounded by time, not batch count, so a large
# backfill ("include all history") drains at full speed while one slow stream
# cannot hold the tick; the lease (LEASE_SECONDS) always outlasts the budget.
STREAM_BUDGET_SECONDS = 20.0
STREAM_CONCURRENCY = 4
MAX_BACKOFF_SECONDS = 900
STAMP_BATCH = 2000
STAMP_BUDGET_SECONDS = 10.0
# Outlasts one stamping pass; a pass whose lease release fails (a locked
# database) only blocks the next stamper until the lease expires.
STAMP_LEASE_SECONDS = 30


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


async def _ensure_state(maker) -> None:
    async with maker() as db:
        if await db.get(AuditExportState, 1) is None:
            db.add(AuditExportState(id=1, last_seq=0))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()  # another worker created it first


async def stamp_visible_rows(now: Optional[datetime] = None) -> int:
    """Give newly visible rows of streamed organizations the next export_seq.

    Returns the number of rows stamped (0 when another process holds the
    stamper lease).
    """
    now = now or datetime.utcnow()
    maker = _session_maker()
    await _ensure_state(maker)
    async with maker() as db:
        won = await db.execute(
            update(AuditExportState)
            .where(
                AuditExportState.id == 1,
                or_(AuditExportState.stamp_lease_until.is_(None), AuditExportState.stamp_lease_until <= now),
            )
            .values(stamp_lease_until=now + timedelta(seconds=STAMP_LEASE_SECONDS))
            .execution_options(synchronize_session=False)
        )
        if won.rowcount != 1:
            await db.rollback()
            return 0
        await db.commit()

    stamped = 0
    started = time.monotonic()
    try:
        while time.monotonic() - started < STAMP_BUDGET_SECONDS:
            try:
                n = await _stamp_batch(maker)
            except OperationalError:
                # Writers hold the database (SQLite lock, PG lock timeout):
                # nothing was committed; try again within this pass.
                logger.debug("audit export: stamping batch hit a lock; retrying", exc_info=True)
                await asyncio.sleep(0.25)
                continue
            stamped += n
            if n < STAMP_BATCH:
                break
    finally:
        for attempt in range(5):
            try:
                async with maker() as db:
                    await db.execute(update(AuditExportState).where(AuditExportState.id == 1).values(stamp_lease_until=None))
                    await db.commit()
                break
            except OperationalError:
                await asyncio.sleep(0.2 * (attempt + 1))
    return stamped


async def _stamp_batch(maker) -> int:
    async with maker() as db:
        streamed_orgs = select(AuditLogStream.organization_id).where(AuditLogStream.deleted_at.is_(None))
        ids = (await db.execute(
            select(AuditLog.id)
            .where(AuditLog.export_seq.is_(None), AuditLog.organization_id.in_(streamed_orgs))
            .order_by(AuditLog.created_at, AuditLog.id)
            .limit(STAMP_BATCH)
        )).scalars().all()
        if not ids:
            return 0
        state = await db.get(AuditExportState, 1)
        base = state.last_seq or 0
        await db.execute(update(AuditLog), [{"id": i, "export_seq": base + k + 1} for k, i in enumerate(ids)])
        state.last_seq = base + len(ids)
        await db.commit()
        return len(ids)


def _deliverable(q, organization_id: str, cursor_seq: Optional[int], start_after: Optional[datetime]):
    q = q.where(AuditLog.organization_id == organization_id, AuditLog.export_seq.isnot(None))
    if cursor_seq is not None:
        q = q.where(AuditLog.export_seq > cursor_seq)
    if start_after is not None:
        q = q.where(AuditLog.created_at >= start_after)
    return q


async def fetch_batch(db, organization_id: str, cursor_seq: Optional[int], start_after: Optional[datetime], limit: int):
    from app.models.user import User

    q = select(AuditLog, User.email).outerjoin(User, User.id == AuditLog.user_id)
    q = _deliverable(q, organization_id, cursor_seq, start_after)
    return (await db.execute(q.order_by(AuditLog.export_seq).limit(limit))).all()


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
            "cursor": st.cursor_seq,
            "start_after": st.start_after,
            "failures": st.consecutive_failures or 0,
        }
        return snap


async def _retry_locked(fn, attempts: int = 6):
    """Run a short write transaction, retrying while the database is locked
    by other writers, so bookkeeping never strands a stream behind its lease."""
    for attempt in range(attempts):
        try:
            return await fn()
        except OperationalError:
            if attempt == attempts - 1:
                raise
            await asyncio.sleep(0.2 * (attempt + 1))


async def _save_progress(stream_id: str, cursor: int, delivered: int, now: datetime) -> None:
    await _retry_locked(lambda: _save_progress_once(stream_id, cursor, delivered, now))


async def _save_progress_once(stream_id: str, cursor: int, delivered: int, now: datetime) -> None:
    maker = _session_maker()
    async with maker() as db:
        st = await db.get(AuditLogStream, stream_id)
        if st is None:
            return
        st.cursor_seq = cursor
        st.delivered_count = (st.delivered_count or 0) + delivered
        if delivered:
            st.last_delivered_at = now
        await db.commit()


async def _finish(snap: dict, result: SendResult, now: datetime) -> Optional[str]:
    """Record the outcome; return the new terminal state if it changed."""
    return await _retry_locked(lambda: _finish_once(snap, result, now))


async def _finish_once(snap: dict, result: SendResult, now: datetime) -> Optional[str]:
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
    cursor = snap["cursor"]
    delivered = 0
    result = SendResult.success()
    maker = _session_maker()
    started = time.monotonic()
    try:
        while time.monotonic() - started < STREAM_BUDGET_SECONDS:
            async with maker() as db:
                rows = await fetch_batch(db, snap["organization_id"], cursor, snap["start_after"], dest.max_batch)
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
            cursor = rows[-1][0].export_seq
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
    """One scheduler tick: stamp newly visible rows, then deliver every due
    stream (all organizations)."""
    now = now or datetime.utcnow()
    try:
        await stamp_visible_rows(now)
    except Exception:
        logger.exception("audit stream export: sequence stamping failed")
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
    gate = asyncio.Semaphore(STREAM_CONCURRENCY)

    async def one(sid: str) -> None:
        async with gate:
            try:
                out[sid] = await deliver_stream(sid, now)
            except Exception:
                logger.exception("audit stream %s: delivery tick failed", sid)

    await asyncio.gather(*(one(sid) for sid in ids))
    return out


async def scheduled_export_tick() -> None:
    """APScheduler entry point; enterprise-gated so unlicensed installs no-op."""
    from app.ee.license import has_feature

    if not has_feature("audit_log_streams"):
        return
    await run_exporter_tick()


async def stream_status(db, st: AuditLogStream, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    # Pending = stamped rows past the cursor + rows not stamped yet.
    q = select(func.count(AuditLog.id), func.min(AuditLog.created_at)).where(
        AuditLog.organization_id == st.organization_id,
        or_(AuditLog.export_seq.is_(None), AuditLog.export_seq > (st.cursor_seq if st.cursor_seq is not None else -1)),
    )
    if st.start_after is not None:
        q = q.where(AuditLog.created_at >= st.start_after)
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
