"""Dashboards on their own (docs/design/dashboards-first-class.md, phase 4).

- `/d/{ref}`: the published dashboard, by id or name, for its readers.
- `/d/{ref}/draft`: the draft, for the owner and the project.
- `/artifacts/{dashboard_id}/sharing|settings`: the owner's controls.

The path parameter is `dashboard_id`, not `artifact_id`: the permission
decorator reads `artifact_id` as a version id and resolves its conversation.
"""
import asyncio
import contextlib

from fastapi import APIRouter, Depends, Query, Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user, current_user_optional
from app.core.embed_auth import viewer_optional
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError, ErrorCode
from app.models.organization import Organization
from app.models.user import User
from app.schemas.dashboard_schema import (
    DashboardConnectionKind,
    DashboardForkRequest,
    DashboardListResponse,
    DashboardNotifyRequest,
    DashboardPublishRequest,
    DashboardPublishStatusSchema,
    DashboardSettingsSchema,
    DashboardSettingsUpdate,
    DashboardSharingSchema,
    DashboardSharingUpdate,
    DashboardSourceKind,
    DashboardJoinRequest,
    DraftDashboardSchema,
    PublicGeneralSettings,
    PublishedDashboardSchema,
)
from app.schemas.completion_v2_schema import CompletionCreate
from app.schemas.query_schema import QueryRunRequest
from app.services import dashboard_access, dashboard_settings_service

router = APIRouter(tags=["dashboards"])


async def _general(db, artifact) -> PublicGeneralSettings:
    """The organization's public branding for this dashboard's pages. "Made
    with BOW" shows only when both the organization and the dashboard allow it."""
    from app.models.organization_settings import OrganizationSettings

    settings = (await db.execute(
        select(OrganizationSettings).where(OrganizationSettings.organization_id == str(artifact.organization_id))
    )).scalar_one_or_none()
    general = (settings.config.get("general", {}) or {}) if settings is not None and isinstance(settings.config, dict) else {}
    return PublicGeneralSettings(ai_analyst_name=general.get("ai_analyst_name", "AI Analyst"),
                                 bow_credit=bool(general.get("bow_credit", True)) and bool(getattr(artifact, "bow_credit", True)),
                                 icon_url=general.get("icon_url"))


