"""
Service for forking reports.

Creates a new report from an existing published/shared report, duplicating
queries, visualizations, widgets, and artifacts with proper ID remapping.
Generates an AI summary of the original conversation as the first message.
"""

import uuid
from typing import Optional, Dict, List, Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.report import Report
from app.models.completion import Completion
from app.models.query import Query
from app.models.visualization import Visualization
from app.models.widget import Widget
from app.models.step import Step
from app.models.artifact import Artifact
from app.models.data_source import DataSource
from app.models.connection import Connection
from app.models.dashboard_layout_version import DashboardLayoutVersion
from app.models.user import User
from app.services.artifact_service import ArtifactService
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

artifact_service = ArtifactService()


class ForkEligibility:
    def __init__(self, can_fork: bool, reason: Optional[str] = None):
        self.can_fork = can_fork
        self.reason = reason

    def to_dict(self):
        return {"can_fork": self.can_fork, "reason": self.reason}


class ForkService:
    """Handles forking a published/shared report into a user's workspace."""

    async def check_eligibility(
        self,
        db: AsyncSession,
        report: Report,
        user: Optional[User],
    ) -> ForkEligibility:
        """Check if a user can fork a given report."""
        if user is None:
            return ForkEligibility(False, "not_logged_in")

        if str(user.organization_id) != str(report.organization_id):
            return ForkEligibility(False, "different_org")

        if str(user.id) == str(report.user_id):
            return ForkEligibility(False, "is_owner")

        # Check all data source connections use system_only auth
        for ds in report.data_sources:
            if not hasattr(ds, 'connections') or not ds.connections:
                continue
            for conn in ds.connections:
                if conn.auth_policy != "system_only":
                    return ForkEligibility(False, "user_auth_required")

        # Check user has access to all data sources
        for ds in report.data_sources:
            if ds.is_public:
                continue
            if not ds.has_membership(str(user.id)):
                return ForkEligibility(False, "no_data_source_access")

        return ForkEligibility(True)

    async def fork_report(
        self,
        db: AsyncSession,
        report_id: str,
        user: User,
        title: Optional[str] = None,
    ) -> Report:
        """Fork a report, creating a new report with duplicated assets."""
        # 1. Load original report with all relationships
        result = await db.execute(
            select(Report)
            .options(
                selectinload(Report.user),
                selectinload(Report.data_sources).selectinload(DataSource.connections),
                selectinload(Report.widgets).selectinload(Widget.steps),
                selectinload(Report.queries).selectinload(Query.visualizations),
                selectinload(Report.queries).selectinload(Query.default_step),
                selectinload(Report.completions),
                selectinload(Report.dashboard_layout_versions),
            )
            .where(Report.id == report_id)
        )
        original = result.unique().scalar_one_or_none()
        if not original:
            raise HTTPException(status_code=404, detail="Report not found")

        # Must be published or have conversation sharing enabled
        if original.status != "published" and not original.conversation_share_enabled:
            raise HTTPException(status_code=403, detail="Report is not available for forking")

        # Check eligibility
        eligibility = await self.check_eligibility(db, original, user)
        if not eligibility.can_fork:
            raise HTTPException(status_code=403, detail=f"Cannot fork: {eligibility.reason}")

        # 2. Create new report
        fork_title = title or f"Fork of {original.title}"
        new_report = Report(
            title=fork_title,
            slug=f"fork-{uuid.uuid4().hex[:8]}",
            status="draft",
            report_type="regular",
            mode=getattr(original, "mode", "chat"),
            theme_name=original.theme_name,
            theme_overrides=original.theme_overrides,
            user_id=str(user.id),
            organization_id=str(original.organization_id),
            forked_from_id=str(original.id),
        )
        db.add(new_report)
        await db.flush()

        # 3. Link data sources
        for ds in original.data_sources:
            from app.models.report_data_source_association import report_data_source_association
            await db.execute(
                report_data_source_association.insert().values(
                    report_id=str(new_report.id),
                    data_source_id=str(ds.id),
                )
            )

        # 4. Duplicate widgets, queries, visualizations (with ID remapping)
        widget_id_map, query_id_map, viz_id_map = await self._duplicate_assets(
            db, original, new_report, user
        )

        # 5. Copy dashboard layout with remapped widget IDs
        await self._duplicate_layouts(db, original, new_report, widget_id_map)

        # 6. Duplicate latest artifact with remapped viz IDs
        new_artifact = await self._duplicate_artifact(
            db, original, new_report, user, viz_id_map
        )

        # 7. Generate fork summary completion
        await self._create_fork_summary(
            db, original, new_report, user,
            query_id_map, viz_id_map, new_artifact,
        )

        await db.commit()
        await db.refresh(new_report)

        return new_report

    async def _duplicate_assets(
        self,
        db: AsyncSession,
        original: Report,
        new_report: Report,
        user: User,
    ) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
        """Duplicate widgets, queries, and visualizations with ID remapping.

        Returns (widget_id_map, query_id_map, viz_id_map).
        """
        widget_id_map: Dict[str, str] = {}
        query_id_map: Dict[str, str] = {}
        viz_id_map: Dict[str, str] = {}

        # First create widgets (queries reference widgets via widget_id)
        for old_widget in original.widgets:
            new_widget = Widget(
                title=old_widget.title,
                slug=f"fork-{uuid.uuid4().hex[:8]}",
                status=old_widget.status,
                x=old_widget.x,
                y=old_widget.y,
                width=old_widget.width,
                height=old_widget.height,
                report_id=str(new_report.id),
            )
            db.add(new_widget)
            await db.flush()
            widget_id_map[str(old_widget.id)] = str(new_widget.id)

        # Then create queries and visualizations
        for old_query in original.queries:
            old_widget_id = str(old_query.widget_id)
            new_widget_id = widget_id_map.get(old_widget_id)
            if not new_widget_id:
                # Widget not in map — create a new one
                new_widget = Widget(
                    title=old_query.title or "",
                    slug=f"fork-{uuid.uuid4().hex[:8]}",
                    status="draft",
                    x=0, y=0, width=5, height=9,
                    report_id=str(new_report.id),
                )
                db.add(new_widget)
                await db.flush()
                new_widget_id = str(new_widget.id)
                widget_id_map[old_widget_id] = new_widget_id

            new_query = Query(
                title=old_query.title,
                description=old_query.description,
                report_id=str(new_report.id),
                widget_id=new_widget_id,
                default_step_id=old_query.default_step_id,  # shared step reference
                organization_id=str(new_report.organization_id),
                user_id=str(user.id),
            )
            db.add(new_query)
            await db.flush()
            query_id_map[str(old_query.id)] = str(new_query.id)

            # Duplicate visualizations for this query
            for old_viz in old_query.visualizations:
                new_viz = Visualization(
                    title=old_viz.title,
                    status=old_viz.status,
                    report_id=str(new_report.id),
                    query_id=str(new_query.id),
                    view=old_viz.view,
                )
                db.add(new_viz)
                await db.flush()
                viz_id_map[str(old_viz.id)] = str(new_viz.id)

        return widget_id_map, query_id_map, viz_id_map

    async def _duplicate_layouts(
        self,
        db: AsyncSession,
        original: Report,
        new_report: Report,
        widget_id_map: Dict[str, str],
    ):
        """Copy dashboard layouts with remapped widget IDs."""
        for layout in original.dashboard_layout_versions:
            # Remap widget IDs in layout blocks
            new_blocks = []
            for block in (layout.blocks or []):
                new_block = dict(block)
                old_wid = new_block.get("widget_id")
                if old_wid and old_wid in widget_id_map:
                    new_block["widget_id"] = widget_id_map[old_wid]
                new_blocks.append(new_block)

            new_layout = DashboardLayoutVersion(
                name=layout.name,
                version=1,
                is_active=layout.is_active,
                theme_name=layout.theme_name,
                theme_overrides=layout.theme_overrides,
                blocks=new_blocks,
                report_id=str(new_report.id),
            )
            db.add(new_layout)

    async def _duplicate_artifact(
        self,
        db: AsyncSession,
        original: Report,
        new_report: Report,
        user: User,
        viz_id_map: Dict[str, str],
    ) -> Optional[Artifact]:
        """Duplicate the latest artifact with remapped visualization_ids."""
        latest = await artifact_service.get_latest_by_report(db, str(original.id))
        if not latest:
            return None

        # Remap visualization_ids in content
        old_content = latest.content or {}
        new_content = dict(old_content)
        old_viz_ids = old_content.get("visualization_ids", [])
        if old_viz_ids:
            new_content["visualization_ids"] = [
                viz_id_map.get(vid, vid) for vid in old_viz_ids
            ]

        new_artifact = Artifact(
            report_id=str(new_report.id),
            user_id=str(user.id),
            organization_id=str(new_report.organization_id),
            title=latest.title,
            mode=latest.mode,
            content=new_content,
            generation_prompt=latest.generation_prompt,
            version=1,
            status="completed",
        )
        db.add(new_artifact)
        await db.flush()

        # Copy thumbnail if exists
        if latest.thumbnail_path:
            try:
                from app.services.thumbnail_service import ThumbnailService
                thumbnail_service = ThumbnailService()
                new_thumbnail_path = thumbnail_service.copy_thumbnail(
                    str(latest.id), str(new_artifact.id)
                )
                if new_thumbnail_path:
                    new_artifact.thumbnail_path = new_thumbnail_path
            except Exception as e:
                logger.warning("Failed to copy thumbnail during fork: %s", e)

        return new_artifact

    async def _create_fork_summary(
        self,
        db: AsyncSession,
        original: Report,
        new_report: Report,
        user: User,
        query_id_map: Dict[str, str],
        viz_id_map: Dict[str, str],
        new_artifact: Optional[Artifact],
    ):
        """Create a summary completion with asset references for the forked report."""
        # Build asset refs list using NEW IDs
        asset_refs: List[Dict[str, Any]] = []

        for old_query in original.queries:
            new_qid = query_id_map.get(str(old_query.id))
            if new_qid:
                asset_refs.append({
                    "type": "query",
                    "id": new_qid,
                    "title": old_query.title or "",
                    "description": old_query.description or "",
                })
            for old_viz in old_query.visualizations:
                new_vid = viz_id_map.get(str(old_viz.id))
                if new_vid:
                    asset_refs.append({
                        "type": "visualization",
                        "id": new_vid,
                        "title": old_viz.title or "",
                    })

        if new_artifact:
            asset_refs.append({
                "type": "artifact",
                "id": str(new_artifact.id),
                "title": new_artifact.title or "",
                "mode": new_artifact.mode,
            })

        # Build summary text from conversation context
        summary_parts = []
        summary_parts.append(f'This report was forked from "{original.title}".')

        # Include query summaries
        if original.queries:
            summary_parts.append(f"\n{len(original.queries)} queries were inherited:")
            for old_query in original.queries:
                new_qid = query_id_map.get(str(old_query.id), "")
                step_info = ""
                if old_query.default_step:
                    step = old_query.default_step
                    step_info = f" ({step.type})"
                    if step.description:
                        step_info += f" - {step.description[:100]}"
                summary_parts.append(
                    f"- {old_query.title or 'Untitled'}{step_info}"
                )

        if new_artifact:
            summary_parts.append(
                f"\nAn artifact ({new_artifact.mode} mode) was also inherited: "
                f'"{new_artifact.title or "Untitled"}".'
            )

        summary_text = "\n".join(summary_parts)

        # Create the fork summary completion
        completion = Completion(
            prompt={"content": ""},
            completion={"content": summary_text},
            status="success",
            model="system",
            turn_index=0,
            role="system",
            message_type="ai_completion",
            report_id=str(new_report.id),
            user_id=str(user.id),
            is_fork_summary="true",
            source_report_id=str(original.id),
            fork_asset_refs=asset_refs,
        )
        db.add(completion)


fork_service = ForkService()
