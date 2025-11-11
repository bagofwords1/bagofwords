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
    InstructionPrivateStatus,
    InstructionGlobalStatus,
    InstructionCategory
)
from app.models.instruction import Instruction
from app.schemas.instruction_analysis_schema import (
    InstructionAnalysisRequest,
    InstructionAnalysisResponse,
)

router = APIRouter(tags=["instructions"])
instruction_service = InstructionService()

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
@router.get("/instructions", response_model=List[InstructionListSchema])
@requires_permission('view_instructions')
async def get_instructions(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    status: Optional[InstructionStatus] = Query(None),
    category: Optional[InstructionCategory] = Query(None),
    include_own: bool = Query(True),
    include_drafts: bool = Query(False),
    include_archived: bool = Query(False), 
    include_hidden: bool = Query(False),
    user_id: Optional[str] = Query(None),
    data_source_id: Optional[str] = Query(None, description="Filter by data source id"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get instructions with automatic permission-based filtering"""
    return await instruction_service.get_instructions(
        db, organization, current_user,
        skip=skip, limit=limit,
        status=status.value if status else None,
        category=category.value if category else None,
        include_own=include_own,
        include_drafts=include_drafts,
        include_archived=include_archived,
        include_hidden=include_hidden,
        user_id=user_id,
        data_source_id=data_source_id
    )

# SUGGESTION WORKFLOW
@router.post("/instructions/{instruction_id}/suggest", response_model=InstructionSchema)
@requires_permission('suggest_instructions')
async def suggest_instruction(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """User promotes their private instruction to suggestion - Private Published -> Suggested"""
    return await instruction_service.suggest_instruction(db, instruction_id, current_user, organization)

@router.post("/instructions/{instruction_id}/withdraw", response_model=InstructionSchema)
@requires_permission('suggest_instructions')
async def withdraw_suggestion(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """User withdraws their suggestion back to private - Suggested -> Private Published"""
    return await instruction_service.withdraw_suggestion(db, instruction_id, current_user, organization)

@router.post("/instructions/{instruction_id}/approve", response_model=InstructionSchema)
@requires_permission('review_suggestions')
async def approve_suggestion(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Admin approves suggestion, making it global - Suggested -> Global Published"""
    return await instruction_service.approve_suggestion(db, instruction_id, current_user, organization)

@router.post("/instructions/{instruction_id}/reject", response_model=InstructionSchema)
@requires_permission('review_suggestions')
async def reject_suggestion(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Admin rejects suggestion, returning it to private - Suggested -> Private Published"""
    return await instruction_service.reject_suggestion(db, instruction_id, current_user, organization)

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

@router.get("/instructions/private-statuses", response_model=List[str])
@requires_permission('view_instructions')
async def get_instruction_private_statuses():
    """Get all available private instruction statuses"""
    return [status.value for status in InstructionPrivateStatus]

@router.get("/instructions/global-statuses", response_model=List[str])
@requires_permission('view_instructions')
async def get_instruction_global_statuses():
    """Get all available global instruction statuses"""
    return [status.value for status in InstructionGlobalStatus]


@router.post("/instructions/analysis", response_model=InstructionAnalysisResponse)
@requires_permission('view_instructions')
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

