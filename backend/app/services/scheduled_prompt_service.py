import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException

from app.models.scheduled_prompt import ScheduledPrompt
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization
from app.schemas.scheduled_prompt_schema import ScheduledPromptCreate, ScheduledPromptUpdate
from app.core.scheduler import scheduler
from app.services.notification_service import notification_service
from app.settings.config import settings

from apscheduler.jobstores.base import JobLookupError

logger = logging.getLogger(__name__)


def _parse_cron_expression(cron_expression: str) -> Optional[dict]:
    """Parse a cron expression into APScheduler kwargs. Supports 5 or 6 fields."""
    if not cron_expression:
        return None
    if isinstance(cron_expression, str) and cron_expression.strip().lower() in {"none", "null", "false"}:
        return None

    parts = cron_expression.split()
    if len(parts) == 6:
        second, minute, hour, day, month, day_of_week = parts
        return {'second': second, 'minute': minute, 'hour': hour, 'day': day, 'month': month, 'day_of_week': day_of_week}
    elif len(parts) == 5:
        minute, hour, day, month, day_of_week = parts
        return {'minute': minute, 'hour': hour, 'day': day, 'month': month, 'day_of_week': day_of_week}
    else:
        return None


class ScheduledPromptService:

    def _job_id(self, sp_id: str) -> str:
        return f"scheduled_prompt_{sp_id}"

    async def create_scheduled_prompt(
        self,
        db: AsyncSession,
        report_id: str,
        data: ScheduledPromptCreate,
        current_user: User,
        organization: Organization,
    ) -> ScheduledPrompt:
        # Verify report exists and belongs to user
        result = await db.execute(select(Report).filter(Report.id == report_id))
        report = result.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        # Parse cron to validate early
        cron_params = _parse_cron_expression(data.cron_schedule)
        if cron_params is None:
            raise HTTPException(status_code=400, detail="Invalid cron schedule")

        subscribers = [s.model_dump() for s in data.notification_subscribers] if data.notification_subscribers else None

        sp = ScheduledPrompt(
            report_id=report_id,
            user_id=current_user.id,
            prompt=data.prompt,
            cron_schedule=data.cron_schedule,
            is_active=True,
            notification_subscribers=subscribers,
        )
        db.add(sp)
        await db.commit()
        await db.refresh(sp)

        # Register APScheduler job
        self._register_job(sp)

        logger.info(f"Created scheduled prompt {sp.id} for report {report_id}")
        return sp

    async def update_scheduled_prompt(
        self,
        db: AsyncSession,
        scheduled_prompt_id: str,
        data: ScheduledPromptUpdate,
        current_user: User,
        organization: Organization,
    ) -> ScheduledPrompt:
        sp = await self._get_or_404(db, scheduled_prompt_id)

        if data.prompt is not None:
            sp.prompt = data.prompt
        if data.cron_schedule is not None:
            cron_params = _parse_cron_expression(data.cron_schedule)
            if cron_params is None:
                raise HTTPException(status_code=400, detail="Invalid cron schedule")
            sp.cron_schedule = data.cron_schedule
        if data.is_active is not None:
            sp.is_active = data.is_active
        if data.notification_subscribers is not None:
            sp.notification_subscribers = [s.model_dump() for s in data.notification_subscribers]

        await db.commit()
        await db.refresh(sp)

        # Re-register or remove job
        self._remove_job(sp.id)
        if sp.is_active:
            self._register_job(sp)

        logger.info(f"Updated scheduled prompt {sp.id}")
        return sp

    async def delete_scheduled_prompt(
        self,
        db: AsyncSession,
        scheduled_prompt_id: str,
        current_user: User,
        organization: Organization,
    ) -> None:
        sp = await self._get_or_404(db, scheduled_prompt_id)
        sp.deleted_at = datetime.utcnow()
        await db.commit()
        self._remove_job(sp.id)
        logger.info(f"Deleted scheduled prompt {sp.id}")

    async def list_scheduled_prompts(
        self,
        db: AsyncSession,
        report_id: str,
    ) -> List[ScheduledPrompt]:
        result = await db.execute(
            select(ScheduledPrompt)
            .filter(ScheduledPrompt.report_id == report_id)
            .filter(ScheduledPrompt.deleted_at == None)
            .order_by(ScheduledPrompt.created_at.asc())
        )
        return list(result.scalars().all())

    async def list_all_scheduled_prompts(
        self,
        db: AsyncSession,
        organization_id: str,
        page: int = 1,
        limit: int = 20,
        search: str = None,
        filter: str = 'my',
        current_user_id: str = None,
    ) -> dict:
        """List all scheduled prompts across all reports for an organization."""
        from sqlalchemy import func
        from sqlalchemy.orm import joinedload

        query = (
            select(ScheduledPrompt)
            .join(Report, ScheduledPrompt.report_id == Report.id)
            .filter(Report.organization_id == organization_id)
            .filter(ScheduledPrompt.deleted_at == None)
            .filter(Report.deleted_at == None)
            .options(joinedload(ScheduledPrompt.report), joinedload(ScheduledPrompt.user))
        )

        if filter == 'my' and current_user_id:
            query = query.filter(ScheduledPrompt.user_id == current_user_id)
        elif filter == 'shared' and current_user_id:
            query = query.filter(ScheduledPrompt.user_id != current_user_id)

        if search:
            search_term = f"%{search}%"
            query = query.filter(
                Report.title.ilike(search_term)
            )

        # Count total
        count_query = select(func.count()).select_from(query.subquery())
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        total_pages = max(1, (total + limit - 1) // limit)

        # Fetch page
        query = query.order_by(ScheduledPrompt.created_at.desc())
        query = query.offset((page - 1) * limit).limit(limit)
        result = await db.execute(query)
        prompts = list(result.unique().scalars().all())

        return {
            "prompts": prompts,
            "meta": {
                "total": total,
                "page": page,
                "limit": limit,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1,
            }
        }

    async def get_scheduled_prompt(
        self,
        db: AsyncSession,
        scheduled_prompt_id: str,
    ) -> ScheduledPrompt:
        return await self._get_or_404(db, scheduled_prompt_id)

    # ---- Execution (called by APScheduler, no HTTP context) ----

    async def scheduled_run_prompt(self, scheduled_prompt_id: str):
        """Execute a scheduled prompt. Called by APScheduler cron trigger."""
        from app.dependencies import async_session_maker
        from app.services.completion_service import CompletionService
        completion_service = CompletionService()
        from app.schemas.completion_v2_schema import CompletionCreate
        from app.schemas.completion_schema import PromptSchema

        async with async_session_maker() as db:
            sp = await db.get(ScheduledPrompt, scheduled_prompt_id)
            if not sp or sp.deleted_at or not sp.is_active:
                return

            user = await db.get(User, sp.user_id)
            if not user:
                logger.error(f"Scheduled prompt {sp.id}: user {sp.user_id} not found")
                return

            report = await db.get(Report, sp.report_id)
            if not report:
                logger.error(f"Scheduled prompt {sp.id}: report {sp.report_id} not found")
                return

            organization = await db.get(Organization, report.organization_id)
            if not organization:
                logger.error(f"Scheduled prompt {sp.id}: organization not found")
                return

            response = None
            try:
                prompt_data = PromptSchema(**sp.prompt)
                response = await completion_service.create_completion(
                    db=db,
                    report_id=report.id,
                    completion_data=CompletionCreate(prompt=prompt_data),
                    current_user=user,
                    organization=organization,
                    background=False,
                    scheduled_prompt_id=sp.id,
                )
            except Exception as e:
                logger.error(f"Scheduled prompt {sp.id} execution failed: {e}")

            # Update last_run_at
            sp.last_run_at = datetime.utcnow()
            await db.commit()

            # Build execution summary from response
            exec_summary = self._build_execution_summary(response)

            # Send notification
            if sp.notification_subscribers:
                base_url = getattr(settings.bow_config, 'base_url', 'http://localhost:3000') if settings.bow_config else 'http://localhost:3000'
                report_url = f"{base_url}/reports/{report.id}"
                asyncio.create_task(
                    notification_service.send_scheduled_prompt_results(
                        report_id=report.id,
                        report_title=report.title or "Untitled Report",
                        subscribers=sp.notification_subscribers,
                        report_url=report_url,
                        exec_summary=exec_summary,
                    )
                )

    def _build_execution_summary(self, response) -> dict:
        """Extract execution stats from CompletionsV2Response."""
        summary = {
            "iterations": 0,
            "queries": 0,
            "artifacts": 0,
            "last_content": None,
        }
        if not response:
            return summary

        for c in (response.completions or []):
            if c.role != 'system':
                continue
            blocks = c.completion_blocks or []
            summary["iterations"] = len(blocks)
            for b in blocks:
                te = getattr(b, 'tool_execution', None)
                if not te:
                    continue
                tool_name = getattr(te, 'tool_name', '')
                status = getattr(te, 'status', '')
                if tool_name == 'create_data' and status == 'success' and getattr(te, 'created_step_id', None):
                    summary["queries"] += 1
                if tool_name in ('create_artifact', 'edit_artifact') and status == 'success':
                    summary["artifacts"] += 1
            # Get the last block's content as summary text
            for b in reversed(blocks):
                content = getattr(b, 'content', None)
                if content:
                    summary["last_content"] = content
                    break

        return summary

    # ---- Startup re-registration ----

    async def register_all_jobs(self):
        """Re-register APScheduler jobs for all active scheduled prompts. Call on app startup."""
        from app.dependencies import async_session_maker

        async with async_session_maker() as db:
            result = await db.execute(
                select(ScheduledPrompt)
                .filter(ScheduledPrompt.is_active == True)
                .filter(ScheduledPrompt.deleted_at == None)
            )
            prompts = result.scalars().all()

            for sp in prompts:
                try:
                    self._register_job(sp)
                    logger.info(f"Re-registered job for scheduled prompt {sp.id}")
                except Exception as e:
                    logger.error(f"Failed to register job for scheduled prompt {sp.id}: {e}")

            logger.info(f"Registered {len(prompts)} scheduled prompt jobs on startup")

    # ---- Internal helpers ----

    def _register_job(self, sp: ScheduledPrompt):
        cron_params = _parse_cron_expression(sp.cron_schedule)
        if cron_params is None:
            return
        scheduler.add_job(
            func=self.scheduled_run_prompt,
            trigger='cron',
            id=self._job_id(sp.id),
            args=[sp.id],
            replace_existing=True,
            **cron_params,
        )

    def _remove_job(self, sp_id: str):
        try:
            scheduler.remove_job(job_id=self._job_id(sp_id))
        except JobLookupError:
            pass

    async def _get_or_404(self, db: AsyncSession, sp_id: str) -> ScheduledPrompt:
        result = await db.execute(
            select(ScheduledPrompt)
            .filter(ScheduledPrompt.id == sp_id)
            .filter(ScheduledPrompt.deleted_at == None)
        )
        sp = result.scalar_one_or_none()
        if not sp:
            raise HTTPException(status_code=404, detail="Scheduled prompt not found")
        return sp


scheduled_prompt_service = ScheduledPromptService()
