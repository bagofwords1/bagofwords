from typing import Any, Optional, List
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, select
from sqlalchemy.orm import defer, lazyload, load_only

from app.models.artifact import Artifact, ArtifactVersion
from app.models.report import Report
from app.schemas.artifact_schema import (
    ArtifactCreate,
    ArtifactUpdate,
)


# ---------------------------------------------------------------------------
# Version factory
#
# Every code path that inserts an artifact row goes through one of these two
# functions — the AI tools, the MCP tools, the REST routes, forking, seeding
# and the test fixtures. They are the only place that decides what `version`
# a row gets, so the numbering rule lives here and nowhere else.
#
# Both flush + refresh and deliberately do NOT commit: callers own their
# transaction (fork_service builds a whole report in one; the tools commit
# once after the row and its side effects are in place).
# ---------------------------------------------------------------------------


def _id(value: Any) -> Optional[str]:
    """Stringify an id (uuid / str) without turning None into "None"."""
    return str(value) if value is not None else None


async def new_artifact(
    db: AsyncSession,
    *,
    report_id: str,
    organization_id: str,
    user_id: str,
    mode: str,
    title: Optional[str],
    content: dict,
    generation_prompt: Optional[str] = None,
    completion_id: Optional[str] = None,
    status: str = "completed",
    **version_fields: Any,
) -> ArtifactVersion:
    """Start a brand-new artifact (dashboard / deck / doc) at version 1.

    Extra keyword arguments (thumbnail_path, screenshot_base64, created_at,
    ...) are set on the version row as-is.
    """
    parent = Artifact(
        report_id=_id(report_id),
        organization_id=_id(organization_id),
        created_by=_id(user_id),
        mode=mode,
        title=title,
    )
    db.add(parent)
    await db.flush()
    version = ArtifactVersion(
        artifact_id=str(parent.id),
        report_id=_id(report_id),
        user_id=_id(user_id),
        organization_id=_id(organization_id),
        content=content,
        generation_prompt=generation_prompt,
        completion_id=completion_id,
        status=status,
        version=1,
        **version_fields,
    )
    db.add(version)
    await db.flush()
    # refresh also resolves the title/mode read-throughs from the parent
    await db.refresh(version)
    return version


async def next_version_number(db: AsyncSession, source: ArtifactVersion) -> int:
    """The number the next version after ``source`` gets.

    max(version) over the parent artifact's rows, plus one. Soft-deleted
    rows count too: uq_artifact_versions_artifact_version spans them, so a
    number a deleted row still holds must never be reissued. Never derived
    from ``source.version + 1``: editing an older version would otherwise
    mint a number that already exists further up the chain.
    """
    max_version = (await db.execute(
        select(func.max(ArtifactVersion.version)).where(
            ArtifactVersion.artifact_id == str(source.artifact_id),
        )
    )).scalar() or 0
    return max_version + 1


async def new_version(
    db: AsyncSession,
    source: ArtifactVersion,
    *,
    content: dict,
    user_id: Optional[str] = None,
    generation_prompt: Optional[str] = None,
    completion_id: Optional[str] = None,
    status: str = "completed",
    title: Optional[str] = None,
    **version_fields: Any,
) -> ArtifactVersion:
    """Append the next version to the artifact ``source`` belongs to.

    The new row joins ``source``'s parent artifact — a version can never
    move between artifacts, and mode is fixed there. ``user_id`` (the author
    of this version) defaults to the source's. Passing ``title`` RENAMES the
    parent, i.e. every version of this artifact at once.
    """
    version = ArtifactVersion(
        artifact_id=str(source.artifact_id),
        report_id=_id(source.report_id),
        user_id=_id(user_id) or _id(source.user_id),
        organization_id=_id(source.organization_id),
        content=content,
        generation_prompt=generation_prompt,
        completion_id=completion_id,
        status=status,
        version=await next_version_number(db, source),
        **version_fields,
    )
    db.add(version)
    if title is not None and title != source.title:
        parent = await db.get(Artifact, str(source.artifact_id))
        if parent is not None:
            parent.title = title
            db.add(parent)
    await db.flush()
    # refresh also resolves the title/mode read-throughs from the parent
    await db.refresh(version)
    return version


