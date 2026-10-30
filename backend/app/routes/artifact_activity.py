"""``POST /api/artifacts/{id}/runtime/activity``: the editor's page broker
sends what happened in the app (services/artifact_activity.py).

App sessions only (``current_user``: embed sessions never reach it), and only
someone who edits the artifact, so viewers and host applications are never
recorded.
"""
from __future__ import annotations

from typing import Any, List, Optional

from fastapi import APIRouter, Depends
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


@router.post("/activity")
async def record_activity(artifact_id: str, body: ActivityBatch, db: AsyncSession = Depends(get_async_db),
                          user: User = Depends(current_user)):
    from app.models.artifact import Artifact

    artifact = await db.get(Artifact, artifact_id)
    if artifact is None or artifact.deleted_at is not None:
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    report_id = await _report_for(db, artifact, user, body.report_id)
    if report_id is None:
        # Viewers are never recorded; said like a missing artifact.
        raise AppError.not_found(ErrorCode.ARTIFACT_NOT_FOUND)
    await admit(db, [str(artifact.id), str(user.id), "activity"], BATCHES_PER_MINUTE)
    stored = await artifact_activity.record(
        db, organization_id=str(artifact.organization_id), artifact_id=str(artifact.id), report_id=report_id,
        user_id=str(user.id), session_id=body.session_id, source="editor", events=body.events,
        artifact_version=body.artifact_version,
    )
    return {"stored": stored}
