"""
Git Router - Endpoints for Git operations

This router provides endpoints for:
- Repository CRUD (org-level, with optional data_source_id for backwards compat)
- Syncing branches from Git to BOW
- Pushing builds to Git
- Repository status and capabilities

URL Pattern: /git/repositories for CRUD, /git/{repo_id}/... for operations
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List
from pydantic import BaseModel

from app.dependencies import get_async_db, get_current_organization
from app.services.git_service import GitService
from app.schemas.git_repository_schema import GitRepositoryCreate, GitRepositoryUpdate, GitRepositorySchema
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.core.permissions_decorator import requires_permission


router = APIRouter(prefix="/git", tags=["git-operations"])
git_service = GitService()


# ==================== Request/Response Schemas ====================

class SyncBranchRequest(BaseModel):
    """Request to sync a branch from Git to BOW."""
    branch: str


class SyncBranchResponse(BaseModel):
    """Response from branch sync."""
    build_id: str
    build_number: int
    branch: str
    status: str
    message: str


class PushBuildRequest(BaseModel):
    """Request to push a build to Git."""
    build_id: str
    create_pr: bool = False


class PushBuildResponse(BaseModel):
    """Response from build push."""
    build_id: str
    branch_name: str
    pushed: bool
    pr_url: Optional[str] = None
    message: Optional[str] = None


class RepositoryStatusResponse(BaseModel):
    """Repository status and capabilities."""
    id: str
    provider: str
    branch: Optional[str]
    status: str
    has_ssh_key: bool
    has_access_token: bool
    can_push: bool
    can_create_pr: bool
    write_enabled: bool
    is_self_hosted: bool
    last_synced_at: Optional[str]


# ==================== Repository CRUD Endpoints (Org-Level) ====================

@router.get("/repositories", response_model=List[GitRepositorySchema])
@requires_permission('view_data_source')
async def list_repositories(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """List all Git repositories for the organization."""
    return await git_service.list_repositories(db, organization)


@router.post("/repositories", response_model=GitRepositorySchema)
@requires_permission('create_data_source')
async def create_repository(
    git_repo: GitRepositoryCreate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Create a new Git repository integration for the organization.
    
    The data_source_id is optional - if provided, instructions will be scoped
    to that domain. If omitted, instructions are org-wide.
    """
    return await git_service.create_git_repository(
        db, 
        git_repo, 
        current_user, 
        organization
    )


