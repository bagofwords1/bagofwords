"""Agent Lists API — typed per-agent record collections.

Access follows the owning agent:
- read (lists, rows, revisions, CSV): ``data_source`` view
- write (create/update/delete list, edit/delete/revert rows): ``data_source`` manage
"""
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_resource_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.agent_list import AgentListRow, AgentListRowRevision
from app.models.organization import Organization
from app.models.user import User
from app.schemas.agent_list import (
    AgentListOut,
    ListSchemaIn,
    RevisionOut,
    RowOut,
    RowPatchIn,
    RowsPage,
)
from app.services.agent_lists import service as svc
from app.services.agent_lists.access import can_access_agent
from app.services.agent_lists.naming import agent_slug
from app.services.agent_lists.records import (
    ListValidationError,
    RowConflictError,
    patch_row,
    revert_revision,
)

router = APIRouter(tags=["agent_lists"])


def _validation_422(exc: ListValidationError) -> HTTPException:
    return HTTPException(status_code=422, detail={"message": "Validation failed", "errors": exc.errors})


@router.get("/agent_lists/counts")
async def agent_list_counts(
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Live list count per agent, for the agents the caller can view (tree badges)."""
    from app.models.agent_list import AgentList
    from app.services.agent_lists.access import viewable_agent_ids

    rows = (await db.execute(
        select(AgentList.data_source_id, func.count(AgentList.id))
        .where(AgentList.organization_id == str(organization.id), AgentList.deleted_at.is_(None))
        .group_by(AgentList.data_source_id)
    )).all()
    counts = {str(ds): int(n) for ds, n in rows}
    visible = await viewable_agent_ids(db, current_user, organization, counts.keys())
    return {"by_agent": {k: v for k, v in counts.items() if k in visible}}


@router.get("/data_sources/{data_source_id}/lists", response_model=List[AgentListOut])
@requires_resource_permission("data_source", "view")
async def list_agent_lists(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lists = await svc.live_lists_for_agents(db, [ds.id])
    counts = await svc.row_counts(db, [l.id for l in lists])
    can_manage = await can_access_agent(db, current_user, organization, ds, "manage")
    lists.sort(key=lambda l: (l.created_at or 0))
    return [svc.serialize_list(l, ds, row_count=counts.get(str(l.id), 0), can_manage=can_manage) for l in lists]


@router.post("/data_sources/{data_source_id}/lists", response_model=AgentListOut, status_code=201)
@requires_resource_permission("data_source", "manage")
async def create_agent_list(
    data_source_id: str,
    payload: ListSchemaIn,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.create_list(db, organization, ds, payload, current_user)
    return svc.serialize_list(lst, ds, can_manage=True)


@router.get("/data_sources/{data_source_id}/lists/{list_id}", response_model=AgentListOut)
@requires_resource_permission("data_source", "view")
async def get_agent_list(
    data_source_id: str,
    list_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    counts = await svc.row_counts(db, [lst.id])
    can_manage = await can_access_agent(db, current_user, organization, ds, "manage")
    return svc.serialize_list(lst, ds, row_count=counts.get(str(lst.id), 0), can_manage=can_manage)


@router.put("/data_sources/{data_source_id}/lists/{list_id}", response_model=AgentListOut)
@requires_resource_permission("data_source", "manage")
async def update_agent_list(
    data_source_id: str,
    list_id: str,
    payload: ListSchemaIn,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    change = await svc.update_list(db, ds, lst, payload)
    counts = await svc.row_counts(db, [lst.id])
    return svc.serialize_list(lst, ds, row_count=counts.get(str(lst.id), 0), can_manage=True, change=change)


@router.delete("/data_sources/{data_source_id}/lists/{list_id}", status_code=204)
@requires_resource_permission("data_source", "manage")
async def delete_agent_list(
    data_source_id: str,
    list_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    await svc.delete_list(db, lst)
    return None


@router.get("/data_sources/{data_source_id}/lists/{list_id}/rows", response_model=RowsPage)
@requires_resource_permission("data_source", "view")
async def list_rows(
    data_source_id: str,
    list_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    report_id: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    base = svc.rows_query(lst, report_id=report_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await db.execute(
        base.order_by(AgentListRow.created_at.asc(), AgentListRow.id.asc()).offset(offset).limit(limit)
    )).scalars().all()
    return RowsPage(rows=[svc.serialize_row(r, lst) for r in rows], total=int(total), offset=offset, limit=limit)


@router.patch("/data_sources/{data_source_id}/lists/{list_id}/rows/{row_id}", response_model=RowOut)
@requires_resource_permission("data_source", "manage")
async def patch_list_row(
    data_source_id: str,
    list_id: str,
    row_id: str,
    payload: RowPatchIn,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    row = await svc.get_row(db, lst, row_id)
    try:
        row = await patch_row(db, lst, row, row_version=payload.row_version, fields=payload.fields,
                              unlock=payload.unlock, user_id=str(current_user.id))
    except ListValidationError as exc:
        raise _validation_422(exc)
    except RowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return svc.serialize_row(row, lst)


@router.delete("/data_sources/{data_source_id}/lists/{list_id}/rows/{row_id}", status_code=204)
@requires_resource_permission("data_source", "manage")
async def delete_list_row(
    data_source_id: str,
    list_id: str,
    row_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    row = await svc.get_row(db, lst, row_id)
    await svc.delete_row(db, row)
    return None


@router.get(
    "/data_sources/{data_source_id}/lists/{list_id}/rows/{row_id}/revisions",
    response_model=List[RevisionOut],
)
@requires_resource_permission("data_source", "view")
async def list_row_revisions(
    data_source_id: str,
    list_id: str,
    row_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    row = await svc.get_row(db, lst, row_id)
    revs = (await db.execute(
        select(AgentListRowRevision, User.name)
        .outerjoin(User, User.id == AgentListRowRevision.actor_user_id)
        .where(AgentListRowRevision.row_id == row.id)
        .order_by(AgentListRowRevision.created_at.desc())
    )).all()
    return [
        RevisionOut(
            id=str(r.id), actor_type=r.actor_type, actor_user_id=r.actor_user_id, actor_name=name,
            action=r.action, report_id=r.report_id, changed=r.changed or {}, created_at=r.created_at,
        )
        for r, name in revs
    ]


@router.post(
    "/data_sources/{data_source_id}/lists/{list_id}/rows/{row_id}/revisions/{revision_id}/revert",
    response_model=RowOut,
)
@requires_resource_permission("data_source", "manage")
async def revert_row_revision(
    data_source_id: str,
    list_id: str,
    row_id: str,
    revision_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    row = await svc.get_row(db, lst, row_id)
    rev = (await db.execute(
        select(AgentListRowRevision).where(
            AgentListRowRevision.id == str(revision_id), AgentListRowRevision.row_id == row.id
        )
    )).scalar_one_or_none()
    if rev is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    try:
        row = await revert_revision(db, lst, row, rev, user_id=str(current_user.id))
    except ListValidationError as exc:
        raise _validation_422(exc)
    except RowConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return svc.serialize_row(row, lst)


@router.get("/data_sources/{data_source_id}/lists/{list_id}/rows.csv")
@requires_resource_permission("data_source", "view")
async def export_rows_csv(
    data_source_id: str,
    list_id: str,
    include: str = Query("", description="Comma list of: status, evidence, provenance"),
    report_id: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    inc = {p.strip() for p in include.split(",") if p.strip()} & {"status", "evidence", "provenance"}
    rows = (await db.execute(
        svc.rows_query(lst, report_id=report_id).order_by(AgentListRow.created_at.asc(), AgentListRow.id.asc())
    )).scalars().all()
    header = svc.csv_header(lst, inc)
    body = [svc.csv_row(lst, r, inc) for r in rows]

    def _iter():
        # UTF-8 BOM so Excel opens Hebrew / accented text correctly.
        yield "﻿" + svc.csv_line(header)
        for cells in body:
            yield svc.csv_line(cells)

    filename = f"{agent_slug(ds.name, str(ds.id))}-{lst.slug}-{date.today().isoformat()}.csv"
    return StreamingResponse(
        (chunk.encode("utf-8") for chunk in _iter()),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
