"""The app activity log over HTTP (services/artifact_activity.py).

* ``POST /api/artifacts/{id}/runtime/activity``: the editor's page broker
  sends what happened in the app.
* ``GET /api/artifacts/{id}/runtime/activity``: the editor reads it back
  (the Activity log panel), newest first.

App sessions only (``current_user``: embed sessions never reach it), and only
someone who edits the artifact, so viewers and host applications are never
recorded and never read it.
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.dependencies import get_async_db
from app.errors import AppError, ErrorCode
from app.models.report import Report
from app.models.user import User
from app.services import artifact_activity
from app.services.artifact_admission import admit

router = APIRouter(prefix="/artifacts/{artifact_id}/runtime", tags=["artifact activity"])

BATCHES_PER_MINUTE = 60


class ActivityBatch(BaseModel):
    session_id: str = Field(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    report_id: Optional[str] = Field(default=None, max_length=36)
    artifact_version: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    events: List[dict[str, Any]] = Field(max_length=artifact_activity.MAX_BATCH)


async def _report_for(db, artifact, user, report_id: Optional[str]) -> Optional[str]:
    """The conversation these events belong to: one editing this artifact,
    that the person may use. None when they edit nothing here."""
    from app.services import dashboard_access
    from app.services.dashboard_collab import editing_report_ids

    editing = await editing_report_ids(db, artifact)
    if artifact.report_id and str(artifact.report_id) not in editing:
        editing.insert(0, str(artifact.report_id))
    candidates = [report_id] if report_id else editing
    can_edit = None
    for rid in candidates:
        if rid not in editing:
            continue
        report = await db.get(Report, rid)
        if report is None or report.deleted_at is not None or str(report.organization_id) != str(artifact.organization_id):
            continue
        if str(report.user_id) == str(user.id):
            return rid
        if can_edit is None:
            can_edit = await dashboard_access.can_edit(db, artifact, user)
        if can_edit:
            return rid
    return None


async def _editing(db, artifact_id: str, user: User, report_id: Optional[str]) -> tuple:
    """The artifact and the conversation the person edits it in; 404 for
    anyone else (viewers are told nothing, as if the artifact did not exist)."""
    from app.models.artifact import Artifact

    artifact = await db.get(Artifact, artifact_id)
    if artifact is None or artifact.deleted_at is not None:
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    report_id = await _report_for(db, artifact, user, report_id)
    if report_id is None:
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    return artifact, report_id


@router.get("/activity")
async def read_activity(artifact_id: str, report_id: Optional[str] = None,
                        only_errors: bool = False, source: Optional[str] = None,
                        limit: int = Query(100, ge=1, le=200),
                        db: AsyncSession = Depends(get_async_db), user: User = Depends(current_user)):
    """The log as the Activity log panel shows it: newest first, each event
    with its time, source, version and a one-line description."""
    artifact, rid = await _editing(db, artifact_id, user, report_id)
    await admit(db, [str(artifact.id), str(user.id), "activity-read"], BATCHES_PER_MINUTE)
    rows = await artifact_activity.recent(db, report_id=rid, artifact_id=str(artifact.id),
                                          only_errors=only_errors, source=source, limit=limit)
    return {
        "events": [{
            "id": e.id, "created_at": e.created_at.isoformat() + "Z" if e.created_at else None,
            "kind": e.kind, "source": e.source, "version": e.artifact_version, "status": e.status,
            "error_code": e.error_code, "failed": artifact_activity.is_failure(e),
            "text": artifact_activity.describe(e), "session_id": e.session_id,
        } for e in reversed(rows)],
        "retention_days": artifact_activity.RETENTION_DAYS,
        "keep_per_report": artifact_activity.KEEP_PER_REPORT,
    }


@router.post("/activity")
async def record_activity(artifact_id: str, body: ActivityBatch, db: AsyncSession = Depends(get_async_db),
                          user: User = Depends(current_user)):
    artifact, report_id = await _editing(db, artifact_id, user, body.report_id)
    await admit(db, [str(artifact.id), str(user.id), "activity"], BATCHES_PER_MINUTE)
    stored = await artifact_activity.record(
        db, organization_id=str(artifact.organization_id), artifact_id=str(artifact.id), report_id=report_id,
        user_id=str(user.id), session_id=body.session_id, source="editor", events=body.events,
        artifact_version=body.artifact_version,
    )
    return {"stored": stored}
