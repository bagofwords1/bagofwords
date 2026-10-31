"""delete_list_rows — remove rows from an Agent List, after the user confirms.

The agent picks rows by ``_row_id`` (from the list's bow table), by key value,
or all of them. Matching runs first: a selection that matches nothing returns
without a card. Otherwise the user sees a card with the count and the rows,
and nothing is deleted until they approve. Only the rows shown on the card
are deleted, so a row added while the card is open is kept.

Same gate as the REST delete: MANAGE on the owning agent, which must be
attached to the report. Not offered to sub-agents or in dashboard viewer chat.
"""
from __future__ import annotations


import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Type

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from app.services.conversation_surface import is_dashboard_chat
from app.ai.tools.base import Tool
from app.ai.tools.implementations._list_tool_common import (
    find_list,
    manageable_report_agents,
    review,
    session_maker_from,
    validation_errors,
)
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.delete_list_rows import DeleteListRowsInput, DeleteListRowsOutput
from app.ai.tools.schemas.events import ToolEndEvent, ToolErrorEvent, ToolEvent, ToolStartEvent

logger = logging.getLogger(__name__)

TOOL_NAME = "delete_list_rows"
# Rows listed on the card; the count covers the rest.
PREVIEW_ROWS = 20
PREVIEW_FIELDS = 4
PREVIEW_CHARS = 120


def _end(output: DeleteListRowsOutput, summary: str) -> ToolEndEvent:
    if not output.message:
        output.message = summary
    return ToolEndEvent(type="tool.end", payload={
        "output": output.model_dump(),
        "observation": {"summary": summary, "success": output.success, "artifacts": []},
    })


