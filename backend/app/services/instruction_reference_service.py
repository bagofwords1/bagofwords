from typing import List, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, delete

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
            and_(InstructionReference.instruction_id == instruction_id, InstructionReference.deleted_at.is_(None))
        )
        res = await db.execute(stmt)
        items = res.scalars().all()
        
        # Populate referenced objects for each item
        populated_items = []
        for item in items:
            item_data = InstructionReferenceSchema.from_orm(item).model_dump()
            
            # Fetch and add the referenced object
            referenced_obj = await self._fetch_referenced_object(db, item.object_type, item.object_id)
            if referenced_obj:
                if item.object_type == "metadata_resource":
                    from app.schemas.metadata_resource_schema import MetadataResourceSchema
                    item_data["object"] = MetadataResourceSchema.from_orm(referenced_obj).model_dump()
                elif item.object_type == "datasource_table":
                    from app.schemas.datasource_table_schema import DataSourceTableSchema
                    item_data["object"] = DataSourceTableSchema.from_orm(referenced_obj).model_dump()
                elif item.object_type == "memory":
                    from app.schemas.memory_schema import MemorySchema
                    item_data["object"] = MemorySchema.from_orm(referenced_obj).model_dump()
            
            populated_items.append(InstructionReferenceSchema(**item_data))
        
        return populated_items

    async def replace_for_instruction(
        self,
        db: AsyncSession,
        instruction_id: str,
        references: List[InstructionReferenceCreate],
        organization: Organization,
        data_source_ids: Optional[List[str]] = None,
    ) -> List[InstructionReferenceSchema]:
        # Delete existing references
        await db.execute(
            delete(InstructionReference).where(
                and_(InstructionReference.instruction_id == instruction_id)
            )
        )

        await db.flush()

        created: List[InstructionReference] = []
        for ref in references or []:
            validated_obj = await self._validate_reference(db, ref, organization, data_source_ids)
            
            # Get display text from the validated object if not provided
            display_text = ref.display_text
            if not display_text and validated_obj:
                if hasattr(validated_obj, 'name'):
                    display_text = validated_obj.name
                elif hasattr(validated_obj, 'title'):
                    display_text = validated_obj.title
                else:
                    display_text = f"{ref.object_type}_{ref.object_id}"
            
            model = InstructionReference(
                instruction_id=instruction_id,
                object_type=ref.object_type,
                object_id=ref.object_id,
                column_name=ref.column_name,
                relation_type=ref.relation_type,
                display_text=display_text,
            )
            db.add(model)
            created.append(model)

        await db.flush()  # Flush changes to get IDs, but don't commit yet
        for m in created:
            await db.refresh(m)
        return [InstructionReferenceSchema.from_orm(m) for m in created]

    async def _validate_reference(
        self,
        db: AsyncSession,
        ref: InstructionReferenceCreate,
        organization: Organization,
        data_source_ids: Optional[List[str]] = None,
    ) -> Any:
        # Validate object exists and belongs to org where applicable
        if ref.object_type == "metadata_resource":
            q = select(MetadataResource).where(
                and_(MetadataResource.id == ref.object_id)
            )
            res = await db.execute(q)
            obj = res.scalar_one_or_none()
            if not obj:
                raise ValueError("metadata_resource not found")
            
            # Validate data source constraint if specified
            if data_source_ids and obj.data_source_id not in data_source_ids:
                raise ValueError(f"metadata_resource {ref.object_id} does not belong to the selected data sources")
            
            return obj
                
        elif ref.object_type == "datasource_table":
            q = select(DataSourceTable).where(DataSourceTable.id == ref.object_id)
            res = await db.execute(q)
            obj = res.scalar_one_or_none()
            if not obj:
                raise ValueError("datasource_table not found")
            
            # Validate data source constraint if specified
            if data_source_ids and obj.datasource_id not in data_source_ids:
                raise ValueError(f"datasource_table {ref.object_id} does not belong to the selected data sources")
            
            return obj
                
        elif ref.object_type == "memory":
            q = select(Memory).where(Memory.id == ref.object_id)
            res = await db.execute(q)
            obj = res.scalar_one_or_none()
            if not obj:
                raise ValueError("memory not found")
            # Memory is not tied to data sources, so no additional validation needed
            return obj
        else:
            raise ValueError("unsupported object_type")
    
    async def _fetch_referenced_object(self, db: AsyncSession, object_type: str, object_id: str) -> Optional[Any]:
        """Fetch the referenced object by type and ID."""
        try:
            if object_type == "metadata_resource":
                q = select(MetadataResource).where(MetadataResource.id == object_id)
                res = await db.execute(q)
                return res.scalar_one_or_none()
            elif object_type == "datasource_table":
                q = select(DataSourceTable).where(DataSourceTable.id == object_id)
                res = await db.execute(q)
                return res.scalar_one_or_none()
            elif object_type == "memory":
                q = select(Memory).where(Memory.id == object_id)
                res = await db.execute(q)
                return res.scalar_one_or_none()
        except Exception:
            return None
        return None

