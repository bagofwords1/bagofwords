"""Dashboards on their own (docs/design/dashboards-first-class.md, phase 4).

- `/d/{ref}`: the published dashboard, by id or name, for its readers.
- `/d/{ref}/draft`: the draft, for the owner and the project.
- `/dashboards/{dashboard_id}/sharing|settings`: the owner's controls.

The path parameter is `dashboard_id`, not `artifact_id`: the permission
decorator reads `artifact_id` as a version id and resolves its conversation.
"""
import asyncio

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user, current_user_optional
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError, ErrorCode
from app.models.artifact import ArtifactVersion
from app.models.organization import Organization
from app.models.user import User
from app.schemas.dashboard_schema import (
    DashboardSettingsSchema,
    DashboardSettingsUpdate,
    DashboardSharingSchema,
    DashboardSharingUpdate,
    DraftDashboardSchema,
    PublicGeneralSettings,
    PublishedDashboardSchema,
)
from app.schemas.query_schema import QueryRunRequest
from app.services import dashboard_access, dashboard_settings_service

router = APIRouter(tags=["dashboards"])


async def _general(db, organization_id) -> PublicGeneralSettings | None:
    from app.models.organization_settings import OrganizationSettings

    settings = (await db.execute(
        select(OrganizationSettings).where(OrganizationSettings.organization_id == str(organization_id))
    )).scalar_one_or_none()
    if settings is None or not isinstance(settings.config, dict):
        return None
    general = settings.config.get("general", {}) or {}
    return PublicGeneralSettings(ai_analyst_name=general.get("ai_analyst_name", "AI Analyst"),
                                 bow_credit=general.get("bow_credit", True), icon_url=general.get("icon_url"))


