from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization
from app.services.git_repository_service import GitRepositoryService
from app.schemas.git_repository_schema import GitRepositoryCreate, GitRepositoryUpdate
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.core.permissions_decorator import requires_permission
from app.models.data_source import DataSource

router = APIRouter(tags=["git"])
git_repository_service = GitRepositoryService()

@router.get("/data_sources/{data_source_id}/git_repository")
@requires_permission('update_data_source')
async def get_git_repository(
    data_source_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await git_repository_service.get_git_repository(
        db,
        data_source_id,
        organization
    )



@router.post("/data_sources/{data_source_id}/git_repository/test")
@requires_permission('update_data_source')
async def test_git_connection(
    data_source_id: str,
    git_repo: GitRepositoryCreate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await git_repository_service.test_connection(
        db,
        data_source_id,
        git_repo,
        organization
    )

@router.post("/data_sources/{data_source_id}/git_repository")
@requires_permission('update_data_source')
async def create_git_repository(
    data_source_id: str,
    git_repo: GitRepositoryCreate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await git_repository_service.create_git_repository(
        db, 
        data_source_id, 
        git_repo, 
        current_user, 
        organization
    )

@router.put("/data_sources/{data_source_id}/git_repository/{repository_id}")
@requires_permission('update_data_source')
async def update_git_repository(
    data_source_id: str,
    repository_id: str,
    git_repo: GitRepositoryUpdate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await git_repository_service.update_git_repository(
        db,
        repository_id,
        data_source_id,
        git_repo,
        organization
    )

@router.delete("/data_sources/{data_source_id}/git_repository/{repository_id}")
@requires_permission('update_data_source')
async def delete_git_repository(
    data_source_id: str,
    repository_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await git_repository_service.delete_git_repository(
        db,
        repository_id,
        data_source_id,
        organization
    )

@router.post("/data_sources/{data_source_id}/git_repository/{repository_id}/index")
@requires_permission('update_data_source')
async def index_git_repository(
    data_source_id: str,
    repository_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    return await git_repository_service.index_git_repository(
        db,
        repository_id,
        data_source_id,
        organization
    )
