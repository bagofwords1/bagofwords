"""Shared plumbing for the list tools that pause on a review card (create_list,
edit_list, delete_list_rows): resolve the agent, validate the schema through the same
``ListSchemaIn`` contract the REST API uses, pause on the review card, and
shape the output.

Writes run on a short-lived session (``session_maker``): the service commits
and may raise on a race (duplicate name, list cap), and a rollback on the
agent loop's shared session would expire the run's own objects mid-loop.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import ValidationError
from sqlalchemy import select

from app.ai.tools.schemas.create_list import ListFieldOut, ListToolOutput
from app.ai.tools.schemas.events import ToolEndEvent

logger = logging.getLogger(__name__)

# Review pause must fit inside the tool's timeout_seconds (the runner's hard
# deadline); keepalives inside stream_user_confirmation keep its watchdog quiet.
REVIEW_TIMEOUT_S = 240.0
# Set on runtime_ctx after a successful write so AgentV2 re-registers the
# submit_<slug> tools for this run (see AgentV2._register_list_tools).
LISTS_CHANGED_KEY = "lists_changed"


def end_event(output: ListToolOutput, summary: str, artifacts: Optional[list] = None) -> ToolEndEvent:
    if not output.message:
        output.message = summary
    return ToolEndEvent(type="tool.end", payload={
        "output": output.model_dump(),
        "observation": {"summary": summary, "success": output.success, "artifacts": artifacts or []},
    })


def validation_errors(exc: ValidationError) -> List[str]:
    out: List[str] = []
    for e in exc.errors():
        loc = ".".join(str(p) for p in e.get("loc", ()) if p != "__root__")
        msg = e.get("msg", "invalid")
        if msg.startswith("Value error, "):
            msg = msg[len("Value error, "):]
        out.append(f"{loc}: {msg}" if loc else msg)
    return out


async def manageable_report_agents(db, report, user, organization) -> List[Any]:
    """Agents attached to the report that the user may MANAGE (lists follow
    the owning agent; creating or editing one is an agent-management act)."""
    from app.models.data_source import DataSource
    from app.models.report_data_source_association import report_data_source_association
    from app.services.agent_lists.access import can_access_agent

    if report is None or user is None or organization is None:
        return []
    ds_ids = [str(r[0]) for r in (await db.execute(
        select(report_data_source_association.c.data_source_id)
        .where(report_data_source_association.c.report_id == str(report.id))
    )).all()]
    if not ds_ids:
        return []
    agents = (await db.execute(
        select(DataSource).where(
            DataSource.id.in_(ds_ids),
            DataSource.organization_id == str(organization.id),
        )
    )).scalars().all()
    out = []
    for ds in agents:
        if await can_access_agent(db, user, organization, ds, "manage"):
            out.append(ds)
    out.sort(key=lambda d: (d.name or "", str(d.id)))
    return out


def agent_choices(agents: Iterable[Any]) -> List[Dict[str, Any]]:
    return [{"id": str(d.id), "name": d.name} for d in agents]


def pick_agent(agents: List[Any], agent_id: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    """Resolve the owning agent. Returns (agent, status) — status set when
    the agent could not be resolved."""
    if agent_id:
        ds = next((d for d in agents if str(d.id) == str(agent_id)), None)
        return (ds, None) if ds is not None else (None, "agent_not_found")
    if len(agents) == 1:
        return agents[0], None
    return None, "needs_agent"


def find_list(lists, ref: str):
    """Resolve by id, then slug, exact name (case-insensitive) or submit_<slug> tool name."""
    ref = (ref or "").strip()
    if not ref:
        return None
    for l in lists:
        if str(l.id) == ref:
            return l
    low = ref.casefold()
    for l in lists:
        if l.slug == low or l.name.strip().casefold() == low:
            return l
    if low.startswith("submit_"):
        rest = low[len("submit_"):]
        hits = [l for l in lists if rest == l.slug or rest.startswith(l.slug + "_")]
        if len(hits) == 1:
            return hits[0]
    return None


def fields_out(fields: List[Dict[str, Any]], key_field_id: Optional[str]) -> List[ListFieldOut]:
    return [
        ListFieldOut(
            id=f["id"], name=f["name"], type=f.get("type") or "string",
            description=f.get("description") or "", required=bool(f.get("required")),
            enum=f.get("enum"), unit=f.get("unit"), key=(f["id"] == key_field_id),
        )
        for f in (fields or [])
    ]


def key_name(fields: List[Dict[str, Any]], key_field_id: Optional[str]) -> Optional[str]:
    if not key_field_id:
        return None
    return next((f["name"] for f in (fields or []) if f["id"] == key_field_id), None)


def card_fields(fields: List[Dict[str, Any]], key_field_id: Optional[str],
                statuses: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Compact field rows for the review card (and the durable confirmation
    row, so the card re-renders after a reload)."""
    rows = []
    for f in fields or []:
        row = {
            "name": f["name"], "type": f.get("type") or "string",
            "required": bool(f.get("required")), "key": f["id"] == key_field_id,
            "description": (f.get("description") or "")[:300],
        }
        if f.get("enum"):
            row["enum"] = list(f["enum"])
        if f.get("unit"):
            row["unit"] = f["unit"]
        if statuses is not None:
            row["status"] = statuses.get(f["id"], "unchanged")
        rows.append(row)
    return rows