@router.get("/d/{ref}", response_model=PublishedDashboardSchema)
async def get_published_dashboard(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    artifact, release = await dashboard_access.open_published(db, ref, user)
    return PublishedDashboardSchema(
        id=str(artifact.id), slug=artifact.slug, title=artifact.title, mode=artifact.mode,
        release_id=str(release.id), published_at=release.published_at, last_run_at=artifact.last_run_at,
        theme_name=release.theme_name, theme_overrides=release.theme_overrides,
        include_data_tab=bool(artifact.include_data_tab), chat_enabled=bool(artifact.chat_enabled),
        general=await _general(db, artifact.organization_id),
    )


@router.get("/d/{ref}/draft", response_model=DraftDashboardSchema)
async def get_draft_dashboard(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from app.models.report import Report

    artifact = await dashboard_access.open_draft(db, ref, user)
    version_id = (await db.execute(
        select(ArtifactVersion.id).where(
            ArtifactVersion.artifact_id == str(artifact.id),
            ArtifactVersion.deleted_at.is_(None),
            ArtifactVersion.status == "completed",
        ).order_by(ArtifactVersion.version.desc()).limit(1)
    )).scalar_one_or_none()
    theme = (await db.execute(
        select(Report.theme_name, Report.theme_overrides).where(Report.id == str(artifact.report_id))
    )).first() if artifact.report_id else None
    return DraftDashboardSchema(
        id=str(artifact.id), slug=artifact.slug, title=artifact.title, mode=artifact.mode,
        report_id=str(artifact.report_id) if artifact.report_id else None,
        version_id=str(version_id) if version_id else None,
        published=await dashboard_access.published_release(db, artifact) is not None,
        theme_name=theme[0] if theme else None, theme_overrides=theme[1] if theme else None,
    )


@router.get("/dashboards/{dashboard_id}/sharing", response_model=DashboardSharingSchema)
@requires_permission("publish_reports")
async def get_dashboard_sharing(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await dashboard_settings_service.owned_dashboard(db, dashboard_id, current_user, organization)
    return await dashboard_settings_service.get_sharing(db, artifact)


@router.put("/dashboards/{dashboard_id}/sharing", response_model=DashboardSharingSchema)
@requires_permission("publish_reports")
async def set_dashboard_sharing(
    dashboard_id: str,
    payload: DashboardSharingUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await dashboard_settings_service.owned_dashboard(db, dashboard_id, current_user, organization)
    return await dashboard_settings_service.set_sharing(
        db, artifact, visibility=payload.visibility, user_ids=payload.user_ids, group_ids=payload.group_ids,
        current_user=current_user, organization=organization,
    )


@router.get("/dashboards/{dashboard_id}/settings", response_model=DashboardSettingsSchema)
@requires_permission("publish_reports")
async def get_dashboard_settings(
    dashboard_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await dashboard_settings_service.owned_dashboard(db, dashboard_id, current_user, organization)
    return dashboard_settings_service.get_settings(artifact)


@router.put("/dashboards/{dashboard_id}/settings", response_model=DashboardSettingsSchema)
@requires_permission("publish_reports")
async def set_dashboard_settings(
    dashboard_id: str,
    payload: DashboardSettingsUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    artifact = await dashboard_settings_service.owned_dashboard(db, dashboard_id, current_user, organization)
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
_pdf_locks: dict[str, asyncio.Lock] = {}


def _lock(key: str):
    return _pdf_locks.setdefault(key, asyncio.Lock())


async def _withheld_from(db, artifact, release, user, *, baked: bool = False) -> bool:
    """Whether the copied snapshot is withheld from this reader as a whole
    (the owner always gets their own view). `baked`: the output pictures
    every chart's stored rows (a deck, its PDF, slide images), so one chart
    holding one identity's slice withholds it too."""
    from app.services.dashboard_access import counts_as_owner
    from app.services.dashboard_data import any_copy_tainted, release_snapshot_withheld

    if await counts_as_owner(db, artifact, user):
        return False
    if await release_snapshot_withheld(db, artifact, release):
        return True
    return baked and await any_copy_tainted(db, release)


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
    user: User | None = Depends(current_user_optional),
):
    artifact, release = await dashboard_access.open_published(db, ref, user)
    return {"mode": artifact.mode, "content": release.content or {}}


@router.get("/d/{ref}/queries")
async def list_published_queries(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
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
    user: User | None = Depends(current_user_optional),
):
    from app.services import dashboard_data

    artifact, release = await dashboard_access.open_published(db, ref, user)
    query = await dashboard_data.release_query(db, release, query_id)
    step = await dashboard_data.default_step(db, query)
    if step is None:
        raise dashboard_data.not_found()
    resolution = await dashboard_data.resolve_release_step(db, step, artifact, release, user)
    return await dashboard_data.public_step(db, step, query, resolution,
                                            organization_id=artifact.organization_id, user=user)


@router.get("/d/{ref}/queries/{query_id}/export")
async def export_published_step(
    ref: str,
    query_id: str,
    request: Request,
    format: str = Query("csv"),
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
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
    user: User | None = Depends(current_user_optional),
):
    """A token for a file the published version embeds, and nothing else."""
    from app.core.file_tokens import file_embed_url, mint_file_token
    from app.services import dashboard_data

    _, release = await dashboard_access.open_published(db, ref, user)
    if str(file_id) not in dashboard_data.file_ids(release.content or {}):
        raise dashboard_data.not_found()
    token = mint_file_token(str(file_id))
    return {"file_id": str(file_id), "token": token, "url": file_embed_url(str(file_id), token)}


@router.get("/d/{ref}/previews")
async def list_published_previews(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
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
    user: User | None = Depends(current_user_optional),
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
    user: User | None = Depends(current_user_optional),
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
        raise AppError.conflict(ErrorCode.DASHBOARD_EXPORT_WITHHELD,
                                "This dashboard shows each viewer their own data, so it cannot be exported.")
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

    if mode == "slides":
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

    try:
        pdf_path = await asyncio.wait_for(_locked(), timeout=_PDF_EXPORT_TIMEOUT_SECONDS)
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

@router.post("/d/{ref}/run")
async def run_published_dashboard(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
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
    user: User | None = Depends(current_user_optional),
):
    """One chart with the reader's filter values, cached per reader and values."""
    from app.services import dashboard_data, dashboard_runs

    if user is None:
        raise AppError.unauthorized()
    artifact, release = await dashboard_access.open_published(db, ref, user)
    query = await dashboard_data.release_query(db, release, query_id)
    return await dashboard_runs.filter_run(db, artifact, release, query, user, payload)


# ── Reading the draft (part B) ───────────────────────────────────────────

async def _draft(db, ref, user):
    from app.services import dashboard_data

    artifact = await dashboard_access.open_draft(db, ref, user)
    version = await dashboard_data.draft_version(db, artifact)
    return artifact, version


@router.get("/d/{ref}/draft/content")
async def get_draft_content(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from app.services import dashboard_data

    artifact, version = await _draft(db, ref, user)
    if version is None:
        raise dashboard_data.not_found()
    return {"mode": artifact.mode, "version_id": str(version.id), "content": version.content or {}}


@router.get("/d/{ref}/draft/queries")
async def list_draft_queries(
    ref: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from sqlalchemy.orm import lazyload, selectinload

    from app.models.query import Query as QueryModel
    from app.models.step import Step
    from app.schemas.query_schema import PublicQuerySchema
    from app.services import dashboard_data

    artifact, version = await _draft(db, ref, user)
    ids = await dashboard_data.draft_query_ids(db, version)
    if not ids:
        return []
    queries = (await db.execute(
        select(QueryModel).join(Step, Step.id == QueryModel.default_step_id)
        .options(lazyload("*"), selectinload(QueryModel.visualizations).options(lazyload("*")))
        .where(QueryModel.id.in_(ids), QueryModel.report_id == str(artifact.report_id),
               QueryModel.deleted_at.is_(None), Step.status == "success")
    )).scalars().all()
    return [PublicQuerySchema.model_validate(q) for q in queries]


@router.get("/d/{ref}/draft/queries/{query_id}/step")
async def get_draft_step(
    ref: str,
    query_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from sqlalchemy.orm import lazyload

    from app.models.query import Query as QueryModel
    from app.services import dashboard_data
    from app.services.viewer_data_policy import resolve_step_data

    artifact, version = await _draft(db, ref, user)
    if str(query_id) not in await dashboard_data.draft_query_ids(db, version):
        raise dashboard_data.not_found()
    query = (await db.execute(
        select(QueryModel).options(lazyload("*")).where(QueryModel.id == str(query_id),
                                                        QueryModel.report_id == str(artifact.report_id))
    )).scalar_one_or_none()
    step = await dashboard_data.default_step(db, query) if query is not None else None
    if step is None:
        raise dashboard_data.not_found()
    report = await dashboard_data.draft_report(db, artifact)
    resolution = await resolve_step_data(db, step, report, user)
    return await dashboard_data.public_step(db, step, query, resolution,
                                            organization_id=artifact.organization_id, user=user)


@router.get("/d/{ref}/draft/files/{file_id}/embed_token")
async def get_draft_file_embed_token(
    ref: str,
    file_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: User | None = Depends(current_user_optional),
):
    from app.core.file_tokens import file_embed_url, mint_file_token
    from app.services import dashboard_data

    _, version = await _draft(db, ref, user)
    if version is None or str(file_id) not in dashboard_data.file_ids(version.content or {}):
        raise dashboard_data.not_found()
    token = mint_file_token(str(file_id))
    return {"file_id": str(file_id), "token": token, "url": file_embed_url(str(file_id), token)}


# ── Old links (part B) ───────────────────────────────────────────────────

@router.get("/r/{report_id}/dashboard")
async def get_legacy_link_target(report_id: str, db: AsyncSession = Depends(get_async_db)):
    """The dashboard an old /r/{report_id} link leads to: the one recorded
    for that conversation by the data migration (artifacts.legacy_report_id).
    It reveals only an id; the dashboard's own /d check decides who opens it.
    Nothing recorded: /r keeps serving the conversation as before."""
    from app.models.artifact import Artifact
    from app.services import dashboard_data

    dashboard_id = (await db.execute(
        select(Artifact.id).where(Artifact.legacy_report_id == str(report_id), Artifact.deleted_at.is_(None))
    )).scalar_one_or_none()
    if dashboard_id is None:
        raise dashboard_data.not_found()
    return {"dashboard_id": str(dashboard_id)}
