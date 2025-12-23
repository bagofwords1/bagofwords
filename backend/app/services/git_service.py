"""
GitService - Consolidated Git operations for BOW

This service handles all Git-related operations:
- Repository CRUD (create, read, update, delete)
- Connection testing
- Cloning and indexing
- Sync from Git (pull changes from a branch)
- Push to Git (write build contents to a new branch)
- PR creation (GitHub, GitLab, Bitbucket Cloud/Server)
"""

import git
import tempfile
import os
import logging
import shutil
import re
import httpx
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, and_
from urllib.parse import urlparse

from app.models.git_repository import GitRepository
from app.models.data_source import DataSource
from app.models.instruction_build import InstructionBuild
from app.models.organization import Organization
from app.models.user import User
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.metadata_resource import MetadataResource
from app.models.instruction import Instruction
from app.schemas.git_repository_schema import (
    GitRepositoryCreate,
    GitRepositoryUpdate,
    GitRepositorySchema,
)
from app.core.telemetry import telemetry


logger = logging.getLogger(__name__)


class GitService:
    """Consolidated service for all Git operations."""

    def __init__(self):
        from app.services.metadata_indexing_job_service import MetadataIndexingJobService
        self.metadata_indexing_job_service = MetadataIndexingJobService()

    # ==================== Repository CRUD ====================

    async def _verify_data_source(
        self, db: AsyncSession, data_source_id: str, organization: Organization
    ) -> DataSource:
        """Verify data source exists and belongs to organization."""
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

    async def _verify_repository(
        self, db: AsyncSession, repository_id: str, data_source_id: str, organization: Organization
    ) -> GitRepository:
        """Verify repository exists and belongs to organization."""
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

    async def get_repository_by_id(
        self, db: AsyncSession, repository_id: str
    ) -> Optional[GitRepository]:
        """Get a repository by ID without verification."""
        result = await db.execute(
            select(GitRepository).where(GitRepository.id == repository_id)
        )
        return result.scalar_one_or_none()

    async def get_git_repository(
        self,
        db: AsyncSession,
        data_source_id: str,
        organization: Organization
    ) -> DataSource:
        """Get git repository for a data source."""
        return await self._verify_data_source(db, data_source_id, organization)

    async def test_connection(
        self,
        db: AsyncSession,
        data_source_id: str,
        git_repo: GitRepositoryCreate,
        organization: Organization
    ) -> Dict[str, Any]:
        """Test Git repository connection using provided credentials."""
        await self._verify_data_source(db, data_source_id, organization)

        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # Use PAT for HTTPS or SSH key for SSH
                if git_repo.access_token:
                    remote_refs = await self._ls_remote_with_pat(
                        git_repo.repo_url,
                        git_repo.access_token,
                        git_repo.access_token_username
                    )
                elif git_repo.ssh_key:
                    remote_refs = await self._ls_remote_with_ssh(
                        git_repo.repo_url, git_repo.ssh_key
                    )
                else:
                    # Public repo - no auth
                    remote_refs = git.cmd.Git().ls_remote(git_repo.repo_url)

                # Verify that the configured branch exists
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
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Connection failed: {str(e)}")

    async def _ls_remote_with_ssh(self, repo_url: str, ssh_key: str) -> str:
        """List remote refs using SSH key."""
        temp_dir = tempfile.mkdtemp()
        try:
            ssh_key_path = os.path.join(temp_dir, 'id_rsa')
            
            # Write SSH key with proper line endings
            key_lines = ssh_key.strip().split('\n')
            with open(ssh_key_path, 'w') as f:
                for line in key_lines:
                    f.write(line.strip() + '\n')
            
            os.chmod(ssh_key_path, 0o600)
            
            # Validate key format
            import subprocess
            try:
                subprocess.run(
                    ['ssh-keygen', '-y', '-f', ssh_key_path],
                    check=True, capture_output=True, text=True
                )
            except subprocess.CalledProcessError as e:
                raise HTTPException(status_code=400, detail=f"Invalid SSH key format: {e.stderr}")
            
            git_env = os.environ.copy()
            git_env["GIT_SSH_COMMAND"] = f'ssh -i {ssh_key_path} -o StrictHostKeyChecking=no'
            
            return git.cmd.Git().ls_remote(repo_url, env=git_env)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _ls_remote_with_pat(
        self, repo_url: str, access_token: str, username: Optional[str] = None
    ) -> str:
        """List remote refs using Personal Access Token (HTTPS)."""
        # Convert SSH URL to HTTPS if needed
        https_url = self._convert_to_https_url(repo_url, access_token, username)
        return git.cmd.Git().ls_remote(https_url)

    def _convert_to_https_url(
        self, repo_url: str, access_token: str, username: Optional[str] = None
    ) -> str:
        """Convert repo URL to HTTPS URL with embedded credentials."""
        # If already HTTPS, inject credentials
        if repo_url.startswith('https://'):
            parsed = urlparse(repo_url)
            if username:
                # Bitbucket Cloud: username:app_password@
                auth = f"{username}:{access_token}"
            else:
                # GitHub/GitLab: token@
                auth = access_token
            return f"https://{auth}@{parsed.netloc}{parsed.path}"
        
        # Convert SSH URL to HTTPS
        # git@github.com:user/repo.git -> https://github.com/user/repo.git
        ssh_match = re.match(r'git@([^:]+):(.+)', repo_url)
        if ssh_match:
            host = ssh_match.group(1)
            path = ssh_match.group(2)
            if username:
                auth = f"{username}:{access_token}"
            else:
                auth = access_token
            return f"https://{auth}@{host}/{path}"
        
        raise ValueError(f"Unsupported URL format: {repo_url}")

    async def create_git_repository(
        self,
        db: AsyncSession,
        data_source_id: str,
        git_repo: GitRepositoryCreate,
        current_user: User,
        organization: Organization
    ) -> GitRepositorySchema:
        """Create a new Git repository integration."""
        await self._verify_data_source(db, data_source_id, organization)

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
            custom_host=git_repo.custom_host,
            write_enabled=git_repo.write_enabled,
            access_token_username=git_repo.access_token_username,
        )

        if git_repo.ssh_key:
            git_repository.encrypt_ssh_key(git_repo.ssh_key)

        if git_repo.access_token:
            git_repository.encrypt_access_token(git_repo.access_token)

        db.add(git_repository)
        await db.commit()
        await db.refresh(git_repository)

        # Telemetry
        try:
            host = urlparse(git_repo.repo_url).hostname
        except Exception:
            host = None
        
        try:
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

        return GitRepositorySchema.from_orm_with_capabilities(git_repository)

    async def update_git_repository(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        git_repo: GitRepositoryUpdate,
        organization: Organization
    ) -> GitRepositorySchema:
        """Update an existing Git repository integration."""
        repository = await self._verify_repository(db, repository_id, data_source_id, organization)

        update_data = git_repo.dict(exclude_unset=True)

        # Handle encrypted fields separately
        if git_repo.ssh_key:
            repository.encrypt_ssh_key(git_repo.ssh_key)
            update_data.pop('ssh_key', None)

        if git_repo.access_token:
            repository.encrypt_access_token(git_repo.access_token)
            update_data.pop('access_token', None)

        if update_data:
            await db.execute(
                update(GitRepository)
                .where(GitRepository.id == repository_id)
                .values(**update_data)
            )
            await db.commit()
            await db.refresh(repository)

        return GitRepositorySchema.from_orm_with_capabilities(repository)

    async def get_linked_instructions_count(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        organization: Organization
    ) -> Dict[str, int]:
        """Get the count of instructions linked to a git repository's resources."""
        await self._verify_repository(db, repository_id, data_source_id, organization)

        indexing_jobs_result = await self.metadata_indexing_job_service.get_indexing_jobs(
            db, data_source_id, organization
        )
        metadata_indexing_jobs = indexing_jobs_result.get("items", []) if isinstance(indexing_jobs_result, dict) else []

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
    ) -> Dict[str, str]:
        """Delete a Git repository and associated indexing jobs and resources."""
        repository = await self._verify_repository(db, repository_id, data_source_id, organization)

        # Find related indexing jobs
        indexing_jobs_result = await self.metadata_indexing_job_service.get_indexing_jobs(
            db, data_source_id, organization
        )
        metadata_indexing_jobs = indexing_jobs_result.get("items", []) if isinstance(indexing_jobs_result, dict) else []

        job_ids = [job.id for job in metadata_indexing_jobs]

        # Find resources
        resources_to_delete_stmt = select(MetadataResource).where(
            (MetadataResource.metadata_indexing_job_id.in_(job_ids)) |
            (MetadataResource.data_source_id == data_source_id)
        )
        resources_result = await db.execute(resources_to_delete_stmt)
        resources_to_delete = resources_result.scalars().all()

        resource_ids = [r.id for r in resources_to_delete]
        job_ids_to_delete = [job.id for job in metadata_indexing_jobs]

        # Handle synced instructions
        if resource_ids:
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

            # Create deletion build
            if instruction_ids_to_delete:
                try:
                    from app.services.build_service import BuildService
                    build_service = BuildService()

                    ds_result = await db.execute(
                        select(DataSource).where(DataSource.id == data_source_id)
                    )
                    data_source = ds_result.scalar_one_or_none()

                    if data_source:
                        deletion_build = await build_service.get_or_create_draft_build(
                            db, data_source.organization_id, source='git', user_id=None
                        )
                        for instruction_id in instruction_ids_to_delete:
                            await build_service.remove_from_build(db, deletion_build.id, instruction_id)
                        await db.commit()
                        await build_service.submit_build(db, deletion_build.id)
                        await build_service.approve_build(db, deletion_build.id, approved_by_user_id=None)
                        await build_service.promote_build(db, deletion_build.id)
                        logger.info(f"Created deletion build {deletion_build.id}")
                except Exception as build_error:
                    logger.warning(f"Failed to create deletion build: {build_error}")

            # Soft-delete instructions
            for instruction in instructions_to_delete:
                instruction.deleted_at = datetime.utcnow()
                instruction.source_metadata_resource_id = None
                logger.info(f"Soft-deleted instruction {instruction.id}")

            if deleted_count > 0:
                logger.info(f"Soft-deleted {deleted_count} instructions")

            await db.commit()

        # Delete resources
        if resource_ids:
            resources_stmt = select(MetadataResource).where(MetadataResource.id.in_(resource_ids))
            resources_result = await db.execute(resources_stmt)
            resources = resources_result.scalars().all()
            for resource in resources:
                await db.delete(resource)
            await db.commit()

        # Delete indexing jobs
        if job_ids_to_delete:
            jobs_stmt = select(MetadataIndexingJob).where(MetadataIndexingJob.id.in_(job_ids_to_delete))
            jobs_result = await db.execute(jobs_stmt)
            jobs = jobs_result.scalars().all()
            for job in jobs:
                await db.delete(job)
            await db.commit()

        # Delete repository
        repo_stmt = select(GitRepository).where(GitRepository.id == repository_id)
        repo_result = await db.execute(repo_stmt)
        repository = repo_result.scalar_one_or_none()
        if repository:
            await db.delete(repository)
            await db.commit()

        logger.info(f"Deleted GitRepository {repository_id}")
        return {"message": "Repository and associated data deleted successfully"}

    # ==================== Cloning and Indexing ====================

    def _detect_project_types(self, repo_path: str) -> List[str]:
        """Detect known project types within the cloned repository path."""
        detected_types = []
        repo_root = Path(repo_path)

        if (repo_root / 'dbt_project.yml').is_file():
            detected_types.append('dbt')

        if list(repo_root.glob('**/*.model.lkml')) or list(repo_root.glob('**/*.lkml')):
            detected_types.append('lookml')

        if list(repo_root.glob('**/*.md')):
            detected_types.append('markdown')

        if list(repo_root.glob('**/*.tds')) or list(repo_root.glob('**/*.tdsx')):
            detected_types.append('tableau')

        if (repo_root / 'dataform.json').is_file() or list(repo_root.glob('**/*.sqlx')):
            detected_types.append('dataform')

        if not detected_types:
            logger.warning(f"No known project type detected in {repo_path}")

        return detected_types

    async def index_git_repository(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        organization: Organization
    ) -> Dict[str, str]:
        """Index/sync a Git repository."""
        repository = await self._verify_repository(db, repository_id, data_source_id, organization)

        try:
            temp_dir = tempfile.mkdtemp()
            repo = await self.clone_repository(repository, temp_dir)
            detected_types = self._detect_project_types(temp_dir)

            job = await self.metadata_indexing_job_service.start_indexing_background(
                db=db,
                repository_id=repository.id,
                repo_path=temp_dir,
                data_source_id=data_source_id,
                organization=organization,
                detected_project_types=detected_types
            )

            repository.status = "indexing"
            await db.commit()
            await db.refresh(repository)

            return {"status": "success", "message": "Repository indexing started in background"}

        except Exception as e:
            repository.status = "failed"
            await db.commit()
            raise HTTPException(status_code=500, detail=f"Failed to index repository: {str(e)}")

    async def clone_repository(
        self,
        repository: GitRepository,
        clone_dir: str,
        branch: Optional[str] = None
    ) -> git.Repo:
        """
        Clone a git repository to a directory.
        
        Args:
            repository: GitRepository model with credentials
            clone_dir: Directory to clone into
            branch: Optional branch to clone (defaults to repository.branch)
        """
        target_branch = branch or repository.branch
        
        try:
            if repository.has_access_token:
                # Clone via HTTPS with PAT
                pat = repository.decrypt_access_token()
                https_url = self._convert_to_https_url(
                    repository.repo_url, pat, repository.access_token_username
                )
                repo = git.Repo.clone_from(
                    https_url,
                    clone_dir,
                    branch=target_branch,
                    depth=1,
                    multi_options=["--single-branch", "--no-tags"]
                )
            elif repository.has_ssh_key:
                # Clone via SSH
                ssh_dir = tempfile.mkdtemp()
                try:
                    ssh_key_path = os.path.join(ssh_dir, 'id_rsa')
                    ssh_key_data = repository.decrypt_ssh_key()

                    key_lines = ssh_key_data.strip().split('\n')
                    with open(ssh_key_path, 'w') as f:
                        for line in key_lines:
                            f.write(line.strip() + '\n')

                    os.chmod(ssh_key_path, 0o600)

                    git_env = os.environ.copy()
                    git_env["GIT_SSH_COMMAND"] = f'ssh -i {ssh_key_path} -o StrictHostKeyChecking=no'

                    repo = git.Repo.clone_from(
                        repository.repo_url,
                        clone_dir,
                        branch=target_branch,
                        depth=1,
                        env=git_env,
                        multi_options=["--single-branch", "--no-tags"]
                    )
                finally:
                    shutil.rmtree(ssh_dir, ignore_errors=True)
            else:
                # Public repo - no auth
                repo = git.Repo.clone_from(
                    repository.repo_url,
                    clone_dir,
                    branch=target_branch,
                    depth=1,
                    multi_options=["--single-branch", "--no-tags"]
                )

            return repo
        except git.GitCommandError as e:
            raise HTTPException(status_code=500, detail=f"Failed to clone repository: {str(e)}")

    async def get_indexing_job_status(
        self,
        db: AsyncSession,
        repository_id: str,
        data_source_id: str,
        organization: Organization
    ) -> Dict[str, Any]:
        """Get current indexing job status with progress percentage."""
        await self._verify_repository(db, repository_id, data_source_id, organization)

        result = await db.execute(
            select(MetadataIndexingJob)
            .where(MetadataIndexingJob.git_repository_id == repository_id)
            .order_by(MetadataIndexingJob.created_at.desc())
            .limit(1)
        )
        job = result.scalar_one_or_none()

        if not job:
            return {"status": "none", "progress": 0}

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

    # ==================== Sync Branch (Git -> BOW) ====================

    async def sync_branch(
        self,
        db: AsyncSession,
        repository_id: str,
        branch: str,
        organization: Organization,
        user_id: Optional[str] = None
    ) -> InstructionBuild:
        """
        Sync a specific branch from Git to BOW.
        Creates a DRAFT build with the contents of that branch.
        
        Use case: User creates a feature branch in Git, creates a PR,
        then triggers this to create a BOW build for testing.
        
        Args:
            repository_id: The git repository ID
            branch: The branch name to sync (e.g., "feature/new-metric")
            organization: The organization
            user_id: Optional user triggering the sync
            
        Returns:
            The created draft InstructionBuild
        """
        # Get repository
        result = await db.execute(
            select(GitRepository).where(
                GitRepository.id == repository_id,
                GitRepository.organization_id == organization.id
            )
        )
        repository = result.scalar_one_or_none()
        if not repository:
            raise HTTPException(status_code=404, detail="Git repository not found")

        try:
            # Clone the specific branch
            temp_dir = tempfile.mkdtemp()
            logger.info(f"Syncing branch '{branch}' from repository {repository_id}")

            repo = await self.clone_repository(repository, temp_dir, branch=branch)
            commit_sha = repo.head.commit.hexsha

            # Detect project types
            detected_types = self._detect_project_types(temp_dir)

            # Create a draft build for this branch sync
            from app.services.build_service import BuildService
            build_service = BuildService()

            build = await build_service.create_build(
                db=db,
                org_id=organization.id,
                source='git',
                user_id=user_id,
                commit_sha=commit_sha,
                branch=branch,
                copy_from_main=False,  # Start fresh from the branch
            )

            logger.info(f"Created draft build {build.id} for branch '{branch}'")

            # Start indexing in background with this build
            await self.metadata_indexing_job_service.start_indexing_background(
                db=db,
                repository_id=repository.id,
                repo_path=temp_dir,
                data_source_id=repository.data_source_id,
                organization=organization,
                detected_project_types=detected_types,
                build_id=build.id,
            )

            return build

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to sync branch '{branch}': {e}")
            raise HTTPException(status_code=500, detail=f"Failed to sync branch: {str(e)}")

    # ==================== Push Build (BOW -> Git) ====================

    async def push_build(
        self,
        db: AsyncSession,
        build_id: str,
        repository_id: str,
        organization: Organization,
        user_id: Optional[str] = None,
        create_pr: bool = False,
    ) -> Dict[str, Any]:
        """
        Push a BOW build to a new Git branch.
        Optionally creates a PR.
        
        Args:
            build_id: The build to push
            repository_id: The git repository to push to
            organization: The organization
            user_id: Optional user triggering the push
            create_pr: If True, also create a PR (requires PAT)
            
        Returns:
            Dict with branch_name, pushed (bool), pr_url (if created)
        """
        # Get repository
        result = await db.execute(
            select(GitRepository).where(
                GitRepository.id == repository_id,
                GitRepository.organization_id == organization.id
            )
        )
        repository = result.scalar_one_or_none()
        if not repository:
            raise HTTPException(status_code=404, detail="Git repository not found")

        if not repository.can_push:
            raise HTTPException(
                status_code=400,
                detail="Repository is not configured for write operations. Add SSH key or PAT."
            )

        # Get build
        from app.services.build_service import BuildService
        build_service = BuildService()
        build = await build_service.get_build(db, build_id)
        
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if build.organization_id != organization.id:
            raise HTTPException(status_code=403, detail="Build does not belong to this organization")

        # Generate branch name: BOW-<build_number>
        branch_name = f"BOW-{build.build_number}"
        
        try:
            temp_dir = tempfile.mkdtemp()
            logger.info(f"Pushing build {build_id} to branch '{branch_name}'")

            # Clone the default branch (full clone for pushing)
            repo = await self._clone_for_push(repository, temp_dir)

            # Create new branch
            repo.git.checkout('-b', branch_name)

            # Write build contents to files
            await self._write_build_to_repo(db, build, temp_dir)

            # Commit changes
            repo.git.add('-A')
            
            # Check if there are changes to commit
            if repo.is_dirty() or repo.untracked_files:
                commit_message = f"BOW Build #{build.build_number}"
                repo.index.commit(commit_message)
                
                # Push the branch
                await self._push_branch(repository, repo, branch_name)
                
                # Update build with git info
                build.git_branch_name = branch_name
                build.git_pushed_at = datetime.utcnow()
                
                pr_url = None
                if create_pr and repository.can_create_pr:
                    pr_url = await self.create_pr(
                        repository=repository,
                        source_branch=branch_name,
                        target_branch=repository.branch or "main",
                        title=f"BOW Build #{build.build_number}",
                        description=f"Automated PR from Bag of Words build #{build.build_number}",
                    )
                    build.git_pr_url = pr_url
                
                await db.commit()
                await db.refresh(build)
                
                logger.info(f"Pushed build {build_id} to branch '{branch_name}', PR: {pr_url}")
                
                return {
                    "branch_name": branch_name,
                    "pushed": True,
                    "pr_url": pr_url,
                    "build_id": build_id,
                }
            else:
                return {
                    "branch_name": branch_name,
                    "pushed": False,
                    "message": "No changes to push",
                    "build_id": build_id,
                }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Failed to push build {build_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to push build: {str(e)}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    async def _clone_for_push(self, repository: GitRepository, clone_dir: str) -> git.Repo:
        """Clone repository for push operations (full clone, not shallow)."""
        target_branch = repository.branch or "main"
        
        if repository.has_access_token:
            pat = repository.decrypt_access_token()
            https_url = self._convert_to_https_url(
                repository.repo_url, pat, repository.access_token_username
            )
            return git.Repo.clone_from(https_url, clone_dir, branch=target_branch)
        elif repository.has_ssh_key:
            ssh_dir = tempfile.mkdtemp()
            try:
                ssh_key_path = os.path.join(ssh_dir, 'id_rsa')
                ssh_key_data = repository.decrypt_ssh_key()

                key_lines = ssh_key_data.strip().split('\n')
                with open(ssh_key_path, 'w') as f:
                    for line in key_lines:
                        f.write(line.strip() + '\n')

                os.chmod(ssh_key_path, 0o600)

                git_env = os.environ.copy()
                git_env["GIT_SSH_COMMAND"] = f'ssh -i {ssh_key_path} -o StrictHostKeyChecking=no'

                return git.Repo.clone_from(
                    repository.repo_url, clone_dir, branch=target_branch, env=git_env
                )
            finally:
                shutil.rmtree(ssh_dir, ignore_errors=True)
        else:
            raise HTTPException(status_code=400, detail="No credentials configured for push")

    async def _push_branch(self, repository: GitRepository, repo: git.Repo, branch_name: str):
        """Push a branch to the remote.
        
        Uses --force to allow updating existing branches. This is safe because:
        - Branch names are unique per build (BOW-{build_number})
        - Re-pushing a build should update the branch with latest changes
        - These are feature branches, not protected branches
        """
        if repository.has_access_token:
            # PAT is already embedded in the remote URL from clone
            repo.git.push('origin', branch_name, '--set-upstream', '--force')
        elif repository.has_ssh_key:
            ssh_dir = tempfile.mkdtemp()
            try:
                ssh_key_path = os.path.join(ssh_dir, 'id_rsa')
                ssh_key_data = repository.decrypt_ssh_key()

                key_lines = ssh_key_data.strip().split('\n')
                with open(ssh_key_path, 'w') as f:
                    for line in key_lines:
                        f.write(line.strip() + '\n')

                os.chmod(ssh_key_path, 0o600)

                git_env = os.environ.copy()
                git_env["GIT_SSH_COMMAND"] = f'ssh -i {ssh_key_path} -o StrictHostKeyChecking=no'

                repo.git.push('origin', branch_name, '--set-upstream', '--force', env=git_env)
            finally:
                shutil.rmtree(ssh_dir, ignore_errors=True)

    async def _resolve_reference_names(
        self, db: AsyncSession, references_json: list
    ) -> list[str]:
        """
        Resolve references to simple table names (e.g., "public.payment").
        Preserves original order from references_json.
        """
        from app.models.datasource_table import DataSourceTable
        
        # Collect IDs that need lookup
        table_ids_to_lookup = []
        for ref in references_json:
            if isinstance(ref, dict):
                if not ref.get('display_text') and ref.get('object_type') == 'datasource_table' and ref.get('object_id'):
                    table_ids_to_lookup.append(ref['object_id'])
        
        # Batch lookup and build id->name map
        table_id_to_name = {}
        if table_ids_to_lookup:
            stmt = select(DataSourceTable.id, DataSourceTable.name).where(
                DataSourceTable.id.in_(table_ids_to_lookup)
            )
            result = await db.execute(stmt)
            table_id_to_name = {row.id: row.name for row in result}
        
        # Build output in original order
        simple_refs = []
        for ref in references_json:
            if isinstance(ref, dict):
                display = ref.get('display_text')
                if display:
                    simple_refs.append(display)
                elif ref.get('object_type') == 'datasource_table' and ref.get('object_id'):
                    name = table_id_to_name.get(ref['object_id'])
                    if name:
                        simple_refs.append(name)
            elif isinstance(ref, str):
                simple_refs.append(ref)
        
        return simple_refs

    async def _write_build_to_repo(
        self, db: AsyncSession, build: InstructionBuild, repo_path: str
    ):
        """
        Write build contents to the repository directory as markdown files.
        
        Path logic:
        - Linked git instructions (source_sync_enabled=True): SKIP, already in Git
        - Unlinked git instructions (source_sync_enabled=False): Write to original source_file_path
        - User-created instructions (no source_file_path): Write to bow/{category}/{filename}.md
        """
        from app.services.build_service import BuildService
        
        build_service = BuildService()
        contents = await build_service.get_build_contents(db, build.id)

        written_count = 0
        skipped_linked = 0
        skipped_no_version = 0

        for content in contents:
            version = content.instruction_version
            instruction = content.instruction
            
            if not version:
                skipped_no_version += 1
                continue

            # RULE 1: Skip linked git instructions - they're already in Git
            if instruction.source_type == 'git' and instruction.source_sync_enabled:
                logger.debug(f"Skipping linked git instruction: {instruction.source_file_path}")
                skipped_linked += 1
                continue
            
            # RULE 2: Unlinked git instruction - write to original path (preserve extension)
            if instruction.source_file_path:
                filepath = Path(repo_path) / instruction.source_file_path
                filepath.parent.mkdir(parents=True, exist_ok=True)
            else:
                # RULE 3: User-created instruction - write to bow/{category}/{filename}.md
                category = instruction.category or 'general'
                
                # Map category names to directory names
                category_dir_map = {
                    'code_gen': 'code',
                    'data_modeling': 'data-modeling',
                    'visualizations': 'visualizations',
                    'dashboard': 'dashboard',
                    'system': 'system',
                    'general': 'general',
                }
                safe_category = category_dir_map.get(category, category)
                # Fallback sanitization for unknown categories
                safe_category = re.sub(r'[^\w\s-]', '', safe_category).strip().replace(' ', '-').lower()
                if not safe_category:
                    safe_category = 'general'
                
                bow_dir = Path(repo_path) / "bow" / safe_category
                bow_dir.mkdir(parents=True, exist_ok=True)
                
                title = version.title or instruction.title or str(instruction.id)
                safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '-').lower()
                if not safe_title:
                    safe_title = str(instruction.id)
                
                filename = f"{safe_title}.md"
                filepath = bow_dir / filename

            # Only write references to frontmatter (if present)
            # status, load_mode, category are BOW-only metadata - not written to git
            # This avoids noisy diffs and lets git be the source of truth for content
            simple_refs = []
            if version.references_json and isinstance(version.references_json, list):
                simple_refs = await self._resolve_reference_names(db, version.references_json)

            # Write file
            with open(filepath, 'w') as f:
                # Only write frontmatter block if there are references
                if simple_refs:
                    f.write("---\n")
                    f.write("references:\n")
                    for ref in simple_refs:
                        f.write(f"  - {ref}\n")
                    f.write("---\n\n")
                
                if version.title:
                    f.write(f"# {version.title}\n\n")
                f.write(version.text or "")

            written_count += 1
            logger.debug(f"Wrote instruction to {filepath}")
        
        logger.info(f"Push to git: wrote {written_count} file(s), skipped {skipped_linked} linked, {skipped_no_version} without version")

    # ==================== PR Creation ====================

    async def create_pr(
        self,
        repository: GitRepository,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str = "",
    ) -> Optional[str]:
        """
        Create a Pull Request on the Git provider.
        
        Returns the PR URL if successful, None otherwise.
        """
        if not repository.can_create_pr:
            logger.warning("Cannot create PR - no access token configured")
            return None

        provider = repository.provider.lower()
        pat = repository.decrypt_access_token()

        try:
            if provider == 'github':
                return await self._create_github_pr(
                    repository, pat, source_branch, target_branch, title, description
                )
            elif provider == 'gitlab':
                return await self._create_gitlab_pr(
                    repository, pat, source_branch, target_branch, title, description
                )
            elif provider == 'bitbucket':
                return await self._create_bitbucket_pr(
                    repository, pat, source_branch, target_branch, title, description
                )
            else:
                logger.warning(f"PR creation not supported for provider: {provider}")
                return None
        except Exception as e:
            logger.error(f"Failed to create PR: {e}")
            return None

    def _extract_repo_info(self, repo_url: str) -> Tuple[str, str, str]:
        """
        Extract host, owner, and repo name from a git URL.
        
        Returns: (host, owner, repo_name)
        """
        # Handle SSH URLs: git@github.com:owner/repo.git
        ssh_match = re.match(r'git@([^:]+):([^/]+)/(.+?)(?:\.git)?$', repo_url)
        if ssh_match:
            return ssh_match.group(1), ssh_match.group(2), ssh_match.group(3)
        
        # Handle HTTPS URLs: https://github.com/owner/repo.git
        parsed = urlparse(repo_url)
        path_parts = parsed.path.strip('/').split('/')
        if len(path_parts) >= 2:
            owner = path_parts[0]
            repo = path_parts[1].replace('.git', '')
            return parsed.netloc, owner, repo
        
        raise ValueError(f"Could not parse repository URL: {repo_url}")

    async def _create_github_pr(
        self,
        repository: GitRepository,
        pat: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> Optional[str]:
        """Create a GitHub Pull Request."""
        host, owner, repo = self._extract_repo_info(repository.repo_url)
        
        # Use custom_host for GitHub Enterprise, otherwise api.github.com
        if repository.is_self_hosted and repository.custom_host:
            api_base = f"https://{repository.custom_host}/api/v3"
        else:
            api_base = "https://api.github.com"

        url = f"{api_base}/repos/{owner}/{repo}/pulls"
        headers = {
            "Authorization": f"token {pat}",
            "Accept": "application/vnd.github.v3+json",
        }
        payload = {
            "title": title,
            "body": description,
            "head": source_branch,
            "base": target_branch,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code == 201:
                return response.json().get("html_url")
            else:
                logger.error(f"GitHub PR creation failed: {response.status_code} - {response.text}")
                return None

    async def _create_gitlab_pr(
        self,
        repository: GitRepository,
        pat: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> Optional[str]:
        """Create a GitLab Merge Request."""
        host, owner, repo = self._extract_repo_info(repository.repo_url)
        
        # Use custom_host for self-hosted GitLab, otherwise gitlab.com
        if repository.is_self_hosted and repository.custom_host:
            api_base = f"https://{repository.custom_host}/api/v4"
        else:
            api_base = "https://gitlab.com/api/v4"

        # GitLab uses URL-encoded project path
        import urllib.parse
        project_path = urllib.parse.quote(f"{owner}/{repo}", safe='')

        url = f"{api_base}/projects/{project_path}/merge_requests"
        headers = {
            "PRIVATE-TOKEN": pat,
        }
        payload = {
            "title": title,
            "description": description,
            "source_branch": source_branch,
            "target_branch": target_branch,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code == 201:
                return response.json().get("web_url")
            else:
                logger.error(f"GitLab MR creation failed: {response.status_code} - {response.text}")
                return None

    async def _create_bitbucket_pr(
        self,
        repository: GitRepository,
        pat: str,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
    ) -> Optional[str]:
        """Create a Bitbucket Pull Request (Cloud or Server)."""
        host, owner, repo = self._extract_repo_info(repository.repo_url)
        
        is_server = repository.is_self_hosted and repository.custom_host
        
        if is_server:
            # Bitbucket Server API
            api_base = f"https://{repository.custom_host}/rest/api/1.0"
            url = f"{api_base}/projects/{owner}/repos/{repo}/pull-requests"
            headers = {
                "Authorization": f"Bearer {pat}",
                "Content-Type": "application/json",
            }
            payload = {
                "title": title,
                "description": description,
                "fromRef": {"id": f"refs/heads/{source_branch}"},
                "toRef": {"id": f"refs/heads/{target_branch}"},
            }
        else:
            # Bitbucket Cloud API
            api_base = "https://api.bitbucket.org/2.0"
            url = f"{api_base}/repositories/{owner}/{repo}/pullrequests"
            
            # Bitbucket Cloud uses username:app_password
            if repository.access_token_username:
                import base64
                credentials = f"{repository.access_token_username}:{pat}"
                auth_header = base64.b64encode(credentials.encode()).decode()
                headers = {
                    "Authorization": f"Basic {auth_header}",
                    "Content-Type": "application/json",
                }
            else:
                headers = {
                    "Authorization": f"Bearer {pat}",
                    "Content-Type": "application/json",
                }
            
            payload = {
                "title": title,
                "description": description,
                "source": {"branch": {"name": source_branch}},
                "destination": {"branch": {"name": target_branch}},
            }

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code in (200, 201):
                data = response.json()
                # Bitbucket Cloud uses "links.html.href", Server uses "links.self[0].href"
                if "links" in data:
                    if "html" in data["links"]:
                        return data["links"]["html"]["href"]
                    elif "self" in data["links"]:
                        return data["links"]["self"][0]["href"]
                return None
            else:
                logger.error(f"Bitbucket PR creation failed: {response.status_code} - {response.text}")
                return None

    # ==================== Repository Status ====================

    async def get_repository_status(
        self,
        db: AsyncSession,
        repository_id: str,
        organization: Organization
    ) -> Dict[str, Any]:
        """
        Get comprehensive status of a git repository.
        
        Returns capabilities, last sync info, and configuration status.
        """
        result = await db.execute(
            select(GitRepository).where(
                GitRepository.id == repository_id,
                GitRepository.organization_id == organization.id
            )
        )
        repository = result.scalar_one_or_none()
        if not repository:
            raise HTTPException(status_code=404, detail="Git repository not found")

        return {
            "id": repository.id,
            "provider": repository.provider,
            "branch": repository.branch,
            "status": repository.status,
            "has_ssh_key": repository.has_ssh_key,
            "has_access_token": repository.has_access_token,
            "can_push": repository.can_push,
            "can_create_pr": repository.can_create_pr,
            "write_enabled": repository.write_enabled,
            "is_self_hosted": repository.is_self_hosted,
            "last_synced_at": repository.updated_at.isoformat() if repository.updated_at else None,
        }


# For backwards compatibility - keep the old class name as an alias
GitRepositoryService = GitService

