"""CRUD + serialization for Agent Lists (used by routes and tools)."""
import csv
import io
import json
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from fastapi import HTTPException
from sqlalchemy import delete, func, select

from app.models.agent_list import AgentList, AgentListRow, AgentListRowRevision
from app.models.data_source import DataSource
from app.schemas.agent_list import (
    MAX_LISTS_PER_AGENT,
    AgentListOut,
    ListSchemaIn,
    RowOut,
)
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
    lst = (await db.execute(
        select(AgentList).where(
            AgentList.id == str(list_id),
            AgentList.data_source_id == str(ds.id),
            AgentList.deleted_at.is_(None),
        )
    )).scalar_one_or_none()
    if lst is None:
        raise HTTPException(status_code=404, detail="List not found")
    return lst


async def live_lists_for_agents(db, data_source_ids: Iterable[str]) -> List[AgentList]:
    ids = [str(i) for i in data_source_ids if i]
    if not ids:
        return []
    return list((await db.execute(
        select(AgentList)
        .where(AgentList.data_source_id.in_(ids), AgentList.deleted_at.is_(None))
        .order_by(AgentList.data_source_id, AgentList.slug)
    )).scalars().all())


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


def serialize_list(lst: AgentList, ds: DataSource, *, row_count: int = 0, can_manage: bool = False,
                   change: Optional[str] = None) -> AgentListOut:
    return AgentListOut(
        id=str(lst.id),
        data_source_id=str(lst.data_source_id),
        name=lst.name,
        slug=lst.slug,
        description=lst.description or "",
        fields=lst.fields or [],
        key_field_id=lst.key_field_id,
        key_field=_key_name(lst),
        require_evidence=bool(lst.require_evidence),
        allow_viewer_submissions=bool(lst.allow_viewer_submissions),
        version=int(lst.version or 1),
        row_count=row_count,
        tool_name=tool_name_for(lst.slug, list_id=str(lst.id)),
        table_name=table_name_for(ds.name, str(ds.id), lst.slug),
        created_at=lst.created_at,
        updated_at=lst.updated_at,
        can_manage=can_manage,
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
    if len(existing) >= MAX_LISTS_PER_AGENT:
        raise HTTPException(status_code=400, detail=f"An agent can have at most {MAX_LISTS_PER_AGENT} lists.")
    if any(l.name.strip().casefold() == payload.name.casefold() for l in existing):
        raise HTTPException(status_code=409, detail="A list with this name already exists on this agent.")
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
        version=1,
    )
    db.add(lst)
    await db.commit()
    await db.refresh(lst)
    return lst


async def update_list(db, ds: DataSource, lst: AgentList, payload: ListSchemaIn) -> str:
    others = [l for l in await live_lists_for_agents(db, [ds.id]) if l.id != lst.id]
    if any(l.name.strip().casefold() == payload.name.casefold() for l in others):
        raise HTTPException(status_code=409, detail="A list with this name already exists on this agent.")
    fields = _fields_payload(payload)
    key_id = _key_id(payload, fields)
    change = classify_schema_change(lst.fields or [], lst.key_field_id, fields, key_id)
    if change == "none" and (lst.name != payload.name or (lst.description or "") != (payload.description or "")
                             or bool(lst.require_evidence) != payload.require_evidence
                             or bool(lst.allow_viewer_submissions) != payload.allow_viewer_submissions):
        change = "additive"
    lst.name = payload.name
    lst.description = payload.description or ""
    lst.fields = fields
    lst.key_field_id = key_id
    lst.require_evidence = payload.require_evidence
    lst.allow_viewer_submissions = payload.allow_viewer_submissions
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
        locked_fields=list(row.locked_fields or []),
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
