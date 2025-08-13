from typing import List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_

from app.models.instruction_reference import InstructionReference
from app.models.metadata_resource import MetadataResource
from app.models.datasource_table import DataSourceTable
from app.models.memory import Memory
from app.models.organization import Organization

from app.schemas.instruction_reference_schema import (
    InstructionReferenceCreate,
    InstructionReferenceSchema,
)


class InstructionReferenceService:
    async def list_for_instruction(self, db: AsyncSession, instruction_id: str) -> List[InstructionReferenceSchema]:
        stmt = select(InstructionReference).where(
            and_(InstructionReference.instruction_id == instruction_id, InstructionReference.deleted_at == None)
        )
        res = await db.execute(stmt)
        items = res.scalars().all()
        return [InstructionReferenceSchema.from_orm(i) for i in items]

    async def replace_for_instruction(
        self,
        db: AsyncSession,
        instruction_id: str,
        references: List[InstructionReferenceCreate],
        organization: Organization,
    ) -> List[InstructionReferenceSchema]:
        # Delete existing
        stmt = select(InstructionReference).where(
            and_(InstructionReference.instruction_id == instruction_id, InstructionReference.deleted_at == None)
        )
        res = await db.execute(stmt)
        existing = res.scalars().all()
        for ref in existing:
            db.delete(ref)
        await db.flush()

        created: List[InstructionReference] = []
        for ref in references or []:
            await self._validate_reference(db, ref, organization)
            model = InstructionReference(
                instruction_id=instruction_id,
                object_type=ref.object_type,
                object_id=ref.object_id,
                column_name=ref.column_name,
                relation_type=ref.relation_type,
                display_text=ref.display_text,
            )
            db.add(model)
            created.append(model)

        await db.commit()
        for m in created:
            await db.refresh(m)
        return [InstructionReferenceSchema.from_orm(m) for m in created]

    async def _validate_reference(
        self,
        db: AsyncSession,
        ref: InstructionReferenceCreate,
        organization: Organization,
    ) -> None:
        # Validate object exists and belongs to org where applicable
        if ref.object_type == "metadata_resource":
            q = select(MetadataResource).where(
                and_(MetadataResource.id == ref.object_id)
            )
            res = await db.execute(q)
            obj = res.scalar_one_or_none()
            if not obj:
                raise ValueError("metadata_resource not found")
        elif ref.object_type == "datasource_table":
            q = select(DataSourceTable).where(DataSourceTable.id == ref.object_id)
            res = await db.execute(q)
            obj = res.scalar_one_or_none()
            if not obj:
                raise ValueError("datasource_table not found")
        elif ref.object_type == "memory":
            q = select(Memory).where(Memory.id == ref.object_id)
            res = await db.execute(q)
            obj = res.scalar_one_or_none()
            if not obj:
                raise ValueError("memory not found")
        else:
            raise ValueError("unsupported object_type")

