from app.schemas.text_widget_schema import TextWidgetCreate, TextWidgetUpdate, TextWidgetSchema
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from fastapi import HTTPException
from typing import List
from app.models.text_widget import TextWidget
from app.models.report import Report
from app.models.user import User
from app.models.organization import Organization
from app.services.dashboard_layout_service import DashboardLayoutService

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
        """Delete a text widget if it exists and scrub dangling layout references.

        This operation is idempotent: if the text widget does not exist, it still
        succeeds after ensuring the active and historical layouts no longer
        reference the given id.
        """
        result = await db.execute(
            select(TextWidget).filter(TextWidget.id == text_widget_id, TextWidget.report_id == report_id)
        )
        text_widget = result.scalar_one_or_none()

        if text_widget:
            await db.delete(text_widget)
            await db.commit()

        # Regardless of existence, remove any dangling block references
        try:
            layout_service = DashboardLayoutService()
            await layout_service.remove_blocks_for_text_widget(db, report_id, text_widget_id)
        except Exception:
            # Do not fail the delete due to layout cleanup issues
            logger.warning(
                "Failed to fully scrub text_widget %s from layouts for report %s",
                text_widget_id,
                report_id,
            )
        # Return None to indicate success
        return None

    
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
