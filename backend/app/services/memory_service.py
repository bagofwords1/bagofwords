from fastapi import UploadFile, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.memory import Memory
from app.models.user import User
from app.models.organization import Organization
from app.schemas.memory_schema import MemorySchema, MemoryCreate
from typing import Optional
from sqlalchemy.future import select
from app.models.widget import Widget
from app.services.widget_service import WidgetService
from app.schemas.widget_schema import WidgetSchema
from app.services.step_service import StepService


class MemoryService:
    def __init__(self):
        self.widget_service = WidgetService()
        self.step_service = StepService()
    async def create_memory(self, db: AsyncSession, memory_create: MemoryCreate, current_user: User, organization: Organization) -> MemorySchema:

        memory = Memory(
            title=memory_create.title,
            description=memory_create.description,
            is_public=memory_create.is_public,
            user_id=current_user.id,
            organization_id=organization.id,
            report_id=memory_create.report_id,
            step_id=memory_create.step_id,
            widget_id=memory_create.widget_id
        )

        db.add(memory)
        await db.commit()
        await db.refresh(memory)

        return MemorySchema.from_orm(memory)

    async def get_memories(self, db: AsyncSession, current_user: User, organization: Organization) -> list[MemorySchema]:
        result = await db.execute(
            select(Memory).filter(
                Memory.organization_id == organization.id
            )
        )
        memories = result.scalars().all()
        return [MemorySchema.from_orm(memory) for memory in memories]

    async def remove_memory(self, db: AsyncSession, memory_id: str, current_user: User, organization: Organization) -> bool:
        memory = await self.get_memory(db, memory_id, current_user, organization)
        memory_model = await db.get(Memory, memory.id)

        if not memory_model:
            raise HTTPException(status_code=404, detail="Memory not found")

        await db.delete(memory_model)
        await db.commit()

        return True

    async def get_memory(self, db: AsyncSession, memory_id: str, current_user: User, organization: Organization) -> MemorySchema:
        result = await db.execute(
            select(Memory).filter(
                Memory.id == memory_id,
                Memory.organization_id == organization.id
            )
        )
        memory = result.scalar_one_or_none()

        if not memory:
            raise HTTPException(status_code=404, detail="Memory not found")

        return MemorySchema.from_orm(memory)
    
    async def rerun_memory_step(self, db: AsyncSession, memory_id: str, current_user: User, organization: Organization) -> MemorySchema:
        memory = await self.get_memory(db, memory_id, current_user, organization)
        new_step = await self.step_service.rerun_step(db, memory.step_id)

        # update memory with the new step id
        memory_model = await db.get(Memory, memory.id)
        memory_model.step_id = new_step.id
        await db.commit()
        await db.refresh(memory_model)
        memory = MemorySchema.from_orm(memory_model)

        return memory
    
    async def get_widget_by_memory(self, db: AsyncSession, memory_id: str, current_user: User, organization: Organization) -> WidgetSchema:
        memory = await self.get_memory(db, memory_id, current_user, organization)
        return await self.widget_service.get_widget_by_id_and_step(db, memory.widget_id, memory.step_id, current_user, organization)
