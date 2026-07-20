from fastapi import APIRouter, Depends, HTTPException, Response, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db, get_current_organization
from app.services.step_service import StepService
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.ee.audit.service import audit_service
import io
import logging
from urllib.parse import quote
from app.schemas.step_schema import StepSchema

router = APIRouter(tags=["steps"])
step_service = StepService()

# (maintype/subtype, file extension) per supported export format.
_EXPORT_FORMATS = {
    "csv": ("text/csv", "csv"),
    "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
}

@router.get("/steps/{step_id}/export", response_class=Response)
@requires_permission('view_reports')
async def export_step(
    step_id: str,
    request: Request,
    format: str = Query("csv", description="Export format: 'csv' or 'xlsx'"),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db)
):
    fmt = (format or "csv").lower()
    if fmt not in _EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported export format '{format}'. Use 'csv' or 'xlsx'.")

    logging.info(f"{fmt.upper()} export request received for step {step_id}")
    try:
        df, step = await step_service.export_step_to_csv(db, step_id)

        if fmt == "xlsx":
            buffer = io.BytesIO()
            # openpyxl is the default engine (declared in pyproject); Excel reads
            # Unicode natively so no BOM/encoding handling is needed here.
            df.to_excel(buffer, index=False)
            content = buffer.getvalue()
        else:
            # utf-8-sig prepends a BOM so Excel auto-detects UTF-8 and renders
            # non-ASCII headers/values (e.g. Hebrew) correctly instead of ANSI mojibake.
            content = df.to_csv(index=False).encode("utf-8-sig")

        media_type, extension = _EXPORT_FORMATS[fmt]

        try:
            await audit_service.log(
                db=db,
                organization_id=organization.id,
                action="data.exported",
                user_id=current_user.id,
                resource_type="step",
                resource_id=step_id,
                details={"format": fmt, "row_count": len(df)},
                request=request,
            )
        except Exception:
            pass

        response = Response(content=content, media_type=media_type)
        widget_title = "".join(c for c in step.widget.title if c.isalnum() or c in (' ', '_')).rstrip()
        file_name = f"{widget_title}-{step.slug}.{extension}".replace(" ", "_")
        # HTTP headers are latin-1 only, so a Unicode (e.g. Hebrew) title would
        # crash the response. Emit an ASCII fallback plus an RFC 6266 UTF-8
        # filename* so clients still get the original name.
        ascii_name = file_name.encode("ascii", "ignore").decode("ascii").strip("-_") or f"export.{extension}"
        response.headers["Content-Disposition"] = (
            f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(file_name)}"
        )
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
    schema = StepSchema.from_orm(step)
    # Redact PII from the full result grid for display (stored data untouched).
    from app.ai.llm.pii.display import load_and_redact_grid
    from app.dependencies import async_session_maker
    redacted = await load_and_redact_grid(
        schema.data, str(organization.id) if organization else None, async_session_maker
    )
    if redacted is not schema.data:
        schema = schema.model_copy(update={"data": redacted})
    return schema