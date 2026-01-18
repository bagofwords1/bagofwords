from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload
from sqlalchemy import and_, or_, func, update as sql_update
from typing import List, Optional, Dict, Any
from datetime import datetime
from fastapi import HTTPException

from app.models.instruction_build import InstructionBuild
from app.models.instruction_version import InstructionVersion
from app.models.build_content import BuildContent
from app.models.instruction import Instruction
from app.models.organization import Organization
from app.models.user import User
from app.models.eval import TestRun

import logging
logger = logging.getLogger(__name__)


def _generate_build_title(
    source: str,
    added: int = 0,
    modified: int = 0,
    removed: int = 0,
    branch: Optional[str] = None,
) -> str:
    """Generate a human-readable title for the build based on changes."""
    parts = []
    
    if added > 0:
        parts.append(f"Added {added}")
    if modified > 0:
        parts.append(f"Modified {modified}")
    if removed > 0:
        parts.append(f"Removed {removed}")
    
    if parts:
        total = added + modified + removed
        title = ", ".join(parts) + " instruction" + ("s" if total != 1 else "")
    else:
        title = "Empty build"
    
    # Add source context
    if source == 'git' and branch:
        title = f"[{branch}] {title}"
    elif source == 'rollback':
        title = f"Rollback: {title}"
    elif source == 'merge':
        title = f"Merged: {title}"
    
    return title


