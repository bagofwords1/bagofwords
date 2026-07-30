from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.models.user import User
from app.models.organization import Organization
from app.services.project_service import project_service
from app.ee.audit.service import audit_service
from app.schemas.project_schema import (
    ProjectAgentMini,
    ProjectAutomationItem,
    ProjectCreate,
    ProjectUpdate,
    ProjectSchema,
    ProjectListResponse,
    ProjectMemberSchema,
    ProjectMemberUpsert,
    ProjectDataSourcesUpdate,
    ProjectFilesUpdate,
    ReportMoveRequest,
)

router = APIRouter(tags=["projects"])


@router.post("/projects", response_model=ProjectSchema)
@requires_permission('create_reports')
async def create_project(
    project: ProjectCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await project_service.create_project(db, project, current_user, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="project.created",
        user_id=current_user.id, resource_type="project", resource_id=result.id,
        details={"name": result.name}, request=request,
    )
    return result


@router.get("/projects", response_model=ProjectListResponse)
@requires_permission('view_reports')
async def list_projects(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await project_service.list_projects(db, current_user, organization)


@router.get("/projects/{project_id}", response_model=ProjectSchema)
@requires_permission('view_reports')
async def get_project(
    project_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await project_service.get_project(db, project_id, current_user, organization)


@router.put("/projects/{project_id}", response_model=ProjectSchema)
@requires_permission('update_reports')
async def update_project(
    project_id: str,
    project: ProjectUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await project_service.update_project(db, project_id, project, current_user, organization)


@router.delete("/projects/{project_id}", response_model=ProjectSchema)
@requires_permission('delete_reports')
async def delete_project(
    project_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await project_service.delete_project(db, project_id, current_user, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="project.deleted",
        user_id=current_user.id, resource_type="project", resource_id=project_id,
        details={"name": result.name, "reports_archived": result.report_count}, request=request,
    )
    return result


@router.put("/projects/{project_id}/data_sources", response_model=ProjectSchema)
@requires_permission('update_reports')
async def set_project_data_sources(
    project_id: str,
    payload: ProjectDataSourcesUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await project_service.set_default_data_sources(
        db, project_id, payload.data_source_ids, current_user, organization
    )


@router.put("/projects/{project_id}/files", response_model=ProjectSchema)
@requires_permission('update_reports')
async def set_project_files(
    project_id: str,
    payload: ProjectFilesUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await project_service.set_default_files(
        db, project_id, payload.file_ids, current_user, organization
    )


@router.get("/projects/{project_id}/data_sources/selectable", response_model=List[ProjectAgentMini])
@requires_permission('view_reports')
async def list_selectable_project_data_sources(
    project_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Agents that may become project defaults: visible to the caller AND to
    every current member (defaults must stay resolvable by all members)."""
    return await project_service.list_selectable_data_sources(
        db, project_id, current_user, organization
    )


@router.get("/projects/{project_id}/automations", response_model=List[ProjectAutomationItem])
@requires_permission('view_reports')
async def list_project_automations(
    project_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await project_service.list_automations(db, project_id, current_user, organization)


@router.get("/projects/{project_id}/members", response_model=List[ProjectMemberSchema])
@requires_permission('view_reports')
async def list_project_members(
    project_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await project_service.list_members(db, project_id, current_user, organization)


@router.put("/projects/{project_id}/members", response_model=List[ProjectMemberSchema])
@requires_permission('view_reports')
async def upsert_project_member(
    project_id: str,
    payload: ProjectMemberUpsert,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await project_service.upsert_member(db, project_id, payload, current_user, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="project.member_upserted",
        user_id=current_user.id, resource_type="project", resource_id=project_id,
        details={"member_user_id": payload.user_id, "permissions": payload.permissions},
        request=request,
    )
    return result


@router.delete("/projects/{project_id}/members/{user_id}", response_model=List[ProjectMemberSchema])
@requires_permission('view_reports')
async def remove_project_member(
    project_id: str,
    user_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await project_service.remove_member(db, project_id, user_id, current_user, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="project.member_removed",
        user_id=current_user.id, resource_type="project", resource_id=project_id,
        details={"member_user_id": user_id}, request=request,
    )
    return result


@router.post("/reports/bulk/move")
@requires_permission('update_reports')
async def bulk_move_reports(
    payload: ReportMoveRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    moved = await project_service.move_reports(
        db, payload.report_ids, payload.project_id, current_user, organization
    )
    return {"moved": moved}
