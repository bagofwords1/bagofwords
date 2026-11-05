# Audit maintenance jobs: platform stream sync, heartbeat, retention purge.
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy import delete, func, select

from app.ee.audit import context as audit_context
from app.ee.audit.models import AuditLog
from app.ee.audit.streams.models import AuditLogStream

logger = logging.getLogger(__name__)

PLATFORM_STREAM_NAME = "platform"
PURGE_BATCH = 5000


def _session_maker():
    from app.dependencies import async_session_maker
    return async_session_maker


def _cfg():
    from app.settings.config import settings
    return settings.bow_config.audit


async def sync_platform_stream() -> Optional[str]:
    """Create, update or pause the single organization-less stream from
    bow-config ``audit.platform_stream``. Idempotent; safe on every start.
    Returns the stream id when one is active."""
    cfg = _cfg().platform_stream
    maker = _session_maker()
    async with maker() as db:
        st = (await db.execute(
            select(AuditLogStream).where(AuditLogStream.organization_id.is_(None), AuditLogStream.deleted_at.is_(None))
        )).scalars().first()
        if not cfg.enabled:
            if st is not None and st.state == "active":
                st.state = "inactive"
                st.config_version = (st.config_version or 0) + 1
                await db.commit()
                logger.info("platform audit stream paused: audit.platform_stream is disabled")
            return None
        changed = False
        if st is None:
            st = AuditLogStream(
                organization_id=None, name=PLATFORM_STREAM_NAME, destination=cfg.destination,
                config=dict(cfg.config or {}), action_filter=list(cfg.action_filter) if cfg.action_filter else None,
                state="active", start_from="beginning", config_version=1,
            )
            st.set_secrets(dict(cfg.secrets or {}))
            db.add(st)
            changed = True
        else:
            desired_secrets = {k: v for k, v in (cfg.secrets or {}).items() if v not in (None, "")}
            try:
                current_secrets = st.get_secrets()
            except Exception:
                current_secrets = None
            if (st.destination != cfg.destination or dict(st.config or {}) != dict(cfg.config or {})
                    or (st.action_filter or None) != (list(cfg.action_filter) if cfg.action_filter else None)
                    or current_secrets != desired_secrets):
                st.destination = cfg.destination
                st.config = dict(cfg.config or {})
                st.action_filter = list(cfg.action_filter) if cfg.action_filter else None
                st.set_secrets(desired_secrets)
                st.config_version = (st.config_version or 0) + 1
                changed = True
            if st.state != "active":
                st.state = "active"
                st.consecutive_failures = 0
                st.next_attempt_at = None
                changed = True
        if changed:
            await db.commit()
            logger.info("platform audit stream configured (%s)", cfg.destination)
        return st.id


async def heartbeat_tick() -> int:
    """Write ``audit.heartbeat`` for every organization with an active stream
    (and for the platform stream). A collector that stops receiving
    heartbeats knows the producer is broken, not idle. Returns rows written."""
    cfg = _cfg()
    interval = int(cfg.heartbeat_interval_seconds or 0)
    if interval <= 0:
        return 0
    from app.ee.audit.service import audit_service
    from app.ee.audit.tool_audit import get_tool_audit_queue_stats, spill_backlog

    maker = _session_maker()
    async with maker() as db:
        orgs = (await db.execute(
            select(AuditLogStream.organization_id).where(
                AuditLogStream.state == "active", AuditLogStream.deleted_at.is_(None)
            ).distinct()
        )).scalars().all()
    if not orgs:
        return 0
    stats = get_tool_audit_queue_stats()
    details = {
        "interval_seconds": interval,
        "queue_depth": stats.get("queued", 0),
        "spill_backlog": spill_backlog(),
        "recording_failures": stats.get("recording_failures", 0),
    }
    n = 0
    with audit_context.scope(audit_context.for_job("audit_heartbeat")):
        for org_id in orgs:
            try:
                async with maker() as db:
                    await audit_service.log(
                        db=db, organization_id=org_id, action="audit.heartbeat", user_id=None,
                        resource_type="audit_log_stream", details=details, outcome="success",
                    )
                n += 1
            except Exception:
                logger.warning("audit heartbeat failed for org %s", org_id, exc_info=True)
    return n


async def retention_purge(now: Optional[datetime] = None) -> Dict[str, int]:
    """Delete rows older than ``audit.retention_days`` that every non-deleted
    stream of their organization has already delivered. Never runs under a
    legal hold, never removes an undelivered row, and records what it removed."""
    cfg = _cfg()
    days = int(cfg.retention_days or 0)
    if days <= 0 or cfg.legal_hold:
        return {}
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=days)
    from app.ee.audit.service import audit_service

    maker = _session_maker()
    removed: Dict[str, int] = {}
    async with maker() as db:
        org_rows = (await db.execute(
            select(AuditLog.organization_id).where(AuditLog.created_at < cutoff).distinct()
        )).scalars().all()
    for org_id in org_rows:
        async with maker() as db:
            org_pred = AuditLog.organization_id.is_(None) if org_id is None else AuditLog.organization_id == org_id
            stream_pred = AuditLogStream.organization_id.is_(None) if org_id is None else AuditLogStream.organization_id == org_id
            streams = (await db.execute(
                select(AuditLogStream.cursor_seq).where(stream_pred, AuditLogStream.deleted_at.is_(None))
            )).scalars().all()
            conds = [org_pred, AuditLog.created_at < cutoff]
            if streams:
                # Undelivered = not yet stamped, or past the slowest cursor.
                floor = min((c for c in streams if c is not None), default=None)
                if floor is None:
                    continue  # a stream has delivered nothing yet: hold everything
                conds += [AuditLog.export_seq.isnot(None), AuditLog.export_seq <= floor]
            ids = (await db.execute(select(AuditLog.id).where(*conds).limit(PURGE_BATCH))).scalars().all()
            if not ids:
                continue
            await db.execute(delete(AuditLog).where(AuditLog.id.in_(ids)))
            await db.commit()
            removed[org_id or "platform"] = len(ids)
            with audit_context.scope(audit_context.for_job("audit_retention_purge")):
                await audit_service.log(
                    db=db, organization_id=org_id, action="audit.retention_purged", user_id=None,
                    resource_type="audit_log", details={"removed": len(ids), "retention_days": days, "cutoff": cutoff.isoformat() + "Z"},
                    outcome="success",
                )
    if removed:
        logger.info("audit retention purge removed %s", removed)
    return removed


async def pending_counts(db) -> Dict[str, int]:
    """Rows not yet stamped for export (health endpoint)."""
    unstamped = (await db.execute(select(func.count(AuditLog.id)).where(AuditLog.export_seq.is_(None)))).scalar() or 0
    return {"unstamped_rows": int(unstamped)}
