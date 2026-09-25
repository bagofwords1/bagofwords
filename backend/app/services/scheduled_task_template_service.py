"""Enable/disable built-in scheduled-task templates for a user.

The catalog itself is read-only content shipped in code
(``app.schemas.scheduled_task_template_schema``). Enabling an entry creates a
host report server-side (attaching the user's usable data sources) and then a
regular ``ScheduledPrompt`` through the existing service, stamped with
``template_key``. Disabling pauses the row (``is_active=False``) rather than
deleting it, so run history survives and re-enabling resumes the same task.

State is scoped per user (not per org): each user who flips a template on gets
their own report + task, owned and run as them — mirroring how user-created
scheduled tasks already work.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.report import Report
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.user import User
from app.schemas.report_schema import ReportCreate
from app.schemas.scheduled_prompt_schema import ScheduledPromptCreate, ScheduledPromptUpdate
from app.schemas.scheduled_task_template_schema import (
    ScheduledTaskTemplateDefinition,
    ScheduledTaskTemplateEnableRequest,
    ScheduledTaskTemplateState,
    get_scheduled_task_template,
    list_scheduled_task_templates,
)
from app.services.scheduled_prompt_service import scheduled_prompt_service

logger = logging.getLogger(__name__)


class ScheduledTaskTemplateService:

    async def _rows_by_key(
        self, db: AsyncSession, current_user: User, organization: Organization
    ) -> Dict[str, List[ScheduledPrompt]]:
        """template_key -> every live row this user has for it, oldest first.

        Normally one row per key: ``enable`` checks before creating. That check
        is not atomic and there is no unique constraint behind it, so two
        concurrent enables can both win. Returning the full list is what lets
        ``disable`` pause the duplicate too.
        """
        result = await db.execute(
            select(ScheduledPrompt)
            .join(Report, ScheduledPrompt.report_id == Report.id)
            .where(
                and_(
                    Report.organization_id == organization.id,
                    ScheduledPrompt.user_id == current_user.id,
                    ScheduledPrompt.template_key.isnot(None),
                    ScheduledPrompt.deleted_at.is_(None),
                    Report.deleted_at.is_(None),
                )
            )
            .order_by(ScheduledPrompt.created_at.asc())
        )
        rows: Dict[str, List[ScheduledPrompt]] = {}
        for row in result.scalars().all():
            rows.setdefault(row.template_key, []).append(row)
        for key, key_rows in rows.items():
            if len(key_rows) > 1:
                logger.warning(
                    "user %s has %d rows for scheduled-task template %s — "
                    "disabling it will pause them all",
                    current_user.id, len(key_rows), key,
                )
        return rows

    @staticmethod
    def _entry_state(
        template: ScheduledTaskTemplateDefinition,
        rows: Optional[List[ScheduledPrompt]],
    ) -> ScheduledTaskTemplateState:
        state = ScheduledTaskTemplateState(
            key=template.key,
            title=template.title,
            description=template.description,
            prompt_content=template.prompt_content,
            default_cron=template.default_cron,
            spawn_new_report=template.spawn_new_report,
            tags=list(template.tags),
            icon=template.icon,
        )
        if not rows:
            return state
        row = rows[0]
        state.enabled = bool(row.is_active)
        state.paused = not row.is_active
        state.scheduled_prompt_id = str(row.id)
        state.report_id = str(row.report_id)
        state.cron_schedule = row.cron_schedule
        state.duplicate_count = len(rows) - 1
        return state

    async def list_templates(
        self, db: AsyncSession, current_user: User, organization: Organization
    ) -> List[ScheduledTaskTemplateState]:
        rows = await self._rows_by_key(db, current_user, organization)
        return [
            self._entry_state(template, rows.get(template.key))
            for template in list_scheduled_task_templates()
        ]

    @staticmethod
    def _require_entry(key: str) -> ScheduledTaskTemplateDefinition:
        template = get_scheduled_task_template(key)
        if template is None:
            raise HTTPException(status_code=404, detail="Unknown scheduled-task template")
        return template

    async def enable(
        self,
        db: AsyncSession,
        key: str,
        current_user: User,
        organization: Organization,
        options: Optional[ScheduledTaskTemplateEnableRequest] = None,
    ) -> ScheduledTaskTemplateState:
        template = self._require_entry(key)
        rows = (await self._rows_by_key(db, current_user, organization)).get(key)

        if rows:
            if not any(r.is_active for r in rows):
                # Paused → resume the existing row: re-registers the cron job
                # and keeps the run history attached.
                await scheduled_prompt_service.update_scheduled_prompt(
                    db, str(rows[0].id), ScheduledPromptUpdate(is_active=True),
                    current_user, organization,
                )
            # Already active → idempotent no-op.
        else:
            # Import here to avoid a route-level import cycle at module load.
            from app.services.data_source_service import DataSourceService
            from app.services.report_service import ReportService

            # Agent scope: the caller's explicit pick when given, otherwise
            # every data source the user can use. create_report re-filters by
            # permission either way, so a forged id is silently dropped.
            if options and options.data_source_ids is not None:
                ds_ids = [str(i) for i in options.data_source_ids]
            else:
                ds_items = await DataSourceService().get_active_data_sources(
                    db, organization, current_user
                )
                ds_ids = [str(d.id) for d in ds_items]
            report = await ReportService().create_report(
                db,
                ReportCreate(title=template.title, files=[], data_sources=ds_ids),
                current_user,
                organization,
            )
            await scheduled_prompt_service.create_scheduled_prompt(
                db,
                str(report.id),
                ScheduledPromptCreate(
                    prompt={"content": template.prompt_content, "mode": "chat"},
                    title=template.title,
                    cron_schedule=(options.cron_schedule if options and options.cron_schedule else template.default_cron),
                    is_active=True,
                    spawn_new_report=template.spawn_new_report,
                    notification_subscribers=None,
                ),
                current_user,
                organization,
                template_key=template.key,
            )

        # Re-read so a concurrent enable surfaces as duplicate_count instead of
        # being silently hidden.
        fresh = (await self._rows_by_key(db, current_user, organization)).get(key)
        return self._entry_state(template, fresh)

    async def disable(
        self, db: AsyncSession, key: str, current_user: User, organization: Organization
    ) -> ScheduledTaskTemplateState:
        template = self._require_entry(key)
        rows = (await self._rows_by_key(db, current_user, organization)).get(key) or []
        for row in rows:
            if row.is_active:
                await scheduled_prompt_service.update_scheduled_prompt(
                    db, str(row.id), ScheduledPromptUpdate(is_active=False),
                    current_user, organization,
                )
        fresh = (await self._rows_by_key(db, current_user, organization)).get(key)
        return self._entry_state(template, fresh)


scheduled_task_template_service = ScheduledTaskTemplateService()
