from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_db, get_async_db, release_request_db
from app.dependencies import get_current_organization
from app.ee.audit.service import audit_service

from typing import List, Optional
from pydantic import BaseModel
from app.services.report_service import ReportService
from app.services.dashboard_layout_service import DashboardLayoutService
from app.services.notification_service import notification_service
from app.services.fork_service import fork_service
from app.schemas.report_schema import ReportSchema, ReportCreate, ReportUpdate, ReportListResponse, ReportVisibilityUpdate, ReportRerunResultSchema, ViewerRunResultSchema, ReportActivityResponse
from app.schemas.notification_schema import NotifyRequest, NotifyResponse, NotificationType, NotificationChannel, ScheduleRequest
from app.schemas.dashboard_layout_version_schema import (
    DashboardLayoutVersionSchema,
    DashboardLayoutVersionCreate,
    DashboardLayoutVersionUpdate,
    DashboardLayoutBlocksPatch,
)
from app.models.user import User

from app.core.auth import current_user, current_user_optional
from app.models.organization import Organization
from app.core.permissions_decorator import requires_permission
from app.models.report import Report
from app.settings.config import settings as app_settings
from sqlalchemy import select


class ForkRequest(BaseModel):
    title: Optional[str] = None


class ForkResponse(BaseModel):
    id: str
    title: str
    forked_from_id: str
    slug: str


class StarResponse(BaseModel):
    id: str
    is_starred: bool


import logging
logger = logging.getLogger(__name__)

router = APIRouter(tags=["reports"])
report_service = ReportService()
layout_service = DashboardLayoutService()

