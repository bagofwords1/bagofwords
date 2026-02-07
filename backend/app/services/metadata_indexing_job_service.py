import logging
from collections import defaultdict
from fastapi import HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func, delete, or_
from datetime import datetime
from typing import Optional, Dict, List, Any
import tempfile
import asyncio
import shutil
from pathlib import Path

from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.metadata_resource import MetadataResource
from app.models.git_repository import GitRepository
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.schemas.metadata_resource_schema import MetadataResourceCreate
from app.core.dbt_parser import DBTResourceExtractor
from app.core.lookml_parser import LookMLResourceExtractor
from app.core.markdown_parser import MarkdownResourceExtractor
from app.core.tableau_parser import TableauTDSResourceExtractor
from app.core.sqlx_parser import SQLXResourceExtractor
from app.dependencies import async_session_maker # Import the session maker
from app.settings.config import settings
from app.services.instruction_sync_service import InstructionSyncService
from app.services.build_service import BuildService

logger = logging.getLogger(__name__)

class MetadataIndexingJobService:
    def __init__(self):
        self.parsers = {
            'dbt': DBTResourceExtractor,
            'lookml': LookMLResourceExtractor,
            'markdown': MarkdownResourceExtractor,
            'tableau': TableauTDSResourceExtractor,
            'dataform': SQLXResourceExtractor,
        }
        self.instruction_sync_service = InstructionSyncService()
        self.build_service = BuildService()

    async def _verify_data_source(self, db: AsyncSession, data_source_id: str, organization: Organization):
        """Verify data source exists and belongs to organization"""
        result = await db.execute(
            select(DataSource).where(
                DataSource.id == data_source_id,
                DataSource.organization_id == organization.id
            )
        )
        data_source = result.scalar_one_or_none()
        if not data_source:
            raise HTTPException(status_code=404, detail="Data source not found")
        return data_source
    
    async def _get_git_repository(self, db: AsyncSession, data_source_id: str, organization: Organization):
        """Get git repository for the data source"""
        result = await db.execute(
            select(GitRepository).where(
                GitRepository.data_source_id == data_source_id,
                GitRepository.organization_id == organization.id
            )
        )
        git_repository = result.scalar_one_or_none()
        
        if not git_repository:
            raise HTTPException(status_code=404, detail="Git repository not found for this data source")
        return git_repository
    
    async def start_indexing(
        self,
        db: AsyncSession,
        repo_id: str,
        organization: Organization,
        detected_project_types: Optional[List[str]] = None,
        data_source_id: Optional[str] = None,
        repo_name: Optional[str] = None,
    ):
        """Creates the MetadataIndexingJob record before background processing starts."""
        # Get the GitRepository to link the job
        git_repository = await db.execute(
            select(GitRepository).where(
                GitRepository.organization_id == organization.id,
                GitRepository.id == repo_id
            )
        )
        git_repository = git_repository.scalar_one_or_none()
        if not git_repository:
             raise HTTPException(status_code=404, detail=f"Git repository {repo_id} not found")

        # Create a new indexing job record (data_source_id is optional for org-level repos)
        job = MetadataIndexingJob(
            data_source_id=data_source_id,  # Can be None for org-level repos
            organization_id=organization.id,
            git_repository_id=git_repository.id,
            status="running", # Start as running
            started_at=datetime.utcnow(),
            detected_project_types=detected_project_types or [], # Store detected types
            current_phase='starting',  # Initial phase for progress tracking
            processed_files=0,
        )
        db.add(job)
        try:
            await db.commit()
            await db.refresh(job)
            logger.info(f"Created MetadataIndexingJob {job.id} for repo {repo_id}, repo_name={repo_name}")
            return job
        except Exception as e:
            await db.rollback()
            logger.error(f"Failed to create MetadataIndexingJob record for repo {repo_id}: {e}", exc_info=True)
            # Re-raise or handle appropriately - maybe raise HTTPException
            raise HTTPException(status_code=500, detail=f"Failed to create indexing job record: {e}")

    async def _parse_dbt_resources(
        self,
        db: AsyncSession,
        temp_dir: str,
        job_id: str,
        organization_id: str,
        data_source_id: Optional[str] = None,
        activate_new_resources: bool = True,
    ):
        """Parse DBT resources from a cloned repository and save using MetadataResource."""
        created_or_updated_resources = []
        try:
            logger.info(f"Starting DBT resource parsing for job {job_id} in {temp_dir}")
            extractor = DBTResourceExtractor(temp_dir)
            # Assuming extract_all_resources returns (resources_dict, columns_by_resource, docs_by_resource)
            resources_dict, columns_by_resource, docs_by_resource = extractor.extract_all_resources()

            # Mapping from DBT parser output keys to MetadataResource types
            # Ensure these types match what's expected elsewhere (e.g., frontend)
            resource_type_map = {
                'metrics': 'metric',
                'models': 'model',
                'sources': 'source',
                'seeds': 'seed',
                'macros': 'macro',
                'tests': 'test', # Includes singular_tests which parser might put here
                'exposures': 'exposure'
            }

            for parser_key, resource_type_singular in resource_type_map.items():
                resource_list = resources_dict.get(parser_key, [])
                for item in resource_list:
                    if not isinstance(item, dict):
                        logger.warning(f"Skipping non-dict item in {parser_key}: {item}")
                        continue

                    item_name = item.get('name', '')
                    if not item_name:
                        logger.warning(f"Skipping item with no name in {parser_key}: {item}")
                        continue

                    # Construct the fully qualified resource key for columns/docs lookup
                    # Needs refinement based on how keys are actually generated in parser
                    # Example: 'model.my_model', 'source.my_source.my_table'
                    resource_key_prefix = resource_type_singular
                    if parser_key == 'tests' and item.get('type') == 'singular_test':
                         resource_key_prefix = 'singular_test' # Or adjust based on parser's keys

                    # Ensure item_name is used correctly for the key
                    resource_lookup_key = f"{resource_key_prefix}.{item_name}"
                    # Special case for sources which have compound names
                    if parser_key == 'sources':
                         resource_lookup_key = f"source.{item_name}"


                    # Get columns and depends_on (adjust based on actual keys in item)
                    columns = columns_by_resource.get(resource_lookup_key, [])
                    depends_on = item.get('depends_on', []) # DBT extractor might put this directly in item

                    # Create or update the resource using the unified method
                    resource = await self._create_or_update_metadata_resource(
                        db=db,
                        item=item, # Pass the raw item dictionary
                        resource_type=f"dbt_{resource_type_singular}", # Add 'dbt_' prefix
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                        columns=[col for col in columns if isinstance(col, dict)], # Ensure columns are dicts
                        depends_on=[dep for dep in depends_on if isinstance(dep, str)] if isinstance(depends_on, list) else [],
                        sql_content=item.get('sql_content'),
                        # Pass source-specific fields if applicable
                        source_name=item.get('source_name') if parser_key == 'sources' else None,
                        database=item.get('database') if parser_key == 'sources' else None,
                        schema=item.get('schema') if parser_key == 'sources' else None
                    )
                    if resource:
                        created_or_updated_resources.append(resource)

            logger.info(f"Finished DBT resource parsing for job {job_id}. Found {len(created_or_updated_resources)} resources.")

        except Exception as e:
            logger.error(f"Error during DBT resource parsing for job {job_id}: {e}", exc_info=True)
            # Decide if parsing failure should fail the whole job or just log
            # For now, re-raise to let the main job handler catch it
            raise

        return created_or_updated_resources

    async def _parse_tableau_resources(
        self,
        db: AsyncSession,
        temp_dir: str,
        job_id: str,
        organization_id: str,
        data_source_id: Optional[str] = None,
        activate_new_resources: bool = True,
    ):
        """Parse Tableau TDS/TDSX resources from a cloned repository."""
        created_resources = []
        try:
            logger.info(f"Starting Tableau parsing for job {job_id} in {temp_dir}")

            extractor = TableauTDSResourceExtractor(temp_dir)
            resources_dict, columns_by_resource, docs_by_resource = extractor.extract_all_resources()

            # Iterate over all resource arrays and create MetadataResource for each
            for resource_type_key, items in resources_dict.items():
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    item_name = item.get('name')
                    if not item_name:
                        continue

                    # Lookup columns using unified key: resource_type.name
                    item_resource_type = item.get('resource_type', resource_type_key)
                    # No Tableau-specific filtering here; parser controls what is emitted
                    lookup_key = f"{item_resource_type}.{item_name}"
                    item_columns = columns_by_resource.get(lookup_key, [])

                    # For SQL: read standardized key 'sql_content' when present
                    sql_content = item.get('sql_content')

                    metadata_resource = await self._create_or_update_metadata_resource(
                        db=db,
                        item=item,
                        resource_type=item_resource_type,
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                        columns=item_columns,
                        depends_on=item.get('depends_on', []),
                        sql_content=sql_content,
                    )
                    if metadata_resource:
                        created_resources.append(metadata_resource)

            logger.info(f"Completed Tableau parsing for job {job_id}. Created/updated {len(created_resources)} resources")
            return created_resources

        except Exception as e:
            logger.error(f"Error during Tableau parsing for job {job_id}: {e}", exc_info=True)
            raise

    async def _parse_lookml_resources(
        self,
        db: AsyncSession,
        temp_dir: str,
        job_id: str,
        organization_id: str,
        data_source_id: Optional[str] = None,
        activate_new_resources: bool = True,
    ):
        """Parse LookML resources from a cloned repository."""
        created_resources = []
        try:
            logger.info(f"Starting LookML parsing for job {job_id} in {temp_dir}")
            
            # Initialize the LookML extractor
            extractor = LookMLResourceExtractor(temp_dir)
            
            # Debug: Log the directory structure
            logger.debug(f"LookML project structure:")
            for path in Path(temp_dir).rglob('*.lkml'):
                logger.debug(f"Found LookML file: {path.relative_to(temp_dir)}")
            
            # Extract all resources
            resources_dict, columns_by_resource, docs_by_resource = extractor.extract_all_resources()
            # Debug: Log what we found
            logger.debug(f"Extracted resources: {extractor.get_summary()}")
            
            # Process each resource type
            for resource_type, resources in resources_dict.items():
                logger.debug(f"Processing {len(resources)} {resource_type}")
                for resource_item in resources:
                    # Construct the lookup key to get columns for this resource
                    item_name = resource_item.get('name')
                    item_type_from_resource = resource_item.get('resource_type', resource_type)
                    lookup_key = f"{item_type_from_resource}.{item_name}"
                    
                    # Get columns from the separate dictionary, similar to DBT parsing
                    item_columns = columns_by_resource.get(lookup_key, [])

                    # Create/update the metadata resource
                    metadata_resource = await self._create_or_update_metadata_resource(
                        db=db,
                        item=resource_item, # Pass the entire resource item
                        resource_type=item_type_from_resource, # Use specific type if available
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                        # Pass the columns we just looked up
                        columns=item_columns,
                        depends_on=resource_item.get('depends_on', [])
                    )
                    
                    if metadata_resource:
                        created_resources.append(metadata_resource)
                        logger.debug(f"Created/updated {resource_type} resource: {resource_item.get('name')}")

            logger.info(f"Completed LookML parsing for job {job_id}. Created/updated {len(created_resources)} resources")
            return created_resources

        except Exception as e:
            logger.error(f"Error during LookML parsing for job {job_id}: {e}", exc_info=True)
            raise

    async def _parse_markdown_resources(
        self,
        db: AsyncSession,
        temp_dir: str,
        job_id: str,
        organization_id: str,
        data_source_id: Optional[str] = None,
        activate_new_resources: bool = True,
    ):
        """Parse Markdown files from a cloned repository."""
        created_resources = []
        try:
            logger.info(f"Starting Markdown parsing for job {job_id} in {temp_dir}")

            # Deactivate existing markdown resources for this job's organization so new chunks replace them
            await db.execute(
                update(MetadataResource)
                .where(
                    MetadataResource.metadata_indexing_job_id.in_(
                        select(MetadataIndexingJob.id).where(
                            MetadataIndexingJob.organization_id == organization_id
                        )
                    ),
                    MetadataResource.resource_type == 'markdown_document',
                )
                .values(is_active=False)
            )
            await db.commit()
            
            # Initialize the Markdown extractor
            extractor = MarkdownResourceExtractor(temp_dir)
            
            # Debug: Log the directory structure
            logger.debug(f"Markdown project structure:")
            for path in Path(temp_dir).rglob('*.md'):
                logger.debug(f"Found Markdown file: {path.relative_to(temp_dir)}")
            
            # Extract all resources
            resources_dict, columns_by_resource, docs_by_resource = extractor.extract_all_resources()
            # Debug: Log what we found
            logger.debug(f"Extracted resources: {extractor.get_summary()}")
            
            # Process markdown documents
            markdown_docs = resources_dict.get('markdown_documents', [])
            logger.debug(f"Processing {len(markdown_docs)} markdown documents")
            
            for doc_item in markdown_docs:
                # Create/update the metadata resource
                metadata_resource = await self._create_or_update_metadata_resource(
                    db=db,
                    item=doc_item, # Pass the entire document item
                    resource_type='markdown_document',
                    job_id=job_id,
                    organization_id=organization_id,
                    data_source_id=data_source_id,
                    activate_new_resources=activate_new_resources,
                    columns=[], # Markdown files don't have columns
                    depends_on=[] # Markdown files typically don't have dependencies
                )
                
                if metadata_resource:
                    created_resources.append(metadata_resource)
                    #  logger.debug(f"Created/updated markdown resource: {doc_item.get('name')}")

            logger.info(f"Completed Markdown parsing for job {job_id}. Created/updated {len(created_resources)} resources")
            return created_resources

        except Exception as e:
            logger.error(f"Error during Markdown parsing for job {job_id}: {e}", exc_info=True)
            raise

    async def _parse_sqlx_resources(
        self,
        db: AsyncSession,
        temp_dir: str,
        job_id: str,
        organization_id: str,
        data_source_id: Optional[str] = None,
        activate_new_resources: bool = True,
    ):
        """Parse Dataform resources (from .sqlx files) from a cloned repository."""
        created_or_updated_resources = []
        try:
            logger.info(f"Starting Dataform resource parsing for job {job_id} in {temp_dir}")
            extractor = SQLXResourceExtractor(temp_dir)
            resources_dict, columns_by_resource, docs_by_resource = extractor.extract_all_resources()

            # Use "dataform_*" as the canonical resource_type prefix for SQLX/Dataform
            resource_type_map = {
                "tables": "dataform_table",
                "assertions": "dataform_assertion",
                "operations": "dataform_operation",
                "declarations": "dataform_declaration",
            }

            for parser_key, resource_type in resource_type_map.items():
                resource_list = resources_dict.get(parser_key, [])
                for item in resource_list:
                    if not isinstance(item, dict):
                        logger.warning(f"Skipping non-dict SQLX item in {parser_key}: {item}")
                        continue

                    item_name = item.get("name", "")
                    if not item_name:
                        logger.warning(f"Skipping SQLX item with no name in {parser_key}: {item}")
                        continue

                    lookup_key = f"{resource_type}.{item_name}"
                    item_columns = columns_by_resource.get(lookup_key, [])
                    depends_on = item.get("depends_on", [])

                    resource = await self._create_or_update_metadata_resource(
                        db=db,
                        item=item,
                        resource_type=resource_type,
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                        columns=[col for col in item_columns if isinstance(col, dict)],
                        depends_on=[dep for dep in depends_on if isinstance(dep, str)] if isinstance(depends_on, list) else [],
                        sql_content=item.get("sql_body"),
                    )
                    if resource:
                        created_or_updated_resources.append(resource)

            logger.info(
                f"Finished SQLX resource parsing for job {job_id}. "
                f"Found {len(created_or_updated_resources)} resources."
            )

        except Exception as e:
            logger.error(f"Error during Dataform resource parsing for job {job_id}: {e}", exc_info=True)
            raise

        return created_or_updated_resources

    async def _create_or_update_metadata_resource(
        self,
        db: AsyncSession,
        item: Dict[str, Any], # Raw dictionary from the parser
        resource_type: str, # Should include prefix like 'dbt_model' or 'lookml_view'
        job_id: str,
        organization_id: str,
        data_source_id: Optional[str] = None,
        activate_new_resources: bool = True,
        columns: Optional[List[Dict[str, Any]]] = None,
        depends_on: Optional[List[str]] = None,
        sql_content: Optional[str] = None,
        source_name: Optional[str] = None, # DBT source specific
        database: Optional[str] = None,    # DBT source specific
        schema: Optional[str] = None       # DBT source specific
    ):
        """Create or update a generic MetadataResource"""
        resource_name = item.get('name', '')
        if not resource_name:
             logger.warning(f"Skipping resource creation/update due to missing name. Type: {resource_type}, Item: {item}")
             return None

        # Clean path relative to project root - IMPORTANT: Ensure this is done consistently!
        # The parser might already do this, or it should be done here.
        # Assuming path is already relative IF it exists in item.
        resource_path = item.get('path', '') # Path should be relative here

        resource_data = MetadataResourceCreate(
             name=resource_name,
             resource_type=resource_type,
             path=resource_path,
             description=item.get('description', ''),
             raw_data=item, # Store the original extracted item
             sql_content=sql_content, # Pass specific SQL content if available
             # Pass DBT source specific fields if provided
             source_name=source_name,
             database=database,
             schema=schema,
             # Pass columns/depends_on if provided
             columns=columns or [],
             depends_on=depends_on or [],
             is_active=True, # Default to active on create/update
             data_source_id=data_source_id,
             metadata_indexing_job_id=job_id,
             organization_id=organization_id,
        )

        try:
            # Check if resource already exists (using name, type, and organization_id as unique key)
            # Use job's organization for org-level git repos
            stmt = select(MetadataResource).where(
                 MetadataResource.name == resource_data.name,
                 MetadataResource.resource_type == resource_data.resource_type,
                 MetadataResource.metadata_indexing_job_id.in_(
                     select(MetadataIndexingJob.id).where(
                         MetadataIndexingJob.organization_id == organization_id
                     )
                 )
             )
            result = await db.execute(stmt)
            existing_resource = result.scalar_one_or_none()

            current_time = datetime.utcnow()

            if existing_resource:
                # Update existing resource - preserve user's is_active preference
                logger.debug(f"Updating existing resource: {resource_type} {resource_data.name}")
                update_data = resource_data.dict(exclude_unset=True)
                # Ensure essential fields like raw_data, columns, depends_on are updated
                update_data['raw_data'] = resource_data.raw_data
                update_data['columns'] = resource_data.columns
                update_data['depends_on'] = resource_data.depends_on
                update_data['last_synced_at'] = current_time
                update_data['metadata_indexing_job_id'] = job_id # Link to the latest job
                # NOTE: Do NOT override is_active - preserve user's selection preference
                update_data.pop('is_active', None)
                update_data['updated_at'] = current_time # Explicitly set updated_at

                # Apply updates
                for key, value in update_data.items():
                     setattr(existing_resource, key, value)

                db.add(existing_resource) # Add to session to track changes
                await db.commit()
                await db.refresh(existing_resource)
                return existing_resource
            else:
                # Create new resource
                resource_dict = resource_data.dict()
                if not activate_new_resources:
                    resource_dict["is_active"] = False
                new_resource = MetadataResource(**resource_dict)
                new_resource.last_synced_at = current_time
                # created_at and updated_at should be handled by BaseSchema default/onupdate if configured,
                # otherwise set them explicitly if needed:
                # new_resource.created_at = current_time
                # new_resource.updated_at = current_time
                db.add(new_resource)
                await db.commit()
                await db.refresh(new_resource)
                return new_resource
        except Exception as db_error:
             logger.error(f"Database error creating/updating resource {resource_type} {resource_name}: {db_error}", exc_info=True)
             await db.rollback() # Rollback on error for this specific resource
             return None # Indicate failure for this resource

    async def get_metadata_resources(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
        resource_type: Optional[str] = None, # Can filter by 'dbt_model', 'lookml_view', etc.
        skip: int = 0,
        limit: int = 100
    ):
        """Get active MetadataResources for a data source, optionally filtered by type."""
        await self._verify_data_source(db, data_source_id, organization)

        # Base query for active resources linked to the data source
        query = select(MetadataResource).where(
             MetadataResource.data_source_id == data_source_id,
             MetadataResource.is_active == True
        )


        # Apply type filter if provided
        if resource_type:
             query = query.where(MetadataResource.resource_type == resource_type)

        # Get total count for pagination
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar_one()

        # Get paginated results
        query = query.order_by(MetadataResource.name).offset(skip).limit(limit)
        result = await db.execute(query)
        resources = result.scalars().all()


        return {"items": resources, "total": total}

    async def get_indexing_jobs(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
        skip: int = 0,
        limit: int = 100
    ):
        """Get indexing jobs for a data source"""
        # Verify data source exists
        await self._verify_data_source(db, data_source_id, organization)
        
        # Get total count
        result = await db.execute(
            select(MetadataIndexingJob).where(
                MetadataIndexingJob.data_source_id == data_source_id,
                MetadataIndexingJob.organization_id == organization.id
            )
        )
        total = len(result.scalars().all())
        
        # Get paginated results
        result = await db.execute(
            select(MetadataIndexingJob)
            .where(
                MetadataIndexingJob.data_source_id == data_source_id,
                MetadataIndexingJob.organization_id == organization.id
            )
            .order_by(MetadataIndexingJob.started_at.desc())
            .offset(skip)
            .limit(limit)
        )
        jobs = result.scalars().all()
        
        return {"items": jobs, "total": total}

    async def start_indexing_background(
        self,
        db: AsyncSession,
        repository_id: str,
        repo_path: str,
        organization,
        detected_project_types: Optional[List[str]] = None,
        build_id: Optional[str] = None,  # Optional pre-created build to use
        data_source_id: Optional[str] = None,  # Optional - for backwards compatibility
        repo_name: Optional[str] = None,  # New file-based flow
    ):
        """Start indexing a Git repository in the background

        Args:
            build_id: Optional build ID to add instructions to. If not provided,
                     a new build will be created during indexing.
            data_source_id: Optional data source ID for backwards compatibility.
                     Org-level repos don't need this.
            repo_name: If provided, uses the new file-based indexing flow instead
                     of the legacy parser-based flow.
        """
        # Call start_indexing first to create the job record synchronously
        job = await self.start_indexing(
            db=db,
            repo_id=repository_id,
            organization=organization,
            detected_project_types=detected_project_types,
            data_source_id=data_source_id,
            repo_name=repo_name,
        )

        # Choose indexing method based on whether repo_name is provided
        run_method = self._run_file_indexing_job if repo_name else self._run_indexing_job

        run_kwargs = dict(
            repository_id=repository_id,
            repo_path=repo_path,
            organization=organization,
            job_id=job.id,
            build_id=build_id,
            data_source_id=data_source_id,
        )
        if repo_name:
            run_kwargs['repo_name'] = repo_name
        else:
            run_kwargs['detected_project_types'] = detected_project_types or []

        # In tests, run the indexing job inline so the task isn't cancelled when the event loop ends.
        if settings.TESTING:
            logger.info(f"TESTING mode: running indexing job {job.id} inline")
            await run_method(**run_kwargs)
            return {
                "status": "completed",
                "message": "Indexing job completed inline (testing mode)",
                "job_id": job.id,
            }

        # Now schedule the background task to perform the actual parsing
        asyncio.create_task(run_method(**run_kwargs))

        logger.info(f"Scheduled background indexing task for job {job.id}")
        return {"status": "started", "message": "Indexing job started in background", "job_id": job.id}

    async def _run_file_indexing_job(
        self,
        repository_id: str,
        repo_path: str,
        organization,
        job_id: str,
        repo_name: str,
        build_id: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ):
        """New file-based indexing: walk files, classify, create Instructions directly.

        No MetadataResource creation. No parser calls.
        """
        from app.core.git_file_walker import walk_repo_files

        organization_id = organization.id if hasattr(organization, 'id') else organization

        async with async_session_maker() as db:
            try:
                # Re-fetch org
                org_result = await db.execute(
                    select(Organization).where(Organization.id == organization_id)
                )
                current_org = org_result.scalar_one_or_none()
                if not current_org:
                    logger.error(f"Job {job_id}: Organization {organization_id} not found")
                    error_message = f"Organization {organization_id} not found"
                    await db.execute(
                        update(MetadataIndexingJob)
                        .where(MetadataIndexingJob.id == job_id)
                        .values({
                            "status": "failed",
                            "completed_at": datetime.utcnow(),
                            "error_message": error_message,
                        })
                    )
                    await db.commit()
                    return

                # Re-fetch git repo
                git_repo_result = await db.execute(
                    select(GitRepository).where(GitRepository.id == repository_id)
                )
                git_repo = git_repo_result.scalar_one_or_none()
                if not git_repo:
                    logger.error(f"Job {job_id}: GitRepository {repository_id} not found")
                    error_message = f"GitRepository {repository_id} not found"
                    await db.execute(
                        update(MetadataIndexingJob)
                        .where(MetadataIndexingJob.id == job_id)
                        .values({
                            "status": "failed",
                            "completed_at": datetime.utcnow(),
                            "error_message": error_message,
                        })
                    )
                    await db.commit()
                    return

                # Re-fetch data source if provided
                data_source = None
                if data_source_id:
                    ds_result = await db.execute(
                        select(DataSource).where(DataSource.id == data_source_id)
                    )
                    data_source = ds_result.scalar_one_or_none()

                # Phase 1: Walk files
                await db.execute(
                    update(MetadataIndexingJob)
                    .where(MetadataIndexingJob.id == job_id)
                    .values(current_phase='parsing', processed_files=0)
                )
                await db.commit()

                files = walk_repo_files(repo_path, repo_name)
                total_files = len(files)
                logger.info(f"Job {job_id}: File walker found {total_files} files in repo '{repo_name}'")

                await db.execute(
                    update(MetadataIndexingJob)
                    .where(MetadataIndexingJob.id == job_id)
                    .values(current_phase='syncing', total_files=total_files, processed_files=0)
                )
                await db.commit()

                # Phase 2: Create/get build
                sync_build = None
                try:
                    if build_id:
                        sync_build = await self.build_service.get_build(db, build_id)
                        if sync_build:
                            logger.info(f"Job {job_id}: Using pre-created build {build_id}")

                    if not sync_build:
                        sync_build = await self.build_service.get_or_create_draft_build(
                            db,
                            current_org.id,
                            source='git',
                            metadata_indexing_job_id=job_id,
                            commit_sha=git_repo.last_indexed_commit_sha,
                            branch=git_repo.branch,
                        )
                        logger.info(f"Job {job_id}: Created build {sync_build.id} for file indexing")

                    await db.execute(
                        update(MetadataIndexingJob)
                        .where(MetadataIndexingJob.id == job_id)
                        .values(build_id=sync_build.id)
                    )
                    await db.commit()
                except Exception as build_error:
                    logger.warning(f"Job {job_id}: Failed to create/get build: {build_error}")

                # Phase 3: Sync each file to instruction
                synced_count = 0
                sync_errors = 0
                for i, file_info in enumerate(files):
                    try:
                        result = await self.instruction_sync_service.sync_file_to_instruction(
                            db=db,
                            file_path=file_info.relative_path,
                            file_content=file_info.content,
                            content_hash=file_info.content_hash,
                            organization=current_org,
                            git_repo=git_repo,
                            resource_type=file_info.resource_type,
                            build=sync_build,
                            data_source=data_source,
                        )
                        if result:
                            synced_count += 1
                    except Exception as sync_error:
                        sync_errors += 1
                        logger.error(
                            f"Job {job_id}: Failed to sync file {file_info.relative_path}: {sync_error}",
                            exc_info=True,
                        )

                    # Update progress every 10 files or on last item
                    if (i + 1) % 10 == 0 or i == total_files - 1:
                        await db.execute(
                            update(MetadataIndexingJob)
                            .where(MetadataIndexingJob.id == job_id)
                            .values(processed_files=i + 1)
                        )
                        await db.commit()

                logger.info(f"Job {job_id}: Synced {synced_count}/{total_files} files ({sync_errors} errors)")

                # Phase 4: Archive deleted files (scoped to this repo)
                current_paths = {f.relative_path for f in files}
                path_prefix = f"{repo_name}/"
                archived_count = await self.instruction_sync_service.archive_deleted_files(
                    db=db,
                    org_id=current_org.id,
                    current_file_paths=current_paths,
                    path_prefix=path_prefix,
                    build=sync_build,
                )
                if archived_count > 0:
                    logger.info(f"Job {job_id}: Archived {archived_count} instructions for deleted files")

                # Phase 5: Finalize build
                if sync_build and not build_id:
                    try:
                        await self.build_service.submit_build(db, sync_build.id)
                        await self.build_service.approve_build(db, sync_build.id, approved_by_user_id=None)
                        await self.build_service.promote_build(db, sync_build.id)
                        logger.info(f"Job {job_id}: Finalized build {sync_build.id}")
                    except Exception as finalize_error:
                        logger.warning(f"Job {job_id}: Failed to finalize build: {finalize_error}")
                elif sync_build and build_id:
                    logger.info(f"Job {job_id}: Build {sync_build.id} left in draft for manual review")

                # Update job status
                await db.execute(
                    update(MetadataIndexingJob)
                    .where(MetadataIndexingJob.id == job_id)
                    .values({
                        "status": "completed",
                        "completed_at": datetime.utcnow(),
                        "total_resources": total_files,
                        "processed_resources": synced_count,
                        "total_files": total_files,
                        "processed_files": total_files,
                        "current_phase": "completed",
                    })
                )
                await db.execute(
                    update(GitRepository)
                    .where(GitRepository.id == repository_id)
                    .values({
                        "status": "completed",
                        "updated_at": datetime.utcnow(),
                        "last_indexed_at": datetime.utcnow(),
                    })
                )
                await db.commit()

            except Exception as e:
                logger.error(f"Job {job_id}: Error during file indexing: {e}", exc_info=True)
                await db.rollback()
                error_message = f"File indexing failed: {str(e)[:500]}"
                await db.execute(
                    update(MetadataIndexingJob)
                    .where(MetadataIndexingJob.id == job_id)
                    .values({
                        "status": "failed",
                        "completed_at": datetime.utcnow(),
                        "error_message": error_message,
                    })
                )
                await db.execute(
                    update(GitRepository)
                    .where(GitRepository.id == repository_id)
                    .values({"status": "failed", "updated_at": datetime.utcnow()})
                )
                await db.commit()

            finally:
                try:
                    shutil.rmtree(repo_path)
                    logger.info(f"Job {job_id}: Cleaned up temporary directory: {repo_path}")
                except Exception as cleanup_e:
                    logger.error(f"Job {job_id}: Error cleaning up {repo_path}: {cleanup_e}")

    async def _run_indexing_job(
        self,
        # The db: AsyncSession parameter is removed from the signature
        repository_id: str,
        repo_path: str,
        organization,
        detected_project_types: List[str],
        job_id: str,
        build_id: Optional[str] = None,
        data_source_id: Optional[str] = None,
    ):
        """Run the actual indexing job and update repository status when complete
        
        Args:
            build_id: Optional pre-created build ID to use. If provided, instructions
                     will be added to this build instead of creating a new one.
        """
        job_status = "failed"  # Default status
        job_error_message = None
        all_created_resources = []
        
        # Store organization_id since the organization object may be from a different session
        organization_id = organization.id if hasattr(organization, 'id') else organization

        # Create a new, independent session for this background task, just like in the slack service
        async with async_session_maker() as db:
            try:
                # Re-fetch organization in this session to avoid detached instance issues
                org_result = await db.execute(
                    select(Organization).where(Organization.id == organization_id)
                )
                current_org = org_result.scalar_one_or_none()
                if not current_org:
                    logger.error(f"Job {job_id}: Organization {organization_id} not found")
                    return
                
                logger.info(f"Background job {job_id}: Starting parsing for types {detected_project_types}")

                # Update job phase to 'parsing'
                await db.execute(
                    update(MetadataIndexingJob)
                    .where(MetadataIndexingJob.id == job_id)
                    .values(current_phase='parsing', processed_files=0)
                )
                await db.commit()

                # Check for existing resources to determine if we should auto-activate new ones
                existing_count_result = await db.execute(
                    select(func.count(MetadataResource.id)).where(
                        MetadataResource.metadata_indexing_job_id.in_(
                            select(MetadataIndexingJob.id).where(
                                MetadataIndexingJob.organization_id == organization_id
                            )
                        )
                    )
                )
                has_existing_resources = existing_count_result.scalar_one() > 0
                activate_new_resources = not has_existing_resources

                # --- Trigger DBT Parsing ---
                if 'dbt' in detected_project_types:
                    dbt_resources = await self._parse_dbt_resources(
                        db=db,
                        temp_dir=repo_path,
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                    )
                    all_created_resources.extend(dbt_resources or [])

                # --- Trigger LookML Parsing ---
                if 'lookml' in detected_project_types:
                    lookml_resources = await self._parse_lookml_resources(
                        db=db,
                        temp_dir=repo_path,
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                    )
                    all_created_resources.extend(lookml_resources or [])

                # --- Trigger Markdown Parsing ---
                if 'markdown' in detected_project_types:
                    markdown_resources = await self._parse_markdown_resources(
                        db=db,
                        temp_dir=repo_path,
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                    )
                    all_created_resources.extend(markdown_resources or [])

                # --- Trigger Tableau Parsing ---
                if 'tableau' in detected_project_types:
                    tableau_resources = await self._parse_tableau_resources(
                        db=db,
                        temp_dir=repo_path,
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                    )
                    all_created_resources.extend(tableau_resources or [])

                # --- Trigger Dataform Parsing ---
                if 'dataform' in detected_project_types:
                    sqlx_resources = await self._parse_sqlx_resources(
                        db=db,
                        temp_dir=repo_path,
                        job_id=job_id,
                        organization_id=organization_id,
                        data_source_id=data_source_id,
                        activate_new_resources=activate_new_resources,
                    )
                    all_created_resources.extend(sqlx_resources or [])

                if not detected_project_types:
                    logger.warning(f"Job {job_id}: No project types were detected, nothing to parse.")
                    job_status = "completed"
                    job_error_message = "No project types detected."
                elif not all_created_resources:
                    logger.warning(f"Job {job_id}: Project types {detected_project_types} detected, but no resources were parsed.")
                    job_status = "failed"
                    job_error_message = f"Detected {detected_project_types} but no resources parsed."
                else:
                    logger.info(f"Job {job_id}: Parsing completed successfully. Parsed {len(all_created_resources)} resources.")
                    job_status = "completed"

                if job_status == "completed":
                    # Get stale resources before deleting them (for instruction archival)
                    # For org-level repos, look for resources from same organization but not from this job
                    stale_stmt = select(MetadataResource.id).where(
                        MetadataResource.metadata_indexing_job_id.in_(
                            select(MetadataIndexingJob.id).where(
                                MetadataIndexingJob.organization_id == organization_id,
                                MetadataIndexingJob.git_repository_id == repository_id
                            )
                        ),
                        MetadataResource.metadata_indexing_job_id != job_id,
                    )
                    stale_result = await db.execute(stale_stmt)
                    stale_resource_ids = [row[0] for row in stale_result.fetchall()]
                    
                    # Archive instructions for deleted resources
                    for resource_id in stale_resource_ids:
                        try:
                            await self.instruction_sync_service.archive_instruction_for_deleted_resource(
                                db, resource_id
                            )
                        except Exception as archive_error:
                            logger.warning(f"Job {job_id}: Failed to archive instruction for resource {resource_id}: {archive_error}")
                    
                    # Delete stale resources
                    delete_stmt = delete(MetadataResource).where(
                        MetadataResource.id.in_(stale_resource_ids)
                    )
                    result = await db.execute(delete_stmt)
                    logger.info(
                        f"Job {job_id}: Deleted {result.rowcount or 0} stale metadata resources for organization {organization_id}"
                    )
                    
                    # Sync all created/updated resources to instructions
                    logger.info(f"Job {job_id}: Syncing {len(all_created_resources)} resources to instructions")
                    
                    # Update job phase to 'syncing' with total count
                    total_resources = len(all_created_resources)
                    await db.execute(
                        update(MetadataIndexingJob)
                        .where(MetadataIndexingJob.id == job_id)
                        .values(current_phase='syncing', total_files=total_resources, processed_files=0)
                    )
                    await db.commit()
                    
                    # === Build System Integration ===
                    # Use pre-created build or create a draft build for this git sync job
                    sync_build = None
                    try:
                        if build_id:
                            # Use pre-created build (from sync_branch flow)
                            sync_build = await self.build_service.get_build(db, build_id)
                            if sync_build:
                                logger.info(f"Job {job_id}: Using pre-created build {build_id}")
                            else:
                                logger.warning(f"Job {job_id}: Pre-created build {build_id} not found, creating new one")
                        
                        if not sync_build:
                            # Get git repository info for build metadata
                            git_repo_result = await db.execute(
                                select(GitRepository).where(GitRepository.id == repository_id)
                            )
                            git_repo = git_repo_result.scalar_one_or_none()
                            
                            sync_build = await self.build_service.get_or_create_draft_build(
                                db, 
                                current_org.id, 
                                source='git',
                                metadata_indexing_job_id=job_id,
                                commit_sha=None,  # Could be added if we track commits
                                branch=git_repo.branch if git_repo else None
                            )
                            logger.info(f"Job {job_id}: Created build {sync_build.id} for git sync")
                        
                        # Link the build to the job
                        await db.execute(
                            update(MetadataIndexingJob)
                            .where(MetadataIndexingJob.id == job_id)
                            .values(build_id=sync_build.id)
                        )
                        await db.commit()
                    except Exception as build_error:
                        logger.warning(f"Job {job_id}: Failed to create/get build: {build_error}")
                    
                    synced_count = 0
                    sync_errors = 0
                    for i, resource in enumerate(all_created_resources):
                        try:
                            result = await self.instruction_sync_service.sync_resource_to_instruction(
                                db, resource, current_org, build=sync_build
                            )
                            if result:
                                synced_count += 1
                                logger.debug(f"Job {job_id}: Synced resource {resource.id} ({resource.name}) -> instruction {result.id}")
                            else:
                                logger.warning(f"Job {job_id}: Resource {resource.id} ({resource.name}) was not synced (returned None)")
                        except Exception as sync_error:
                            sync_errors += 1
                            logger.error(f"Job {job_id}: Failed to sync resource {resource.id} ({getattr(resource, 'name', 'unknown')}) to instruction: {sync_error}", exc_info=True)
                        
                        # Update progress every 10 resources or on last item
                        if (i + 1) % 10 == 0 or i == total_resources - 1:
                            await db.execute(
                                update(MetadataIndexingJob)
                                .where(MetadataIndexingJob.id == job_id)
                                .values(processed_files=i + 1)
                            )
                            await db.commit()
                    
                    logger.info(f"Job {job_id}: Synced {synced_count}/{len(all_created_resources)} resources to instructions ({sync_errors} errors)")
                    
                    # === Finalize Build ===
                    # Auto-finalize the build to make instructions visible in main
                    # Skip auto-finalize if build_id was pre-provided (user wants to review)
                    if sync_build and not build_id:
                        try:
                            await self.build_service.submit_build(db, sync_build.id)
                            await self.build_service.approve_build(db, sync_build.id, approved_by_user_id=None)
                            await self.build_service.promote_build(db, sync_build.id)
                            logger.info(f"Job {job_id}: Finalized build {sync_build.id} (synced: {synced_count}, total: {sync_build.total_instructions})")
                        except Exception as finalize_error:
                            logger.warning(f"Job {job_id}: Failed to finalize build {sync_build.id}: {finalize_error}")
                    elif sync_build and build_id:
                        # Pre-created build stays in draft for user review
                        logger.info(f"Job {job_id}: Build {sync_build.id} left in draft status for manual review")

                # All database operations below will use the new session
                await db.execute(
                    update(MetadataIndexingJob)
                    .where(MetadataIndexingJob.id == job_id)
                    .values({
                        "status": job_status,
                        "completed_at": datetime.utcnow(),
                        "total_resources": len(all_created_resources),
                        "processed_resources": len(all_created_resources),
                        "total_files": len(all_created_resources),
                        "processed_files": len(all_created_resources),
                        "current_phase": 'completed' if job_status == 'completed' else 'failed',
                        "error_message": job_error_message
                    })
                )

                repo_status = "completed" if job_status == "completed" else "failed"
                repo_update_values = {
                    "status": repo_status,
                    "updated_at": datetime.utcnow()
                }
                if repo_status == "completed":
                    repo_update_values["last_indexed_at"] = datetime.utcnow()
                await db.execute(
                    update(GitRepository)
                    .where(GitRepository.id == repository_id)
                    .values(repo_update_values)
                )
                await db.commit()

            except Exception as e:
                logger.error(f"Job {job_id}: Error during parsing: {e}", exc_info=True)
                # Handle error state
                await db.rollback()
                error_message = f"Parsing failed: {str(e)[:500]}"
                await db.execute(
                    update(MetadataIndexingJob)
                    .where(MetadataIndexingJob.id == job_id)
                    .values({
                        "status": "failed",
                        "completed_at": datetime.utcnow(),
                        "error_message": error_message
                    })
                )
                await db.execute(
                    update(GitRepository)
                    .where(GitRepository.id == repository_id)
                    .values({
                        "status": "failed",
                        "updated_at": datetime.utcnow()
                    })
                )
                await db.commit()

            finally:
                try:
                    shutil.rmtree(repo_path)
                    logger.info(f"Job {job_id}: Cleaned up temporary directory: {repo_path}")
                except Exception as cleanup_e:
                    logger.error(f"Job {job_id}: Error cleaning up temporary directory {repo_path}: {cleanup_e}")

    async def deactivate_metadata_indexing_job(
        self,
        db: AsyncSession,
        job_id: str,
        data_source_id: str,
        organization: Organization
    ):
        
        metadata_indexing_job = await db.execute(
            select(MetadataIndexingJob).where(
                MetadataIndexingJob.id == job_id,
                MetadataIndexingJob.data_source_id == data_source_id,
                MetadataIndexingJob.organization_id == organization.id
            )
        )
        metadata_indexing_job = metadata_indexing_job.scalar_one_or_none()

        if metadata_indexing_job:
            metadata_indexing_job.is_active = False
            await db.commit()
            return metadata_indexing_job
        else:
            raise HTTPException(status_code=404, detail="Metadata indexing job not found")