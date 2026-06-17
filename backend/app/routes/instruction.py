from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel

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


# BULK UPDATE
@router.put("/instructions/bulk", response_model=InstructionBulkResponse)
@requires_permission('manage_instructions')
async def bulk_update_instructions(
    bulk_update: InstructionBulkUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Bulk update multiple instructions (admin only)"""
    return await instruction_service.bulk_update_instructions(
        db, bulk_update, current_user, organization
    )


# BULK DELETE
@router.delete("/instructions/bulk", response_model=InstructionBulkResponse)
@requires_permission('manage_instructions')
async def bulk_delete_instructions(
    bulk_delete: InstructionBulkDelete,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Bulk delete multiple instructions (admin only)"""
    return await instruction_service.bulk_delete_instructions(
        db, bulk_delete.ids, current_user, organization
    )


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
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new instruction label."""
    return await instruction_label_service.create_label(db, label, organization, current_user)


@router.patch("/instructions/labels/{label_id}", response_model=InstructionLabelSchema)
@requires_permission('manage_instructions')
async def update_instruction_label(
    label_id: str,
    label: InstructionLabelUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Update an instruction label."""
    return await instruction_label_service.update_label(db, label_id, label, organization, current_user)


@router.delete("/instructions/labels/{label_id}")
@requires_permission('manage_instructions')
async def delete_instruction_label(
    label_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Delete (soft delete) an instruction label."""
    success = await instruction_label_service.delete_label(db, label_id, organization, current_user)
    if not success:
        raise AppError.not_found(ErrorCode.INSTRUCTION_LABEL_NOT_FOUND, "Instruction label not found")
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
    return resolved


# ==================== Pending Builds (Tracked Changes) ====================

from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.models.instruction_version import InstructionVersion


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

    # Exclude builds that contain this instruction at the same version the
    # MAIN build already has — those are drafts that just inherited the main
    # build's content without making a real change. Compared against main
    # (not against `instruction.current_version_id`) so that a brand-new
    # instruction created inside a draft (and not yet in main) still surfaces
    # as a pending suggestion.
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
    if main_version_id is not None:
        where_clauses.append(BuildContent.instruction_version_id != main_version_id)

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

    result = []
    for _content, build, version in rows:
        # Skip suggestions whose text already matches the live (main) text —
        # there is nothing to review.
        if main_text is not None and (version.text or "") == (main_text or ""):
            continue
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
            "pending_title": version.title,
            # Build-level "commit message": auto-generated summary + free-text rationale.
            "build_title": build.title,
            "message": build.description,
            # Provenance: which report/completion generated this suggestion (AI only).
            "report_id": (trace or {}).get("report_id"),
            "completion_id": (trace or {}).get("completion_id"),
        })
    return result