class BuildService:
    """
    Service for managing InstructionBuild lifecycle.
    Handles creation, draft editing, submission, approval, promotion, diffing, and rollback.
    """
    
    async def create_build(
        self,
        db: AsyncSession,
        org_id: str,
        source: str = 'user',
        user_id: Optional[str] = None,
        job_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        commit_sha: Optional[str] = None,
        branch: Optional[str] = None,
        copy_from_main: bool = True,
    ) -> InstructionBuild:
        """
        Create a new draft build.
        
        Args:
            db: Database session
            org_id: Organization ID
            source: 'user' | 'git' | 'ai'
            user_id: Creator user ID
            job_id: MetadataIndexingJob ID (for git source)
            agent_id: AgentExecution ID (for ai source)
            commit_sha: Git commit SHA (for git source)
            branch: Git branch name (for git source)
            copy_from_main: If True, copy all contents from current main build
        """
        # Get next build number for this organization
        build_number = await self._get_next_build_number(db, org_id)
        
        build = InstructionBuild(
            build_number=build_number,
            status='draft',
            source=source,
            is_main=False,
            organization_id=org_id,
            created_by_user_id=user_id,
            metadata_indexing_job_id=job_id,
            agent_execution_id=agent_id,
            commit_sha=commit_sha,
            branch=branch,
            total_instructions=0,
            added_count=0,
            modified_count=0,
            removed_count=0,
        )
        
        db.add(build)
        await db.commit()
        await db.refresh(build)
        
        logger.info(f"Created build {build.id} (#{build.build_number}) for org {org_id}, source={source}")
        
        # Copy contents from current main build (if exists and requested)
        # This is non-fatal - if it fails, we just start with an empty build
        if copy_from_main:
            try:
                main_build = await self.get_main_build(db, org_id)
                if main_build:
                    # Track base for auto-merge on deploy
                    build.base_build_id = main_build.id
                    logger.debug(f"Copying contents from main build {main_build.id} to {build.id}")
                    copied = await self._copy_build_contents(db, main_build.id, build.id)
                    logger.info(f"Copied {copied} instructions from main build to build {build.id}")
                    # Commit base_build_id and copied contents, then refresh
                    await db.commit()
                    await db.refresh(build)
                else:
                    logger.debug(f"No main build found for org {org_id}, starting with empty build")
            except Exception as e:
                # Log but don't fail - just start with empty build
                logger.warning(f"Failed to copy from main build for build {build.id}: {e}")
        
        return build
    
    async def _copy_build_contents(
        self,
        db: AsyncSession,
        source_build_id: str,
        target_build_id: str,
    ) -> int:
        """
        Copy all BuildContent records from source build to target build.
        Returns the number of records copied.
        """
        # Get all contents from source build
        result = await db.execute(
            select(BuildContent).where(BuildContent.build_id == source_build_id)
        )
        source_contents = result.scalars().all()
        
        copied_count = 0
        for content in source_contents:
            new_content = BuildContent(
                build_id=target_build_id,
                instruction_id=content.instruction_id,
                instruction_version_id=content.instruction_version_id,
            )
            db.add(new_content)
            copied_count += 1
        
        if copied_count > 0:
            # Update target build's total_instructions count directly via SQL
            await db.execute(
                sql_update(InstructionBuild)
                .where(InstructionBuild.id == target_build_id)
                .values(total_instructions=copied_count)
            )
            await db.commit()
        
        return copied_count
    
    async def get_build(self, db: AsyncSession, build_id: str) -> Optional[InstructionBuild]:
        """Get a build by ID."""
        result = await db.execute(
            select(InstructionBuild)
            .options(selectinload(InstructionBuild.contents))
            .where(
                and_(
                    InstructionBuild.id == build_id,
                    InstructionBuild.deleted_at == None
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def get_main_build(self, db: AsyncSession, org_id: str) -> Optional[InstructionBuild]:
        """Get the main (active/live) build for an organization."""
        result = await db.execute(
            select(InstructionBuild)
            .options(selectinload(InstructionBuild.contents))
            .where(
                and_(
                    InstructionBuild.organization_id == org_id,
                    InstructionBuild.is_main == True,
                    InstructionBuild.deleted_at == None
                )
            )
        )
        return result.scalar_one_or_none()
    
    async def list_builds(
        self,
        db: AsyncSession,
        org_id: str,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 50,
        created_by_user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List builds for an organization with optional status filter."""
        # Base conditions
        conditions = [
            InstructionBuild.organization_id == org_id,
            InstructionBuild.deleted_at == None
        ]
        
        if status:
            conditions.append(InstructionBuild.status == status)
        
        if created_by_user_id:
            conditions.append(InstructionBuild.created_by_user_id == created_by_user_id)
        
        # Count total
        count_query = select(func.count()).select_from(
            select(InstructionBuild).where(and_(*conditions)).subquery()
        )
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0
        
        # Fetch builds with user relationships
        query = (
            select(InstructionBuild)
            .options(
                selectinload(InstructionBuild.created_by_user),
                selectinload(InstructionBuild.approved_by_user),
            )
            .where(and_(*conditions))
            .order_by(InstructionBuild.build_number.desc())
            .offset(skip)
            .limit(limit)
        )
        
        result = await db.execute(query)
        builds = list(result.scalars().all())
        
        # Fetch latest test run per build
        build_ids = [str(b.id) for b in builds]
        test_runs_by_build = {}
        
        if build_ids:
            # Subquery to get max started_at per build_id
            latest_run_subq = (
                select(
                    TestRun.build_id,
                    func.max(TestRun.started_at).label('max_started')
                )
                .where(TestRun.build_id.in_(build_ids))
                .group_by(TestRun.build_id)
                .subquery()
            )
            
            # Query to get the actual test runs
            runs_query = (
                select(TestRun)
                .join(
                    latest_run_subq,
                    and_(
                        TestRun.build_id == latest_run_subq.c.build_id,
                        TestRun.started_at == latest_run_subq.c.max_started
                    )
                )
            )
            runs_result = await db.execute(runs_query)
            runs = runs_result.scalars().all()
            
            for run in runs:
                test_runs_by_build[run.build_id] = run
        
        # Enrich builds with test run data and user info
        enriched_items = []
        for build in builds:
            # Get user names from relationships
            created_by_name = None
            if build.created_by_user:
                created_by_name = getattr(build.created_by_user, 'full_name', None) or getattr(build.created_by_user, 'name', None)
            
            approved_by_name = None
            if build.approved_by_user:
                approved_by_name = getattr(build.approved_by_user, 'full_name', None) or getattr(build.approved_by_user, 'name', None)
            
            build_dict = {
                "id": str(build.id),
                "build_number": build.build_number,
                "title": build.title,
                "status": build.status,
                "source": build.source,
                "is_main": build.is_main,
                "base_build_id": build.base_build_id,
                "commit_sha": build.commit_sha,
                "branch": build.branch,
                "total_instructions": build.total_instructions,
                "added_count": build.added_count,
                "modified_count": build.modified_count,
                "removed_count": build.removed_count,
                "created_at": build.created_at,
                "approved_at": build.approved_at,
                "git_branch_name": build.git_branch_name,
                "git_pr_url": build.git_pr_url,
                "git_pushed_at": build.git_pushed_at,
                "created_by_user_id": build.created_by_user_id,
                "created_by_user_name": created_by_name,
                "approved_by_user_id": build.approved_by_user_id,
                "approved_by_user_name": approved_by_name,
                "test_run_id": None,
                "test_status": None,
                "test_passed": None,
                "test_failed": None,
            }
            
            # Add test run data if exists
            run = test_runs_by_build.get(str(build.id))
            if run:
                build_dict["test_run_id"] = str(run.id)
                summary = run.summary_json or {}
                passed = summary.get('passed', 0)
                failed = summary.get('failed', 0)
                build_dict["test_passed"] = passed
                build_dict["test_failed"] = failed
                if run.status in ('success', 'error', 'stopped'):
                    build_dict["test_status"] = 'passed' if failed == 0 and passed > 0 else ('failed' if failed > 0 else None)
                elif run.status == 'in_progress':
                    build_dict["test_status"] = 'pending'
            
            enriched_items.append(build_dict)
        
        return {
            "items": enriched_items,
            "total": total,
            "page": (skip // limit) + 1 if limit > 0 else 1,
            "per_page": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 1
        }
    
    async def get_or_create_draft_build(
        self,
        db: AsyncSession,
        org_id: str,
        source: str = 'user',
        user_id: Optional[str] = None,
        metadata_indexing_job_id: Optional[str] = None,
        agent_execution_id: Optional[str] = None,
        commit_sha: Optional[str] = None,
        branch: Optional[str] = None,
    ) -> InstructionBuild:
        """
        Get an existing draft build or create a new one.
        Used for accumulating user changes before submission.

        For git syncs, creates a new build per job (no reuse).
        For ai source, creates a new build per agent execution (training session).
        For user changes, reuses existing draft build if available.
        """
        # For git source with a job ID, always create a new build (one per sync job)
        # Git syncs copy from main to preserve user-created instructions
        if source == 'git' and metadata_indexing_job_id:
            return await self.create_build(
                db, org_id,
                source=source,
                user_id=user_id,
                job_id=metadata_indexing_job_id,
                commit_sha=commit_sha,
                branch=branch,
                copy_from_main=True  # Preserve user-created instructions
            )

        # For ai source with agent_execution_id, scope to specific training session
        if source == 'ai' and agent_execution_id:
            result = await db.execute(
                select(InstructionBuild)
                .where(
                    and_(
                        InstructionBuild.organization_id == org_id,
                        InstructionBuild.status == 'draft',
                        InstructionBuild.source == source,
                        InstructionBuild.agent_execution_id == agent_execution_id,
                        InstructionBuild.deleted_at == None
                    )
                )
                .limit(1)
            )
            existing = result.scalar_one_or_none()

            if existing:
                return existing

            return await self.create_build(
                db, org_id, source=source, user_id=user_id, agent_id=agent_execution_id
            )

        # For user sources (or ai without execution id), check for existing draft build
        result = await db.execute(
            select(InstructionBuild)
            .where(
                and_(
                    InstructionBuild.organization_id == org_id,
                    InstructionBuild.status == 'draft',
                    InstructionBuild.source == source,
                    InstructionBuild.deleted_at == None
                )
            )
            .order_by(InstructionBuild.created_at.desc())
            .limit(1)
        )
        existing = result.scalar_one_or_none()

        if existing:
            return existing

        return await self.create_build(db, org_id, source=source, user_id=user_id)
    
    # ==================== Draft Editing ====================
    
    async def add_to_build(
        self,
        db: AsyncSession,
        build_id: str,
        instruction_id: str,
        version_id: str,
    ) -> BuildContent:
        """
        Add or update an instruction version in a draft build.
        Raises error if build is not in draft status.
        """
        build = await self.get_build(db, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if not build.can_be_edited:
            raise HTTPException(status_code=400, detail="Build is not editable (must be draft or pending_approval)")
        
        # Check if instruction already exists in build
        existing = await db.execute(
            select(BuildContent).where(
                and_(
                    BuildContent.build_id == build_id,
                    BuildContent.instruction_id == instruction_id
                )
            )
        )
        existing_content = existing.scalar_one_or_none()
        
        if existing_content:
            # Update to new version (only if version actually changed)
            if existing_content.instruction_version_id != version_id:
                existing_content.instruction_version_id = version_id
                build.modified_count += 1
                # Auto-generate title based on updated stats
                build.title = _generate_build_title(
                    source=build.source,
                    added=build.added_count,
                    modified=build.modified_count,
                    removed=build.removed_count,
                    branch=build.branch,
                )
            await db.commit()
            await db.refresh(existing_content)
            return existing_content
        else:
            # Add new content
            content = BuildContent(
                build_id=build_id,
                instruction_id=instruction_id,
                instruction_version_id=version_id,
            )
            db.add(content)
            build.total_instructions += 1
            build.added_count += 1
            # Auto-generate title based on updated stats
            build.title = _generate_build_title(
                source=build.source,
                added=build.added_count,
                modified=build.modified_count,
                removed=build.removed_count,
                branch=build.branch,
            )
            await db.commit()
            await db.refresh(content)
            return content
    
    async def remove_from_build(
        self,
        db: AsyncSession,
        build_id: str,
        instruction_id: str,
    ) -> bool:
        """
        Remove an instruction from a draft build.
        Raises error if build is not in draft status.
        """
        build = await self.get_build(db, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if not build.can_be_edited:
            raise HTTPException(status_code=400, detail="Build is not editable (must be draft or pending_approval)")
        
        result = await db.execute(
            select(BuildContent).where(
                and_(
                    BuildContent.build_id == build_id,
                    BuildContent.instruction_id == instruction_id
                )
            )
        )
        content = result.scalar_one_or_none()
        
        if not content:
            return False
        
        await db.delete(content)
        build.total_instructions = max(0, build.total_instructions - 1)
        build.removed_count += 1
        # Auto-generate title based on updated stats
        build.title = _generate_build_title(
            source=build.source,
            added=build.added_count,
            modified=build.modified_count,
            removed=build.removed_count,
            branch=build.branch,
        )
        await db.commit()
        return True
    
    async def get_build_contents(
        self,
        db: AsyncSession,
        build_id: str,
    ) -> List[BuildContent]:
        """Get all contents of a build with instruction and version details."""
        result = await db.execute(
            select(BuildContent)
            .options(
                selectinload(BuildContent.instruction),
                selectinload(BuildContent.instruction_version),
            )
            .where(BuildContent.build_id == build_id)
        )
        return list(result.scalars().all())
    
    # ==================== Lifecycle ====================
    
    async def submit_build(
        self,
        db: AsyncSession,
        build_id: str,
    ) -> InstructionBuild:
        """
        Submit a draft build for approval.
        Transitions: draft -> pending_approval
        """
        build = await self.get_build(db, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if not build.can_be_submitted:
            raise HTTPException(
                status_code=400, 
                detail=f"Build cannot be submitted (current status: {build.status})"
            )
        
        build.status = 'pending_approval'
        await db.commit()
        await db.refresh(build)
        return build
    
    async def approve_build(
        self,
        db: AsyncSession,
        build_id: str,
        approved_by_user_id: Optional[str] = None,
    ) -> InstructionBuild:
        """
        Approve a pending build.
        Transitions: pending_approval -> approved
        """
        build = await self.get_build(db, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if not build.can_be_approved:
            raise HTTPException(
                status_code=400, 
                detail=f"Build cannot be approved (current status: {build.status})"
            )
        
        build.status = 'approved'
        build.approved_by_user_id = approved_by_user_id
        build.approved_at = datetime.utcnow()
        await db.commit()
        await db.refresh(build)
        return build

    async def reject_build(
        self,
        db: AsyncSession,
        build_id: str,
        user_id: str,
        reason: Optional[str] = None,
    ) -> InstructionBuild:
        """
        Reject a pending build.
        Transitions: pending_approval -> rejected
        """
        build = await self.get_build(db, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if not build.can_be_approved:  # Same check - can only reject pending builds
            raise HTTPException(
                status_code=400, 
                detail=f"Build cannot be rejected (current status: {build.status})"
            )
        
        build.status = 'rejected'
        build.approved_by_user_id = user_id  # Reviewer
        build.approved_at = datetime.utcnow()
        build.rejection_reason = reason
        await db.commit()
        await db.refresh(build)
        return build
    
    async def promote_build(
        self,
        db: AsyncSession,
        build_id: str,
    ) -> InstructionBuild:
        """
        Promote an approved build to main.
        Sets is_main=True on this build and is_main=False on the previous main build.
        Also updates Instruction.current_version_id for all instructions in the build.
        """
        build = await self.get_build(db, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if not build.can_be_promoted:
            raise HTTPException(
                status_code=400, 
                detail=f"Build cannot be promoted (status: {build.status}, is_main: {build.is_main})"
            )
        
        # Clear is_main from current main build (if any)
        await db.execute(
            select(InstructionBuild)
            .where(
                and_(
                    InstructionBuild.organization_id == build.organization_id,
                    InstructionBuild.is_main == True,
                    InstructionBuild.id != build_id
                )
            )
        )
        
        # Use raw SQL update for efficiency
        from sqlalchemy import update
        await db.execute(
            update(InstructionBuild)
            .where(
                and_(
                    InstructionBuild.organization_id == build.organization_id,
                    InstructionBuild.is_main == True,
                    InstructionBuild.id != build_id
                )
            )
            .values(is_main=False)
        )
        
        # Set this build as main
        build.is_main = True
        
        # Update Instruction.current_version_id for all instructions in this build
        contents = await self.get_build_contents(db, build_id)
        for content in contents:
            result = await db.execute(
                select(Instruction).where(Instruction.id == content.instruction_id)
            )
            instruction = result.scalar_one_or_none()
            if instruction:
                instruction.current_version_id = content.instruction_version_id
        
        await db.commit()
        await db.refresh(build)
        return build
    
    async def publish_build(
        self,
        db: AsyncSession,
        build_id: str,
        user_id: str,
    ) -> Dict[str, Any]:
        """
        Publish a build with auto-merge support.
        
        This is the single action to make a build live:
        - Auto-approves if draft/pending
        - Fresh build (base == current main): simple promote
        - Stale build (main changed): auto-merge user's changes onto main
        - Uses last-modified-wins: user's changes overwrite main's for same instruction
        """
        build = await self.get_build(db, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if build.status == 'rejected':
            raise HTTPException(status_code=400, detail="Cannot publish a rejected build")
        
        if build.status == 'approved':
            raise HTTPException(status_code=400, detail="Build is already published. Use rollback to revert to a previous build.")
        
        # Auto-approve if needed (reusing existing methods)
        if build.status == 'draft':
            build = await self.submit_build(db, build_id)
        if build.status == 'pending_approval':
            build = await self.approve_build(db, build_id, user_id)
        
        current_main = await self.get_main_build(db, build.organization_id)
        
        # Case 1: Fresh build or no main - simple promote
        if not current_main or not build.base_build_id or build.base_build_id == current_main.id:
            promoted = await self.promote_build(db, build_id)
            return {"build": promoted, "merged": False}
        
        # Case 2: Stale build - compute diff and merge
        user_diff = await self.diff_builds(db, build.base_build_id, build_id)
        
        # Get source build contents for added instructions (need version_ids)
        source_contents = await self.get_build_contents(db, build_id)
        source_map = {c.instruction_id: c for c in source_contents}
        
        # Race condition check
        fresh_main = await self.get_main_build(db, build.organization_id)
        if fresh_main and current_main and fresh_main.id != current_main.id:
            raise HTTPException(status_code=409, detail="Main build changed during deploy, please retry")
        
        # Create merged build from current main (reuses create_build with copy_from_main)
        merged = await self.create_build(
            db, 
            org_id=current_main.organization_id, 
            source='merge', 
            user_id=user_id,
            copy_from_main=True,
        )
        
        # Apply user's additions
        for instruction_id in user_diff['added']:
            content = source_map.get(instruction_id)
            if content:
                await self.add_to_build(db, merged.id, instruction_id, content.instruction_version_id)
        
        # Apply user's modifications (overwrites main's versions)
        for mod in user_diff['modified']:
            await self.add_to_build(db, merged.id, mod['instruction_id'], mod['to_version_id'])
        
        # Apply user's removals
        for instruction_id in user_diff['removed']:
            await self.remove_from_build(db, merged.id, instruction_id)
        
        # Finalize merged build
        merged.status = 'approved'
        merged.approved_by_user_id = user_id
        merged.approved_at = datetime.utcnow()
        await db.commit()
        
        promoted = await self.promote_build(db, merged.id)
        
        logger.info(f"Deployed build {build_id} via auto-merge: +{user_diff['added_count']} ~{user_diff['modified_count']} -{user_diff['removed_count']}")
        
        return {"build": promoted, "merged": True}
    
    # ==================== Diff ====================
    
    async def diff_builds(
        self,
        db: AsyncSession,
        build_id_a: str,
        build_id_b: str,
    ) -> Dict[str, Any]:
        """
        Compare two builds and return the differences.
        
        Returns:
            {
                "added": [instruction_ids added in B but not in A],
                "removed": [instruction_ids in A but not in B],
                "modified": [{"instruction_id": ..., "from_version": ..., "to_version": ...}],
            }
        """
        # Get contents of both builds
        contents_a = await self.get_build_contents(db, build_id_a)
        contents_b = await self.get_build_contents(db, build_id_b)
        
        # Build lookup maps
        map_a = {c.instruction_id: c for c in contents_a}
        map_b = {c.instruction_id: c for c in contents_b}
        
        ids_a = set(map_a.keys())
        ids_b = set(map_b.keys())
        
        # Calculate differences
        added = list(ids_b - ids_a)
        removed = list(ids_a - ids_b)
        
        modified = []
        for instruction_id in ids_a & ids_b:
            content_a = map_a[instruction_id]
            content_b = map_b[instruction_id]
            if content_a.instruction_version_id != content_b.instruction_version_id:
                modified.append({
                    "instruction_id": instruction_id,
                    "from_version_id": content_a.instruction_version_id,
                    "to_version_id": content_b.instruction_version_id,
                    "from_version_number": content_a.instruction_version.version_number if content_a.instruction_version else None,
                    "to_version_number": content_b.instruction_version.version_number if content_b.instruction_version else None,
                })
        
        return {
            "build_a_id": build_id_a,
            "build_b_id": build_id_b,
            "added": added,
            "removed": removed,
            "modified": modified,
            "added_count": len(added),
            "removed_count": len(removed),
            "modified_count": len(modified),
        }
    
    async def diff_builds_detailed(
        self,
        db: AsyncSession,
        build_id_a: str,
        build_id_b: str,
    ) -> Dict[str, Any]:
        """
        Compare two builds and return detailed differences with full instruction content.
        build_a is the parent/previous build, build_b is the current build.
        
        Returns dict with items containing full text for display and diffing.
        """
        # Get both builds for metadata
        build_a = await self.get_build(db, build_id_a)
        build_b = await self.get_build(db, build_id_b)
        
        if not build_a or not build_b:
            raise HTTPException(status_code=404, detail="One or both builds not found")
        
        # Get contents of both builds
        contents_a = await self.get_build_contents(db, build_id_a)
        contents_b = await self.get_build_contents(db, build_id_b)
        
        # Build lookup maps
        map_a = {c.instruction_id: c for c in contents_a}
        map_b = {c.instruction_id: c for c in contents_b}
        
        ids_a = set(map_a.keys())
        ids_b = set(map_b.keys())
        
        items = []
        
        # Added instructions (in B but not in A)
        for instruction_id in (ids_b - ids_a):
            content = map_b[instruction_id]
            version = content.instruction_version
            instruction = content.instruction
            
            # Get category from version (category_ids) or instruction
            category = None
            if version and version.category_ids:
                category = version.category_ids[0] if isinstance(version.category_ids, list) and version.category_ids else version.category_ids
            elif instruction:
                category = instruction.category
            
            items.append({
                "instruction_id": instruction_id,
                "change_type": "added",
                "title": version.title if version else (instruction.title if instruction else None),
                "text": version.text if version else (instruction.text if instruction else ""),
                "category": category,
                "source_type": instruction.source_type if instruction else None,
                "status": version.status if version else (instruction.status if instruction else None),
                "load_mode": version.load_mode if version else (instruction.load_mode if instruction else None),
                "to_version_id": content.instruction_version_id,
                "to_version_number": version.version_number if version else None,
            })
        
        # Removed instructions (in A but not in B)
        for instruction_id in (ids_a - ids_b):
            content = map_a[instruction_id]
            version = content.instruction_version
            instruction = content.instruction
            
            # Get category from version (category_ids) or instruction
            category = None
            if version and version.category_ids:
                category = version.category_ids[0] if isinstance(version.category_ids, list) and version.category_ids else version.category_ids
            elif instruction:
                category = instruction.category
            
            items.append({
                "instruction_id": instruction_id,
                "change_type": "removed",
                "title": version.title if version else (instruction.title if instruction else None),
                "text": version.text if version else (instruction.text if instruction else ""),
                "category": category,
                "source_type": instruction.source_type if instruction else None,
                "status": version.status if version else (instruction.status if instruction else None),
                "load_mode": version.load_mode if version else (instruction.load_mode if instruction else None),
                "from_version_id": content.instruction_version_id,
                "from_version_number": version.version_number if version else None,
            })
        
        # Modified instructions (in both, but different versions)
        for instruction_id in (ids_a & ids_b):
            content_a = map_a[instruction_id]
            content_b = map_b[instruction_id]
            if content_a.instruction_version_id != content_b.instruction_version_id:
                version_a = content_a.instruction_version
                version_b = content_b.instruction_version
                instruction = content_b.instruction
                
                # Compute which fields changed
                changed_fields = []
                references_added = 0
                references_removed = 0
                if version_a and version_b:
                    if version_a.text != version_b.text:
                        changed_fields.append('text')
                    if version_a.title != version_b.title:
                        changed_fields.append('title')
                    if version_a.status != version_b.status:
                        changed_fields.append('status')
                    if version_a.load_mode != version_b.load_mode:
                        changed_fields.append('load_mode')
                    if version_a.category_ids != version_b.category_ids:
                        changed_fields.append('category')
                    
                    # Check references changes
                    refs_a = set()
                    refs_b = set()
                    if version_a.references_json:
                        refs_a = {(r.get('object_type'), r.get('object_id')) for r in version_a.references_json}
                    if version_b.references_json:
                        refs_b = {(r.get('object_type'), r.get('object_id')) for r in version_b.references_json}
                    
                    if refs_a != refs_b:
                        changed_fields.append('references')
                        references_added = len(refs_b - refs_a)
                        references_removed = len(refs_a - refs_b)
                
                # Get category values
                category_a = None
                category_b = None
                if version_a and version_a.category_ids:
                    category_a = version_a.category_ids[0] if isinstance(version_a.category_ids, list) and version_a.category_ids else version_a.category_ids
                if version_b and version_b.category_ids:
                    category_b = version_b.category_ids[0] if isinstance(version_b.category_ids, list) and version_b.category_ids else version_b.category_ids
                elif instruction:
                    category_b = instruction.category
                
                items.append({
                    "instruction_id": instruction_id,
                    "change_type": "modified",
                    "title": version_b.title if version_b else (instruction.title if instruction else None),
                    "text": version_b.text if version_b else (instruction.text if instruction else ""),
                    "previous_text": version_a.text if version_a else None,
                    "previous_title": version_a.title if version_a else None,
                    "category": category_b,
                    "previous_category": category_a,
                    "source_type": instruction.source_type if instruction else None,
                    "status": version_b.status if version_b else (instruction.status if instruction else None),
                    "previous_status": version_a.status if version_a else None,
                    "load_mode": version_b.load_mode if version_b else (instruction.load_mode if instruction else None),
                    "previous_load_mode": version_a.load_mode if version_a else None,
                    "changed_fields": changed_fields if changed_fields else None,
                    "references_added": references_added if references_added else None,
                    "references_removed": references_removed if references_removed else None,
                    "from_version_id": content_a.instruction_version_id,
                    "to_version_id": content_b.instruction_version_id,
                    "from_version_number": version_a.version_number if version_a else None,
                    "to_version_number": version_b.version_number if version_b else None,
                })
        
        added_count = len([i for i in items if i["change_type"] == "added"])
        modified_count = len([i for i in items if i["change_type"] == "modified"])
        removed_count = len([i for i in items if i["change_type"] == "removed"])
        
        return {
            "build_a_id": build_id_a,
            "build_b_id": build_id_b,
            "build_a_number": build_a.build_number,
            "build_b_number": build_b.build_number,
            "items": items,
            "added_count": added_count,
            "modified_count": modified_count,
            "removed_count": removed_count,
        }
    
    # ==================== Rollback ====================
    
    async def rollback_to_build(
        self,
        db: AsyncSession,
        target_build_id: str,
        org_id: str,
        user_id: str,
    ) -> InstructionBuild:
        """
        Rollback by creating a new build that copies from an older approved build.
        
        This creates a new build with:
        - New build_number (next in sequence)
        - source='rollback' to distinguish from regular builds
        - All contents copied from the target build
        - Auto-approved and promoted to main
        
        This provides clear audit trail of when rollbacks happened.
        
        Note: The target build must be in 'approved' status.
        """
        target_build = await self.get_build(db, target_build_id)
        if not target_build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        if target_build.organization_id != org_id:
            raise HTTPException(status_code=403, detail="Build does not belong to this organization")
        
        if target_build.status != 'approved':
            raise HTTPException(
                status_code=400, 
                detail=f"Can only rollback to approved builds (current status: {target_build.status})"
            )
        
        # === Restore soft-deleted instructions that are in the target build ===
        # This makes delete reversible via rollback
        target_contents = await self.get_build_contents(db, target_build_id)
        instruction_ids_to_restore = [c.instruction_id for c in target_contents]
        
        if instruction_ids_to_restore:
            # Clear deleted_at for any instructions in the target build that were soft-deleted
            result = await db.execute(
                sql_update(Instruction)
                .where(
                    and_(
                        Instruction.id.in_(instruction_ids_to_restore),
                        Instruction.deleted_at != None
                    )
                )
                .values(deleted_at=None)
            )
            restored_count = result.rowcount
            if restored_count > 0:
                logger.info(f"Rollback: restored {restored_count} soft-deleted instructions")
        
        # Create a new build with source='rollback' (don't copy from main)
        new_build = await self.create_build(
            db, 
            org_id, 
            source='rollback',
            user_id=user_id,
            copy_from_main=False,  # Don't copy from current main
        )
        
        # Copy contents from the TARGET build (not main)
        copied = await self._copy_build_contents(db, target_build_id, new_build.id)
        logger.info(f"Rollback: copied {copied} instructions from build {target_build_id} to new build {new_build.id}")
        
        # Auto-approve and promote the new build
        new_build.status = 'approved'
        new_build.approved_by_user_id = user_id
        new_build.approved_at = datetime.utcnow()
        await db.commit()
        await db.refresh(new_build)
        
        # Promote to main
        return await self.promote_build(db, new_build.id)
    
    # ==================== Helpers ====================
    
    async def _get_next_build_number(self, db: AsyncSession, org_id: str) -> int:
        """Get the next build number for an organization."""
        result = await db.execute(
            select(func.max(InstructionBuild.build_number))
            .where(InstructionBuild.organization_id == org_id)
        )
        max_number = result.scalar() or 0
        return max_number + 1
    
    async def update_build_stats(
        self,
        db: AsyncSession,
        build_id: str,
        added: int = 0,
        modified: int = 0,
        removed: int = 0,
    ) -> InstructionBuild:
        """Update build statistics."""
        build = await self.get_build(db, build_id)
        if not build:
            raise HTTPException(status_code=404, detail="Build not found")
        
        build.added_count = added
        build.modified_count = modified
        build.removed_count = removed
        build.total_instructions = added + modified  # Active instructions
        
        await db.commit()
        await db.refresh(build)
        return build
    
    async def update_build_title(
        self,
        db: AsyncSession,
        build_id: str,
        title: Optional[str] = None,
    ) -> None:
        """
        Update build title. If title is None, auto-generate from stats.
        Called after changes to keep the title in sync.
        """
        build = await self.get_build(db, build_id)
        if not build:
            return
        
        if title:
            build.title = title
        else:
            build.title = _generate_build_title(
                source=build.source,
                added=build.added_count,
                modified=build.modified_count,
                removed=build.removed_count,
                branch=build.branch,
            )
        
        await db.commit()

