import git
import tempfile
import os
import logging
from pathlib import Path
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from datetime import datetime

from app.models.git_repository import GitRepository
from app.models.data_source import DataSource
from app.schemas.git_repository_schema import GitRepositoryCreate, GitRepositoryUpdate
from app.models.user import User
from app.models.organization import Organization
from app.services.metadata_indexing_job_service import MetadataIndexingJobService
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.metadata_resource import MetadataResource

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
                        
                        git.cmd.Git().ls_remote(git_repo.repo_url, env=git_env)
                    finally:
                        # Clean up
                        import shutil
                        shutil.rmtree(temp_dir, ignore_errors=True)
                else:
                    # If no SSH key, use regular ls-remote
                    git.cmd.Git().ls_remote(git_repo.repo_url)

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
            status="pending"
        )
    
        if git_repo.ssh_key:
            git_repository.encrypt_ssh_key(git_repo.ssh_key)

        db.add(git_repository)
        await db.commit()
        await db.refresh(git_repository)

        await self.index_git_repository(db, git_repository.id, data_source_id, organization)

        return git_repository

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

        return repository

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

        # 3. Delete resources
        for resource in resources_to_delete:
            await db.delete(resource)
            self.logger.info(f"Deleting MetadataResource {resource.id} ({resource.name}) linked to data source {data_source_id}")

        # 4. Delete indexing jobs
        for job in metadata_indexing_jobs:
             await db.delete(job)
             self.logger.info(f"Deleting MetadataIndexingJob {job.id} linked to data source {data_source_id}")

        # 5. Delete the repository itself
        await db.delete(repository)
        await db.commit() # Commit all deletions

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
        
        # Add checks for other types (e.g., Airflow) here
        # if (repo_root / 'dags').is_dir():
        #     detected_types.append('airflow')

        if not detected_types:
             self.logger.warning(f"No known project type (DBT, LookML, Markdown) detected in {repo_path}")

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
            
            # Detect project types
            detected_types = self._detect_project_types(temp_dir)

            if not detected_types:
                # Handle case where nothing is detected - maybe skip indexing or log prominently
                self.logger.warning(f"No project types detected in repository {repository_id}, skipping indexing job creation.")
                # Optionally update repo status to 'completed' or 'empty'?
                # repository.status = "completed" # Or a new status like 'no_projects_found'
                # await db.commit()
                # Clean up temp dir if no job is started
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)
                # Return a specific message
                return {"status": "skipped", "message": "No known project types (DBT, LookML) found in the repository."}

            # Start index in background, passing detected types
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
                        env=git_env
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
                    depth=1
                )
            
            return repo
        except git.GitCommandError as e:
            raise HTTPException(status_code=500, detail=f"Failed to clone repository: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to clone repository: {str(e)}")
