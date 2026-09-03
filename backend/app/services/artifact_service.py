from typing import Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.orm import defer, lazyload, load_only

from app.models.artifact import Artifact
from app.models.report import Report
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

    async def create_doc_version(
        self,
        db: AsyncSession,
        artifact_id: str,
        markdown: str,
        title: Optional[str],
        user_id: str,
        organization_id: str,
    ) -> Artifact:
        """Persist a user-edited version of a doc artifact (mode='doc').

        Mirrors the edit_doc tool's contract: validates {{viz:...}} placeholders,
        enforces the size cap, refuses non-doc artifacts, and inserts a NEW row
        (version+1) so history is preserved. Also rejects the save while an
        agent run is in progress on the report — the run-lock that prevents the
        agent and the user from clobbering each other.
        """
        from fastapi import HTTPException

        from app.ai.tools.implementations._doc_markdown import (
            MAX_DOC_CHARS,
            extract_viz_placeholders,
        )
        from app.ai.tools.implementations.create_doc import validate_doc_visualizations

        artifact = await self.get(db, artifact_id)
        if artifact is None:
            raise HTTPException(status_code=404, detail="Document not found")
        if artifact.mode != "doc":
            raise HTTPException(status_code=400, detail="Artifact is not a document")
        if not markdown or not markdown.strip():
            raise HTTPException(status_code=400, detail="Document cannot be empty")
        if len(markdown) > MAX_DOC_CHARS:
            raise HTTPException(status_code=400, detail=f"Document too long (max {MAX_DOC_CHARS} chars)")

        # Run-lock: refuse the save while the agent is working on this report
        from app.models.completion import Completion
        running = await db.execute(
            select(Completion.id).where(
                Completion.report_id == str(artifact.report_id),
                Completion.status == "in_progress",
                Completion.deleted_at.is_(None),
            ).limit(1)
        )
        if running.first() is not None:
            raise HTTPException(
                status_code=409,
                detail="An analysis is currently running on this report — try again when it finishes",
            )

        viz_ids = extract_viz_placeholders(markdown)
        valid_viz_ids, problems = await validate_doc_visualizations(
            db, str(artifact.report_id), viz_ids
        )
        if problems:
            raise HTTPException(status_code=400, detail="Invalid visualization placeholders: " + "; ".join(problems))

        new_artifact = Artifact(
            report_id=str(artifact.report_id),
            user_id=str(user_id),
            organization_id=str(organization_id),
            title=title or artifact.title,
            mode="doc",
            content={"markdown": markdown, "visualization_ids": valid_viz_ids},
            generation_prompt=None,
            version=(artifact.version or 1) + 1,
            status="completed",
        )
        db.add(new_artifact)
        await db.commit()
        await db.refresh(new_artifact)
        return new_artifact

    async def get(self, db: AsyncSession, artifact_id: str) -> Optional[Artifact]:
        """Get an artifact by ID.

        lazyload("*"): consumers only use the artifact's own columns;
        Artifact.report would otherwise selectin-cascade the entire report
        graph (every step version's data JSON) on each fetch.
        """
        stmt = select(Artifact).options(
            lazyload("*"),
            defer(Artifact.screenshot_base64),
            defer(Artifact.render_errors),
        ).where(
            Artifact.id == str(artifact_id),
            Artifact.deleted_at.is_(None),
        )
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_report(
        self,
        db: AsyncSession,
        report_id: str,
        organization_id: Optional[str] = None,
    ) -> List[Artifact]:
        """List all artifacts for a report, scoped to the caller's organization.

        When ``organization_id`` is provided the read is constrained to that
        org so artifacts of a report owned by a different organization are
        never returned (defense in depth — the route decorator also enforces
        this binding).
        """
        stmt = (
            select(Artifact)
            .options(
                lazyload("*"),
                load_only(
                    Artifact.id,
                    Artifact.report_id,
                    Artifact.title,
                    Artifact.mode,
                    Artifact.version,
                    Artifact.status,
                    Artifact.created_at,
                    Artifact.updated_at,
                ),
            )
            .where(
                Artifact.report_id == str(report_id),
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc())
        )
        if organization_id:
            stmt = stmt.where(Artifact.organization_id == str(organization_id))
        res = await db.execute(stmt)
        return list(res.scalars().all())

    async def get_latest_by_report(
        self, db: AsyncSession, report_id: str, include_docs: bool = False
    ) -> Optional[Artifact]:
        """Get the most recent artifact for a report.

        By default docs (mode='doc') are excluded: every existing consumer of
        "the report's latest artifact" means the dashboard/slides deliverable.
        Pass include_docs=True to consider docs too.
        """
        stmt = (
            select(Artifact)
            .options(
                lazyload("*"),
                defer(Artifact.screenshot_base64),
                defer(Artifact.render_errors),
            )
            .where(
                Artifact.report_id == str(report_id),
                Artifact.deleted_at.is_(None),
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
        if not include_docs:
            stmt = stmt.where(Artifact.mode.in_(("page", "slides")))
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
        Also copies the thumbnail if it exists.
        """
        original = await self.get(db, artifact_id)
        if not original:
            return None

        # Next version number, scoped to the artifact's own KIND.
        #
        # This used to take max(version) over every artifact of the report, so
        # a revert borrowed its number from an unrelated deliverable: a doc at
        # v1/v2 reverted alongside a dashboard chain that had reached v7 came
        # back as v8. Scoping to `mode` keeps a doc's numbering out of a
        # dashboard's and vice versa.
        #
        # It is still a superset of the true version chain when one report
        # holds two artifacts of the same mode (two dashboards) — there is no
        # lineage column to scope by yet. That only ever overshoots, never
        # collides: the result is greater than every version in the chain
        # being reverted.
        max_version = (await db.execute(
            select(func.max(Artifact.version)).where(
                Artifact.report_id == str(original.report_id),
                Artifact.organization_id == str(original.organization_id),
                Artifact.mode == original.mode,
                Artifact.deleted_at.is_(None),
            )
        )).scalar() or 0

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

        # Copy thumbnail from original artifact if it exists, otherwise regenerate
        import asyncio
        from app.services.thumbnail_service import ThumbnailService
        thumbnail_service = ThumbnailService()

        if original.thumbnail_path:
            new_thumbnail_path = thumbnail_service.copy_thumbnail(
                str(original.id), str(new_artifact.id)
            )
            if new_thumbnail_path:
                new_artifact.thumbnail_path = new_thumbnail_path
                await db.commit()
        else:
            # Original has no thumbnail - regenerate for the report in background
            asyncio.create_task(thumbnail_service.regenerate_for_report(str(new_artifact.report_id)))

        return new_artifact
