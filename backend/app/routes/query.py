from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user as current_user_dep
from app.core.permissions_decorator import requires_permission

from app.models.user import User
from app.models.organization import Organization
from app.models.query import Query
from app.schemas.query_schema import QueryCreate, QuerySchema, QueryRunRequest
from app.services.query_service import QueryService


router = APIRouter(prefix="/queries", tags=["queries"])
service = QueryService()


@router.get("", response_model=list[QuerySchema])
@requires_permission('view_reports')
async def list_queries(
    report_id: str | None = None,
    artifact_id: str | None = None,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """List queries, optionally filtered by report_id and/or artifact_id.

    If artifact_id is provided, only returns queries for visualizations used by that artifact.
    """
    queries = await service.list_queries(
        db,
        report_id=report_id,
        artifact_id=artifact_id,
        organization_id=str(organization.id) if organization else None,
    )
    # Pydantic v2: model_validate for each
    return [QuerySchema.model_validate(q) for q in queries]

@router.post("", response_model=QuerySchema)
@requires_permission('view_reports')
async def create_query(
    payload: QueryCreate,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        q = await service.create_query(
            db,
            payload,
            organization_id=str(organization.id) if organization else None,
            user_id=str(current_user.id) if current_user else None,
        )
        return QuerySchema.model_validate(q)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{query_id}", response_model=QuerySchema)
@requires_permission('view_reports', model=Query)
async def get_query(
    query_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    q = await service.get_query(
        db,
        query_id,
        organization_id=str(organization.id) if organization else None,
    )
    if not q:
        raise HTTPException(status_code=404, detail="Query not found")
    return QuerySchema.model_validate(q)

@router.post("/{query_id}/run", response_model=dict)
@requires_permission('view_reports', model=Query)
async def run_query_new_step(
    query_id: str,
    payload: QueryRunRequest,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        q_dict, step_obj = await service.run_query_new_step(
            db,
            query_id,
            payload,
            organization_id=str(organization.id) if organization else None,
            user_id=str(current_user.id) if current_user else None,
        )
        # If backend marked the step as error, reflect that in response so UI can show error state
        if isinstance(step_obj, dict) and step_obj.get("status") == "error":
            return {"query": q_dict, "step": step_obj, "error": step_obj.get("status_reason")}
        return {"query": q_dict, "step": step_obj}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/steps/{step_id}/run", response_model=dict)
@requires_permission('view_reports')
async def run_existing_step(
    step_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        step = await service.run_existing_step(
            db,
            step_id,
            current_user=current_user,
            organization_id=str(organization.id) if organization else None,
        )
        return {"step": step}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{query_id}/default_step", response_model=dict)
@requires_permission('view_reports', model=Query)
async def get_default_step(
    query_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    step = await service.get_default_step_for_query(
        db,
        query_id,
        organization_id=str(organization.id) if organization else None,
    )
    if not step:
        return {"step": None}
    payload = step.model_dump() if hasattr(step, 'model_dump') else step.dict()

    # Advertise the stored full result (when the step's data was truncated to
    # the preview cap) so clients can page beyond the preview via
    # /default_step/data instead of assuming the preview is everything.
    try:
        from app.services.result_store import ResultStore
        step_id = payload.get("id")
        if step_id:
            rf = await ResultStore().latest_for_step(
                db, str(organization.id), step_id=str(step_id)
            )
            if rf is not None and rf.wrapped_key:
                payload["full_result"] = {
                    "available": True,
                    "result_file_id": str(rf.id),
                    "row_count": int(rf.row_count),
                }
    except Exception:
        # Availability metadata must never break step loading.
        pass
    return {"step": payload}


@router.get("/{query_id}/default_step/data", response_model=dict)
@requires_permission('view_reports', model=Query)
async def get_default_step_data(
    query_id: str,
    limit: int = 500,
    offset: int = 0,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Page the step's FULL stored result (falls back to the preview rows when
    no full payload exists). Serves artifact/table views that need more than
    the bounded preview embedded in Step.data — the response stays bounded by
    the server-side slice cap, so client payloads can never balloon."""
    from app.services.result_store import ResultStore

    step = await service.get_default_step_for_query(
        db,
        query_id,
        organization_id=str(organization.id) if organization else None,
    )
    if not step:
        raise HTTPException(status_code=404, detail="No default step for query")
    payload = step.model_dump() if hasattr(step, 'model_dump') else step.dict()
    step_id = payload.get("id")

    svc = ResultStore()
    rf = None
    if step_id:
        rf = await svc.latest_for_step(db, str(organization.id), step_id=str(step_id))

    if rf is not None and rf.wrapped_key:
        try:
            s = await svc.slice(rf, limit=limit, offset=offset)
        except Exception as e:
            raise HTTPException(status_code=503, detail=str(e))
        cols = s.get("columns") or []
        # Frontend-friendly dict rows (slice returns positional rows).
        rows = [dict(zip(cols, r)) for r in (s.get("rows") or [])]
        return {
            "rows": rows,
            "columns": cols,
            "row_count": len(rows),
            "total_rows": int(rf.row_count),
            "truncated": bool(s.get("truncated")),
            "next_offset": s.get("next_offset"),
            "source": "full_result",
        }

    data = payload.get("data") or {}
    rows = (data.get("rows") or [])
    page = rows[offset: offset + max(1, min(limit, 5000))]
    return {
        "rows": page,
        "columns": [c.get("field") for c in (data.get("columns") or []) if isinstance(c, dict)],
        "row_count": len(page),
        "total_rows": len(rows),
        "truncated": offset + len(page) < len(rows),
        "next_offset": (offset + len(page)) if offset + len(page) < len(rows) else None,
        "source": "preview_rows",
    }


@router.post("/{query_id}/preview", response_model=dict)
@requires_permission('view_reports', model=Query)
async def preview_query_code(
    query_id: str,
    payload: QueryRunRequest,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        result = await service.preview_query_code(
            db,
            query_id,
            payload,
            organization_id=str(organization.id) if organization else None,
            user_id=str(current_user.id) if current_user else None,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

