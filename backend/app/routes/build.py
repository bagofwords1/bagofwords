from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
import logging

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import (
    requires_permission, check_resource_permissions, require_org_permission,
)
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
    BuildPublishSchema,
    PaginatedBuildResponse,
)


router = APIRouter(prefix="/builds", tags=["builds"])
build_service = BuildService()
logger = logging.getLogger(__name__)


async def _require_authority_over_instructions(
    db: AsyncSession,
    instruction_ids,
    user_id: str,
    org_id: str,
    *,
    scope_deltas=None,
) -> None:
    """``manage_instructions`` over every listed instruction.

    Scoped instructions need it on each of their agents. A global instruction
    (no agent) reaches every agent, so it needs the org-level permission; a
    per-agent grant is no authority over it. Same rule as
    ``_require_instruction_authority`` in ``app/routes/instruction.py``.

    ``scope_deltas`` ({instruction_id: {"added": set, "global": bool}}) folds in
    what a scope change would DO — not the whole proposed snapshot — so the gate
    authorizes both sides: the live agents (a removal/text edit on a scoped
    rule), the agents the change ADDS (a share to a new agent), and a move to
    global (``global`` → org-level, which managing the agents it drops does not
    confer). Measuring the delta, not the snapshot, avoids a false 403 when a
    text-only edit's version still lists a detached or deleted agent.
    """
    scopes = await build_service.get_instruction_scopes(db, instruction_ids)
    merged = {iid: set(ds) for iid, ds in scopes.items()}
    goes_global = False
    for iid, delta in (scope_deltas or {}).items():
        merged.setdefault(str(iid), set()).update(delta.get("added") or set())
        if delta.get("global"):
            goes_global = True  # the change empties a previously-scoped rule
    ds_ids = sorted({d for ds in merged.values() for d in ds})
    if ds_ids:
        await check_resource_permissions(
            db, user_id, org_id, "data_source", ds_ids, "manage_instructions",
            name_resource=False,
        )
    if goes_global or any(not ds for ds in scopes.values()):
        await require_org_permission(db, user_id, org_id, "manage_instructions")


async def _enforce_build_ds_access(
    db: AsyncSession,
    build_id: str,
    user_id: str,
    org_id: str,
    *,
    changed_instruction_ids=None,
) -> None:
    """Gate for build write ops.

    1. ``manage_instructions`` on every agent touched by the build's snapshot.
       Admins pass through ORG_PERM_IMPLIES_RESOURCE in the resolver.
    2. Authority over every instruction the build CHANGES. That includes
       org-level authority whenever a change touches a global instruction.
       Without this step, a build changing only global rules had no agents
       to check, so step 1 passed for any per-agent manager.

    ``changed_instruction_ids`` overrides the change set. Rollback uses it,
    since it replaces the whole live set rather than applying a diff.
    A build with nothing to check at all still needs org-level authority.
    """
    ds_ids = await build_service.get_build_data_source_ids(db, build_id)
    if ds_ids:
        await check_resource_permissions(
            db, user_id, org_id, "data_source", ds_ids, "manage_instructions",
            name_resource=False,
        )
    # The scope DELTA of each changed instruction authorizes what a publish WILL
    # attach (and whether it goes global). Rollback passes its own change set and
    # applies no scope delta (it restores the live set), so it skips this side.
    scope_deltas = None
    if changed_instruction_ids is None:
        changed_instruction_ids = await build_service.get_build_changed_instruction_ids(db, build_id)
        scope_deltas = await build_service.get_build_scope_deltas(db, build_id)
    if changed_instruction_ids:
        await _require_authority_over_instructions(
            db, changed_instruction_ids, user_id, org_id, scope_deltas=scope_deltas,
        )
    elif not ds_ids:
        await require_org_permission(db, user_id, org_id, "manage_instructions")


async def _instruction_read_filter(
    db: AsyncSession,
    user: User,
    organization: Organization,
):
    """Returns a function that narrows instruction ids to the ones ``user`` may read.

    Returns None when the user may read every instruction in the org. That is
    org admins and org-level ``manage_instructions`` holders, who review and
    publish every build. Everyone else sees global instructions plus those on
    agents they can access, matching ``GET /builds`` and the instruction
    detail routes. A build snapshots the whole org, so these reads filter
    rows; they don't hide whole builds.
    """
    from app.core.permission_resolver import get_accessible_data_source_ids, resolve_permissions
    is_admin, accessible = await get_accessible_data_source_ids(db, str(user.id), str(organization.id))
    if is_admin:
        return None
    resolved = await resolve_permissions(db, str(user.id), str(organization.id))
    if "manage_instructions" in resolved.org_permissions:
        return None

    async def _visible(instruction_ids) -> set[str]:
        return await build_service.visible_instruction_ids(
            db, str(organization.id), str(user.id), instruction_ids, accessible,
        )
    return _visible


