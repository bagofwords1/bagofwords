from app.models.widget import Widget
from app.models.report import Report
from app.schemas.widget_schema import WidgetSchema
from app.schemas.step_schema import StepSchema
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.step_service import StepService
from app.models.step import Step
from fastapi import HTTPException
from sqlalchemy import select
from app.models.user import User
from app.models.organization import Organization

class WidgetService:
    def __init__(self):
        self.step_service = StepService()

    async def run_widget_step(self, db: AsyncSession, widget: Widget, current_user: User, organization: Organization) -> WidgetSchema:
        step = await self._get_last_step(db, widget.id)

        if not step:
            raise ValueError("Step not found")
        # Run as the triggering user so user_required connections resolve
        # their credentials (mirrors the artifact rerun path).
        return await self.step_service.rerun_step(db, step.id, current_user=current_user)

    async def get_published_widgets_for_report(self, db_session, report_id: str) -> list[WidgetSchema]:
        # Existence check via the id column only — a bare select(Report) would
        # trigger the mapper-level selectin cascade and hydrate the whole
        # report graph (every step version's data) just to look up widgets.
        report_row = await db_session.execute(select(Report.id).filter(Report.id == report_id))
        if report_row.first() is None:
            raise HTTPException(status_code=404, detail="Report not found")

        widgets = await db_session.execute(select(Widget).filter(Widget.report_id == report_id).filter(Widget.status != 'archived'))
        widgets = widgets.scalars().all()
        return [
            WidgetSchema.from_orm(widget).copy(update={"last_step": await self._get_last_step(db_session, widget.id)})
            for widget in widgets
        ]

    async def _get_org_widget(self, db_session, widget_id: str, organization: Organization) -> Widget:
        """Load a widget only if its report belongs to `organization`; any
        other id is answered like one that does not exist."""
        stmt = select(Widget).join(Report, Report.id == Widget.report_id).filter(Widget.id == widget_id)
        if organization is not None:
            stmt = stmt.filter(Report.organization_id == organization.id)
        widget = (await db_session.execute(stmt)).scalar_one_or_none()
        if widget is None:
            raise HTTPException(status_code=404, detail="Widget not found")
        return widget

    async def get_widget_by_id(self, db_session, widget_id: str, current_user: User, organization: Organization) -> WidgetSchema:
        from app.ai.llm.pii.display import display_redaction
        from app.dependencies import async_session_maker
        widget = await self._get_org_widget(db_session, widget_id, organization)
        async with display_redaction(str(organization.id) if organization else None, async_session_maker):
            return WidgetSchema.from_orm(widget).copy(update={"last_step": await self._get_last_step(db_session, widget.id)})

    async def get_widget_by_id_and_step(self, db_session, widget_id: str, step_id: str, current_user: User, organization: Organization) -> WidgetSchema:
        widget = await self._get_org_widget(db_session, widget_id, organization)

        # The step must be one of this widget's own.
        step = await db_session.execute(
            select(Step).filter(Step.id == step_id, Step.widget_id == widget.id)
        )
        step = step.scalar_one_or_none()
        if step is None:
            raise HTTPException(status_code=404, detail="Step not found")

        step_schema = StepSchema.from_orm(step)
        from app.ai.llm.pii.display import load_and_redact_grid
        from app.dependencies import async_session_maker
        redacted = await load_and_redact_grid(
            step_schema.data, str(organization.id) if organization else None, async_session_maker
        )
        if redacted is not step_schema.data:
            step_schema = step_schema.model_copy(update={"data": redacted})
        return WidgetSchema.from_orm(widget).copy(update={"last_step": step_schema})

    async def _get_last_step(self, db_session: AsyncSession, widget_id: str) -> StepSchema | None:
        last_step = await db_session.execute(select(Step).filter(Step.widget_id == widget_id).order_by(Step.created_at.desc()).limit(1))
        last_step = last_step.scalar_one_or_none()
        if last_step:
            # Ensure data and data_model are dictionaries, defaulting to empty dict if None
            # (maintenance service purges these fields for old steps)
            from app.ai.llm.pii.display import redact_grid_display
            step_dict = {
                **last_step.__dict__,
                'data': redact_grid_display(last_step.data or {}),
                'data_model': last_step.data_model or {}
            }
            return StepSchema.model_validate(step_dict)
        return None