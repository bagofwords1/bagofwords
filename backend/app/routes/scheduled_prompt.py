import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.core.permission_resolver import resolve_permissions, FULL_ADMIN
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization
from app.services.scheduled_prompt_service import scheduled_prompt_service
from app.services.scheduled_task_template_service import scheduled_task_template_service
from app.ee.audit.service import audit_service
from app.schemas.scheduled_task_template_schema import (
    ScheduledTaskTemplateEnableRequest,
    ScheduledTaskTemplateState,
)
from app.schemas.scheduled_prompt_schema import (
    ScheduledPromptCreate,
    ScheduledPromptUpdate,
    ScheduledPromptSchema,
    ScheduledPromptListResponse,
    ScheduledPromptWithReport,
    ScheduledPromptReportInfo,
    ScheduledPromptRunListResponse,
)

router = APIRouter()


@router.get("/scheduled-prompts", response_model=ScheduledPromptListResponse)
async def list_all_scheduled_prompts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    search: Optional[str] = None,
    filter: str = Query('my'),
    status: str = Query('all', pattern='^(all|active|paused)$'),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """List all scheduled prompts across all reports in the organization."""
    # `filter=shared` returns other users' prompt text + report titles, which
    # bypasses the owner_only gate on the report-scoped scheduled-prompt
    # endpoints. Restrict cross-user visibility to admins only.
    if filter == 'shared':
        resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
        if FULL_ADMIN not in resolved.org_permissions:
            raise HTTPException(status_code=403, detail="Not allowed to list shared scheduled prompts")
    result = await scheduled_prompt_service.list_all_scheduled_prompts(
        db=db,
        organization_id=organization.id,
        page=page,
        limit=limit,
        search=search,
        filter=filter,
        current_user_id=current_user.id,
        status=status,
    )

    items = []
    run_status = await scheduled_prompt_service.last_run_status_map(db, result["prompts"])
    for sp in result["prompts"]:
        report_info = ScheduledPromptReportInfo(id=sp.report.id, title=sp.report.title) if sp.report else None
        user_name = sp.user.name if sp.user and hasattr(sp.user, 'name') else None
        base = ScheduledPromptSchema.model_validate(sp).model_dump()
        base["next_run_at"] = scheduled_prompt_service.next_run_at(str(sp.id))
        base["last_run_status"] = run_status.get(str(sp.id))
        item = ScheduledPromptWithReport(
            **base,
            report=report_info,
            user_name=user_name,
        )
        items.append(item)

    return ScheduledPromptListResponse(scheduled_prompts=items, meta=result["meta"])


