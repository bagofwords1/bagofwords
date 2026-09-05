from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional
from pydantic import BaseModel, Field

from app.ee.audit.service import audit_service
from app.dependencies import get_async_db, get_current_organization, release_request_db
from app.errors import AppError, ErrorCode
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission, check_resource_permissions, require_org_permission
from app.services.instruction_service import InstructionService
from app.services.skill_catalog_service import SkillCatalogService
from app.services.instruction_activity_service import (
    ACTIVITY_SOURCES,
    InstructionActivityService,
)
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
from app.services.instruction_directory_service import InstructionDirectoryService
from app.schemas.instruction_directory_schema import (
    InstructionDirectoryCreate,
    InstructionDirectorySchema,
    InstructionDirectoryUpdate,
    InstructionDirectoryTreeSchema,
    InstructionPlacementUpdate,
)
from app.schemas.instruction_analysis_schema import (
    InstructionAnalysisRequest,
    InstructionAnalysisResponse,
)

router = APIRouter(tags=["instructions"])

#: Page ceilings for GET /instructions. The full row carries the instruction
#: body three ways over (see InstructionListItemSchema), so it stays capped
#: where it was. The light row is a few hundred bytes, which is what lets a
#: tree or list load an org's whole instruction set instead of silently
#: truncating at the cap and showing a partial list as if it were complete.
FULL_MAX_LIMIT = 200
LIGHT_MAX_LIMIT = 2000

instruction_service = InstructionService()
skill_catalog_service = SkillCatalogService(instruction_service)
instruction_activity_service = InstructionActivityService()
instruction_label_service = InstructionLabelService()
instruction_directory_service = InstructionDirectoryService()


async def _require_instruction_authority(
    db: AsyncSession, current_user: User, organization: Organization, existing,
    *, allow_owner: bool = False,
) -> None:
    """Authority to mutate or review ``existing``.

    - Attached to agents → ``manage_instructions`` on EVERY one of them.
      Managing one agent an instruction is shared with is not authority over
      the others.
    - Attached to NO agent → org-level ``manage_instructions``. A scope-less
      instruction reaches every agent in the org, so a per-agent grant confers
      no authority over it; this mirrors the /instructions/global create gate.

    The empty-scope branch matters: without it, these routes skipped the check
    entirely for global instructions, letting any per-agent manager edit,
    accept or reject org-wide rules.

    ``allow_owner`` keeps the pre-existing carve-out on the edit path: the author
    of a scope-less instruction may still edit it (the service then restricts
    them to content fields — governance on a scope-less rule stays org-level).
    Review actions do NOT pass it: authoring a rule is not authority to approve
    changes to it.
    """
    ds_ids = [str(ds.id) for ds in (getattr(existing, "data_sources", None) or [])]
    if ds_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", ds_ids, "manage_instructions",
        )
        return
    if allow_owner:
        owner_id = getattr(existing, "user_id", None)
        if owner_id is not None and str(owner_id) == str(current_user.id):
            return
    await require_org_permission(
        db, str(current_user.id), str(organization.id), "manage_instructions",
    )


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
    else:
        # Truly org-wide (no data source) → stays an org-level capability.
        # An agent manager's per-DS `manage` must NOT let them author global
        # instructions that apply to every agent.
        await require_org_permission(
            db, str(current_user.id), str(organization.id), "manage_instructions",
        )
    return await instruction_service.create_instruction(db, instruction, current_user, organization, force_global=True)

