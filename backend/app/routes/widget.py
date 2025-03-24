from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from app.services.widget_service import WidgetService
from app.schemas.widget_schema import WidgetCreate, WidgetUpdate, WidgetSchema
from app.dependencies import get_db, get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from sqlalchemy.ext.asyncio import AsyncSession
import logging
import io
from app.core.permissions_decorator import requires_permission
from app.models.report import Report

router = APIRouter(tags=["widgets"])
widget_service = WidgetService()

#@router.post("/reports/{report_slug}/widgets", response_model=WidgetSchema)
async def create_widget(report_slug: str, widget: WidgetCreate, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await widget_service.create_widget(db, report_slug, widget, current_user, organization)

@router.get("/reports/{report_id}/widgets", response_model=list[WidgetSchema])
@requires_permission('view_widgets', model=Report)
async def get_widgets_by_report(report_id: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await widget_service.get_widgets_by_report(db, report_id, organization, current_user)

@router.get("/reports/{report_id}/widgets/{widget_uuid}", response_model=WidgetSchema)
@requires_permission('view_widgets', model=Report)
async def get_widget_by_id(widget_uuid: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await widget_service.get_widget_by_id(db, widget_uuid, organization, current_user)

@router.put("/reports/{report_id}/widgets/{widget_uuid}", response_model=WidgetUpdate)
@requires_permission('update_widgets', model=Report)
async def update_widget(widget_uuid: str, widget: WidgetUpdate, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await widget_service.update_widget(db, widget_uuid, widget, current_user, organization)

@router.delete("/reports/{report_id}/widgets/{widget_uuid}")
@requires_permission('delete_widgets', model=Report)
async def delete_widget(widget_uuid: str, current_user: User = Depends(current_user), organization: Organization = Depends(get_current_organization), db: AsyncSession = Depends(get_async_db)):
    return await widget_service.delete_widget(db, widget_uuid, current_user, organization)

@router.get("/reports/{report_id}/widgets/{widget_id}/export")
@requires_permission('export_widgets', model=Report)
async def export_widget(
    widget_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    logging.info(f"CSV export request received for widget {widget_id}")
    try:
        csv_data = await widget_service.export_widget_to_csv(db, widget_id)
        logging.info(f"CSV data generated, size: {len(csv_data)} characters")
        
        # Create a StringIO object from the csv_data
        csv_buffer = io.StringIO()
        csv_data.to_csv(csv_buffer, index=False)
        
        response = Response(content=csv_buffer.getvalue(), media_type="text/csv")
        response.headers["Content-Disposition"] = f"attachment; filename={widget_id}.csv"
        return response

    except Exception as e:
        logging.error(f"Error in export_widget route: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error during export: {str(e)}")

@router.get("/r/{report_id}/widgets", response_model=list[WidgetSchema])
async def get_widgets_for_public_report(report_id: str, db: AsyncSession = Depends(get_async_db)):
    return await widget_service.get_widgets_for_public_report(db, report_id)