@router.get("/scheduled-prompt-templates", response_model=List[ScheduledTaskTemplateState])
async def list_scheduled_prompt_templates(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Built-in task templates, annotated with the calling user's enabled state."""
    return await scheduled_task_template_service.list_templates(db, current_user, organization)


@router.post("/scheduled-prompt-templates/{key}/enable", response_model=ScheduledTaskTemplateState)
@requires_permission('create_reports')
async def enable_scheduled_prompt_template(
    key: str,
    request: Request,
    body: Optional[ScheduledTaskTemplateEnableRequest] = None,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Turn a template on for the calling user.

    First enable creates the host report plus the scheduled task; the optional
    body narrows the agent scope and overrides the shipped cron (defaults:
    every usable data source, the template's schedule). Enabling a paused
    template resumes the existing row with its history. Gated on
    ``create_reports`` because there is no report to scope to yet — enabling
    is what creates it.
    """
    state = await scheduled_task_template_service.enable(db, key, current_user, organization, options=body)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="scheduled_prompt.template_enabled",
            user_id=current_user.id, resource_type="scheduled_prompt",
            resource_id=state.scheduled_prompt_id,
            details={"template_key": key, "report_id": state.report_id},
            request=request,
        )
    except Exception:
        pass
    return state


@router.post("/scheduled-prompt-templates/{key}/disable", response_model=ScheduledTaskTemplateState)
async def disable_scheduled_prompt_template(
    key: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Pause the calling user's instance of a template (history kept).

    No permission decorator: the service only touches rows owned by the caller.
    """
    state = await scheduled_task_template_service.disable(db, key, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="scheduled_prompt.template_disabled",
            user_id=current_user.id, resource_type="scheduled_prompt",
            resource_id=state.scheduled_prompt_id,
            details={"template_key": key}, request=request,
        )
    except Exception:
        pass
    return state


@router.post("/reports/{report_id}/scheduled-prompts", response_model=ScheduledPromptSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def create_scheduled_prompt(
    report_id: str,
    body: ScheduledPromptCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    sp = await scheduled_prompt_service.create_scheduled_prompt(db, report_id, body, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="scheduled_prompt.created",
            user_id=current_user.id, resource_type="scheduled_prompt", resource_id=sp.id,
            details={"report_id": report_id, "cron": getattr(sp, "cron_schedule", None)},
            request=request,
        )
    except Exception:
        pass
    return sp


@router.get("/reports/{report_id}/scheduled-prompts", response_model=List[ScheduledPromptSchema])
@requires_permission('view_reports', model=Report, owner_only=True)
async def list_scheduled_prompts(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await scheduled_prompt_service.list_scheduled_prompts(db, report_id)


@router.put("/reports/{report_id}/scheduled-prompts/{sp_id}", response_model=ScheduledPromptSchema)
@requires_permission('update_reports', model=Report, owner_only=True)
async def update_scheduled_prompt(
    report_id: str,
    sp_id: str,
    body: ScheduledPromptUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    sp = await scheduled_prompt_service.update_scheduled_prompt(db, sp_id, body, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="scheduled_prompt.updated",
            user_id=current_user.id, resource_type="scheduled_prompt", resource_id=sp_id,
            details={"report_id": report_id,
                     "fields": list(body.dict(exclude_unset=True).keys())},
            request=request,
        )
    except Exception:
        pass
    return sp


@router.delete("/reports/{report_id}/scheduled-prompts/{sp_id}", status_code=204)
@requires_permission('update_reports', model=Report, owner_only=True)
async def delete_scheduled_prompt(
    report_id: str,
    sp_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await scheduled_prompt_service.delete_scheduled_prompt(db, sp_id, current_user, organization)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="scheduled_prompt.deleted",
            user_id=current_user.id, resource_type="scheduled_prompt", resource_id=sp_id,
            details={"report_id": report_id}, request=request,
        )
    except Exception:
        pass


@router.get("/reports/{report_id}/scheduled-prompts/{sp_id}/runs", response_model=ScheduledPromptRunListResponse)
@requires_permission('view_reports', model=Report, owner_only=True)
async def list_scheduled_prompt_runs(
    report_id: str,
    sp_id: str,
    limit: int = Query(20, ge=1, le=100),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Past runs of a scheduled task — the reports it produced, newest first."""
    return await scheduled_prompt_service.list_runs(db, sp_id, limit=limit)


@router.post("/reports/{report_id}/scheduled-prompts/{sp_id}/trigger", status_code=200)
@requires_permission('update_reports', model=Report, owner_only=True)
async def trigger_scheduled_prompt(
    report_id: str,
    sp_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Manually trigger a scheduled prompt execution (for testing / on-demand runs).

    The run is launched in the background so the request returns immediately —
    the agent run can take a while, and the caller (e.g. the "Run now" button)
    should not block on it. ``force=True`` bypasses the cross-worker claim and
    the paused check so a manual run always executes.
    """
    # Validate existence/visibility before kicking off the background run so the
    # caller gets a clean 404 instead of a silent no-op.
    await scheduled_prompt_service.get_scheduled_prompt(db, sp_id)
    asyncio.create_task(scheduled_prompt_service.scheduled_run_prompt(sp_id, force=True))
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="scheduled_prompt.triggered",
            user_id=current_user.id, resource_type="scheduled_prompt", resource_id=sp_id,
            details={"report_id": report_id}, request=request,
        )
    except Exception:
        pass
    return {"status": "triggered", "scheduled_prompt_id": sp_id}