# LIST INSTRUCTIONS
# No org-level perm gate: instruction visibility is derived from data_source
# access (public DSes are visible to every member). The service applies
# user-permission-based filtering internally via _get_user_permissions.
@router.get("/instructions")
async def get_instructions(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=LIGHT_MAX_LIMIT),
    view: str = Query(
        "full",
        description=(
            "'full' (default) returns the complete list row. 'light' drops the "
            "instruction body (text / formatted_content / structured_data) and "
            "the nested user records, keeping a short `preview` — for list and "
            "tree surfaces that render a label plus badges. Only 'light' may "
            f"exceed limit={FULL_MAX_LIMIT}."
        ),
    ),
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
    global_only: bool = Query(False, description="Return only global instructions (attached to no agent) — used by the lazy 'Global instructions' group"),
    live: Optional[bool] = Query(
        True,
        description=(
            "true (default) = instructions the live build carries — what the agent uses. "
            "false = instructions the org holds that the live build does NOT carry, so "
            "they aren't reaching the agent. Omit with live=null semantics via all=… is "
            "not supported; pass live=true or live=false."
        ),
    ),
    pending_only: bool = Query(False, description="Return only instructions that have a LIVE pending change — drives the 'Pending changes' view. Access-scoped exactly like the normal list."),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get instructions with automatic permission-based filtering. Returns paginated response.
    
    By default, loads instructions from the main build (is_main=True).
    Pass build_id to load from a specific build instead.
    """
    if view not in ("full", "light"):
        raise AppError(
            ErrorCode.VALIDATION,
            f"Unknown view '{view}'. Expected 'full' or 'light'.",
        )
    light = view == "light"
    # Reject rather than silently clamp: a caller asking for 1000 full rows has
    # mis-sized its request, and quietly returning 200 of them is exactly the
    # silent-truncation failure this parameter exists to remove.
    if not light and limit > FULL_MAX_LIMIT:
        raise AppError(
            ErrorCode.VALIDATION,
            f"limit must be <= {FULL_MAX_LIMIT} for view=full; "
            f"use view=light for larger pages.",
        )

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
    
    result = await instruction_service.get_instructions(
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
        include_global=include_global,
        global_only=global_only,
        pending_only=pending_only,
        light=light,
        live=live,
    )
    await release_request_db(db)  # free the pooled connection before serialization (Cause A, Phase 1)
    return result


# CHANGELOG — every change to the org's instructions, newest first. Declared
# before /instructions/{instruction_id} so "activity" isn't captured as an id.
@router.get("/instructions/activity")
async def get_instruction_activity(
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
    agent_ids: Optional[str] = Query(None, description="Comma-separated agent IDs — changes touching any of them"),
    user_ids: Optional[str] = Query(None, description="Comma-separated user IDs — changes made by any of them"),
    sources: Optional[str] = Query(None, description="Comma-separated: user | ai | git | rollback"),
    since: Optional[datetime] = Query(None, description="Only changes at or after this time"),
    include_empty: bool = Query(
        False,
        description="Include builds with no net effect (carry-over snapshots). Noise in a changelog.",
    ),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Instruction changelog. Each entry is a build, with the net effect it had
    on the instruction set — added / modified / removed. Scoped to the data
    sources the caller can access, exactly like the build list."""
    def _split(v: Optional[str]) -> Optional[List[str]]:
        if not v:
            return None
        parts = [p.strip() for p in v.split(",") if p.strip()]
        return parts or None

    parsed_sources = _split(sources)
    for s in (parsed_sources or []):
        if s not in ACTIVITY_SOURCES:
            raise AppError(
                ErrorCode.VALIDATION,
                f"Unknown source '{s}'. Expected one of: {', '.join(ACTIVITY_SOURCES)}.",
            )
    result = await instruction_activity_service.get_activity(
        db, organization, current_user,
        skip=skip, limit=limit, agent_ids=_split(agent_ids), user_ids=_split(user_ids),
        sources=parsed_sources, since=since, include_empty=include_empty,
    )
    await release_request_db(db)
    return result