class ArtifactService:
    """Service for managing Artifact CRUD operations."""

    async def create(
        self,
        db: AsyncSession,
        payload: ArtifactCreate,
        user_id: str,
        organization_id: str,
    ) -> ArtifactVersion:
        """Create a new artifact."""
        artifact = await new_artifact(
            db,
            report_id=str(payload.report_id),
            organization_id=str(organization_id),
            user_id=str(user_id),
            mode=payload.mode,
            title=payload.title,
            content=payload.content,
            generation_prompt=payload.generation_prompt,
            completion_id=payload.completion_id,
        )
        await db.commit()
        return artifact

    async def create_doc_version(
        self,
        db: AsyncSession,
        artifact_id: str,
        markdown: str,
        title: Optional[str],
        user_id: str,
        organization_id: str,
    ) -> ArtifactVersion:
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

        new_artifact = await new_version(
            db,
            artifact,
            user_id=str(user_id),
            title=title or None,
            content={"markdown": markdown, "visualization_ids": valid_viz_ids},
        )
        await db.commit()
        return new_artifact

    async def get(self, db: AsyncSession, artifact_id: str) -> Optional[ArtifactVersion]:
        """Get an artifact by ID.

        lazyload("*"): consumers only use the artifact's own columns;
        Artifact.report would otherwise selectin-cascade the entire report
        graph (every step version's data JSON) on each fetch.
        """
        stmt = select(ArtifactVersion).options(
            lazyload("*"),
            defer(ArtifactVersion.screenshot_base64),
            defer(ArtifactVersion.render_errors),
        ).where(
            ArtifactVersion.id == str(artifact_id),
            ArtifactVersion.deleted_at.is_(None),
        )
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_by_report(
        self,
        db: AsyncSession,
        report_id: str,
        organization_id: Optional[str] = None,
    ) -> List[ArtifactVersion]:
        """List all artifacts for a report, scoped to the caller's organization.

        When ``organization_id`` is provided the read is constrained to that
        org so artifacts of a report owned by a different organization are
        never returned (defense in depth — the route decorator also enforces
        this binding).
        """
        stmt = (
            select(ArtifactVersion)
            .options(
                lazyload("*"),
                load_only(
                    ArtifactVersion.id,
                    ArtifactVersion.artifact_id,
                    ArtifactVersion.report_id,
                    ArtifactVersion.title,
                    ArtifactVersion.mode,
                    ArtifactVersion.version,
                    ArtifactVersion.status,
                    ArtifactVersion.created_at,
                    ArtifactVersion.updated_at,
                ),
            )
            .where(
                ArtifactVersion.report_id == str(report_id),
                ArtifactVersion.deleted_at.is_(None),
            )
            .order_by(ArtifactVersion.created_at.desc())
        )
        if organization_id:
            stmt = stmt.where(ArtifactVersion.organization_id == str(organization_id))
        res = await db.execute(stmt)
        return list(res.scalars().all())

    async def get_latest_by_report(
        self, db: AsyncSession, report_id: str, include_docs: bool = False
    ) -> Optional[ArtifactVersion]:
        """Get the most recent artifact for a report.

        By default docs (mode='doc') are excluded: every existing consumer of
        "the report's latest artifact" means the dashboard/slides deliverable.
        Pass include_docs=True to consider docs too.
        """
        stmt = (
            select(ArtifactVersion)
            .options(
                lazyload("*"),
                defer(ArtifactVersion.screenshot_base64),
                defer(ArtifactVersion.render_errors),
            )
            .where(
                ArtifactVersion.report_id == str(report_id),
                ArtifactVersion.deleted_at.is_(None),
            )
            .order_by(ArtifactVersion.created_at.desc())
            .limit(1)
        )
        if not include_docs:
            # Explicit join beats the column_property's correlated subquery
            # on this hot path.
            stmt = stmt.join(
                Artifact, Artifact.id == ArtifactVersion.artifact_id
            ).where(Artifact.mode.in_(("page", "slides")))
        res = await db.execute(stmt)
        return res.scalar_one_or_none()

    async def update(
        self, db: AsyncSession, artifact_id: str, patch: ArtifactUpdate
    ) -> Optional[ArtifactVersion]:
        """Update an existing artifact."""
        artifact = await self.get(db, artifact_id)
        if not artifact:
            return None

        if patch.title is not None:
            # Writes to version.title are silent no-ops (column_property):
            # a rename targets the parent — the whole artifact, on purpose.
            parent = await db.get(Artifact, str(artifact.artifact_id))
            if parent is not None:
                parent.title = patch.title
                db.add(parent)
        if patch.content is not None:
            # In-place edit of THIS row. Version numbers are minted only by
            # new_version(); bumping here would collide with the next row.
            artifact.content = patch.content
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

    async def duplicate(
        self,
        db: AsyncSession,
        artifact_id: str,
        user_id: str,
    ) -> Optional[ArtifactVersion]:
        """Duplicate an artifact to make it the latest version.

        This creates a copy of the artifact with a new timestamp,
        effectively making it the 'default' since latest = default.
        Also copies the thumbnail if it exists.
        """
        original = await self.get(db, artifact_id)
        if not original:
            return None

        new_artifact = await new_version(
            db,
            original,
            user_id=str(user_id),
            content=original.content,
            generation_prompt=original.generation_prompt,
            completion_id=original.completion_id,
        )
        await db.commit()

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
