# Audit Log Routes
# Licensed under the BOW Enterprise License
# See backend/app/ee/LICENSE for details

import csv
import io
import json
from typing import Literal, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.ee.license import require_enterprise
from app.ee.audit.service import audit_service
from app.ee.audit.schemas import (
    AuditLogResponse,
    AuditLogListResponse,
    AuditLogFilters,
)
from app.ee.audit.streams.envelope import build_envelope
from app.errors import AppError, ErrorCode
from app.models.user import User
from app.models.organization import Organization

EXPORT_MAX_ROWS = 100_000
CSV_COLUMNS = [
    "id", "occurred_at", "action", "actor_type", "actor_email", "actor_id",
    "resource_type", "resource_id", "title", "ip_address", "user_agent", "details_json",
]

router = APIRouter(prefix="/enterprise/audit", tags=["enterprise", "audit"])


@router.get("", response_model=AuditLogListResponse)
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    action: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    search: Optional[str] = Query(None),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    List audit logs for the organization.
    Enterprise feature - requires audit_logs license feature.
    """
    filters = AuditLogFilters(
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        user_id=user_id,
        start_date=start_date,
        end_date=end_date,
        search=search,
    )

    logs, total = await audit_service.get_logs(
        db=db,
        organization_id=str(organization.id),
        filters=filters,
        page=page,
        page_size=page_size,
    )

    total_pages = (total + page_size - 1) // page_size

    return AuditLogListResponse(
        items=logs,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )


@router.get("/action-types", response_model=list[str])
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def get_action_types(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    Get list of available action types for filtering.
    Enterprise feature - requires audit_logs license feature.
    """
    action_types = await audit_service.get_action_types(
        db=db,
        organization_id=str(organization.id),
    )
    
    return action_types