@router.post("/repositories/test")
@requires_permission('create_data_source')
async def test_repository_connection(
    git_repo: GitRepositoryCreate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Test Git repository connection before creating."""
    return await git_service.test_connection(db, git_repo, organization)


@router.get("/repositories/{repository_id}", response_model=GitRepositorySchema)
@requires_permission('view_data_source')
async def get_repository(
    repository_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get a specific Git repository by ID."""
    return await git_service.get_repository(db, repository_id, organization)


@router.put("/repositories/{repository_id}", response_model=GitRepositorySchema)
@requires_permission('update_data_source')
async def update_repository(
    repository_id: str,
    git_repo: GitRepositoryUpdate,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Update an existing Git repository integration."""
    return await git_service.update_git_repository(
        db, repository_id, git_repo, organization
    )


@router.delete("/repositories/{repository_id}")
@requires_permission('update_data_source')
async def delete_repository(
    repository_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Delete a Git repository and associated data."""
    return await git_service.delete_git_repository(
        db, repository_id, organization, user_id=current_user.id
    )


@router.get("/repositories/{repository_id}/linked_instructions_count")
@requires_permission('view_data_source')
async def get_linked_instructions_count(
    repository_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get the count of instructions linked to a git repository."""
    return await git_service.get_linked_instructions_count(db, repository_id, organization)


@router.post("/{repo_id}/index")
@requires_permission('update_data_source')
async def index_repository(
    repo_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Trigger indexing/re-indexing of a Git repository."""
    return await git_service.index_git_repository(db, repo_id, organization)


@router.get("/{repo_id}/job_status")
@requires_permission('view_data_source')
async def get_indexing_job_status(
    repo_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """Get current indexing job status with progress percentage."""
    return await git_service.get_indexing_job_status(db, repo_id, organization)


# ==================== Sync Endpoints ====================

@router.post("/{repo_id}/sync", response_model=SyncBranchResponse)
@requires_permission('update_data_source')
async def sync_branch(
    repo_id: str,
    request: SyncBranchRequest,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Sync a specific Git branch to BOW.
    
    Creates a DRAFT build containing the contents of the specified branch.
    Use this when you have a feature branch with changes you want to test in BOW.
    
    Example CI/CD usage:
    ```
    curl -X POST "https://api.bagofwords.io/git/{repo_id}/sync" \\
         -H "Authorization: Bearer $BOW_API_KEY" \\
         -d '{"branch": "feature/new-metrics"}'
    ```
    """
    build = await git_service.sync_branch(
        db=db,
        repository_id=repo_id,
        branch=request.branch,
        organization=organization,
        user_id=current_user.id,
    )
    
    return SyncBranchResponse(
        build_id=str(build.id),
        build_number=build.build_number,
        branch=request.branch,
        status=build.status,
        message=f"Created draft build #{build.build_number} from branch '{request.branch}'"
    )


# ==================== Push Endpoints ====================

@router.post("/{repo_id}/push", response_model=PushBuildResponse)
@requires_permission('create_builds')
async def push_build(
    repo_id: str,
    request: PushBuildRequest,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Push a BOW build to a new Git branch.
    
    Creates a new branch named 'BOW-{build_number}' with the build contents.
    Optionally creates a Pull Request if create_pr=true and PAT is configured.
    
    Example CI/CD usage:
    ```
    curl -X POST "https://api.bagofwords.io/git/{repo_id}/push" \\
         -H "Authorization: Bearer $BOW_API_KEY" \\
         -d '{"build_id": "...", "create_pr": true}'
    ```
    """
    result = await git_service.push_build(
        db=db,
        build_id=request.build_id,
        repository_id=repo_id,
        organization=organization,
        user_id=current_user.id,
        create_pr=request.create_pr,
    )
    
    return PushBuildResponse(
        build_id=result["build_id"],
        branch_name=result["branch_name"],
        pushed=result["pushed"],
        pr_url=result.get("pr_url"),
        message=result.get("message"),
    )


# ==================== Status Endpoints ====================

@router.get("/{repo_id}/status", response_model=RepositoryStatusResponse)
@requires_permission('view_data_source')
async def get_repository_status(
    repo_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Get repository status and capabilities.
    
    Returns information about:
    - Authentication methods configured (SSH key, PAT)
    - Capabilities (can_push, can_create_pr)
    - Current sync status
    """
    status = await git_service.get_repository_status(
        db=db,
        repository_id=repo_id,
        organization=organization,
    )
    
    return RepositoryStatusResponse(**status)


# ==================== Build Publish Endpoint ====================
# Note: This is an alias endpoint for convenience
# The primary publish endpoint is at /builds/{id}/publish

@router.post("/{repo_id}/publish/{build_id}")
@requires_permission('create_builds')
async def publish_build_via_git(
    repo_id: str,
    build_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Publish a build to main with auto-merge support.
    
    This is a convenience alias for POST /builds/{build_id}/publish.
    Publishes a build to become the active/live build.
    
    Example CI/CD usage:
    ```
    curl -X POST "https://api.bagofwords.io/git/{repo_id}/publish/{build_id}" \\
         -H "Authorization: Bearer $BOW_API_KEY"
    ```
    """
    from app.services.build_service import BuildService
    from app.schemas.build_schema import InstructionBuildSchema
    
    build_service = BuildService()
    
    build = await build_service.get_build(db, build_id)
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    if build.status == 'rejected':
        raise HTTPException(status_code=400, detail="Cannot publish a rejected build")
    
    result = await build_service.publish_build(db, build_id, current_user.id)
    
    return InstructionBuildSchema.model_validate(result["build"])

