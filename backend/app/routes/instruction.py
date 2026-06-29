from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel

from app.ee.audit.service import audit_service
from app.dependencies import get_async_db, get_current_organization
from app.errors import AppError, ErrorCode
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission, check_resource_permissions
from app.services.instruction_service import InstructionService
from app.schemas.instruction_schema import (
    InstructionCreate,
    InstructionUpdate,
    InstructionSchema,
    InstructionListSchema,
    InstructionStatus,
    InstructionCategory,
    InstructionBulkUpdate,
    InstructionBulkDelete,
    InstructionBulkResponse
)
from app.models.instruction import Instruction
from app.schemas.instruction_label_schema import (
    InstructionLabelSchema,
    InstructionLabelCreate,
    InstructionLabelUpdate,
)
from app.services.instruction_label_service import InstructionLabelService
from app.schemas.instruction_analysis_schema import (
    InstructionAnalysisRequest,
    InstructionAnalysisResponse,
)

router = APIRouter(tags=["instructions"])
instruction_service = InstructionService()
instruction_label_service = InstructionLabelService()

# CREATE INSTRUCTIONS
@router.post("/instructions", response_model=InstructionSchema)
@requires_permission('manage_instructions', resource_scoped=True)
async def create_private_instruction(
    instruction: InstructionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new private instruction (auto-published) - Private Published: published, null, published"""
    if instruction.data_source_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", instruction.data_source_ids, "manage_instructions",
        )
    return await instruction_service.create_instruction(db, instruction, current_user, organization, force_global=False)

@router.post("/instructions/global", response_model=InstructionSchema)
@requires_permission('manage_instructions', resource_scoped=True)
async def create_global_instruction(
    instruction: InstructionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new global instruction (admin only) - Global Draft/Published: null, approved, draft/published"""
    if instruction.data_source_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", instruction.data_source_ids, "manage_instructions",
        )
    return await instruction_service.create_instruction(db, instruction, current_user, organization, force_global=True)

