from fastapi import HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime
from typing import Optional, Dict, List, Any
import tempfile
import asyncio
import shutil

from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.dbt_resource import DBTResource
from app.models.git_repository import GitRepository
from app.models.data_source import DataSource
from app.models.organization import Organization
from app.schemas.dbt_resource_schema import DBTResourceCreate
from app.core.dbt_parser import DBTResourceExtractor
from app.schemas.dbt_config_schema import DBTResourcesSchema, ColumnSchema

class MetadataIndexingJobService:
    def __init__(self):
        pass

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
        temp_dir: str,
        data_source_id: str,
        organization: Organization
    ):
        """Start a metadata indexing job for a data source"""
        # Verify data source exists
        await self._verify_data_source(db, data_source_id, organization)

        git_repository = await db.execute(
            select(GitRepository).where(
                GitRepository.data_source_id == data_source_id,
                GitRepository.organization_id == organization.id,
                GitRepository.id == repo_id
            )
        )
        git_repository = git_repository.scalar_one_or_none()
        # Get git repository
        # Create a new indexing job
        job = MetadataIndexingJob(
            data_source_id=data_source_id,
            organization_id=organization.id,
            git_repository_id=git_repository.id,
            status="running",
            started_at=datetime.utcnow()
        )
        
        db.add(job)
        await db.commit()
        await db.refresh(job)


        try:
            # Parse DBT resources
            resources = await self._parse_dbt_resources(
                db=db,
                temp_dir=temp_dir,
                data_source_id=data_source_id,
                job_id=job.id,
            )
            # Update job status
            await db.execute(
                update(MetadataIndexingJob)
                .where(MetadataIndexingJob.id == job.id)
                .values(
                    status="completed",
                    completed_at=datetime.utcnow()
                )
            )
            await db.commit()
            await db.refresh(job)
            
            return job
        except Exception as e:
            # Update job status on failure
            job.status = "failed"
            job.error_message = str(e)
            job.completed_at = datetime.utcnow()
            await db.commit()
            raise HTTPException(status_code=500, detail=f"Failed to index metadata: {str(e)}")
    
    async def _parse_dbt_resources(
        self,
        db: AsyncSession,
        temp_dir: str,
        data_source_id: str,
        job_id: str,
    ):
        """Parse DBT resources from a cloned repository"""
        created_resources = []
        # Parse DBT resources using the updated extractor
        extractor = DBTResourceExtractor(temp_dir)
        resources_dict = extractor.extract_all_resources()
        # Convert the dictionary to a DBTResourcesSchema object
        resources_schema = DBTResourcesSchema(
            metrics=resources_dict.get('metrics', []),
            models=resources_dict.get('models', []),
            sources=resources_dict.get('sources', []),
            seeds=resources_dict.get('seeds', []),
            macros=resources_dict.get('macros', []),
            tests=resources_dict.get('tests', []),
            exposures=resources_dict.get('exposures', []),
            columns_by_resource=extractor.columns_by_resource,
            docs_by_resource=extractor.docs_by_resource
        )
        
        # Create DBT resource records for each resource type
        resource_types = {
            'metrics': resources_schema.metrics if hasattr(resources_schema, 'metrics') else [],
            'models': resources_schema.models if hasattr(resources_schema, 'models') else [],
            'sources': resources_schema.sources if hasattr(resources_schema, 'sources') else [],
            'seeds': resources_schema.seeds if hasattr(resources_schema, 'seeds') else [],
            'macros': resources_schema.macros if hasattr(resources_schema, 'macros') else [],
            'tests': resources_schema.tests if hasattr(resources_schema, 'tests') else [],
            'exposures': resources_schema.exposures if hasattr(resources_schema, 'exposures') else []
        }
        
        for resource_type, resource_list in resource_types.items():
            for item in resource_list:
                # Get columns for this resource
                resource_key = f"{resource_type[:-1]}.{item.name if hasattr(item, 'name') else item['name']}"
                columns = resources_schema.columns_by_resource.get(resource_key, []) if hasattr(resources_schema, 'columns_by_resource') else []
                
                # Convert item to dict if it's a Pydantic model
                item_dict = item.dict() if hasattr(item, 'dict') else item
                # Clean up the path - remove temp directory
                if temp_dir and 'path' in item_dict:
                    item_dict['path'] = item_dict['path'].replace(temp_dir, '').lstrip('/')
                
                # Create or update DBT resource
                resource = await self._create_or_update_dbt_resource(
                    db=db,
                    item=item_dict,
                    resource_type=resource_type,
                    data_source_id=data_source_id,
                    job_id=job_id,
                    columns=[col.dict() if hasattr(col, 'dict') else col for col in columns],
                    temp_dir=temp_dir
                )
                created_resources.append(resource)
        
        # Update the job with the resource count
        result = await db.execute(
            select(MetadataIndexingJob).where(MetadataIndexingJob.id == job_id)
        )
        job = result.scalar_one_or_none()
        if job:
            job.total_resources = len(created_resources)
            job.processed_resources = len(created_resources)
            await db.commit()
        
        return created_resources
    
    async def _create_or_update_dbt_resource(
        self,
        db: AsyncSession,
        item: Dict[str, Any],
        resource_type: str,
        data_source_id: str,
        job_id: str,
        columns: List[Dict[str, Any]] = None,
        temp_dir: str = None
    ):
        
        """Create or update a DBT resource"""
        # Extract source name for sources
        source_name = None
        if resource_type == 'sources' and '.' in item.get('name', ''):
            source_name = item['name'].split('.')[0]
        # Prepare resource data
        resource_data = DBTResourceCreate(
            name=item.get('name', ''),
            resource_type=resource_type.rstrip('s'),  # Convert plural to singular
            path=item.get('path', ''),
            description=item.get('description', ''),
            raw_data=item,  # item already has the cleaned path
            sql_content=item.get('sql_content', ''),
            source_name=source_name,
            database=item.get('database', ''),
            schema=item.get('schema', ''),
            columns=columns or [],
            depends_on=item.get('depends_on', []),
            is_active=True,
            data_source_id=data_source_id,
            metadata_indexing_job_id=job_id
        )
        
        # Check if resource already exists
        result = await db.execute(
            select(DBTResource).where(
                DBTResource.name == resource_data.name,
                DBTResource.resource_type == resource_data.resource_type,
                DBTResource.data_source_id == data_source_id
            )
        )
        existing_resource = result.scalar_one_or_none()
        
        if existing_resource:
            # Update existing resource
            for key, value in resource_data.dict(exclude_unset=True).items():
                setattr(existing_resource, key, value)
            existing_resource.last_synced_at = datetime.utcnow()
            await db.commit()
            return existing_resource
        else:
            # Create new resource
            dbt_resource = DBTResource(**resource_data.dict())
            dbt_resource.last_synced_at = datetime.utcnow()
            db.add(dbt_resource)
            await db.commit()
            return dbt_resource
    
    async def get_dbt_resources(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization,
        resource_type: Optional[str] = None,
        skip: int = 0,
        limit: int = 100
    ):
        """Get DBT resources for a data source"""
        # Verify data source exists
        await self._verify_data_source(db, data_source_id, organization)
        
        # Build query
        query = select(DBTResource).where(
            DBTResource.data_source_id == data_source_id,
            DBTResource.is_active == True
        )
        
        # Filter by resource type if provided
        if resource_type:
            query = query.where(DBTResource.resource_type == resource_type)
        
        # Get total count
        result = await db.execute(query)
        total = len(result.scalars().all())
        
        # Get paginated results
        result = await db.execute(
            query
            .order_by(DBTResource.name)
            .offset(skip)
            .limit(limit)
        )
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
        organization
    ):
        """Start indexing a Git repository in the background"""
        # Create a task that runs in the background
        asyncio.create_task(
            self._run_indexing_job(db, repository_id, repo_path, data_source_id, organization)
        )
        
        return {"status": "started", "message": "Indexing job started in background"}
    
    async def _run_indexing_job(
        self,
        db: AsyncSession,
        repository_id: str,
        repo_path: str,
        data_source_id: str,
        organization
    ):
        """Run the actual indexing job and update repository status when complete"""
        try:
            # Create a new session for this background task
            async_session = AsyncSession(bind=db.bind, expire_on_commit=False)
            
            try:
                # Perform the indexing work
                await self.start_indexing(
                    db=async_session, 
                    repo_id=repository_id, 
                    temp_dir=repo_path, 
                    data_source_id=data_source_id, 
                    organization=organization
                )
                
                # Get the specific repository first
                result = await async_session.execute(
                    select(GitRepository).where(
                        GitRepository.id == repository_id,
                        GitRepository.data_source_id == data_source_id,
                        GitRepository.organization_id == organization.id
                    )
                )
                repo = result.scalar_one_or_none()
                
                if repo:
                    repo.status = "completed"
                    repo.updated_at = datetime.utcnow()
                    await async_session.commit()

            finally:
                await async_session.close()
                
        except Exception as e:
            try:
                # Create a new session for error handling
                async_session = AsyncSession(bind=db.bind, expire_on_commit=False)
                try:
                    # Get the specific repository first
                    result = await async_session.execute(
                        select(GitRepository).where(
                            GitRepository.id == repository_id,
                            GitRepository.data_source_id == data_source_id,
                            GitRepository.organization_id == organization.id
                        )
                    )
                    repo = result.scalar_one_or_none()
                    
                    if repo:
                        repo.status = "failed"
                        repo.error_message = str(e)
                        repo.updated_at = datetime.utcnow()
                        await async_session.commit()
                finally:
                    await async_session.close()
            except Exception as inner_e:
                print(f"Error updating repository status: {str(e)}")
                print(f"Inner exception: {str(inner_e)}")
        finally:
            # Clean up the temporary directory
            try:
                shutil.rmtree(repo_path)
            except Exception as e:
                print(f"Error cleaning up temporary directory: {str(e)}")
