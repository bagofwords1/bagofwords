import logging
from collections import defaultdict
from fastapi import HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
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
from app.dependencies import async_session_maker # Import the session maker

logger = logging.getLogger(__name__)

class MetadataIndexingJobService:
    def __init__(self):
        self.parsers = {
            'dbt': DBTResourceExtractor,
            'lookml': LookMLResourceExtractor,
            'markdown': MarkdownResourceExtractor,
        }

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
        data_source_id: str,
        organization: Organization,
        detected_project_types: List[str]
    ):
        """Creates the MetadataIndexingJob record before background processing starts."""
        # Verify data source exists
        await self._verify_data_source(db, data_source_id, organization)

        # Get the GitRepository to link the job
        git_repository = await db.execute(
            select(GitRepository).where(
                GitRepository.data_source_id == data_source_id,
                GitRepository.organization_id == organization.id,
                GitRepository.id == repo_id
            )
        )
        git_repository = git_repository.scalar_one_or_none()
        if not git_repository:
             # This case should ideally be caught earlier, but double-check
             raise HTTPException(status_code=404, detail=f"Git repository {repo_id} not found for data source {data_source_id}")

        # Create a new indexing job record
        job = MetadataIndexingJob(
            data_source_id=data_source_id,
            organization_id=organization.id,
            git_repository_id=git_repository.id,
            status="running", # Start as running
            started_at=datetime.utcnow(),
            detected_project_types=detected_project_types # Store detected types
        )
        db.add(job)
        try:
            await db.commit()
            await db.refresh(job)
            logger.info(f"Created MetadataIndexingJob {job.id} for repo {repo_id}, types: {detected_project_types}")
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
        data_source_id: str,
        job_id: str,
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
                        data_source_id=data_source_id,
                        job_id=job_id,
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

    async def _parse_lookml_resources(
        self,
        db: AsyncSession,
        temp_dir: str,
        data_source_id: str,
        job_id: str,
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
                        data_source_id=data_source_id,
                        job_id=job_id,
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
        data_source_id: str,
        job_id: str,
    ):
        """Parse Markdown files from a cloned repository."""
        created_resources = []
        try:
            logger.info(f"Starting Markdown parsing for job {job_id} in {temp_dir}")
            
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
                    data_source_id=data_source_id,
                    job_id=job_id,
                    columns=[], # Markdown files don't have columns
                    depends_on=[] # Markdown files typically don't have dependencies
                )
                
                if metadata_resource:
                    created_resources.append(metadata_resource)
                    logger.debug(f"Created/updated markdown resource: {doc_item.get('name')}")

            logger.info(f"Completed Markdown parsing for job {job_id}. Created/updated {len(created_resources)} resources")
            return created_resources

        except Exception as e:
            logger.error(f"Error during Markdown parsing for job {job_id}: {e}", exc_info=True)
            raise

    async def _create_or_update_metadata_resource(
        self,
        db: AsyncSession,
        item: Dict[str, Any], # Raw dictionary from the parser
        resource_type: str, # Should include prefix like 'dbt_model' or 'lookml_view'
        data_source_id: str,
        job_id: str,
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
             metadata_indexing_job_id=job_id
        )

        try:
            # Check if resource already exists (using name, type, and data_source_id as unique key)
            stmt = select(MetadataResource).where(
                 MetadataResource.name == resource_data.name,
                 MetadataResource.resource_type == resource_data.resource_type,
                 MetadataResource.data_source_id == data_source_id
             )
            result = await db.execute(stmt)
            existing_resource = result.scalar_one_or_none()

            current_time = datetime.utcnow()

            if existing_resource:
                # Update existing resource
                logger.debug(f"Updating existing resource: {resource_type} {resource_data.name}")
                update_data = resource_data.dict(exclude_unset=True)
                # Ensure essential fields like raw_data, columns, depends_on are updated
                update_data['raw_data'] = resource_data.raw_data
                update_data['columns'] = resource_data.columns
                update_data['depends_on'] = resource_data.depends_on
                update_data['last_synced_at'] = current_time
                update_data['metadata_indexing_job_id'] = job_id # Link to the latest job
                update_data['is_active'] = True # Mark as active on update
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
                logger.debug(f"Creating new resource: {resource_type} {resource_data.name}")
                new_resource = MetadataResource(**resource_data.dict())
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
        data_source_id: str,
        organization,
        detected_project_types: List[str] # Add detected_project_types here
    ):
        """Start indexing a Git repository in the background"""
        # Call start_indexing first to create the job record synchronously
        job = await self.start_indexing(
            db=db,
            repo_id=repository_id,
            data_source_id=data_source_id,
            organization=organization,
            detected_project_types=detected_project_types
        )

        # Now schedule the background task to perform the actual parsing
        asyncio.create_task(
            self._run_indexing_job(
                # The db session is no longer passed here
                repository_id=repository_id,
                repo_path=repo_path,
                data_source_id=data_source_id,
                organization=organization,
                detected_project_types=detected_project_types,
                job_id=job.id
            )
        )

        logger.info(f"Scheduled background indexing task for job {job.id}")
        return {"status": "started", "message": "Indexing job started in background", "job_id": job.id}

    async def _run_indexing_job(
        self,
        # The db: AsyncSession parameter is removed from the signature
        repository_id: str,
        repo_path: str,
        data_source_id: str,
        organization,
        detected_project_types: List[str],
        job_id: str
    ):
        """Run the actual indexing job and update repository status when complete"""
        job_status = "failed"  # Default status
        job_error_message = None
        all_created_resources = []

        # Create a new, independent session for this background task, just like in the slack service
        async with async_session_maker() as db:
            try:
                logger.info(f"Background job {job_id}: Starting parsing for types {detected_project_types}")

                # --- Trigger DBT Parsing ---
                if 'dbt' in detected_project_types:
                    dbt_resources = await self._parse_dbt_resources(
                        db=db,
                        temp_dir=repo_path,
                        data_source_id=data_source_id,
                        job_id=job_id,
                    )
                    all_created_resources.extend(dbt_resources or [])

                # --- Trigger LookML Parsing ---
                if 'lookml' in detected_project_types:
                    lookml_resources = await self._parse_lookml_resources(
                        db=db,
                        temp_dir=repo_path,
                        data_source_id=data_source_id,
                        job_id=job_id,
                    )
                    all_created_resources.extend(lookml_resources or [])

                # --- Trigger Markdown Parsing ---
                if 'markdown' in detected_project_types:
                    markdown_resources = await self._parse_markdown_resources(
                        db=db,
                        temp_dir=repo_path,
                        data_source_id=data_source_id,
                        job_id=job_id,
                    )
                    all_created_resources.extend(markdown_resources or [])

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

                # All database operations below will use the new session
                await db.execute(
                    update(MetadataIndexingJob)
                    .where(MetadataIndexingJob.id == job_id)
                    .values({
                        "status": job_status,
                        "completed_at": datetime.utcnow(),
                        "total_resources": len(all_created_resources),
                        "processed_resources": len(all_created_resources),
                        "error_message": job_error_message
                    })
                )

                repo_status = "completed" if job_status == "completed" else "failed"
                await db.execute(
                    update(GitRepository)
                    .where(GitRepository.id == repository_id)
                    .values({
                        "status": repo_status,
                        "updated_at": datetime.utcnow()
                    })
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