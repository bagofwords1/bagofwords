"""
InstructionSyncService - Syncs MetadataResources to Instructions

This service handles the conversion of git-indexed MetadataResources into
Instructions, including:
- Creating new instructions from resources
- Updating existing instructions when resources change
- Creating pending versions for published instructions
- Archiving instructions when resources are deleted
- Formatting structured data into readable text
- Creating InstructionVersions and adding to builds
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_
from sqlalchemy.orm import selectinload

from app.models.instruction import Instruction
from app.models.metadata_resource import MetadataResource
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.data_source import DataSource
from app.models.git_repository import GitRepository
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.instruction_build import InstructionBuild
from app.models.datasource_table import DataSourceTable
from app.models.instruction_reference import InstructionReference
from app.schemas.organization_settings_schema import OrganizationSettingsConfig

logger = logging.getLogger(__name__)


class InstructionSyncService:
    """Service for syncing MetadataResources to Instructions."""
    
    # Default load modes by resource type
    DEFAULT_LOAD_MODES = {
        # Markdown documents - always load (they're documentation)
        'markdown_document': 'always',
        
        # DBT resources - intelligent loading (search-based)
        'dbt_model': 'intelligent',
        'dbt_metric': 'intelligent',
        'dbt_source': 'intelligent',
        'dbt_seed': 'intelligent',
        'dbt_macro': 'disabled',  # Macros are usually too technical
        'dbt_test': 'disabled',   # Tests are usually not relevant for AI
        'dbt_exposure': 'intelligent',
        
        # LookML resources
        'lookml_view': 'intelligent',
        'lookml_model': 'intelligent',
        'lookml_explore': 'intelligent',
        'lookml_dashboard': 'intelligent',
        
        # Tableau resources
        'tableau_datasource': 'intelligent',
        'tableau_calculation': 'intelligent',
        
        # Dataform resources
        'dataform_table': 'intelligent',
        'dataform_assertion': 'disabled',
        'dataform_operation': 'disabled',
        'dataform_declaration': 'intelligent',
    }
    
    def __init__(self):
        # Import here to avoid circular imports
        from app.services.build_service import BuildService
        from app.services.instruction_version_service import InstructionVersionService
        
        self.build_service = BuildService()
        self.version_service = InstructionVersionService()
    
    async def _get_org_settings(self, db: AsyncSession, organization_id: str) -> Optional[OrganizationSettings]:
        """Get organization settings directly without user context."""
        result = await db.execute(
            select(OrganizationSettings).where(
                OrganizationSettings.organization_id == organization_id
            )
        )
        return result.scalar_one_or_none()
    
    async def sync_resource_to_instruction(
        self,
        db: AsyncSession,
        resource: MetadataResource,
        organization: Organization,
        commit_sha: Optional[str] = None,
        build: Optional[InstructionBuild] = None,
    ) -> Optional[Instruction]:
        """
        Create or update an instruction from a metadata resource.
        
        Args:
            db: Database session
            resource: The metadata resource to sync
            organization: The organization
            commit_sha: Optional git commit SHA
            build: Optional build to add the instruction version to
            
        Returns:
            The created or updated instruction, or None if skipped
        """
        # Re-fetch resource to ensure it's in current session and not expired
        resource_stmt = select(MetadataResource).where(MetadataResource.id == resource.id)
        resource_result = await db.execute(resource_stmt)
        fresh_resource = resource_result.scalar_one_or_none()
        
        if not fresh_resource:
            logger.warning(f"Resource {resource.id} not found in database, skipping sync")
            return None
        
        logger.debug(f"Syncing resource {fresh_resource.id} ({fresh_resource.name}) to instruction")
        
        # Check if there's already an instruction linked to this resource
        existing = await self._find_instruction_for_resource(db, fresh_resource.id)
        
        if existing:
            return await self._handle_existing_instruction(db, existing, fresh_resource, organization, commit_sha, build)
        else:
            # Before creating a new instruction, check if there was an unlinked/deleted one
            # If an instruction was previously unlinked (source_sync_enabled=False), don't recreate it
            unlinked_instruction = await self._find_instruction_for_resource(db, fresh_resource.id, include_deleted=True)
            if unlinked_instruction and not unlinked_instruction.source_sync_enabled:
                logger.debug(f"Skipping resource {fresh_resource.id} - previously unlinked instruction {unlinked_instruction.id} exists")
                return None
            
            return await self._create_instruction_from_resource(db, fresh_resource, organization, commit_sha, build)
    
    async def _find_instruction_for_resource(
        self,
        db: AsyncSession,
        resource_id: str,
        include_deleted: bool = False
    ) -> Optional[Instruction]:
        """Find an existing instruction linked to a metadata resource.
        
        Args:
            resource_id: The metadata resource ID
            include_deleted: If True, also finds soft-deleted instructions
        """
        if include_deleted:
            # Find any instruction (including deleted) - used to check for unlinked instructions
            stmt = select(Instruction).where(
                Instruction.source_metadata_resource_id == resource_id
            ).order_by(Instruction.created_at.desc())  # Get most recent
        else:
            stmt = select(Instruction).where(
                and_(
                    Instruction.source_metadata_resource_id == resource_id,
                    Instruction.deleted_at == None
                )
            )
        result = await db.execute(stmt)
        return result.scalars().first()
    
    async def _get_git_repository_for_resource(
        self,
        db: AsyncSession,
        resource: MetadataResource
    ) -> Optional[GitRepository]:
        """Get the git repository associated with a metadata resource via its indexing job."""
        if not resource.metadata_indexing_job_id:
            return None
        
        # Get the indexing job
        job_stmt = select(MetadataIndexingJob).where(
            MetadataIndexingJob.id == resource.metadata_indexing_job_id
        )
        job_result = await db.execute(job_stmt)
        job = job_result.scalar_one_or_none()
        
        if not job or not job.git_repository_id:
            return None
        
        # Get the git repository
        repo_stmt = select(GitRepository).where(
            GitRepository.id == job.git_repository_id
        )
        repo_result = await db.execute(repo_stmt)
        return repo_result.scalar_one_or_none()
    
    async def _create_instruction_from_resource(
        self,
        db: AsyncSession,
        resource: MetadataResource,
        organization: Organization,
        commit_sha: Optional[str] = None,
        build: Optional[InstructionBuild] = None,
    ) -> Instruction:
        """Create a new instruction from a metadata resource."""
        # Get git repository settings from the resource's indexing job
        git_repo = await self._get_git_repository_for_resource(db, resource)
        auto_publish = git_repo.auto_publish if git_repo else False
        
        # Format the resource content as readable text
        formatted_text = self._format_resource_as_text(resource)
        
        # Ensure text is not empty (required field)
        if not formatted_text or not formatted_text.strip():
            formatted_text = f"# {resource.name}\n\nType: {resource.resource_type}\nPath: {resource.path or 'N/A'}"
        
        # Determine load mode from frontmatter or git repository settings
        load_mode = self._get_load_mode_for_resource(resource, git_repo)
        
        # Parse status from frontmatter (fall back to auto_publish setting)
        frontmatter_status = self._get_frontmatter_value(resource, 'status')
        if frontmatter_status in ('published', 'draft', 'archived'):
            status = frontmatter_status
        else:
            status = 'published' if auto_publish else 'draft'
        
        # Parse category from frontmatter (default: 'general')
        category = self._get_frontmatter_value(resource, 'category') or 'general'
        
        # Derive global_status based on status
        global_status = 'approved' if status == 'published' else None
        
        # Build structured data for storage
        structured_data = self._build_structured_data(resource)
        
        # Get data source info for context (if resource is domain-scoped)
        # For org-level git repos, resource.data_source_id is None
        data_source = None
        if resource.data_source_id:
            data_source_result = await db.execute(
                select(DataSource).where(DataSource.id == resource.data_source_id)
            )
            data_source = data_source_result.scalar_one_or_none()
        
        # Get user_id from git repository (creator of the repo)
        # This is needed for test environments where user_id may still be NOT NULL
        user_id = git_repo.user_id if git_repo else None
        
        instruction = Instruction(
            text=formatted_text,
            title=resource.name,
            source_type='git',
            source_metadata_resource_id=resource.id,
            source_file_path=resource.path,  # Git file path for write-back
            source_git_commit_sha=commit_sha,
            source_sync_enabled=True,
            load_mode=load_mode,
            status=status,
            private_status=None,
            global_status=global_status,
            category=category,
            structured_data=structured_data,
            formatted_content=formatted_text,
            organization_id=organization.id,
            user_id=user_id,  # Use git repo creator as instruction creator
            is_seen=True,
            can_user_toggle=True,
        )
        
        db.add(instruction)
        await db.commit()
        await db.refresh(instruction)
        
        # Associate instruction with the data source (only for domain-scoped resources)
        # Org-level git repos create org-wide instructions with no domain association
        if data_source:
            instruction.data_sources.append(data_source)
            await db.commit()
        
        # Resolve frontmatter references (e.g., table references from Cursor Rules-like format)
        await self._resolve_frontmatter_references(db, instruction, resource)
        
        logger.info(f"Created git instruction {instruction.id} for resource {resource.id} ({resource.name})")
        
        # Update the resource with the instruction link
        # Re-fetch resource to ensure it's attached to current session
        resource_stmt = select(MetadataResource).where(MetadataResource.id == resource.id)
        resource_result = await db.execute(resource_stmt)
        fresh_resource = resource_result.scalar_one_or_none()
        if fresh_resource:
            fresh_resource.instruction_id = instruction.id
            await db.commit()
            logger.debug(f"Linked resource {resource.id} to instruction {instruction.id}")
        
        # === Build System Integration ===
        # Create version and add to build
        if build:
            try:
                # Re-fetch instruction with relationships for version creation
                inst_stmt = (
                    select(Instruction)
                    .options(
                        selectinload(Instruction.data_sources),
                        selectinload(Instruction.labels),
                        selectinload(Instruction.references),
                    )
                    .where(Instruction.id == instruction.id)
                )
                inst_result = await db.execute(inst_stmt)
                instruction_with_rels = inst_result.scalar_one()
                
                # Create the first version
                version = await self.version_service.create_version(
                    db, instruction_with_rels, user_id=user_id
                )
                
                # Update instruction's current version
                instruction_with_rels.current_version_id = version.id
                
                # Add the version to the build
                await self.build_service.add_to_build(
                    db, build.id, instruction_with_rels.id, version.id
                )
                
                await db.commit()
                logger.info(f"Created version {version.id} for git instruction {instruction.id}, added to build {build.id}")
            except Exception as e:
                logger.warning(f"Failed to create version for git instruction {instruction.id}: {e}")
                # Don't fail the instruction creation if versioning fails
        
        logger.info(f"Created instruction {instruction.id} from resource {resource.id} ({resource.resource_type})")
        return instruction
    
    async def _handle_existing_instruction(
        self,
        db: AsyncSession,
        existing: Instruction,
        resource: MetadataResource,
        organization: Organization,
        commit_sha: Optional[str] = None,
        build: Optional[InstructionBuild] = None,
    ) -> Optional[Instruction]:
        """Handle update to an existing instruction."""
        # If unlinked from git, skip
        if not existing.source_sync_enabled:
            logger.debug(f"Skipping unlinked instruction {existing.id}")
            return None
        
        # Format the new content
        new_text = self._format_resource_as_text(resource)
        new_structured_data = self._build_structured_data(resource)
        
        # Check if content actually changed
        if existing.text == new_text and existing.structured_data == new_structured_data:
            # Just update the commit SHA
            existing.source_git_commit_sha = commit_sha
            await db.commit()
            return existing
        
        # Content changed - update instruction directly (all statuses now get versioned)
        existing.text = new_text
        existing.title = resource.name
        existing.structured_data = new_structured_data
        existing.formatted_content = new_text
        existing.source_git_commit_sha = commit_sha
        
        # Update load mode from frontmatter if present
        git_repo = await self._get_git_repository_for_resource(db, resource)
        existing.load_mode = self._get_load_mode_for_resource(resource, git_repo)
        
        # Update status from frontmatter if present
        frontmatter_status = self._get_frontmatter_value(resource, 'status')
        if frontmatter_status in ('published', 'draft', 'archived'):
            existing.status = frontmatter_status
            existing.global_status = 'approved' if frontmatter_status == 'published' else None
        
        # Update category from frontmatter if present
        frontmatter_category = self._get_frontmatter_value(resource, 'category')
        if frontmatter_category:
            existing.category = frontmatter_category
        
        await db.commit()
        await db.refresh(existing, ['data_sources'])
        
        # Re-resolve frontmatter references (clear old ones and create new)
        # First, delete existing references for this instruction
        from sqlalchemy import delete
        await db.execute(
            delete(InstructionReference).where(
                InstructionReference.instruction_id == existing.id
            )
        )
        await db.commit()
        
        # Then create new references from frontmatter
        await self._resolve_frontmatter_references(db, existing, resource)
        
        # === Build System Integration ===
        # Create new version for the updated instruction
        if build:
            try:
                # Re-fetch instruction with relationships for version creation
                inst_stmt = (
                    select(Instruction)
                    .options(
                        selectinload(Instruction.data_sources),
                        selectinload(Instruction.labels),
                        selectinload(Instruction.references),
                    )
                    .where(Instruction.id == existing.id)
                )
                inst_result = await db.execute(inst_stmt)
                instruction_with_rels = inst_result.scalar_one()
                
                # Create new version
                version = await self.version_service.create_version(
                    db, instruction_with_rels, user_id=existing.user_id
                )
                
                # Update instruction's current version
                instruction_with_rels.current_version_id = version.id
                
                # Add the version to the build
                await self.build_service.add_to_build(
                    db, build.id, instruction_with_rels.id, version.id
                )
                
                await db.commit()
                logger.info(f"Created version {version.id} for updated git instruction {existing.id}, added to build {build.id}")
            except Exception as e:
                logger.warning(f"Failed to create version for updated git instruction {existing.id}: {e}")
        
        logger.info(f"Updated instruction {existing.id} from resource {resource.id}")
        return existing
    
    async def _create_pending_version(
        self,
        db: AsyncSession,
        published: Instruction,
        resource: MetadataResource,
        organization: Organization,
        commit_sha: Optional[str] = None,
    ) -> Instruction:
        """Create a pending version of a published instruction."""
        formatted_text = self._format_resource_as_text(resource)
        structured_data = self._build_structured_data(resource)
        load_mode = self._get_load_mode_for_resource(resource, None)
        
        new_version = Instruction(
            text=formatted_text,
            title=resource.name,
            source_type='git',
            source_metadata_resource_id=resource.id,
            source_git_commit_sha=commit_sha,
            source_sync_enabled=True,
            source_instruction_id=published.id,  # Link to parent
            load_mode=published.load_mode,  # Inherit load mode from parent
            status='draft',
            private_status=None,
            global_status='suggested',  # Mark as suggested for review
            category=published.category,
            structured_data=structured_data,
            formatted_content=formatted_text,
            organization_id=organization.id,
            user_id=published.user_id,  # Inherit user from parent instruction
            is_seen=True,
            can_user_toggle=True,
        )
        
        db.add(new_version)
        await db.commit()
        await db.refresh(new_version)
        
        logger.info(f"Created pending version {new_version.id} for published instruction {published.id}")
        return new_version
    
    async def archive_instruction_for_deleted_resource(
        self,
        db: AsyncSession,
        resource_id: str,
    ) -> Optional[Instruction]:
        """Archive an instruction when its source resource is deleted."""
        instruction = await self._find_instruction_for_resource(db, resource_id)
        
        if not instruction:
            return None
        
        if not instruction.source_sync_enabled:
            # Unlinked, don't archive
            return None
        
        instruction.status = 'archived'
        instruction.formatted_content = (
            f"{instruction.formatted_content or instruction.text}\n\n"
            "---\n"
            "_Note: Source file was removed from the git repository._"
        )
        
        await db.commit()
        await db.refresh(instruction)
        
        logger.info(f"Archived instruction {instruction.id} - source resource {resource_id} was deleted")
        return instruction
    
    def _get_load_mode_for_resource(
        self,
        resource: MetadataResource,
        git_repo: Optional[GitRepository] = None,
    ) -> str:
        """Determine the load mode for a resource.
        
        Priority order:
        1. Frontmatter load_mode field (direct value: 'always', 'intelligent', 'never')
        2. Frontmatter alwaysApply field (legacy alias)
           - alwaysApply: true → 'always'
           - alwaysApply: false → 'intelligent'
        3. Git repository default_load_mode (if set)
           - 'auto' mode: markdown → 'always', others → 'intelligent'
           - Other modes are applied directly
        4. Type-specific default from DEFAULT_LOAD_MODES
        """
        # Check frontmatter for load_mode (highest priority)
        load_mode = self._get_frontmatter_value(resource, 'load_mode')
        if load_mode in ('always', 'intelligent', 'never'):
            return load_mode
        
        # Check frontmatter for alwaysApply (legacy alias)
        always_apply = self._get_frontmatter_value(resource, 'alwaysApply')
        if always_apply is True:
            return 'always'
        elif always_apply is False:
            return 'intelligent'
        
        # Git repository setting takes priority if explicitly configured
        if git_repo and git_repo.default_load_mode:
            if git_repo.default_load_mode == 'auto':
                # Auto mode: markdown files always load, others use intelligent
                if resource.resource_type == 'markdown_document':
                    return 'always'
                return 'intelligent'
            return git_repo.default_load_mode

        # Use type-specific default (this is more intentional than the model default)
        return self.DEFAULT_LOAD_MODES.get(resource.resource_type, 'intelligent')
    
    def _get_frontmatter_value(self, resource: MetadataResource, key: str, default=None):
        """
        Extract a value from resource frontmatter, handling various nesting structures.
        
        The frontmatter can be stored in different locations depending on how the
        resource was created:
        - resource.raw_data['always_apply'] (top-level from markdown_parser)
        - resource.raw_data['frontmatter'][key] (frontmatter dict)
        - resource.raw_data['raw_data']['frontmatter'][key] (nested when entire item stored)
        """
        if not resource.raw_data or not isinstance(resource.raw_data, dict):
            return default
        
        # Map frontmatter keys to top-level convenience keys
        key_mapping = {
            'alwaysApply': 'always_apply',
            'references': 'references',
        }
        
        # Try top-level convenience key first (set by markdown_parser)
        top_level_key = key_mapping.get(key, key)
        if top_level_key in resource.raw_data:
            return resource.raw_data[top_level_key]
        
        # Try frontmatter dict at top level
        frontmatter = resource.raw_data.get('frontmatter', {})
        if isinstance(frontmatter, dict) and key in frontmatter:
            return frontmatter[key]
        
        # Try nested raw_data.frontmatter (when entire item dict is stored)
        nested_raw_data = resource.raw_data.get('raw_data', {})
        if isinstance(nested_raw_data, dict):
            nested_frontmatter = nested_raw_data.get('frontmatter', {})
            if isinstance(nested_frontmatter, dict) and key in nested_frontmatter:
                return nested_frontmatter[key]
        
        return default
    
    async def _resolve_frontmatter_references(
        self,
        db: AsyncSession,
        instruction: Instruction,
        resource: MetadataResource,
    ) -> int:
        """
        Resolve frontmatter references and create InstructionReference entries.
        
        Looks up table names in DataSourceTable, scoped to the instruction's data sources.
        Non-matching references are silently skipped.
        
        Args:
            db: Database session
            instruction: The instruction to add references to
            resource: The source metadata resource with frontmatter
            
        Returns:
            Number of references created
        """
        # Get references from frontmatter using helper that handles various nesting structures
        references = self._get_frontmatter_value(resource, 'references', default=[])
        
        if not references or not isinstance(references, list):
            logger.debug(f"No references in frontmatter for resource {resource.id}")
            return 0
        
        logger.info(f"Found {len(references)} references in frontmatter: {references}")
        
        # Refresh instruction to ensure data_sources relationship is loaded
        await db.refresh(instruction, ['data_sources'])
        
        # Get the instruction's data source IDs for scoping
        data_source_ids = [ds.id for ds in instruction.data_sources] if instruction.data_sources else []
        logger.debug(f"Instruction {instruction.id} has {len(data_source_ids)} data sources")
        
        created_count = 0
        for ref_name in references:
            if not isinstance(ref_name, str) or not ref_name.strip():
                continue
            
            ref_name = ref_name.strip()
            
            # Build query to find matching table
            table_query = select(DataSourceTable).where(
                and_(
                    DataSourceTable.name == ref_name,
                    DataSourceTable.is_active == True,
                )
            )
            
            # Scope to instruction's data sources if any
            if data_source_ids:
                table_query = table_query.where(
                    DataSourceTable.datasource_id.in_(data_source_ids)
                )
            
            result = await db.execute(table_query)
            table = result.scalars().first()
            
            if table:
                # Check if reference already exists
                existing_ref = await db.execute(
                    select(InstructionReference).where(
                        and_(
                            InstructionReference.instruction_id == instruction.id,
                            InstructionReference.object_type == 'datasource_table',
                            InstructionReference.object_id == table.id,
                        )
                    )
                )
                if not existing_ref.scalars().first():
                    # Create the reference
                    instruction_ref = InstructionReference(
                        instruction_id=instruction.id,
                        object_type='datasource_table',
                        object_id=table.id,
                    )
                    db.add(instruction_ref)
                    created_count += 1
                    logger.info(f"Created reference from instruction {instruction.id} to table '{table.name}' (id={table.id})")
            else:
                logger.warning(f"Reference '{ref_name}' not found in DataSourceTable (scoped to {len(data_source_ids)} data sources)")
        
        if created_count > 0:
            await db.commit()
            logger.info(f"Created {created_count} references for instruction {instruction.id}")
        
        return created_count
    
    def _build_structured_data(self, resource: MetadataResource) -> Dict[str, Any]:
        """Build structured data dictionary for storage."""
        return {
            'resource_type': resource.resource_type,
            'path': resource.path,
            'name': resource.name,
            'description': resource.description,
            'columns': resource.columns,
            'depends_on': resource.depends_on,
            'sql_content': resource.sql_content,
            'source_name': resource.source_name,
            'database': resource.database,
            'schema': resource.schema,
            'raw_data': resource.raw_data,
            'data_source_id': resource.data_source_id,
        }
    
    def _format_resource_as_text(self, resource: MetadataResource) -> str:
        """
        Return raw file content for a metadata resource.
        
        The instruction text should be the gold truth - the exact file content
        without any formatting, headers, or metadata appended.
        """
        # Priority 1: Markdown content (stored in raw_data.content)
        if resource.raw_data and isinstance(resource.raw_data, dict):
            if resource.raw_data.get('content'):
                return resource.raw_data['content']
            # LookML stores raw file content in file_content
            if resource.raw_data.get('file_content'):
                return resource.raw_data['file_content']
        
        # Priority 2: SQL content (for dbt models, macros, dataform, etc.)
        if resource.sql_content:
            return resource.sql_content
        
        # Priority 3: SQLX source snippet (Dataform stores full file here)
        if resource.raw_data and isinstance(resource.raw_data, dict):
            if resource.raw_data.get('sqlx_source_snippet'):
                return resource.raw_data['sqlx_source_snippet']
        
        # Priority 4: Description (for YAML-defined resources like metrics)
        if resource.description:
            return resource.description
        
        # Fallback: empty content indicator
        return f"# {resource.name}\n\n_No content available_"
    
    def _format_dbt_model(self, resource: MetadataResource) -> str:
        """Format a dbt model as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** dbt model")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        # Add columns
        if resource.columns:
            parts.append("## Columns")
            parts.append("")
            for col in resource.columns:
                if isinstance(col, dict):
                    col_line = f"- **{col.get('name', 'unknown')}**"
                    if col.get('data_type'):
                        col_line += f" ({col['data_type']})"
                    if col.get('description'):
                        col_line += f": {col['description']}"
                    parts.append(col_line)
            parts.append("")
        
        # Add dependencies
        if resource.depends_on:
            parts.append(f"**Depends on:** {', '.join(resource.depends_on)}")
            parts.append("")
        
        # Add SQL (truncated if too long)
        if resource.sql_content:
            sql = resource.sql_content
            if len(sql) > 2000:
                sql = sql[:2000] + "\n-- [truncated]"
            parts.append("## SQL")
            parts.append("")
            parts.append("```sql")
            parts.append(sql)
            parts.append("```")
        
        return "\n".join(parts)
    
    def _format_dbt_metric(self, resource: MetadataResource) -> str:
        """Format a dbt metric as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** dbt metric")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        # Extract metric-specific info from raw_data
        if resource.raw_data:
            if resource.raw_data.get('calculation_method'):
                parts.append(f"**Calculation:** {resource.raw_data['calculation_method']}")
            if resource.raw_data.get('expression'):
                parts.append(f"**Expression:** `{resource.raw_data['expression']}`")
            if resource.raw_data.get('timestamp'):
                parts.append(f"**Timestamp:** {resource.raw_data['timestamp']}")
            if resource.raw_data.get('time_grains'):
                parts.append(f"**Time grains:** {', '.join(resource.raw_data['time_grains'])}")
        
        return "\n".join(parts)
    
    def _format_dbt_source(self, resource: MetadataResource) -> str:
        """Format a dbt source as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** dbt source")
        
        if resource.source_name:
            parts.append(f"**Source:** {resource.source_name}")
        if resource.database:
            parts.append(f"**Database:** {resource.database}")
        if resource.schema:
            parts.append(f"**Schema:** {resource.schema}")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        # Add columns
        if resource.columns:
            parts.append("## Columns")
            parts.append("")
            for col in resource.columns:
                if isinstance(col, dict):
                    col_line = f"- **{col.get('name', 'unknown')}**"
                    if col.get('data_type'):
                        col_line += f" ({col['data_type']})"
                    if col.get('description'):
                        col_line += f": {col['description']}"
                    parts.append(col_line)
        
        return "\n".join(parts)
    
    def _format_dbt_seed(self, resource: MetadataResource) -> str:
        """Format a dbt seed as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** dbt seed (static data)")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        if resource.columns:
            parts.append("## Columns")
            parts.append("")
            for col in resource.columns:
                if isinstance(col, dict):
                    parts.append(f"- **{col.get('name', 'unknown')}**")
        
        return "\n".join(parts)
    
    def _format_dbt_macro(self, resource: MetadataResource) -> str:
        """Format a dbt macro as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** dbt macro")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        if resource.sql_content:
            parts.append("```sql")
            parts.append(resource.sql_content[:1000] if len(resource.sql_content) > 1000 else resource.sql_content)
            parts.append("```")
        
        return "\n".join(parts)
    
    def _format_dbt_test(self, resource: MetadataResource) -> str:
        """Format a dbt test as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** dbt test")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
        
        return "\n".join(parts)
    
    def _format_dbt_exposure(self, resource: MetadataResource) -> str:
        """Format a dbt exposure as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** dbt exposure")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        if resource.raw_data:
            if resource.raw_data.get('type'):
                parts.append(f"**Exposure type:** {resource.raw_data['type']}")
            if resource.raw_data.get('owner'):
                owner = resource.raw_data['owner']
                if isinstance(owner, dict):
                    parts.append(f"**Owner:** {owner.get('name', '')} ({owner.get('email', '')})")
        
        if resource.depends_on:
            parts.append(f"**Depends on:** {', '.join(resource.depends_on)}")
        
        return "\n".join(parts)
    
    def _format_markdown(self, resource: MetadataResource) -> str:
        """Format a markdown document - just return the content."""
        # For markdown, the raw content is already readable
        if resource.raw_data and resource.raw_data.get('content'):
            return resource.raw_data['content']
        if resource.description:
            return resource.description
        return f"# {resource.name}\n\n_No content available_"
    
    def _format_lookml_view(self, resource: MetadataResource) -> str:
        """Format a LookML view as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** LookML view")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        # Add dimensions/measures from columns
        if resource.columns:
            dimensions = [c for c in resource.columns if isinstance(c, dict) and c.get('type') == 'dimension']
            measures = [c for c in resource.columns if isinstance(c, dict) and c.get('type') == 'measure']
            
            if dimensions:
                parts.append("## Dimensions")
                for dim in dimensions:
                    parts.append(f"- **{dim.get('name')}**: {dim.get('description', '')}")
                parts.append("")
            
            if measures:
                parts.append("## Measures")
                for measure in measures:
                    parts.append(f"- **{measure.get('name')}**: {measure.get('description', '')}")
        
        return "\n".join(parts)
    
    def _format_lookml_model(self, resource: MetadataResource) -> str:
        """Format a LookML model as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** LookML model")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
        
        return "\n".join(parts)
    
    def _format_lookml_explore(self, resource: MetadataResource) -> str:
        """Format a LookML explore as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** LookML explore")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        if resource.depends_on:
            parts.append(f"**Views used:** {', '.join(resource.depends_on)}")
        
        return "\n".join(parts)
    
    def _format_tableau_datasource(self, resource: MetadataResource) -> str:
        """Format a Tableau datasource as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** Tableau datasource")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        if resource.columns:
            parts.append("## Fields")
            for col in resource.columns:
                if isinstance(col, dict):
                    parts.append(f"- **{col.get('name', 'unknown')}**: {col.get('description', '')}")
        
        return "\n".join(parts)
    
    def _format_dataform_table(self, resource: MetadataResource) -> str:
        """Format a Dataform table as readable text."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** Dataform table")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        if resource.columns:
            parts.append("## Columns")
            for col in resource.columns:
                if isinstance(col, dict):
                    col_line = f"- **{col.get('name', 'unknown')}**"
                    if col.get('description'):
                        col_line += f": {col['description']}"
                    parts.append(col_line)
            parts.append("")
        
        if resource.depends_on:
            parts.append(f"**Depends on:** {', '.join(resource.depends_on)}")
            parts.append("")
        
        if resource.sql_content:
            sql = resource.sql_content
            if len(sql) > 2000:
                sql = sql[:2000] + "\n-- [truncated]"
            parts.append("## SQL")
            parts.append("")
            parts.append("```sql")
            parts.append(sql)
            parts.append("```")
        
        return "\n".join(parts)
    
    def _format_generic(self, resource: MetadataResource) -> str:
        """Generic formatter for unknown resource types."""
        parts = [f"# {resource.name}", ""]
        parts.append(f"**Type:** {resource.resource_type}")
        
        if resource.path:
            parts.append(f"**Path:** `{resource.path}`")
        
        parts.append("")
        
        if resource.description:
            parts.append(resource.description)
            parts.append("")
        
        if resource.columns:
            parts.append("## Fields")
            for col in resource.columns:
                if isinstance(col, dict):
                    parts.append(f"- {col.get('name', 'unknown')}")
        
        return "\n".join(parts)

    # ========================================
    # NEW: Instruction-first flow (1 file = 1 instruction)
    # ========================================
    
    async def sync_file_to_instruction(
        self,
        db: AsyncSession,
        file_path: str,
        file_content: str,
        content_hash: str,
        organization: Organization,
        git_repo: GitRepository,
        resource_type: str = 'generic_file',
        build: Optional[InstructionBuild] = None,
        data_source: Optional[DataSource] = None,
    ) -> Optional[Instruction]:
        """
        Sync a single file to its instruction (1 file = 1 instruction).

        Follows the 5-rule reindex logic:
        1. New file -> Create instruction
        2. User-created -> Never touch
        3. Unlinked -> Skip
        4. Linked -> Update text field directly
        5. Deleted files -> Archive (handled separately)
        """
        from pathlib import Path as PurePath

        existing = await self._find_instruction_by_file_path(db, file_path, organization.id)

        # Rule 1: New file -> Create
        if existing is None:
            return await self._create_file_instruction(
                db, file_path, file_content, content_hash, organization,
                git_repo, resource_type=resource_type, build=build,
                data_source=data_source,
            )

        # Rule 2: User-created -> Never touch
        if existing.source_type != 'git':
            logger.debug(f"Skipping user-created instruction {existing.id}")
            return None

        # Rule 3: Unlinked -> Skip
        if not existing.source_sync_enabled:
            logger.debug(f"Skipping unlinked instruction {existing.id}")
            return None

        # Rule 4: Linked -> Update text field directly
        # Check if content actually changed (using hash)
        if existing.content_hash == content_hash:
            existing.source_git_commit_sha = git_repo.last_indexed_commit_sha
            await db.commit()
            return existing

        # Content changed - update
        ext = PurePath(file_path).suffix.lower()
        existing.text = file_content
        existing.formatted_content = file_content
        existing.content_hash = content_hash
        existing.source_git_commit_sha = git_repo.last_indexed_commit_sha
        existing.updated_at = datetime.utcnow()
        existing.structured_data = {
            'resource_type': resource_type,
            'path': file_path,
            'extension': ext,
        }

        # Update frontmatter-driven fields for markdown
        if ext in ('.md', '.markdown'):
            frontmatter = self._parse_frontmatter_from_content(file_content)
            fm_status = frontmatter.get('status')
            if fm_status in ('published', 'draft', 'archived'):
                existing.status = fm_status
                existing.global_status = 'approved' if fm_status == 'published' else None
            fm_category = frontmatter.get('category')
            if fm_category:
                existing.category = fm_category
            fm_load_mode = frontmatter.get('load_mode')
            if fm_load_mode in ('always', 'intelligent', 'never'):
                existing.load_mode = fm_load_mode
            else:
                always_apply = frontmatter.get('alwaysApply')
                if always_apply is True:
                    existing.load_mode = 'always'
                elif always_apply is False:
                    existing.load_mode = 'intelligent'

        await db.commit()
        await db.refresh(existing)

        # Re-extract table references
        from sqlalchemy import delete as sa_delete
        await db.execute(
            sa_delete(InstructionReference).where(
                InstructionReference.instruction_id == existing.id
            )
        )
        await db.commit()
        await self._extract_table_references(db, existing, organization.id)

        # Build integration: create version + add to build
        if build:
            try:
                inst_stmt = (
                    select(Instruction)
                    .options(
                        selectinload(Instruction.data_sources),
                        selectinload(Instruction.labels),
                        selectinload(Instruction.references),
                    )
                    .where(Instruction.id == existing.id)
                )
                inst_result = await db.execute(inst_stmt)
                instruction_with_rels = inst_result.scalar_one()

                version = await self.version_service.create_version(
                    db, instruction_with_rels, user_id=existing.user_id
                )
                instruction_with_rels.current_version_id = version.id
                await self.build_service.add_to_build(
                    db, build.id, instruction_with_rels.id, version.id
                )
                await db.commit()
                logger.debug(f"Created version {version.id} for updated file instruction {existing.id}")
            except Exception as e:
                logger.warning(f"Failed to create version for file instruction {existing.id}: {e}")

        logger.info(f"Updated instruction {existing.id} from file {file_path}")
        return existing
    
    async def _find_instruction_by_file_path(
        self,
        db: AsyncSession,
        file_path: str,
        org_id: str,
    ) -> Optional[Instruction]:
        """Find instruction by source_file_path."""
        stmt = select(Instruction).where(
            and_(
                Instruction.source_file_path == file_path,
                Instruction.organization_id == org_id,
                Instruction.deleted_at == None,
            )
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
    async def _create_file_instruction(
        self,
        db: AsyncSession,
        file_path: str,
        file_content: str,
        content_hash: str,
        organization: Organization,
        git_repo: GitRepository,
        resource_type: str = 'generic_file',
        build: Optional[InstructionBuild] = None,
        data_source: Optional[DataSource] = None,
    ) -> Instruction:
        """Create new instruction for a git file."""
        from pathlib import Path

        # Extract title from file path
        title = Path(file_path).stem
        ext = Path(file_path).suffix.lower()

        # Parse frontmatter from file content (for .md files)
        frontmatter = self._parse_frontmatter_from_content(file_content)

        # Determine load mode: frontmatter > file extension > repo default
        load_mode = frontmatter.get('load_mode')
        if load_mode not in ('always', 'intelligent', 'never'):
            always_apply = frontmatter.get('alwaysApply')
            if always_apply is True:
                load_mode = 'always'
            elif always_apply is False:
                load_mode = 'intelligent'
            else:
                load_mode = self._get_load_mode_for_file(file_path, git_repo)

        # Parse status from frontmatter; default to 'draft' (auto_publish upgrades below)
        status = frontmatter.get('status')
        if status not in ('published', 'draft', 'archived'):
            status = 'draft'

        # Parse category from frontmatter (default: 'general')
        category = frontmatter.get('category') or 'general'

        # Auto-publish: override status to 'published' when enabled
        if git_repo.auto_publish:
            status = 'published'

        # Derive global_status based on final status
        global_status = 'approved' if status == 'published' else None

        instruction = Instruction(
            text=file_content,
            formatted_content=file_content,
            title=title,
            source_type='git',
            source_file_path=file_path,
            content_hash=content_hash,
            source_sync_enabled=True,
            source_git_commit_sha=git_repo.last_indexed_commit_sha,
            load_mode=load_mode,
            status=status,
            private_status=None,
            global_status=global_status,
            category=category,
            organization_id=organization.id,
            user_id=git_repo.user_id,
            is_seen=True,
            can_user_toggle=True,
            structured_data={
                'resource_type': resource_type,
                'path': file_path,
                'extension': ext,
            },
        )

        db.add(instruction)
        await db.commit()
        await db.refresh(instruction)

        # Associate instruction with the data source
        if data_source:
            instruction.data_sources.append(data_source)
            await db.commit()

        # Resolve frontmatter references for markdown files
        if ext in ('.md', '.markdown') and frontmatter:
            try:
                await self._resolve_frontmatter_references_from_dict(
                    db, instruction, frontmatter, organization.id
                )
            except Exception as e:
                logger.warning(f"Failed to resolve frontmatter references for {file_path}: {e}")

        # Extract table references for all files
        await self._extract_table_references(db, instruction, organization.id)

        # Build integration: create version + add to build
        if build:
            try:
                inst_stmt = (
                    select(Instruction)
                    .options(
                        selectinload(Instruction.data_sources),
                        selectinload(Instruction.labels),
                        selectinload(Instruction.references),
                    )
                    .where(Instruction.id == instruction.id)
                )
                inst_result = await db.execute(inst_stmt)
                instruction_with_rels = inst_result.scalar_one()

                version = await self.version_service.create_version(
                    db, instruction_with_rels, user_id=git_repo.user_id
                )
                instruction_with_rels.current_version_id = version.id
                await self.build_service.add_to_build(
                    db, build.id, instruction_with_rels.id, version.id
                )
                await db.commit()
                logger.debug(f"Created version {version.id} for new file instruction {instruction.id}")
            except Exception as e:
                logger.warning(f"Failed to create version for file instruction {instruction.id}: {e}")

        logger.info(f"Created file instruction {instruction.id} for {file_path}")
        return instruction
    
    def _get_load_mode_for_file(self, file_path: str, git_repo: GitRepository) -> str:
        """Determine load mode based on file extension and repo settings."""
        from pathlib import Path
        
        ext = Path(file_path).suffix.lower()
        
        # Git repository setting takes priority if explicitly configured
        if git_repo.default_load_mode:
            if git_repo.default_load_mode == 'auto':
                # Auto mode: markdown files always load, others use intelligent
                if ext in ['.md', '.markdown']:
                    return 'always'
                return 'intelligent'
            return git_repo.default_load_mode
        
        # Default by extension
        if ext in ['.md', '.markdown']:
            return 'always'
        return 'intelligent'
    
    def _parse_frontmatter_from_content(self, content: str) -> Dict[str, Any]:
        """
        Parse YAML frontmatter from file content.
        
        Handles content in the format:
        ---
        key: value
        ---
        ... body content ...
        
        Returns empty dict if no frontmatter found.
        """
        import yaml
        
        if not content or not content.strip().startswith('---'):
            return {}
        
        try:
            # Find the closing ---
            lines = content.split('\n')
            if len(lines) < 2:
                return {}
            
            # Skip the opening ---
            end_idx = None
            for i, line in enumerate(lines[1:], start=1):
                if line.strip() == '---':
                    end_idx = i
                    break
            
            if end_idx is None:
                return {}
            
            # Extract and parse frontmatter
            frontmatter_text = '\n'.join(lines[1:end_idx])
            frontmatter = yaml.safe_load(frontmatter_text)
            
            return frontmatter if isinstance(frontmatter, dict) else {}
        except Exception as e:
            logger.warning(f"Failed to parse frontmatter: {e}")
            return {}
    
    async def rebuild_chunks(
        self,
        db: AsyncSession,
        instruction: Instruction,
        chunks: List[Dict[str, Any]],
        data_source_id: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> List[MetadataResource]:
        """
        Delete old chunks and create new ones for an instruction.
        
        Args:
            db: Database session
            instruction: The parent instruction
            chunks: List of chunk dicts with keys like 'name', 'resource_type', 
                    'start_line', 'end_line', 'description', 'raw_data', etc.
            data_source_id: Data source to associate chunks with
            job_id: Indexing job ID to associate chunks with
        """
        from sqlalchemy import delete
        
        # Batch delete old chunks
        await db.execute(
            delete(MetadataResource).where(
                MetadataResource.instruction_id == instruction.id
            )
        )
        
        # Batch insert new chunks
        new_resources = []
        for chunk in chunks:
            resource = MetadataResource(
                instruction_id=instruction.id,
                name=chunk.get('name', ''),
                resource_type=chunk.get('resource_type', 'unknown'),
                path=instruction.source_file_path,
                description=chunk.get('description', ''),
                raw_data=chunk.get('raw_data', {}),
                sql_content=chunk.get('sql_content'),
                columns=chunk.get('columns', []),
                depends_on=chunk.get('depends_on', []),
                chunk_start_line=chunk.get('start_line'),
                chunk_end_line=chunk.get('end_line'),
                data_source_id=data_source_id,
                metadata_indexing_job_id=job_id,
                is_active=True,
            )
            new_resources.append(resource)
        
        if new_resources:
            db.add_all(new_resources)
            await db.commit()
        
        logger.debug(f"Rebuilt {len(new_resources)} chunks for instruction {instruction.id}")
        return new_resources
    
    async def archive_deleted_files(
        self,
        db: AsyncSession,
        org_id: str,
        current_file_paths: set,
        path_prefix: Optional[str] = None,
        build: Optional[InstructionBuild] = None,
    ) -> int:
        """
        Archive instructions for files that no longer exist in git.

        Rule 5: Deleted files -> Archive

        Args:
            path_prefix: If provided, only consider instructions whose
                         source_file_path starts with this prefix (e.g. 'my-repo/').
                         This scopes the archival to a single repo.
            build: If provided, add archived instructions to the build.

        Returns:
            Number of instructions archived
        """
        conditions = [
            Instruction.source_type == 'git',
            Instruction.source_sync_enabled == True,
            Instruction.organization_id == org_id,
            Instruction.source_file_path != None,
            Instruction.deleted_at == None,
            Instruction.status != 'archived',
        ]
        if path_prefix:
            conditions.append(Instruction.source_file_path.like(f'{path_prefix}%'))

        stmt = select(Instruction).where(and_(*conditions))
        result = await db.execute(stmt)
        instructions = result.scalars().all()

        archived_count = 0
        for instruction in instructions:
            if instruction.source_file_path not in current_file_paths:
                instruction.status = 'archived'
                archived_count += 1
                logger.info(f"Archived instruction {instruction.id} - file {instruction.source_file_path} was deleted")

                if build:
                    try:
                        version = await self.version_service.create_version(
                            db, instruction, user_id=instruction.user_id
                        )
                        instruction.current_version_id = version.id
                        await self.build_service.add_to_build(
                            db, build.id, instruction.id, version.id
                        )
                    except Exception as e:
                        logger.warning(f"Failed to add archived instruction {instruction.id} to build: {e}")

        if archived_count > 0:
            await db.commit()

        return archived_count

    async def _resolve_frontmatter_references_from_dict(
        self,
        db: AsyncSession,
        instruction: Instruction,
        frontmatter: Dict[str, Any],
        org_id: str,
    ) -> int:
        """Resolve frontmatter references from a dict (new file-based flow)."""
        references = frontmatter.get('references', [])
        if not references or not isinstance(references, list):
            return 0

        await db.refresh(instruction, ['data_sources'])
        data_source_ids = [ds.id for ds in instruction.data_sources] if instruction.data_sources else []

        created_count = 0
        for ref_name in references:
            if not isinstance(ref_name, str) or not ref_name.strip():
                continue

            ref_name = ref_name.strip()
            table_query = select(DataSourceTable).where(
                and_(
                    DataSourceTable.name == ref_name,
                    DataSourceTable.is_active == True,
                )
            )
            if data_source_ids:
                table_query = table_query.where(
                    DataSourceTable.datasource_id.in_(data_source_ids)
                )

            result = await db.execute(table_query)
            table = result.scalars().first()

            if table:
                existing_ref = await db.execute(
                    select(InstructionReference).where(
                        and_(
                            InstructionReference.instruction_id == instruction.id,
                            InstructionReference.object_type == 'datasource_table',
                            InstructionReference.object_id == table.id,
                        )
                    )
                )
                if not existing_ref.scalars().first():
                    instruction_ref = InstructionReference(
                        instruction_id=instruction.id,
                        object_type='datasource_table',
                        object_id=table.id,
                    )
                    db.add(instruction_ref)
                    created_count += 1

        if created_count > 0:
            await db.commit()
        return created_count

    async def _extract_table_references(
        self,
        db: AsyncSession,
        instruction: Instruction,
        org_id: str,
    ) -> int:
        """
        Auto-extract table references by scanning instruction text for known
        DataSourceTable names (case-sensitive, word-boundary match).

        Works for any file format: dbt SQL, dbt YAML, Snowflake semantic YAML,
        LookML, raw SQL, etc.
        """
        import re as _re

        text = instruction.formatted_content or instruction.text
        if not text:
            return 0

        # Get instruction's data source ids for scoping
        await db.refresh(instruction, ['data_sources'])
        data_source_ids = [ds.id for ds in instruction.data_sources] if instruction.data_sources else []

        # If no data sources on instruction, look at all org data sources
        if not data_source_ids:
            from app.models.data_source import DataSource as DS
            ds_stmt = select(DS.id).where(
                and_(DS.organization_id == org_id, DS.deleted_at == None)
            )
            ds_result = await db.execute(ds_stmt)
            data_source_ids = [row[0] for row in ds_result.all()]

        if not data_source_ids:
            return 0

        # Load all active table names for these data sources
        table_stmt = select(DataSourceTable).where(
            and_(
                DataSourceTable.datasource_id.in_(data_source_ids),
                DataSourceTable.is_active == True,
            )
        )
        table_result = await db.execute(table_stmt)
        tables = table_result.scalars().all()

        created_count = 0
        for table in tables:
            name = table.name
            # Filter names < 3 chars to avoid false positives
            if not name or len(name) < 3:
                continue

            pattern = r'\b' + _re.escape(name) + r'\b'
            if _re.search(pattern, text):
                # Check if reference already exists
                existing_ref = await db.execute(
                    select(InstructionReference).where(
                        and_(
                            InstructionReference.instruction_id == instruction.id,
                            InstructionReference.object_type == 'datasource_table',
                            InstructionReference.object_id == table.id,
                        )
                    )
                )
                if not existing_ref.scalars().first():
                    instruction_ref = InstructionReference(
                        instruction_id=instruction.id,
                        object_type='datasource_table',
                        object_id=table.id,
                    )
                    db.add(instruction_ref)
                    created_count += 1

        if created_count > 0:
            await db.commit()
            logger.debug(f"Extracted {created_count} table references for instruction {instruction.id}")

        return created_count
