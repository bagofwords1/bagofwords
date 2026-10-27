from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import lazyload

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user as current_user_dep
from app.core.permissions_decorator import requires_permission

from app.models.user import User
from app.models.organization import Organization
from app.models.query import Query
from app.models.report import Report
from app.schemas.query_schema import QueryCreate, QuerySchema, QueryRunRequest, QueryLastRunSchema, QueryStepStatusSchema
from app.services.query_service import QueryService


router = APIRouter(prefix="/queries", tags=["queries"])
service = QueryService()


async def _require_run_custom_code(db, current_user, organization) -> None:
    """Gate caller-supplied code execution behind ``run_custom_code``.

    This is the check the removed ``enable_code_editing`` org setting never
    performed: it lived only in two Vue components, so the API accepted
    arbitrary caller-supplied SQL/Python from any member regardless of the
    toggle. Hiding an editor is not a permission check.
    """
    from app.core.permission_resolver import resolve_permissions
    from app.core.code_visibility import can_run_custom_code

    resolved = await resolve_permissions(db, str(current_user.id), str(organization.id))
    if not can_run_custom_code(resolved):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to edit and run custom code",
        )


async def _require_query_owner(db, query_id: str, current_user, organization) -> None:
    """A builder run replaces the query's default step — the code every viewer
    then executes — so it is an edit, not a read. Edits stay owner-only, as on
    the report routes (``update_reports``, ``owner_only=True``): the query's
    creator or the owner of the report it lives on."""
    q = (await db.execute(
        select(Query).options(lazyload("*")).where(
            Query.id == str(query_id),
            Query.organization_id == str(organization.id),
        )
    )).scalar_one_or_none()
    if q is None:
        raise HTTPException(status_code=404, detail="Query not found")
    uid = str(current_user.id)
    if q.user_id and str(q.user_id) == uid:
        return
    from app.services.row_owner import query_report_id

    report_id = await query_report_id(db, q)
    if report_id:
        report_owner = (await db.execute(
            select(Report.user_id).where(
                Report.id == str(report_id),
                Report.organization_id == str(organization.id),
            )
        )).scalar_one_or_none()
        if report_owner and str(report_owner) == uid:
            return
    raise HTTPException(
        status_code=403,
        detail="Only the query's owner can change its code. Run it with parameter values instead.",
    )


async def _query_parent_report(db, q: Query, organization):
    """The report a query lives on: its own report_id, else its widget's
    (row_owner); none for a published dashboard's copy or an orphan."""
    from app.services.row_owner import query_report_id

    report_id = await query_report_id(db, q)
    if not report_id:
        return None
    return (await db.execute(
        select(Report).options(lazyload("*")).where(
            Report.id == str(report_id),
            Report.organization_id == str(organization.id),
        )
    )).scalar_one_or_none()


async def _can_read_query(db, q: Query, current_user, organization) -> bool:
    """A query is readable/runnable iff its parent report is readable: the
    report owner, a full admin, or anyone the report's conversation/artifact
    visibility (shared, internal, public) or project admits — the same rule as
    ``entities/from_step`` and report files. A query with no report is visible
    only to its creator (and full admins)."""
    from app.services.file_access_service import (
        user_can_view_report, is_full_admin, CONVERSATION_OR_ARTIFACT,
    )
    report = await _query_parent_report(db, q, organization)
    if report is None:
        if q.user_id and str(q.user_id) == str(current_user.id):
            return True
        return await is_full_admin(db, current_user, organization)
    return await user_can_view_report(
        db, current_user, organization, report, CONVERSATION_OR_ARTIFACT
    )


async def _require_query_read(db, query_id: str, current_user, organization) -> None:
    q = (await db.execute(
        select(Query).options(lazyload("*")).where(
            Query.id == str(query_id),
            Query.organization_id == str(organization.id),
        )
    )).scalar_one_or_none()
    if q is None or not await _can_read_query(db, q, current_user, organization):
        raise HTTPException(status_code=404, detail="Query not found")


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
    from app.services.bow_source_access import can_read, report_access
    from app.services.row_owner import query_report_id
    queries = [q for q in queries
               if await can_read(db, await report_access(db, await query_report_id(db, q)), current_user)]
    queries = [q for q in queries if await _can_read_query(db, q, current_user, organization)]
    # Pydantic v2: model_validate for each, then apply the per-viewer step-data
    # policy so a non-owner never receives a withheld creator snapshot in the
    # embedded default_step.
    viewer_id = str(current_user.id) if current_user else None
    last_runs = await service.get_last_runs(db, [str(q.id) for q in queries])
    result = []
    scope_memo: dict = {}
    for q in queries:
        schema = QuerySchema.model_validate(q)
        schema = await service.overlay_viewer_on_query_schema(db, q, schema, viewer_id)
        schema = await service.annotate_credential_scope(db, q, schema, memo=scope_memo)
        lr = last_runs.get(str(q.id))
        # Withheld-snapshot viewers must not see the creator's run outcome —
        # status_reason carries raw execution errors (table names, SQL).
        if lr and not (schema.default_step and schema.default_step.snapshot_withheld):
            schema.last_run = QueryLastRunSchema(**lr)
        result.append(schema)
    return result

