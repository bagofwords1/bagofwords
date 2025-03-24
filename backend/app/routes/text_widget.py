from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_db, get_async_db
from app.dependencies import get_current_organization

from typing import List
from app.services.text_widget_service import TextWidgetService

from app.schemas.text_widget_schema import TextWidgetSchema, TextWidgetCreate, TextWidgetUpdate
from app.models.user import User

from app.core.auth import current_user
from app.models.organization import Organization
from app.core.permissions_decorator import requires_permission
from app.models.report import Report

router = APIRouter(tags=["text widgets"])
text_widget_service = TextWidgetService()

@router.post("/reports/{report_id}/text_widgets", response_model=TextWidgetSchema)
@requires_permission('create_text_widgets', model=Report)
async def create_text_widget(report_id: str, text_widget: TextWidgetCreate, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await text_widget_service.create_text_widget(db, report_id, text_widget, current_user, organization)

@router.get("/reports/{report_id}/text_widgets", response_model=List[TextWidgetSchema])
@requires_permission('view_text_widgets', model=Report)
async def get_text_widgets(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await text_widget_service.get_text_widgets(db, report_id, current_user, organization)

@router.get("/reports/{report_id}/text_widgets/{text_widget_id}", response_model=TextWidgetSchema)
@requires_permission('view_text_widgets', model=Report)
async def get_text_widget(report_id: str, text_widget_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await text_widget_service.get_text_widget(db, report_id, text_widget_id, current_user, organization)

@router.put("/reports/{report_id}/text_widgets/{text_widget_id}", response_model=TextWidgetSchema)
@requires_permission('update_text_widgets', model=Report)
async def update_text_widget(
    report_id: str, 
    text_widget_id: str, 
    text_widget: TextWidgetUpdate, 
    current_user: User = Depends(current_user), 
    db: AsyncSession = Depends(get_async_db), 
    organization: Organization = Depends(get_current_organization)
):
    return await text_widget_service.update_text_widget(
        db, 
        report_id, 
        text_widget_id, 
        text_widget, 
        current_user, 
        organization
    )

@router.delete("/reports/{report_id}/text_widgets/{text_widget_id}")
@requires_permission('delete_text_widgets', model=Report)
async def delete_text_widget(report_id: str, text_widget_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await text_widget_service.delete_text_widget(db, report_id, text_widget_id, current_user, organization)

@router.get("/r/{report_id}/text_widgets", response_model=list[TextWidgetSchema])
async def get_widgets_for_public_report(report_id: str, db: AsyncSession = Depends(get_async_db)):
    return await text_widget_service.get_text_widgets_for_public_report(db, report_id)