# LIST INSTRUCTIONS
# No org-level perm gate: instruction visibility is derived from data_source
# access (public DSes are visible to every member). The service applies
# user-permission-based filtering internally via _get_user_permissions.
@router.get("/instructions")
async def get_instructions(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    status: Optional[InstructionStatus] = Query(None),
    kind: Optional[str] = Query(None, description="Filter by instruction kind: 'instruction' or 'skill'"),
    category: Optional[InstructionCategory] = Query(None, description="Single category filter (deprecated, use categories)"),
    categories: Optional[str] = Query(None, description="Comma-separated categories"),
    include_own: bool = Query(True),
    include_drafts: bool = Query(False),
    include_archived: bool = Query(False), 
    include_hidden: bool = Query(False),
    user_id: Optional[str] = Query(None),
    data_source_id: Optional[str] = Query(None, description="Filter by single data source/agent id (deprecated, use data_source_ids)"),
    data_source_ids: Optional[str] = Query(None, description="Comma-separated agent IDs to filter by"),
    source_types: Optional[str] = Query(None, description="Comma-separated source types: dbt, markdown, user, ai"),
    load_mode: Optional[str] = Query(None, description="Single load mode filter (deprecated, use load_modes)"),
    load_modes: Optional[str] = Query(None, description="Comma-separated load modes: always, intelligent, disabled"),
    label_ids: Optional[str] = Query(None, description="Comma-separated label IDs"),
    search: Optional[str] = Query(None, description="Search in instruction text and title"),
    build_id: Optional[str] = Query(None, description="Load from specific build (defaults to main build)"),
    include_global: bool = Query(True, description="Include global instructions (no data sources) when filtering by data_source_ids"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get instructions with automatic permission-based filtering. Returns paginated response.
    
    By default, loads instructions from the main build (is_main=True).
    Pass build_id to load from a specific build instead.
    """
    # Parse label_ids from comma-separated string
    parsed_label_ids = None
    if label_ids:
        parsed_label_ids = [lid.strip() for lid in label_ids.split(',') if lid.strip()]
    
    # Parse source_types from comma-separated string
    parsed_source_types = None
    if source_types:
        parsed_source_types = [st.strip() for st in source_types.split(',') if st.strip()]
    
    # Parse categories from comma-separated string (prefer multi, fall back to single)
    parsed_categories = None
    if categories:
        parsed_categories = [c.strip() for c in categories.split(',') if c.strip()]
    elif category:
        parsed_categories = [category.value]
    
    # Parse load_modes from comma-separated string (prefer multi, fall back to single)
    parsed_load_modes = None
    if load_modes:
        parsed_load_modes = [lm.strip() for lm in load_modes.split(',') if lm.strip()]
    elif load_mode:
        parsed_load_modes = [load_mode]
    
    # Parse data_source_ids from comma-separated string (prefer multi, fall back to single)
    parsed_data_source_ids = None
    if data_source_ids:
        parsed_data_source_ids = [ds_id.strip() for ds_id in data_source_ids.split(',') if ds_id.strip()]
    elif data_source_id:
        parsed_data_source_ids = [data_source_id]
    
    return await instruction_service.get_instructions(
        db, organization, current_user,
        skip=skip, limit=limit,
        status=status.value if status else None,
        kind=kind,
        categories=parsed_categories,
        include_own=include_own,
        include_drafts=include_drafts,
        include_archived=include_archived,
        include_hidden=include_hidden,
        user_id=user_id,
        data_source_ids=parsed_data_source_ids,
        source_types=parsed_source_types,
        load_modes=parsed_load_modes,
        label_ids=parsed_label_ids,
        search=search,
        build_id=build_id,
        include_global=include_global
    )


# COUNTS — drives the /agents tree badges (per-agent count + pending dot,
# global/skills/total-pending) without hydrating instruction rows. Declared
# before /instructions/{instruction_id} so "counts" isn't captured as an id.
@router.get("/instructions/counts")
async def get_instruction_counts(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await instruction_service.get_instruction_counts(db, organization, current_user)


# CROSS-ENTITY SEARCH for the /agents "Search everything" box — grouped shape
# (agents + instructions), distinct from the instruction list.
@router.get("/knowledge/search")
async def search_knowledge(
    q: str = Query("", description="Search query (matches agent names and instruction text/title)"),
    limit: int = Query(20, ge=1, le=50),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await instruction_service.search_knowledge(db, organization, current_user, q, limit=limit)


# BULK UPDATE
@router.put("/instructions/bulk", response_model=InstructionBulkResponse)
@requires_permission('manage_instructions')
async def bulk_update_instructions(
    bulk_update: InstructionBulkUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Bulk update multiple instructions (admin only)"""
    result = await instruction_service.bulk_update_instructions(
        db, bulk_update, current_user, organization
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.bulk_updated",
            user_id=current_user.id, resource_type="instruction",
            details={"ids": list(getattr(bulk_update, "ids", []) or [])},
            request=request,
        )
    except Exception:
        pass
    return result


# BULK DELETE
@router.delete("/instructions/bulk", response_model=InstructionBulkResponse)
@requires_permission('manage_instructions')
async def bulk_delete_instructions(
    bulk_delete: InstructionBulkDelete,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Bulk delete multiple instructions (admin only)"""
    result = await instruction_service.bulk_delete_instructions(
        db, bulk_delete.ids, current_user, organization
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.bulk_deleted",
            user_id=current_user.id, resource_type="instruction",
            details={"ids": list(bulk_delete.ids or [])},
            request=request,
        )
    except Exception:
        pass
    return result


# ENHANCE INSTRUCTION (kept - not part of suggestion workflow)
@router.post("/instructions/enhance", response_model=str)
@requires_permission('manage_instructions', resource_scoped=True)
async def enhance_instruction(
    instruction_data: InstructionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Enhance an instruction with AI"""
    return await instruction_service.enhance_instruction(db, instruction_data, organization, current_user)

@router.get("/instructions/available-references", response_model=List[dict])
async def get_available_references(
    q: Optional[str] = Query(None, description="search text"),
    types: Optional[str] = Query(None, description="comma-separated types: metadata_resource,datasource_table,memory"),
    data_source_filter: Optional[str] = Query(None, description="comma-separated data source IDs"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Get available reference objects that the user has access to"""
    return await instruction_service.get_available_references(
        db=db,
        organization=organization,
        current_user=current_user,
        q=q,
        types=types,
        data_source_ids=data_source_filter,
    )

# UTILITY ROUTES
@router.get("/instructions/source-types", response_model=List[dict])
async def get_instruction_source_types(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get available source types based on existing instructions (dbt, markdown, user, ai)"""
    return await instruction_service.get_available_source_types(db, organization)


@router.get("/instructions/categories", response_model=List[str])
async def get_instruction_categories():
    """Get all available instruction categories"""
    return [category.value for category in InstructionCategory]

@router.get("/instructions/statuses", response_model=List[str])
async def get_instruction_statuses():
    """Get all available instruction statuses"""
    return [status.value for status in InstructionStatus]


# LABEL MANAGEMENT
@router.get("/instructions/labels", response_model=List[InstructionLabelSchema])
async def list_instruction_labels(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """List instruction labels for the current organization."""
    return await instruction_label_service.list_labels(db, organization, current_user)


@router.post("/instructions/labels", response_model=InstructionLabelSchema)
@requires_permission('manage_instructions')
async def create_instruction_label(
    label: InstructionLabelCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new instruction label."""
    created = await instruction_label_service.create_label(db, label, organization, current_user)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction_label.created",
            user_id=current_user.id, resource_type="instruction_label",
            resource_id=str(getattr(created, "id", "") or ""),
            details={"name": getattr(created, "name", None)}, request=request,
        )
    except Exception:
        pass
    return created


@router.patch("/instructions/labels/{label_id}", response_model=InstructionLabelSchema)
@requires_permission('manage_instructions')
async def update_instruction_label(
    label_id: str,
    label: InstructionLabelUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Update an instruction label."""
    updated = await instruction_label_service.update_label(db, label_id, label, organization, current_user)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction_label.updated",
            user_id=current_user.id, resource_type="instruction_label", resource_id=str(label_id),
            details={"fields": list(label.dict(exclude_unset=True).keys())}, request=request,
        )
    except Exception:
        pass
    return updated


@router.delete("/instructions/labels/{label_id}")
@requires_permission('manage_instructions')
async def delete_instruction_label(
    label_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Delete (soft delete) an instruction label."""
    success = await instruction_label_service.delete_label(db, label_id, organization, current_user)
    if not success:
        raise AppError.not_found(ErrorCode.INSTRUCTION_LABEL_NOT_FOUND, "Instruction label not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction_label.deleted",
            user_id=current_user.id, resource_type="instruction_label", resource_id=str(label_id),
            request=request,
        )
    except Exception:
        pass
    return {"message": "Label deleted successfully"}


@router.post("/instructions/analysis", response_model=InstructionAnalysisResponse)
async def analyze_instruction_endpoint(
    body: InstructionAnalysisRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Naive analysis for an instruction text (impact, related instructions, related resources)."""
    return await instruction_service.analyze_instruction(
        db=db,
        organization=organization,
        current_user=current_user,
        request=body,
    )


# NB: declared before /instructions/{instruction_id} so the literal segment
# isn't captured as a path param.
@router.get("/instructions/pending-changes")
async def get_pending_change_instruction_ids(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Instruction IDs that have at least one LIVE hunk in the per-hunk review
    (cherry-pick model). Authoritative: a suggestion build whose hunks are all
    accepted/rejected/already-applied no longer counts. Drives the per-row
    pending dots so they match exactly what the review shows.

    Delegates to the shared ``InstructionService.get_pending_change_instruction_ids``
    so the list and the single-instruction detail derive "Pending review" from
    this exact same rule."""
    pending = await instruction_service.get_pending_change_instruction_ids(
        db, organization=organization, current_user=current_user
    )
    return {"instruction_ids": sorted(pending)}


async def _get_pending_change_instruction_ids_legacy(organization, db, current_user):
    from sqlalchemy import select as _select, and_ as _and
    from app.models.instruction_build import InstructionBuild
    from app.models.build_content import BuildContent
    from app.models.instruction_version import InstructionVersion
    from app.services.suggestion_merge import covers as _covers

    org_id = str(organization.id)

    # 1) Main build contents → instruction_id -> (version_id, text)
    main_rows = (
        await db.execute(
            _select(BuildContent.instruction_id, InstructionVersion.id, InstructionVersion.text)
            .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
            .join(InstructionVersion, InstructionVersion.id == BuildContent.instruction_version_id)
            .where(_and(
                InstructionBuild.organization_id == org_id,
                InstructionBuild.is_main.is_(True),
                InstructionBuild.deleted_at.is_(None),
            ))
        )
    ).all()
    main_version = {str(i): str(v) for i, v, _t in main_rows}
    main_text = {str(i): (t or "") for i, _v, t in main_rows}

    # 2) Pending (non-main) builds → id -> base_build_id
    pend_builds = (
        await db.execute(
            _select(InstructionBuild.id, InstructionBuild.base_build_id)
            .where(_and(
                InstructionBuild.organization_id == org_id,
                InstructionBuild.is_main.is_(False),
                InstructionBuild.deleted_at.is_(None),
                InstructionBuild.status.in_(["draft", "pending_approval"]),
            ))
        )
    ).all()
    base_of = {str(bid): (str(base) if base else None) for bid, base in pend_builds}
    if not base_of:
        return {"instruction_ids": []}

    # 3) Base builds' contents → (base_build_id, instruction_id) -> version_id
    base_ids = [b for b in base_of.values() if b]
    base_version: dict = {}
    if base_ids:
        base_rows = (
            await db.execute(
                _select(BuildContent.build_id, BuildContent.instruction_id, BuildContent.instruction_version_id)
                .where(BuildContent.build_id.in_(base_ids))
            )
        ).all()
        for b_id, i_id, v_id in base_rows:
            base_version[(str(b_id), str(i_id))] = str(v_id)

    # Versions that are the base of some pending build (per instruction) — a
    # pending build whose own version is one of these is an intermediate snapshot
    # of a chained edit (v15->v16->v17) and is superseded by its child/leaf.
    superseded_pairs = {(i_id, v_id) for (_b, i_id), v_id in base_version.items()}

    # 4) Pending builds' contents (with text) → decide "really changed vs base"
    pend_rows = (
        await db.execute(
            _select(BuildContent.build_id, BuildContent.instruction_id, BuildContent.instruction_version_id, InstructionVersion.text)
            .join(InstructionVersion, InstructionVersion.id == BuildContent.instruction_version_id)
            .where(BuildContent.build_id.in_(list(base_of.keys())))
        )
    ).all()

    changed: set = set()
    for b_id, i_id, v_id, v_text in pend_rows:
        b_id, i_id, v_id = str(b_id), str(i_id), str(v_id)
        base_id = base_of.get(b_id)
        if base_id:
            base_vid = base_version.get((base_id, i_id))
            really_changed = (base_vid != v_id) if base_vid is not None else True
        else:
            really_changed = (i_id not in main_version) or (main_version.get(i_id) != v_id)
        if not really_changed:
            continue
        # A stale sibling (base behind current) is still a real pending change —
        # the per-instruction view rebases its intended change onto current. We
        # no longer exclude it on freshness; only genuine no-ops below drop out.
        if (i_id, v_id) in superseded_pairs:
            continue  # intermediate snapshot of a chained edit — leaf covers it
        if i_id in main_text and ((v_text or "") == main_text[i_id] or _covers(v_text or "", main_text[i_id])):
            continue  # already live (no-op) — exact, or fully contained in current
        changed.add(i_id)

    return {"instruction_ids": sorted(changed)}


# STANDARD CRUD
@router.get("/instructions/{instruction_id}", response_model=InstructionSchema)
async def get_instruction(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get a specific instruction by ID"""
    instruction = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if instruction is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    # Mirror list visibility: an instruction tied to a data source the user
    # can't access (and that isn't public/global) must not be viewable by id —
    # even for admins — so it stays hidden in the detail modal too.
    if not await instruction_service.user_can_view_instruction(db, instruction, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    return instruction

@router.put("/instructions/{instruction_id}", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def update_instruction(
    instruction_id: str,
    instruction: InstructionUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Update an instruction (only if private and user owns it)"""
    # Per-DS gate on existing attached DSes (admin bypass via manage_instructions
    # is handled in the resolver).
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", existing_ds_ids, "manage_instructions",
        )
    if instruction.data_source_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", instruction.data_source_ids, "manage_instructions",
        )
    updated_instruction = await instruction_service.update_instruction(
        db, instruction_id, instruction, organization, current_user
    )
    if updated_instruction is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    return updated_instruction

@router.delete("/instructions/{instruction_id}")
@requires_permission('manage_instructions', model=Instruction, owner_only=False, resource_scoped=True)
async def delete_instruction(
    instruction_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Delete an instruction (admins or users with per-DS manage_instructions grant)"""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", existing_ds_ids, "manage_instructions",
        )
    success = await instruction_service.delete_instruction(db, instruction_id, organization, current_user)
    if not success:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.deleted",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            request=request,
        )
    except Exception:
        pass
    return {"message": "Instruction deleted successfully"}


# ==================== Version Endpoints ====================

from app.services.instruction_version_service import InstructionVersionService
from app.schemas.instruction_version_schema import (
    InstructionVersionSchema,
    InstructionVersionListSchema,
    PaginatedVersionResponse,
)

instruction_version_service = InstructionVersionService()


@router.get("/instructions/{instruction_id}/versions")
async def get_instruction_versions(
    instruction_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get version history for an instruction."""
    # Verify instruction exists and belongs to org
    instruction = await instruction_service.get_instruction(
        db, instruction_id, organization, current_user
    )
    if not instruction:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    if not await instruction_service.user_can_view_instruction(db, instruction, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")

    result = await instruction_version_service.get_versions(
        db, instruction_id, skip=skip, limit=limit
    )
    
    # Convert to list schemas
    items = [InstructionVersionListSchema.model_validate(v) for v in result["items"]]
    
    return PaginatedVersionResponse(
        items=items,
        total=result["total"],
        page=result["page"],
        per_page=result["per_page"],
        pages=result["pages"],
        instruction_id=instruction_id,
    )


# NB: /versions/compare must be declared BEFORE /versions/{version_id} so the
# literal segment isn't captured as a path param.
@router.get("/instructions/{instruction_id}/versions/compare")
async def compare_instruction_versions(
    instruction_id: str,
    from_version_id: str = Query(..., description="The base version to diff from"),
    to_version_id: str = Query(..., description="The target version to diff to"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Compare two versions of the same instruction."""
    instruction = await instruction_service.get_instruction(
        db, instruction_id, organization, current_user
    )
    if not instruction:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    if not await instruction_service.user_can_view_instruction(db, instruction, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")

    from_version = await instruction_version_service.get_version(db, from_version_id)
    to_version = await instruction_version_service.get_version(db, to_version_id)
    if not from_version or not to_version:
        raise AppError.not_found(ErrorCode.INSTRUCTION_VERSION_NOT_FOUND, "Version not found")
    if from_version.instruction_id != instruction_id or to_version.instruction_id != instruction_id:
        raise AppError.bad_request(
            "instruction.version_mismatch",
            "Version does not belong to this instruction",
        )

    return await instruction_version_service.compare_versions(
        db, from_version_id, to_version_id
    )


@router.get("/instructions/{instruction_id}/versions/{version_id}", response_model=InstructionVersionSchema)
async def get_instruction_version(
    instruction_id: str,
    version_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get a specific version of an instruction."""
    # Verify instruction exists and belongs to org
    instruction = await instruction_service.get_instruction(
        db, instruction_id, organization, current_user
    )
    if not instruction:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    if not await instruction_service.user_can_view_instruction(db, instruction, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")

    version = await instruction_version_service.get_version(db, version_id)

    if not version:
        raise AppError.not_found(ErrorCode.INSTRUCTION_VERSION_NOT_FOUND, "Version not found")

    if version.instruction_id != instruction_id:
        raise AppError.bad_request(
            "instruction.version_mismatch",
            "Version does not belong to this instruction",
        )

    return InstructionVersionSchema.model_validate(version)


@router.post("/instructions/{instruction_id}/versions/{version_id}/revert", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def revert_instruction_to_version(
    instruction_id: str,
    version_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Revert an instruction to a prior version (admin only).

    Creates a new version copying the target version's content and stages it
    in a draft build. For admins, the build is auto-promoted to main; the
    instruction's status field is secondary — what is live is gated by the
    build promotion, not the instruction's status field.
    """
    existing = await instruction_service.get_instruction(
        db, instruction_id, organization, current_user
    )
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", existing_ds_ids, "manage_instructions",
        )

    reverted = await instruction_service.revert_instruction_to_version(
        db, instruction_id, version_id, organization, current_user
    )
    if reverted is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    return reverted


class ResolveSuggestionRequest(BaseModel):
    """Apply a partial (per-hunk) resolution of a suggested change.

    The client computes two full texts from the inline diff:
    - ``promote_text``: current text + the accepted hunks → promoted as a new
      version (a build-of-one) if it differs from the live text.
    - ``remaining_text``: what should stay proposed (current text + the hunks
      still pending). If it equals the live text, the suggestion is fully
      resolved and the instruction is dropped from the source build.
    """
    build_id: Optional[str] = None
    promote_text: str = ""
    remaining_text: str = ""
    title: Optional[str] = None


@router.post("/instructions/{instruction_id}/resolve", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def resolve_instruction_suggestion(
    instruction_id: str,
    body: ResolveSuggestionRequest,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Accept/reject part of a suggested change at hunk granularity.

    Accepting promotes the accepted hunks immediately (build-of-one); the
    remaining hunks stay pending against the new current text. Used by the
    inline tracked-changes review UI.
    """
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", existing_ds_ids, "manage_instructions",
        )

    resolved = await instruction_service.resolve_suggestion(
        db, instruction_id,
        build_id=body.build_id,
        promote_text=body.promote_text,
        remaining_text=body.remaining_text,
        title=body.title,
        organization=organization,
        current_user=current_user,
    )
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.suggestion_resolved",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={"build_id": getattr(body, "build_id", None)}, request=request,
        )
    except Exception:
        pass
    return resolved


# ==================== Per-hunk tracked changes (cherry-pick model) ===========

@router.get("/instructions/{instruction_id}/review-hunks")
async def get_instruction_review_hunks(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Server-authoritative tracked changes for an instruction: every pending
    suggestion as word-level hunks diffed against current main (rejected hunks
    filtered out). The unit of accept/reject is the hunk."""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    if not await instruction_service.user_can_view_instruction(db, existing, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    result = await instruction_service.review_hunks(db, instruction_id, organization=organization, current_user=current_user)
    if result is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    return result


class AcceptHunkRequest(BaseModel):
    build_id: str
    hunk_key: str
    against_main_version_id: Optional[str] = None


class RejectHunkRequest(BaseModel):
    build_id: str
    hunk_key: str


@router.post("/instructions/{instruction_id}/hunks/accept", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def accept_instruction_hunk(
    instruction_id: str,
    body: AcceptHunkRequest,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Cherry-pick one hunk of a suggestion onto main (new immutable build)."""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", existing_ds_ids, "manage_instructions",
        )
    resolved, status = await instruction_service.accept_hunk(
        db, instruction_id, build_id=body.build_id, hunk_key=body.hunk_key,
        against_main_version_id=body.against_main_version_id,
        organization=organization, current_user=current_user,
    )
    if status == "conflict":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "This change moved since you viewed it — refresh and try again.")
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.hunk_accepted",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={"hunk_key": getattr(body, "hunk_key", None), "build_id": getattr(body, "build_id", None)},
            request=request,
        )
    except Exception:
        pass
    return resolved


@router.post("/instructions/{instruction_id}/hunks/reject", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def reject_instruction_hunk(
    instruction_id: str,
    body: RejectHunkRequest,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Reject one hunk of a suggestion (records it; build snapshot stays immutable)."""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", existing_ds_ids, "manage_instructions",
        )
    resolved, _status = await instruction_service.reject_hunk(
        db, instruction_id, build_id=body.build_id, hunk_key=body.hunk_key,
        organization=organization, current_user=current_user,
    )
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.hunk_rejected",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={"hunk_key": getattr(body, "hunk_key", None), "build_id": getattr(body, "build_id", None)},
            request=request,
        )
    except Exception:
        pass
    return resolved


class AcceptAllRequest(BaseModel):
    against_main_version_id: Optional[str] = None


@router.post("/instructions/{instruction_id}/hunks/accept-all", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def accept_all_instruction_hunks(
    instruction_id: str,
    request: Request,
    body: AcceptAllRequest = AcceptAllRequest(),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Accept every live hunk in one pass (single new build)."""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(db, str(current_user.id), str(organization.id), "data_source", existing_ds_ids, "manage_instructions")
    resolved, status = await instruction_service.accept_all_hunks(
        db, instruction_id, against_main_version_id=body.against_main_version_id,
        organization=organization, current_user=current_user,
    )
    if status == "conflict":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "These changes moved since you viewed them — refresh and try again.")
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.hunks_accepted_all",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            request=request,
        )
    except Exception:
        pass
    return resolved


@router.post("/instructions/{instruction_id}/hunks/reject-all", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def reject_all_instruction_hunks(
    instruction_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Reject every live hunk in one pass (records them; main unchanged)."""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(db, str(current_user.id), str(organization.id), "data_source", existing_ds_ids, "manage_instructions")
    resolved, _status = await instruction_service.reject_all_hunks(
        db, instruction_id, organization=organization, current_user=current_user,
    )
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.hunks_rejected_all",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            request=request,
        )
    except Exception:
        pass
    return resolved


# ==================== Pending Builds (Tracked Changes) ====================

from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.models.instruction_version import InstructionVersion


@router.get("/instructions/{instruction_id}/resolved-evals")
async def get_instruction_resolved_evals(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Evals that 'resolve' for an instruction — the active eval cases scoped to
    the agent(s) this instruction is attached to. Used by the editor / suggestion
    review to show, per pending build, what running an eval would actually
    measure. A run is launched separately via POST /tests/runs with the chosen
    build_id (the suggestion build) so it tests exactly that change.

    Global instructions (no data sources) resolve to every agent's cases — we
    return the aggregate count + agent count rather than inlining them all.
    """
    from app.models.eval import TestCase, TestSuite, TEST_CASE_STATUS_ACTIVE
    from app.models.data_source import DataSource as _DS
    from app.services.agent_reliability_service import AgentReliabilityService

    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    if not await instruction_service.user_can_view_instruction(db, existing, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")

    org_id = str(organization.id)
    ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    is_global = len(ds_ids) == 0
    rel = AgentReliabilityService()

    if is_global:
        # Aggregate across the whole org — count only, don't inline.
        agent_count = (await db.execute(
            select(func.count()).select_from(_DS).where(
                _DS.organization_id == org_id, _DS.deleted_at.is_(None)
            )
        )).scalar() or 0
        case_rows = (await db.execute(
            select(TestCase.id)
            .join(TestSuite, TestSuite.id == TestCase.suite_id)
            .where(
                TestSuite.organization_id == org_id,
                TestCase.status == TEST_CASE_STATUS_ACTIVE,
                TestCase.deleted_at.is_(None),
            )
        )).all()
        return {
            "instruction_id": instruction_id,
            "is_global": True,
            "data_source_ids": [],
            "agent_count": int(agent_count),
            "case_count": len(case_rows),
            "cases": [],  # not inlined for global — UI shows count + link
        }

    # Scoped: union of each attached agent's active cases.
    case_ids: list[str] = []
    seen: set = set()
    for ds_id in ds_ids:
        for cid in await rel.list_agent_eval_case_ids(db, org_id, ds_id):
            if cid not in seen:
                seen.add(cid); case_ids.append(cid)
    cases = []
    if case_ids:
        rows = (await db.execute(
            select(TestCase.id, TestCase.name).where(TestCase.id.in_(case_ids))
        )).all()
        cases = [{"id": str(i), "name": n} for i, n in rows]
    return {
        "instruction_id": instruction_id,
        "is_global": False,
        "data_source_ids": ds_ids,
        "agent_count": len(ds_ids),
        "case_count": len(cases),
        "cases": cases,
    }


@router.get("/instructions/{instruction_id}/pending-builds")
async def get_instruction_pending_builds(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """List all pending/draft builds containing this instruction, with the
    pending version text. Used by the tracked-changes UI to show suggested
    edits awaiting approval."""
    existing = await instruction_service.get_instruction(
        db, instruction_id, organization, current_user
    )
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    if not await instruction_service.user_can_view_instruction(db, existing, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")

    # A pending build is only a *suggestion for this instruction* if it actually
    # changed this instruction relative to its OWN base (the main build it forked
    # from) — not merely relative to current main. Otherwise a build that forked
    # from an older main and only touched some other instruction would show up
    # here for every instruction whenever main later moved ahead (the "stale
    # snapshot" noise). We compare each build's version against its base build's
    # version below; main is used only to drop already-applied (no-op) leftovers.
    main_version_stmt = (
        select(BuildContent.instruction_version_id)
        .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
        .where(
            and_(
                BuildContent.instruction_id == instruction_id,
                InstructionBuild.organization_id == str(organization.id),
                InstructionBuild.is_main.is_(True),
                InstructionBuild.deleted_at.is_(None),
            )
        )
        .limit(1)
    )
    main_version_id = (await db.execute(main_version_stmt)).scalar_one_or_none()

    # Text of the current main version — used to drop suggestions that no longer
    # propose a real change (e.g. after a hunk was accepted, the leftover build
    # points at a different version row but identical text). Comparing text makes
    # "empty diff" suggestions self-resolve out of the review queue.
    main_text = None
    if main_version_id is not None:
        main_text = (
            await db.execute(
                select(InstructionVersion.text).where(InstructionVersion.id == main_version_id)
            )
        ).scalar_one_or_none()

    where_clauses = [
        BuildContent.instruction_id == instruction_id,
        InstructionBuild.organization_id == str(organization.id),
        InstructionBuild.deleted_at.is_(None),
        InstructionBuild.status.in_(["draft", "pending_approval"]),
        InstructionBuild.is_main.is_(False),
    ]

    stmt = (
        select(BuildContent, InstructionBuild, InstructionVersion)
        .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
        .join(
            InstructionVersion,
            InstructionVersion.id == BuildContent.instruction_version_id,
        )
        .where(and_(*where_clauses))
        .options(selectinload(InstructionBuild.created_by_user))
        .order_by(InstructionBuild.created_at.desc())
    )
    rows = (await db.execute(stmt)).all()

    # For each candidate build, look up what version of THIS instruction its base
    # build had, so we can tell "intentionally changed here" from "inherited".
    base_build_ids = [str(b.base_build_id) for _c, b, _v in rows if getattr(b, "base_build_id", None)]
    base_version_by_build: dict = {}
    if base_build_ids:
        base_rows = (
            await db.execute(
                select(BuildContent.build_id, BuildContent.instruction_version_id)
                .where(
                    and_(
                        BuildContent.build_id.in_(base_build_ids),
                        BuildContent.instruction_id == instruction_id,
                    )
                )
            )
        ).all()
        for b_id, v_id in base_rows:
            base_version_by_build[str(b_id)] = str(v_id)

    # Text of each base version, so the client can rebase a suggestion's
    # *intended change* (base_text -> pending_text) onto the current text rather
    # than diffing the full snapshot. This keeps a still-valid sibling suggestion
    # applicable after another sibling was accepted (main advanced past its
    # base), and avoids spurious "re-add removed text" hunks.
    base_text_by_vid: dict = {}
    base_vids = [v for v in base_version_by_build.values() if v]
    if base_vids:
        bt_rows = (
            await db.execute(
                select(InstructionVersion.id, InstructionVersion.text)
                .where(InstructionVersion.id.in_(base_vids))
            )
        ).all()
        for v_id, txt in bt_rows:
            base_text_by_vid[str(v_id)] = txt or ""

    def _base_text_for(build) -> str:
        base_id = getattr(build, "base_build_id", None)
        if not base_id:
            return ""  # no base → suggestion adds the instruction from scratch
        base_vid = base_version_by_build.get(str(base_id))
        if base_vid is None:
            return ""  # instruction not present in base → newly added here
        return base_text_by_vid.get(str(base_vid), "")

    # Supersede chained edits: if pending build A forked from pending build B
    # (a chain v15 -> v16 -> v17 from sequential chat edits), then B is an
    # intermediate snapshot fully contained in A. Showing both produces
    # overlapping/duplicated diff hunks ("hello world hello world"). Only the
    # leaf should surface. A version is superseded iff it is the base version
    # of some OTHER pending build. base_version_by_build holds, for each pending
    # build's base, the instruction version that base carried — so any of those
    # values that is itself a pending build's version is an intermediate.
    superseded_versions = set(base_version_by_build.values())

    def _build_changed_instruction(build, version) -> bool:
        """True if `build` actually changed this instruction vs its own base."""
        base_id = getattr(build, "base_build_id", None)
        if base_id:
            base_vid = base_version_by_build.get(str(base_id))
            if base_vid is not None:
                return base_vid != str(version.id)  # changed vs base
            return True  # not present in base → newly added in this build
        # No recorded base → fall back to comparing against current main.
        return main_version_id is None or str(version.id) != str(main_version_id)

    # Resolve the originating report for AI suggestions, so the UI can show a
    # "generated from <report>" provenance link on each suggestion. The chain is
    # build.agent_execution_id -> AgentExecution.report_id.
    from app.models.agent_execution import AgentExecution
    exec_ids = [str(b.agent_execution_id) for _c, b, _v in rows if getattr(b, "agent_execution_id", None)]
    trace_by_exec: dict = {}
    if exec_ids:
        exec_rows = (
            await db.execute(
                select(AgentExecution.id, AgentExecution.report_id, AgentExecution.completion_id)
                .where(AgentExecution.id.in_(exec_ids))
            )
        ).all()
        for ex_id, report_id, completion_id in exec_rows:
            trace_by_exec[str(ex_id)] = {
                "report_id": str(report_id) if report_id else None,
                "completion_id": str(completion_id) if completion_id else None,
            }

    def _passes_basic(build, version) -> bool:
        # Only builds that intentionally changed THIS instruction (vs their base).
        if not _build_changed_instruction(build, version):
            return False
        # Skip intermediate snapshots of a chained edit — only the leaf (the
        # build no other pending build forked from) is a real suggestion.
        if str(version.id) in superseded_versions:
            return False
        # Skip suggestions that contribute nothing to the live (main) text —
        # already applied / no-op leftover. This covers both an exact match and
        # the case where current already contains everything the suggestion
        # proposes (its text is a pure-insertion subset of main), e.g. after the
        # change was accepted via a slightly different version row. A *stale*
        # sibling that still has genuinely new content is kept (the client
        # rebases its intended change onto current).
        from app.services.suggestion_merge import covers
        if main_text is not None and (
            (version.text or "") == (main_text or "")
            or covers(version.text or "", main_text or "")
        ):
            return False
        return True

    # Content-level supersede: when sequential edits (often separate chat turns)
    # produce sibling builds that DON'T chain via base_build_id but whose text is
    # a cumulative superset of one another, keep only the maximal (leaf) one. This
    # collapses "+lorem" and "+lorem +hello" into the single cumulative suggestion
    # instead of rendering the shared text twice.
    from app.services.suggestion_merge import superseded_by_containment
    cand_by_build = {
        str(build.id): ((version.text or ""), _base_text_for(build))
        for _c, build, version in rows if _passes_basic(build, version)
    }
    superseded_by_content = superseded_by_containment(cand_by_build)

    result = []
    for _content, build, version in rows:
        if not _passes_basic(build, version):
            continue
        if str(build.id) in superseded_by_content:
            continue  # an intermediate snapshot a later sibling already covers
        creator = getattr(build, "created_by_user", None)
        trace = trace_by_exec.get(str(build.agent_execution_id)) if getattr(build, "agent_execution_id", None) else None
        result.append({
            "build_id": str(build.id),
            "build_number": build.build_number,
            "status": build.status,
            "source": build.source,
            "created_at": build.created_at.isoformat() if build.created_at else None,
            "created_by": (
                {"id": str(creator.id), "name": getattr(creator, "name", None) or getattr(creator, "email", None)}
                if creator else None
            ),
            "pending_version_id": str(version.id),
            "pending_version_number": version.version_number,
            "pending_text": version.text or "",
            # The text this suggestion forked from — lets the client rebase the
            # intended change onto current text (3-way merge) so siblings stay
            # applicable after one is accepted.
            "base_text": _base_text_for(build),
            "pending_title": version.title,
            # Build-level "commit message": auto-generated summary + free-text rationale.
            "build_title": build.title,
            "message": build.description,
            # Provenance: which report/completion generated this suggestion (AI only).
            "report_id": (trace or {}).get("report_id"),
            "completion_id": (trace or {}).get("completion_id"),
        })
    return result

