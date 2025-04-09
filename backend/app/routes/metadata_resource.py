from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization
from app.services.metadata_indexing_job_service import MetadataIndexingJobService # Updated service name
from app.schemas.metadata_resource_schema import MetadataResourceList # Updated schema name
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.core.permissions_decorator import requires_permission
from typing import Optional

# Use a more generic tag like 'metadata'
router = APIRouter(tags=["metadata"])
metadata_indexing_job_service = MetadataIndexingJobService()

# Endpoint renamed from /dbt_resources to /metadata_resources
@router.get("/data_sources/{data_source_id}/metadata_resources", response_model=MetadataResourceList)
@requires_permission('read_data_source') # Assuming read permission is sufficient
async def get_metadata_resources(
    data_source_id: str,
    resource_type: Optional[str] = None, # Allow filtering by type (e.g., 'model', 'lookml_view')
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get metadata resources (DBT, LookML, etc.) for a specific data source."""
    # Call the renamed service method
    resources = await metadata_indexing_job_service.get_metadata_resources(
        db,
        data_source_id,
        organization,
        resource_type=resource_type,
        skip=skip,
        limit=limit
    )
    return resources

# Endpoint for indexing jobs (remains largely the same, just confirms service name)
@router.get("/data_sources/{data_source_id}/metadata_indexing_jobs") 
@requires_permission('read_data_source') # Assuming read permission is sufficient
async def get_metadata_indexing_jobs(
    data_source_id: str,
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get metadata indexing jobs for a specific data source."""
    jobs = await metadata_indexing_job_service.get_indexing_jobs(
        db,
        data_source_id,
        organization,
        skip=skip,
        limit=limit
    )
    return jobs

# Consider adding endpoints to get a single resource by ID or update resource status (e.g., is_active)
# Example:
# @router.get("/metadata_resources/{resource_id}")
# ...

# @router.put("/metadata_resources/{resource_id}")
# ... 