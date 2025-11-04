
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.report import Report
from app.schemas.report_schema import ReportCreate, ReportSchema, ReportUpdate
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
from sqlalchemy import select, or_, func
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.base import JobLookupError
from logging import getLogger
import asyncio

from app.core.scheduler import scheduler
from app.models.dashboard_layout_version import DashboardLayoutVersion
from app.services.dashboard_layout_service import DashboardLayoutService
from app.models.visualization import Visualization
from app.models.query import Query
from app.models.step import Step

logger = getLogger(__name__)

class ReportService:

    def __init__(self):
        self.widget_service = WidgetService()
        self.layout_service = DashboardLayoutService()
    
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
                selectinload(Report.data_sources)  # Add this line to load data sources
            )
            .filter(Report.id == report_id)
            .filter(Report.report_type == 'regular')
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
            user=user_schema,
            cron_schedule=report.cron_schedule,
            created_at=report.created_at,
            updated_at=report.updated_at,
            app_version=app_version,
            data_sources=report.data_sources,
            external_platform=report.external_platform,
            theme_name=report.theme_name,
            theme_overrides=report.theme_overrides
        )
        return report_schema

    async def create_report(self, db: AsyncSession, report_data: ReportCreate, current_user: User, organization: Organization) -> ReportSchema:
        # Extract widget data and remove it from report_data
        widget_data = report_data.widget
        del report_data.widget
        file_uuids = report_data.files
        del report_data.files
        data_sources = report_data.data_sources
        del report_data.data_sources

        # Create and persist the report
        report = Report(**report_data.dict())
        # Ensure a default theme is set for new reports
        if getattr(report, 'theme_name', None) in (None, ''):
            report.theme_name = 'default'
        report.user_id = current_user.id
        report.organization_id = organization.id
        await self._set_slug_for_report(db, report)
        db.add(report)
        await db.commit()
        await db.refresh(report)

        # Telemetry: report created (minimal fields only)
        try:
            await telemetry.capture(
                "report_created",
                {
                    "report_id": str(report.id),
                    "status": report.status,
                    "count_files": len(file_uuids or []),
                    "count_data_sources": len(data_sources or [])
                },
                user_id=current_user.id,
                org_id=organization.id,
            )
        except Exception:
            pass

        # Create an empty dashboard layout version for this report
        try:
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
            await db.commit()
        except Exception as e:
            logger.exception("Failed to create initial DashboardLayoutVersion: %s", e)
            # Do not fail report creation on layout init issues
            await db.rollback()

        # Associate files with the report
        await self._associate_files_with_report(db, report, file_uuids)
        await self._associate_data_sources_with_report(db, report, data_sources)

        # Explicitly load the user and widgets relationships to avoid lazy-loading
        await db.refresh(report, ["user", "data_sources", "files"])

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
                    raise HTTPException(status_code=400, detail=f"No step found for visualization {viz_id}")
                if not step.code or not str(step.code).strip():
                    raise HTTPException(status_code=400, detail=f"Step code is empty for visualization {viz_id}; cannot rerun")

                logger.info(f"Running visualization {viz_id} via step {step.id} for report {report_id}")
                await self.widget_service.step_service.rerun_step(db, step.id)
        else:
            # Legacy fallback: rerun last step for each published widget
            published_widgets = await self.widget_service.get_published_widgets_for_report(db, report_id)
            for widget in published_widgets:
                logger.info(f"Running widget {widget.id} for report {report_id}")
                await self.widget_service.run_widget_step(db, widget, current_user, organization)

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
        return report
    
    async def get_public_report(self, db: AsyncSession, report_id: str) -> ReportSchema:
        result = await db.execute(select(Report).filter(Report.id == report_id).filter(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        if report.status != 'published':
            raise HTTPException(status_code=404, detail="Report not found")
        
        schema = ReportSchema.from_orm(report)
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

    async def get_public_layouts(self, db: AsyncSession, report_id: str):
        # Ensure report exists and is published
        result = await db.execute(select(Report).where(Report.id == report_id).where(Report.report_type == 'regular'))
        report = result.scalar_one_or_none()
        if not report or report.status != 'published':
            raise HTTPException(status_code=404, detail="Report not found")

        rows = await db.execute(
            select(DashboardLayoutVersion).where(DashboardLayoutVersion.report_id == report_id).order_by(
                DashboardLayoutVersion.created_at.asc()
            )
        )
        layouts = rows.scalars().all()

        from app.schemas.dashboard_layout_version_schema import DashboardLayoutVersionSchema
        return [DashboardLayoutVersionSchema.from_orm(l) for l in layouts]

    async def get_reports(self, db: AsyncSession, current_user: User, organization: Organization, page: int = 1, limit: int = 10, filter: str = "my"):
        # Calculate offset
        offset = (page - 1) * limit
        
        # Build filter conditions based on filter parameter
        base_conditions = [
            Report.organization_id == organization.id,
            Report.status != 'archived',
            Report.report_type == 'regular',
        ]
        
        if filter == "my":
            # Show only reports owned by current user
            base_conditions.append(Report.user_id == current_user.id)
        elif filter == "published":
            # Show only published reports
            base_conditions.append(Report.status == 'published')
        else:
            # Default: show reports user can view (owned by user OR published)
            base_conditions.append(
                or_(Report.status == 'published', Report.user_id == current_user.id)
            )
        
        # Base query for filtering
        base_query = select(Report).where(*base_conditions)
        
        # Count total items
        count_query = select(func.count(Report.id)).where(*base_conditions)
        
        total_result = await db.execute(count_query)
        total = total_result.scalar()
        
        # Get paginated results
        query = base_query.options(
            selectinload(Report.user), 
            selectinload(Report.widgets)
        ).order_by(Report.created_at.desc()).offset(offset).limit(limit)
        
        result = await db.execute(query)
        reports = result.scalars().all()

        # Convert to schemas
        report_schemas = []
        for report in reports:
            report_schema = ReportSchema.from_orm(report)
            report_schema.user = UserSchema.from_orm(report.user)
            report_schemas.append(report_schema)

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

        return ReportListResponse(reports=report_schemas, meta=meta)

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
        if data_source_ids:
            # Load all requested data sources
            result = await db.execute(
                select(DataSource)
                .options(selectinload(DataSource.data_source_memberships))
                .filter(DataSource.id.in_(data_source_ids))
            )
            new_data_sources = result.scalars().all()
        else:
            new_data_sources = []

        # Replace associations
        report.data_sources = list(new_data_sources)
        await db.commit()
        await db.refresh(report, ["data_sources"])
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

    async def set_report_schedule(self, db: AsyncSession, report_id: str, cron_expression: str, current_user: User, organization: Organization) -> Report:
        
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
        return report