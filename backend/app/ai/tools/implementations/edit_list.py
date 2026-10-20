"""edit_list — change an Agent List's schema during training (one approval).

Partial update over the list's current fields: add, update (by current name;
renames keep the field id and its rows), remove, change the key, flags, name
or description. The result is validated through ``ListSchemaIn`` like the
REST ``PUT``, classified as none / additive / breaking (``classify_schema_change``),
shown on a review card with the per-field diff, and saved on approval. A
breaking edit bumps the list version exactly as the editor does; rows written
under the old version are marked stale until re-extracted.

Gates: training mode and MANAGE on the owning agent.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple, Type

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.implementations._list_tool_common import (
    card_fields,
    declined_output,
    end_event,
    manageable_report_agents,
    mark_lists_changed,
    output_for,
    review,
    session_maker_from,
    validation_errors,
)
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_list import ListToolOutput
from app.ai.tools.schemas.edit_list import EditListInput
from app.ai.tools.schemas.events import ToolErrorEvent, ToolEvent, ToolStartEvent

logger = logging.getLogger(__name__)

TOOL_NAME = "edit_list"


def _find_list(lists, ref: str):
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


def apply_edits(current: List[Dict[str, Any]], data: EditListInput) -> Tuple[List[Dict[str, Any]], Dict[str, str], List[str]]:
    """Compose the new field list from the current one. Returns
    (fields, status_by_field_id, errors). Field ids are preserved so renames
    and attribute edits are not removals."""
    errors: List[str] = []
    by_name = {f["name"]: dict(f) for f in current}
    statuses: Dict[str, str] = {f["id"]: "unchanged" for f in current}
    removed: Dict[str, Dict[str, Any]] = {}

    for name in data.remove_fields or []:
        f = by_name.pop(name, None)
        if f is None:
            errors.append(f"remove_fields: no field named '{name}'")
        else:
            removed[f["id"]] = f
            statuses[f["id"]] = "removed"

    for upd in data.update_fields or []:
        f = by_name.get(upd.name)
        if f is None:
            errors.append(f"update_fields: no field named '{upd.name}'")
            continue
        changed = False
        for attr in ("type", "description", "required", "enum", "unit"):
            val = getattr(upd, attr)
            if val is not None and val != f.get(attr):
                f[attr] = val
                changed = True
        if upd.new_name and upd.new_name != f["name"]:
            if upd.new_name in by_name:
                errors.append(f"update_fields: a field named '{upd.new_name}' already exists")
                continue
            by_name.pop(upd.name)
            f["name"] = upd.new_name
            by_name[upd.new_name] = f
            changed = True
        if changed:
            statuses[f["id"]] = "changed"

    # Renames moved keys in by_name; keep the list's current order (by id).
    ordered = sorted(by_name.values(), key=lambda f: next(i for i, c in enumerate(current) if c["id"] == f["id"]))

    for spec in data.add_fields or []:
        f = spec.model_dump()
        f["id"] = None  # ListFieldDef assigns a stable id
        if f["name"] in by_name:
            errors.append(f"add_fields: a field named '{f['name']}' already exists")
            continue
        by_name[f["name"]] = f
        ordered.append(f)

    return ordered, statuses, errors


class EditListTool(Tool):

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name=TOOL_NAME,
            description=(
                "ACTION (training): change the schema of an existing list on one of this report's agents — "
                "add, rename or re-describe fields, change a type or allowed values, drop a field, set the key, "
                "or rename the list. Partial: only what you pass changes; renames keep the field's rows. Pass "
                "the list id (from create_list, or the submit_<slug> tool's description) or its exact name. The "
                "user sees the per-field diff on a review card marked additive or BREAKING (type change, removed "
                "field, new required field, key change — bumps the version and marks old rows stale) and approves "
                "or declines with feedback. Prefer additive edits; propose a breaking one only when the user asked "
                "for it or the current schema is wrong."
            ),
            category="action",
            version="1.0.0",
            input_schema=EditListInput.model_json_schema(),
            output_schema=ListToolOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=300,
            idempotent=False,
            required_permissions=["manage", "manage_data_sources"],
            allowed_modes=["training"],
            tags=["training", "lists", "structured-extraction", "agent-building"],
            examples=[
                {
                    "input": {
                        "list_id": "<list-id>",
                        "add_fields": [{"name": "signed_on", "type": "date", "description": "Date on the signature page."}],
                        "update_fields": [{"name": "annual_value", "description": "Total value per 12 months excluding VAT, in the contract's currency."}],
                        "reason": "User asked to track the signing date as well.",
                    },
                    "description": "Additive edit: one new optional field and a clearer description.",
                },
                {
                    "input": {"list_id": "Contracts", "remove_fields": ["auto_renew"], "reason": "User said auto-renewal is never stated in these contracts."},
                    "description": "Breaking edit (field removal) — the card flags it; the version bumps on approval.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return EditListInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListToolOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = EditListInput(**(tool_input or {}))
        except ValidationError as e:
            yield end_event(
                ListToolOutput(success=False, status="invalid", errors=validation_errors(e)),
                "The edit has problems — fix them and call edit_list again: " + " | ".join(validation_errors(e)),
            )
            return
        except Exception as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return

        yield ToolStartEvent(type="tool.start", payload={"list_id": data.list_id, "name": data.name})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        if not all([db, organization, user, report]):
            yield ToolErrorEvent(type="tool.error", payload={"error": "edit_list requires an active report.", "code": "MISSING_CONTEXT"})
            return

        from app.schemas.agent_list import ListSchemaIn
        from app.services.agent_lists import service as svc
        from app.services.agent_lists.schema_change import classify_schema_change

        agents = await manageable_report_agents(db, report, user, organization)
        if not agents:
            yield end_event(
                ListToolOutput(success=False, status="permission_denied"),
                "Lists can only be edited on agents you manage, and none of this report's agents is one.",
            )
            return
        by_agent = {str(d.id): d for d in agents}
        lists = await svc.live_lists_for_agents(db, list(by_agent))
        lst = _find_list(lists, data.list_id)
        if lst is None:
            available = ", ".join(f"'{l.name}' (id {l.id}, agent {by_agent[str(l.data_source_id)].name})" for l in lists) or "none"
            yield end_event(
                ListToolOutput(success=False, status="list_not_found"),
                f"No list matching {data.list_id!r} on the agents you manage in this report. Available: {available}.",
            )
            return
        ds = by_agent[str(lst.data_source_id)]
        current = [dict(f) for f in (lst.fields or [])]

        # ---- Compose + validate the new schema ----
        new_fields, statuses, errors = apply_edits(current, data)
        if errors:
            yield end_event(
                output_for(lst, ds, status="invalid", message=None).model_copy(update={"errors": errors, "success": False}),
                "The edit has problems — fix them and call edit_list again: " + " | ".join(errors),
            )
            return
        current_key = next((f["name"] for f in current if f["id"] == lst.key_field_id), None)
        if data.key_field is None:
            key_field: Optional[str] = current_key
            if key_field is not None and key_field not in {f["name"] for f in new_fields}:
                # The key was renamed or removed: follow the rename, clear on removal.
                key_field = next((f["name"] for f in new_fields if f["id"] == lst.key_field_id), None)
        elif data.key_field == "":
            key_field = None
        else:
            key_field = data.key_field

        try:
            payload = ListSchemaIn(
                name=data.name if data.name is not None else lst.name,
                description=data.description if data.description is not None else (lst.description or ""),
                fields=new_fields,
                key_field=key_field,
                require_evidence=data.require_evidence if data.require_evidence is not None else bool(lst.require_evidence),
                allow_viewer_submissions=bool(lst.allow_viewer_submissions),
                keep_human_edits=data.keep_human_edits if data.keep_human_edits is not None else bool(lst.keep_human_edits),
            )
        except ValidationError as e:
            errs = validation_errors(e)
            yield end_event(
                output_for(lst, ds, status="invalid").model_copy(update={"errors": errs, "success": False}),
                "The edit has problems — fix them and call edit_list again: " + " | ".join(errs),
            )
            return

        fields = [f.model_dump() for f in payload.fields]
        for f in fields:
            statuses.setdefault(f["id"], "added")
        key_id = next((f["id"] for f in fields if f["name"] == payload.key_field or f["id"] == payload.key_field), None) if payload.key_field else None
        change = classify_schema_change(current, lst.key_field_id, fields, key_id)
        meta_changed = (
            lst.name != payload.name or (lst.description or "") != (payload.description or "")
            or bool(lst.require_evidence) != payload.require_evidence
            or bool(lst.keep_human_edits) != payload.keep_human_edits
        )
        if change == "none" and not meta_changed:
            yield end_event(
                output_for(lst, ds, status="no_change", change="none"),
                f"Nothing to change: '{lst.name}' already matches this edit.",
            )
            return
        if change == "none":
            change = "additive"

        counts = await svc.row_counts(db, [str(lst.id)])
        row_count = counts.get(str(lst.id), 0)

        # Card shows removed fields too (greyed), so include them after the kept ones.
        removed_cards = card_fields([f for f in current if statuses.get(f["id"]) == "removed"], lst.key_field_id, statuses)
        decision: Dict[str, Any] = {}
        async for ev in review(runtime_ctx, tool_name=TOOL_NAME, payload={
            "action": "edit",
            "agent_id": str(ds.id),
            "agent_name": ds.name,
            "list_id": str(lst.id),
            "list_name": payload.name,
            "previous_name": lst.name if lst.name != payload.name else None,
            "description": payload.description or "",
            "fields": card_fields(fields, key_id, statuses) + removed_cards,
            "key_field": payload.key_field,
            "require_evidence": payload.require_evidence,
            "keep_human_edits": payload.keep_human_edits,
            "change": change,
            "version": int(lst.version or 1),
            "row_count": row_count,
            "reason": data.reason,
        }):
            if isinstance(ev, dict):
                decision = ev
            else:
                yield ev

        if decision.get("timed_out") or not decision.get("approved"):
            status = "timed_out" if decision.get("timed_out") else "rejected"
            out, summary = declined_output(status, decision.get("feedback"), TOOL_NAME, "change")
            out.list_id, out.list_name, out.agent_id, out.agent_name = str(lst.id), lst.name, str(ds.id), ds.name
            yield end_event(out, summary)
            return

        # ---- Save (own session) ----
        from app.models.data_source import DataSource as _DS

        maker = session_maker_from(runtime_ctx)
        try:
            async with maker() as s:
                s_ds = await s.get(_DS, str(ds.id))
                s_lst = await svc.get_list(s, s_ds, str(lst.id))
                saved_change = await svc.update_list(s, s_ds, s_lst, payload)
                out = output_for(s_lst, s_ds, status="updated", change=saved_change, row_count=row_count)
        except HTTPException as e:
            status = "duplicate_name" if e.status_code == 409 else "list_not_found" if e.status_code == 404 else "failed"
            yield end_event(ListToolOutput(success=False, status=status, list_id=str(lst.id), list_name=lst.name,
                                           agent_id=str(ds.id), agent_name=ds.name),
                            f"Could not save the change: {e.detail}")
            return
        except Exception as e:
            logger.exception("edit_list: write failed")
            yield end_event(ListToolOutput(success=False, status="failed", list_id=str(lst.id), list_name=lst.name,
                                           agent_id=str(ds.id), agent_name=ds.name),
                            f"Could not save the change: {e}")
            return

        mark_lists_changed(runtime_ctx)
        try:
            from app.ee.audit.service import audit_service
            await audit_service.log(
                db=db, organization_id=str(organization.id), action="agent_list.edited_by_agent",
                user_id=str(user.id), resource_type="data_source", resource_id=str(ds.id),
                details={"list_id": out.list_id, "name": out.list_name, "change": out.change,
                         "version": out.version, "report_id": str(report.id)},
            )
        except Exception:
            pass

        summary = f"Updated list '{out.list_name}' on agent '{ds.name}' ({out.change} change"
        if out.change == "breaking":
            summary += f", now version {out.version}; {row_count} existing row(s) are marked stale until re-extracted"
        summary += f"). {len(out.fields)} fields" + (f", key: {out.key_field}" if out.key_field else "") + \
                   f". The {out.tool_name} tool now uses the new schema."
        yield end_event(out, summary, artifacts=[{"type": "agent_list", "id": out.list_id, "name": out.list_name}])
