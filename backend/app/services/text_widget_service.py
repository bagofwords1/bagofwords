from app.schemas.text_widget_schema import TextWidgetCreate, TextWidgetUpdate, TextWidgetSchema
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
from typing import List
from app.models.text_widget import TextWidget
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization

import logging

logger = logging.getLogger(__name__)

class TextWidgetService:
    def __init__(self):
        pass


    async def create_text_widget(self, db: AsyncSession, report_id: str, text_widget_data: TextWidgetCreate, current_user: User, organization: Organization) -> TextWidgetSchema:
        report = await db.execute(select(Report).filter(Report.id == report_id))
        report = report.scalar_one_or_none()
        if not report:
            raise HTTPException(status_code=404, detail="Report not found")
        text_widget = TextWidget(report_id=report.id, **text_widget_data.dict())
        db.add(text_widget)
        await db.commit()
        return TextWidgetSchema.from_orm(text_widget)


    async def update_text_widget(
        self,
        db: AsyncSession,
        report_id: str,
        text_widget_id: str,
        text_widget_data: TextWidgetUpdate,
        current_user: User,
        organization: Organization
    ) -> TextWidgetSchema:
        text_widget = await db.execute(
            select(TextWidget).filter(
                TextWidget.id == text_widget_id,
                TextWidget.report_id == report_id
            )
        )
        text_widget = text_widget.scalar_one_or_none()
        if not text_widget:
            raise HTTPException(status_code=404, detail="Text widget not found")

        # Update all non-None fields from the update data
        for key, value in text_widget_data.dict(exclude_unset=True).items():
            if value is not None:
                setattr(text_widget, key, value)

        await db.commit()
        return TextWidgetSchema.from_orm(text_widget)
    async def delete_text_widget(self, db: AsyncSession, report_id: str, text_widget_id: str, current_user: User, organization: Organization) -> None:
        text_widget = await db.execute(select(TextWidget).filter(TextWidget.id == text_widget_id, TextWidget.report_id == report_id))
        text_widget = text_widget.scalar_one_or_none()
        if not text_widget:
            raise HTTPException(status_code=404, detail="Text widget not found")
        await db.delete(text_widget)
        await db.commit()

    
    async def get_text_widget(self, db: AsyncSession, report_id: str, text_widget_id: str, current_user: User, organization: Organization) -> TextWidgetSchema:
        pass


    async def get_text_widgets(self, db: AsyncSession, report_id: str, current_user: User, organization: Organization) -> List[TextWidgetSchema]:
        text_widgets = await db.execute(select(TextWidget).filter(TextWidget.report_id == report_id))
        text_widgets = text_widgets.scalars().all()
        return [TextWidgetSchema.from_orm(text_widget) for text_widget in text_widgets]

    async def get_text_widgets_for_public_report(self, db: AsyncSession, report_id: str) -> List[TextWidgetSchema]:
        report = await db.execute(select(Report).filter(Report.id == report_id))
        report = report.scalar_one_or_none()
        if report.status != 'published':
            raise HTTPException(status_code=404, detail="Report not found")

        text_widgets = await db.execute(select(TextWidget).filter(TextWidget.report_id == report.id, TextWidget.status == 'published'))
        text_widgets = text_widgets.scalars().all()
        return [TextWidgetSchema.from_orm(text_widget) for text_widget in text_widgets]
