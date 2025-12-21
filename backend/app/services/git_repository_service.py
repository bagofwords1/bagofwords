import git
import tempfile
import os
import logging
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, and_
from datetime import datetime

from app.models.git_repository import GitRepository
from app.models.data_source import DataSource
from app.schemas.git_repository_schema import (
    GitRepositoryCreate,
    GitRepositoryUpdate,
    GitRepositorySchema,
)
from app.models.user import User
from app.models.organization import Organization
from app.services.metadata_indexing_job_service import MetadataIndexingJobService
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.metadata_resource import MetadataResource
from app.models.instruction import Instruction
from app.models.build_content import BuildContent
from app.models.instruction_version import InstructionVersion
from app.core.telemetry import telemetry
from urllib.parse import urlparse

class GitRepositoryService:

    def __init__(self):
        self.metadata_indexing_job_service = MetadataIndexingJobService()
        self.logger = logging.getLogger(__name__)

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

    async def _verify_repository(self, db: AsyncSession, repository_id: str, data_source_id: str, organization: Organization):
        """Verify repository exists and belongs to organization"""
        result = await db.execute(
            select(GitRepository).where(
                GitRepository.id == repository_id,
                GitRepository.data_source_id == data_source_id,
                GitRepository.organization_id == organization.id
            )
        )
        repository = result.scalar_one_or_none()
        if not repository:
            raise HTTPException(status_code=404, detail="Git repository not found")
        return repository
    
    async def get_git_repository(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization
    ):
        return await self._verify_data_source(db, data_source_id, organization)

    async def test_connection(
        self,
        db: AsyncSession,
        data_source_id: str,
        git_repo: GitRepositoryCreate,
        organization: Organization
    ):
        """Test Git repository connection using provided credentials"""
        await self._verify_data_source(db, data_source_id, organization)

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # Set up SSH command if SSH key is provided
                if git_repo.ssh_key:
                    # Create a persistent temporary directory
                    temp_dir = tempfile.mkdtemp()
                    try:
                        ssh_key_path = os.path.join(temp_dir, 'id_rsa')
                        
                        # Split the key into lines and write them with proper line endings
                        key_lines = git_repo.ssh_key.strip().split('\n')
                        with open(ssh_key_path, 'w') as f:
                            for line in key_lines:
                                f.write(line.strip() + '\n')
                        
                        # Set correct permissions (600)
                        os.chmod(ssh_key_path, 0o600)
                        
                        git_env = os.environ.copy()
                        git_env["GIT_SSH_COMMAND"] = f'ssh -i {ssh_key_path} -o StrictHostKeyChecking=no'
                        
                        # Validate key format
                        import subprocess
                        try:
                            subprocess.run(
                                ['ssh-keygen', '-y', '-f', ssh_key_path],
                                check=True,
                                capture_output=True,
                                text=True
                            )
                        except subprocess.CalledProcessError as e:
                            raise HTTPException(status_code=400, detail=f"Invalid SSH key format: {e.stderr}")
                        
                        # List remote refs and ensure the requested branch exists
                        remote_refs = git.cmd.Git().ls_remote(git_repo.repo_url, env=git_env)
                    finally:
                        # Clean up
                        import shutil
                        shutil.rmtree(temp_dir, ignore_errors=True)
                else:
                    # If no SSH key, use regular ls-remote
                    remote_refs = git.cmd.Git().ls_remote(git_repo.repo_url)

                # Verify that the configured branch exists in the remote refs
                branch_name = git_repo.branch or "main"
                expected_ref = f"refs/heads/{branch_name}"
                if expected_ref not in remote_refs:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Git branch '{branch_name}' not found in repository"
                    )

                return {"success": True, "message": "Connection successful"}

        except git.GitCommandError as e:
            raise HTTPException(status_code=400, detail=f"Git error: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")

    async def create_git_repository(
        self, 
        db: AsyncSession, 
        data_source_id: str, 
        git_repo: GitRepositoryCreate,
        current_user: User,
        organization: Organization
    ):
        """Create a new Git repository integration"""
        data_source = await self._verify_data_source(db, data_source_id, organization)

        # Test connection before creating
        connection_test = await self.test_connection(db, data_source_id, git_repo, organization)
        if not connection_test["success"]:
            raise HTTPException(
                status_code=400,
                detail=f"Git repository connection test failed: {connection_test['message']}"
            )
        git_repository = GitRepository(
            provider=git_repo.provider,
            repo_url=git_repo.repo_url,
            branch=git_repo.branch,
            user_id=current_user.id,
            organization_id=organization.id,
            data_source_id=data_source_id,
            status="pending",
            auto_publish=git_repo.auto_publish,
            default_load_mode=git_repo.default_load_mode,
        )
    
        if git_repo.ssh_key:
            git_repository.encrypt_ssh_key(git_repo.ssh_key)

        db.add(git_repository)
        await db.commit()
        await db.refresh(git_repository)

        # Telemetry: git repository created (minimal fields only)
        try:
            try:
                host = urlparse(git_repo.repo_url).hostname
            except Exception:
                host = None
            await telemetry.capture(
                "git_repository_created",
                {
                    "repository_id": str(git_repository.id),
                    "provider": git_repository.provider,
                    "branch": git_repository.branch,
                    "data_source_id": data_source_id,
                    "repo_host": host,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        await self.index_git_repository(db, git_repository.id, data_source_id, organization)

        return GitRepositorySchema.from_orm(git_repository)

    async def update_git_repository(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        git_repo: GitRepositoryUpdate,
        organization: Organization
    ):
        """Update an existing Git repository integration"""
        repository = await self._verify_repository(db, repository_id, data_source_id, organization)

        update_data = git_repo.dict(exclude_unset=True)
        
        if git_repo.ssh_key:
            repository.encrypt_ssh_key(git_repo.ssh_key)
            update_data.pop('ssh_key', None)

        if update_data:
            await db.execute(
                update(GitRepository)
                .where(GitRepository.id == repository_id)
                .values(**update_data)
            )
            await db.commit()
            await db.refresh(repository)

        return GitRepositorySchema.from_orm(repository)

    async def get_linked_instructions_count(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        organization: Organization
    ) -> dict:
        """Get the count of instructions linked to a git repository's resources.
        Only counts instructions that are still synced (source_sync_enabled=True)."""
        await self._verify_repository(db, repository_id, data_source_id, organization)

        # Find related indexing jobs
        indexing_jobs_result = await self.metadata_indexing_job_service.get_indexing_jobs(db, data_source_id, organization)
        metadata_indexing_jobs = indexing_jobs_result.get("items", []) if isinstance(indexing_jobs_result, dict) else []

        # Find resources linked to these jobs or the data source
        job_ids = [job.id for job in metadata_indexing_jobs]
        resources_stmt = select(MetadataResource).where(
            (MetadataResource.metadata_indexing_job_id.in_(job_ids)) |
            (MetadataResource.data_source_id == data_source_id)
        )
        resources_result = await db.execute(resources_stmt)
        resources = resources_result.scalars().all()

        resource_ids = [r.id for r in resources]
        instruction_count = 0

        if resource_ids:
            from sqlalchemy import func
            # Only count instructions that are still synced with git
            count_stmt = select(func.count(Instruction.id)).where(
                and_(
                    Instruction.source_metadata_resource_id.in_(resource_ids),
                    Instruction.source_sync_enabled == True
                )
            )
            count_result = await db.execute(count_stmt)
            instruction_count = count_result.scalar() or 0
        
        return {"instruction_count": instruction_count}

    async def delete_git_repository(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        organization: Organization
    ):
        """Delete a Git repository and associated indexing jobs and resources"""
        repository = await self._verify_repository(db, repository_id, data_source_id, organization)

        # 1. Find related indexing jobs
        indexing_jobs_result = await self.metadata_indexing_job_service.get_indexing_jobs(db, data_source_id, organization)
        # Assuming the result structure is {'items': [...], 'total': ...} based on the service method
        metadata_indexing_jobs = indexing_jobs_result.get("items", []) if isinstance(indexing_jobs_result, dict) else []

        # 2. Find resources linked to these jobs (or directly to the data source if jobs might be missing)
        job_ids = [job.id for job in metadata_indexing_jobs]
        
        # Find resources linked either to the jobs OR directly to the data source being deleted
        resources_to_delete_stmt = select(MetadataResource).where(
            (MetadataResource.metadata_indexing_job_id.in_(job_ids)) |
            (MetadataResource.data_source_id == data_source_id)
        )
        resources_result = await db.execute(resources_to_delete_stmt)
        resources_to_delete = resources_result.scalars().all()

        # Store IDs for later deletion (objects may detach after commits)
        resource_ids = [r.id for r in resources_to_delete]
        job_ids_to_delete = [job.id for job in metadata_indexing_jobs]
        
        # 3. Handle synced instructions linked to these resources (before deleting resources)
        # Only process instructions that are still synced (source_sync_enabled=True)
        # Unlinked instructions are kept as user-owned
        if resource_ids:
            # Find instructions that reference these resources AND are still synced
            instructions_stmt = select(Instruction).where(
                and_(
                    Instruction.source_metadata_resource_id.in_(resource_ids),
                    Instruction.source_sync_enabled == True
                )
            )
            instructions_result = await db.execute(instructions_stmt)
            instructions_to_delete = instructions_result.scalars().all()

            deleted_count = len(instructions_to_delete)
            instruction_ids_to_delete = [inst.id for inst in instructions_to_delete]

            # === Build System Integration ===
            # Create a new build that REMOVES these instructions
            # This preserves history - old builds still contain the instructions for diffing
            if instruction_ids_to_delete:
                try:
                    from app.services.build_service import BuildService
                    build_service = BuildService()
                    
                    # Get the organization from the data source
                    ds_result = await db.execute(
                        select(DataSource).where(DataSource.id == data_source_id)
                    )
                    data_source = ds_result.scalar_one_or_none()
                    
                    if data_source:
                        # Create a new build for this deletion
                        deletion_build = await build_service.get_or_create_draft_build(
                            db,
                            data_source.organization_id,
                            source='git',
                            user_id=None
                        )
                        
                        # Remove instructions from the new build (not from old builds!)
                        for instruction_id in instruction_ids_to_delete:
                            await build_service.remove_from_build(db, deletion_build.id, instruction_id)
                            self.logger.debug(f"Removed instruction {instruction_id} from build {deletion_build.id}")
                        
                        await db.commit()
                        
                        # Auto-finalize the build
                        await build_service.submit_build(db, deletion_build.id)
                        await build_service.approve_build(db, deletion_build.id, approved_by_user_id=None)
                        await build_service.promote_build(db, deletion_build.id)
                        
                        self.logger.info(f"Created deletion build {deletion_build.id} removing {len(instruction_ids_to_delete)} instructions")
                except Exception as build_error:
                    self.logger.warning(f"Failed to create deletion build: {build_error}")

            # Soft-delete instructions (set deleted_at) instead of hard delete
            # This preserves them in old builds for history/diffing
            from datetime import datetime
            for instruction in instructions_to_delete:
                instruction.deleted_at = datetime.utcnow()
                instruction.source_metadata_resource_id = None  # Unlink from resource
                self.logger.info(f"Soft-deleted instruction {instruction.id} (was linked to resource in data source {data_source_id})")

            if deleted_count > 0:
                self.logger.info(f"Soft-deleted {deleted_count} instructions linked to git repository {repository_id}")
            
            await db.commit()

        # 4. Delete resources (re-fetch to avoid detached instance issues)
        if resource_ids:
            resources_stmt = select(MetadataResource).where(MetadataResource.id.in_(resource_ids))
            resources_result = await db.execute(resources_stmt)
            resources = resources_result.scalars().all()
            for resource in resources:
                await db.delete(resource)
                self.logger.info(f"Deleting MetadataResource {resource.id} ({resource.name}) linked to data source {data_source_id}")
            await db.commit()

        # 5. Delete indexing jobs (re-fetch to avoid detached instance issues)
        if job_ids_to_delete:
            from app.models.metadata_indexing_job import MetadataIndexingJob
            jobs_stmt = select(MetadataIndexingJob).where(MetadataIndexingJob.id.in_(job_ids_to_delete))
            jobs_result = await db.execute(jobs_stmt)
            jobs = jobs_result.scalars().all()
            for job in jobs:
                await db.delete(job)
                self.logger.info(f"Deleting MetadataIndexingJob {job.id} linked to data source {data_source_id}")
            await db.commit()

        # 6. Delete the repository itself (re-fetch to avoid detached instance issues)
        repo_stmt = select(GitRepository).where(GitRepository.id == repository_id)
        repo_result = await db.execute(repo_stmt)
        repository = repo_result.scalar_one_or_none()
        if repository:
            await db.delete(repository)
            await db.commit()

        self.logger.info(f"Deleted GitRepository {repository_id} and associated data for data source {data_source_id}")
        return {"message": "Repository and associated data deleted successfully"}

    def _detect_project_types(self, repo_path: str) -> list[str]:
        """Detect known project types within the cloned repository path."""
        detected_types = []
        repo_root = Path(repo_path)
        
        # Check for DBT
        if (repo_root / 'dbt_project.yml').is_file():
            detected_types.append('dbt')
            self.logger.info(f"Detected DBT project in {repo_path}")

        # Check for LookML
        # Check for *.model.lkml or any *.lkml file as indicators
        if list(repo_root.glob('**/*.model.lkml')) or list(repo_root.glob('**/*.lkml')):
            detected_types.append('lookml')
            self.logger.info(f"Detected LookML project in {repo_path}")
        
        # Check for Markdown files
        # Check for any *.md file as an indicator
        if list(repo_root.glob('**/*.md')):
            detected_types.append('markdown')
            self.logger.info(f"Detected Markdown files in {repo_path}")
        
        # Check for Tableau TDS/TDSX files
        if list(repo_root.glob('**/*.tds')) or list(repo_root.glob('**/*.tdsx')):
            detected_types.append('tableau')
            self.logger.info(f"Detected Tableau datasource files in {repo_path}")
        
        # Check for Dataform projects (SQLX files)
        # Heuristic: presence of dataform.json or any *.sqlx file
        if (repo_root / 'dataform.json').is_file() or list(repo_root.glob('**/*.sqlx')):
            detected_types.append('dataform')
            self.logger.info(f"Detected Dataform project in {repo_path}")
        
        # Add checks for other types (e.g., Airflow) here
        # if (repo_root / 'dags').is_dir():
        #     detected_types.append('airflow')
        
        if not detected_types:
            self.logger.warning(
                f"No known project type (DBT, LookML, Markdown, Tableau, Dataform) detected in {repo_path}"
            )
        
        return detected_types

    async def index_git_repository(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        organization: Organization
    ):
        """Index/sync a Git repository"""
        repository = await self._verify_repository(db, repository_id, data_source_id, organization)

        try:
            # Create temp directory without context manager so it persists
            temp_dir = tempfile.mkdtemp()
            
            # Clone repo
            repo = await self.clone_git_repo(repository, temp_dir)
            
            # Detect project types (dbt, LookML, Markdown, Tableau, etc.)
            detected_types = self._detect_project_types(temp_dir)

            # Always create an indexing job, even if no project types are detected.
            # The background job will mark itself as completed with an appropriate
            # message in that case, and will also update the repository status.
            job = await self.metadata_indexing_job_service.start_indexing_background(
                db=db,
                repository_id=repository.id,
                repo_path=temp_dir,
                data_source_id=data_source_id,
                organization=organization,
                detected_project_types=detected_types # Pass detected types
            )

            # Update repository status
            repository.status = "indexing"
            await db.commit()
            await db.refresh(repository)

            return {"status": "success", "message": "Repository indexing started in background"}

        except Exception as e:
            repository.status = "failed"
            await db.commit()
            raise HTTPException(status_code=500, detail=f"Failed to index repository: {str(e)}")

    async def clone_git_repo(
        self,
        repository: GitRepository,
        clone_dir: str
    ):
        """Clone a git repository to a temporary directory and return the repo object"""
        try:
            # Set up SSH if needed
            if repository.ssh_key:
                # Create a temporary directory for SSH files
                ssh_dir = tempfile.mkdtemp()
                try:
                    ssh_key_path = os.path.join(ssh_dir, 'id_rsa')
                    ssh_key_data = repository.decrypt_ssh_key()
                    
                    # Split the key into lines and write them with proper line endings
                    key_lines = ssh_key_data.strip().split('\n')
                    with open(ssh_key_path, 'w') as f:
                        for line in key_lines:
                            f.write(line.strip() + '\n')
                    
                    os.chmod(ssh_key_path, 0o600)
                    
                    # Set up Git environment with SSH command
                    git_env = os.environ.copy()
                    git_env["GIT_SSH_COMMAND"] = f'ssh -i {ssh_key_path} -o StrictHostKeyChecking=no'
                    
                    # Validate key format
                    import subprocess
                    try:
                        subprocess.run(
                            ['ssh-keygen', '-y', '-f', ssh_key_path],
                            check=True,
                            capture_output=True,
                            text=True
                        )
                    except subprocess.CalledProcessError as e:
                        raise HTTPException(status_code=400, detail=f"Invalid SSH key format: {e.stderr}")
                    
                    # Clone repository with depth=1 for shallow clone
                    repo = git.Repo.clone_from(
                        repository.repo_url,
                        clone_dir,
                        branch=repository.branch,
                        depth=1,
                        env=git_env,
                        multi_options=["--single-branch", "--no-tags"]
                    )
                finally:
                    # Clean up SSH directory
                    import shutil
                    shutil.rmtree(ssh_dir, ignore_errors=True)
            else:
                # Clone without SSH key
                repo = git.Repo.clone_from(
                    repository.repo_url,
                    clone_dir,
                    branch=repository.branch,
                    depth=1,
                    multi_options=["--single-branch", "--no-tags"]
                )
            
            return repo
        except git.GitCommandError as e:
            raise HTTPException(status_code=500, detail=f"Failed to clone repository: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to clone repository: {str(e)}")

    async def get_indexing_job_status(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        organization: Organization
    ):
        """Get current indexing job status with progress percentage"""
        # Verify repository exists
        await self._verify_repository(db, repository_id, data_source_id, organization)
        
        # Get the latest indexing job for this repository
        result = await db.execute(
            select(MetadataIndexingJob)
            .where(MetadataIndexingJob.git_repository_id == repository_id)
            .order_by(MetadataIndexingJob.created_at.desc())
            .limit(1)
        )
        job = result.scalar_one_or_none()
        
        if not job:
            return {"status": "none", "progress": 0}
        
        # Calculate progress percentage
        progress = 0
        if job.total_files and job.total_files > 0:
            progress = int((job.processed_files or 0) / job.total_files * 100)
        
        return {
            "status": job.status,
            "phase": job.current_phase,
            "progress": progress,
            "processed_files": job.processed_files or 0,
            "total_files": job.total_files or 0,
            "error_message": job.error_message,
        }