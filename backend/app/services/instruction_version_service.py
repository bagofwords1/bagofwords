from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import and_, func
from typing import List, Optional, Dict, Any
import hashlib
import json

from app.models.instruction_version import InstructionVersion
from app.models.instruction import Instruction


class InstructionVersionService:
    """
    Service for managing InstructionVersion records.
    Handles creation, retrieval, and content hashing.
    """
    
    async def create_version(
        self,
        db: AsyncSession,
        instruction: Instruction,
        user_id: Optional[str] = None,
    ) -> InstructionVersion:
        """
        Create a new version by snapshotting the current instruction state.
        
        Copies: text, title, structured_data, formatted_content, load_mode,
        references (as JSON), data_sources (as IDs), labels (as IDs), categories.
        Computes content_hash from all versioned fields.
        """
        # Get next version number
        version_number = await self.get_next_version_number(db, instruction.id)
        
        # Extract relationship IDs
        data_source_ids = [ds.id for ds in instruction.data_sources] if instruction.data_sources else None
        label_ids = [label.id for label in instruction.labels] if instruction.labels else None
        
        # Extract references as JSON
        references_json = None
        if instruction.references:
            references_json = [
                {
                    "object_type": ref.object_type,
                    "object_id": ref.object_id,
                    "column_name": ref.column_name,
                    "display_text": ref.display_text,
                }
                for ref in instruction.references
            ]
        
        # Handle category - could be string or list
        category_ids = None
        if instruction.category:
            # If it's already a list/JSON, use as is; otherwise wrap in list
            if isinstance(instruction.category, list):
                category_ids = instruction.category
            else:
                category_ids = [instruction.category] if instruction.category else None
        
        # Compute content hash (includes status for versioning)
        version_data = {
            "text": instruction.text,
            "title": instruction.title,
            "structured_data": instruction.structured_data,
            "formatted_content": instruction.formatted_content,
            "status": instruction.status,
            "load_mode": instruction.load_mode,
            "references_json": references_json,
            "data_source_ids": data_source_ids,
            "label_ids": label_ids,
            "category_ids": category_ids,
        }
        content_hash = self.compute_content_hash(version_data)
        
        # Create version
        version = InstructionVersion(
            instruction_id=instruction.id,
            version_number=version_number,
            text=instruction.text,
            title=instruction.title,
            structured_data=instruction.structured_data,
            formatted_content=instruction.formatted_content,
            status=instruction.status,
            load_mode=instruction.load_mode or 'always',
            references_json=references_json,
            data_source_ids=data_source_ids,
            label_ids=label_ids,
            category_ids=category_ids,
            content_hash=content_hash,
            created_by_user_id=user_id,
        )
        
        db.add(version)
        await db.commit()
        await db.refresh(version)
        
        return version
    
    async def create_version_from_data(
        self,
        db: AsyncSession,
        instruction_id: str,
        text: str,
        title: Optional[str] = None,
        structured_data: Optional[dict] = None,
        formatted_content: Optional[str] = None,
        status: str = 'published',
        load_mode: str = 'always',
        references_json: Optional[list] = None,
        data_source_ids: Optional[list] = None,
        label_ids: Optional[list] = None,
        category_ids: Optional[list] = None,
        user_id: Optional[str] = None,
    ) -> InstructionVersion:
        """
        Create a version from explicit data.
        Used for AI suggestions, git sync, or direct version creation.
        """
        # Get next version number
        version_number = await self.get_next_version_number(db, instruction_id)
        
        # Compute content hash
        version_data = {
            "text": text,
            "title": title,
            "structured_data": structured_data,
            "formatted_content": formatted_content,
            "status": status,
            "load_mode": load_mode,
            "references_json": references_json,
            "data_source_ids": data_source_ids,
            "label_ids": label_ids,
            "category_ids": category_ids,
        }
        content_hash = self.compute_content_hash(version_data)
        
        # Create version
        version = InstructionVersion(
            instruction_id=instruction_id,
            version_number=version_number,
            text=text,
            title=title,
            structured_data=structured_data,
            formatted_content=formatted_content,
            status=status,
            load_mode=load_mode,
            references_json=references_json,
            data_source_ids=data_source_ids,
            label_ids=label_ids,
            category_ids=category_ids,
            content_hash=content_hash,
            created_by_user_id=user_id,
        )
        
        db.add(version)
        await db.commit()
        await db.refresh(version)
        
        return version
    
    async def get_version(
        self,
        db: AsyncSession,
        version_id: str,
    ) -> Optional[InstructionVersion]:
        """Get a specific version by ID."""
        result = await db.execute(
            select(InstructionVersion)
            .options(selectinload(InstructionVersion.instruction))
            .where(
                and_(
                    InstructionVersion.id == version_id,
                    InstructionVersion.deleted_at == None
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def get_versions(
        self,
        db: AsyncSession,
        instruction_id: str,
        skip: int = 0,
        limit: int = 50,
    ) -> Dict[str, Any]:
        """Get all versions of an instruction with pagination."""
        # Count total
        count_query = select(func.count()).select_from(
            select(InstructionVersion).where(
                and_(
                    InstructionVersion.instruction_id == instruction_id,
                    InstructionVersion.deleted_at == None
                )
            ).subquery()
        )
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0
        
        # Fetch versions
        query = (
            select(InstructionVersion)
            .where(
                and_(
                    InstructionVersion.instruction_id == instruction_id,
                    InstructionVersion.deleted_at == None
                )
            )
            .order_by(InstructionVersion.version_number.desc())
            .offset(skip)
            .limit(limit)
        )
        
        result = await db.execute(query)
        versions = list(result.scalars().all())
        
        return {
            "items": versions,
            "total": total,
            "page": (skip // limit) + 1 if limit > 0 else 1,
            "per_page": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 1
        }
    
    async def get_latest_version(
        self,
        db: AsyncSession,
        instruction_id: str,
    ) -> Optional[InstructionVersion]:
        """Get the latest version of an instruction."""
        result = await db.execute(
            select(InstructionVersion)
            .where(
                and_(
                    InstructionVersion.instruction_id == instruction_id,
                    InstructionVersion.deleted_at == None
                )
            )
            .order_by(InstructionVersion.version_number.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
    
    async def get_next_version_number(
        self,
        db: AsyncSession,
        instruction_id: str,
    ) -> int:
        """Get the next version number for an instruction."""
        result = await db.execute(
            select(func.max(InstructionVersion.version_number))
            .where(InstructionVersion.instruction_id == instruction_id)
        )
        max_version = result.scalar() or 0
        return max_version + 1
    
    def compute_content_hash(self, version_data: dict) -> str:
        """
        Compute SHA-256 hash of the version content for change detection.
        Uses a consistent JSON serialization for reproducibility.
        """
        # Sort keys and handle None values consistently
        normalized_data = {}
        for key, value in sorted(version_data.items()):
            if value is None:
                normalized_data[key] = None
            elif isinstance(value, (list, dict)):
                # Serialize complex types consistently
                normalized_data[key] = json.dumps(value, sort_keys=True)
            else:
                normalized_data[key] = str(value)
        
        # Create hash
        content_string = json.dumps(normalized_data, sort_keys=True)
        return hashlib.sha256(content_string.encode('utf-8')).hexdigest()
    
    async def has_content_changed(
        self,
        db: AsyncSession,
        instruction: Instruction,
    ) -> bool:
        """
        Check if instruction content has changed since the last version.
        Compares current instruction state against the latest version's hash.
        """
        latest_version = await self.get_latest_version(db, instruction.id)
        
        if not latest_version:
            # No versions exist, so content is "changed"
            return True
        
        # Compute current hash
        data_source_ids = [ds.id for ds in instruction.data_sources] if instruction.data_sources else None
        label_ids = [label.id for label in instruction.labels] if instruction.labels else None
        
        references_json = None
        if instruction.references:
            references_json = [
                {
                    "object_type": ref.object_type,
                    "object_id": ref.object_id,
                    "column_name": ref.column_name,
                    "display_text": ref.display_text,
                }
                for ref in instruction.references
            ]
        
        category_ids = None
        if instruction.category:
            if isinstance(instruction.category, list):
                category_ids = instruction.category
            else:
                category_ids = [instruction.category] if instruction.category else None
        
        current_data = {
            "text": instruction.text,
            "title": instruction.title,
            "structured_data": instruction.structured_data,
            "formatted_content": instruction.formatted_content,
            "status": instruction.status,
            "load_mode": instruction.load_mode,
            "references_json": references_json,
            "data_source_ids": data_source_ids,
            "label_ids": label_ids,
            "category_ids": category_ids,
        }
        current_hash = self.compute_content_hash(current_data)
        
        return current_hash != latest_version.content_hash
    
    async def compare_versions(
        self,
        db: AsyncSession,
        version_id_a: str,
        version_id_b: str,
    ) -> Dict[str, Any]:
        """
        Compare two versions and return the differences.
        """
        version_a = await self.get_version(db, version_id_a)
        version_b = await self.get_version(db, version_id_b)
        
        if not version_a or not version_b:
            return {"error": "One or both versions not found"}
        
        changes = []
        fields = ['text', 'title', 'structured_data', 'formatted_content', 
                  'status', 'load_mode', 'references_json', 'data_source_ids', 
                  'label_ids', 'category_ids']
        
        for field in fields:
            val_a = getattr(version_a, field)
            val_b = getattr(version_b, field)
            if val_a != val_b:
                changes.append({
                    "field": field,
                    "from": val_a,
                    "to": val_b,
                })
        
        return {
            "version_a": {
                "id": version_a.id,
                "version_number": version_a.version_number,
            },
            "version_b": {
                "id": version_b.id,
                "version_number": version_b.version_number,
            },
            "changes": changes,
            "has_changes": len(changes) > 0,
        }

