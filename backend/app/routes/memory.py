from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_db, get_current_organization, get_async_db
from typing import Optional

from app.services.memory_service import MemoryService
from app.schemas.memory_schema import MemorySchema, MemoryCreate
from app.models.user import User
from app.core.auth import current_user
from app.models.organization import Organization
from app.schemas.widget_schema import WidgetSchema
from app.core.permissions_decorator import requires_permission
from app.models.memory import Memory
router = APIRouter(tags=["memories"])
memory_service = MemoryService()

@router.post("/memories", response_model=MemorySchema)
@requires_permission('create_memories')
async def create_memory(
    memory: MemoryCreate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await memory_service.create_memory(db, memory, current_user, organization)

@router.get("/memories", response_model=list[MemorySchema])
@requires_permission('view_memories')
async def get_memories(
current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await memory_service.get_memories(db, current_user, organization)

@router.delete("/memories/{memory_id}")
@requires_permission('delete_memories', model=Memory)
async def remove_memory(
    memory_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await memory_service.remove_memory(db, memory_id, current_user, organization)

@router.get("/memories/{memory_id}", response_model=MemorySchema)
@requires_permission('view_memories', model=Memory)
async def get_memory(
    memory_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await memory_service.get_memory(db, memory_id, current_user, organization)

@router.post("/memories/{memory_id}/refresh", response_model=MemorySchema)
@requires_permission('rerun_memory_step', model=Memory)
async def rerun_memory_step(
    memory_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await memory_service.rerun_memory_step(db, memory_id, current_user, organization)

@router.get("/memories/{memory_id}/widget", response_model=WidgetSchema)
@requires_permission('view_memories', model=Memory)
async def get_widget_by_memory(
    memory_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await memory_service.get_widget_by_memory(db, memory_id, current_user, organization)