@router.get("/d/{ref}", response_model=PublishedDashboardSchema)
async def get_published_dashboard(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    from app.services.dashboard_lifecycle import fork_eligibility
    from app.services.dashboard_runs import release_data_sources

    artifact, release = await dashboard_access.open_published(db, ref, user)
    sources = [DashboardSourceKind(connections=[
        DashboardConnectionKind(type=c.type, auth_policy=c.auth_policy) for c in (ds.connections or [])
    ]) for ds in await release_data_sources(db, release)]
    return PublishedDashboardSchema(
        id=str(artifact.id), slug=artifact.slug, title=artifact.title, mode=artifact.mode,
        release_id=str(release.id), published_at=release.published_at, last_run_at=artifact.last_run_at,
        theme_name=release.theme_name, theme_overrides=release.theme_overrides,
        include_data_tab=bool(artifact.include_data_tab), chat_enabled=bool(artifact.chat_enabled),
        general=await _general(db, artifact),
        is_owner=await dashboard_access.counts_as_owner(db, artifact, user),
        data_is_mine=await dashboard_access.holds_snapshot(db, artifact, release, user),
        fork_eligibility=await fork_eligibility(db, artifact, release, user),
        refresh_on_view=bool(artifact.refresh_on_view), data_sources=sources,
        exports_own_rows=await _withheld_from(db, artifact, release, user,
                                              baked=(artifact.mode or "page") == "slides"),
        capabilities=sorted(await dashboard_access.capabilities(db, artifact, user)),
    )


@router.get("/d/{ref}/draft", response_model=DraftDashboardSchema)
async def get_draft_dashboard(
    ref: str,
    version: int | None = Query(None, ge=1, description="A version number to preview (collab01)"),
    draft: str | None = Query(None, max_length=120, description="A named draft's slug; none: the implicit draft"),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from app.models.report import Report
    from app.services import dashboard_collab

    artifact, shown, named = await _draft(db, ref, user, version=version, draft=draft, light=True)
    report_id = str(shown.report_id) if shown is not None else (
        str(artifact.report_id) if artifact.report_id else None)
    report = (await db.execute(
        select(Report.theme_name, Report.theme_overrides, Report.user_id).where(Report.id == report_id)
    )).first() if report_id else None
    drafts = await dashboard_collab.list_drafts(db, artifact)
    current = next((d for d in drafts if d["id"] == (str(named.id) if named is not None else None)), None)
    return DraftDashboardSchema(
        id=str(artifact.id), slug=artifact.slug, title=artifact.title, mode=artifact.mode,
        report_id=report_id,
        version_id=str(shown.id) if shown is not None else None,
        published=await dashboard_access.published_release(db, artifact) is not None,
        theme_name=report[0] if report else None, theme_overrides=report[1] if report else None,
        general=await _general(db, artifact),
        can_edit=await dashboard_access.can_edit(db, artifact, user),
        is_owner=await dashboard_access.counts_as_owner(db, artifact, user),
        capabilities=sorted(await dashboard_access.capabilities(db, artifact, user)),
        version_number=shown.version if shown is not None else None,
        version_author=await dashboard_collab.person(db, report[2]) if report else None,
        version_is_mine=bool(report and user is not None and str(report[2]) == str(user.id)),
        my_report_id=await dashboard_collab.my_conversation(db, artifact, user),
        draft=current, drafts=drafts,
    )


@router.get("/artifacts", response_model=DashboardListResponse)
@requires_permission("view_reports")
async def list_dashboards(
    page: int = Query(1, ge=1),
    limit: int = Query(15, ge=1, le=100),
    filter: str = Query("my", pattern="^(my|shared)$"),
    search: str | None = Query(None, max_length=200),
    mode: str | None = Query(None, pattern="^(page|slides|doc)$"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """The dashboards page (phase 8): one card per dashboard, mine or shared
    with me -- only those whose /d page would open for me."""
    from app.services.dashboard_list import list_dashboards as list_for

    return await list_for(db, current_user, organization, filter=filter, search=search, mode=mode,
                          page=page, limit=limit)


@router.get("/artifacts/{dashboard_id}/sharing", response_model=DashboardSharingSchema)
@requires_permission("publish_reports")
async def get_dashboard_sharing(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await dashboard_settings_service.dashboard_for(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    return await dashboard_settings_service.get_sharing(db, artifact)


@router.put("/artifacts/{dashboard_id}/sharing", response_model=DashboardSharingSchema)
@requires_permission("publish_reports")
async def set_dashboard_sharing(
    dashboard_id: str,
    payload: DashboardSharingUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await dashboard_settings_service.dashboard_for(db, dashboard_id, current_user, organization, dashboard_access.CAP_SHARE)
    return await dashboard_settings_service.set_sharing(
        db, artifact, visibility=payload.visibility, user_ids=payload.user_ids, group_ids=payload.group_ids,
        current_user=current_user, organization=organization, editor_user_ids=payload.editor_user_ids,
        editor_group_ids=payload.editor_group_ids,
    )


@router.post("/artifacts/{dashboard_id}/notify")
@requires_permission("publish_reports")
async def notify_dashboard_share(
    dashboard_id: str,
    payload: DashboardNotifyRequest,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """The share window's notify (phase 8): the email, and to members the
    in-app notice, with the dashboard's own /d link -- by its name when it
    has one. Published dashboards only: sharing waits for Publish."""
    from app.dependencies import _locale_from_org
    from app.ee.audit.service import audit_service
    from app.models.membership import Membership
    from app.schemas.notification_schema import NotificationChannel, NotificationType
    from app.services.email_client_resolver import is_outbound_available
    from app.services.inbox_service import inbox_service
    from app.services.notification_service import notification_service
    from app.settings.config import settings as app_settings

    artifact = await dashboard_settings_service.dashboard_for(db, dashboard_id, current_user, organization, dashboard_access.CAP_SHARE)
    if await dashboard_access.published_release(db, artifact) is None:
        raise AppError.conflict(ErrorCode.DASHBOARD_SHARE_REQUIRES_PUBLISH)
    if NotificationChannel.EMAIL in payload.channels and not await is_outbound_available(
            db, str(organization.id), purpose="system"):
        raise AppError.bad_request(ErrorCode.VALIDATION, "Email notifications are not available (SMTP not configured)")
    share_url = f"{app_settings.bow_config.base_url}/d/{artifact.slug or artifact.id}"
    members = [str(r[0]) for r in (await db.execute(
        select(User.id).join(Membership, Membership.user_id == User.id).where(
            Membership.organization_id == str(organization.id), Membership.deleted_at.is_(None),
            User.email.in_(payload.recipients)))).all()]
    if members:
        try:
            await inbox_service.notify_dashboard_share(db, artifact=artifact, user_ids=members,
                                                       actor_user=current_user)
        except Exception:
            pass  # the email still goes
    release = await dashboard_access.published_release(db, artifact)
    title = (artifact.title or "").strip() or "dashboard"

    made: list[str] = []

    async def attachments() -> list:
        # As the conversation's share email attaches its PDF: the release's,
        # as any reader would export it -- none when each reader sees their own.
        if await _withheld_from(db, artifact, release, None, baked=artifact.mode == "slides"):
            return []
        path = await _release_pdf(db, artifact, release, None)
        if not path:
            return []
        made.append(path)
        return [{"file": path, "filename": f"{title}.pdf", "type": "application", "subtype": "pdf"}]

    from pathlib import Path

    try:
        response = await notification_service.dispatch(
            attachment_builder=attachments,
            notification_type=NotificationType.SHARE_DASHBOARD, channels=payload.channels,
            recipients=payload.recipients, share_url=share_url, report_title=artifact.title,
            sender_name=current_user.name or current_user.email, message=payload.message,
            report_id=str(artifact.id), locale=_locale_from_org(organization),
            db=db, organization_id=str(organization.id),
        )
    finally:
        # The file holds the release's rows: it goes once the email went --
        # or failed -- as the /d export removes its file once sent.
        for path in made:
            Path(path).unlink(missing_ok=True)
    try:
        await audit_service.log(db=db, organization_id=str(organization.id), action="dashboard.notification_sent",
                                user_id=str(current_user.id), resource_type="artifact", resource_id=str(artifact.id),
                                details={"channels": [c.value for c in payload.channels],
                                         "recipient_count": len(payload.recipients)}, request=request)
    except Exception:
        pass
    return response


@router.post("/artifacts/{dashboard_id}/publish", response_model=DashboardPublishStatusSchema, status_code=202)
@requires_permission("publish_reports")
async def publish_dashboard(
    dashboard_id: str,
    payload: DashboardPublishRequest | None = None,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Starts publishing (202); the run continues in the background. Poll GET."""
    from app.core import fire_and_forget
    from app.services import dashboard_publish

    artifact = await dashboard_settings_service.dashboard_for(db, dashboard_id, current_user, organization, dashboard_access.CAP_PUBLISH)
    release = await dashboard_publish.start_publish(db, artifact, current_user, organization,
                                                    version_id=payload.version_id if payload else None,
                                                    force=bool(payload and payload.force))
    fire_and_forget.spawn(dashboard_publish.run_publish(str(release.id)))
    return await dashboard_publish.publish_status(db, artifact)


@router.get("/artifacts/{dashboard_id}/publish", response_model=DashboardPublishStatusSchema)
@requires_permission("publish_reports")
async def get_dashboard_publish_status(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    from app.services import dashboard_publish

    artifact = await dashboard_settings_service.dashboard_for(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    return await dashboard_publish.publish_status(db, artifact)


@router.post("/artifacts/{dashboard_id}/edit")
@requires_permission("publish_reports")
async def edit_dashboard(
    dashboard_id: str,
    payload: DashboardJoinRequest | None = None,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Edit: the caller's own conversation editing the dashboard (collab01)
    -- theirs if they have one, else a new copy of the draft (`draft`: a
    named one), or of the published release when no draft is left. A copy
    of someone else's work runs again with the caller's credentials."""
    from app.core import fire_and_forget
    from app.services import dashboard_collab, dashboard_lifecycle
    from app.services.fork_service import fork_service

    artifact = await dashboard_settings_service.dashboard_for(
        db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    draft = await dashboard_collab.draft_by_slug(db, artifact, payload.draft if payload else None)
    if (draft is None and not await dashboard_collab.editing_report_ids(db, artifact)
            and await dashboard_access.counts_as_owner(db, artifact, current_user)
            and await dashboard_access.published_release(db, artifact) is not None):
        # The owner's Edit of a published dashboard whose conversations were
        # all deleted (phase 7): one conversation however many click at once.
        # Never published (an editor deleted the last conversation): the copy
        # below goes on from its newest version.
        result = await dashboard_lifecycle.edit_dashboard(db, artifact, current_user, organization)
    else:
        result = await dashboard_collab.join(db, artifact, current_user, organization, draft=draft)
    pending = result.get("pending_code") or {}
    if pending:
        fire_and_forget.spawn(fork_service.hydrate_fork(result["report_id"], str(current_user.id),
                                                       str(organization.id), pending, retire=False))
    return {"report_id": result["report_id"], "created": bool(result.get("created")), "rerunning": bool(pending)}


@router.post("/artifacts/{dashboard_id}/refresh")
@requires_permission("publish_reports")
async def refresh_dashboard(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """The owner's Refresh of the published dashboard: every copy runs again
    with their credentials; all of the new rows are written, or none."""
    from app.services import dashboard_refresh

    artifact = await dashboard_settings_service.dashboard_for(db, dashboard_id, current_user, organization, dashboard_access.CAP_PUBLISH)
    return await dashboard_refresh.owner_refresh(db, artifact, current_user, organization)


@router.get("/artifacts/{dashboard_id}/settings", response_model=DashboardSettingsSchema)
@requires_permission("publish_reports")
async def get_dashboard_settings(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await dashboard_settings_service.dashboard_for(db, dashboard_id, current_user, organization, dashboard_access.CAP_EDIT)
    return dashboard_settings_service.get_settings(artifact)


@router.put("/artifacts/{dashboard_id}/settings", response_model=DashboardSettingsSchema)
@requires_permission("publish_reports")
async def set_dashboard_settings(
    dashboard_id: str,
    payload: DashboardSettingsUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await dashboard_settings_service.dashboard_for(db, dashboard_id, current_user, organization, dashboard_access.CAP_SETTINGS)
    return await dashboard_settings_service.set_settings(
        db, artifact, payload.model_dump(), current_user=current_user, organization=organization,
    )


# ── Reading a published dashboard (part B) ───────────────────────────────
# Every route opens the door first (dashboard_access.open_published) and
# reads only the live release. A row that is not the release's answers like
# a missing one.

_EXPORT_FORMATS = {
    "csv": ("text/csv", "csv"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
}
_PDF_EXPORT_TIMEOUT_SECONDS = 330
_pdf_locks: dict[str, tuple[asyncio.Lock, int]] = {}


@contextlib.asynccontextmanager
async def _lock(key: str):
    """One render of `key` at a time in this worker; the entry goes with its
    last user, so the table holds only renders in progress."""
    lock, users = _pdf_locks.get(key, (None, 0))
    lock = lock or asyncio.Lock()
    _pdf_locks[key] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        lock, users = _pdf_locks[key]
        if users <= 1:
            del _pdf_locks[key]
        else:
            _pdf_locks[key] = (lock, users - 1)


async def _withheld_from(db, artifact, release, user, *, baked: bool = False) -> bool:
    """Whether the copied snapshot is withheld from this reader as a whole
    (the owner always gets their own view). `baked`: the output pictures
    every chart's stored rows (a deck, its PDF, slide images), so one chart
    holding one identity's slice withholds it too."""
    from app.services.dashboard_access import holds_snapshot
    from app.services.dashboard_data import any_copy_tainted, release_snapshot_withheld

    if await holds_snapshot(db, artifact, release, user):
        return False
    if await release_snapshot_withheld(db, artifact, release):
        return True
    return baked and await any_copy_tainted(db, release)


async def _has_own_run(db, release, user) -> bool:
    """Whether `user` holds a successful run of their own of any query of the
    release -- what a withheld export then holds instead of the snapshot."""
    from app.models.query import Query
    from app.models.step import Step
    from app.models.step_user_result import StepUserResult
    from app.services.dashboard_runs import needs_input_names

    if user is None:
        return False
    rows = (await db.execute(
        select(StepUserResult.data).join(Step, Step.id == StepUserResult.step_id)
        .join(Query, Query.id == Step.query_id)
        .where(Query.artifact_release_id == str(release.id), StepUserResult.user_id == str(user.id),
               StepUserResult.status == "success")
    )).scalars().all()
    # A wait for input is not a run of theirs (resolve_release_step): it holds no rows. The marker
    # is inside the encrypted data, so it is read here, not in SQL.
    return any(not needs_input_names(data) for data in rows)


async def _release_pdf(db, artifact, release, user) -> str | None:
    """The release's PDF as `user` (None: any reader) would export it from
    /d; the caller has checked it is not withheld. Raises TimeoutError."""
    import os

    from app.core.path_safety import UnsafePathError, safe_join
    from app.services import dashboard_data
    from app.services.report_pdf_service import ReportPdfService

    render = dashboard_data.ReleaseRender.build(artifact, release, user)
    if (artifact.mode or "page") == "slides":
        render.pptx_path = None
        try:
            deck = safe_join(ReportPdfService.PPTX_DIR, os.path.basename(release.pptx_path or ""))
            render.pptx_path = str(deck) if release.pptx_path and deck.is_file() else None
        except UnsafePathError:
            pass
    service = ReportPdfService()

    async def _locked():
        # One render of a release at a time, as /r renders one per report.
        async with _lock(str(release.id)):
            return await service.generate_for_release(db, render)

    return await asyncio.wait_for(_locked(), timeout=_PDF_EXPORT_TIMEOUT_SECONDS)


async def _audit_export(db, artifact, user, fmt, request, *, step=None, row_count=None):
    """As the conversation's exports audit: a dashboard file is
    'artifact.exported', a chart's rows 'data.exported'."""
    try:
        from app.ee.audit.service import audit_service

        if step is not None:
            await audit_service.log(db=db, organization_id=artifact.organization_id, action="data.exported",
                                    user_id=str(user.id) if user else None, resource_type="step",
                                    resource_id=str(step.id),
                                    details={"format": fmt, "row_count": row_count, "via": "dashboard_link"},
                                    request=request)
            return
        await audit_service.log(db=db, organization_id=artifact.organization_id, action="artifact.exported",
                                user_id=str(user.id) if user else None, resource_type="artifact",
                                resource_id=str(artifact.id),
                                details={"format": fmt, "title": artifact.title, "via": "dashboard_link"},
                                request=request)
    except Exception:
        pass


@router.get("/d/{ref}/content")
async def get_published_content(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    artifact, release = await dashboard_access.open_published(db, ref, user)
    return _with_compiled({"mode": artifact.mode, "content": release.content or {}})


def _with_compiled(body: dict) -> dict:
    """compiled_js / compiled_js_key for the page's code, as the artifact read
    routes carry them (app.services.artifact_precompile): derived only from
    the code this reader already gets; absent on a cache miss."""
    from types import SimpleNamespace

    from app.services.artifact_precompile import attach_compiled

    holder = SimpleNamespace(mode=body.get("mode"), content=body.get("content"),
                             compiled_js=None, compiled_js_key=None)
    attach_compiled(holder)
    if holder.compiled_js:
        body["compiled_js"] = holder.compiled_js
        body["compiled_js_key"] = holder.compiled_js_key
    return body


@router.get("/d/{ref}/queries")
async def list_published_queries(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    from app.schemas.query_schema import PublicQuerySchema
    from app.services import dashboard_data

    _, release = await dashboard_access.open_published(db, ref, user)
    return [PublicQuerySchema.model_validate(q) for q in await dashboard_data.release_queries(db, release)]


@router.get("/d/{ref}/queries/{query_id}/step")
async def get_published_step(
    ref: str,
    query_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    from app.services import dashboard_data

    artifact, release = await dashboard_access.open_published(db, ref, user)
    query = await dashboard_data.release_query(db, release, query_id)
    out = await _published_step(db, artifact, release, query, user)
    if out is None:
        raise dashboard_data.not_found()
    return out


async def _published_step(db, artifact, release, query, user):
    """The step GET /d/{ref}/queries/{id}/step serves, or None where it 404s."""
    from app.services import dashboard_data

    step = await dashboard_data.default_step(db, query)
    if step is None:
        return None
    resolution = await dashboard_data.resolve_release_step(db, step, artifact, release, user)
    return await dashboard_data.public_step(db, step, query, resolution,
                                            organization_id=artifact.organization_id, user=user)


@router.get("/d/{ref}/queries_with_steps")
async def list_published_queries_with_steps(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """GET /d/{ref}/queries + each query's step in one response.

    `default_step` is exactly what /d/{ref}/queries/{id}/step returns for the
    same reader (same helper), or null where that route would 404.
    """
    from app.schemas.query_schema import PublicQuerySchema
    from app.services import dashboard_data

    artifact, release = await dashboard_access.open_published(db, ref, user)
    out = []
    queries = await dashboard_data.release_queries(db, release)
    # Encoded before any step sets the code-visibility context, as the list
    # route's response is.
    listed = [jsonable_encoder(PublicQuerySchema.model_validate(q)) for q in queries]
    for q, row in zip(queries, listed):
        step = await _published_step(db, artifact, release, q, user)
        out.append({**row, "default_step": jsonable_encoder(step) if step is not None else None})
    return out


@router.get("/d/{ref}/queries/{query_id}/export")
async def export_published_step(
    ref: str,
    query_id: str,
    request: Request,
    format: str = Query("csv"),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """The Data tab's download: the rows the reader sees on the page."""
    import io
    from urllib.parse import quote

    from fastapi.responses import Response

    from app.services import dashboard_data
    from app.services.step_service import StepService

    fmt = (format or "csv").lower()
    if fmt not in _EXPORT_FORMATS:
        raise AppError.bad_request(ErrorCode.VALIDATION, "Use 'csv' or 'xlsx'")
    artifact, release = await dashboard_access.open_published(db, ref, user)
    query = await dashboard_data.release_query(db, release, query_id)
    step = await dashboard_data.default_step(db, query)
    if step is None:
        raise dashboard_data.not_found()
    resolution = await dashboard_data.resolve_release_step(db, step, artifact, release, user)
    if resolution.withheld:
        raise AppError.forbidden(ErrorCode.ACCESS_DENIED,
                                 "This dashboard shows data based on your access — run it to export your own data")
    df = StepService()._df_from_step_data(resolution.data)
    if fmt == "xlsx":
        buffer = io.BytesIO()
        df.to_excel(buffer, index=False)
        content = buffer.getvalue()
    else:
        content = df.to_csv(index=False).encode("utf-8-sig")
    await _audit_export(db, artifact, user, fmt, request, step=step, row_count=len(df))
    media_type, extension = _EXPORT_FORMATS[fmt]
    name = f"{(query.title or 'data').strip() or 'data'}.{extension}"
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f"attachment; filename=\"data.{extension}\"; "
                                                    f"filename*=UTF-8''{quote(name)}"})


@router.get("/d/{ref}/files/{file_id}/embed_token")
async def get_published_file_embed_token(
    ref: str,
    file_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """A token for a file the published version embeds, and nothing else."""
    from app.core.file_tokens import file_embed_url, mint_file_token
    from app.services import dashboard_data

    artifact, release = await dashboard_access.open_published(db, ref, user)
    if str(file_id) not in dashboard_data.file_ids(release.content or {}):
        raise dashboard_data.not_found()
    # Anyone outside the organization got here through the public link; their
    # token loses access with it (see get_file_embed).
    member = user is not None and await dashboard_access._is_member(db, user, artifact.organization_id)
    token = mint_file_token(str(file_id), organization_id=str(release.organization_id),
                            release_id=str(release.id), via_public_link=not member)
    return {"file_id": str(file_id), "token": token, "url": file_embed_url(str(file_id), token)}


@router.get("/d/{ref}/previews")
async def list_published_previews(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    artifact, release = await dashboard_access.open_published(db, ref, user)
    if await _withheld_from(db, artifact, release, user, baked=True):
        return {"count": 0}  # the previews picture the owner's data
    return {"count": len((release.content or {}).get("preview_images") or [])}


@router.get("/d/{ref}/previews/{index}")
async def get_published_preview(
    ref: str,
    index: int,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    from pathlib import Path

    from fastapi.responses import FileResponse

    from app.core.path_safety import UnsafePathError, safe_join
    from app.services import dashboard_data

    artifact, release = await dashboard_access.open_published(db, ref, user)
    images = (release.content or {}).get("preview_images") or []
    if index < 0 or index >= len(images) or await _withheld_from(db, artifact, release, user, baked=True):
        raise dashboard_data.not_found()
    # Only the release's own copies (copy_dashboard writes them under
    # release-<id>/); a path it did not copy is not served.
    own_dir = Path(__file__).parent.parent.parent / "uploads" / "pptx_previews" / f"release-{release.id}"
    try:
        path = safe_join(own_dir, str(images[index]).removeprefix(f"pptx_previews/release-{release.id}/"))
    except UnsafePathError:
        raise dashboard_data.not_found()
    if not path.is_file() or path.suffix.lower() != ".png":
        raise dashboard_data.not_found()
    return FileResponse(path=str(path), media_type="image/png")


@router.get("/d/{ref}/export/{fmt}")
async def export_published_dashboard(
    ref: str,
    fmt: str,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """PDF, PowerPoint or standalone HTML of the published version, holding
    the rows this reader sees (PR #1304's rule for public outputs)."""
    import os
    import re
    from urllib.parse import quote

    from fastapi.responses import FileResponse, Response

    from app.core.path_safety import UnsafePathError, safe_join
    from app.services import dashboard_data
    from app.services.report_pdf_service import ReportPdfService

    if fmt not in ("pdf", "pptx", "html"):
        raise dashboard_data.not_found()
    artifact, release = await dashboard_access.open_published(db, ref, user)
    mode = artifact.mode or "page"
    if (fmt == "html" and mode != "page") or (fmt == "pptx" and mode != "slides"):
        raise AppError.conflict(ErrorCode.DASHBOARD_EXPORT_UNAVAILABLE, f"This {mode} cannot be exported as {fmt}")
    if await _withheld_from(db, artifact, release, user, baked=mode == "slides"):
        # A page or doc is drawn for this request, from the rows this reader
        # sees: with the snapshot withheld, their own run's (ReleaseRender's
        # rows_for). A deck was drawn once, from the owner's rows.
        if mode == "slides" or user is None:
            raise AppError.conflict(ErrorCode.DASHBOARD_EXPORT_WITHHELD,
                                    "This dashboard shows each viewer their own data, so it cannot be exported.")
        if not await _has_own_run(db, release, user):
            raise AppError.conflict(ErrorCode.DASHBOARD_EXPORT_NEEDS_RUN,
                                    "Run the dashboard first to export your data.")
    render = dashboard_data.ReleaseRender.build(artifact, release, user)
    title = (artifact.title or "").strip() or "dashboard"

    if fmt == "pptx":
        try:
            deck = safe_join(ReportPdfService.PPTX_DIR, os.path.basename(release.pptx_path or ""))
        except UnsafePathError:
            deck = None
        if deck is None or not release.pptx_path or not deck.is_file():
            raise AppError.conflict(ErrorCode.DASHBOARD_EXPORT_UNAVAILABLE,
                                    "PowerPoint export is not available for this dashboard")
        await _audit_export(db, artifact, user, fmt, request)
        ascii_title = re.sub(r"[^\w\s-]", "", title.encode("ascii", "ignore").decode("ascii")).strip() or "presentation"
        return FileResponse(str(deck), filename=f"{ascii_title}.pptx", content_disposition_type="attachment",
                            media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")

    if fmt == "html":
        from app.services.html_export_service import ExportUnavailable, ascii_fallback_filename, build_standalone_html

        try:
            html = await build_standalone_html(db, render)
        except ExportUnavailable as e:
            raise AppError.conflict(ErrorCode.DASHBOARD_EXPORT_UNAVAILABLE, str(e))
        await _audit_export(db, artifact, user, fmt, request)
        filename = f"{title}.html"
        return Response(content=html, media_type="text/html; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{ascii_fallback_filename(filename)}"; '
                                   f"filename*=UTF-8''{quote(filename)}",
            "Cache-Control": "no-store"})

    try:
        pdf_path = await _release_pdf(db, artifact, release, user)
    except TimeoutError:
        raise AppError(ErrorCode.INTERNAL, "PDF export timed out. Please try again.", status_code=504)
    uploads_root = ReportPdfService.UPLOADS_DIR.resolve()
    safe_pdf = (uploads_root / os.path.basename(pdf_path or "")).resolve() if pdf_path else None
    if safe_pdf is None or not safe_pdf.is_relative_to(uploads_root) or not safe_pdf.is_file():
        raise AppError.conflict(ErrorCode.DASHBOARD_EXPORT_UNAVAILABLE, "PDF export is not available for this dashboard")
    await _audit_export(db, artifact, user, fmt, request)
    from starlette.background import BackgroundTask

    # The file holds this reader's rows: it goes once it is sent.
    return FileResponse(str(safe_pdf), media_type="application/pdf", filename=f"{title}.pdf",
                        content_disposition_type="attachment",
                        background=BackgroundTask(safe_pdf.unlink, missing_ok=True))


# ── Running a published dashboard (part C) ───────────────────────────────

# ── readers' chat (phase 7) ──────────────────────────────────────────────

@router.get("/d/{ref}/chat")
async def get_dashboard_chat_status(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """Availability and scope for the chat bubble, as /r/{id}/chat answers
    it. Never creates anything."""
    from fastapi import HTTPException

    from app.models.data_source import DataSource
    from app.models.organization import Organization
    from app.services import dashboard_chat

    try:
        artifact, release = await dashboard_chat.open_chat(db, ref, user)
    except (HTTPException, AppError) as e:
        if user is None and e.status_code == 401:
            return {"enabled": True, "available": False, "reason": "auth_required"}
        detail = str(getattr(e, "detail", "") or e)
        if e.status_code == 403 and user is not None and ("not enabled" in detail or "members of" in detail):
            reason = "disabled" if "not enabled" in detail else "not_member"
            return {"enabled": reason != "disabled", "available": False, "reason": reason}
        raise
    organization = await db.get(Organization, str(artifact.organization_id))
    agent_ids = await dashboard_chat.effective_agent_ids(db, artifact, release, organization, user)
    agents = []
    for aid in agent_ids:
        ds = await db.get(DataSource, aid)
        if ds is not None:
            agents.append({"id": aid, "name": ds.name})
    thread = await dashboard_chat.find_thread(db, artifact, user)
    return {"enabled": True, "available": True, "reason": None,
            "scope": "agents" if agent_ids else "data_only", "agents": agents,
            "chat_report_id": str(thread.id) if thread else None}


@router.post("/d/{ref}/chat/completions")
async def create_dashboard_chat_completion(
    ref: str,
    completion: CompletionCreate,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """A message into the reader's own thread on this dashboard. Streams SSE.
    Access, roster, model and context are re-resolved on every message."""
    from fastapi import HTTPException

    from app.models.organization import Organization
    from app.services import dashboard_chat
    from app.services.completion_service import CompletionService

    artifact, release = await dashboard_chat.open_chat(db, ref, user)
    organization = await db.get(Organization, str(artifact.organization_id))
    agent_ids = await dashboard_chat.effective_agent_ids(db, artifact, release, organization, user)
    thread = await dashboard_chat.resolve_thread(db, artifact, user, agent_ids)
    if completion.prompt is None or not (completion.prompt.content or "").strip():
        raise HTTPException(status_code=400, detail="Message content is required")
    completion.prompt.widget_id = None
    completion.prompt.step_id = None
    completion.prompt.mode = "chat"
    completion.prompt.platform = "artifact_chat"
    completion.prompt.platform_context = await dashboard_chat.build_context(db, artifact, release, agent_ids, user)
    # The host's current context (the bubble inside a host application):
    # advisory prompt text in platform_context only, never kept on the prompt
    # and never used for access or queries.
    host_context = dashboard_chat.sanitize_host_context(completion.prompt.host_context)
    if host_context:
        completion.prompt.platform_context["host_context"] = host_context
    completion.prompt.host_context = None
    try:
        completion.queue = False
    except Exception:
        pass
    return await CompletionService().create_completion_stream(db, str(thread.id), completion, user, organization)


@router.get("/d/{ref}/chat/completions")
async def get_dashboard_chat_completions(
    ref: str,
    limit: int = 20,
    before: str | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """The reader's own thread on this dashboard. Empty before the first message."""
    from app.models.organization import Organization
    from app.services import dashboard_chat
    from app.services.completion_service import CompletionService

    artifact, _ = await dashboard_chat.open_chat(db, ref, user)
    thread = await dashboard_chat.find_thread(db, artifact, user)
    if thread is None:
        return {"report_id": None, "completions": [], "total_completions": 0}
    organization = await db.get(Organization, str(artifact.organization_id))
    return await CompletionService().get_completions_v2(db, str(thread.id), organization, user, limit=limit,
                                                        before=before)


@router.get("/d/{ref}/chat/completions/{completion_id}/stream")
async def watch_dashboard_chat_completion(
    ref: str,
    completion_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """Re-attachable SSE watch for a completion in the reader's own thread."""
    from fastapi import HTTPException

    from app.models.organization import Organization
    from app.services import dashboard_chat
    from app.services.completion_service import CompletionService

    artifact, _ = await dashboard_chat.open_chat(db, ref, user)
    thread = await dashboard_chat.find_thread(db, artifact, user)
    if thread is None:
        raise HTTPException(status_code=404, detail="No chat thread")
    organization = await db.get(Organization, str(artifact.organization_id))
    return await CompletionService().watch_completion_stream(db, str(thread.id), completion_id, user, organization)


@router.post("/d/{ref}/fork")
async def fork_published_dashboard(
    ref: str,
    payload: DashboardForkRequest | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """Fork of the published dashboard into the reader's own (phase 7)."""
    from app.core import fire_and_forget
    from app.services import dashboard_lifecycle
    from app.services.fork_service import fork_service

    if user is None:
        raise AppError.unauthorized()
    artifact, release = await dashboard_access.open_published(db, ref, user)
    title = payload.title if payload else None
    forked = await dashboard_lifecycle.fork_release(db, artifact, release, user, title=title)
    if forked["pending_code"]:
        # Over a personal connection the code runs again as the forker, as
        # a conversation's Fork does.
        fire_and_forget.spawn(fork_service.hydrate_fork(forked["report_id"], str(user.id),
                                                       str(artifact.organization_id), forked["pending_code"]))
    return {"report_id": forked["report_id"], "artifact_id": forked["artifact_id"]}


@router.post("/d/{ref}/refresh")
async def refresh_published_dashboard_on_view(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """A reader opened the dashboard: refresh it as the owner, unless it is
    off, fresh, being published or already refreshing (phase 6)."""
    from app.services import dashboard_refresh

    artifact, _ = await dashboard_access.open_published(db, ref, user)
    return await dashboard_refresh.refresh_on_view(db, artifact)


@router.post("/d/{ref}/run")
async def run_published_dashboard(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """The reader re-runs the release's charts; results are theirs alone."""
    from app.services import dashboard_runs

    if user is None:
        raise AppError.unauthorized()
    artifact, release = await dashboard_access.open_published(db, ref, user)
    return await dashboard_runs.viewer_run(db, artifact, release, user)


@router.post("/d/{ref}/queries/{query_id}/run")
async def run_published_query(
    ref: str,
    query_id: str,
    payload: QueryRunRequest,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(viewer_optional),
):
    """One chart with the reader's filter values, cached per reader and values."""
    from app.services import dashboard_data, dashboard_runs

    if user is None:
        raise AppError.unauthorized()
    artifact, release = await dashboard_access.open_published(db, ref, user)
    query = await dashboard_data.release_query(db, release, query_id)
    return await dashboard_runs.filter_run(db, artifact, release, query, user, payload)


# ── Reading the draft (part B) ───────────────────────────────────────────

async def _draft(db, ref, user, *, version: int | None = None, draft: str | None = None, light: bool = False):
    """The draft door, and the version shown: `version` (a number) when it
    is one of a live editing conversation's, else `draft`'s newest (None:
    the implicit draft's). Returns (artifact, version, named draft)."""
    from app.services import dashboard_collab, dashboard_data

    artifact = await dashboard_access.open_draft(db, ref, user)
    named = await dashboard_collab.draft_by_slug(db, artifact, draft)
    if version is not None:
        from sqlalchemy.orm import lazyload

        from app.models.artifact import ArtifactVersion

        stmt = select(ArtifactVersion)
        if light:
            stmt = stmt.options(lazyload("*"))
        shown = (await db.execute(stmt.where(
            ArtifactVersion.artifact_id == str(artifact.id), ArtifactVersion.version == int(version),
            ArtifactVersion.status == "completed", ArtifactVersion.deleted_at.is_(None),
            ArtifactVersion.report_id.in_(await dashboard_collab.editing_report_ids(db, artifact)),
        ))).scalar_one_or_none()
        if shown is None:
            raise dashboard_data.not_found()
    else:
        shown = await dashboard_data.draft_version(db, artifact, light=light,
                                                   draft_id=str(named.id) if named is not None else None)
    if shown is not None:
        # open_draft checked one editing conversation; the version shown may
        # live in another's, with its own monitoring lineage (#1455 review, 3).
        from app.services.bow_source_access import assert_read, report_access

        await assert_read(db, await report_access(db, shown.report_id), user)
    return artifact, shown, named


@router.get("/d/{ref}/draft/content")
async def get_draft_content(
    ref: str,
    version: int | None = Query(None, ge=1),
    draft: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from app.services import dashboard_data

    artifact, version, _ = await _draft(db, ref, user, version=version, draft=draft)
    if version is None:
        raise dashboard_data.not_found()
    return _with_compiled({"mode": artifact.mode, "version_id": str(version.id), "content": version.content or {}})


@router.get("/d/{ref}/draft/queries")
async def list_draft_queries(
    ref: str,
    version: int | None = Query(None, ge=1),
    draft: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from sqlalchemy.orm import lazyload, selectinload

    from app.models.query import Query as QueryModel
    from app.models.step import Step
    from app.schemas.query_schema import PublicQuerySchema
    from app.services import dashboard_data

    artifact, version, _ = await _draft(db, ref, user, version=version, draft=draft)
    return [PublicQuerySchema.model_validate(q) for q in await _draft_queries(db, version)]


async def _draft_queries(db, version):
    from sqlalchemy.orm import lazyload, selectinload

    from app.models.query import Query as QueryModel
    from app.models.step import Step
    from app.services import dashboard_data

    ids = await dashboard_data.draft_query_ids(db, version)
    if not ids:
        return []
    from app.services.fork_service import FORK_PENDING_STATUS

    queries = (await db.execute(
        select(QueryModel).join(Step, Step.id == QueryModel.default_step_id)
        .options(lazyload("*"), selectinload(QueryModel.visualizations).options(lazyload("*")))
        # A joining editor's copies are 'pending' until they ran with their
        # credentials (collab01): listed, so the page waits on them.
        .where(QueryModel.id.in_(ids), QueryModel.report_id == str(version.report_id),
               QueryModel.deleted_at.is_(None), Step.status.in_(("success", FORK_PENDING_STATUS)))
    )).scalars().all()
    return list(queries)


@router.get("/d/{ref}/draft/queries/{query_id}/step")
async def get_draft_step(
    ref: str,
    query_id: str,
    version: int | None = Query(None, ge=1),
    draft: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from sqlalchemy.orm import lazyload

    from app.models.query import Query as QueryModel
    from app.services import dashboard_data
    from app.services.viewer_data_policy import resolve_step_data

    artifact, version, _ = await _draft(db, ref, user, version=version, draft=draft)
    if str(query_id) not in await dashboard_data.draft_query_ids(db, version):
        raise dashboard_data.not_found()
    query = (await db.execute(
        select(QueryModel).options(lazyload("*")).where(QueryModel.id == str(query_id),
                                                        QueryModel.report_id == str(version.report_id))
    )).scalar_one_or_none()
    out = await _draft_step(db, artifact, version, query, user)
    if out is None:
        raise dashboard_data.not_found()
    return out


async def _draft_step(db, artifact, version, query, user):
    """The step GET /d/{ref}/draft/queries/{id}/step serves for a query of
    the shown version, or None where it 404s."""
    from app.services import dashboard_data
    from app.services.viewer_data_policy import resolve_step_data

    step = await dashboard_data.default_step(db, query) if query is not None else None
    if step is None:
        pending = await dashboard_data.pending_step(db, query) if query is not None else None
        if pending is not None:
            # Still running with this editor's credentials (collab01): the
            # chart waits instead of showing nothing.
            return {"id": str(pending.id), "title": pending.title or "", "type": pending.type or "table",
                    "code": None, "data_model": {}, "data": {}, "view": {}, "viewer_result": None,
                    "snapshot_withheld": False, "applied_params": None, "pending": True}
        return None
    report = await dashboard_data.version_report(db, version)
    resolution = await resolve_step_data(db, step, report, user)
    return await dashboard_data.public_step(db, step, query, resolution,
                                            organization_id=artifact.organization_id, user=user)


@router.get("/d/{ref}/draft/queries_with_steps")
async def list_draft_queries_with_steps(
    ref: str,
    version: int | None = Query(None, ge=1),
    draft: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """GET /d/{ref}/draft/queries + each query's step in one response; each
    `default_step` is what the per-query draft step route returns for the
    same reader, or null where it would 404."""
    from app.schemas.query_schema import PublicQuerySchema

    artifact, version, _ = await _draft(db, ref, user, version=version, draft=draft)
    out = []
    queries = await _draft_queries(db, version)
    listed = [jsonable_encoder(PublicQuerySchema.model_validate(q)) for q in queries]
    for q, row in zip(queries, listed):
        step = await _draft_step(db, artifact, version, q, user)
        out.append({**row, "default_step": jsonable_encoder(step) if step is not None else None})
    return out


@router.post("/d/{ref}/draft/queries/{query_id}/run")
async def run_draft_query(
    ref: str,
    query_id: str,
    payload: QueryRunRequest,
    version: int | None = Query(None, ge=1),
    draft: str | None = Query(None, max_length=120),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    """A filter on the draft page (collab01): one chart of the version shown,
    with the reader's values and credentials, cached as theirs alone. The
    draft door admits them -- the version may live in another editor's
    conversation, whose own sharing is not asked."""
    from app.services import dashboard_data
    from app.services.query_service import QueryService

    if user is None:
        raise AppError.unauthorized()
    artifact, shown, _ = await _draft(db, ref, user, version=version, draft=draft, light=True)
    if shown is None or str(query_id) not in await dashboard_data.draft_query_ids(db, shown):
        raise dashboard_data.not_found()
    # Parameter values only: no code, no view-as.
    request = payload.model_copy(update={"mode": "viewer", "code": None, "run_as_user_id": None})
    try:
        return await QueryService().run_query_viewer(db, str(query_id), request,
                                                     organization_id=str(artifact.organization_id),
                                                     user_id=str(user.id), reader_credentials=True)
    except ValueError as e:
        raise AppError.bad_request(ErrorCode.VALIDATION, str(e))


@router.get("/d/{ref}/draft/files/{file_id}/embed_token")
async def get_draft_file_embed_token(
    ref: str,
    file_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from app.core.file_tokens import file_embed_url, mint_file_token
    from app.services import dashboard_data

    _, version, _ = await _draft(db, ref, user)
    if version is None or str(file_id) not in dashboard_data.file_ids(version.content or {}):
        raise dashboard_data.not_found()
    # The draft lives in its conversation: bound to it, as /r embeds are.
    token = mint_file_token(str(file_id), organization_id=str(version.organization_id),
                            report_id=str(version.report_id))
    return {"file_id": str(file_id), "token": token, "url": file_embed_url(str(file_id), token)}


# ── Old links (part B) ───────────────────────────────────────────────────

@router.get("/r/{report_id}/dashboard")
async def get_legacy_link_target(report_id: str, db: AsyncSession = Depends(get_async_db),
                                 user: User | None = Depends(current_user_optional)):
    """The dashboard an old /r/{report_id} link leads to. It reveals only an
    id; the dashboard's own /d check decides who opens it. None: /r keeps
    serving the conversation as before (no dashboard -- widget era)."""
    from app.services.dashboard_legacy import link_target

    dashboard_id = await link_target(db, report_id, user)
    if dashboard_id is None:
        from app.services import dashboard_data
        raise dashboard_data.not_found()
    return {"dashboard_id": dashboard_id}
