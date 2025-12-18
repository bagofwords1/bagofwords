"""
InstructionSyncService - Syncs MetadataResources to Instructions

This service handles the conversion of git-indexed MetadataResources into
Instructions, including:
- Creating new instructions from resources
- Updating existing instructions when resources change
- Creating pending versions for published instructions
- Archiving instructions when resources are deleted
- Formatting structured data into readable text
"""

import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_

from app.models.instruction import Instruction
from app.models.metadata_resource import MetadataResource
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.data_source import DataSource
from app.models.git_repository import GitRepository
from app.models.metadata_indexing_job import MetadataIndexingJob
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
        pass  # No external service dependencies
    
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
    ) -> Optional[Instruction]:
        """
        Create or update an instruction from a metadata resource.
        
        Args:
            db: Database session
            resource: The metadata resource to sync
            organization: The organization
            commit_sha: Optional git commit SHA
            
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
            return await self._handle_existing_instruction(db, existing, fresh_resource, organization, commit_sha)
        else:
            return await self._create_instruction_from_resource(db, fresh_resource, organization, commit_sha)
    
    async def _find_instruction_for_resource(
        self,
        db: AsyncSession,
        resource_id: str
    ) -> Optional[Instruction]:
        """Find an existing instruction linked to a metadata resource."""
        stmt = select(Instruction).where(
            and_(
                Instruction.source_metadata_resource_id == resource_id,
                Instruction.deleted_at == None
            )
        )
        result = await db.execute(stmt)
        return result.scalar_one_or_none()
    
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
        
        # Determine load mode from git repository settings
        load_mode = self._get_load_mode_for_resource(resource, git_repo)
        
        # Build structured data for storage
        structured_data = self._build_structured_data(resource)
        
        # Get data source info for context
        data_source_result = await db.execute(
            select(DataSource).where(DataSource.id == resource.data_source_id)
        )
        data_source = data_source_result.scalar_one_or_none()
        
        instruction = Instruction(
            text=formatted_text,
            title=resource.name,
            source_type='git',
            source_metadata_resource_id=resource.id,
            source_git_commit_sha=commit_sha,
            source_sync_enabled=True,
            load_mode=load_mode,
            status='published' if auto_publish else 'draft',
            private_status=None,
            global_status='approved' if auto_publish else None,
            category='general',
            structured_data=structured_data,
            formatted_content=formatted_text,
            organization_id=organization.id,
            # Don't assign user_id - this is system-created
            user_id=None,
            is_seen=True,
            can_user_toggle=True,
        )
        
        db.add(instruction)
        await db.commit()
        await db.refresh(instruction)
        
        # Associate instruction with the data source
        if data_source:
            instruction.data_sources.append(data_source)
            await db.commit()
        
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
        
        logger.info(f"Created instruction {instruction.id} from resource {resource.id} ({resource.resource_type})")
        return instruction
    
    async def _handle_existing_instruction(
        self,
        db: AsyncSession,
        existing: Instruction,
        resource: MetadataResource,
        organization: Organization,
        commit_sha: Optional[str] = None,
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
        
        # Content changed - handle based on status
        if existing.status == 'draft':
            # Safe to auto-update drafts
            existing.text = new_text
            existing.title = resource.name
            existing.structured_data = new_structured_data
            existing.formatted_content = new_text
            existing.source_git_commit_sha = commit_sha
            await db.commit()
            await db.refresh(existing)
            logger.info(f"Updated draft instruction {existing.id} from resource {resource.id}")
            return existing
        
        if existing.status == 'published':
            # Create new version for review
            return await self._create_pending_version(db, existing, resource, organization, commit_sha)
        
        # For archived instructions, don't update
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
            user_id=None,
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
        """Determine the load mode for a resource."""
        # Check if resource has a specific load_mode set
        if resource.load_mode:
            return resource.load_mode

        # Check git repository default load mode
        if git_repo and git_repo.default_load_mode:
            return git_repo.default_load_mode

        # Use type-specific default
        return self.DEFAULT_LOAD_MODES.get(resource.resource_type, 'intelligent')
    
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
        """Convert a metadata resource to readable text format."""
        formatters = {
            'dbt_model': self._format_dbt_model,
            'dbt_metric': self._format_dbt_metric,
            'dbt_source': self._format_dbt_source,
            'dbt_seed': self._format_dbt_seed,
            'dbt_macro': self._format_dbt_macro,
            'dbt_test': self._format_dbt_test,
            'dbt_exposure': self._format_dbt_exposure,
            'markdown_document': self._format_markdown,
            'lookml_view': self._format_lookml_view,
            'lookml_model': self._format_lookml_model,
            'lookml_explore': self._format_lookml_explore,
            'tableau_datasource': self._format_tableau_datasource,
            'dataform_table': self._format_dataform_table,
        }
        
        formatter = formatters.get(resource.resource_type, self._format_generic)
        return formatter(resource)
    
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