def _preview(lst, rows) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(columns, rows) for the card: the key field first, then the next few fields."""
    fields = list(lst.fields or [])
    fields.sort(key=lambda f: 0 if f["id"] == lst.key_field_id else 1)
    cols = fields[:PREVIEW_FIELDS]
    out = []
    for row in rows[:PREVIEW_ROWS]:
        vals = row.values or {}
        cells = {}
        for f in cols:
            v = (vals.get(f["id"]) or {}).get("value")
            if v is not None:
                v = str(v)
                cells[f["name"]] = v if len(v) <= PREVIEW_CHARS else v[:PREVIEW_CHARS - 1] + "…"
        out.append({"row_id": str(row.id), "values": cells})
    return [{"name": f["name"], "key": f["id"] == lst.key_field_id} for f in cols], out


class DeleteListRowsTool(Tool):

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name=TOOL_NAME,
            description=(
                "ACTION: permanently delete rows from a list on one of this report's agents (the lists behind "
                "the submit_<slug> tools). Pick rows with exactly one of: row_ids (the _row_id column — query the "
                "list's bow table first to select rows by any condition), keys (key-field values), or all=true "
                "to clear the list (the schema stays). The user sees a card with the matching rows and must "
                "confirm before anything is deleted; deletion cannot be undone. Use only when the user asked "
                "to remove rows — to correct a value, resubmit the row through its submit tool instead."
            ),
            category="action",
            version="1.0.0",
            input_schema=DeleteListRowsInput.model_json_schema(),
            output_schema=DeleteListRowsOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=300,
            idempotent=False,
            required_permissions=["manage", "manage_data_sources"],
            allowed_modes=["chat", "training"],
            tags=["lists", "structured-extraction"],
            examples=[
                {
                    "input": {"list_id": "Contracts", "keys": ["Acme Corp"], "reason": "User asked to remove Acme."},
                    "description": "Delete one row by its key value.",
                },
                {
                    "input": {"list_id": "<list-id>", "row_ids": ["<row-id-1>", "<row-id-2>"],
                              "reason": "User asked to drop contracts that expired before 2024."},
                    "description": "Delete rows found by querying the list's table first.",
                },
                {
                    "input": {"list_id": "Contracts", "all": True, "reason": "User asked to start the list over."},
                    "description": "Clear every row; the list and its schema stay.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return DeleteListRowsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return DeleteListRowsOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = DeleteListRowsInput(**(tool_input or {}))
        except ValidationError as e:
            errs = validation_errors(e)
            yield _end(DeleteListRowsOutput(success=False, status="invalid", errors=errs),
                       "Fix the request and call delete_list_rows again: " + " | ".join(errs))
            return
        except Exception as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return

        yield ToolStartEvent(type="tool.start", payload={"list_id": data.list_id})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        if not all([db, organization, user, report]):
            yield ToolErrorEvent(type="tool.error", payload={"error": "delete_list_rows requires an active report.", "code": "MISSING_CONTEXT"})
            return
        platform_context = runtime_ctx.get("platform_context")
        if is_dashboard_chat(report, platform_context):
            yield _end(DeleteListRowsOutput(success=False, status="permission_denied"),
                       "Rows cannot be deleted from a shared-artifact conversation.")
            return

        from app.models.agent_list import AgentListRow
        from app.services.agent_lists import service as svc
        from app.services.agent_lists.records import normalize_key

        agents = await manageable_report_agents(db, report, user, organization)
        by_agent = {str(d.id): d for d in agents}
        lists = await svc.live_lists_for_agents(db, list(by_agent)) if by_agent else []
        lst = find_list(lists, data.list_id)
        if lst is None:
            if not agents:
                summary = ("Only people who manage an agent can delete its list rows, and you don't manage any "
                           "of this report's agents. Tell the user to ask an agent manager or use the list page.")
                yield _end(DeleteListRowsOutput(success=False, status="permission_denied"), summary)
                return
            available = ", ".join(f"'{l.name}' (id {l.id})" for l in lists) or "none"
            yield _end(DeleteListRowsOutput(success=False, status="list_not_found"),
                       f"No list matching {data.list_id!r} on the agents you manage in this report. Available: {available}.")
            return
        ds = by_agent[str(lst.data_source_id)]
        base = dict(list_id=str(lst.id), list_name=lst.name, agent_id=str(ds.id), agent_name=ds.name)

        # ---- Match ----
        key_field = next((f for f in (lst.fields or []) if f["id"] == lst.key_field_id), None)
        if data.keys and key_field is None:
            yield _end(DeleteListRowsOutput(success=False, status="invalid", **base,
                                            errors=["keys: this list has no key field — use row_ids"]),
                       f"List '{lst.name}' has no key field. Query its table for _row_id and pass row_ids instead.")
            return
        q = select(AgentListRow).where(AgentListRow.list_id == lst.id, AgentListRow.deleted_at.is_(None))
        not_found: List[str] = []
        if data.row_ids:
            wanted = list(dict.fromkeys(str(r).strip() for r in data.row_ids if str(r).strip()))
            rows = list((await db.execute(q.where(AgentListRow.id.in_(wanted)))).scalars().all())
            hit = {str(r.id) for r in rows}
            not_found = [r for r in wanted if r not in hit]
        elif data.keys:
            norm = {}
            for k in data.keys:
                nk = normalize_key(key_field, k)
                if nk is not None:
                    norm.setdefault(nk, k)
            rows = list((await db.execute(q.where(AgentListRow.key_value.in_(list(norm))))).scalars().all())
            hit = {r.key_value for r in rows}
            not_found = [orig for nk, orig in norm.items() if nk not in hit]
        else:
            rows = list((await db.execute(q)).scalars().all())
        rows.sort(key=lambda r: (str(r.created_at or ""), str(r.id)))
        total = (await svc.row_counts(db, [str(lst.id)])).get(str(lst.id), 0)

        if not rows:
            what = "The list is already empty" if data.all else "No rows matched"
            summary = f"{what}; nothing to delete from '{lst.name}'."
            if not_found:
                summary += f" Not found: {', '.join(not_found[:20])}."
            yield _end(DeleteListRowsOutput(success=False, status="no_match", not_found=not_found,
                                            remaining=total, **base), summary)
            return

        columns, preview = _preview(lst, rows)

        # ---- Confirm ----
        decision: Dict[str, Any] = {}
        async for ev in review(runtime_ctx, tool_name=TOOL_NAME, payload={
            "action": "delete_rows",
            **base,
            "clear_all": bool(data.all),
            "count": len(rows),
            "total": total,
            "columns": columns,
            "rows": preview,
            "not_found": not_found[:20],
            "reason": data.reason,
        }):
            if isinstance(ev, dict):
                decision = ev
            else:
                yield ev

        if decision.get("timed_out") or not decision.get("approved"):
            status = "timed_out" if decision.get("timed_out") else "rejected"
            feedback = decision.get("feedback")
            if status == "timed_out":
                summary = "The delete was not confirmed in time; nothing was deleted. Ask the user whether to try again."
            else:
                summary = "The user cancelled the delete; nothing was deleted."
                summary += f" Their note: \"{feedback}\"." if feedback else " Do not retry unless they ask."
            yield _end(DeleteListRowsOutput(success=False, status=status, feedback=feedback, matched=len(rows),
                                            total=total, remaining=total, preview=preview, columns=columns, **base),
                       summary)
            return

        # ---- Delete (own session; only the confirmed rows) ----
        from app.models.data_source import DataSource as _DS

        ids = [str(r.id) for r in rows]
        maker = session_maker_from(runtime_ctx)
        try:
            async with maker() as s:
                s_ds = await s.get(_DS, str(ds.id))
                s_lst = await svc.get_list(s, s_ds, str(lst.id))
                deleted = await svc.delete_rows(s, s_lst, ids)
                remaining = (await svc.row_counts(s, [str(lst.id)])).get(str(lst.id), 0)
        except HTTPException as e:
            status = "list_not_found" if e.status_code == 404 else "failed"
            yield _end(DeleteListRowsOutput(success=False, status=status, matched=len(rows), **base),
                       f"Could not delete the rows: {e.detail}")
            return
        except Exception as e:
            logger.exception("delete_list_rows: delete failed")
            yield _end(DeleteListRowsOutput(success=False, status="failed", matched=len(rows), **base),
                       f"Could not delete the rows: {e}")
            return

        try:
            from app.ee.audit.service import audit_service
            await audit_service.log(
                db=db, organization_id=str(organization.id), action="agent_list.rows_deleted_by_agent",
                user_id=str(user.id), resource_type="data_source", resource_id=str(ds.id),
                details={"list_id": str(lst.id), "name": lst.name, "deleted": deleted,
                         "clear_all": bool(data.all), "report_id": str(report.id)},
            )
        except Exception:
            pass

        summary = f"Deleted {deleted} row(s) from list '{lst.name}'; {remaining} remain."
        if not_found:
            summary += f" Not found (skipped): {', '.join(not_found[:20])}."
        yield _end(DeleteListRowsOutput(success=True, status="deleted", matched=len(rows), deleted=deleted,
                                        total=total, remaining=remaining, not_found=not_found, preview=preview,
                                        columns=columns, **base), summary)
