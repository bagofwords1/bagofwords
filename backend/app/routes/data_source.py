from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from typing import Optional

from app.models.user import User
from app.core.auth import current_user
from app.models.organization import Organization
from app.dependencies import get_current_organization
from app.services.data_source_service import DataSourceService
from app.schemas.data_source_schema import DataSourceCreate, DataSourceBase, DataSourceSchema, DataSourceUpdate
from app.schemas.metadata_indexing_job_schema import MetadataIndexingJobSchema

from app.schemas.datasource_table_schema import DataSourceTableSchema
from app.core.permissions_decorator import requires_permission
from app.models.data_source import DataSource

router = APIRouter()
data_source_service = DataSourceService()

@router.get("/available_data_sources", response_model=list[dict])
@requires_permission('view_data_source')
async def get_available_data_sources(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_available_data_sources(db, organization)

@router.get("/data_sources", response_model=list[DataSourceSchema])
@requires_permission('view_data_source')
async def get_data_sources(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_data_sources(db, current_user, organization)

@router.get("/data_sources/active", response_model=list[DataSourceSchema])
@requires_permission('view_data_source')
async def get_active_data_sources(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_active_data_sources(db, organization)

@router.get("/data_sources/{data_source_id}", response_model=DataSourceSchema)
@requires_permission('view_data_source', model=DataSource)
async def get_data_source(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_data_source(db, data_source_id, organization)


@router.get("/data_sources/{data_source_type}/fields", response_model=dict)
@requires_permission('view_data_source', model=DataSource)
async def get_data_source_fields(
    data_source_type: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await data_source_service.get_data_source_fields(db, data_source_type, organization, current_user)

@router.post("/data_sources", response_model=DataSourceSchema)
@requires_permission('create_data_source')
async def create_data_source(
    data_source: DataSourceCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.create_data_source(db, organization, current_user, data_source)

@router.delete("/data_sources/{data_source_id}")
@requires_permission('delete_data_source', model=DataSource)
async def delete_data_source(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.delete_data_source(db, data_source_id, organization, current_user)

@router.get("/data_sources/{data_source_id}/test_connection", response_model=dict)
@requires_permission('view_data_source', model=DataSource)
async def test_data_source_connection(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.test_data_source_connection(db, data_source_id, organization, current_user)

@router.put("/data_sources/{data_source_id}", response_model=DataSourceSchema)
@requires_permission('update_data_source', model=DataSource)
async def update_data_source(
    data_source_id: str,
    data_source: DataSourceUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.update_data_source(db, data_source_id, organization, data_source, current_user)

@router.get("/data_sources/{data_source_id}/schema", response_model=list)
@requires_permission('view_data_source', model=DataSource)
async def get_data_source_schema(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.get_data_source_schema(db, data_source_id, include_inactive=False, organization=organization, current_user=current_user)

@router.get("/data_sources/{data_source_id}/full_schema", response_model=list)
@requires_permission('view_data_source_full_schema', model=DataSource)
async def get_data_source_full_schema(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.get_data_source_schema(db, data_source_id, include_inactive=True, organization=organization, current_user=current_user)

@router.put("/data_sources/{data_source_id}/update_schema", response_model=DataSourceSchema)
@requires_permission('view_data_source_full_schema', model=DataSource)
async def update_table_status_in_schema(
    data_source_id: str,
    tables: list[DataSourceTableSchema],
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.update_table_status_in_schema(db, data_source_id, tables, organization)

@router.get("/data_sources/{data_source_id}/generate_items", response_model=dict)
@requires_permission('update_data_source', model=DataSource)
async def generate_data_source_items(
    data_source_id: str,
    item: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.generate_data_source_items(db, item, data_source_id, organization, current_user)

@router.get("/data_sources/{data_source_id}/refresh_schema", response_model=list)
@requires_permission('view_data_source_full_schema', model=DataSource)
async def refresh_data_source_schema(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.refresh_data_source_schema(db, data_source_id, organization, current_user)

@router.get("/data_sources/{data_source_id}/metadata_resources", response_model=MetadataIndexingJobSchema)
@requires_permission('view_data_source', model=DataSource)
async def get_metadata_resources(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.get_metadata_resources(db, data_source_id, organization, current_user)

@router.put("/data_sources/{data_source_id}/update_metadata_resources", response_model=MetadataIndexingJobSchema)
@requires_permission('update_data_source', model=DataSource)
async def update_metadata_resources(
    data_source_id: str,
    resources: list = Body(...),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Update the active status of metadata resources for a data source"""
    return await data_source_service.update_resources_status(
        db=db,
        data_source_id=data_source_id,
        resources=resources,
        organization=organization,
        current_user=current_user
    )
