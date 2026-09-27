"""Agent Lists exposed as BOW tables: ``bow.<agent>.lists.<list>``.

Discovery (schema context) advertises one table per list on the report's
agents the user can VIEW, in every mode. Execution goes through the built-in
``bow`` client with ``{"dataset": "list", "list_id": "..."}``; the table name
is display-only, so renaming an agent or list never breaks saved code.

Access is re-checked on every execution (including saved-query refresh):
the executing user must still be able to view the owning agent.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
from sqlalchemy import select

from app.models.agent_list import AgentList, AgentListRow
from app.models.data_source import DataSource
from app.services.agent_lists.access import can_access_agent, viewable_agent_ids
from app.services.agent_lists.naming import table_name_for

META_COLUMNS = ["_row_id", "_key", "_schema_version", "_report_id", "_created_at", "_updated_at", "_edited_by_human"]
_DTYPE = {"string": "string", "number": "number", "integer": "number", "boolean": "boolean",
          "date": "date", "enum": "string"}


def list_columns(lst: AgentList, *, evidence: bool = False) -> List[Tuple[str, str]]:
    cols: List[Tuple[str, str]] = []
    for f in lst.fields or []:
        cols.append((f["name"], _DTYPE.get(f.get("type") or "string", "string")))
        cols.append((f"{f['name']}__status", "string"))
        if evidence:
            cols += [(f"{f['name']}__quote", "string"), (f"{f['name']}__page", "number"),
                     (f"{f['name']}__verified", "boolean")]
    cols += [(c, "boolean" if c == "_edited_by_human" else ("number" if c == "_schema_version" else "string"))
             for c in META_COLUMNS]
    return cols


async def list_tables(db, user, organization, data_source_ids) -> List[Dict[str, Any]]:
    """Tables for lists on the given agents that ``user`` may view (sorted)."""
    allowed = await viewable_agent_ids(db, user, organization, data_source_ids)
    if not allowed:
        return []
    lists = (await db.execute(
        select(AgentList, DataSource)
        .join(DataSource, DataSource.id == AgentList.data_source_id)
        .where(AgentList.data_source_id.in_(sorted(allowed)), AgentList.deleted_at.is_(None))
        .order_by(AgentList.data_source_id, AgentList.slug)
    )).all()
    out = []
    for lst, ds in lists:
        out.append({
            "name": table_name_for(ds.name, str(ds.id), lst.slug),
            "list": lst,
            "agent": ds,
            "columns": list_columns(lst),
        })
    return out


def describe_table(entry: Dict[str, Any]) -> str:
    lst: AgentList = entry["list"]
    fields = "; ".join(
        f"{f['name']} ({f.get('type')}{': ' + f['description'] if f.get('description') else ''})"
        for f in (lst.fields or [])
    )
    return (
        f"Agent List '{lst.name}' of agent '{entry['agent'].name}' — one row per record the agent saved. "
        + (f"{lst.description.strip()} " if lst.description else "")
        + f"Fields: {fields}. "
        f"Query ONLY via ds_clients[\"bow\"].execute_query({{\"dataset\": \"list\", \"list_id\": \"{lst.id}\"}}) "
        "(always pass list_id, never the table name). Optional: \"columns\" [...], \"query\" \"field:value ...\" "
        "(exact match), \"group_by\" + \"metrics\" [{\"op\":\"sum\",\"field\":...,\"name\":...}], "
        "\"sort\" [{\"field\":...,\"direction\":\"desc\"}], \"limit\". Add evidence columns "
        "(<field>__quote/__page/__verified) by naming them in columns. _row_id identifies a row "
        "(pass it as row_id to the list's submit tool to update it)."
    )


async def resolve_list(db, organization, list_id: Optional[str], list_ref: Optional[str]) -> AgentList:
    q = select(AgentList).where(AgentList.organization_id == str(organization.id), AgentList.deleted_at.is_(None))
    if list_id:
        lst = (await db.execute(q.where(AgentList.id == str(list_id)))).scalar_one_or_none()
        if lst is None:
            raise ValueError(f"Unknown list_id {list_id!r}")
        return lst
    ref = (list_ref or "").strip()
    m = re.fullmatch(r"bow\.([a-z0-9_]+)\.lists\.([a-z0-9_]+)", ref)
    if not m:
        raise ValueError("Pass list_id (preferred) or list as 'bow.<agent>.lists.<list>'")
    agent_part, slug = m.group(1), m.group(2)
    candidates = (await db.execute(
        select(AgentList, DataSource).join(DataSource, DataSource.id == AgentList.data_source_id)
        .where(AgentList.organization_id == str(organization.id), AgentList.deleted_at.is_(None),
               AgentList.slug == slug)
    )).all()
    for lst, ds in candidates:
        if table_name_for(ds.name, str(ds.id), lst.slug) == ref:
            return lst
    raise ValueError(f"List {ref!r} not found — it may have been renamed; query by list_id instead")


def rows_to_frame(lst: AgentList, rows: List[AgentListRow], *, evidence: bool) -> pd.DataFrame:
    records = []
    for r in rows:
        vals = r.values or {}
        rec: Dict[str, Any] = {}
        for f in lst.fields or []:
            env = vals.get(f["id"]) or {}
            rec[f["name"]] = env.get("value")
            rec[f"{f['name']}__status"] = env.get("status")
            if evidence:
                ev = next((e for e in (env.get("evidence") or []) if e.get("quote")), None) or {}
                rec[f"{f['name']}__quote"] = ev.get("quote")
                rec[f"{f['name']}__page"] = ev.get("page")
                rec[f"{f['name']}__verified"] = ev.get("verified") if ev else None
        rec.update({
            "_row_id": r.id, "_key": r.key_value, "_schema_version": r.schema_version,
            "_report_id": r.report_id,
            "_created_at": r.created_at.isoformat() if r.created_at else None,
            "_updated_at": r.updated_at.isoformat() if r.updated_at else None,
            "_edited_by_human": bool(r.locked_fields),
        })
        records.append(rec)
    cols = [c for c, _ in list_columns(lst, evidence=evidence)]
    df = pd.DataFrame.from_records(records, columns=cols)
    for f in lst.fields or []:
        if f.get("type") in ("number", "integer") and f["name"] in df:
            df[f["name"]] = pd.to_numeric(df[f["name"]], errors="coerce")
    return df


def _apply_query(df: pd.DataFrame, query: str) -> pd.DataFrame:
    for tok in re.findall(r'(\w+):("[^"]*"|\S+)', query or ""):
        col, val = tok[0], tok[1].strip('"')
        if col not in df.columns:
            raise ValueError(f"Unknown column in query: {col}")
        df = df[df[col].astype(str).str.casefold() == val.casefold()]
    return df


async def query_list(db, organization, user, q) -> pd.DataFrame:
    lst = await resolve_list(db, organization, getattr(q, "list_id", None), getattr(q, "list_name", None))
    ds = await db.get(DataSource, lst.data_source_id)
    if not await can_access_agent(db, user, organization, ds, "view"):
        raise PermissionError("You do not have access to this list's agent")
    rows = (await db.execute(
        select(AgentListRow).where(AgentListRow.list_id == lst.id, AgentListRow.deleted_at.is_(None))
        .order_by(AgentListRow.created_at.asc(), AgentListRow.id.asc())
    )).scalars().all()
    wants_evidence = any(c.endswith(("__quote", "__page", "__verified")) for c in (q.columns or []))
    df = rows_to_frame(lst, rows, evidence=wants_evidence)
    df = _apply_query(df, q.query)
    if q.metrics:
        for g in q.group_by:
            if g not in df.columns:
                raise ValueError(f"Unknown group_by column: {g}")
        aggs = {}
        for m in q.metrics:
            if m.op == "count":
                aggs[m.name] = ("_row_id", "count")
            else:
                if not m.field or m.field not in df.columns:
                    raise ValueError(f"Metric {m.name} needs a valid field")
                aggs[m.name] = (m.field, {"count_distinct": "nunique", "sum": "sum", "avg": "mean"}[m.op])
        df = (df.groupby(q.group_by, dropna=False).agg(**aggs).reset_index() if q.group_by
              else pd.DataFrame([{name: df[col].agg(fn) for name, (col, fn) in aggs.items()}]))
    elif q.columns:
        unknown = [c for c in q.columns if c not in df.columns]
        if unknown:
            raise ValueError(f"Unknown columns: {', '.join(unknown)}")
        df = df[list(q.columns)]
    for s in reversed(q.sort or []):
        if s.field not in df.columns:
            raise ValueError(f"Unknown sort column: {s.field}")
        df = df.sort_values(s.field, ascending=s.direction == "asc", kind="stable")
    if q.limit:
        df = df.head(q.limit)
    df = df.reset_index(drop=True)
    df.attrs["bow_list"] = {"list_id": str(lst.id), "data_source_id": str(lst.data_source_id)}
    return df


async def report_has_lists(db, report, user, organization) -> bool:
    if report is None or user is None or organization is None:
        return False
    from app.models.report_data_source_association import report_data_source_association as assoc
    ds_ids = [str(r[0]) for r in (await db.execute(
        select(assoc.c.data_source_id).where(assoc.c.report_id == str(report.id))
    )).all()]
    if not ds_ids:
        return False
    live = (await db.execute(
        select(AgentList.data_source_id).where(AgentList.data_source_id.in_(ds_ids), AgentList.deleted_at.is_(None))
    )).scalars().all()
    if not live:
        return False
    return bool(await viewable_agent_ids(db, user, organization, set(map(str, live))))
