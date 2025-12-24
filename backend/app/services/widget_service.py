from app.models.widget import Widget
from app.models.report import Report
from app.schemas.widget_schema import WidgetCreate, WidgetUpdate, WidgetSchema
from app.schemas.step_schema import StepSchema
from sqlalchemy.orm import Session
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.step_service import StepService
from app.models.step import Step
import uuid
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select
import pandas as pd
import io
import logging
import csv
from app.models.user import User
from app.models.organization import Organization

class WidgetService:
    def __init__(self):
        self.step_service = StepService()

    async def create_widget(self, db: AsyncSession, report_slug: str, widget_data: WidgetCreate, current_user: User, organization: Organization) -> WidgetSchema:
        report = await db.execute(select(Report).filter(Report.slug == report_slug))
        report = report.scalar_one_or_none()

        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        messages = widget_data.messages
        new_message = widget_data.new_message

        del widget_data.messages
        del widget_data.new_message

        widget = Widget(report_id=report.id, **widget_data.dict())
        self._set_widget_slug(db, widget)
        self._set_widget_as_published(db, widget)

        db.add(widget)
        await db.commit()
        await db.refresh(widget)

        # Create the first step associated with this widget
        # step_schema = self.step_service.create_step(db, widget.id, new_message=new_message, messages=messages)
        # db.refresh(widget)

        w = WidgetSchema(title=widget.title,slug=widget.slug,status=widget.status,x=widget.x,y=widget.y,width=widget.width,height=widget.height)
                    
        return w
    
    async def run_widget_step(self, db: AsyncSession, widget: Widget, current_user: User, organization: Organization) -> WidgetSchema:
        step = await self._get_last_step(db, widget.id) 

        if not step:
            raise ValueError("Step not found")
        return await self.step_service.rerun_step(db, step.id)

    async def get_widgets_by_report(self, db_session, report_id: str, current_user: User, organization: Organization) -> list[WidgetSchema]:
        report = await db_session.execute(select(Report).filter(Report.id == report_id))
        report = report.scalar_one_or_none()
        widgets = await db_session.execute(select(Widget).filter(Widget.report_id == report.id).filter(Widget.status != 'archived'))
        widgets = widgets.scalars().all()
        return [
            WidgetSchema.from_orm(widget).copy(update={"last_step": await self._get_last_step(db_session, widget.id)})
            for widget in widgets
        ]
    
    async def get_widgets_for_public_report(self, db_session, report_id: str) -> list[WidgetSchema]:
        report = await db_session.execute(select(Report).filter(Report.id == report_id))
        report = report.scalar_one_or_none()
        if report.status != 'published':
            raise HTTPException(status_code=404, detail="Report not found")

        widgets = await db_session.execute(select(Widget).filter(Widget.report_id == report.id).filter(Widget.status != 'archived'))
        widgets = widgets.scalars().all()
        return [
            WidgetSchema.from_orm(widget).copy(update={"last_step": await self._get_last_step(db_session, widget.id)})
            for widget in widgets
        ]
    
    async def get_published_widgets_for_report(self, db_session, report_id: str) -> list[WidgetSchema]:
        report = await db_session.execute(select(Report).filter(Report.id == report_id))
        report = report.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")

        widgets = await db_session.execute(select(Widget).filter(Widget.report_id == report.id).filter(Widget.status != 'archived'))
        widgets = widgets.scalars().all()
        return [
            WidgetSchema.from_orm(widget).copy(update={"last_step": await self._get_last_step(db_session, widget.id)})
            for widget in widgets
        ]

    async def get_widget_by_id(self, db_session, widget_id: str, current_user: User, organization: Organization) -> WidgetSchema:
        widget = await db_session.execute(select(Widget).filter(Widget.id == widget_id))
        widget = widget.scalar_one_or_none()
        return WidgetSchema.from_orm(widget).copy(update={"last_step": await self._get_last_step(db_session, widget.id)})

    async def update_widget(self, db_session, widget_id, widget_data: WidgetUpdate, current_user: User, organization: Organization):
        widget = await db_session.execute(select(Widget).filter(Widget.id == widget_id))
        widget = widget.scalar_one_or_none()
        if widget:
            for key, value in widget_data.dict().items():
                # Skip height/width if they are 0
                if key in ['height', 'width'] and value == 0:
                    continue
                if value is not None:
                   setattr(widget, key, value)
            await db_session.commit()
            await db_session.refresh(widget)
        return widget

    async def delete_widget(self, db_session, widget_id: str, current_user: User, organization: Organization):
        widget = await db_session.execute(select(Widget).filter(Widget.id == widget_id))
        widget = widget.scalar_one_or_none()
        if widget:
            widget.status = 'draft'
            await db_session.commit()
            await db_session.refresh(widget)
        return widget
    
    async def get_widget_by_id_and_step(self, db_session, widget_id: str, step_id: str, current_user: User, organization: Organization) -> WidgetSchema:
        widget = await db_session.execute(select(Widget).filter(Widget.id == widget_id))
        widget = widget.scalar_one_or_none()

        step = await db_session.execute(select(Step).filter(Step.id == step_id))
        step = step.scalar_one_or_none()

        return WidgetSchema.from_orm(widget).copy(update={"last_step": StepSchema.from_orm(step)})

    async def export_widget_to_csv(self, db_session, widget_id: str, current_user: User, organization: Organization) -> str:
        logging.info(f"Starting CSV export for widget {widget_id}")
        try:
            widget = await db_session.execute(select(Widget).filter(Widget.id == widget_id))
            widget = widget.scalar_one_or_none()
            if not widget:
                logging.error(f"Widget {widget_id} not found")
                raise ValueError(f"Widget {widget_id} not found")
            
            last_step = await self._get_last_step(db_session, widget.id)
            logging.info(f"Got last step: {last_step}")
            
            # read the last_step.data[rows] and columns as df
            columns = last_step.data['columns']
            rows = last_step.data['rows']
            df = pd.DataFrame(rows, columns=[col['headerName'] for col in columns])
            
            return df

        except Exception as e:
            logging.error(f"Error during CSV export: {str(e)}")
            raise


    async def _set_widget_slug(self, db: AsyncSession, widget: Widget):
        title_slug = widget.title.replace(" ", "-").lower()
        title_slug = "".join(e for e in title_slug if e.isalnum() or e == "-")

        _uuid = uuid.uuid4().hex[:4]

        while (await db.execute(select(Report).filter(Report.slug == (title_slug + "-" + _uuid)))).scalar_one_or_none():
            _uuid = uuid.uuid4().hex[:6]
        else:
            title_slug = title_slug + "-" + _uuid
            widget.slug = title_slug
    
    async def _set_widget_as_published(self, db: AsyncSession, widget: Widget):
        widget.status = 'published'

    async def _get_last_step(self, db_session: AsyncSession, widget_id: str) -> StepSchema | None:
        last_step = await db_session.execute(select(Step).filter(Step.widget_id == widget_id).order_by(Step.created_at.desc()).limit(1))
        last_step = last_step.scalar_one_or_none()
        if last_step:
            # Ensure data_model is a dictionary, defaulting to an empty dict if None
            step_dict = {
                **last_step.__dict__,
                'data_model': last_step.data_model or {}
            }
            return StepSchema.model_validate(step_dict)
        return None