@router.get("/statuses", response_model=list[QueryStepStatusSchema])
@requires_permission('view_reports')
async def list_query_step_statuses(
    report_id: str,
    artifact_id: str | None = None,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """The status of each query's default step behind a dashboard, in one
    request and without rows -- what the dashboard polls while the queries
    it was brought in with run again as the person who brought it.

    The same queries, filtered by the same read checks, as ``GET /queries``.
    A step's failure reason is the run's own error, so it is given only to
    the owner of the conversation the step lives on (the person whose run
    it was); anyone else gets the status alone.
    """
    from app.services.access_errors import NO_ACCESS_CODE, NO_ACCESS_REASON
    from app.services.bow_source_access import can_read, report_access
    from app.services.row_owner import query_report_id

    queries = await service.list_queries(
        db, report_id=report_id, artifact_id=artifact_id, organization_id=str(organization.id),
    )
    owners: dict = {}
    result = []
    for q in queries:
        rid = await query_report_id(db, q)
        if not await can_read(db, await report_access(db, rid), current_user):
            continue
        if not await _can_read_query(db, q, current_user, organization):
            continue
        if rid not in owners:
            owners[rid] = (await db.execute(
                select(Report.user_id).where(Report.id == str(rid))
            )).scalar_one_or_none() if rid else None
        mine = owners[rid] is not None and str(owners[rid]) == str(current_user.id)
        step = q.default_step
        status = getattr(step, "status", None)
        reason = getattr(step, "status_reason", None) if status == "error" else None
        result.append(QueryStepStatusSchema(
            query_id=str(q.id), title=q.title, step_id=str(step.id) if step else None, status=status,
            status_reason=reason if mine else None,
            error_code=NO_ACCESS_CODE if reason == NO_ACCESS_REASON else None,
        ))
    return result

@router.post("", response_model=QuerySchema)
@requires_permission('view_reports')
async def create_query(
    payload: QueryCreate,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    # report_id / widget_id come from the body, so the decorator can't scope
    # them: the target report must be in this org and owned by the caller,
    # and a given widget must belong to that report.
    if payload.report_id:
        report = (await db.execute(
            select(Report).options(lazyload("*")).where(
                Report.id == str(payload.report_id),
                Report.organization_id == str(organization.id),
            )
        )).scalar_one_or_none()
        if report is None or str(report.user_id) != str(current_user.id):
            raise HTTPException(status_code=404, detail="Report not found")
    if payload.widget_id:
        from app.models.widget import Widget
        widget_report_id = (await db.execute(
            select(Widget.report_id).where(Widget.id == str(payload.widget_id))
        )).scalar_one_or_none()
        if widget_report_id is None or not payload.report_id or str(widget_report_id) != str(payload.report_id):
            raise HTTPException(status_code=404, detail="Widget not found")
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
    await _require_query_read(db, query_id, current_user, organization)
    q = await service.get_query(
        db,
        query_id,
        organization_id=str(organization.id) if organization else None,
    )
    if not q:
        raise HTTPException(status_code=404, detail="Query not found")
    schema = QuerySchema.model_validate(q)
    viewer_id = str(current_user.id) if current_user else None
    schema = await service.overlay_viewer_on_query_schema(db, q, schema, viewer_id)
    schema = await service.annotate_credential_scope(db, q, schema)
    lr = (await service.get_last_runs(db, [str(q.id)])).get(str(q.id))
    if lr and not (schema.default_step and schema.default_step.snapshot_withheld):
        schema.last_run = QueryLastRunSchema(**lr)
    return schema

@router.delete("/{query_id}", response_model=dict)
@requires_permission('delete_reports', model=Query, owner_only=True)
async def delete_query(
    query_id: str,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Soft-delete a query along with its visualizations."""
    deleted = await service.delete_query(
        db,
        query_id,
        organization_id=str(organization.id) if organization else None,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="Query not found")
    return {"message": "Query deleted"}


@router.post("/{query_id}/run", response_model=dict)
@requires_permission('view_reports', model=Query)
async def run_query_new_step(
    query_id: str,
    payload: QueryRunRequest,
    current_user: User = Depends(current_user_dep),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    if (payload.mode or "builder") != "viewer":
        # Builder mode executes caller-supplied `code`: an edit, owner-only.
        await _require_run_custom_code(db, current_user, organization)
        await _require_query_owner(db, query_id, current_user, organization)
    # Every mode reads the stored query and its data. Viewer mode runs the
    # stored step with parameter values only (ordinary report usage), so it is
    # open to anyone who can read the parent report (owner, shared,
    # published/public, project), not to every member.
    await _require_query_read(db, query_id, current_user, organization)
    try:

        if (payload.mode or "builder") == "viewer":
            # Viewer-execute: run the default step's code with param values,
            # cached per (step, viewer, values). No new Step is created.
            result = await service.run_query_viewer(
                db,
                query_id,
                payload,
                organization_id=str(organization.id) if organization else None,
                user_id=str(current_user.id) if current_user else None,
            )
            return result
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
    # Rerunning persists the result into the step's shared snapshot under the
    # caller's credentials, so it is an edit of that report: owner-only.
    from app.services.row_owner import step_report_id

    # A published dashboard's copy belongs to no conversation: not found here.
    report_id = await step_report_id(db, str(step_id))
    owner_id = (await db.execute(
        select(Report.user_id).where(Report.id == str(report_id), Report.organization_id == str(organization.id))
    )).scalar_one_or_none() if report_id else None
    if owner_id is None:
        raise HTTPException(status_code=404, detail="Step not found")
    if str(owner_id) != str(current_user.id):
        raise HTTPException(status_code=403, detail="Only the report's owner can rerun this step.")
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
    await _require_query_read(db, query_id, current_user, organization)
    step = await service.get_default_step_for_query(
        db,
        query_id,
        organization_id=str(organization.id) if organization else None,
        viewer_user_id=str(current_user.id) if current_user else None,
    )
    if not step:
        return {"step": None}
    return {"step": step.model_dump() if hasattr(step, 'model_dump') else step.dict()}


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
        # Previewing runs caller-supplied code against the source, same trust
        # boundary as a builder-mode run.
        await _require_run_custom_code(db, current_user, organization)
        await _require_query_read(db, query_id, current_user, organization)
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

