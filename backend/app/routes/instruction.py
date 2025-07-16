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
    InstructionCategory
)
from app.models.instruction import Instruction

router = APIRouter(tags=["instructions"])
instruction_service = InstructionService()

@router.post("/instructions", response_model=InstructionSchema)
@requires_permission('create_instructions') 
async def create_instruction(
    instruction: InstructionCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Create a new instruction"""
    return await instruction_service.create_instruction(db, instruction, current_user, organization)

@router.get("/instructions", response_model=List[InstructionSchema])
@requires_permission('view_instructions') 
async def get_instructions(
    skip: int = Query(0, ge=0, description="Number of instructions to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of instructions to return"),
    status: Optional[InstructionStatus] = Query(None, description="Filter by status"),
    category: Optional[InstructionCategory] = Query(None, description="Filter by category"),
    data_source_id: Optional[str] = Query(None, description="Filter by data source (includes global instructions)"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get instructions with optional filtering"""
    return await instruction_service.get_instructions(
        db, 
        organization, 
        current_user, 
        skip=skip, 
        limit=limit,
        status=status,
        category=category,
        data_source_id=data_source_id
    )

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
    """Update an instruction"""
    updated_instruction = await instruction_service.update_instruction(
        db, instruction_id, instruction, organization, current_user
    )
    if updated_instruction is None:
        raise HTTPException(status_code=404, detail="Instruction not found")
    return updated_instruction

@router.delete("/instructions/{instruction_id}")
@requires_permission('delete_instructions', model=Instruction)
async def delete_instruction(
    instruction_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Delete an instruction (soft delete)"""
    success = await instruction_service.delete_instruction(db, instruction_id, organization, current_user)
    if not success:
        raise HTTPException(status_code=404, detail="Instruction not found")
    return {"message": "Instruction deleted successfully"}

@router.get("/data_sources/{data_source_id}/instructions", response_model=List[InstructionSchema])
@requires_permission('view_data_source')
async def get_instructions_for_data_source(
    data_source_id: str,
    status: InstructionStatus = Query(InstructionStatus.PUBLISHED, description="Filter by status"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all instructions that apply to a specific data source (including global ones)"""
    return await instruction_service.get_instructions_for_data_source(
        db, data_source_id, organization, current_user, status=status
    )

@router.get("/instructions/categories", response_model=List[str])
async def get_instruction_categories():
    """Get all available instruction categories"""
    return [category.value for category in InstructionCategory]

@router.get("/instructions/statuses", response_model=List[str])
async def get_instruction_statuses():
    """Get all available instruction statuses"""
    return [status.value for status in InstructionStatus]
