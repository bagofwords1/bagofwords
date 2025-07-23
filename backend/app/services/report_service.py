
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.report import Report
from app.schemas.report_schema import ReportCreate, ReportSchema
from app.services.widget_service import WidgetService
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

logger = getLogger(__name__)

class ReportService:

    def __init__(self):
        self.widget_service = WidgetService()
        

    async def get_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> ReportSchema:
        result = await db.execute(
            select(Report)
            .options(
                selectinload(Report.user),  # Use selectinload for async loading
                selectinload(Report.data_sources)  # Add this line to load data sources
            )
            .filter(Report.id == report_id)
        )
        report = result.unique().scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        user_schema = UserSchema.from_orm(report.user)

        report_schema = ReportSchema(
            id=report.id,
            title=report.title,
            status=report.status,
            slug=report.slug,
            user=user_schema,
            cron_schedule=report.cron_schedule,
            created_at=report.created_at,
            updated_at=report.updated_at,
            data_sources=report.data_sources,
            external_platform=report.external_platform
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
        report.user_id = current_user.id
        report.organization_id = organization.id
        await self._set_slug_for_report(db, report)
        db.add(report)
        await db.commit()
        await db.refresh(report)

        # Associate files with the report
        await self._associate_files_with_report(db, report, file_uuids)
        await self._associate_data_sources_with_report(db, report, data_sources)

        # Explicitly load the user and widgets relationships to avoid lazy-loading
        await db.refresh(report, ["user", "data_sources", "files"])

        return ReportSchema.from_orm(report).copy(update={"user": UserSchema.from_orm(current_user)})

    async def update_report(self, db: AsyncSession, report_id: str, report_data: ReportCreate, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        report.title = report_data.title
        if report_data.status:
            report.status = report_data.status 
        await self._set_slug_for_report(db, report)

        await db.commit()
        await db.refresh(report)
        return report
    
    async def rerun_report_steps(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> Report:
        logger.info(f"Executing scheduled report run for report_id: {report_id}")
        report = await self.get_report(db, report_id, current_user, organization)
        published_widgets = await self.widget_service.get_published_widgets_for_report(db, report_id)
        for widget in published_widgets:
            logger.info(f"Running widget {widget.id} for report {report_id}")
            await self.widget_service.run_widget_step(db, widget, current_user, organization)
        
        logger.info(f"Completed scheduled report run for report_id: {report_id}")
        return report

    async def archive_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        report.status = 'archived'
        await db.commit()
        await db.refresh(report)
        return report

    async def publish_report(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> Report:
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        if report.status == 'published':
            report.status = 'draft'
        else:
            report.status = 'published'

        await db.commit()
        await db.refresh(report)
        return report
    
    async def get_public_report(self, db: AsyncSession, report_id: str) -> ReportSchema:
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        
        if report.status != 'published':
            raise HTTPException(status_code=404, detail="Report not found")
        
        return ReportSchema.from_orm(report)

    async def get_reports(self, db: AsyncSession, current_user: User, organization: Organization, page: int = 1, limit: int = 10, filter: str = "my"):
        # Calculate offset
        offset = (page - 1) * limit
        
        # Build filter conditions based on filter parameter
        base_conditions = [
            Report.organization_id == organization.id,
            Report.status != 'archived'
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
        # Fetch the data sources asynchronously
        result = await db.execute(select(DataSource).filter(DataSource.id.in_(data_source_ids)))
        data_sources = result.scalars().all()
        
        report.data_sources.extend(data_sources)
        await db.commit()  

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
        if 'None' in cron_expression:
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
        
        # Continue with scheduling the new job
        cron_expression_parsed = self._parse_cron_expression(cron_expression)

        if cron_expression is not None:
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
        
        # Update the cron expression in the report
        report.cron_schedule = cron_expression
        
        await db.commit()
        await db.refresh(report)
        return report