@router.post("/reports", response_model=ReportSchema)
@requires_permission('create_reports')
async def create_report(
    report: ReportCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await report_service.create_report(db, report, current_user, organization)
    await audit_service.log(
        db=db,
        organization_id=organization.id,
        action="report.created",
        user_id=current_user.id,
        resource_type="report",
        resource_id=result.id,
        details={"title": result.title},
        request=request,
    )
    return result

@router.get("/reports", response_model=ReportListResponse)
@requires_permission('view_reports')
async def get_reports(
    page: int = Query(1, ge=1, description="Page number"),
    limit: int = Query(10, ge=1, le=100, description="Number of items per page"),
    filter: str = Query("my", description="Filter: 'my' or 'published'"),
    search: str | None = Query(None, description="Optional search term for report title"),
    scheduled: bool | None = Query(None, description="Filter by scheduled reports (true = only scheduled, false = only non-scheduled)"),
    status: str | None = Query(None, description="Filter by status: 'draft' or 'published'"),
    data_source_id: str | None = Query(None, description="Filter by data source ID"),
    mode: str | None = Query(None, description="Filter by mode: 'chat' or 'training'"),
    has_artifacts: str | None = Query(None, description="Filter by artifacts: 'yes' or 'no'"),
    artifact_mode: str | None = Query(None, description="Filter by artifact mode: 'page', 'slides' or 'doc'"),
    view: str | None = Query(None, description="'minimal' for a lightweight list (sidebar): basic fields only, no widgets/queries/completions"),
    project_id: str | None = Query(None, description="Filter by project: a project id, or 'none' for reports outside any project"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await report_service.get_reports(db, current_user, organization, page, limit, filter, search, scheduled, status, data_source_id, mode, has_artifacts, view, artifact_mode, project_id)
    await release_request_db(db)  # free the pooled connection before serialization (Cause A, Phase 1)
    return result

@router.get("/reports/activity", response_model=ReportActivityResponse)
@requires_permission('view_reports')
async def get_reports_activity(
    ids: str = Query(..., description="Comma-separated report ids (max 100)"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Lightweight live status for list badges: awaiting_user/running/queued + unread/error flags."""
    id_list = [i.strip() for i in ids.split(',') if i.strip()]
    result = await report_service.get_reports_activity(db, id_list, current_user, organization)
    await release_request_db(db)
    return result

@router.get("/reports/activity/stream")
@requires_permission('view_reports')
async def stream_reports_activity(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Live SSE feed of report activity changes for the caller's org.

    Deltas only — clients take a GET /reports/activity snapshot on every
    (re)connect; a dropped event is corrected by the next snapshot. The
    stream holds no DB connection while idle (the per-worker watcher opens
    its own short-lived sessions).
    """
    import asyncio as _asyncio
    from fastapi.responses import StreamingResponse
    from app.streaming.report_activity_hub import report_activity_hub, format_activity_event

    org_id = str(organization.id)
    user_id = str(current_user.id)
    await release_request_db(db)  # never pin a pool slot for the stream's lifetime

    sub = report_activity_hub.subscribe(org_id, user_id)

    async def gen():
        try:
            yield ":connected\n\n"
            while True:
                try:
                    ev = await _asyncio.wait_for(sub.queue.get(), timeout=15.0)
                except _asyncio.TimeoutError:
                    yield ":hb\n\n"  # keep proxies from reaping the idle stream
                    continue
                # A confirmation pause is "waiting for you" only for its
                # target users; everyone else sees the running state the
                # in-progress completion already implies.
                targets = ev.get('awaiting_user_ids')
                out = {k: v for k, v in ev.items() if k != 'awaiting_user_ids'}
                if targets and user_id not in targets:
                    out['state'] = 'running'
                yield format_activity_event(out)
        finally:
            report_activity_hub.unsubscribe(org_id, sub)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })

@router.post("/reports/{report_id}/viewed")
@requires_permission('view_reports', model=Report)
async def mark_report_viewed(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    """Bump the current user's last-viewed watermark (clears the unread badge)."""
    return await report_service.mark_report_viewed(db, report_id, current_user, organization)

@router.put("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def update_report(report_id: str, report: ReportUpdate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await report_service.update_report(db, report_id, report, current_user, organization)

@router.get("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('view_reports', model=Report, owner_only=True, allow_public=True)
async def get_report(report_id: str, db: AsyncSession = Depends(get_async_db), current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization)):
    return await report_service.get_report(db, report_id, current_user, organization)


@router.delete("/reports/{report_id}", response_model=ReportSchema)
@requires_permission('delete_reports', model=Report, owner_only=True)
async def delete_report(
    report_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await report_service.archive_report(db, report_id, current_user, organization)
    await audit_service.log(
        db=db,
        organization_id=organization.id,
        action="report.deleted",
        user_id=current_user.id,
        resource_type="report",
        resource_id=report_id,
        details={"title": result.title},
        request=request,
    )
    return result


@router.post("/reports/bulk/archive")
@requires_permission('delete_reports')
async def bulk_archive_reports(
    report_ids: List[str],
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """
    Archive multiple reports in a single operation.
    Only reports owned by the current user (or otherwise deletable per service rules) will be archived.
    """
    return await report_service.bulk_archive_reports(db, report_ids, current_user, organization)

@router.post("/reports/{report_id}/rerun", response_model=ReportRerunResultSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def rerun_report(
    report_id: str,
    artifact_id: str | None = Query(None, description="Refresh the queries behind this artifact; defaults to the report's latest artifact"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await report_service.rerun_report_steps(db, report_id, current_user, organization, artifact_id=artifact_id)

@router.post("/reports/{report_id}/publish", response_model=ReportSchema)
@requires_permission('publish_reports', model=Report, owner_only=True)
async def publish_report(
    report_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    result = await report_service.publish_report(db, report_id, current_user, organization)
    await audit_service.log(
        db=db,
        organization_id=organization.id,
        action="report.published",
        user_id=current_user.id,
        resource_type="report",
        resource_id=report_id,
        details={"title": result.title, "status": result.status},
        request=request,
    )
    return result

@router.post("/reports/{report_id}/star", response_model=StarResponse)
@requires_permission('view_reports', model=Report)
async def star_report(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    """Star (favorite) a report for the current user. Per-user; any viewer may star."""
    return await report_service.set_report_star(db, report_id, current_user, organization, starred=True)

@router.delete("/reports/{report_id}/star", response_model=StarResponse)
@requires_permission('view_reports', model=Report)
async def unstar_report(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    """Remove the current user's star from a report."""
    return await report_service.set_report_star(db, report_id, current_user, organization, starred=False)

@router.post("/reports/{report_id}/conversation-share")
@requires_permission('publish_reports', model=Report, owner_only=True)
async def toggle_conversation_share(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    """Toggle conversation sharing for a report. Returns enabled status and share token."""
    return await report_service.toggle_conversation_share(db, report_id, current_user, organization)

@router.put("/reports/{report_id}/visibility/{share_type}")
@requires_permission('publish_reports', model=Report, owner_only=True)
async def set_report_visibility(
    report_id: str,
    share_type: str,
    payload: ReportVisibilityUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Set visibility for artifact or conversation sharing.

    share_type: 'artifact' or 'conversation'
    """
    if share_type not in ('artifact', 'conversation'):
        raise HTTPException(status_code=400, detail="share_type must be 'artifact' or 'conversation'")
    return await report_service.set_visibility(
        db, report_id, share_type, payload.visibility,
        payload.shared_user_ids, current_user, organization,
        run_identity=payload.run_identity,
        shared_group_ids=payload.shared_group_ids,
        include_data_tab=payload.include_data_tab,
        artifact_chat_enabled=payload.artifact_chat_enabled,
        artifact_chat_data_source_ids=payload.artifact_chat_data_source_ids,
    )


@router.get("/reports/{report_id}/shares/{share_type}")
@requires_permission('publish_reports', model=Report, owner_only=True)
async def get_report_shares(
    report_id: str,
    share_type: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Get list of users a report is shared with for a given type."""
    if share_type not in ('artifact', 'conversation'):
        raise HTTPException(status_code=400, detail="share_type must be 'artifact' or 'conversation'")
    return await report_service.get_shares(db, report_id, share_type)


@router.get("/reports/{report_id}/artifact_chat/agents")
@requires_permission('publish_reports', model=Report, owner_only=True)
async def get_artifact_chat_agents(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Candidate agents for the ShareModal's chat scope picker: the agents this
    report actually uses — its attached roster, or (for Auto reports) the
    agents recovered from its tool executions. See
    ArtifactChatService.candidate_agent_ids."""
    from sqlalchemy.orm import selectinload, lazyload
    from app.models.data_source import DataSource
    from app.services.artifact_chat_service import artifact_chat_service

    report = (await db.execute(
        select(Report)
        .options(lazyload("*"), selectinload(Report.data_sources).options(lazyload("*")))
        .where(Report.id == report_id)
    )).unique().scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    agent_ids = await artifact_chat_service.candidate_agent_ids(db, report)
    by_id = {str(ds.id): ds for ds in (report.data_sources or [])}
    agents = []
    for aid in agent_ids:
        ds = by_id.get(aid) or await db.get(DataSource, aid)
        if ds is not None and ds.deleted_at is None:
            agents.append({"id": str(ds.id), "name": ds.name})
    return {"agents": agents}


@router.post("/reports/{report_id}/fork", response_model=ForkResponse)
@requires_permission('create_reports')
async def fork_report(
    report_id: str,
    body: ForkRequest,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Fork a published/shared report into the current user's workspace."""
    new_report = await fork_service.fork_report(
        db, report_id, current_user, title=body.title,
    )
    await audit_service.log(
        db=db,
        organization_id=organization.id,
        action="report.forked",
        user_id=current_user.id,
        resource_type="report",
        resource_id=new_report.id,
        details={"forked_from_id": report_id, "title": new_report.title},
        request=request,
    )
    return ForkResponse(
        id=str(new_report.id),
        title=new_report.title,
        forked_from_id=report_id,
        slug=new_report.slug,
    )


@router.post("/reports/{report_id}/notify", response_model=NotifyResponse)
@requires_permission('publish_reports', model=Report, owner_only=True)
async def notify_report(
    report_id: str,
    payload: NotifyRequest,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Send notifications (email, etc.) for shared dashboards, conversations, or scheduled reports."""
    # Load report
    result = await db.execute(select(Report).filter(Report.id == report_id))
    report = result.scalar_one_or_none()
    if not report:
        raise HTTPException(status_code=404, detail="Report not found")

    # Guard: schedule notifications only for published reports
    if payload.type == NotificationType.SCHEDULE_REPORT and report.status != "published":
        raise HTTPException(status_code=400, detail="Schedule notifications are only available for published reports")

    # Guard: share_url required for share types
    if payload.type in (NotificationType.SHARE_DASHBOARD, NotificationType.SHARE_CONVERSATION) and not payload.share_url:
        raise HTTPException(status_code=400, detail="share_url is required for share notifications")

    # Guard: email channel requires SMTP
    if NotificationChannel.EMAIL in payload.channels and not app_settings.email_client:
        raise HTTPException(status_code=400, detail="Email notifications are not available (SMTP not configured)")

    # Build share_url for schedule type if not provided
    share_url = payload.share_url or f"{app_settings.bow_config.base_url}/r/{report.id}"

    # Notify-first: for share notifications, create/refresh the durable in-app
    # notification for recipients who are users in this org *before* dispatching
    # email (email is the downstream channel). External addresses get email only;
    # the shared group_key dedups against the auto-notification from set_visibility.
    if payload.type in (NotificationType.SHARE_DASHBOARD, NotificationType.SHARE_CONVERSATION):
        try:
            from app.services.inbox_service import inbox_service
            from app.models.membership import Membership
            rows = (await db.execute(
                select(User.id).join(Membership, Membership.user_id == User.id).where(
                    Membership.organization_id == str(organization.id),
                    User.email.in_(payload.recipients),
                )
            )).all()
            user_ids = list({str(r[0]) for r in rows})
            if user_ids:
                share_type = 'conversation' if payload.type == NotificationType.SHARE_CONVERSATION else 'artifact'
                await inbox_service.notify_share(
                    db, report=report, share_type=share_type,
                    user_ids=user_ids, actor_user=current_user,
                )
        except Exception:
            logger.warning("notify_report in-app notification failed", exc_info=True)

    from app.dependencies import _locale_from_org
    response = await notification_service.dispatch(
        notification_type=payload.type,
        channels=payload.channels,
        recipients=payload.recipients,
        share_url=share_url,
        report_title=report.title,
        sender_name=current_user.name or current_user.email,
        message=payload.message,
        report_id=str(report.id),
        locale=_locale_from_org(organization),
    )

    # Audit log
    try:
        await audit_service.log(
            db=db,
            organization_id=str(organization.id),
            action="report.notification_sent",
            user_id=str(current_user.id),
            resource_type="report",
            resource_id=str(report.id),
            details={
                "type": payload.type.value,
                "channels": [c.value for c in payload.channels],
                "recipient_count": len(payload.recipients),
            },
            request=request,
        )
    except Exception:
        pass

    return response


@router.get("/r/{report_id}")
async def get_public_report(
    report_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    schema = await report_service.get_public_report(db, report_id, user=user)
    result = schema.model_dump()
    # Check fork eligibility for logged-in users.
    # lazyload("*"): check_eligibility only needs data_sources+connections;
    # without it this second Report load re-runs the full selectin cascade.
    from sqlalchemy.orm import selectinload, lazyload
    from app.models.data_source import DataSource
    report_result = await db.execute(
        select(Report)
        .options(
            lazyload("*"),
            selectinload(Report.data_sources).options(
                lazyload("*"),
                selectinload(DataSource.connections).options(lazyload("*")),
            ),
        )
        .where(Report.id == report_id)
    )
    report_obj = report_result.unique().scalar_one_or_none()
    if report_obj:
        eligibility = await fork_service.check_eligibility(db, report_obj, user)
        result["fork_eligibility"] = eligibility.to_dict()
    return result

@router.get("/c/{token}")
async def get_public_conversation(
    token: str,
    limit: int = 10,
    before: str | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Public endpoint to fetch a shared conversation by its token. Supports pagination."""
    result = await report_service.get_public_conversation(db, token, limit=limit, before=before, user=user)
    # Attach fork eligibility if user is logged in
    report_id = result.get("report_id") if isinstance(result, dict) else None
    if report_id:
        from sqlalchemy.orm import selectinload, lazyload
        from app.models.data_source import DataSource
        report_result = await db.execute(
            select(Report)
            .options(
                lazyload("*"),
                selectinload(Report.data_sources).options(
                    lazyload("*"),
                    selectinload(DataSource.connections).options(lazyload("*")),
                ),
            )
            .where(Report.id == report_id)
        )
        report_obj = report_result.unique().scalar_one_or_none()
        if report_obj:
            eligibility = await fork_service.check_eligibility(db, report_obj, user)
            result["fork_eligibility"] = eligibility.to_dict()
    return result


@router.get("/r/{report_id}/files/{file_id}/embed_token")
async def get_public_file_embed_token(
    report_id: str,
    file_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Mint a file-embed token for a file embedded in a PUBLIC report's artifact.

    Authorization: (1) the report must be publicly viewable (get_public_report
    raises otherwise), and (2) the file must actually be embedded in one of the
    report's artifacts. This is what lets a published /r/{id} page render an
    embedded image/PDF for a non-authenticated viewer, scoped to that report."""
    import uuid as _uuid
    try:
        _uuid.UUID(file_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="File not found")

    # (1) report is publicly accessible — raises if not.
    await report_service.get_public_report(db, report_id, user=user)

    # (2) file is embedded in one of this report's artifacts.
    from app.models.artifact import Artifact
    artifacts = (await db.execute(
        select(Artifact).where(Artifact.report_id == report_id)
    )).scalars().all()
    embedded = any(
        isinstance(a.content, dict)
        and any(str(f.get("id")) == file_id for f in (a.content.get("files") or []) if isinstance(f, dict))
        for a in artifacts
    )
    if not embedded:
        raise HTTPException(status_code=404, detail="File is not part of this report")

    from app.core.file_tokens import mint_file_token, file_embed_url
    token = mint_file_token(file_id)
    return {"file_id": file_id, "token": token, "url": file_embed_url(file_id, token)}


# --- Public Query/Step Routes (for published reports) ---

from app.schemas.query_schema import PublicQuerySchema
from app.schemas.step_schema import PublicStepSchema


@router.post("/r/{report_id}/run", response_model=ViewerRunResultSchema)
async def viewer_run_report(
    report_id: str,
    artifact_id: str | None = Query(None, description="Run the queries behind this artifact; defaults to the report's latest artifact"),
    db: AsyncSession = Depends(get_async_db),
    user: User = Depends(current_user),
):
    """Re-run a shared artifact's queries as the viewing user.

    Requires authentication (anonymous public viewers cannot run) and the
    same artifact share visibility that gates the /r read endpoints. Results
    are stored per-viewer (step_user_results) — the shared snapshot the owner
    and other viewers see is never modified. Whose credentials execute is
    controlled by the report's shared_run_identity setting.
    """
    return await report_service.viewer_rerun_report_steps(
        db, report_id, user, artifact_id=artifact_id,
    )


@router.get("/r/{report_id}/queries", response_model=List[PublicQuerySchema])
async def get_public_queries(
    report_id: str,
    artifact_id: str | None = Query(None, description="Filter queries to only those used by this artifact"),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Get queries for a shared report."""
    return await report_service.get_public_queries(db, report_id, artifact_id=artifact_id, user=user)


@router.get("/r/{report_id}/queries/{query_id}/step", response_model=PublicStepSchema)
async def get_public_query_step(
    report_id: str,
    query_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Get the default step for a query in a shared report."""
    return await report_service.get_public_step(db, report_id, query_id, user=user)


# --- Public Artifact Routes ---

from app.schemas.artifact_schema import ArtifactListSchema, ArtifactSchema


@router.get("/r/{report_id}/artifacts", response_model=List[ArtifactListSchema])
async def get_public_artifacts(
    report_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """List artifacts for a shared report."""
    return await report_service.get_public_artifacts(db, report_id, user=user)


@router.get("/r/{report_id}/artifacts/{artifact_id}", response_model=ArtifactSchema)
async def get_public_artifact(
    report_id: str,
    artifact_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Get a specific artifact for a shared report."""
    return await report_service.get_public_artifact(db, report_id, artifact_id, user=user)


# One render per report at a time: each export launches a headless Chromium,
# so concurrent clicks (or several viewers exporting the same dashboard) would
# stack browser instances and race on the same uploads/pdfs/{artifact_id}.pdf.
import asyncio as _asyncio
from collections import defaultdict as _defaultdict
_pdf_export_locks: dict[str, "_asyncio.Lock"] = _defaultdict(_asyncio.Lock)

# Upper bound on one export request: waiting for the lock (at most one render
# already in flight) plus our own render. The render itself is capped at
# ReportPdfService.RENDER_TIMEOUT_SECONDS; without this outer bound a request
# queued behind a wedged render would wait forever and the client would sit at
# "Exporting..." with no way to learn it failed.
_PDF_EXPORT_TIMEOUT_SECONDS = 330


@router.get("/r/{report_id}/export_pdf")
async def export_public_report_pdf(
    report_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Download the shared report's dashboard as a PDF.

    Same renderer as the emailed dashboard share (ReportPdfService →
    headless Chromium over the shared snapshot), same visibility gate as the
    other /r read endpoints, and the same viewer-data policy: in
    viewer-identity mode on user-scoped connections the shared snapshot is
    withheld, so generate_for_report refuses and this returns 409.
    """
    # Raises 401/403/404 exactly like the page's other public endpoints.
    schema = await report_service.get_public_report(db, report_id, user=user)

    from app.services.report_pdf_service import ReportPdfService

    async def _generate_locked() -> str | None:
        async with _pdf_export_locks[str(report_id)]:
            return await ReportPdfService().generate_for_report(str(report_id))

    try:
        pdf_path = await _asyncio.wait_for(
            _generate_locked(), timeout=_PDF_EXPORT_TIMEOUT_SECONDS
        )
    except _asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail="PDF export timed out. Please try again.",
        )

    import os
    if not pdf_path or not os.path.isfile(pdf_path):
        raise HTTPException(
            status_code=409,
            detail="PDF export is not available for this report",
        )

    # Defense-in-depth: generate_for_report builds this path from the DB
    # artifact id under UPLOADS_DIR — never from raw request input. Still, rebuild
    # the served path from that fixed root plus only the bare filename, so no
    # directory component of the computed path can point outside the export tree,
    # and confirm the result resolves back inside UPLOADS_DIR before streaming it.
    uploads_root = ReportPdfService.UPLOADS_DIR.resolve()
    safe_pdf = (uploads_root / os.path.basename(pdf_path)).resolve()
    if not safe_pdf.is_relative_to(uploads_root) or not safe_pdf.is_file():
        raise HTTPException(
            status_code=409,
            detail="PDF export is not available for this report",
        )

    from fastapi.responses import FileResponse
    return FileResponse(
        str(safe_pdf),
        media_type="application/pdf",
        filename=f"{schema.title or 'report'}.pdf",
        content_disposition_type="attachment",
    )


@router.get("/r/{report_id}/export_pptx")
async def export_public_report_pptx(
    report_id: str,
    request: Request,
    artifact_id: str | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Download the shared deck as a PowerPoint file.

    The in-app PPTX export lives on the artifact router, which requires a
    signed-in user, so a share-link viewer had no way to reach it. This is the
    report-scoped twin of export_pdf/export_html: same gates, and it serves the
    .pptx that slide generation already wrote to disk — no rendering, so no
    lock or timeout is needed.
    """
    import os
    import re

    from fastapi.responses import FileResponse

    from app.core.path_safety import UnsafePathError, safe_join
    from app.ee.audit.service import audit_service
    from app.models.artifact import Artifact as ArtifactModel
    from app.services.report_pdf_service import ReportPdfService
    from app.services.viewer_data_policy import report_snapshot_withheld

    # Raises 401/403/404 exactly like the page's other public endpoints, and
    # applies the artifact_visibility gate rather than only the report one.
    listed = await report_service.get_public_artifacts(db, report_id, user=user)

    decks = [a for a in listed if (getattr(a, "mode", None) or "page") == "slides"]
    if artifact_id is not None:
        # Only ids this viewer was just shown; never a raw id off the request.
        if not any(str(a.id) == str(artifact_id) for a in decks):
            raise HTTPException(status_code=404, detail="Not found")
        chosen_id = artifact_id
    else:
        if not decks:
            raise HTTPException(
                status_code=409,
                detail="PowerPoint export is not available for this report",
            )
        # get_public_artifacts returns newest first.
        chosen_id = str(decks[0].id)

    # The deck bakes in the shared snapshot exactly as the PDF and HTML exports
    # do, and once downloaded it leaves our control entirely — so this is the
    # last point at which the viewer-data policy can be applied.
    if await report_snapshot_withheld(db, str(report_id)):
        raise HTTPException(
            status_code=409,
            detail="This deck runs with each viewer's own credentials, so it cannot be exported.",
        )

    artifact = await db.get(ArtifactModel, chosen_id)
    if artifact is None or artifact.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Not found")

    # Same gate as the in-app twin: a failed artifact would export stale or
    # broken content.code as a successful download, with nothing telling the
    # viewer the file is built from a render that never completed.
    if artifact.status == "failed":
        raise HTTPException(
            status_code=409,
            detail="This deck failed to generate. Regenerate it before exporting.",
        )

    # The in-app route audits every export; without this, a member who
    # downloads the full snapshot through the share link instead of the in-app
    # button leaves no trail, and a "who exported this dashboard" review misses
    # them entirely. user_id is None for an anonymous share-link viewer, which
    # the column allows — the entry still records that the export happened.
    try:
        await audit_service.log(
            db=db,
            organization_id=artifact.organization_id,
            action="artifact.exported",
            user_id=str(user.id) if user else None,
            resource_type="artifact",
            resource_id=str(artifact.id),
            details={"format": "pptx", "title": artifact.title, "via": "share_link"},
            request=request,
        )
    except Exception:
        pass

    # Serve only a file under the generated-decks root, rebuilt from that fixed
    # root plus the bare filename, so no stored path component can point out of
    # the export tree.
    candidate = artifact.pptx_path or f"{artifact.id}.pptx"
    try:
        safe_pptx = safe_join(ReportPdfService.PPTX_DIR, os.path.basename(candidate))
    except UnsafePathError:
        safe_pptx = None
    if safe_pptx is None or not safe_pptx.is_file():
        raise HTTPException(
            status_code=409,
            detail="PowerPoint export is not available for this report",
        )

    safe_title = (artifact.title or "presentation").encode("ascii", "ignore").decode("ascii")
    safe_title = re.sub(r"[^\w\s-]", "", safe_title).strip() or "presentation"
    return FileResponse(
        str(safe_pptx),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"{safe_title}.pptx",
        content_disposition_type="attachment",
    )


@router.get("/r/{report_id}/export_html")
async def export_public_report_html(
    report_id: str,
    request: Request,
    artifact_id: str | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Download the shared dashboard as one standalone, fully offline HTML file.

    Same bundler as the in-app export (services/html_export_service.py), and
    the same gates as the other /r endpoints: get_public_artifacts enforces the
    report's artifact_visibility, and the shared snapshot is refused in
    viewer-identity mode on user-scoped connections exactly as export_pdf
    refuses it — the file bakes that snapshot in and then leaves our control
    entirely, so this is the last point at which the policy can be applied.
    """
    from urllib.parse import quote

    from fastapi.responses import Response

    from app.ee.audit.service import audit_service
    from app.models.artifact import Artifact as ArtifactModel
    from app.services.html_export_service import (
        ExportUnavailable,
        ascii_fallback_filename,
        build_standalone_html,
        suggested_filename,
    )
    from app.services.viewer_data_policy import report_snapshot_withheld

    # Raises 401/403/404 exactly like the page's other public endpoints, and
    # applies the artifact_visibility gate rather than only the report one.
    listed = await report_service.get_public_artifacts(db, report_id, user=user)

    dashboards = [a for a in listed if (getattr(a, "mode", None) or "page") == "page"]
    if artifact_id is not None:
        # Only ids this viewer was just shown; never a raw id off the request.
        if not any(str(a.id) == str(artifact_id) for a in dashboards):
            raise HTTPException(status_code=404, detail="Not found")
        chosen_id = artifact_id
    else:
        if not dashboards:
            raise HTTPException(
                status_code=409,
                detail="HTML export is not available for this report",
            )
        # get_public_artifacts returns newest first.
        chosen_id = str(dashboards[0].id)

    if await report_snapshot_withheld(db, str(report_id)):
        raise HTTPException(
            status_code=409,
            detail="This dashboard runs with each viewer's own credentials, so it cannot be exported.",
        )

    artifact = await db.get(ArtifactModel, chosen_id)
    if artifact is None or artifact.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Not found")

    # Same gate as the in-app twin: a failed artifact would export stale or
    # broken content.code as a successful download, with nothing telling the
    # viewer the file is built from a render that never completed.
    if artifact.status == "failed":
        raise HTTPException(
            status_code=409,
            detail="This dashboard failed to generate. Regenerate it before exporting.",
        )

    # The in-app route audits every export; without this, a member who
    # downloads the full snapshot through the share link instead of the in-app
    # button leaves no trail, and a "who exported this dashboard" review misses
    # them entirely. user_id is None for an anonymous share-link viewer, which
    # the column allows — the entry still records that the export happened.
    try:
        await audit_service.log(
            db=db,
            organization_id=artifact.organization_id,
            action="artifact.exported",
            user_id=str(user.id) if user else None,
            resource_type="artifact",
            resource_id=str(artifact.id),
            details={"format": "html", "title": artifact.title, "via": "share_link"},
            request=request,
        )
    except Exception:
        pass

    try:
        html = await build_standalone_html(db, artifact)
    except ExportUnavailable as e:
        raise HTTPException(status_code=409, detail=str(e))

    filename = suggested_filename(artifact)
    disposition = (
        f'attachment; filename="{ascii_fallback_filename(filename)}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
        },
    )



@router.post("/r/{report_id}/rerun", response_model=ReportRerunResultSchema)
async def refresh_public_report_on_view(
    report_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Rerun a shared report's queries when a viewer opens its page.

    Unauthenticated by design — it serves the public /r/{id} page — so the
    service enforces opt-in, view permission, a hardcoded staleness gate and a
    single-flight claim. A declined request returns 200 with skipped=true; the
    page simply renders the data it already has.
    """
    return await report_service.refresh_on_view_rerun(db, report_id, user=user)


@router.post("/reports/{report_id}/schedule", response_model=ReportSchema)
@requires_permission('publish_reports', model=Report, owner_only=True)
async def schedule_report(
    report_id: str,
    body: ScheduleRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    subscribers = None
    if body.notification_subscribers is not None:
        subscribers = [s.model_dump() for s in body.notification_subscribers]
    return await report_service.set_report_schedule(
        db, report_id, body.cron_expression, current_user, organization, subscribers,
        refresh_on_view=body.refresh_on_view,
    )

# --- Report Summary ---

@router.get("/reports/{report_id}/notes")
@requires_permission('view_reports', model=Report, owner_only=True, allow_public=True)
async def get_report_notes(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Return the agent's working notes for this report (read-only), newest last."""
    from sqlalchemy import select
    from app.models.note import Note
    from app.schemas.note_schema import NoteSchema
    result = await db.execute(
        select(Note)
        .where(Note.report_id == report_id, Note.deleted_at.is_(None))
        .order_by(Note.created_at.asc())
    )
    notes = result.scalars().all()
    return [NoteSchema.model_validate(n) for n in notes]


@router.get("/reports/{report_id}/summary")
@requires_permission('view_reports', model=Report, owner_only=True)
async def get_report_summary(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Return all query executions and instruction mutations for the report summary panel.

    Unlike completions (which are paginated), this returns the full set so the
    summary sidebar is complete on first load.
    """
    return await report_service.get_report_summary(db, report_id)


# --- Training Mode Instructions ---

@router.get("/reports/{report_id}/instructions")
@requires_permission('view_reports', model=Report, owner_only=True)
async def get_report_instructions(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all instructions created during this report's training sessions.

    Returns instructions that were created by the AI agent during training mode.
    These are the instructions that were auto-generated and published to the
    training session's build.
    """
    from app.services.instruction_service import InstructionService
    instruction_service = InstructionService()
    return await instruction_service.get_instructions_by_report(db, report_id, organization)

# --- Dashboard Layout Routes ---

@router.get("/reports/{report_id}/layouts", response_model=List[DashboardLayoutVersionSchema])
@requires_permission('view_reports', model=Report, owner_only=True)
async def list_layouts(report_id: str, hydrate: bool = False, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await layout_service.get_layouts_for_report(db, report_id, hydrate=hydrate)

@router.post("/reports/{report_id}/layouts", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def create_layout(report_id: str, payload: DashboardLayoutVersionCreate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    # Ensure payload.report_id matches route
    if payload.report_id != report_id:
        raise HTTPException(status_code=400, detail="report_id mismatch")
    return await layout_service.create_layout(db, payload)

@router.get("/reports/{report_id}/layouts/{layout_id}", response_model=DashboardLayoutVersionSchema)
@requires_permission('view_reports', model=Report, owner_only=True)
async def get_layout(report_id: str, layout_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    layout = await layout_service.get_layout(db, layout_id)
    if layout.report_id != report_id:
        raise HTTPException(status_code=404, detail="Layout not found for report")
    return layout

@router.patch("/reports/{report_id}/layouts/{layout_id}", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def update_layout(report_id: str, layout_id: str, payload: DashboardLayoutVersionUpdate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    layout = await layout_service.get_layout(db, layout_id)
    if layout.report_id != report_id:
        raise HTTPException(status_code=404, detail="Layout not found for report")
    return await layout_service.update_layout(db, layout_id, payload, current_user, organization)

@router.patch("/reports/{report_id}/layouts/active/blocks", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def patch_active_layout_blocks(report_id: str, payload: DashboardLayoutBlocksPatch, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await layout_service.patch_active_layout_blocks(db, report_id, payload, current_user, organization)


@router.patch("/reports/{report_id}/layouts/{layout_id}/blocks", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def patch_layout_blocks(report_id: str, layout_id: str, payload: DashboardLayoutBlocksPatch, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await layout_service.patch_layout_blocks(db, report_id, layout_id, payload, current_user, organization)

@router.post("/reports/{report_id}/layouts/{layout_id}/activate", response_model=DashboardLayoutVersionSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def activate_layout(report_id: str, layout_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    layout = await layout_service.get_layout(db, layout_id)
    if layout.report_id != report_id:
        raise HTTPException(status_code=404, detail="Layout not found for report")
    return await layout_service.set_active_layout(db, report_id, layout_id)

# --- Public (read-only) Dashboard Layout Routes ---

@router.get("/r/{report_id}/layouts", response_model=List[DashboardLayoutVersionSchema])
async def get_public_layouts(
    report_id: str,
    hydrate: bool = False,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from app.services.report_service import ReportService
    rs = ReportService()
    # Public service currently returns unhydrated; use private service for hydration
    if hydrate:
        return await layout_service.get_layouts_for_report(db, report_id, hydrate=True)
    return await rs.get_public_layouts(db, report_id, user=user)
