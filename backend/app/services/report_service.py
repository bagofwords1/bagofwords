
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.report import Report
from app.schemas.report_schema import ReportCreate, ReportSchema, ReportUpdate
from app.schemas.data_source_schema import DataSourceReportSchema
from app.services.widget_service import WidgetService
from app.core.telemetry import telemetry
from app.schemas.widget_schema import WidgetSchema
from app.schemas.step_schema import StepSchema
from app.schemas.user_schema import UserSchema
from app.models.file import File
from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.report_data_source_association import report_data_source_association
from app.models.report_file_association import report_file_association
from fastapi import HTTPException

import uuid
from sqlalchemy import select, or_, func, cast, delete, String as SAString
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError
from logging import getLogger
import asyncio

from app.core.scheduler import scheduler
from app.models.dashboard_layout_version import DashboardLayoutVersion
from app.services.dashboard_layout_service import DashboardLayoutService
from app.ee.audit.service import audit_service
from app.models.visualization import Visualization
from app.models.query import Query
from app.models.step import Step
from app.core.otel import get_tracer

logger = getLogger(__name__)
tracer = get_tracer(__name__)

class ReportService:

    def __init__(self):
        self.widget_service = WidgetService()
        self.layout_service = DashboardLayoutService()
    
    async def _check_visibility(
        self,
        db: AsyncSession,
        report: Report,
        visibility_field: str,
        user=None,
    ) -> None:
        """Check if a user can access a report based on its visibility setting.

        visibility_field: 'artifact_visibility' or 'conversation_visibility'
        Raises 401 if login needed, 403 if denied, or passes silently if allowed.
        """
        from app.models.membership import Membership
        from app.models.report_share import ReportShare

        visibility = getattr(report, visibility_field, 'none') or 'none'

        if visibility == 'none':
            # Only owner can access
            if user is None or str(user.id) != str(report.user_id):
                raise HTTPException(status_code=404, detail="Not found")
            return

        if visibility == 'public':
            return  # Anyone can view

        if visibility == 'internal':
            if user is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            stmt = select(Membership).where(
                Membership.user_id == user.id,
                Membership.organization_id == report.organization_id,
            )
            result = await db.execute(stmt)
            if not result.scalar_one_or_none():
                raise HTTPException(status_code=403, detail="Access denied")
            return

        if visibility == 'shared':
            if user is None:
                raise HTTPException(status_code=401, detail="Authentication required")
            # Owner always has access
            if str(user.id) == str(report.user_id):
                return
            share_type = 'artifact' if visibility_field == 'artifact_visibility' else 'conversation'
            stmt = select(ReportShare).where(
                ReportShare.report_id == report.id,
                ReportShare.user_id == user.id,
                ReportShare.share_type == share_type,
                ReportShare.deleted_at.is_(None),
            )
            result = await db.execute(stmt)
            if not result.scalar_one_or_none():
                raise HTTPException(status_code=403, detail="Access denied")
            return

    async def set_visibility(
        self,
        db: AsyncSession,
        report_id: str,
        share_type: str,
        visibility: str,
        shared_user_ids: list[str] | None,
        current_user: User,
        organization: Organization,
    ) -> dict:
        """Set visibility for artifact or conversation sharing.

        share_type: 'artifact' or 'conversation'
        visibility: 'none', 'shared', 'internal', 'public'
        shared_user_ids: list of user IDs (required when visibility == 'shared')
        """
        from app.models.report_share import ReportShare

        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        field = 'artifact_visibility' if share_type == 'artifact' else 'conversation_visibility'
        setattr(report, field, visibility)

        # Sync legacy fields for backward compatibility
        if share_type == 'artifact':
            report.status = 'published' if visibility != 'none' else 'draft'
        elif share_type == 'conversation':
            if visibility != 'none':
                report.conversation_share_enabled = True
                if not report.conversation_share_token:
                    report.conversation_share_token = uuid.uuid4().hex
            else:
                report.conversation_share_enabled = False

        # Update shares list
        if visibility == 'shared' and shared_user_ids is not None:
            # Remove existing shares for this type
            await db.execute(
                delete(ReportShare).where(
                    ReportShare.report_id == report_id,
                    ReportShare.share_type == share_type,
                )
            )
            # Add new shares
            for uid in shared_user_ids:
                share = ReportShare(
                    report_id=report_id,
                    user_id=uid,
                    share_type=share_type,
                )
                db.add(share)
        elif visibility != 'shared':
            # Clear shares if moving away from shared mode
            await db.execute(
                delete(ReportShare).where(
                    ReportShare.report_id == report_id,
                    ReportShare.share_type == share_type,
                )
            )

        await db.commit()
        await db.refresh(report)

        # Telemetry
        try:
            await telemetry.capture(
                "report_visibility_changed",
                {
                    "report_id": str(report.id),
                    "share_type": share_type,
                    "visibility": visibility,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.visibility_changed",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={
                    "title": report.title,
                    "share_type": share_type,
                    "visibility": visibility,
                },
            )
        except Exception:
            pass

        return {
            "share_type": share_type,
            "visibility": visibility,
            "shared_user_ids": shared_user_ids or [],
            "conversation_share_token": report.conversation_share_token if share_type == 'conversation' and visibility != 'none' else None,
        }

    async def get_shares(
        self,
        db: AsyncSession,
        report_id: str,
        share_type: str,
    ) -> list[dict]:
        """Get list of users a report is shared with for a given share_type."""
        from app.models.report_share import ReportShare

        stmt = (
            select(ReportShare)
            .options(selectinload(ReportShare.user))
            .where(
                ReportShare.report_id == report_id,
                ReportShare.share_type == share_type,
                ReportShare.deleted_at.is_(None),
            )
        )
        result = await db.execute(stmt)
        shares = result.scalars().all()

        return [
            {
                "id": str(s.id),
                "user_id": str(s.user_id),
                "user_name": s.user.name if s.user else None,
                "user_email": s.user.email if s.user else None,
                "share_type": s.share_type,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in shares
        ]

    async def _detect_app_version(self, db: AsyncSession, report_id: str) -> str:
        """Detect app version for routing decisions based on agent execution data."""
        from app.models.agent_execution import AgentExecution
        from app.models.completion import Completion
        
        # Check if there are any agent executions for this report
        ae_query = select(AgentExecution).join(
            Completion, AgentExecution.completion_id == Completion.id
        ).where(
            Completion.report_id == report_id,
            Completion.role == 'system'
        ).order_by(AgentExecution.created_at.desc()).limit(1)
        
        result = await db.execute(ae_query)
        latest_execution = result.scalar_one_or_none()
        
        if latest_execution and latest_execution.bow_version:
            return latest_execution.bow_version
        
        # Fallback: check if any system completions exist (indicating it has AI interactions)
        completion_query = select(Completion).where(
            Completion.report_id == report_id,
            Completion.role == 'system'
        ).limit(1)
        
        completion_result = await db.execute(completion_query)
        has_system_completions = completion_result.scalar_one_or_none() is not None
        
        # If it has system completions but no agent executions, it's legacy
        if has_system_completions:
            return "0.0.189"  # Legacy version
        
        # New reports default to current version
        from app.settings.config import settings
        return settings.version
        

    async def get_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> ReportSchema:
        result = await db.execute(
            select(Report)
            .options(
                selectinload(Report.user),  # Use selectinload for async loading
                selectinload(Report.data_sources).selectinload(DataSource.connections)  # Load data sources and their connections
            )
            .filter(Report.id == report_id)
        )
        report = result.unique().scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        user_schema = UserSchema.from_orm(report.user)

        # Detect app version for routing
        app_version = await self._detect_app_version(db, report.id)

        report_schema = ReportSchema(
            id=report.id,
            title=report.title,
            status=report.status,
            slug=report.slug,
            report_type=report.report_type,
            user=user_schema,
            cron_schedule=report.cron_schedule,
            created_at=report.created_at,
            updated_at=report.updated_at,
            app_version=app_version,
            data_sources=report.data_sources,
            external_platform=report.external_platform,
            theme_name=report.theme_name,
            theme_overrides=report.theme_overrides,
            mode=getattr(report, "mode", "chat"),
            # Conversation sharing
            conversation_share_enabled=bool(getattr(report, "conversation_share_enabled", False)),
            conversation_share_token=getattr(report, "conversation_share_token", None),
            # Sharing visibility
            artifact_visibility=getattr(report, "artifact_visibility", "none") or "none",
            conversation_visibility=getattr(report, "conversation_visibility", "none") or "none",
            artifact_shared_user_ids=[
                str(s.user_id) for s in (report.shares or [])
                if s.share_type == 'artifact' and s.deleted_at is None
            ],
            conversation_shared_user_ids=[
                str(s.user_id) for s in (report.shares or [])
                if s.share_type == 'conversation' and s.deleted_at is None
            ],
            # Scheduled rerun notification subscribers
            notification_subscribers=getattr(report, "notification_subscribers", None),
        )
        # Summary counts (for auto-opening sidebar)
        report_schema.query_count = len(report.queries or [])
        report_schema.artifact_count = len(report.artifacts or [])
        active_sps = [
            sp for sp in (report.scheduled_prompts or [])
            if sp.is_active and sp.deleted_at is None
        ]
        report_schema.has_scheduled_prompts = len(active_sps) > 0
        report_schema.scheduled_prompt_count = len(active_sps)

        # Instruction count
        from app.models.instruction import Instruction
        from app.models.agent_execution import AgentExecution
        ic_result = await db.execute(
            select(func.count(Instruction.id))
            .join(AgentExecution, Instruction.agent_execution_id == AgentExecution.id)
            .where(
                AgentExecution.report_id == report.id,
                Instruction.deleted_at == None,
            )
        )
        report_schema.instruction_count = ic_result.scalar() or 0

        # Enrich fork lineage
        await self._enrich_fork_lineage(db, report, report_schema)
        return report_schema

    async def create_report(self, db: AsyncSession, report_data: ReportCreate, current_user: User, organization: Organization) -> ReportSchema:
        # Extract widget data and remove it from report_data
        widget_data = report_data.widget
        del report_data.widget
        file_uuids = report_data.files or []
        del report_data.files
        data_source_ids = report_data.data_sources or []
        del report_data.data_sources

        # Create the report object
        report = Report(**report_data.dict())
        # Ensure a default theme is set for new reports
        if getattr(report, 'theme_name', None) in (None, ''):
            report.theme_name = 'default'
        report.user_id = current_user.id
        report.organization_id = organization.id
        await self._set_slug_for_report(db, report)
        db.add(report)
        
        # Flush to get report.id without committing (stays in same transaction)
        await db.flush()
        
        # Refresh to initialize relationships for async access
        await db.refresh(report, ["files", "data_sources"])

        # Create dashboard layout in the same transaction
        empty_layout = DashboardLayoutVersion(
            report_id=report.id,
            name="",
            version=1,
            is_active=True,
            theme_name=report.theme_name,
            theme_overrides=report.theme_overrides or {},
            blocks=[],
        )
        db.add(empty_layout)

        # Associate files only if there are any (skip unnecessary query)
        if file_uuids:
            file_result = await db.execute(select(File).filter(File.id.in_(file_uuids)))
            files = file_result.scalars().all()
            report.files.extend(files)

        # Associate data sources only if there are any (skip unnecessary query)
        if data_source_ids:
            ds_result = await db.execute(
                select(DataSource)
                .options(selectinload(DataSource.data_source_memberships))
                .filter(DataSource.id.in_(data_source_ids))
            )
            data_sources = ds_result.scalars().all()
            report.data_sources.extend(data_sources)

        # Single commit for the entire transaction
        await db.commit()
        
        # Final refresh to load all relationships needed by ReportSchema
        await db.refresh(report, ["user", "widgets", "dashboard_layout_versions"])

        # Fire-and-forget telemetry (non-blocking)
        try:
            await telemetry.capture(
                "report_created",
                {
                    "report_id": str(report.id),
                    "status": report.status,
                    "count_files": len(file_uuids),
                    "count_data_sources": len(data_source_ids)
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        return ReportSchema.from_orm(report).copy(update={"user": UserSchema.from_orm(current_user)})

    async def update_report(self, db: AsyncSession, report_id: str, report_data: ReportUpdate, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id).filter(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        if report_data.title:
            report.title = report_data.title
        if report_data.status:
            report.status = report_data.status 
        # Persist theme updates if present in payload
        if hasattr(report_data, 'theme_name') and report_data.theme_name is not None:
            report.theme_name = report_data.theme_name
        if hasattr(report_data, 'theme_overrides') and report_data.theme_overrides is not None:
            report.theme_overrides = report_data.theme_overrides
        # Persist mode update if present in payload
        if hasattr(report_data, 'mode') and report_data.mode is not None:
            # Block training mode if enable_training_mode or allow_llm_see_data is disabled
            if report_data.mode == 'training':
                from app.models.organization_settings import OrganizationSettings
                settings_result = await db.execute(
                    select(OrganizationSettings).filter(OrganizationSettings.organization_id == organization.id)
                )
                org_settings = settings_result.scalar_one_or_none()
                if org_settings:
                    # Check enable_training_mode flag
                    enable_training_mode = org_settings.get_config("enable_training_mode")
                    training_mode_disabled = False
                    if enable_training_mode is not None:
                        if hasattr(enable_training_mode, 'value'):
                            training_mode_disabled = enable_training_mode.value is False
                        elif isinstance(enable_training_mode, dict):
                            training_mode_disabled = enable_training_mode.get('value') is False
                    else:
                        # Default to disabled if not set
                        training_mode_disabled = True
                    if training_mode_disabled:
                        raise HTTPException(
                            status_code=400,
                            detail="Training mode is not enabled for this organization"
                        )

                    # Check allow_llm_see_data flag
                    allow_llm_see_data = org_settings.get_config("allow_llm_see_data")
                    llm_see_data_disabled = False
                    if allow_llm_see_data is not None:
                        if hasattr(allow_llm_see_data, 'value'):
                            llm_see_data_disabled = allow_llm_see_data.value is False
                        elif isinstance(allow_llm_see_data, dict):
                            llm_see_data_disabled = allow_llm_see_data.get('value') is False
                    if llm_see_data_disabled:
                        raise HTTPException(
                            status_code=400,
                            detail="Training mode is not available when 'Allow LLM to see data' is disabled"
                        )
            report.mode = report_data.mode
        # Replace data_sources associations if provided
        if hasattr(report_data, 'data_sources') and report_data.data_sources is not None:
            await self.set_data_sources_for_report(db, report, report_data.data_sources)
        
        #await self._set_slug_for_report(db, report)

        await db.commit()
        await db.refresh(report)
        # Telemetry: report publish status changed
        try:
            await telemetry.capture(
                "report_published" if report.status == "published" else "report_unpublished",
                {
                    "report_id": str(report.id),
                    "status": report.status,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.updated",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title, "status": report.status},
            )
        except Exception:
            pass

        return report

    async def rerun_report_steps(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> Report:
        logger.info(f"Executing scheduled report run for report_id: {report_id}")
        report = await self.get_report(db, report_id, current_user, organization)

        # Prefer visualization/query-based rerun via active dashboard layout
        try:
            layout = await self.layout_service.get_or_create_active_layout(db, report_id)
            blocks = list(layout.blocks or [])
            viz_blocks = [b for b in blocks if isinstance(b, dict) and b.get('type') == 'visualization']
        except Exception as e:
            logger.exception("Failed to load active layout for report %s: %s", report_id, e)
            viz_blocks = []

        if viz_blocks:
            for b in viz_blocks:
                viz_id = b.get('visualization_id')
                if not viz_id:
                    continue
                # Load visualization and its query
                viz_result = await db.execute(select(Visualization).where(Visualization.id == viz_id))
                viz = viz_result.scalar_one_or_none()
                if not viz:
                    logger.warning("Visualization %s not found for report %s; skipping", viz_id, report_id)
                    continue
                if not viz.query_id:
                    logger.warning("Visualization %s has no query_id; skipping", viz_id)
                    continue
                query = await db.get(Query, viz.query_id)
                if not query:
                    logger.warning("Query %s not found for visualization %s; skipping", viz.query_id, viz_id)
                    continue

                # Choose step: prefer default_step, else latest step for query
                step: Step | None = None
                if query.default_step_id:
                    step = await db.get(Step, query.default_step_id)
                if not step:
                    step_result = await db.execute(
                        select(Step).where(Step.query_id == query.id).order_by(Step.created_at.desc()).limit(1)
                    )
                    step = step_result.scalar_one_or_none()

                if not step:
                    logger.warning(f"No step found for visualization {viz_id}; skipping")
                    continue
                if not step.code or not str(step.code).strip():
                    logger.warning(f"Step code is empty for visualization {viz_id}; skipping")
                    continue

                try:
                    logger.info(f"Running visualization {viz_id} via step {step.id} for report {report_id}")
                    await self.widget_service.step_service.rerun_step(db, step.id)
                except Exception as e:
                    logger.warning(f"Failed to rerun step {step.id} for visualization {viz_id}: {e}; continuing")
                    continue
        else:
            # Legacy fallback: rerun last step for each published widget
            published_widgets = await self.widget_service.get_published_widgets_for_report(db, report_id)
            for widget in published_widgets:
                try:
                    logger.info(f"Running widget {widget.id} for report {report_id}")
                    await self.widget_service.run_widget_step(db, widget, current_user, organization)
                except Exception as e:
                    logger.warning(f"Failed to run widget {widget.id}: {e}; continuing")
                    continue

        # Update last_run_at timestamp on the ORM model
        report_orm = await db.get(Report, report_id)
        if report_orm:
            report_orm.last_run_at = datetime.utcnow()
            await db.commit()

        # Regenerate thumbnail for the latest artifact in background
        from app.services.thumbnail_service import ThumbnailService
        thumbnail_service = ThumbnailService()
        asyncio.create_task(thumbnail_service.regenerate_for_report(report_id))

        # Notify subscribers after scheduled rerun
        if report_orm and report_orm.notification_subscribers:
            from app.services.notification_service import notification_service
            from app.settings.config import settings as app_settings
            report_url = f"{app_settings.bow_config.base_url}/r/{report_id}"
            asyncio.create_task(
                notification_service.send_scheduled_report_results(
                    report_id=report_id,
                    report_title=report_orm.title or "Untitled Report",
                    subscribers=report_orm.notification_subscribers,
                    report_url=report_url,
                )
            )

        logger.info(f"Completed scheduled report run for report_id: {report_id}")
        return report

    async def archive_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id).filter(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        report.status = 'archived'
        await db.commit()
        await db.refresh(report)

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.archived",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title},
            )
        except Exception:
            pass

        return report

    async def publish_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id).filter(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        if report.status == 'published':
            report.status = 'draft'
        else:
            report.status = 'published'

        await db.commit()
        await db.refresh(report)
        # Telemetry: report publish status changed
        try:
            await telemetry.capture(
                "report_published" if report.status == "published" else "report_unpublished",
                {
                    "report_id": str(report.id),
                    "status": report.status,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.published" if report.status == "published" else "report.unpublished",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title, "status": report.status},
            )
        except Exception:
            pass

        return report

    async def get_public_report(self, db: AsyncSession, report_id: str, user=None) -> ReportSchema:
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # Check artifact visibility (replaces old status == 'published' check)
        await self._check_visibility(db, report, 'artifact_visibility', user)
        
        schema = ReportSchema.from_orm(report)
        # Enrich fork lineage
        await self._enrich_fork_lineage(db, report, schema)
        # Attach minimal general settings from organization settings
        try:
            from app.models.organization_settings import OrganizationSettings
            settings_result = await db.execute(select(OrganizationSettings).filter(OrganizationSettings.organization_id == report.organization_id))
            settings = settings_result.scalar_one_or_none()
            if settings and isinstance(settings.config, dict):
                general = settings.config.get("general", {}) or {}
                schema.general = ReportSchema.PublicGeneralSettings(
                    ai_analyst_name=general.get("ai_analyst_name", "AI Analyst"),
                    bow_credit=general.get("bow_credit", True),
                    icon_url=general.get("icon_url")
                )
        except Exception:
            pass

        return schema

    async def get_public_layouts(self, db: AsyncSession, report_id: str, user=None):
        # Ensure report exists and has artifact visibility
        result = await db.execute(select(Report).where(Report.id == report_id).where(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        rows = await db.execute(
            select(DashboardLayoutVersion).where(DashboardLayoutVersion.report_id == report_id).order_by(
                DashboardLayoutVersion.created_at.asc()
            )
        )
        layouts = rows.scalars().all()

        from app.schemas.dashboard_layout_version_schema import DashboardLayoutVersionSchema
        return [DashboardLayoutVersionSchema.from_orm(l) for l in layouts]

    async def get_public_queries(self, db: AsyncSession, report_id: str, artifact_id: str | None = None, user=None):
        """Get queries for a shared report.

        If artifact_id is provided, only returns queries for visualizations used by that artifact.
        """
        # Verify report exists and check artifact visibility
        result = await db.execute(select(Report).where(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        # If artifact_id provided, filter to only queries used by that artifact
        query_ids_filter = None
        if artifact_id:
            from app.models.artifact import Artifact
            artifact_result = await db.execute(
                select(Artifact).where(
                    Artifact.id == artifact_id,
                    Artifact.report_id == report_id,
                    Artifact.deleted_at.is_(None)
                )
            )
            artifact = artifact_result.scalar_one_or_none()
            if artifact and artifact.content:
                visualization_ids = artifact.content.get("visualization_ids", [])
                if visualization_ids:
                    # Get query_ids from visualizations
                    viz_result = await db.execute(
                        select(Visualization.query_id).where(Visualization.id.in_(visualization_ids))
                    )
                    query_ids_filter = [row[0] for row in viz_result.all() if row[0]]

        # Fetch queries that have a successful step, eagerly load visualizations
        query_stmt = (
            select(Query)
            .join(Step, Step.id == Query.default_step_id)
            .options(selectinload(Query.visualizations))
            .where(Query.report_id == report_id, Step.status == 'success')
        )

        # Apply artifact filter if present
        if query_ids_filter is not None:
            query_stmt = query_stmt.where(Query.id.in_(query_ids_filter))

        queries_result = await db.execute(query_stmt)
        queries = queries_result.scalars().all()

        from app.schemas.query_schema import PublicQuerySchema
        return [PublicQuerySchema.model_validate(q) for q in queries]

    async def get_public_step(self, db: AsyncSession, report_id: str, query_id: str, user=None):
        """Get the default step for a query in a shared report."""
        # Verify report exists and check artifact visibility
        result = await db.execute(select(Report).where(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        # Fetch query and verify it belongs to this report
        query_result = await db.execute(
            select(Query).where(Query.id == query_id, Query.report_id == report_id)
        )
        query = query_result.scalar_one_or_none()
        if not query:
            raise HTTPException(status_code=404, detail="Not found")

        # Get the default step (or latest successful step if no default)
        step = None
        if query.default_step_id:
            step_result = await db.execute(
                select(Step).where(Step.id == query.default_step_id, Step.status == 'success')
            )
            step = step_result.scalar_one_or_none()

        if not step:
            # Fallback to latest successful step
            step_result = await db.execute(
                select(Step)
                .where(Step.query_id == query_id, Step.status == 'success')
                .order_by(Step.created_at.desc())
                .limit(1)
            )
            step = step_result.scalar_one_or_none()

        if not step:
            raise HTTPException(status_code=404, detail="Not found")

        from app.schemas.step_schema import PublicStepSchema
        # Convert view to dict if it's not already
        view_dict = step.view if isinstance(step.view, dict) else (step.view.dict() if step.view else {})
        return PublicStepSchema(
            id=step.id,
            title=step.title,
            type=step.type,
            code=step.code,
            data_model=step.data_model or {},
            data=step.data or {},
            view=view_dict,
        )

    async def get_public_artifacts(self, db: AsyncSession, report_id: str, user=None):
        """List artifacts for a shared report."""
        # Verify report exists and check artifact visibility
        result = await db.execute(select(Report).where(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        # Fetch artifacts for this report
        from app.models.artifact import Artifact
        artifacts_result = await db.execute(
            select(Artifact)
            .where(Artifact.report_id == report_id, Artifact.deleted_at.is_(None))
            .order_by(Artifact.created_at.desc())
        )
        artifacts = artifacts_result.scalars().all()

        from app.schemas.artifact_schema import ArtifactListSchema
        return [ArtifactListSchema.model_validate(a) for a in artifacts]

    async def get_public_artifact(self, db: AsyncSession, report_id: str, artifact_id: str, user=None):
        """Get a specific artifact for a shared report."""
        # Verify report exists and check artifact visibility
        result = await db.execute(select(Report).where(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Not found")
        await self._check_visibility(db, report, 'artifact_visibility', user)

        # Fetch the artifact and verify it belongs to this report
        from app.models.artifact import Artifact
        artifact_result = await db.execute(
            select(Artifact).where(
                Artifact.id == artifact_id,
                Artifact.report_id == report_id,
                Artifact.deleted_at.is_(None)
            )
        )
        artifact = artifact_result.scalar_one_or_none()
        if not artifact:
            raise HTTPException(status_code=404, detail="Not found")

        from app.schemas.artifact_schema import ArtifactSchema
        return ArtifactSchema.model_validate(artifact)

    async def get_reports(
        self,
        db: AsyncSession,
        current_user: User,
        organization: Organization,
        page: int = 1,
        limit: int = 10,
        filter: str = "my",
        search: str | None = None,
        scheduled: bool | None = None,
        status: str | None = None,
        data_source_id: str | None = None,
        mode: str | None = None,
        has_artifacts: str | None = None,
    ):
        with tracer.start_as_current_span("get_reports") as span:

            span.set_attribute("user.id", str(current_user.name))
            span.set_attribute("org.id", str(organization.name))
            # Calculate offset
            offset = (page - 1) * limit

            # Build filter conditions based on filter parameter
            base_conditions = [
                Report.organization_id == organization.id,
                Report.status != 'archived',
            ]

            base_conditions.append(Report.report_type == 'regular')

            # Optional filter by mode (chat/deep/training)
            if mode and mode in ('chat', 'deep', 'training'):
                base_conditions.append(Report.mode == mode)

            # Shared visibility: reports the user has been explicitly shared with
            from app.models.report_share import ReportShare
            shared_with_user = Report.id.in_(
                select(ReportShare.report_id).where(
                    ReportShare.user_id == current_user.id,
                    ReportShare.deleted_at.is_(None),
                )
            )
            # A report is "visible" if it has any non-none visibility and
            # either it's public/internal or the user is in the share list
            visible_to_user = or_(
                Report.artifact_visibility.in_(['public', 'internal']),
                Report.conversation_visibility.in_(['public', 'internal']),
                shared_with_user,
            )

            if filter == "my":
                # Show only reports owned by current user
                base_conditions.append(Report.user_id == current_user.id)
            elif filter == "published":
                # Show shared/published reports visible to the user
                base_conditions.append(visible_to_user)
            else:
                # Default: show reports user can view (owned OR visible)
                base_conditions.append(
                    or_(Report.user_id == current_user.id, visible_to_user)
                )

            # Optional search on report title and completion content
            if search:
                from app.models.completion import Completion
                base_conditions.append(
                    or_(
                        Report.title.ilike(f"%{search}%"),
                        Report.id.in_(
                            select(Completion.report_id).where(
                                or_(
                                    cast(Completion.prompt, SAString).ilike(f"%{search}%"),
                                    cast(Completion.completion, SAString).ilike(f"%{search}%"),
                                )
                            )
                        )
                    )
                )

            # Optional filter by scheduled status (report-level cron OR active scheduled prompts)
            if scheduled is True:
                from app.models.scheduled_prompt import ScheduledPrompt
                base_conditions.append(
                    or_(
                        Report.cron_schedule.isnot(None),
                        Report.id.in_(
                            select(ScheduledPrompt.report_id).where(
                                ScheduledPrompt.is_active == True,
                                ScheduledPrompt.deleted_at == None,
                            )
                        ),
                    )
                )
            elif scheduled is False:
                from app.models.scheduled_prompt import ScheduledPrompt
                base_conditions.append(Report.cron_schedule.is_(None))
                base_conditions.append(
                    ~Report.id.in_(
                        select(ScheduledPrompt.report_id).where(
                            ScheduledPrompt.is_active == True,
                            ScheduledPrompt.deleted_at == None,
                        )
                    )
                )

            # Optional filter by report status (draft/published)
            if status in ('draft', 'published'):
                base_conditions.append(Report.status == status)

            # Optional filter by data source
            if data_source_id:
                base_conditions.append(
                    Report.id.in_(
                        select(report_data_source_association.c.report_id).where(
                            report_data_source_association.c.data_source_id == data_source_id
                        )
                    )
                )

            # Optional filter by artifact presence
            if has_artifacts == 'yes':
                from app.models.artifact import Artifact
                base_conditions.append(
                    Report.id.in_(
                        select(Artifact.report_id).where(Artifact.report_id.isnot(None))
                    )
                )
            elif has_artifacts == 'no':
                from app.models.artifact import Artifact
                base_conditions.append(
                    ~Report.id.in_(
                        select(Artifact.report_id).where(Artifact.report_id.isnot(None))
                    )
                )

            # Base query for filtering
            base_query = select(Report).where(*base_conditions)

            # Count total items
            count_query = select(func.count(Report.id)).where(*base_conditions)

            total_result = await db.execute(count_query)
            total = total_result.scalar()

            # Get paginated results - load data_sources with connections to get type
            query = base_query.options(
                selectinload(Report.user),
                selectinload(Report.widgets),
                selectinload(Report.data_sources).selectinload(DataSource.connections),
                selectinload(Report.artifacts)
            ).order_by(Report.created_at.desc()).offset(offset).limit(limit)

            result = await db.execute(query)
            span.add_event("query executed")
            reports = result.scalars().all()
            span.add_event("query result loaded into memory ")

            # Batch-fetch instruction counts for training reports
            report_ids = [str(r.id) for r in reports]
            instruction_counts: dict[str, int] = {}
            if report_ids:
                from app.models.instruction import Instruction
                from app.models.agent_execution import AgentExecution
                ic_query = (
                    select(
                        AgentExecution.report_id,
                        func.count(Instruction.id),
                    )
                    .join(AgentExecution, Instruction.agent_execution_id == AgentExecution.id)
                    .where(
                        AgentExecution.report_id.in_(report_ids),
                        Instruction.deleted_at == None,
                    )
                    .group_by(AgentExecution.report_id)
                )
                ic_result = await db.execute(ic_query)
                instruction_counts = {str(row[0]): row[1] for row in ic_result.all()}

            # Convert to schemas
            report_schemas = []
            for report in reports:
                report_schema = ReportSchema.from_orm(report)
                report_schema.user = UserSchema.from_orm(report.user)

                # Manually build data_sources with type computed from connection
                report_schema.data_sources = [
                    DataSourceReportSchema(
                        id=str(ds.id),
                        name=ds.name,
                        organization_id=str(ds.organization_id),
                        created_at=ds.created_at,
                        updated_at=ds.updated_at,
                        context=ds.context,
                        description=ds.description,
                        summary=ds.summary,
                        is_active=ds.is_active,
                        is_public=ds.is_public,
                        owner_user_id=str(ds.owner_user_id) if ds.owner_user_id else None,
                        use_llm_sync=ds.use_llm_sync,
                        # Compute type from first connection
                        type=ds.connections[0].type if ds.connections else None,
                    )
                    for ds in (report.data_sources or [])
                ]

                # Summary counts
                report_schema.query_count = len(report.queries or [])
                report_schema.artifact_count = len(report.artifacts or [])

                # Check for active scheduled prompts
                active_sps = [
                    sp for sp in (report.scheduled_prompts or [])
                    if sp.is_active and sp.deleted_at is None
                ]
                report_schema.has_scheduled_prompts = len(active_sps) > 0
                report_schema.scheduled_prompt_count = len(active_sps)

                # Instruction count (from batch query)
                report_schema.instruction_count = instruction_counts.get(str(report.id), 0)

                # Compute unique artifact modes for this report
                report_schema.artifact_modes = list(set(
                    a.mode for a in (report.artifacts or []) if a.mode
                ))

                # Get thumbnail URL from latest artifact (prefer page mode)
                if report.artifacts:
                    sorted_artifacts = sorted(
                        [a for a in report.artifacts if a.thumbnail_path],
                        key=lambda a: (a.mode != 'page', -a.created_at.timestamp() if a.created_at else 0)
                    )
                    if sorted_artifacts:
                        # thumbnail_path is like "thumbnails/{artifact_id}.png", serve via /thumbnails/{filename}
                        thumb_path = sorted_artifacts[0].thumbnail_path
                        filename = thumb_path.split("/")[-1] if "/" in thumb_path else thumb_path
                        report_schema.thumbnail_url = f"/thumbnails/{filename}"

                report_schemas.append(report_schema)
            span.add_event("report schemas ready")

            # Calculate pagination metadata
            total_pages = (total + limit - 1) // limit  # Ceiling division
            has_next = page < total_pages
            has_prev = page > 1

            from app.schemas.report_schema import PaginationMeta, ReportListResponse

            meta = PaginationMeta(
                total=total,
                page=page,
                limit=limit,
                total_pages=total_pages,
                has_next=has_next,
                has_prev=has_prev
            )
            span.add_event("get_reports done")
            return ReportListResponse(reports=report_schemas, meta=meta)

    async def bulk_archive_reports(
        self,
        db: AsyncSession,
        report_ids: list[str],
        current_user: User,
        organization: Organization,
    ):
        """
        Archive multiple reports in a single operation.
        Only affects regular reports in the current organization that the user owns.
        """
        if not report_ids:
            return {"archived": 0}

        # Only allow archiving reports the user owns within the org and that are not already archived
        stmt = (
            select(Report)
            .where(
                Report.id.in_(report_ids),
                Report.organization_id == organization.id,
                Report.user_id == current_user.id,
                Report.report_type == "regular",
                Report.status != "archived",
            )
        )
        result = await db.execute(stmt)
        reports = result.scalars().all()

        count = 0
        archived_ids = []
        for report in reports:
            report.status = "archived"
            archived_ids.append(str(report.id))
            count += 1

        if count:
            await db.commit()

            # Audit log
            try:
                await audit_service.log(
                    db=db,
                    organization_id=str(organization.id),
                    action="report.bulk_archived",
                    user_id=str(current_user.id),
                    resource_type="report",
                    details={"count": count, "report_ids": archived_ids},
                )
            except Exception:
                pass

        return {"archived": count}

    @staticmethod
    async def _enrich_fork_lineage(db: AsyncSession, report: Report, schema: "ReportSchema"):
        """Populate fork lineage fields on a ReportSchema from the Report model."""
        forked_from_id = getattr(report, "forked_from_id", None)
        if forked_from_id:
            schema.forked_from_id = forked_from_id
            from app.models.user import User
            result = await db.execute(
                select(Report).options(selectinload(Report.user)).where(Report.id == forked_from_id)
            )
            parent = result.scalar_one_or_none()
            if parent:
                schema.forked_from_title = parent.title
                if parent.user:
                    schema.forked_from_user_name = parent.user.name or parent.user.email

    async def _set_slug_for_report(self, db: AsyncSession, report: Report):
        title_slug = report.title.replace(" ", "-").lower()
        title_slug = "".join(e for e in title_slug if e.isalnum() or e == "-")

        _uuid = uuid.uuid4().hex[:4]

        while (await db.execute(select(Report).filter(Report.slug == (title_slug + "-" + _uuid)))).scalar_one_or_none():
            _uuid = uuid.uuid4().hex[:6]
        else:
            title_slug = title_slug + "-" + _uuid
            report.slug = title_slug

    async def _associate_files_with_report(self, db: AsyncSession, report: Report, file_ids: list[str]):
        # Fetch the files asynchronously
        result = await db.execute(select(File).filter(File.id.in_(file_ids)))
        files = result.scalars().all()
        
        report.files.extend(files)

        await db.commit()

        # Create association table entries
        return report

    async def _associate_data_sources_with_report(self, db: AsyncSession, report: Report, data_source_ids: list[str]):
        # Fetch the data sources asynchronously with memberships preloaded
        result = await db.execute(
            select(DataSource)
            .options(selectinload(DataSource.data_source_memberships))
            .filter(DataSource.id.in_(data_source_ids))
        )
        data_sources = result.scalars().all()
        
        report.data_sources.extend(data_sources)
        await db.commit()  

        return report

    async def set_data_sources_for_report(self, db: AsyncSession, report: Report, data_source_ids: list[str]) -> Report:
        """Replace a report's data source associations atomically with the provided ids."""
        from sqlalchemy import delete
        
        # Delete existing associations directly via SQL to avoid ORM state tracking issues
        await db.execute(
            delete(report_data_source_association).where(
                report_data_source_association.c.report_id == report.id
            )
        )
        
        # Expire and refresh the relationship so ORM sees it as empty (avoids MissingGreenlet on lazy load)
        db.expire(report, ["data_sources"])
        await db.refresh(report, ["data_sources"])
        
        if data_source_ids:
            # Load all requested data sources
            result = await db.execute(
                select(DataSource)
                .options(selectinload(DataSource.data_source_memberships))
                .filter(DataSource.id.in_(data_source_ids))
            )
            new_data_sources = result.scalars().all()
            
            # Add new associations
            report.data_sources.extend(new_data_sources)
        
        await db.flush()
        return report
    

    async def _get_report_messages(self, report: Report):
        messages = report.completions

        context = []
        for completion in messages:
            context.append("====== START MESSAGE =======")
            if completion.role == 'user':
                context.append(f"role: {completion.role}\n content: {completion.prompt['content']}")
            else:
                context.append(f"role: {completion.role}\n content: {completion.completion['content']}")
            context.append("====== END MESSAGE =======")

        return "\n".join(context)

    def _parse_cron_expression(self, cron_expression: str) -> dict:
        # Gracefully handle unschedule values
        if not cron_expression:
            return None
        if isinstance(cron_expression, str) and cron_expression.strip().lower() in {"none", "null", "false"}:
            return None

        parts = cron_expression.split()
        if len(parts) == 6:
            second, minute, hour, day, month, day_of_week = parts
            return {
                'second': second,
                'minute': minute,
                'hour': hour,
                'day': day,
                'month': month,
                'day_of_week': day_of_week
            }
        elif len(parts) == 5:
            minute, hour, day, month, day_of_week = parts
            return {
                'minute': minute,
                'hour': hour,
                'day': day,
                'month': month,
                'day_of_week': day_of_week
            }
        else:
            raise ValueError("Invalid cron expression format")
    
    async def scheduled_rerun_report_steps(self, report_id: str, current_user_id: str, organization_id: str):
        from app.dependencies import async_session_maker
        async with async_session_maker() as db:
            # Load current_user and organization here
            current_user = await db.get(User, current_user_id)
            organization = await db.get(Organization, organization_id)

            # Now call rerun_report_steps with the fresh db and loaded objects
            await self.rerun_report_steps(db, report_id, current_user, organization)

    async def set_report_schedule(self, db: AsyncSession, report_id: str, cron_expression: str, current_user: User, organization: Organization, notification_subscribers: list = None) -> Report:
        
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        try:
            # Try to remove existing job if it exists
            scheduler.remove_job(job_id=f"report_{report_id}")
        except JobLookupError:
            # Job doesn't exist yet, that's fine
            pass
        
        # Continue with scheduling the new job (only if a valid cron is provided)
        cron_expression_parsed = self._parse_cron_expression(cron_expression)

        if cron_expression_parsed is not None:
            job = scheduler.add_job(
                func=self.scheduled_rerun_report_steps,
                trigger='cron',
                id=f"report_{report_id}",
                args=[report_id, current_user.id, organization.id],
                replace_existing=True,
                **cron_expression_parsed
            )
            logger.info(f"Scheduled new cron job for report {report_id}: {job}")
            next_run = job.trigger.get_next_fire_time(None, datetime.now(timezone.utc))
            logger.info(f"Next run time: {next_run}")
        
        # Update the cron expression in the report (normalize unschedule values to None)
        report.cron_schedule = None if cron_expression in (None, '', 'None') else cron_expression

        # Persist notification subscribers (clear if unscheduling)
        if cron_expression in (None, '', 'None'):
            report.notification_subscribers = None
        elif notification_subscribers is not None:
            report.notification_subscribers = notification_subscribers

        from sqlalchemy.orm.attributes import flag_modified
        flag_modified(report, "notification_subscribers")

        await db.commit()
        await db.refresh(report)
        # Telemetry: report schedule changed
        try:
            await telemetry.capture(
                "report_scheduled" if cron_expression is not None else "report_unscheduled",
                {
                    "report_id": str(report.id),
                    "status": "scheduled" if cron_expression is not None else "unscheduled",
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.scheduled" if report.cron_schedule else "report.unscheduled",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title, "cron_schedule": report.cron_schedule},
            )
        except Exception:
            pass

        return report

    async def toggle_conversation_share(
        self, 
        db: AsyncSession, 
        report_id: str, 
        current_user: User, 
        organization: Organization
    ) -> dict:
        """Toggle conversation sharing for a report. Generates token if enabling."""
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        # Toggle the enabled state
        new_enabled = not report.conversation_share_enabled
        
        if new_enabled:
            # Generate a new token if enabling and no token exists
            if not report.conversation_share_token:
                report.conversation_share_token = uuid.uuid4().hex
            report.conversation_share_enabled = True
        else:
            # Keep the token but disable sharing (allows re-enabling with same URL)
            report.conversation_share_enabled = False
        
        await db.commit()
        await db.refresh(report)
        
        # Telemetry
        try:
            await telemetry.capture(
                "conversation_share_toggled",
                {
                    "report_id": str(report.id),
                    "enabled": report.conversation_share_enabled,
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Audit log
        try:
            await audit_service.log(
                db=db,
                organization_id=str(organization.id),
                action="report.share_toggled",
                user_id=str(current_user.id),
                resource_type="report",
                resource_id=str(report.id),
                details={"title": report.title, "share_enabled": report.conversation_share_enabled},
            )
        except Exception:
            pass

        return {
            "enabled": report.conversation_share_enabled,
            "token": report.conversation_share_token if report.conversation_share_enabled else None,
        }

    async def get_public_conversation(
        self,
        db: AsyncSession,
        share_token: str,
        limit: int = 10,
        before: str | None = None,
        user=None,
    ) -> dict:
        """Fetch a shared conversation by its token."""
        result = await db.execute(
            select(Report)
            .options(selectinload(Report.user))
            .filter(Report.conversation_share_token == share_token)
        )
        report = result.scalar_one_or_none()

        if not report:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # Check conversation visibility
        await self._check_visibility(db, report, 'conversation_visibility', user)
        
        # Fetch completions for this report
        from app.models.completion import Completion
        from app.models.completion_block import CompletionBlock
        from app.models.plan_decision import PlanDecision
        from app.models.tool_execution import ToolExecution
        
        # Build query with pagination - fetch newest first, then reverse for display
        completions_query = select(Completion).where(Completion.report_id == report.id)
        
        # If 'before' cursor provided, fetch older completions
        if before:
            cursor_result = await db.execute(select(Completion).where(Completion.id == before))
            cursor_completion = cursor_result.scalar_one_or_none()
            if cursor_completion:
                completions_query = completions_query.where(
                    Completion.created_at < cursor_completion.created_at
                )
        
        # Order by newest first, limit, then we'll reverse
        completions_stmt = (
            completions_query
            .order_by(Completion.created_at.desc())
            .limit(limit + 1)  # Fetch one extra to check if there are more
        )
        completions_res = await db.execute(completions_stmt)
        fetched = list(completions_res.scalars().all())
        
        # Check if there are more older completions
        has_more = len(fetched) > limit
        if has_more:
            fetched = fetched[:limit]  # Remove the extra one
        
        # Reverse to get chronological order (oldest first)
        all_completions = list(reversed(fetched))
        
        # Get the cursor for the next page (oldest completion in this batch)
        next_before = all_completions[0].id if all_completions and has_more else None
        
        completion_ids = [c.id for c in all_completions]
        system_completion_ids = [c.id for c in all_completions if c.role == 'system']
        
        # Fetch blocks for system completions
        blocks: list = []
        pd_map: dict = {}
        te_map: dict = {}
        if system_completion_ids:
            blocks_join_stmt = (
                select(CompletionBlock, PlanDecision, ToolExecution)
                .where(CompletionBlock.completion_id.in_(system_completion_ids))
                .outerjoin(PlanDecision, CompletionBlock.plan_decision_id == PlanDecision.id)
                .outerjoin(ToolExecution, CompletionBlock.tool_execution_id == ToolExecution.id)
                .order_by(CompletionBlock.completion_id.asc(), CompletionBlock.block_index.asc())
            )
            join_res = await db.execute(blocks_join_stmt)
            for row in join_res.all():
                b = row[0]
                pd = row[1]
                te = row[2]
                blocks.append(b)
                if pd is not None:
                    pd_map[pd.id] = pd
                if te is not None:
                    te_map[te.id] = te
        
        
        # Build per-completion block lists (sanitized)
        completion_id_to_blocks: dict = {cid: [] for cid in completion_ids}
        for b in blocks:
            pd = pd_map.get(b.plan_decision_id) if b.plan_decision_id else None
            te = te_map.get(b.tool_execution_id) if b.tool_execution_id else None
            
            # Build sanitized block (no internal IDs, no user feedback)
            block_data = {
                "id": b.id,
                "block_index": b.block_index,
                "status": b.status,
                "content": b.content,
                "reasoning": b.reasoning,
                "started_at": b.started_at.isoformat() if b.started_at else None,
                "completed_at": b.completed_at.isoformat() if b.completed_at else None,
            }
            
            if pd:
                block_data["plan_decision"] = {
                    "reasoning": pd.reasoning,
                    "assistant": pd.assistant,
                    "final_answer": pd.final_answer,
                    "analysis_complete": pd.analysis_complete,
                }
            
            if te:
                # Sanitized tool execution - include results but strip internal IDs
                block_data["tool_execution"] = {
                    "id": te.id,
                    "tool_name": te.tool_name,
                    "tool_action": te.tool_action,
                    "status": te.status,
                    "result_summary": te.result_summary,
                    "result_json": te.result_json,
                    "duration_ms": te.duration_ms,
                }
            
            completion_id_to_blocks[b.completion_id].append(block_data)
        
        # Assemble sanitized completions
        sanitized_completions = []
        for c in all_completions:
            c_blocks = completion_id_to_blocks.get(c.id, [])
            c_blocks.sort(key=lambda x: x["block_index"])
            
            completion_data = {
                "id": c.id,
                "role": c.role,
                "status": c.status,
                "prompt": c.prompt if c.role == "user" else None,
                "completion_blocks": c_blocks if c.role == "system" else [],
                "created_at": c.created_at.isoformat() if c.created_at else None,
            }
            sanitized_completions.append(completion_data)
        
        return {
            "report_id": report.id,
            "title": report.title,
            "user_name": report.user.name if report.user else "Unknown",
            "completions": sanitized_completions,
            "created_at": report.created_at.isoformat() if report.created_at else None,
            "has_more": has_more,
            "next_before": next_before,
        }

    async def get_report_summary(
        self, db: AsyncSession, report_id: str
    ) -> dict:
        """Return all query tool executions and instruction mutations for a report,
        independent of completion pagination."""
        from app.models.completion import Completion
        from app.models.completion_block import CompletionBlock
        from app.models.tool_execution import ToolExecution
        from app.schemas.tool_execution_schema import ToolExecutionSchema
        from app.schemas.step_schema import StepSchema
        from app.schemas.report_summary_schema import (
            SummaryToolExecutionSchema,
            SummaryInstructionItem,
        )
        from app.serializers.completion_v2 import (
            _extract_data_source_ids,
            _resolve_data_sources,
        )

        # 1) Fetch all successful tool executions that created steps (queries)
        query_stmt = (
            select(ToolExecution, Completion.id.label("completion_id"))
            .join(CompletionBlock, CompletionBlock.tool_execution_id == ToolExecution.id)
            .join(Completion, Completion.id == CompletionBlock.completion_id)
            .where(
                Completion.report_id == report_id,
                Completion.deleted_at == None,
                ToolExecution.status == "success",
                ToolExecution.created_step_id != None,
            )
            .order_by(CompletionBlock.created_at.asc())
        )
        query_res = await db.execute(query_stmt)
        query_rows = query_res.all()

        # 2) Batch-load steps
        step_ids = {row.ToolExecution.created_step_id for row in query_rows if row.ToolExecution.created_step_id}
        step_map: dict[str, Step] = {}
        if step_ids:
            step_res = await db.execute(select(Step).where(Step.id.in_(list(step_ids))))
            for s in step_res.scalars().all():
                step_map[s.id] = s

        # 3) Batch-resolve data sources
        all_ds_ids: list[str] = []
        te_to_ds_ids: dict[str, list[str]] = {}
        for row in query_rows:
            te = row.ToolExecution
            ds_ids = _extract_data_source_ids(te)
            if ds_ids:
                te_to_ds_ids[te.id] = ds_ids
                all_ds_ids.extend(ds_ids)

        ds_schema_map: dict[str, object] = {}
        if all_ds_ids:
            from app.models.data_source import DataSource as DS
            from app.models.connection import Connection
            from app.models.domain_connection import domain_connection
            from app.schemas.completion_v2_schema import ToolExecutionDataSourceSchema

            ds_rows = await db.execute(
                select(DS.id, DS.name, Connection.type)
                .join(domain_connection, domain_connection.c.data_source_id == DS.id)
                .join(Connection, Connection.id == domain_connection.c.connection_id)
                .where(DS.id.in_(list(set(all_ds_ids))))
            )
            for r in ds_rows:
                ds_id = str(r[0])
                if ds_id not in ds_schema_map:
                    ds_schema_map[ds_id] = ToolExecutionDataSourceSchema(id=ds_id, name=r[1], type=r[2])

        # 4) Build query schemas
        queries: list[SummaryToolExecutionSchema] = []
        seen_steps = set()
        for row in query_rows:
            te = row.ToolExecution
            step_id = te.created_step_id
            if step_id and step_id in seen_steps:
                continue
            if step_id:
                seen_steps.add(step_id)

            base = ToolExecutionSchema.from_orm(te)
            te_data = base.model_dump()
            # Strip heavy payloads
            rj = te_data.get("result_json")
            if isinstance(rj, dict):
                rj.pop("widget_data", None)
            te_data["result_json"] = rj

            created_step_schema = None
            step_obj = step_map.get(step_id) if step_id else None
            if step_obj:
                step_dict = {
                    **step_obj.__dict__,
                    "data_model": getattr(step_obj, "data_model", None) or {},
                    "data": getattr(step_obj, "data", None) or {},
                }
                created_step_schema = StepSchema.model_validate(step_dict)

            ds_list = None
            ds_ids_for_te = te_to_ds_ids.get(te.id)
            if ds_ids_for_te:
                ds_list = [ds_schema_map[did] for did in ds_ids_for_te if did in ds_schema_map] or None

            queries.append(SummaryToolExecutionSchema(
                **te_data,
                created_step=created_step_schema,
                data_sources=ds_list,
            ))

        # 5) Fetch instruction create/edit tool executions
        instr_stmt = (
            select(ToolExecution, Completion.id.label("completion_id"))
            .join(CompletionBlock, CompletionBlock.tool_execution_id == ToolExecution.id)
            .join(Completion, Completion.id == CompletionBlock.completion_id)
            .where(
                Completion.report_id == report_id,
                Completion.deleted_at == None,
                ToolExecution.status == "success",
                ToolExecution.tool_name.in_(["create_instruction", "edit_instruction"]),
            )
            .order_by(CompletionBlock.created_at.asc())
        )
        instr_res = await db.execute(instr_stmt)
        instr_rows = instr_res.all()

        instructions: list[SummaryInstructionItem] = []
        seen_instr_ids: dict[str, int] = {}  # instruction_id -> index in list
        for row in instr_rows:
            te = row.ToolExecution
            completion_id = row.completion_id
            rj = te.result_json or {}
            if not rj.get("success") or not rj.get("instruction_id"):
                continue
            args = te.arguments_json or {}
            text = args.get("text", "")
            instr_id = rj["instruction_id"]
            is_edit = te.tool_name == "edit_instruction"

            existing_idx = seen_instr_ids.get(instr_id)
            title = text.split("\n")[0].replace("#", "").strip()[:60] if text else "Instruction"

            item = SummaryInstructionItem(
                instruction_id=instr_id,
                title=instructions[existing_idx].title if existing_idx is not None else title,
                category=args.get("category", instructions[existing_idx].category if existing_idx is not None else "general"),
                is_edit=is_edit or (instructions[existing_idx].is_edit if existing_idx is not None else False),
                line_count=len([l for l in text.split("\n") if l.strip()]) if text else (instructions[existing_idx].line_count if existing_idx is not None else 0),
                message_id=str(completion_id),
            )

            if existing_idx is not None:
                instructions[existing_idx] = item
            else:
                seen_instr_ids[instr_id] = len(instructions)
                instructions.append(item)

        return {
            "queries": queries,
            "instructions": instructions,
        }