def output_for(lst, ds, *, status: str, change: Optional[str] = None, row_count: int = 0,
               message: Optional[str] = None, access=None) -> ListToolOutput:
    """``access`` (service.access_for_lists) names a shared list's table the
    way every other surface does."""
    from app.services.agent_lists.naming import tool_name_for
    from app.services.agent_lists.service import list_table_name

    fields = lst.fields or []
    return ListToolOutput(
        success=status in ("created", "updated"),
        status=status,
        message=message,
        list_id=str(lst.id),
        list_name=lst.name,
        slug=lst.slug,
        agent_id=str(ds.id),
        agent_name=ds.name,
        tool_name=tool_name_for(lst.slug, list_id=str(lst.id)),
        table_name=list_table_name(lst, access, ds),
        version=int(lst.version or 1),
        change=change,
        fields=fields_out(fields, lst.key_field_id),
        key_field=key_name(fields, lst.key_field_id),
        row_count=row_count,
    )


async def review(runtime_ctx: Dict[str, Any], *, tool_name: str, payload: Dict[str, Any]):
    """Pause on the card. Yields the tool events; the last yielded item is a
    plain dict ``{"approved", "timed_out", "feedback"}`` — callers forward
    everything else."""
    from app.ai.tools.confirmation import KIND_BUILTIN_TOOL, stream_user_confirmation

    result: Dict[str, Any] = {}
    async for ev in stream_user_confirmation(
        runtime_ctx, kind=KIND_BUILTIN_TOOL, tool_name=tool_name, payload=payload,
        result=result, timeout_s=REVIEW_TIMEOUT_S,
    ):
        yield ev
    decision = (result.get("response") or {}).get("response") or {}
    feedback = None
    if isinstance(decision, dict):
        feedback = (decision.get("feedback") or "").strip() or None
    yield {"approved": bool(result.get("approved")), "timed_out": bool(result.get("timed_out")), "feedback": feedback}


def declined_output(status: str, feedback: Optional[str], tool_name: str, noun: str) -> Tuple[ListToolOutput, str]:
    if status == "timed_out":
        summary = f"The {noun} was not reviewed in time; nothing was saved. Ask the user whether to propose it again."
    else:
        summary = f"The user declined the {noun}; nothing was saved."
        if feedback:
            summary += f" Their feedback: \"{feedback}\". Revise accordingly and call {tool_name} again."
        else:
            summary += " Ask what they would like changed before proposing again."
    return ListToolOutput(success=False, status=status, feedback=feedback), summary


def mark_lists_changed(runtime_ctx: Dict[str, Any]) -> None:
    runtime_ctx[LISTS_CHANGED_KEY] = True


def session_maker_from(runtime_ctx: Dict[str, Any]):
    maker = runtime_ctx.get("session_maker")
    if maker is None:
        from app.dependencies import async_session_maker
        maker = async_session_maker
    return maker
