"""Collaborative dashboards (collab01;
docs/feedback-loops/2026-10-23-collaborative-dashboards.md).

- `/dashboards/{id}/drafts`: the implicit draft and the named ones; start one.
- `/dashboards/{id}/history`: the conversations that edited it, its versions,
  the releases kept for Roll back.
- `/dashboards/{id}/rollback`, `/transfer`: readers back to a kept release;
  the dashboard to another editor.
- `/dashboards/{id}/presence`: who is editing right now.
- `/d/{ref}/draft/changes`, `/d/{ref}/draft/run`: who changed which chart,
  and an editor's own run of a version made in someone else's conversation.
- `/dashboards/conversations/{report_id}`: the "Continued from" card.

The path parameter is `dashboard_id`, not `artifact_id`: the permission
decorator reads `artifact_id` as a version id.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user, current_user_optional
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.models.user import User
from app.schemas.dashboard_schema import (
    DashboardChangesSchema,
    DashboardConversationContext,
    DashboardDraftCreate,
    DashboardDraftSchema,
    DashboardHistorySchema,
    DashboardJoinResponse,
    DashboardPresenceSchema,
    DashboardPresenceUpdate,
    DashboardPublishStatusSchema,
    DashboardRollbackRequest,
    DashboardSharingSchema,
    DashboardTransferRequest,
)
from app.services import dashboard_access, dashboard_collab, dashboard_presence, dashboard_settings_service

router = APIRouter(tags=["dashboards"])


async def _dashboard(db, dashboard_id, user, organization, need):
    return await dashboard_settings_service.dashboard_for(db, dashboard_id, user, organization, need)


def _spawn_rerun(result: dict, user, organization) -> bool:
    from app.core import fire_and_forget
    from app.services.fork_service import fork_service

    pending = result.get("pending_code") or {}
    if pending:
        fire_and_forget.spawn(fork_service.hydrate_fork(result["report_id"], str(user.id), str(organization.id),
                                                       pending, retire=False))
    return bool(pending)


@router.get("/dashboards/{dashboard_id}/drafts", response_model=list[DashboardDraftSchema])
@requires_permission("publish_reports")
async def list_dashboard_drafts(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await _dashboard(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    return await dashboard_collab.list_drafts(db, artifact)


@router.post("/dashboards/{dashboard_id}/drafts", response_model=DashboardJoinResponse)
@requires_permission("publish_reports")
async def create_dashboard_draft(
    dashboard_id: str,
    payload: DashboardDraftCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Start a separate, named draft -- in a new conversation of the caller's."""
    artifact = await _dashboard(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    result = await dashboard_collab.create_draft(db, artifact, current_user, organization, name=payload.name,
                                                 from_version_id=payload.from_version_id)
    return DashboardJoinResponse(report_id=result["report_id"], created=True,
                                 rerunning=_spawn_rerun(result, current_user, organization))


@router.get("/dashboards/{dashboard_id}/history", response_model=DashboardHistorySchema)
@requires_permission("publish_reports")
async def get_dashboard_history(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await _dashboard(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    return await dashboard_collab.history(db, artifact, current_user)


@router.post("/dashboards/{dashboard_id}/rollback", response_model=DashboardPublishStatusSchema)
@requires_permission("publish_reports")
async def rollback_dashboard(
    dashboard_id: str,
    payload: DashboardRollbackRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Readers go back to a kept release at once; nothing runs."""
    from app.services import dashboard_publish

    artifact = await _dashboard(db, dashboard_id, current_user, organization, dashboard_access.CAP_PUBLISH)
    await dashboard_collab.rollback(db, artifact, current_user, organization, payload.release_id)
    await db.refresh(artifact)
    return await dashboard_publish.publish_status(db, artifact)


@router.post("/dashboards/{dashboard_id}/transfer", response_model=DashboardSharingSchema)
@requires_permission("publish_reports")
async def transfer_dashboard(
    dashboard_id: str,
    payload: DashboardTransferRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Hand the dashboard to another editor; the previous owner stays an editor."""
    artifact = await _dashboard(db, dashboard_id, current_user, organization, dashboard_access.CAP_TRANSFER)
    await dashboard_collab.transfer(db, artifact, current_user, organization, payload.user_id)
    await db.refresh(artifact)
    return await dashboard_settings_service.get_sharing(db, artifact)


@router.get("/dashboards/{dashboard_id}/presence", response_model=DashboardPresenceSchema)
@requires_permission("publish_reports")
async def get_dashboard_presence(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await _dashboard(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    return {"entries": await dashboard_presence.entries(db, artifact, current_user)}


@router.put("/dashboards/{dashboard_id}/presence", response_model=DashboardPresenceSchema)
@requires_permission("publish_reports")
async def beat_dashboard_presence(
    dashboard_id: str,
    payload: DashboardPresenceUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """The editor's browser, every ~15 s while the dashboard is open; the
    answer is everyone's presence, so one call serves both."""
    artifact = await _dashboard(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    report_id = payload.report_id if payload.report_id and await dashboard_collab.is_editing_conversation(
        db, artifact, payload.report_id) else None
    await dashboard_presence.heartbeat(db, artifact, current_user, report_id)
    return {"entries": await dashboard_presence.entries(db, artifact, current_user)}


@router.delete("/dashboards/{dashboard_id}/presence", status_code=204)
@requires_permission("publish_reports")
async def leave_dashboard_presence(
    dashboard_id: str,
    report_id: str | None = Query(None),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await _dashboard(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    await dashboard_presence.leave(db, artifact, current_user, report_id)


@router.get("/d/{ref}/draft/changes", response_model=DashboardChangesSchema)
async def get_draft_changes(
    ref: str,
    version: int | None = Query(None, ge=1),
    draft: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Who last changed each chart of the version shown, and how it compares
    with the live release."""
    from app.routes.dashboard import _draft
    from app.services import dashboard_data

    artifact, shown, _ = await _draft(db, ref, user, version=version, draft=draft)
    if shown is None:
        raise dashboard_data.not_found()
    return await dashboard_collab.changes(db, artifact, shown)


@router.post("/d/{ref}/draft/run")
async def run_draft_version(
    ref: str,
    version: int | None = Query(None, ge=1),
    draft: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """An editor's own run of a version made in someone else's conversation:
    its charts with the editor's credentials, kept as theirs alone. The draft
    door admits them; the conversation's sharing is not asked."""
    from app.errors import AppError
    from app.models.report import Report
    from app.routes.dashboard import _draft
    from app.services import dashboard_data
    from app.services.report_service import ReportService

    if user is None:
        raise AppError.unauthorized()
    artifact, shown, _ = await _draft(db, ref, user, version=version, draft=draft)
    if shown is None:
        raise dashboard_data.not_found()
    owner = (await db.execute(select(Report.user_id).where(Report.id == str(shown.report_id)))).scalar()
    if str(owner) == str(user.id):
        return {"steps_total": 0, "steps_succeeded": 0, "steps_failed": 0, "own": True}
    return await ReportService().viewer_rerun_report_steps(db, str(shown.report_id), user,
                                                           artifact_id=str(shown.id), admitted_editor=True)


@router.get("/dashboards/conversations/{report_id}", response_model=list[DashboardConversationContext])
async def get_conversation_dashboards(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """The card on top of a conversation that joined a dashboard: "Continued
    from Dana's conversation", with the summary it started from. Only the
    conversation's own user gets it; anyone else gets nothing."""
    from app.models.artifact import Artifact, ArtifactVersion
    from app.models.artifact_release import ArtifactConversation, ArtifactDraft
    from app.models.report import Report
    from app.models.step import Step
    from app.models.query import Query as QueryModel
    from app.services.fork_service import FORK_PENDING_STATUS

    report = await db.get(Report, str(report_id))
    if report is None or str(report.user_id) != str(current_user.id):
        return []
    rows = (await db.execute(
        select(ArtifactConversation).where(ArtifactConversation.report_id == str(report_id),
                                           ArtifactConversation.ended_at.is_(None))
    )).scalars().all()
    rerunning = (await db.execute(
        select(Step.id).join(QueryModel, QueryModel.default_step_id == Step.id)
        .where(QueryModel.report_id == str(report_id), Step.status == FORK_PENDING_STATUS).limit(1)
    )).first() is not None
    out = []
    for row in rows:
        artifact = await db.get(Artifact, str(row.artifact_id))
        if artifact is None or artifact.deleted_at is not None:
            continue
        summary = row.summary or {}
        base = await db.get(ArtifactVersion, str(row.base_version_id)) if row.base_version_id else None
        previous_report = str(base.report_id) if base is not None else None
        prev_owner = (await db.execute(select(Report.user_id, Report.status, Report.deleted_at)
                                       .where(Report.id == previous_report))).first() if previous_report else None
        can_open = bool(prev_owner and str(prev_owner[0]) == str(current_user.id) and prev_owner[1] != "archived"
                        and prev_owner[2] is None)
        out.append({
            "artifact_id": str(artifact.id), "title": artifact.title, "slug": artifact.slug,
            "continued_from": summary.get("continued_from"),
            "continued_from_report_id": previous_report if can_open else None,
            "can_open_previous": can_open, "started_at": row.started_at,
            "base_version_number": base.version if base is not None else None,
            "summary": summary, "rerunning": rerunning,
            "draft": await _draft_ref(db, ArtifactDraft, row.draft_id),
        })
    return out


async def _draft_ref(db, ArtifactDraft, draft_id) -> dict | None:
    if not draft_id:
        return None
    d = await db.get(ArtifactDraft, str(draft_id))
    if d is None or d.deleted_at is not None:
        return None
    return {"id": str(d.id), "name": d.name, "slug": d.slug}
