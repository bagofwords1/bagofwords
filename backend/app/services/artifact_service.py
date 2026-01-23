from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.artifact import Artifact
from app.schemas.artifact_schema import (
    ArtifactCreate,
    ArtifactUpdate,
)


class ArtifactService:
    """Service for managing Artifact CRUD operations."""

    async def create(
        self,
        db: AsyncSession,
        payload: ArtifactCreate,
        user_id: str,
        organization_id: str,
    ) -> Artifact:
        """Create a new artifact."""
        artifact = Artifact(
            report_id=str(payload.report_id),
            user_id=str(user_id),
            organization_id=str(organization_id),
            title=payload.title,
            mode=payload.mode,
            content=payload.content,
            generation_prompt=payload.generation_prompt,
            completion_id=payload.completion_id,
            version=1,
        )
        db.add(artifact)
        await db.commit()
        await db.refresh(artifact)
        return artifact

    async def get(self, db: AsyncSession, artifact_id: str) -> Optional[Artifact]:
        """Get an artifact by ID."""
        stmt = select(Artifact).where(
            Artifact.id == str(artifact_id),
            Artifact.deleted_at.is_(None),
        )
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_report(self, db: AsyncSession, report_id: str) -> List[Artifact]:
        """List all artifacts for a report."""
        stmt = (
            select(Artifact)
            .where(
                Artifact.report_id == str(report_id),
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc())
        )
        res = await db.execute(stmt)
        return list(res.scalars().all())

    async def get_latest_by_report(
        self, db: AsyncSession, report_id: str
    ) -> Optional[Artifact]:
        """Get the most recent artifact for a report."""
        stmt = (
            select(Artifact)
            .where(
                Artifact.report_id == str(report_id),
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    async def update(
        self, db: AsyncSession, artifact_id: str, patch: ArtifactUpdate
    ) -> Optional[Artifact]:
        """Update an existing artifact."""
        artifact = await self.get(db, artifact_id)
        if not artifact:
            return None

        if patch.title is not None:
            artifact.title = patch.title
        if patch.content is not None:
            artifact.content = patch.content
            artifact.version += 1  # Increment version on content change
        if patch.generation_prompt is not None:
            artifact.generation_prompt = patch.generation_prompt

        db.add(artifact)
        await db.commit()
        await db.refresh(artifact)
        return artifact

    async def delete(self, db: AsyncSession, artifact_id: str) -> bool:
        """Soft delete an artifact."""
        artifact = await self.get(db, artifact_id)
        if not artifact:
            return False

        from datetime import datetime
        artifact.deleted_at = datetime.utcnow()
        db.add(artifact)
        await db.commit()
        return True

    async def create_new_version(
        self,
        db: AsyncSession,
        artifact_id: str,
        new_content: dict,
        user_id: str,
        generation_prompt: Optional[str] = None,
        completion_id: Optional[str] = None,
    ) -> Optional[Artifact]:
        """Create a new version of an artifact by copying and updating content."""
        original = await self.get(db, artifact_id)
        if not original:
            return None

        new_artifact = Artifact(
            report_id=original.report_id,
            user_id=str(user_id),
            organization_id=original.organization_id,
            title=original.title,
            mode=original.mode,
            content=new_content,
            generation_prompt=generation_prompt,
            completion_id=completion_id,
            version=original.version + 1,
        )
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)
        return new_artifact

    async def duplicate(
        self,
        db: AsyncSession,
        artifact_id: str,
        user_id: str,
    ) -> Optional[Artifact]:
        """Duplicate an artifact to make it the latest version.

        This creates a copy of the artifact with a new timestamp,
        effectively making it the 'default' since latest = default.
        """
        original = await self.get(db, artifact_id)
        if not original:
            return None

        # Get the highest version for this report
        existing = await self.list_by_report(db, original.report_id)
        max_version = max((a.version for a in existing), default=0)

        new_artifact = Artifact(
            report_id=original.report_id,
            user_id=str(user_id),
            organization_id=original.organization_id,
            title=original.title,
            mode=original.mode,
            content=original.content,
            generation_prompt=original.generation_prompt,
            completion_id=original.completion_id,
            version=max_version + 1,
        )
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)
        return new_artifact
