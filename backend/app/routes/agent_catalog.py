from typing import List

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.ee.audit.service import audit_service
from app.models.organization import Organization
from app.models.user import User
from app.schemas.agent_catalog_schema import (
    AgentCatalogAgentsUpdate,
    AgentCatalogAssignment,
    AgentCatalogCreate,
    AgentCatalogSchema,
    AgentCatalogUpdate,
)
from app.services.agent_catalog_service import agent_catalog_service

router = APIRouter(tags=["agent_catalogs"])

# Catalogs are organizational only (no access semantics). Any member may list
# them — they label the agents-list filter — but every write is full-admin only.


@router.get("/agent_catalogs", response_model=List[AgentCatalogSchema])
@requires_permission('view_reports')
async def list_agent_catalogs(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await agent_catalog_service.list_catalogs(db, organization)


@router.post("/agent_catalogs", response_model=AgentCatalogSchema)
@requires_permission('full_admin_access')
async def create_agent_catalog(
    catalog: AgentCatalogCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await agent_catalog_service.create_catalog(db, catalog, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="agent_catalog.created",
        user_id=current_user.id, resource_type="agent_catalog", resource_id=result.id,
        details={"name": result.name}, request=request,
    )
    return result


@router.put("/agent_catalogs/{catalog_id}", response_model=AgentCatalogSchema)
@requires_permission('full_admin_access')
async def update_agent_catalog(
    catalog_id: str,
    catalog: AgentCatalogUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await agent_catalog_service.update_catalog(db, catalog_id, catalog, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="agent_catalog.updated",
        user_id=current_user.id, resource_type="agent_catalog", resource_id=result.id,
        details=catalog.model_dump(exclude_unset=True), request=request,
    )
    return result


@router.delete("/agent_catalogs/{catalog_id}", response_model=AgentCatalogSchema)
@requires_permission('full_admin_access')
async def delete_agent_catalog(
    catalog_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await agent_catalog_service.delete_catalog(db, catalog_id, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="agent_catalog.deleted",
        user_id=current_user.id, resource_type="agent_catalog", resource_id=result.id,
        details={"name": result.name}, request=request,
    )
    return result


@router.put("/agent_catalogs/{catalog_id}/agents", response_model=AgentCatalogSchema)
@requires_permission('full_admin_access')
async def set_agent_catalog_agents(
    catalog_id: str,
    payload: AgentCatalogAgentsUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await agent_catalog_service.set_catalog_agents(db, catalog_id, payload, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="agent_catalog.agents_updated",
        user_id=current_user.id, resource_type="agent_catalog", resource_id=result.id,
        details={"data_source_ids": payload.data_source_ids}, request=request,
    )
    return result


# Deliberately not part of PUT /data_sources/{id}: that route is open to anyone
# with `manage` on the agent, while catalog assignment is full-admin only.
@router.put("/data_sources/{data_source_id}/catalog", response_model=AgentCatalogAssignment)
@requires_permission('full_admin_access')
async def set_agent_catalog(
    data_source_id: str,
    payload: AgentCatalogAssignment,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await agent_catalog_service.set_agent_catalog(db, data_source_id, payload, organization)
    await audit_service.log(
        db=db, organization_id=organization.id, action="agent_catalog.agent_assigned",
        user_id=current_user.id, resource_type="data_source", resource_id=data_source_id,
        details={"catalog_id": result.catalog_id}, request=request,
    )
    return result
