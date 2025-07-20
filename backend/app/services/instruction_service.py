from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import and_
from typing import List, Optional
from fastapi import HTTPException

from app.models.instruction import (
    Instruction,
    instruction_data_source_association,
)
from app.models.data_source import DataSource
from app.models.user import User
from app.models.organization import Organization
from app.schemas.instruction_schema import (
    InstructionCreate, 
    InstructionUpdate, 
    InstructionSchema, 
)

class InstructionService:
    
    async def create_instruction(
        self, 
        db: AsyncSession, 
        instruction_data: InstructionCreate, 
        current_user: User, 
        organization: Organization
    ) -> InstructionSchema:
        """Create a new instruction"""
        
        # Validate data sources if provided
        if instruction_data.data_source_ids:
            await self._validate_data_sources(db, instruction_data.data_source_ids, organization)
        
        # Convert enum strings coming from the API and extract their values
        raw = instruction_data.dict(exclude={'data_source_ids'})
        instruction = Instruction(**raw)
        instruction.user_id = current_user.id
        instruction.organization_id = organization.id
        
        db.add(instruction)
        await db.commit()
        await db.refresh(instruction)
        
        # Associate with data sources if provided
        if instruction_data.data_source_ids:
            await self._associate_data_sources(db, instruction, instruction_data.data_source_ids)
        
        # Load relationships and return
        await db.refresh(instruction, ["user", "data_sources"])
        return InstructionSchema.from_orm(instruction)
    
    async def get_instructions(
        self, 
        db: AsyncSession, 
        organization: Organization,
        current_user: User,
        skip: int = 0, 
        limit: int = 100,
        status: Optional[str] = None,
        category: Optional[str] = None,
        data_source_id: Optional[str] = None
    ) -> List[InstructionSchema]:
        """Get instructions with optional filtering"""
        
        query = (
            select(Instruction)
            .options(selectinload(Instruction.user), selectinload(Instruction.data_sources))
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None  # NEW: Filter out soft-deleted instructions
                )
            )
            .offset(skip)
            .limit(limit)
            .order_by(Instruction.created_at.desc())
        )
        
        # Apply filters
        if status:
            query = query.where(Instruction.status == status)
        if category:
            query = query.where(Instruction.category == category)
        if data_source_id:
            # Instructions that either apply globally or to this specific data source
            query = query.where(
                (Instruction.data_sources.any(DataSource.id == data_source_id)) |
                (~Instruction.data_sources.any())
            )
        
        result = await db.execute(query)
        instructions = result.scalars().all()
        return [InstructionSchema.from_orm(instruction) for instruction in instructions]
    
    async def get_instruction(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        organization: Organization,
        current_user: User
    ) -> Optional[InstructionSchema]:
        """Get a single instruction by ID"""
        
        query = (
            select(Instruction)
            .options(selectinload(Instruction.user), selectinload(Instruction.data_sources))
            .where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        
        result = await db.execute(query)
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            return None
            
        return InstructionSchema.from_orm(instruction)
    
    async def update_instruction(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        instruction_data: InstructionUpdate, 
        organization: Organization,
        current_user: User
    ) -> Optional[InstructionSchema]:
        """Update an instruction"""
        
        # Get existing instruction
        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id
                )
            )
        )
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")
        
        # Check if user can update (owner or admin)
        if instruction.user_id != current_user.id:
            # Could add admin check here if needed
            raise HTTPException(status_code=403, detail="Permission denied")
        
        update_data = instruction_data.dict(exclude_unset=True, exclude={'data_source_ids'})

        for field, value in update_data.items():
            setattr(instruction, field, value)
        
        # Handle data source associations
        if instruction_data.data_source_ids is not None:
            if instruction_data.data_source_ids:
                await self._validate_data_sources(db, instruction_data.data_source_ids, organization)
            await self._update_data_source_associations(db, instruction, instruction_data.data_source_ids)
        
        await db.commit()
        await db.refresh(instruction, ["user", "data_sources"])
        return InstructionSchema.from_orm(instruction)
    
    async def delete_instruction(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        organization: Organization,
        current_user: User
    ) -> bool:
        """Delete an instruction (soft delete)"""
        
        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id
                )
            )
        )
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")
        
        # Check if user can delete (owner or admin)
        
        # Soft delete (using BaseSchema's soft delete functionality)
        from datetime import datetime
        instruction.deleted_at = datetime.utcnow()
        await db.commit()
        return True
    
    async def increment_thumbs_up(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        organization: Organization,
        current_user: User
    ) -> InstructionSchema:
        """Increment thumbs up count for an instruction"""
        
        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id
                )
            )
        )
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")
        
        instruction.thumbs_up += 1
        await db.commit()
        await db.refresh(instruction, ["user", "data_sources"])
        return InstructionSchema.from_orm(instruction)
    
    async def get_instructions_for_data_source(
        self, 
        db: AsyncSession, 
        data_source_id: str, 
        organization: Organization,
        current_user: User,
        status: str = "published"
    ) -> List[InstructionSchema]:
        """Get all instructions that apply to a specific data source (including global ones)"""
        
        # Validate data source exists
        await self._validate_data_sources(db, [data_source_id], organization)
        
        query = (
            select(Instruction)
            .options(selectinload(Instruction.user), selectinload(Instruction.data_sources))
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.status == status,
                    Instruction.deleted_at == None,
                    # Either applies to this data source or is global (no data sources)
                    (Instruction.data_sources.any(DataSource.id == data_source_id)) |
                    (~Instruction.data_sources.any())
                )
            )
        ).order_by(Instruction.created_at.desc())
        
        result = await db.execute(query)
        instructions = result.scalars().all()
        return [InstructionSchema.from_orm(instruction) for instruction in instructions]
    
    # Private helper methods
    
    async def _validate_data_sources(
        self, 
        db: AsyncSession, 
        data_source_ids: List[str], 
        organization: Organization
    ):
        """Validate that all data source IDs exist and belong to the organization"""
        
        if not data_source_ids:
            return
        
        result = await db.execute(
            select(DataSource).where(
                and_(
                    DataSource.id.in_(data_source_ids),
                    DataSource.organization_id == organization.id
                )
            )
        )
        found_data_sources = result.scalars().all()
        
        if len(found_data_sources) != len(data_source_ids):
            found_ids = {ds.id for ds in found_data_sources}
            missing_ids = set(data_source_ids) - found_ids
            raise HTTPException(
                status_code=400, 
                detail=f"Data sources not found: {list(missing_ids)}"
            )
    
    async def _associate_data_sources(
        self, 
        db: AsyncSession, 
        instruction: Instruction, 
        data_source_ids: List[str]
    ):
        """Associate instruction with data sources"""
        
        if not data_source_ids:
            return
        
        # Get data source objects
        result = await db.execute(
            select(DataSource).where(DataSource.id.in_(data_source_ids))
        )
        data_sources = result.scalars().all()
        
        # Associate with instruction
        instruction.data_sources = data_sources
        await db.commit()
    
    async def _update_data_source_associations(
        self, 
        db: AsyncSession, 
        instruction: Instruction, 
        data_source_ids: List[str]
    ):
        """Update data source associations for an instruction"""
        
        # Clear existing associations
        instruction.data_sources.clear()
        
        # Add new associations if provided
        if data_source_ids:
            result = await db.execute(
                select(DataSource).where(DataSource.id.in_(data_source_ids))
            )
            data_sources = result.scalars().all()
            instruction.data_sources = data_sources
        
        await db.commit()
