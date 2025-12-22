from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.services.build_service import BuildService
from app.schemas.build_schema import (
    InstructionBuildCreate,
    InstructionBuildSchema,
    InstructionBuildListSchema,
    BuildContentSchema,
    BuildContentCreateSchema,
    BuildDiffSchema,
    BuildDiffDetailedSchema,
    BuildRejectSchema,
    PaginatedBuildResponse,
)


router = APIRouter(prefix="/builds", tags=["builds"])
build_service = BuildService()


# ==================== List and Get ====================

@router.get("", response_model=PaginatedBuildResponse)
@requires_permission('view_builds')
async def list_builds(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None, description="Filter by status: draft | pending_approval | approved | rejected (defaults to approved)"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """List all builds for the organization. Defaults to approved builds only."""
    # Default to approved status if not specified
    effective_status = status if status is not None else 'approved'
    
    result = await build_service.list_builds(
        db=db,
        org_id=organization.id,
        status=effective_status,
        skip=skip,
        limit=limit,
    )
    
    # Convert to list schemas
    items = [InstructionBuildListSchema.model_validate(b) for b in result["items"]]
    
    return PaginatedBuildResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        per_page=result["per_page"],
        pages=result["pages"],
    )


@router.get("/main", response_model=InstructionBuildSchema)
@requires_permission('view_instructions')
async def get_main_build(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get the main (active/live) build for the organization."""
    build = await build_service.get_main_build(db, organization.id)
    
    if not build:
        raise HTTPException(status_code=404, detail="No main build found")
    
    return InstructionBuildSchema.model_validate(build)


@router.get("/{build_id}", response_model=InstructionBuildSchema)
@requires_permission('view_builds')
async def get_build(
    build_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get a specific build by ID."""
    build = await build_service.get_build(db, build_id)
    
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    return InstructionBuildSchema.model_validate(build)


@router.get("/{build_id}/contents")
@requires_permission('view_builds')
async def get_build_contents(
    build_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all instruction versions in a build."""
    build = await build_service.get_build(db, build_id)
    
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    contents = await build_service.get_build_contents(db, build_id)
    
    # Convert to schemas with version details
    items = []
    for content in contents:
        version = content.instruction_version
        instruction = content.instruction
        
        items.append(BuildContentSchema(
            id=content.id,
            build_id=content.build_id,
            instruction_id=content.instruction_id,
            instruction_version_id=content.instruction_version_id,
            version_number=version.version_number if version else None,
            text=version.text if version else None,
            title=version.title if version else None,
            content_hash=version.content_hash if version else None,
            load_mode=version.load_mode if version else None,
            instruction_status=instruction.status if instruction else None,
            instruction_category=instruction.category if instruction else None,
        ))
    
    return {
        "items": items,
        "total": len(items),
        "build_id": build_id,
        "build_number": build.build_number,
    }


# ==================== Lifecycle ====================

@router.post("", response_model=InstructionBuildSchema)
@requires_permission('create_instructions')
async def create_build(
    build_data: InstructionBuildCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new draft build."""
    build = await build_service.create_build(
        db=db,
        org_id=organization.id,
        source=build_data.source,
        user_id=current_user.id,
        commit_sha=build_data.commit_sha,
        branch=build_data.branch,
    )
    
    return InstructionBuildSchema.model_validate(build)


@router.post("/{build_id}/submit", response_model=InstructionBuildSchema)
@requires_permission('create_builds')
async def submit_build(
    build_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Submit a draft build for approval. Transitions: draft -> pending_approval."""
    build = await build_service.get_build(db, build_id)
    
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    build = await build_service.submit_build(db, build_id)
    
    return InstructionBuildSchema.model_validate(build)


@router.post("/{build_id}/approve", response_model=InstructionBuildSchema)
@requires_permission('create_builds')
async def approve_build(
    build_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Approve a pending build. Transitions: pending_approval -> approved."""
    build = await build_service.get_build(db, build_id)
    
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    build = await build_service.approve_build(db, build_id, current_user.id)
    
    return InstructionBuildSchema.model_validate(build)


@router.post("/{build_id}/reject", response_model=InstructionBuildSchema)
@requires_permission('create_builds')
async def reject_build(
    build_id: str,
    reject_data: BuildRejectSchema,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Reject a pending build. Transitions: pending_approval -> rejected."""
    build = await build_service.get_build(db, build_id)
    
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    build = await build_service.reject_build(db, build_id, current_user.id, reject_data.reason)
    
    return InstructionBuildSchema.model_validate(build)


@router.post("/{build_id}/promote", response_model=InstructionBuildSchema)
@requires_permission('create_builds')
async def promote_build(
    build_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Promote an approved build to main (is_main=True). Sets previous main build to is_main=False."""
    build = await build_service.get_build(db, build_id)
    
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    build = await build_service.promote_build(db, build_id)
    
    return InstructionBuildSchema.model_validate(build)


# ==================== Draft Editing ====================

@router.put("/{build_id}/contents/{instruction_id}", response_model=BuildContentSchema)
@requires_permission('create_builds')
async def add_or_update_build_content(
    build_id: str,
    instruction_id: str,
    content_data: BuildContentCreateSchema,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Add or update an instruction version in a draft build."""
    build = await build_service.get_build(db, build_id)
    
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    content = await build_service.add_to_build(
        db, build_id, instruction_id, content_data.instruction_version_id
    )
    
    return BuildContentSchema(
        id=content.id,
        build_id=content.build_id,
        instruction_id=content.instruction_id,
        instruction_version_id=content.instruction_version_id,
    )


@router.delete("/{build_id}/contents/{instruction_id}")
@requires_permission('update_instructions')
async def remove_build_content(
    build_id: str,
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Remove an instruction from a draft build."""
    build = await build_service.get_build(db, build_id)
    
    if not build:
        raise HTTPException(status_code=404, detail="Build not found")
    
    if build.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="Build does not belong to this organization")
    
    removed = await build_service.remove_from_build(db, build_id, instruction_id)
    
    if not removed:
        raise HTTPException(status_code=404, detail="Instruction not found in build")
    
    return {"message": "Instruction removed from build"}


# ==================== Diff and Rollback ====================

@router.get("/{build_id}/diff", response_model=BuildDiffSchema)
@requires_permission('view_builds')
async def diff_builds(
    build_id: str,
    compare_to: str = Query(..., description="ID of the build to compare against"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Compare this build against another build and return differences."""
    # Verify both builds exist and belong to org
    build_a = await build_service.get_build(db, build_id)
    build_b = await build_service.get_build(db, compare_to)
    
    if not build_a:
        raise HTTPException(status_code=404, detail=f"Build {build_id} not found")
    if not build_b:
        raise HTTPException(status_code=404, detail=f"Build {compare_to} not found")
    
    if build_a.organization_id != organization.id or build_b.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="One or both builds do not belong to this organization")
    
    diff = await build_service.diff_builds(db, build_id, compare_to)
    
    return BuildDiffSchema(**diff)


@router.get("/{build_id}/diff/details", response_model=BuildDiffDetailedSchema)
@requires_permission('view_instructions')
async def diff_builds_detailed(
    build_id: str,
    compare_to: str = Query(..., description="ID of the parent build to compare against"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """
    Compare this build against another build and return detailed differences 
    with full instruction content for display and diffing.
    """
    # Verify both builds exist and belong to org
    build_a = await build_service.get_build(db, compare_to)  # parent
    build_b = await build_service.get_build(db, build_id)    # current
    
    if not build_a:
        raise HTTPException(status_code=404, detail=f"Build {compare_to} not found")
    if not build_b:
        raise HTTPException(status_code=404, detail=f"Build {build_id} not found")
    
    if build_a.organization_id != organization.id or build_b.organization_id != organization.id:
        raise HTTPException(status_code=403, detail="One or both builds do not belong to this organization")
    
    diff = await build_service.diff_builds_detailed(db, compare_to, build_id)
    
    return BuildDiffDetailedSchema(**diff)


@router.post("/{build_id}/rollback", response_model=InstructionBuildSchema)
@requires_permission('create_builds')
async def rollback_to_build(
    build_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """
    Rollback by creating a new build that copies from the target approved build.
    
    This creates a new build with source='rollback' and copies all instruction
    versions from the target build, then promotes it to main. This provides
    a clear audit trail of when rollbacks occurred.
    """
    build = await build_service.rollback_to_build(
        db, build_id, organization.id, current_user.id
    )

    return InstructionBuildSchema.model_validate(build)

