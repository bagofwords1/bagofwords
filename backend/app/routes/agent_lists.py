"""Agent Lists API — typed record collections, shared by one or more agents.

Every route works from one agent (the one in the path) and only sees lists
linked to it:
- read (lists, rows, revisions, CSV): ``data_source`` view
- write rows (edit/delete/revert), create a list, remove a list from this
  agent: ``data_source`` manage
- change a list's schema, clear every row of a shared list, or delete it from
  every agent: manage on every agent the list is linked to
"""
from datetime import date
from typing import List, Literal, Optional

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
    RowsDeleteIn,
    RowsDeleteOut,
    RowsPage,
)
from app.services.agent_lists import service as svc
from app.services.agent_lists.access import ListAccess, can_access_agent
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


async def _access(db, user, organization, lst) -> ListAccess:
    return (await svc.access_for_lists(db, user, organization, [lst]))[str(lst.id)]


def _require_schema_authority(access: ListAccess) -> None:
    if not access.can_manage_schema:
        raise HTTPException(
            status_code=403,
            detail="This list is shared with agents you don't manage. Changing it needs manage on all of them.",
        )


@router.get("/agent_lists/counts")
async def agent_list_counts(
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Live list count per agent, for the agents the caller can view (tree badges)."""
    from app.models.agent_list import AgentList, agent_list_data_sources as links
    from app.services.agent_lists.access import viewable_agent_ids

    rows = (await db.execute(
        select(links.c.data_source_id, func.count(AgentList.id))
        .join(AgentList, AgentList.id == links.c.list_id)
        .where(AgentList.organization_id == str(organization.id), AgentList.deleted_at.is_(None))
        .group_by(links.c.data_source_id)
    )).all()
    counts = {str(ds): int(n) for ds, n in rows}
    visible = await viewable_agent_ids(db, current_user, organization, counts.keys())
    return {"by_agent": {k: v for k, v in counts.items() if k in visible}}


@router.get("/agent_lists/{list_id}", response_model=AgentListOut)
async def get_list_anywhere(
    list_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """A list by id, through one of its agents the caller can see; 404 when
    there is none. For links that remember an agent the list has since left
    (or that was deleted): the answer names the agent to use from now on
    (``data_source_id``). Read only; writes stay on the agent routes."""
    lst, ds, access = await svc.readable_list(db, current_user, organization, list_id)
    counts = await svc.row_counts(db, [lst.id])
    can_manage = await can_access_agent(db, current_user, organization, ds, "manage")
    return svc.serialize_list(lst, ds, access, row_count=counts.get(str(lst.id), 0), can_manage=can_manage)


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
    access = await svc.access_for_lists(db, current_user, organization, lists)
    lists.sort(key=lambda l: (l.created_at or 0))
    return [svc.serialize_list(l, ds, access[str(l.id)], row_count=counts.get(str(l.id), 0), can_manage=can_manage)
            for l in lists]


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
    return svc.serialize_list(lst, ds, await _access(db, current_user, organization, lst), can_manage=True)


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
    return svc.serialize_list(lst, ds, await _access(db, current_user, organization, lst),
                      row_count=counts.get(str(lst.id), 0), can_manage=can_manage)


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
    # Hold the list while checking: a link added meanwhile would otherwise
    # let this edit through for an agent the caller doesn't manage.
    await svc.lock_list(db, lst)
    access = await _access(db, current_user, organization, lst)
    _require_schema_authority(access)
    change = await svc.update_list(db, ds, lst, payload)
    counts = await svc.row_counts(db, [lst.id])
    return svc.serialize_list(lst, ds, access, row_count=counts.get(str(lst.id), 0), can_manage=True, change=change)


@router.delete("/data_sources/{data_source_id}/lists/{list_id}", status_code=204)
@requires_resource_permission("data_source", "manage")
async def delete_agent_list(
    data_source_id: str,
    list_id: str,
    scope: Literal["agent", "all"] = Query(
        "all", description="agent: remove the list from this agent only; all: delete it from every agent"
    ),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    if scope == "agent":
        await svc.unlink_list(db, lst, ds)
        return None
    await svc.lock_list(db, lst)
    _require_schema_authority(await _access(db, current_user, organization, lst))
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


@router.get("/data_sources/{data_source_id}/lists/{list_id}/rows/{row_id}", response_model=RowOut)
@requires_resource_permission("data_source", "view")
async def get_list_row(
    data_source_id: str,
    list_id: str,
    row_id: str,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    return svc.serialize_row(await svc.get_row(db, lst, row_id), lst)


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


@router.post("/data_sources/{data_source_id}/lists/{list_id}/rows/delete", response_model=RowsDeleteOut)
@requires_resource_permission("data_source", "manage")
async def delete_list_rows(
    data_source_id: str,
    list_id: str,
    payload: RowsDeleteIn,
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
    current_user: User = Depends(current_user),
):
    """Delete selected rows, or clear the list (``all``) keeping its schema."""
    ds = await svc.get_agent(db, organization, data_source_id)
    lst = await svc.get_list(db, ds, list_id)
    if payload.all:
        # Clearing a shared list wipes what every linked agent saved.
        access = await _access(db, current_user, organization, lst)
        if access.shared:
            _require_schema_authority(access)
    n = await svc.delete_rows(db, lst, None if payload.all else payload.row_ids)
    return RowsDeleteOut(deleted=n)


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
