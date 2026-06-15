from typing import List, Optional, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import and_, delete, func

from app.models.instruction_reference import InstructionReference
from app.models.instruction import Instruction
from app.models.metadata_resource import MetadataResource
from app.models.datasource_table import DataSourceTable
from app.models.organization import Organization
from app.models.connection_tool import ConnectionTool

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
                elif item.object_type == "instruction":
                    from app.schemas.instruction_schema import InstructionSchema
                    item_data["object"] = InstructionSchema.from_orm(referenced_obj).model_dump()
            
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

        await db.flush()
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

            # Fallback: the UI doesn't always pass a real DataSourceTable.id —
            # the `object_id` can be a connection-table id or a bare table name
            # (with or without a `schema.` prefix), depending on the data source
            # / connection type. If the id lookup missed, resolve the table by
            # name within the instruction's data sources.
            if not obj and data_source_ids:
                # Candidate names to match: object_id and display_text, each
                # tolerating a `schema.table` -> `table` reduction.
                candidates = []
                for raw in (ref.object_id, ref.display_text):
                    if not raw:
                        continue
                    raw = str(raw).strip()
                    candidates.append(raw)
                    if "." in raw:
                        candidates.append(raw.rsplit(".", 1)[-1])
                # De-dup, lowercase for case-insensitive comparison.
                lowered = list({c.lower() for c in candidates if c})
                if lowered:
                    name_q = select(DataSourceTable).where(
                        and_(
                            DataSourceTable.datasource_id.in_(data_source_ids),
                            func.lower(DataSourceTable.name).in_(lowered),
                        )
                    )
                    name_res = await db.execute(name_q)
                    obj = name_res.scalars().first()
                    if obj:
                        # Rewrite the reference so the persisted FK points at the
                        # real DataSourceTable.id.
                        ref.object_id = obj.id

            if not obj:
                raise ValueError("datasource_table not found")

            # Validate data source constraint if specified
            if data_source_ids and obj.datasource_id not in data_source_ids:
                raise ValueError(f"datasource_table {ref.object_id} does not belong to the selected data sources")

            return obj
        elif ref.object_type == "instruction":
            q = select(Instruction).where(
                and_(
                    Instruction.id == ref.object_id,
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at.is_(None),
                )
            )
            res = await db.execute(q)
            obj = res.scalar_one_or_none()
            if not obj:
                raise ValueError("instruction not found")
            return obj
        elif ref.object_type == "connection_tool":
            from app.models.connection import Connection
            q = (
                select(ConnectionTool)
                .join(Connection, ConnectionTool.connection_id == Connection.id)
                .where(
                    and_(
                        ConnectionTool.id == ref.object_id,
                        Connection.organization_id == organization.id,
                        ConnectionTool.deleted_at.is_(None),
                    )
                )
            )
            res = await db.execute(q)
            obj = res.scalar_one_or_none()
            if not obj:
                raise ValueError("connection_tool not found")
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
            elif object_type == "instruction":
                q = select(Instruction).where(Instruction.id == object_id)
                res = await db.execute(q)
                return res.scalar_one_or_none()
            elif object_type == "connection_tool":
                q = select(ConnectionTool).where(ConnectionTool.id == object_id)
                res = await db.execute(q)
                return res.scalar_one_or_none()
        except Exception:
            return None
        return None

