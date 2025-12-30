from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Body, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from typing import Optional, List, Union

from app.models.user import User
from app.core.auth import current_user
from app.models.organization import Organization
from app.dependencies import get_current_organization
from app.services.data_source_service import DataSourceService
from app.schemas.data_source_schema import DataSourceCreate, DataSourceBase, DataSourceSchema, DataSourceUpdate, DataSourceMembershipCreate, DataSourceListItemSchema
from app.schemas.metadata_indexing_job_schema import MetadataIndexingJobSchema
from app.schemas.data_source_schema import DataSourceMembershipSchema
from app.schemas.datasource_table_schema import (
    DataSourceTableSchema,
    PaginatedTablesResponse,
    BulkUpdateTablesRequest,
    DeltaUpdateTablesRequest,
    DeltaUpdateTablesResponse,
)
from app.core.permissions_decorator import requires_permission
from app.models.data_source import DataSource

router = APIRouter(tags=["data_sources"])
data_source_service = DataSourceService()

@router.get("/available_data_sources", response_model=list[dict])
@requires_permission('view_data_source')
async def get_available_data_sources(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_available_data_sources(db, organization)

@router.get("/data_sources", response_model=list[DataSourceListItemSchema])
@requires_permission('view_data_source')
async def get_data_sources(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_data_sources(db, current_user, organization)

@router.get("/data_sources/active", response_model=list[DataSourceListItemSchema])
@requires_permission('view_data_source')
async def get_active_data_sources(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_active_data_sources(db, organization, current_user)

@router.get("/data_sources/{data_source_id}", response_model=DataSourceSchema)
@requires_permission('view_data_source', model=DataSource)
async def get_data_source(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.get_data_source(db, data_source_id, organization, current_user)


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

@router.post("/data_sources/test_connection", response_model=dict)
@requires_permission('create_data_source')
async def test_new_data_source_connection(
    data_source: DataSourceCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await data_source_service.test_new_data_source_connection(db=db, data=data_source, organization=organization, current_user=current_user)

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
    with_stats: bool = Query(False),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.get_data_source_schema(db, data_source_id, include_inactive=False, organization=organization, current_user=current_user, with_stats=with_stats)

@router.get("/data_sources/{data_source_id}/full_schema", response_model=Union[PaginatedTablesResponse, list])
@requires_permission('view_data_source_full_schema', model=DataSource)
async def get_data_source_full_schema(
    data_source_id: str,
    with_stats: bool = Query(False),
    # Pagination params (optional - if not provided, returns legacy list response)
    page: Optional[int] = Query(None, ge=1, description="Page number (1-indexed)"),
    page_size: Optional[int] = Query(None, ge=1, le=500, description="Items per page (max 500)"),
    schema_filter: Optional[str] = Query(None, description="Comma-separated schema names to filter"),
    search: Optional[str] = Query(None, description="Search tables by name"),
    sort_by: str = Query("name", description="Sort by: name, centrality_score, is_active, richness"),
    sort_dir: str = Query("asc", description="Sort direction: asc or desc"),
    selected_state: Optional[str] = Query(None, description="Filter by selection state: 'selected' or 'unselected'"),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    # If pagination params provided, use paginated response
    if page is not None or page_size is not None:
        # Default pagination values
        page = page or 1
        page_size = page_size or 100
        
        # Parse schema filter (comma-separated string to list)
        schema_filter_list = None
        if schema_filter:
            schema_filter_list = [s.strip() for s in schema_filter.split(",") if s.strip()]
        
        return await data_source_service.get_data_source_schema_paginated(
            db=db,
            data_source_id=data_source_id,
            organization=organization,
            page=page,
            page_size=page_size,
            schema_filter=schema_filter_list,
            search=search,
            sort_by=sort_by,
            sort_dir=sort_dir,
            include_inactive=True,
            selected_state=selected_state,
            with_stats=with_stats,
            current_user=current_user,
        )
    
    # Legacy behavior: return full list
    return await data_source_service.get_data_source_schema(db, data_source_id, include_inactive=True, organization=organization, current_user=current_user, with_stats=with_stats)

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


@router.post("/data_sources/{data_source_id}/bulk_update_tables", response_model=DeltaUpdateTablesResponse)
@requires_permission('view_data_source_full_schema', model=DataSource)
async def bulk_update_tables(
    data_source_id: str,
    request: BulkUpdateTablesRequest,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """
    Bulk activate/deactivate tables matching filter criteria.
    
    - action: "activate" or "deactivate"
    - filter: {"schema": ["schema1", "schema2"], "search": "pattern"}
    """
    return await data_source_service.bulk_update_tables_status(
        db=db,
        data_source_id=data_source_id,
        organization=organization,
        action=request.action,
        filter_params=request.filter,
        current_user=current_user,
    )


@router.put("/data_sources/{data_source_id}/update_tables_status", response_model=DeltaUpdateTablesResponse)
@requires_permission('view_data_source_full_schema', model=DataSource)
async def update_tables_status_delta(
    data_source_id: str,
    request: DeltaUpdateTablesRequest,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """
    Update table is_active status using delta (efficient for large table counts).
    
    - activate: list of table names to set is_active=True
    - deactivate: list of table names to set is_active=False
    """
    return await data_source_service.update_tables_status_delta(
        db=db,
        data_source_id=data_source_id,
        organization=organization,
        activate=request.activate,
        deactivate=request.deactivate,
        current_user=current_user,
    )


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

@router.post("/data_sources/{data_source_id}/llm_sync", response_model=dict)
@requires_permission('update_data_source', model=DataSource)
async def llm_sync(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.llm_sync(db=db, data_source_id=data_source_id, organization=organization, current_user=current_user)

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


@router.get("/data_sources/{data_source_id}/members", response_model=list[DataSourceMembershipSchema])
@requires_permission('view_data_source', model=DataSource)
async def get_data_source_members(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.get_data_source_members(db, data_source_id, organization, current_user)

@router.post("/data_sources/{data_source_id}/members", response_model=DataSourceMembershipSchema)
@requires_permission('manage_data_source_memberships', model=DataSource)
async def add_data_source_member(
    data_source_id: str,
    member: DataSourceMembershipCreate,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.add_data_source_member(db, data_source_id, member, organization, current_user)

@router.delete("/data_sources/{data_source_id}/members/{user_id}", status_code=204)
@requires_permission('manage_data_source_memberships', model=DataSource)
async def remove_data_source_member(
    data_source_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    return await data_source_service.remove_data_source_member(db, data_source_id, user_id, organization, current_user)


# ==================== Domain-Connection Routes ====================

@router.get("/data_sources/{data_source_id}/connections")
@requires_permission('update_data_source', model=DataSource)
async def get_domain_connections(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Get all connections linked to a domain."""
    connections = await data_source_service.get_domain_connections(db, data_source_id, organization)
    return [
        {
            "id": str(conn.id),
            "name": conn.name,
            "type": conn.type,
            "is_active": conn.is_active,
        }
        for conn in connections
    ]


@router.post("/data_sources/{data_source_id}/connections/{connection_id}")
@requires_permission('update_data_source', model=DataSource)
async def add_connection_to_domain(
    data_source_id: str,
    connection_id: str,
    sync_tables: bool = True,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Add a connection to a domain (M:N relationship)."""
    return await data_source_service.add_connection_to_domain(
        db=db,
        data_source_id=data_source_id,
        connection_id=connection_id,
        organization=organization,
        current_user=current_user,
        sync_tables=sync_tables,
    )


@router.delete("/data_sources/{data_source_id}/connections/{connection_id}")
@requires_permission('update_data_source', model=DataSource)
async def remove_connection_from_domain(
    data_source_id: str,
    connection_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user)
):
    """Remove a connection from a domain."""
    return await data_source_service.remove_connection_from_domain(
        db=db,
        data_source_id=data_source_id,
        connection_id=connection_id,
        organization=organization,
        current_user=current_user,
    )