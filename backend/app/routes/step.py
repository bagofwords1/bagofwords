from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization
from app.services.step_service import StepService
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
import io
import logging
from app.schemas.step_schema import StepSchema

router = APIRouter(tags=["steps"])
step_service = StepService()

@router.get("/steps/{step_id}/export", response_class=Response)
@requires_permission('view_reports')
async def export_step(
    step_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    logging.info(f"CSV export request received for step {step_id}")
    try:
        df, step = await step_service.export_step_to_csv(db, step_id)
        
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, index=False)
        csv_buffer.seek(0)
        
        response = Response(content=csv_buffer.getvalue(), media_type="text/csv")
        widget_title = "".join(c for c in step.widget.title if c.isalnum() or c in (' ', '_')).rstrip()
        file_name = f"{widget_title}-{step.slug}.csv".replace(" ", "_")
        response.headers["Content-Disposition"] = f"attachment; filename=\"{file_name}\""
        return response

    except ValueError as e:
        logging.warning(f"Value error in export_step route for step {step_id}: {str(e)}")
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logging.error(f"Error in export_step route for step {step_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Internal server error during export: {str(e)}") 


@router.get("/steps/{step_id}", response_model=StepSchema)
@requires_permission('view_reports')
async def get_step(
    step_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    step = await step_service.get_step_by_id(db, step_id)
    if not step:
        raise HTTPException(status_code=404, detail="Step not found")
    return StepSchema.from_orm(step)