# ==================== List and Get ====================

@router.get("", response_model=PaginatedBuildResponse)
async def list_builds(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[str] = Query(None, description="Filter by status: draft | pending_approval | approved | rejected (defaults to approved)"),
    created_by: Optional[str] = Query(None, description="Filter by creator: 'me' for current user's builds only"),
    data_source_id: Optional[str] = Query(None, description="Restrict to builds that touch this data source"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """List all builds for the organization. Defaults to approved builds only.
    
    Use status=all to get builds of all statuses.
    Use created_by=me to filter to current user's builds only.
    """
    # Default to approved status if not specified; 'all' means no filtering
    if status == 'all':
        effective_status = None
    else:
        effective_status = status if status is not None else 'approved'
    
    # Handle created_by filter
    created_by_user_id = current_user.id if created_by == 'me' else None

    # Restrict to builds touching only data sources the user can access.
    # Org-level admins (manage_instructions / full_admin_access) bypass the
    # filter via get_accessible_data_source_ids returning is_admin=True.
    from app.core.permission_resolver import get_accessible_data_source_ids
    is_admin, accessible_ds_ids = await get_accessible_data_source_ids(
        db, str(current_user.id), str(organization.id)
    )
    accessible_filter = None if is_admin else accessible_ds_ids

    result = await build_service.list_builds(
        db=db,
        org_id=organization.id,
        status=effective_status,
        skip=skip,
        limit=limit,
        created_by_user_id=created_by_user_id,
        accessible_data_source_ids=accessible_filter,
        data_source_id=data_source_id,
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
        raise HTTPException(status_code=404, detail="Build not found")
    
    return InstructionBuildSchema.model_validate(build)


@router.get("/{build_id}/contents")
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
        raise HTTPException(status_code=404, detail="Build not found")
    
    contents = await build_service.get_build_contents(db, build_id)
    read_filter = await _instruction_read_filter(db, current_user, organization)
    if read_filter is not None:
        visible = await read_filter([c.instruction_id for c in contents])
        contents = [c for c in contents if str(c.instruction_id) in visible]

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
@requires_permission('manage_instructions')
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
@requires_permission('manage_instructions', resource_scoped=True)
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
        raise HTTPException(status_code=404, detail="Build not found")

    await _enforce_build_ds_access(db, build_id, str(current_user.id), str(organization.id))
    build = await build_service.submit_build(db, build_id)
    
    return InstructionBuildSchema.model_validate(build)


@router.post("/{build_id}/reject", response_model=InstructionBuildSchema)
@requires_permission('manage_instructions', resource_scoped=True)
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
        raise HTTPException(status_code=404, detail="Build not found")

    await _enforce_build_ds_access(db, build_id, str(current_user.id), str(organization.id))
    build = await build_service.reject_build(db, build_id, current_user.id, reject_data.reason)
    
    return InstructionBuildSchema.model_validate(build)


# ==================== Draft Editing ====================

@router.put("/{build_id}/contents/{instruction_id}", response_model=BuildContentSchema)
@requires_permission('manage_instructions', resource_scoped=True)
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
        raise HTTPException(status_code=404, detail="Build not found")

    await _enforce_build_ds_access(db, build_id, str(current_user.id), str(organization.id))
    await _require_authority_over_instructions(
        db, [instruction_id], str(current_user.id), str(organization.id),
    )
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
@requires_permission('manage_instructions', resource_scoped=True)
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
        raise HTTPException(status_code=404, detail="Build not found")

    await _enforce_build_ds_access(db, build_id, str(current_user.id), str(organization.id))
    await _require_authority_over_instructions(
        db, [instruction_id], str(current_user.id), str(organization.id),
    )
    removed = await build_service.remove_from_build(db, build_id, instruction_id)
    
    if not removed:
        raise HTTPException(status_code=404, detail="Instruction not found in build")
    
    return {"message": "Instruction removed from build"}


# ==================== Diff and Rollback ====================

@router.get("/{build_id}/diff", response_model=BuildDiffSchema)
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
    
    # Missing and foreign builds read the same (no existence oracle).
    if (
        not build_a or not build_b
        or build_a.organization_id != organization.id
        or build_b.organization_id != organization.id
    ):
        raise HTTPException(status_code=404, detail="Build not found")
    
    diff = await build_service.diff_builds(db, build_id, compare_to)
    read_filter = await _instruction_read_filter(db, current_user, organization)
    if read_filter is not None:
        visible = await read_filter(
            diff["added"] + diff["removed"] + [m["instruction_id"] for m in diff["modified"]]
        )
        diff["added"] = [i for i in diff["added"] if str(i) in visible]
        diff["removed"] = [i for i in diff["removed"] if str(i) in visible]
        diff["modified"] = [m for m in diff["modified"] if str(m["instruction_id"]) in visible]
        diff["added_count"] = len(diff["added"])
        diff["removed_count"] = len(diff["removed"])
        diff["modified_count"] = len(diff["modified"])

    return BuildDiffSchema(**diff)


@router.get("/{build_id}/diff/details", response_model=BuildDiffDetailedSchema)
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
    
    # Missing and foreign builds read the same (no existence oracle).
    if (
        not build_a or not build_b
        or build_a.organization_id != organization.id
        or build_b.organization_id != organization.id
    ):
        raise HTTPException(status_code=404, detail="Build not found")
    
    diff = await build_service.diff_builds_detailed(db, compare_to, build_id)
    read_filter = await _instruction_read_filter(db, current_user, organization)
    if read_filter is not None:
        visible = await read_filter([it["instruction_id"] for it in diff["items"]])
        diff["items"] = [it for it in diff["items"] if str(it["instruction_id"]) in visible]
        for kind in ("added", "modified", "removed"):
            diff[f"{kind}_count"] = sum(1 for it in diff["items"] if it["change_type"] == kind)

    return BuildDiffDetailedSchema(**diff)


@router.post("/{build_id}/rollback", response_model=InstructionBuildSchema)
@requires_permission('manage_instructions', resource_scoped=True)
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
    # Resolve the build inside the caller's org BEFORE any permission check.
    # The checks below name the agents a build touches, so running them on a
    # foreign build id told the caller it existed and what its agents were
    # called. Missing and foreign builds now read the same.
    target = await build_service.get_build(db, build_id)
    if target is None or str(target.organization_id) != str(organization.id):
        raise HTTPException(status_code=404, detail="Build not found")

    # Rollback swaps the whole live set for the target's, so it changes every
    # instruction in either snapshot.
    replaced = await build_service.get_build_instruction_ids(db, build_id)
    main = await build_service.get_main_build(db, organization.id)
    if main is not None:
        replaced |= await build_service.get_build_instruction_ids(db, main.id)
    try:
        await _enforce_build_ds_access(
            db, build_id, str(current_user.id), str(organization.id),
            changed_instruction_ids=replaced,
        )
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        # Object-level denials must not distinguish hidden builds from missing
        # IDs. Keep the authorization reason in server logs only.
        logger.warning(
            "Rollback denied: organization=%s user=%s build=%s reason=%s",
            organization.id, current_user.id, build_id, exc.detail,
        )
        raise HTTPException(status_code=404, detail="Build not found") from exc
    build = await build_service.rollback_to_build(
        db, build_id, organization.id, current_user.id
    )

    return InstructionBuildSchema.model_validate(build)


@router.post("/{build_id}/publish", response_model=InstructionBuildSchema)
@requires_permission('manage_instructions', resource_scoped=True)
async def publish_build(
    build_id: str,
    publish_data: Optional[BuildPublishSchema] = Body(None),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """
    Publish a build to main with auto-merge support.

    This is the single action to make a build live:
    - Auto-approves if draft/pending
    - If the build is based on current main: simple promote
    - If the build is stale (main changed since creation): auto-merge user's changes
    - User's changes always win for the same instruction (last-modified-wins)

    Optionally, pass `instruction_ids` to filter which instructions to include.
    Instructions not in the list will be removed from the build before publishing.

    Ideal for CI/CD integration after a Git PR is merged.

    Example:
    ```
    curl -X POST "https://api.bagofwords.io/builds/{build_id}/publish" \\
         -H "Authorization: Bearer $BOW_API_KEY" \\
         -H "Content-Type: application/json" \\
         -d '{"instruction_ids": ["id1", "id2"]}'
    ```
    """
    build = await build_service.get_build(db, build_id)

    if not build:
        raise HTTPException(status_code=404, detail="Build not found")

    if build.organization_id != organization.id:
        raise HTTPException(status_code=404, detail="Build not found")

    if build.status == 'rejected':
        raise HTTPException(status_code=400, detail="Cannot publish a rejected build")

    if build.is_main:
        raise HTTPException(status_code=400, detail="Build is already published. Use rollback to revert to a previous build.")

    await _enforce_build_ds_access(db, build_id, str(current_user.id), str(organization.id))

    # Extract instruction_ids if provided
    instruction_ids = publish_data.instruction_ids if publish_data else None

    result = await build_service.publish_build(db, build_id, current_user.id, instruction_ids=instruction_ids)

    return InstructionBuildSchema.model_validate(result["build"])

