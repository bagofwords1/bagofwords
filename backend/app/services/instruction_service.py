from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import and_, or_
from typing import List, Optional, Any
from fastapi import HTTPException

from app.models.instruction import (
    Instruction,
    instruction_data_source_association,
)
from app.models.data_source import DataSource
from app.models.data_source_membership import DataSourceMembership, PRINCIPAL_TYPE_USER
from app.models.metadata_resource import MetadataResource
from app.models.datasource_table import DataSourceTable
from app.models.memory import Memory
from app.models.user import User
from app.models.organization import Organization
from app.schemas.instruction_schema import (
    InstructionCreate, 
    InstructionUpdate, 
    InstructionSchema, 
)
from app.services.instruction_reference_service import InstructionReferenceService

class InstructionService:
    def __init__(self):
        self.reference_service = InstructionReferenceService()
    
    async def create_instruction(
        self, 
        db: AsyncSession, 
        instruction_data: InstructionCreate, 
        current_user: User, 
        organization: Organization,
        force_global: bool = False
    ) -> InstructionSchema:
        """Create a new instruction following the dual-status matrix"""
        
        # Validate data sources if provided
        if instruction_data.data_source_ids:
            await self._validate_data_sources(db, instruction_data.data_source_ids, organization)
        
        # Convert enum strings coming from the API and extract their values
        raw = instruction_data.dict(exclude={'data_source_ids', 'references'})
        instruction = Instruction(**raw)
        instruction.user_id = current_user.id
        instruction.organization_id = organization.id
            
        if force_global:
            # Global Draft: null, approved, draft (Admin's draft global instruction)
            instruction.private_status = None
            instruction.global_status = "approved"
            instruction.status = instruction_data.status or "draft"  # Use form status
        else:
            # Check if this is a suggestion (user wants to suggest for global)
            if instruction_data.global_status == "suggested":
                # Suggested: published, suggested, draft
                instruction.private_status = "published"
                instruction.global_status = "suggested"
                instruction.status = "draft"  # Changed from "published" to "draft"
            else:
                # Private Published: published, null, published (User's active private instruction)
                instruction.private_status = "published"
                instruction.global_status = None
                instruction.status = "published"  # Always published for private
            
        db.add(instruction)
        await db.commit()
        await db.refresh(instruction)
        
        # Associate with data sources if provided
        if instruction_data.data_source_ids:
            await self._associate_data_sources(db, instruction, instruction_data.data_source_ids)

        # Handle references if provided
        if getattr(instruction_data, "references", None) is not None:
            # Pass data source IDs for validation (empty list means all data sources)
            ds_ids = instruction_data.data_source_ids if instruction_data.data_source_ids else None
            await self.reference_service.replace_for_instruction(db, instruction.id, instruction_data.references or [], organization, ds_ids)
        
        # Load relationships and return
        await db.refresh(instruction, ["user", "data_sources", "reviewed_by", "references"])
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
        include_own: bool = True,
        include_drafts: bool = False,
        include_archived: bool = False,
        include_hidden: bool = False,
        user_id: Optional[str] = None
    ) -> List[InstructionSchema]:
        """Get instructions with clean permission-based filtering"""
        
        user_permissions = await self._get_user_permissions(db, current_user, organization)
        
        # Build the query conditions cleanly
        conditions = []
        
        # Add user's own instructions
        if include_own:
            conditions.append(self._get_own_instructions_condition(current_user.id))
        
        # Add others' instructions based on permissions
        others_condition = self._get_others_instructions_condition(
            current_user.id, 
            user_permissions, 
            include_drafts, 
            include_archived, 
            include_hidden
        )
        if others_condition is not None:
            conditions.append(others_condition)
        
        # Handle admin user filtering
        if user_id and self._can_filter_by_user(user_permissions):
            conditions = [Instruction.user_id == user_id]
        
        # Execute query
        return await self._execute_instructions_query(
            db, organization, conditions, status, category, skip, limit
        )

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
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
            )
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
            
        return await self._instruction_to_schema_with_references(db, instruction)
    
    async def update_instruction(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        instruction_data: InstructionUpdate, 
        organization: Organization,
        current_user: User
    ) -> Optional[InstructionSchema]:
        """Update an instruction with proper permission and workflow handling"""
        
        # Get the instruction
        instruction = await self._get_instruction_by_id(db, instruction_id, organization)
        
        # Determine what type of update this is and check permissions
        update_type = self._determine_update_type(instruction, instruction_data, current_user)
        
        # Handle the update based on type
        if update_type == "admin_review":
            await self._handle_admin_review(instruction, instruction_data, current_user)
        elif update_type == "admin_edit":
            await self._handle_admin_edit(instruction, instruction_data, current_user)
        elif update_type == "owner_edit":
            await self._handle_owner_edit(instruction, instruction_data)
        else:
            raise HTTPException(status_code=403, detail="Permission denied")
        
        # Handle data source associations
        if instruction_data.data_source_ids is not None:
            if instruction_data.data_source_ids:
                await self._validate_data_sources(db, instruction_data.data_source_ids, organization)
            await self._update_data_source_associations(db, instruction, instruction_data.data_source_ids)
        
        # Handle references if provided
        if getattr(instruction_data, "references", None) is not None:
            # Get current data source IDs for the instruction if not provided in update
            ds_ids = instruction_data.data_source_ids
            if ds_ids is None:
                # Get current data source associations
                current_ds_ids = [ds.id for ds in instruction.data_sources] if instruction.data_sources else None
            else:
                current_ds_ids = ds_ids if ds_ids else None
            await self.reference_service.replace_for_instruction(db, instruction.id, instruction_data.references or [], organization, current_ds_ids)

        await db.commit()
        await db.refresh(instruction, ["user", "data_sources", "reviewed_by", "references"])
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
        
        # Check if user can delete (owner or admin for private instructions)
        if not instruction.is_editable_by_user and instruction.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Permission denied")
        
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
        await db.refresh(instruction, ["user", "data_sources", "reviewed_by"])
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
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
            )
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
    
    async def suggest_instruction(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        current_user: User, 
        organization: Organization
    ) -> InstructionSchema:
        """User promotes their private instruction to suggestion"""
        
        instruction = await self._get_instruction_for_user(db, instruction_id, current_user, organization)
        
        if not instruction.can_be_suggested:
            raise HTTPException(status_code=400, detail="Instruction cannot be suggested")
        
        # Transition: Private Published -> Suggested
        # From: published, null, published
        # To: published, suggested, published
        instruction.global_status = "suggested"
        
        await db.commit()
        await db.refresh(instruction, ["user", "data_sources", "reviewed_by"])
        return InstructionSchema.from_orm(instruction)

    async def withdraw_suggestion(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        current_user: User, 
        organization: Organization
    ) -> InstructionSchema:
        """User withdraws their suggestion back to private"""
        
        instruction = await self._get_instruction_for_user(db, instruction_id, current_user, organization)
        
        if not instruction.can_be_withdrawn:
            raise HTTPException(status_code=400, detail="Suggestion cannot be withdrawn")
        
        # Transition: Suggested -> Private Published
        # From: published, suggested, published
        # To: published, null, published
        instruction.global_status = None
        
        await db.commit()
        await db.refresh(instruction, ["user", "data_sources", "reviewed_by"])
        return InstructionSchema.from_orm(instruction)

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

    async def _get_instruction_for_user(
        self, 
        db: AsyncSession, 
        instruction_id: str, 
        user: User, 
        organization: Organization
    ) -> Instruction:
        """Get instruction that belongs to the user"""
        
        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.user_id == user.id,
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")
        
        return instruction

    def _determine_update_type(self, instruction: Instruction, instruction_data: InstructionUpdate, current_user: User) -> str:
        """Determine what type of update this is based on permissions and changes"""
        
        is_admin = self._is_admin(current_user)
        is_owner = instruction.user_id == current_user.id
        is_suggested = instruction.global_status == "suggested"
        has_status_change = instruction_data.status and instruction_data.status != instruction.status
        
        # Admin reviewing a suggested instruction (approve or reject)
        if is_admin and is_suggested and has_status_change:
            if instruction_data.status in ["published", "archived"]:
                return "admin_review"
        
        # Admin editing any instruction (not review)
        elif is_admin:
            return "admin_edit"
        
        # Owner editing their own private instruction
        elif is_owner and instruction.is_editable_by_user:
            return "owner_edit"
        
        # No permission
        else:
            return "no_permission"

    async def _handle_admin_review(self, instruction: Instruction, instruction_data: InstructionUpdate, admin_user: User):
        """Handle admin reviewing a suggested instruction (approve or reject)"""
        if instruction_data.status == "published":
            # APPROVAL: Suggested -> Global Published
            # From: published, suggested, draft
            # To: null, approved, published
            instruction.private_status = None
            instruction.global_status = "approved"
            instruction.status = "published"
            instruction.reviewed_by_user_id = admin_user.id
            
        elif instruction_data.status == "archived":
            # REJECTION: Suggested -> Private Archived  
            # From: published, suggested, draft
            # To: published, rejected, archived
            instruction.private_status = "published"  # Keep as private
            instruction.global_status = "rejected"    # Mark as rejected
            instruction.status = "archived"           # Archive it
            instruction.reviewed_by_user_id = admin_user.id
        
        # Apply other changes from the form (text, category, etc.)
        allowed_fields = ['text', 'category', 'is_seen', 'can_user_toggle']
        for field in allowed_fields:
            if hasattr(instruction_data, field) and getattr(instruction_data, field) is not None:
                setattr(instruction, field, getattr(instruction_data, field))

    async def _handle_admin_edit(self, instruction: Instruction, instruction_data: InstructionUpdate, admin_user: User):
        """Handle admin editing any instruction (not review)"""
        
        # Admin can change status and gets credited as reviewer for status changes  
        if instruction_data.status and instruction_data.status != instruction.status:
            if instruction_data.status in ["published", "archived"]:
                instruction.reviewed_by_user_id = admin_user.id
        
        # Apply all changes (admin has full control)
        update_data = instruction_data.dict(exclude_unset=True, exclude={'data_source_ids', 'references'})
        for field, value in update_data.items():
            setattr(instruction, field, value)

    async def _handle_owner_edit(self, instruction: Instruction, instruction_data: InstructionUpdate):
        """Handle owner editing their own private instruction"""
        
        # Owner can only edit text, category, and archive (not publish)
        allowed_fields = ['text', 'category', 'is_seen', 'can_user_toggle']
        
        # Allow archiving but not publishing
        if instruction_data.status == "archived":
            allowed_fields.append('status')
        elif instruction_data.status == "published":
            raise HTTPException(status_code=403, detail="Only admins can publish instructions")
        
        # Apply allowed changes only
        for field in allowed_fields:
            if hasattr(instruction_data, field) and getattr(instruction_data, field) is not None:
                setattr(instruction, field, getattr(instruction_data, field))

    def _is_admin(self, user: User) -> bool:
        """Check if user has admin permissions"""
        return True

    async def _get_instruction_by_id(self, db: AsyncSession, instruction_id: str, organization: Organization) -> Instruction:
        """Get instruction by ID with proper error handling"""
        
        result = await db.execute(
            select(Instruction).where(
                and_(
                    Instruction.id == instruction_id,
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        instruction = result.scalar_one_or_none()
        
        if not instruction:
            raise HTTPException(status_code=404, detail="Instruction not found")
        
        return instruction

    def _get_own_instructions_condition(self, user_id: str):
        """Simple condition for user's own instructions"""
        return Instruction.user_id == user_id

    def _get_others_instructions_condition(
        self, 
        user_id: str, 
        permissions: set, 
        include_drafts: bool, 
        include_archived: bool, 
        include_hidden: bool
    ):
        """Get condition for viewing others' instructions based on permissions"""
        
        base = [Instruction.user_id != user_id]
        
        if 'create_instructions' in permissions:
            # Admin: see everything with optional filters
            if not include_drafts:
                base.append(Instruction.status != "draft")
            if not include_archived:
                base.append(Instruction.status != "archived")
            if not include_hidden:
                base.append(Instruction.is_seen == True)
            return and_(*base)
        
        elif 'view_instructions' in permissions:
            # Regular user: only published, visible, approved instructions
            base.extend([
                Instruction.status == "published",
                Instruction.is_seen == True,
                or_(
                    Instruction.global_status == "approved",  # Global instructions
                    and_(Instruction.private_status == "published", Instruction.global_status == None)  # Org-visible private
                )
            ])
            return and_(*base)
        
        else:
            # No permission to see others' instructions
            return None

    def _can_filter_by_user(self, permissions: set) -> bool:
        """Check if user can filter by specific user ID"""
        return 'create_instructions' in permissions

    async def _execute_instructions_query(
        self, 
        db: AsyncSession, 
        organization: Organization, 
        conditions: list, 
        status: Optional[str], 
        category: Optional[str], 
        skip: int, 
        limit: int
    ) -> List[InstructionSchema]:
        """Execute the instructions query with given conditions"""
        
        query = (
            select(Instruction)
            .options(
                selectinload(Instruction.user),
                selectinload(Instruction.data_sources),
                selectinload(Instruction.reviewed_by),
                selectinload(Instruction.references),
            )
            .where(
                and_(
                    Instruction.organization_id == organization.id,
                    Instruction.deleted_at == None
                )
            )
        )
        
        # Apply permission-based conditions
        if conditions:
            query = query.where(or_(*conditions))
        else:
            query = query.where(False)  # No access
        
        # Apply filters
        if status:
            query = query.where(Instruction.status == status)
        if category:
            query = query.where(Instruction.category == category)
        
        # Apply pagination and ordering
        query = query.offset(skip).limit(limit).order_by(Instruction.created_at.desc())
        
        result = await db.execute(query)
        instructions = result.scalars().all()
        return await self._instructions_to_schema_with_references(db, instructions)

    async def _get_user_permissions(self, db: AsyncSession, user: User, organization: Organization) -> set:
        """Get user's permissions in the organization"""
        from app.models.membership import Membership, ROLES_PERMISSIONS
        
        stmt = select(Membership).where(
            Membership.user_id == user.id,
            Membership.organization_id == organization.id
        )
        result = await db.execute(stmt)
        membership = result.scalar_one_or_none()
        
        return ROLES_PERMISSIONS.get(membership.role, set()) if membership else set()

    async def get_available_references(
        self,
        db: AsyncSession,
        organization: Organization,
        current_user: User,
        q: Optional[str] = None,
        types: Optional[str] = None,
        data_source_ids: Optional[str] = None,
    ) -> List[dict]:
        """Get available reference objects that user has access to"""
        wanted = set((types or "metadata_resource,datasource_table,memory").split(","))
        items: List[dict] = []

        # Get accessible data sources for the user
        accessible_data_sources = await self._get_accessible_data_source_ids(db, current_user, organization)
        
        # Parse data_source_ids parameter if provided
        target_data_source_ids = None
        if data_source_ids:
            target_data_source_ids = [ds_id.strip() for ds_id in data_source_ids.split(",") if ds_id.strip()]
            # Only include data sources the user has access to
            target_data_source_ids = [ds_id for ds_id in target_data_source_ids if ds_id in accessible_data_sources]

        if "metadata_resource" in wanted:
            # Build query for metadata resources
            stmt = select(MetadataResource).join(DataSource).where(
                DataSource.organization_id == organization.id
            )
            
            # Filter by accessible data sources
            if target_data_source_ids:
                # Filter by specific requested data sources
                stmt = stmt.where(MetadataResource.data_source_id.in_(target_data_source_ids))
            elif accessible_data_sources:
                # Filter by all accessible data sources
                stmt = stmt.where(MetadataResource.data_source_id.in_(accessible_data_sources))
            else:
                # If user has no access to any data sources, return empty for metadata resources
                pass
            
            if q:
                stmt = stmt.where(MetadataResource.name.ilike(f"%{q}%"))
            
            # Only execute if user has access to data sources
            if target_data_source_ids or accessible_data_sources:
                result = await db.execute(stmt)
                for r in result.scalars().all():
                    items.append({
                        "id": r.id,
                        "type": "metadata_resource",
                        "name": r.name,
                        "data_source_id": r.data_source_id,
                    })

        if "datasource_table" in wanted:
            # Build query for datasource tables
            stmt = select(DataSourceTable).join(DataSource).where(
                DataSource.organization_id == organization.id
            )
            
            # Filter by accessible data sources
            if target_data_source_ids:
                # Filter by specific requested data sources
                stmt = stmt.where(DataSourceTable.datasource_id.in_(target_data_source_ids))
            elif accessible_data_sources:
                # Filter by all accessible data sources
                stmt = stmt.where(DataSourceTable.datasource_id.in_(accessible_data_sources))
            else:
                # If user has no access to any data sources, return empty for tables
                pass
            
            if q:
                stmt = stmt.where(DataSourceTable.name.ilike(f"%{q}%"))
            
            # Only execute if user has access to data sources
            if target_data_source_ids or accessible_data_sources:
                result = await db.execute(stmt)
                for t in result.scalars().all():
                    items.append({
                        "id": t.id,
                        "type": "datasource_table", 
                        "name": t.name,
                        "data_source_id": t.datasource_id,
                    })

        if "memory" in wanted:
            # Memories are org-scoped, user can access all in their org
            stmt = select(Memory).where(Memory.organization_id == organization.id)
            
            if q:
                stmt = stmt.where(Memory.title.ilike(f"%{q}%"))
            
            result = await db.execute(stmt)
            for m in result.scalars().all():
                items.append({
                    "id": m.id,
                    "type": "memory",
                    "name": m.title,
                })

        return items

    async def _get_accessible_data_source_ids(
        self, 
        db: AsyncSession, 
        current_user: User, 
        organization: Organization
    ) -> List[str]:
        """Get list of data source IDs that the user has access to"""
        # Query for data sources the user has access to (same logic as data_source_service)
        query = (
            select(DataSource.id)
            .filter(DataSource.organization_id == organization.id)
            .filter(
                or_(
                    DataSource.is_public == True,  # Public data sources
                    DataSource.id.in_(
                        select(DataSourceMembership.data_source_id)
                        .filter(
                            DataSourceMembership.principal_type == PRINCIPAL_TYPE_USER,
                            DataSourceMembership.principal_id == current_user.id
                        )
                    )  # User has explicit membership
                )
            )
        )
        result = await db.execute(query)
        return [row[0] for row in result.fetchall()]
    
    async def _instruction_to_schema_with_references(self, db: AsyncSession, instruction) -> InstructionSchema:
        """Convert instruction to schema with populated references."""
        # Convert to basic schema
        instruction_dict = InstructionSchema.from_orm(instruction).model_dump()
        
        # Populate the referenced objects for each reference
        if instruction.references:
            populated_references = []
            for ref in instruction.references:
                ref_data = ref.__dict__.copy()
                # Remove SQLAlchemy internal attributes
                ref_data = {k: v for k, v in ref_data.items() if not k.startswith('_')}
                
                # Fetch and add the referenced object
                referenced_obj = await self.reference_service._fetch_referenced_object(db, ref.object_type, ref.object_id)
                if referenced_obj:
                    if ref.object_type == "metadata_resource":
                        from app.schemas.metadata_resource_schema import MetadataResourceSchema
                        ref_data["object"] = MetadataResourceSchema.from_orm(referenced_obj).model_dump()
                    elif ref.object_type == "datasource_table":
                        from app.schemas.datasource_table_schema import DataSourceTableSchema
                        ref_data["object"] = DataSourceTableSchema.from_orm(referenced_obj).model_dump()
                    elif ref.object_type == "memory":
                        from app.schemas.memory_schema import MemorySchema
                        ref_data["object"] = MemorySchema.from_orm(referenced_obj).model_dump()
                
                populated_references.append(ref_data)
            
            instruction_dict["references"] = populated_references
        
        return InstructionSchema(**instruction_dict)
    
    async def _instructions_to_schema_with_references(self, db: AsyncSession, instructions) -> List[InstructionSchema]:
        """Convert multiple instructions to schemas with populated references."""
        result = []
        for instruction in instructions:
            schema = await self._instruction_to_schema_with_references(db, instruction)
            result.append(schema)
        return result
