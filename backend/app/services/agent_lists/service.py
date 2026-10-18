"""CRUD + serialization for Agent Lists (used by routes and tools)."""
import csv
import io
import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy import delete, func, insert, select

from app.models.agent_list import AgentList, AgentListRow, AgentListRowRevision, agent_list_data_sources as links
from app.models.data_source import DataSource
from app.schemas.agent_list import (
    MAX_LISTS_PER_AGENT,
    AgentListOut,
    ListAgentOut,
    ListSchemaIn,
    RowOut,
)
from app.services.agent_lists.access import ListAccess, access_from, first_submittable_agent, resolved_permissions
from app.services.agent_lists.naming import slugify, table_name_for, tool_name_for, unique_slug
from app.services.agent_lists.schema_change import classify_schema_change


async def get_agent(db, organization, data_source_id: str) -> DataSource:
    ds = (await db.execute(
        select(DataSource).where(
            DataSource.id == str(data_source_id),
            DataSource.organization_id == str(organization.id),
        )
    )).scalar_one_or_none()
    if ds is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return ds


async def get_list(db, ds: DataSource, list_id: str) -> AgentList:
    """A live list linked to ``ds`` (404 otherwise, including a list that
    exists but belongs only to other agents)."""
    lst = (await db.execute(
        select(AgentList)
        .join(links, links.c.list_id == AgentList.id)
        .where(
            AgentList.id == str(list_id),
            links.c.data_source_id == str(ds.id),
            AgentList.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if lst is None:
        raise HTTPException(status_code=404, detail="List not found")
    return lst


async def live_lists_for_agents(db, data_source_ids: Iterable[str]) -> List[AgentList]:
    """Live lists linked to any of ``data_source_ids``, each once."""
    ids = [str(i) for i in data_source_ids if i]
    if not ids:
        return []
    linked = select(links.c.list_id).where(links.c.data_source_id.in_(ids))
    return list((await db.execute(
        select(AgentList)
        .where(AgentList.id.in_(linked), AgentList.deleted_at.is_(None))
        .order_by(AgentList.data_source_id, AgentList.slug)
    )).scalars().all())


async def linked_agent_ids(db, list_ids: Iterable[str]) -> Dict[str, List[str]]:
    """``{list_id: [data_source_id, ...]}`` (sorted) for the given lists."""
    ids = [str(i) for i in list_ids if i]
    if not ids:
        return {}
    out: Dict[str, List[str]] = {i: [] for i in ids}
    for lid, ds_id in (await db.execute(
        select(links.c.list_id, links.c.data_source_id).where(links.c.list_id.in_(ids))
    )).all():
        out.setdefault(str(lid), []).append(str(ds_id))
    for v in out.values():
        v.sort()
    return out


async def linked_agents(db, lst: AgentList) -> List[DataSource]:
    """The agents ``lst`` is linked to, in id order."""
    ids = (await linked_agent_ids(db, [lst.id])).get(str(lst.id), [])
    if not ids:
        return []
    return list((await db.execute(
        select(DataSource).where(DataSource.id.in_(ids)).order_by(DataSource.id)
    )).scalars().all())


def _check_fits(existing: List[AgentList], name: str, slug: Optional[str] = None) -> None:
    """A list must fit on every agent it is linked to: the per-agent cap, a
    unique name, and (when linking an existing list) a unique slug."""
    if len(existing) >= MAX_LISTS_PER_AGENT:
        raise HTTPException(status_code=400, detail=f"An agent can have at most {MAX_LISTS_PER_AGENT} lists.")
    if any(l.name.strip().casefold() == name.strip().casefold() for l in existing):
        raise HTTPException(status_code=409, detail="A list with this name already exists on this agent.")
    if slug is not None and any(l.slug == slug for l in existing):
        raise HTTPException(status_code=409, detail="A list with this machine name already exists on this agent.")


async def _lock(db, lst: AgentList) -> None:
    """Serialize link changes on one list, so two agents removing it at once
    cannot both see the other's link and leave it live with none (row lock on
    Postgres; SQLite serializes writes on its own). Reloads ``lst`` under the
    lock, so the home agent compared afterwards is the committed one."""
    await db.execute(
        select(AgentList).where(AgentList.id == str(lst.id)).with_for_update()
        .execution_options(populate_existing=True)
    )


async def link_list(db, lst: AgentList, ds: DataSource, *, commit: bool = True) -> None:
    """Link an existing list to another agent of the same organization, so
    both agents read and fill the same rows."""
    if lst.deleted_at is not None:
        raise HTTPException(status_code=404, detail="List not found")
    if str(ds.organization_id) != str(lst.organization_id):
        raise HTTPException(status_code=404, detail="Agent not found")
    await _lock(db, lst)
    existing = await live_lists_for_agents(db, [ds.id])
    if any(str(l.id) == str(lst.id) for l in existing):
        return
    _check_fits(existing, lst.name, lst.slug)
    await db.execute(insert(links).values(list_id=str(lst.id), data_source_id=str(ds.id)))
    if commit:
        await db.commit()


async def _drop_link(db, lst: AgentList, ds_id: str) -> List[str]:
    """Remove one link; move home to a remaining agent if needed. Returns the
    agents still linked."""
    await db.execute(delete(links).where(links.c.list_id == str(lst.id), links.c.data_source_id == str(ds_id)))
    remaining = (await linked_agent_ids(db, [lst.id])).get(str(lst.id), [])
    if remaining and str(lst.data_source_id) == str(ds_id):
        lst.data_source_id = remaining[0]
    return remaining


async def unlink_list(db, lst: AgentList, ds: DataSource) -> None:
    """Remove ``lst`` from one agent. The list and its rows stay on the other
    agents; a list left with no agent is deleted."""
    await _lock(db, lst)
    remaining = await _drop_link(db, lst, str(ds.id))
    if not remaining:
        lst.deleted_at = datetime.utcnow()
    await db.commit()


async def detach_agent_lists(db, data_source_id: str) -> None:
    """Before an agent is deleted: lists shared with other agents lose only
    this link (and get a new home). Lists on this agent alone are left as they
    are, for the agent's CASCADE to delete with their rows, so a delete that
    fails later leaves them intact. Does not commit."""
    mine = select(links.c.list_id).where(links.c.data_source_id == str(data_source_id))
    elsewhere = select(links.c.list_id).where(links.c.data_source_id != str(data_source_id))
    shared = (await db.execute(
        select(AgentList).where(AgentList.id.in_(mine), AgentList.id.in_(elsewhere))
    )).scalars().all()
    for lst in shared:
        await _lock(db, lst)
        await _drop_link(db, lst, str(data_source_id))
    # Write the new homes before the agent row goes, so its CASCADE spares them.
    await db.flush()


async def access_for_lists(db, user, organization, lists: Iterable[AgentList]) -> Dict[str, ListAccess]:
    """``{list_id: ListAccess}`` for ``user``, in a fixed number of queries."""
    lists = list(lists)
    ids = await linked_agent_ids(db, [l.id for l in lists])
    all_ids = sorted({i for v in ids.values() for i in v})
    agents = {str(a.id): a for a in (await db.execute(
        select(DataSource).where(DataSource.id.in_(all_ids))
    )).scalars().all()} if all_ids else {}
    resolved = await resolved_permissions(db, user, organization)
    return {
        str(l.id): access_from(resolved, organization, [agents[i] for i in ids.get(str(l.id), []) if i in agents],
                               home_id=str(l.data_source_id))
        for l in lists
    }


async def submit_route(db, user, organization, lst: AgentList, report_agent_ids: Iterable[str], *,
                       resolved=None):
    """How a conversation saves to ``lst``: ``(via, here)`` where ``here`` are
    the list's agents among ``report_agent_ids`` that the user can see (by id)
    and ``via`` the first of them the user may save through (or None). Used by
    both the tool registry and the submit gateway, so they always agree."""
    if resolved is None:
        resolved = await resolved_permissions(db, user, organization)
    in_report = {str(i) for i in report_agent_ids}
    ids = [i for i in (await linked_agent_ids(db, [lst.id])).get(str(lst.id), []) if i in in_report]
    agents = list((await db.execute(
        select(DataSource).where(DataSource.id.in_(ids)).order_by(DataSource.id)
    )).scalars().all()) if ids else []
    here = access_from(resolved, organization, agents).visible_agents if resolved is not None else []
    via = await first_submittable_agent(db, user, organization, here, lst, resolved=resolved) if here else None
    return via, here


async def can_manage_schema(db, user, organization, lst: AgentList) -> bool:
    """MANAGE on every agent ``lst`` is linked to: the bar for changing its
    fields, clearing its rows when shared, or deleting it from every agent."""
    return (await access_for_lists(db, user, organization, [lst]))[str(lst.id)].can_manage_schema


async def row_counts(db, list_ids: List[str]) -> Dict[str, int]:
    if not list_ids:
        return {}
    rows = (await db.execute(
        select(AgentListRow.list_id, func.count(AgentListRow.id))
        .where(AgentListRow.list_id.in_(list_ids), AgentListRow.deleted_at.is_(None))
        .group_by(AgentListRow.list_id)
    )).all()
    return {str(lid): int(n) for lid, n in rows}


def _key_name(lst: AgentList) -> Optional[str]:
    if not lst.key_field_id:
        return None
    return next((f["name"] for f in (lst.fields or []) if f["id"] == lst.key_field_id), None)


def serialize_list(lst: AgentList, ds: DataSource, access: Optional[ListAccess] = None, *, row_count: int = 0,
                   can_manage: bool = False, change: Optional[str] = None) -> AgentListOut:
    """``ds`` is the agent the list is being looked at from. ``access`` (from
    ``access_for_lists``) says which linked agents the caller may see; without
    it the list is shown as belonging to ``ds`` alone."""
    access = access or ListAccess(visible_agents=[ds], can_manage_schema=can_manage, home_id=str(ds.id))
    named = access.name_agent or ds
    return AgentListOut(
        id=str(lst.id),
        data_source_id=str(ds.id),
        agents=[ListAgentOut(id=str(a.id), name=a.name) for a in access.visible_agents],
        hidden_agent_count=access.hidden_agent_count,
        shared=access.shared,
        name=lst.name,
        slug=lst.slug,
        description=lst.description or "",
        fields=lst.fields or [],
        key_field_id=lst.key_field_id,
        key_field=_key_name(lst),
        require_evidence=bool(lst.require_evidence),
        allow_viewer_submissions=bool(lst.allow_viewer_submissions),
        keep_human_edits=bool(lst.keep_human_edits),
        version=int(lst.version or 1),
        row_count=row_count,
        tool_name=tool_name_for(lst.slug, list_id=str(lst.id)),
        table_name=table_name_for(named.name, str(named.id), lst.slug),
        created_at=lst.created_at,
        updated_at=lst.updated_at,
        can_manage=can_manage,
        can_manage_schema=can_manage and access.can_manage_schema,
        change=change,
    )


def _fields_payload(payload: ListSchemaIn) -> List[Dict[str, Any]]:
    return [f.model_dump() for f in payload.fields]


def _key_id(payload: ListSchemaIn, fields: List[Dict[str, Any]]) -> Optional[str]:
    if not payload.key_field:
        return None
    return next((f["id"] for f in fields if f["name"] == payload.key_field or f["id"] == payload.key_field), None)


async def create_list(db, organization, ds: DataSource, payload: ListSchemaIn, user) -> AgentList:
    existing = await live_lists_for_agents(db, [ds.id])
    _check_fits(existing, payload.name)
    base = slugify(payload.name, fallback_prefix="list", seed=payload.name)
    slug = unique_slug(base, [l.slug for l in existing])
    fields = _fields_payload(payload)
    lst = AgentList(
        organization_id=str(organization.id),
        data_source_id=str(ds.id),
        created_by_user_id=str(user.id) if user else None,
        name=payload.name,
        slug=slug,
        description=payload.description or "",
        fields=fields,
        key_field_id=_key_id(payload, fields),
        require_evidence=payload.require_evidence,
        allow_viewer_submissions=payload.allow_viewer_submissions,
        keep_human_edits=payload.keep_human_edits,
        version=1,
    )
    db.add(lst)
    await db.flush()
    await db.execute(insert(links).values(list_id=str(lst.id), data_source_id=str(ds.id)))
    await db.commit()
    await db.refresh(lst)
    return lst


async def update_list(db, ds: DataSource, lst: AgentList, payload: ListSchemaIn) -> str:
    # The new name must be free on every agent the list is linked to.
    agent_ids = (await linked_agent_ids(db, [lst.id])).get(str(lst.id)) or [str(ds.id)]
    others = [l for l in await live_lists_for_agents(db, agent_ids) if l.id != lst.id]
    if any(l.name.strip().casefold() == payload.name.casefold() for l in others):
        detail = ("A list with this name already exists on this agent." if len(agent_ids) == 1 else
                  "A list with this name already exists on one of the agents this list is shared with.")
        raise HTTPException(status_code=409, detail=detail)
    fields = _fields_payload(payload)
    key_id = _key_id(payload, fields)
    change = classify_schema_change(lst.fields or [], lst.key_field_id, fields, key_id)
    if change == "none" and (lst.name != payload.name or (lst.description or "") != (payload.description or "")
                             or bool(lst.require_evidence) != payload.require_evidence
                             or bool(lst.allow_viewer_submissions) != payload.allow_viewer_submissions
                             or bool(lst.keep_human_edits) != payload.keep_human_edits):
        change = "additive"
    lst.name = payload.name
    lst.description = payload.description or ""
    lst.fields = fields
    lst.key_field_id = key_id
    lst.require_evidence = payload.require_evidence
    lst.allow_viewer_submissions = payload.allow_viewer_submissions
    lst.keep_human_edits = payload.keep_human_edits
    if change == "breaking":
        lst.version = int(lst.version or 1) + 1
    lst.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(lst)
    return change


async def delete_list(db, lst: AgentList) -> None:
    lst.deleted_at = datetime.utcnow()
    await db.commit()


def serialize_row(row: AgentListRow, lst: AgentList) -> RowOut:
    return RowOut(
        id=str(row.id),
        key_value=row.key_value,
        values=row.values or {},
        schema_version=int(row.schema_version or 1),
        row_version=int(row.row_version or 1),
        # Locks are recorded either way but only mean something (and are only
        # shown) when the list keeps hand edits.
        locked_fields=list(row.locked_fields or []) if lst.keep_human_edits else [],
        report_id=row.report_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        stale=int(row.schema_version or 1) < int(lst.version or 1),
    )


def rows_query(lst: AgentList, *, report_id: Optional[str] = None):
    q = select(AgentListRow).where(AgentListRow.list_id == lst.id, AgentListRow.deleted_at.is_(None))
    if report_id:
        q = q.where(AgentListRow.report_id == str(report_id))
    return q


async def get_row(db, lst: AgentList, row_id: str) -> AgentListRow:
    row = (await db.execute(
        select(AgentListRow).where(
            AgentListRow.id == str(row_id), AgentListRow.list_id == lst.id, AgentListRow.deleted_at.is_(None)
        )
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Row not found")
    return row


async def delete_row(db, row: AgentListRow) -> None:
    revs = (await db.execute(
        select(AgentListRowRevision).where(AgentListRowRevision.row_id == row.id)
    )).scalars().all()
    for r in revs:
        await db.delete(r)
    await db.delete(row)
    await db.commit()


async def delete_rows(db, lst: AgentList, row_ids: Optional[List[str]] = None) -> int:
    """Hard-delete rows of ``lst`` (with their revisions), like a single row
    delete. ``row_ids=None`` empties the list; ids from other lists are
    ignored. Returns how many rows were deleted."""
    q = select(AgentListRow.id).where(AgentListRow.list_id == lst.id, AgentListRow.deleted_at.is_(None))
    if row_ids is not None:
        q = q.where(AgentListRow.id.in_([str(r) for r in row_ids]))
    ids = list((await db.execute(q)).scalars().all())
    if not ids:
        return 0
    await db.execute(delete(AgentListRowRevision).where(AgentListRowRevision.row_id.in_(ids)))
    await db.execute(delete(AgentListRow).where(AgentListRow.id.in_(ids)))
    await db.commit()
    return len(ids)


# ── CSV ───────────────────────────────────────────────────────────────────

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: Any) -> str:
    """Stringify a cell and neutralize spreadsheet formula injection.

    Values come from untrusted documents; a cell such as ``=HYPERLINK(...)``
    must not execute when the export is opened in Excel/Sheets.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        s = "true" if value else "false"
    elif isinstance(value, (dict, list)):
        s = json.dumps(value, ensure_ascii=False)
    else:
        s = str(value)
    if s.startswith(_FORMULA_PREFIXES):
        # Plain negative numbers are data, not formulas.
        if not (s.startswith("-") and _is_number(s)):
            s = "'" + s
    return s


def _is_number(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def csv_header(lst: AgentList, include: set) -> List[str]:
    cols: List[str] = []
    for f in lst.fields or []:
        cols.append(f["name"])
        if "status" in include:
            cols.append(f"{f['name']}__status")
        if "evidence" in include:
            cols += [f"{f['name']}__quote", f"{f['name']}__page", f"{f['name']}__verified"]
    if "provenance" in include:
        cols += ["_row_id", "_report_id", "_updated_at", "_schema_version", "_edited_by_human"]
    return cols


def csv_row(lst: AgentList, row: AgentListRow, include: set) -> List[str]:
    vals = row.values or {}
    out: List[Any] = []
    for f in lst.fields or []:
        env = vals.get(f["id"]) or {}
        out.append(env.get("value"))
        if "status" in include:
            out.append(env.get("status"))
        if "evidence" in include:
            ev = next((e for e in (env.get("evidence") or []) if e.get("quote")), None) or {}
            out += [ev.get("quote"), ev.get("page"), ev.get("verified") if ev else None]
    if "provenance" in include:
        out += [row.id, row.report_id, row.updated_at.isoformat() if row.updated_at else None,
                row.schema_version, bool(row.locked_fields)]
    return [csv_safe(v) for v in out]


def csv_line(cells: List[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf).writerow(cells)
    return buf.getvalue()