# WHAT ONE CHANGELOG ENTRY TOUCHED — loaded when an entry is expanded, so the
# feed itself stays a page of counts.
@router.get("/instructions/activity/{build_id}")
async def get_instruction_activity_entry(
    build_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await instruction_activity_service.get_entry_changes(db, organization, build_id)
    await release_request_db(db)
    return result


# COUNTS — drives the /agents tree badges (per-agent count + pending dot,
# global/skills/total-pending) without hydrating instruction rows. Declared
# before /instructions/{instruction_id} so "counts" isn't captured as an id.
@router.get("/instructions/counts")
async def get_instruction_counts(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    _result = await instruction_service.get_instruction_counts(db, organization, current_user)
    await release_request_db(db)
    return _result


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

# PRE-BUILT SKILL CATALOG
#
# The catalog is code-defined content (app/ai/skills/library). Listing it is
# open to any member — it is product documentation, and the Skills tree shows
# it to everyone. Installing is an org-level `manage_instructions` action: an
# installed skill is a global, agent-wide row that reaches every request in the
# org, so a per-data-source grant confers no authority over it (same gate as
# POST /instructions/global).
async def _require_skill_catalog_admin(
    db: AsyncSession, current_user: User, organization: Organization,
) -> None:
    await require_org_permission(
        db, str(current_user.id), str(organization.id), "manage_instructions",
    )


@router.get("/instructions/skill-catalog", response_model=List[dict])
async def list_skill_catalog(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Pre-built skills, each annotated with this org's installation state."""
    return await skill_catalog_service.list_catalog(db, organization)


@router.post("/instructions/skill-catalog/{key}/install", response_model=dict)
async def install_skill_from_catalog(
    key: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Enable a pre-built skill for the organization (admin only). Idempotent."""
    await _require_skill_catalog_admin(db, current_user, organization)
    return await skill_catalog_service.install(db, key, current_user, organization)


@router.delete("/instructions/skill-catalog/{key}", response_model=dict)
async def uninstall_skill_from_catalog(
    key: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Disable a pre-built skill for the organization (admin only)."""
    await _require_skill_catalog_admin(db, current_user, organization)
    return await skill_catalog_service.uninstall(db, key, current_user, organization)


@router.post("/instructions/skill-catalog/{key}/update", response_model=dict)
async def update_skill_from_catalog(
    key: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Re-sync an installed skill to the current catalog version (admin only).

    Overwrites the org's copy — including any local edits, which the listing
    flags as `is_customized` so the UI can warn before calling this.
    """
    await _require_skill_catalog_admin(db, current_user, organization)
    return await skill_catalog_service.update_to_latest(db, key, current_user, organization)


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


# ── INSTRUCTION DIRECTORIES ─────────────────────────────────────────────────
# Cosmetic, per-agent folders for the /agents tree. A directory belongs to one
# agent (data_source_id) or the Global group (data_source_id omitted/null). They
# carry NO AI semantics. Mutations require manage_instructions on the scope;
# reading the tree is open to any org member (parity with the instruction list,
# which is permission-filtered rather than decorated).
async def _assert_manage_scope(db, current_user, organization, data_source_id):
    """Require manage_instructions on the scope agent (skip for the Global group,
    which the route-level @requires_permission already gates org-wide)."""
    if data_source_id:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", [data_source_id], "manage_instructions",
        )


@router.get("/instructions/directories", response_model=InstructionDirectoryTreeSchema)
async def list_instruction_directories(
    data_source_id: Optional[str] = Query(None, description="Agent id; omit for the Global instructions scope"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Return the folder tree + instruction placements for one scope (agent or
    global). Instructions themselves come from the normal light list."""
    result = await instruction_directory_service.get_tree(
        db, organization, current_user, data_source_id or None
    )
    await release_request_db(db)
    return result


@router.post("/instructions/directories", response_model=InstructionDirectorySchema)
@requires_permission('manage_instructions', resource_scoped=True)
async def create_instruction_directory(
    payload: InstructionDirectoryCreate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    await _assert_manage_scope(db, current_user, organization, payload.data_source_id)
    created = await instruction_directory_service.create(db, organization, current_user, payload)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction_directory.created",
            user_id=current_user.id, resource_type="instruction_directory",
            resource_id=str(getattr(created, "id", "") or ""),
            details={"name": created.name, "data_source_id": payload.data_source_id}, request=request,
        )
    except Exception:
        pass
    return created


@router.patch("/instructions/directories/{directory_id}", response_model=InstructionDirectorySchema)
@requires_permission('manage_instructions', resource_scoped=True)
async def update_instruction_directory(
    directory_id: str,
    payload: InstructionDirectoryUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Rename, move (parent_id) or reorder (position) a directory. Moving to the
    scope root is an explicit parent_id=null in the body."""
    existing = await instruction_directory_service._get_directory(db, organization, directory_id)
    await _assert_manage_scope(db, current_user, organization, existing.data_source_id)
    parent_provided = "parent_id" in payload.model_fields_set
    updated = await instruction_directory_service.update(
        db, organization, directory_id, payload, parent_provided
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction_directory.updated",
            user_id=current_user.id, resource_type="instruction_directory", resource_id=str(directory_id),
            details={"fields": list(payload.dict(exclude_unset=True).keys())}, request=request,
        )
    except Exception:
        pass
    return updated


@router.delete("/instructions/directories/{directory_id}")
@requires_permission('manage_instructions', resource_scoped=True)
async def delete_instruction_directory(
    directory_id: str,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Delete a folder. Its subfolders re-parent up one level and its
    instructions fall back to the scope root — instructions are never deleted."""
    existing = await instruction_directory_service._get_directory(db, organization, directory_id)
    await _assert_manage_scope(db, current_user, organization, existing.data_source_id)
    await instruction_directory_service.delete(db, organization, directory_id)
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction_directory.deleted",
            user_id=current_user.id, resource_type="instruction_directory", resource_id=str(directory_id),
            request=request,
        )
    except Exception:
        pass
    return {"message": "Directory deleted successfully"}


@router.put("/instructions/{instruction_id}/directory", response_model=InstructionDirectoryTreeSchema)
@requires_permission('manage_instructions', resource_scoped=True)
async def set_instruction_directory(
    instruction_id: str,
    payload: InstructionPlacementUpdate,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Move an instruction into a directory (drag & drop), or to the scope root
    when directory_id is null. Scope comes from the directory, or from
    data_source_id when clearing. Returns the refreshed tree for that scope."""
    # Resolve the scope agent to authorize against: the target directory's agent,
    # or the data_source_id supplied when clearing a placement.
    scope_ds = payload.data_source_id or None
    if payload.directory_id:
        target_dir = await instruction_directory_service._get_directory(db, organization, payload.directory_id)
        scope_ds = target_dir.data_source_id or None
    await _assert_manage_scope(db, current_user, organization, scope_ds)
    result = await instruction_directory_service.set_placement(
        db, organization, current_user, instruction_id, payload.directory_id, payload.data_source_id
    )
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction_directory.placement_set",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={"directory_id": payload.directory_id}, request=request,
        )
    except Exception:
        pass
    return result


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
    """Instruction IDs that have a pending review change. Drives the per-row
    dots, so it uses the same non-diffing tier as the tree badges and the list
    (``verify=False``): exact wherever equality settles it, optimistic for a
    suggestion whose base has drifted from main. Opening the instruction runs
    the authoritative per-hunk pass (GET /instructions/{id}/review-hunks) and
    the dot clears itself if nothing is left to review."""
    pending = await instruction_service.get_pending_change_instruction_ids(
        db, organization=organization, current_user=current_user, verify=False
    )
    # The sweep reads builds/versions only — re-scope through the caller's
    # instruction-row visibility (deleted, private-agent membership, hidden) so
    # these ids never disagree with the counts badge or the pending_only list.
    pending = await instruction_service._visible_pending_instruction_ids(
        db, organization, current_user, pending
    )
    await release_request_db(db)  # free the pooled connection before serialization (Cause A, Phase 1)
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
    await _require_instruction_authority(
        db, current_user, organization, existing, allow_owner=True,
    )
    # `is not None` distinguishes "scope not being changed" from "scope cleared".
    # An empty list is FALSY, so a bare truthiness check skipped this branch
    # entirely when the picker was emptied — letting a per-agent manager promote
    # their own rule to global (applies to every agent in the org) by removing
    # the one agent from it, which is precisely what the /instructions/global
    # gate exists to prevent.
    if instruction.data_source_ids is not None:
        if instruction.data_source_ids:
            await check_resource_permissions(
                db, str(current_user.id), str(organization.id),
                "data_source", instruction.data_source_ids, "manage_instructions",
            )
        else:
            await require_org_permission(
                db, str(current_user.id), str(organization.id), "manage_instructions",
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
    # Access view, NOT get_instruction: this endpoint only needs the row's
    # owner, status and attached agents to make its authorization decision, and
    # it returns no detail schema. get_instruction builds the full detail
    # payload, which includes the AUTHORITATIVE pending check — the quadratic
    # per-hunk rebase over every open suggestion on the row. On an instruction
    # carrying dozens of drifted suggestions that dominated the delete: measured
    # 6.0s total, of which ~5.6s was this lookup deciding whether a row we are
    # about to remove has pending changes to review.
    existing = await instruction_service.get_instruction_access_view(db, instruction_id, organization)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    # A user may always retract their OWN unpublished suggestion. Suggesting is
    # open to everyone (the publish gate is where authority lives), so the
    # matching retraction must be too — without this, a member who suggested a
    # rule via the knowledge harness could never withdraw it. Live/published
    # rows still require manage authority below: deleting one changes agent
    # behaviour, retracting a draft does not.
    _is_owner = getattr(existing, "user_id", None) is not None and \
        str(getattr(existing, "user_id")) == str(current_user.id)
    if _is_owner and getattr(existing, "status", None) != "published":
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
    existing_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    if existing_ds_ids:
        await check_resource_permissions(
            db, str(current_user.id), str(organization.id),
            "data_source", existing_ds_ids, "manage_instructions",
        )
    else:
        # Global instruction (no attached agents) applies to every agent, so
        # deleting it stays an org-level capability — mirrors the create_global
        # gate. Without this, the resource_scoped decorator would let any
        # per-agent manager delete a global (they can't create or edit one).
        #
        # Exception: the owner may always delete their OWN global. Deleting your
        # own instruction is strictly de-escalating (it removes a rule), and it
        # prevents "zombie" globals — an owner who created a global (e.g. while
        # they were an admin, or via the AI/MCP path) but later lost org-level
        # rights could otherwise never clean it up. This mirrors the update
        # path, where the owner can already edit their own global. AI-orphan
        # globals (no owner) stay admin-only — no owner to retract them.
        # Ported from PR #732.
        is_owner = getattr(existing, "user_id", None) is not None and \
            str(getattr(existing, "user_id")) == str(current_user.id)
        if not is_owner:
            await require_org_permission(
                db, str(current_user.id), str(organization.id), "manage_instructions",
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
    await _require_instruction_authority(db, current_user, organization, existing)

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
    await _require_instruction_authority(db, current_user, organization, existing)

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
    # Light access check (row + attached data-source ids) — the review pane
    # calls this endpoint per instruction and re-calls it after every hunk
    # action, so it must not pay get_instruction's full detail eager graph.
    existing = await instruction_service.get_instruction_access_view(db, instruction_id, organization)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    if not await instruction_service.user_can_view_instruction(db, existing, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    result = await instruction_service.review_hunks(db, instruction_id, organization=organization, current_user=current_user)
    if result is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    await release_request_db(db)  # free the pooled connection before serialization (Cause A, Phase 1)
    return result


class AcceptHunkRequest(BaseModel):
    build_id: str
    hunk_key: str
    # Required snapshot tokens. ``None`` is still a meaningful value for an org
    # with no main build or an instruction absent from main, but omission must
    # fail closed rather than silently comparing against an unknown baseline.
    against_main_build_id: Optional[str]
    against_main_version_id: Optional[str]


class RejectHunkRequest(BaseModel):
    build_id: str
    hunk_key: str
    against_main_build_id: Optional[str]
    against_main_version_id: Optional[str]


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
    await _require_instruction_authority(db, current_user, organization, existing)
    resolved, status = await instruction_service.accept_hunk(
        db, instruction_id, build_id=body.build_id, hunk_key=body.hunk_key,
        against_main_build_id=body.against_main_build_id,
        against_main_version_id=body.against_main_version_id,
        organization=organization, current_user=current_user,
    )
    if status == "stale":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "This change moved since you viewed it — refresh and try again.")
    if status == "invalid_selection":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "This change couldn't be applied as displayed. Refresh to reload the review; if this keeps happening, please report it.")
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.hunk_accepted",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={
                "hunk_key": body.hunk_key,
                "build_id": body.build_id,
                "against_main_build_id": body.against_main_build_id,
                "against_main_version_id": body.against_main_version_id,
            },
            request=request,
        )
    except Exception:
        pass
    return resolved


class AcceptStagedRequest(BaseModel):
    # The shared draft build the brand-new instruction is staged in (a training
    # session stages every create_instruction in one build).
    build_id: str


@router.post("/instructions/{instruction_id}/accept-staged", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def accept_staged_instruction(
    instruction_id: str,
    body: AcceptStagedRequest,
    request: Request,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Accept ONE brand-new instruction staged in a shared draft build.

    Promotes the staged version as an isolated build-of-one and detaches it from
    the shared draft — leaving sibling suggestions in the draft acceptable. Used
    by the training-mode create_instruction cards, where multiple new
    instructions share one draft build and per-instruction accept must not prune
    the siblings or finalize the whole build (which
    ``POST /builds/{id}/publish`` would do)."""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    await _require_instruction_authority(db, current_user, organization, existing)
    resolved = await instruction_service.accept_staged_instruction(
        db, instruction_id, build_id=body.build_id,
        organization=organization, current_user=current_user,
    )
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.staged_accepted",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={"build_id": getattr(body, "build_id", None)}, request=request,
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
    await _require_instruction_authority(db, current_user, organization, existing)
    resolved, status = await instruction_service.reject_hunk(
        db, instruction_id, build_id=body.build_id, hunk_key=body.hunk_key,
        against_main_build_id=body.against_main_build_id,
        against_main_version_id=body.against_main_version_id,
        organization=organization, current_user=current_user,
    )
    if status == "stale":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "This change moved since you viewed it — refresh and try again.")
    if status == "invalid_selection":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "This change couldn't be applied as displayed. Refresh to reload the review; if this keeps happening, please report it.")
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.hunk_rejected",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={
                "hunk_key": body.hunk_key,
                "build_id": body.build_id,
                "against_main_build_id": body.against_main_build_id,
                "against_main_version_id": body.against_main_version_id,
            },
            request=request,
        )
    except Exception:
        pass
    return resolved


class ReviewHunkSelection(BaseModel):
    build_id: str
    hunk_key: str


class AcceptAllRequest(BaseModel):
    against_main_build_id: Optional[str]
    against_main_version_id: Optional[str]
    hunks: List[ReviewHunkSelection] = Field(min_length=1)


class RejectAllRequest(BaseModel):
    against_main_build_id: Optional[str]
    against_main_version_id: Optional[str]
    hunks: List[ReviewHunkSelection] = Field(min_length=1)


@router.post("/instructions/{instruction_id}/hunks/accept-all", response_model=InstructionSchema)
@requires_permission('manage_instructions', model=Instruction, resource_scoped=True)
async def accept_all_instruction_hunks(
    instruction_id: str,
    request: Request,
    body: AcceptAllRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Accept every live hunk in one pass (single new build)."""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    await _require_instruction_authority(db, current_user, organization, existing)
    resolved, status = await instruction_service.accept_all_hunks(
        db, instruction_id, against_main_version_id=body.against_main_version_id,
        against_main_build_id=body.against_main_build_id,
        organization=organization, current_user=current_user,
        selected_hunks=[(h.build_id, h.hunk_key) for h in body.hunks],
    )
    if status == "stale":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "These changes moved since you viewed them — refresh and try again.")
    if status == "invalid_selection":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "These changes couldn't be applied as displayed. Refresh to reload the review; if this keeps happening, please report it.")
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.hunks_accepted_all",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={
                "against_main_build_id": body.against_main_build_id,
                "against_main_version_id": body.against_main_version_id,
                "hunks": [h.model_dump() for h in body.hunks],
            },
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
    body: RejectAllRequest,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Reject every live hunk in one pass (records them; main unchanged)."""
    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    await _require_instruction_authority(db, current_user, organization, existing)
    resolved, status = await instruction_service.reject_all_hunks(
        db, instruction_id, organization=organization, current_user=current_user,
        against_main_build_id=body.against_main_build_id,
        against_main_version_id=body.against_main_version_id,
        selected_hunks=[(h.build_id, h.hunk_key) for h in body.hunks],
    )
    if status == "stale":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "These changes moved since you viewed them — refresh and try again.")
    if status == "invalid_selection":
        raise AppError.conflict(ErrorCode.RESOURCE_CONFLICT, "These changes couldn't be applied as displayed. Refresh to reload the review; if this keeps happening, please report it.")
    if resolved is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    try:
        await audit_service.log(
            db=db, organization_id=organization.id, action="instruction.hunks_rejected_all",
            user_id=current_user.id, resource_type="instruction", resource_id=str(instruction_id),
            details={
                "against_main_build_id": body.against_main_build_id,
                "against_main_version_id": body.against_main_version_id,
                "hunks": [h.model_dump() for h in body.hunks],
            },
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


async def _resolved_eval_agent_scope(
    db: AsyncSession, current_user: User, organization: Organization, agent_ids: Optional[str],
) -> list:
    """The caller-supplied agents, narrowed to the ones they may actually see.

    The ids arrive from the client (the conversation's own data sources), so
    they are a *request*, not authority — an unfiltered pass-through would let
    any id in the body name another team's agent and read back its suite and
    case names. Mirrors the access rule used for instruction listing: admins
    see everything, everyone else sees public agents plus their grants.
    """
    from sqlalchemy import or_
    from app.models.data_source import DataSource as _DS
    from app.core.permission_resolver import get_accessible_data_source_ids

    requested = [x.strip() for x in (agent_ids or "").split(",") if x.strip()]
    if not requested:
        return []
    is_admin, accessible = await get_accessible_data_source_ids(
        db, str(current_user.id), str(organization.id)
    )
    stmt = select(_DS.id).where(
        _DS.organization_id == str(organization.id),
        _DS.deleted_at.is_(None),
        _DS.id.in_(requested),
    )
    if not is_admin:
        clauses = [_DS.is_public == True]  # noqa: E712
        if accessible:
            clauses.append(_DS.id.in_(list(accessible)))
        stmt = stmt.where(or_(*clauses))
    allowed = {str(i) for i in (await db.execute(stmt)).scalars().all()}
    # Preserve the caller's order — the conversation lists its agents in a
    # meaningful order and the panel groups follow it.
    return [r for r in requested if r in allowed]


@router.get("/instructions/{instruction_id}/resolved-evals")
async def get_instruction_resolved_evals(
    instruction_id: str,
    agent_ids: Optional[str] = Query(
        None,
        description=(
            "Comma-separated data source ids of the conversation this instruction "
            "was edited in. Used ONLY when the instruction is global: such an "
            "instruction resolves to every agent in the org, which is not a "
            "runnable set, so the chat's own agents stand in for it."
        ),
    ),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Evals that 'resolve' for an instruction, grouped the way the agents tree
    groups them: one group per agent, each listing that agent's SUITES.

    Two different questions are being answered at once and conflating them is
    what makes an eval panel lie. A suite is a *home* (``TestSuite`` docstring):
    it says where a case was filed. What actually RUNS against an agent is a
    per-case property (``TestCase.data_source_ids_json``, empty = every agent).
    So a suite homed to agent A can hold a case targeting B, and the org-wide
    Drafts bucket can hold cases that run against A. This endpoint returns the
    union of both — every suite homed to the agent (so an empty Drafts is still
    visible as the place new evals land) plus every suite holding a case that
    resolves for it — and puts in each suite row ONLY the case ids that actually
    resolve. The panel can then show a suite tree whose visible cases are
    exactly the ones a run will execute.

    Cases are returned as explicit ids rather than the caller running a
    ``suite_id``: a suite-level run takes the whole suite, which is a different
    (and for a shared suite, wrong) set than the rows shown here.

    Global instructions have no agents of their own. They resolve to every agent
    in the org, so they get the org-wide 'global' group (agent-less cases, which
    run against everything) plus — via ``agent_ids`` — the agents of the
    conversation the edit was made in, which is the part the author can act on.
    """
    from app.models.eval import (
        TestCase, TestSuite, TEST_CASE_STATUS_ACTIVE, DEFAULT_DRAFTS_SUITE_NAME,
    )
    from app.models.data_source import DataSource as _DS
    from app.services.agent_reliability_service import AgentReliabilityService

    existing = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if existing is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")
    if not await instruction_service.user_can_view_instruction(db, existing, current_user, organization):
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction not found")

    org_id = str(organization.id)
    own_ds_ids = [str(ds.id) for ds in (existing.data_sources or [])]
    is_global = len(own_ds_ids) == 0
    rel = AgentReliabilityService()

    # --- which agents does this panel speak for? -----------------------------
    if is_global:
        agent_scope = await _resolved_eval_agent_scope(db, current_user, organization, agent_ids)
        scope_source = "report" if agent_scope else "none"
    else:
        agent_scope = own_ds_ids
        scope_source = "instruction"

    agent_names = {}
    if agent_scope:
        rows = (await db.execute(
            select(_DS.id, _DS.name).where(_DS.id.in_(agent_scope), _DS.deleted_at.is_(None))
        )).all()
        agent_names = {str(i): n for i, n in rows}
        agent_scope = [a for a in agent_scope if a in agent_names]

    # --- suite shelf, read once ----------------------------------------------
    suite_rows = (await db.execute(
        select(TestSuite.id, TestSuite.name, TestSuite.data_source_id).where(
            TestSuite.organization_id == org_id, TestSuite.deleted_at.is_(None)
        )
    )).all()
    suites_by_id = {
        str(i): {"id": str(i), "name": n, "data_source_id": (str(d) if d else None)}
        for i, n, d in suite_rows
    }

    async def _global_case_ids() -> list:
        """Active cases targeting NO agent — they run against every agent, and
        are the only ones a global instruction can be said to own."""
        rows = (await db.execute(
            select(TestCase.id, TestCase.data_source_ids_json)
            .join(TestSuite, TestSuite.id == TestCase.suite_id)
            .where(
                TestSuite.organization_id == org_id,
                TestCase.status == TEST_CASE_STATUS_ACTIVE,
                TestCase.deleted_at.is_(None),
            )
        )).all()
        return [
            str(i) for i, ds in rows
            if not (isinstance(ds, list) and len(ds) > 0)
        ]

    async def _group(scope: str, label: str, exclude: set) -> dict:
        if scope == "global":
            case_ids = await _global_case_ids()
        else:
            case_ids = await rel.list_agent_eval_case_ids(db, org_id, scope)
        case_ids = [c for c in case_ids if c not in exclude]

        cases = []
        if case_ids:
            rows = (await db.execute(
                select(TestCase.id, TestCase.name, TestCase.suite_id)
                .where(TestCase.id.in_(case_ids))
                .order_by(TestCase.created_at.asc())
            )).all()
            cases = [{"id": str(i), "name": n, "suite_id": str(s)} for i, n, s in rows]

        by_suite = {}
        for c in cases:
            by_suite.setdefault(c["suite_id"], []).append(c)

        # Every suite homed here shows up even at zero resolving cases: that is
        # where the agent's next eval will be filed, and the tree shows it too.
        homed = {
            sid for sid, s in suites_by_id.items()
            if s["data_source_id"] == (None if scope == "global" else scope)
        }

        suites = []
        for sid in set(by_suite) | homed:
            meta = suites_by_id.get(sid)
            if meta is None:
                continue  # case filed in a suite from another org — not ours to show
            rows_for_suite = by_suite.get(sid, [])
            suites.append({
                "id": sid,
                "name": meta["name"],
                "is_home": sid in homed,
                "is_drafts": meta["name"] == DEFAULT_DRAFTS_SUITE_NAME,
                "case_count": len(rows_for_suite),
                "cases": [{"id": c["id"], "name": c["name"]} for c in rows_for_suite],
            })
        # Runnable suites first, then the empty shelves, each alphabetically —
        # an empty Drafts should never push what actually runs below the fold.
        suites.sort(key=lambda s: (s["case_count"] == 0, s["name"].lower()))
        return {
            "scope": scope,
            "label": label,
            "case_count": len(cases),
            "suites": suites,
        }

    groups = []
    claimed: set = set()
    if is_global:
        g = await _group("global", "Org-wide", claimed)
        claimed.update(c["id"] for s in g["suites"] for c in s["cases"])
        groups.append(g)
    for a in agent_scope:
        # `claimed` also de-duplicates BETWEEN agents: an agent-less case
        # resolves for every one of them and would otherwise be listed, and run,
        # once per group.
        g = await _group(a, agent_names[a], claimed)
        claimed.update(c["id"] for s in g["suites"] for c in s["cases"])
        groups.append(g)

    all_cases = [c for g in groups for s in g["suites"] for c in s["cases"]]
    return {
        "instruction_id": instruction_id,
        "is_global": is_global,
        "scope_source": scope_source,
        "data_source_ids": own_ds_ids,
        "agent_count": len(agent_scope),
        "groups": groups,
        # Flat union, in group order — the run payload and the legacy shape.
        "case_count": len(all_cases),
        "cases": all_cases,
    }


@router.get("/instructions/{instruction_id}/builds/{build_id}/verdict")
async def get_instruction_build_verdict(
    instruction_id: str,
    build_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """What a reviewer decided about this build's change to this instruction:
    pending | accepted | rejected | unknown.

    Read from the recorded per-hunk verdicts, so the tool block can state what
    happened instead of inferring it from whether the live text still matches
    the proposal — an inference that reported "rejected" for anything pending,
    and for anything accepted that main later moved past.
    """
    result = await instruction_service.build_verdict(
        db, instruction_id, build_id, organization=organization, current_user=current_user
    )
    if result is None:
        raise AppError.not_found(ErrorCode.INSTRUCTION_NOT_FOUND, "Instruction or build not found")
    return result


@router.get("/instructions/{instruction_id}/pending-builds")
async def get_instruction_pending_builds(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """List all pending/draft builds containing this instruction, with the
    pending version text. Used by the tracked-changes UI to show suggested
    edits awaiting approval.

    Pendingness follows the same authoritative per-hunk rule as
    GET /review-hunks (see live_hunks_by_build below), so this surface can
    never disagree with the /agents tracked-changes review about whether an
    instruction still has changes to review."""
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
    else:
        # Instruction absent from main. When the org HAS a main build this is a
        # CREATE suggestion whose live baseline is empty text — run it through
        # the same per-hunk rule as edits (mirrors _main_text_of), so a create
        # rejected in the per-hunk review resolves here too instead of
        # resurfacing as a whole snapshot. main_text stays None only for legacy
        # orgs that predate main-build content.
        has_main_build = (await db.execute(
            select(InstructionBuild.id).where(and_(
                InstructionBuild.organization_id == str(organization.id),
                InstructionBuild.is_main.is_(True),
                InstructionBuild.deleted_at.is_(None),
            )).limit(1)
        )).scalar_one_or_none()
        if has_main_build:
            main_text = ""

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

    # The authoritative per-hunk rule — same as GET /review-hunks: once the
    # instruction exists in main, a suggestion build is only pending while it
    # still has at least one LIVE hunk (not resolved via hunks/accept|reject,
    # still anchorable onto current main, and actually changing it). Without
    # this, a suggestion fully rejected in the per-hunk review kept resurfacing
    # here — e.g. the Report agent panel showed "changes" the /agents review
    # had already dropped. `None` marks a create suggestion (the instruction
    # isn't in main yet), which stays reviewable as a whole snapshot.
    from app.services.text_hunks import rebased_hunks_against_main
    live_hunks_by_build: dict = {}
    for _c, build, version in rows:
        if main_text is None:
            live_hunks_by_build[str(build.id)] = None
            continue
        rejected = instruction_service._rejected_keys(build, instruction_id)
        live_hunks_by_build[str(build.id)] = [
            h for h in rebased_hunks_against_main(_base_text_for(build), version.text or "", main_text)
            if h["key"] not in rejected
        ]

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
        # Authoritative per-hunk rule: everything this build proposed was
        # already resolved (accepted/rejected) or no longer applies to main.
        live = live_hunks_by_build.get(str(build.id))
        if live is not None and not live:
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
        # Render exactly the LIVE hunks: main + this build's unresolved changes.
        # Diffing the raw snapshot against live text would resurrect hunks the
        # user already rejected/accepted in the per-hunk review. Creates (not in
        # main yet) keep the raw snapshot — the whole text IS the suggestion.
        live = live_hunks_by_build.get(str(build.id))
        if live is None:
            pending_text = version.text or ""
            base_text = _base_text_for(build)
        else:
            pending_text = main_text or ""
            for h in sorted(live, key=lambda x: x["start"], reverse=True):
                pending_text = pending_text[:h["start"]] + h["after"] + pending_text[h["end"]:]
            base_text = main_text or ""
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
            "pending_text": pending_text,
            # The text this suggestion forked from — lets the client rebase the
            # intended change onto current text (3-way merge) so siblings stay
            # applicable after one is accepted.
            "base_text": base_text,
            # False = a create suggestion (instruction not in main yet): clients
            # resolve it via build publish/discard, not the per-hunk endpoints.
            "in_main": live is not None,
            "pending_title": version.title,
            # Build-level "commit message": auto-generated summary + free-text rationale.
            "build_title": build.title,
            "message": build.description,
            # Provenance: which report/completion generated this suggestion (AI only).
            "report_id": (trace or {}).get("report_id"),
            "completion_id": (trace or {}).get("completion_id"),
        })
    return result
