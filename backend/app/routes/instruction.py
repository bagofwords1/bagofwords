from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
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
@requires_permission('create_private_instructions') 
async def create_private_instruction(
    instruction: InstructionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new private instruction (auto-published) - Private Published: published, null, published"""
    return await instruction_service.create_instruction(db, instruction, current_user, organization, force_global=False)

@router.post("/instructions/global", response_model=InstructionSchema)
@requires_permission('create_instructions') 
async def create_global_instruction(
    instruction: InstructionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new global instruction (admin only) - Global Draft/Published: null, approved, draft/published"""
    return await instruction_service.create_instruction(db, instruction, current_user, organization, force_global=True)

# LIST INSTRUCTIONS
@router.get("/instructions")
@requires_permission('view_instructions')
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
    data_source_id: Optional[str] = Query(None, description="Filter by single data source/domain id (deprecated, use data_source_ids)"),
    data_source_ids: Optional[str] = Query(None, description="Comma-separated domain IDs to filter by"),
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
@requires_permission('create_instructions')
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
@requires_permission('delete_instructions')
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
@requires_permission('create_private_instructions')
async def enhance_instruction(
    instruction_data: InstructionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Enhance an instruction with AI"""
    return await instruction_service.enhance_instruction(db, instruction_data, organization, current_user)

@router.get("/instructions/available-references", response_model=List[dict])
@requires_permission('view_instructions')
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
@requires_permission('view_instructions')
async def get_instruction_source_types(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get available source types based on existing instructions (dbt, markdown, user, ai)"""
    return await instruction_service.get_available_source_types(db, organization)


@router.get("/instructions/categories", response_model=List[str])
@requires_permission('view_instructions')
async def get_instruction_categories():
    """Get all available instruction categories"""
    return [category.value for category in InstructionCategory]

@router.get("/instructions/statuses", response_model=List[str])
@requires_permission('view_instructions')
async def get_instruction_statuses():
    """Get all available instruction statuses"""
    return [status.value for status in InstructionStatus]


# LABEL MANAGEMENT
@router.get("/instructions/labels", response_model=List[InstructionLabelSchema])
@requires_permission('view_instructions')
async def list_instruction_labels(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """List instruction labels for the current organization."""
    return await instruction_label_service.list_labels(db, organization, current_user)


@router.post("/instructions/labels", response_model=InstructionLabelSchema)
@requires_permission('create_instructions')
async def create_instruction_label(
    label: InstructionLabelCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Create a new instruction label."""
    return await instruction_label_service.create_label(db, label, organization, current_user)


@router.patch("/instructions/labels/{label_id}", response_model=InstructionLabelSchema)
@requires_permission('update_instructions')
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
@requires_permission('delete_instructions')
async def delete_instruction_label(
    label_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Delete (soft delete) an instruction label."""
    success = await instruction_label_service.delete_label(db, label_id, organization, current_user)
    if not success:
        raise HTTPException(status_code=404, detail="Instruction label not found")
    return {"message": "Label deleted successfully"}


@router.post("/instructions/analysis", response_model=InstructionAnalysisResponse)
@requires_permission('create_instructions')
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
@requires_permission('view_instructions', model=Instruction)
async def get_instruction(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get a specific instruction by ID"""
    instruction = await instruction_service.get_instruction(db, instruction_id, organization, current_user)
    if instruction is None:
        raise HTTPException(status_code=404, detail="Instruction not found")
    return instruction

@router.put("/instructions/{instruction_id}", response_model=InstructionSchema)
@requires_permission('update_instructions', model=Instruction)
async def update_instruction(
    instruction_id: str,
    instruction: InstructionUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Update an instruction (only if private and user owns it)"""
    updated_instruction = await instruction_service.update_instruction(
        db, instruction_id, instruction, organization, current_user
    )
    if updated_instruction is None:
        raise HTTPException(status_code=404, detail="Instruction not found")
    return updated_instruction

@router.delete("/instructions/{instruction_id}")
@requires_permission('delete_instructions', model=Instruction, owner_only=False)
async def delete_instruction(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Delete an instruction (admins can delete any instruction)"""
    success = await instruction_service.delete_instruction(db, instruction_id, organization, current_user)
    if not success:
        raise HTTPException(status_code=404, detail="Instruction not found")
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
@requires_permission('view_instructions')
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
        raise HTTPException(status_code=404, detail="Instruction not found")
    
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


@router.get("/instructions/{instruction_id}/versions/{version_id}", response_model=InstructionVersionSchema)
@requires_permission('view_instructions')
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
        raise HTTPException(status_code=404, detail="Instruction not found")
    
    version = await instruction_version_service.get_version(db, version_id)
    
    if not version:
        raise HTTPException(status_code=404, detail="Version not found")
    
    if version.instruction_id != instruction_id:
        raise HTTPException(status_code=400, detail="Version does not belong to this instruction")
    
    return InstructionVersionSchema.model_validate(version)