@router.get("/resource-types", response_model=list[str])
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def get_resource_types(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Distinct resource types for the resource filter."""
    return await audit_service.get_resource_types(db=db, organization_id=str(organization.id))


def _csv_safe(value) -> str:
    """Neutralize spreadsheet formula injection in exported cells."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv_row(env: dict) -> list:
    target = env["targets"][0] if env["targets"] else {}
    return [_csv_safe(v) for v in (
        env["id"], env["occurred_at"], env["action"], env["actor"]["type"], env["actor"]["email"],
        env["actor"]["id"], target.get("type"), target.get("id"), target.get("name"),
        env["context"]["ip_address"], env["context"]["user_agent"],
        json.dumps(env["metadata"], separators=(",", ":"), default=str),
    )]


@router.get("/health")
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def audit_health(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Operational health of audit recording and delivery, independent of the
    delivery path it describes (docs/design/audit-event-contract.md, "Health").

    Process-level counters come from the durable queue of this worker; stream
    rows cover this organization's streams: pending events, lag, oldest
    pending age, consecutive failures, degraded/invalid state, and when the
    last heartbeat was recorded (a collector not receiving heartbeats while
    this says they are written has a delivery problem, not an idle producer).
    """
    from sqlalchemy import func, select
    from app.ee.audit.models import AuditLog
    from app.ee.audit.contract import REQUIRED_BY_CATEGORY
    from app.ee.audit.maintenance import pending_counts
    from app.ee.audit.streams.exporter import stream_status
    from app.ee.audit.streams.models import AuditLogStream
    from app.ee.audit.tool_audit import get_tool_audit_queue_stats, spill_backlog
    from app.settings.config import settings

    org_id = str(organization.id)
    now = datetime.utcnow()
    streams = (await db.execute(
        select(AuditLogStream).where(AuditLogStream.organization_id == org_id, AuditLogStream.deleted_at.is_(None))
    )).scalars().all()
    stream_rows = []
    for st in streams:
        status = await stream_status(db, st, now)
        stream_rows.append({
            "id": st.id, "name": st.name, "destination": st.destination, "state": st.state,
            "pending": status["pending"], "lag_seconds": status["lag_seconds"],
            "oldest_pending_age_seconds": status["lag_seconds"],
            "consecutive_failures": st.consecutive_failures or 0,
            "degraded_since": st.degraded_at, "next_attempt_at": st.next_attempt_at,
            "last_delivered_at": st.last_delivered_at, "last_error": st.last_error,
        })
    last_heartbeat = (await db.execute(
        select(func.max(AuditLog.created_at)).where(AuditLog.organization_id == org_id, AuditLog.action == "audit.heartbeat")
    )).scalar()
    # Records missing a required field for their category (contract check).
    missing_outcome = (await db.execute(
        select(func.count(AuditLog.id)).where(
            AuditLog.organization_id == org_id, AuditLog.schema_version.isnot(None), AuditLog.outcome.is_(None)
        )
    )).scalar() or 0
    legacy = (await db.execute(
        select(func.count(AuditLog.id)).where(AuditLog.organization_id == org_id, AuditLog.schema_version.is_(None))
    )).scalar() or 0
    queue = get_tool_audit_queue_stats()
    cfg = settings.bow_config.audit
    return {
        "checked_at": now,
        "recording": {
            "queue_depth": queue.get("queued", 0),
            "enqueued": queue.get("enqueued", 0), "written": queue.get("written", 0),
            "failed_writes": queue.get("failed", 0), "recording_failures": queue.get("recording_failures", 0),
            "spilled": queue.get("spilled", 0), "replayed": queue.get("replayed", 0),
            "replay_errors": queue.get("replay_errors", 0), "dropped": queue.get("dropped", 0),
            "spill_backlog": spill_backlog(),
            **(await pending_counts(db)),
        },
        "contract": {"records_missing_outcome": int(missing_outcome), "legacy_records": int(legacy),
                     "required_by_category": REQUIRED_BY_CATEGORY},
        "heartbeat": {"interval_seconds": cfg.heartbeat_interval_seconds, "last_recorded_at": last_heartbeat},
        "retention": {"days": cfg.retention_days, "legal_hold": cfg.legal_hold},
        "streams": stream_rows,
    }


@router.get("/export")
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def export_audit_logs(
    request: Request,
    format: Literal["json", "csv"] = Query("json"),
    action: Optional[str] = Query(None),
    resource_type: Optional[str] = Query(None),
    resource_id: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    search: Optional[str] = Query(None),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Download the filtered audit log, oldest first.

    ``json`` is NDJSON of envelope v1 — the same shape log streams deliver;
    ``csv`` flattens it. Streamed in keyset pages; capped at EXPORT_MAX_ROWS
    per request (narrow the filter, or use a log stream, for more).
    """
    filters = AuditLogFilters(
        action=action, resource_type=resource_type, resource_id=resource_id, user_id=user_id,
        start_date=start_date, end_date=end_date, search=search,
    )
    org_id = str(organization.id)
    # Snapshot before writing the audit_log.exported row: the file holds
    # exactly the counted rows — not this export's own event, not rows
    # written while it streams — and therefore never exceeds the cap.
    total, upper = await audit_service.export_snapshot(db, org_id, filters)
    if total > EXPORT_MAX_ROWS:
        raise AppError.bad_request(
            ErrorCode.AUDIT_EXPORT_TOO_LARGE,
            f"Export is limited to {EXPORT_MAX_ROWS} events; this filter matches {total}.",
            limit=EXPORT_MAX_ROWS, total=total,
        )
    await audit_service.log(
        db=db, organization_id=org_id, action="audit_log.exported", user_id=str(current_user.id),
        resource_type="audit_log", details={
            "format": format, "row_count": total,
            "filters": {k: (v.isoformat() if isinstance(v, datetime) else v)
                        for k, v in filters.model_dump().items() if v is not None},
        }, request=request,
    )
    org_name = organization.name

    async def generate():
        # Own session: the request-scoped one may be closed before the body
        # finishes streaming.
        from app.dependencies import async_session_maker

        async with async_session_maker() as s:
            if format == "csv":
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(CSV_COLUMNS)
                yield buf.getvalue()
            async for rows in audit_service.iter_logs_ascending(s, org_id, filters, upper=upper, limit=total):
                envs = [build_envelope(log, user_email=email, organization_name=org_name) for log, email in rows]
                if format == "csv":
                    buf = io.StringIO()
                    w = csv.writer(buf)
                    for env in envs:
                        w.writerow(_csv_row(env))
                    yield buf.getvalue()
                else:
                    yield "".join(json.dumps(e, separators=(",", ":"), default=str) + "\n" for e in envs)

    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    ext = "csv" if format == "csv" else "jsonl"
    media = "text/csv; charset=utf-8" if format == "csv" else "application/x-ndjson"
    return StreamingResponse(
        generate(), media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="audit-logs-{stamp}.{ext}"',
            "X-Total-Count": str(total),
        },
    )


@router.get("/{log_id}", response_model=AuditLogResponse)
@require_enterprise(feature="audit_logs")
@requires_permission("view_audit_logs")
async def get_audit_log(
    log_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    Get a single audit log entry by ID.
    Enterprise feature - requires audit_logs license feature.
    """
    log = await audit_service.get_log_by_id(
        db=db,
        organization_id=str(organization.id),
        log_id=log_id,
    )

    if not log:
        raise HTTPException(status_code=404, detail="Audit log not found")

    return log
