"""create_list — define a typed Agent List during training (one approval).

1. The agent proposes the schema (name, fields, key, evidence rule) for one
   of the report's agents. It is validated with the same ``ListSchemaIn``
   contract as ``POST /data_sources/{id}/lists``; problems come back as
   ``invalid`` with errors to fix — no review card for a broken schema.
2. The tool pauses on a review card. The user approves or declines with
   feedback, which goes back to the agent to revise and call again.
3. On approval the list is created live (no draft state — same as the Lists
   editor in the Knowledge Explorer) and ``runtime_ctx['lists_changed']`` is
   set so AgentV2 registers the new ``submit_<slug>`` tool for the rest of
   this run.

Gates: training mode (``allowed_modes``) and MANAGE on the owning agent —
the same rule the REST route enforces (lists follow the agent).
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Type

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.implementations._list_tool_common import (
    agent_choices,
    card_fields,
    declined_output,
    end_event,
    manageable_report_agents,
    mark_lists_changed,
    output_for,
    pick_agent,
    review,
    session_maker_from,
    validation_errors,
)
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.create_list import CreateListInput, ListToolOutput
from app.ai.tools.schemas.events import ToolErrorEvent, ToolEvent, ToolStartEvent

logger = logging.getLogger(__name__)

TOOL_NAME = "create_list"


class CreateListTool(Tool):

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name=TOOL_NAME,
            description=(
                "ACTION (training): define a new LIST on one of this report's agents — a typed table the "
                "agent fills in later through its submit_<list> tool (one row per contract, invoice, ticket, "
                "supplier…). Propose the schema: a clear name, what one row represents, 1–40 snake_case fields "
                "with types and a description per field (the extraction instruction), a key field so re-runs "
                "update rows instead of duplicating them, and require_evidence for document extraction. The user "
                "reviews the schema on a card and approves, or declines with feedback — then revise and call "
                "again. On approval the list exists immediately and its submit_<slug> tool is available in THIS "
                "session, so you can extract into it right away. Before proposing: look at the real documents or "
                "query results so field names and types match what is actually there; check the agent's existing "
                "lists (their submit_* tools) and prefer edit_list over a near-duplicate list. Omit agent_id when "
                "the report has one agent you manage."
            ),
            category="action",
            version="1.0.0",
            input_schema=CreateListInput.model_json_schema(),
            output_schema=ListToolOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=300,
            idempotent=False,
            # MANAGE on an agent (resource grant) or org-wide agent admin; the
            # tool re-checks the specific agent at run time.
            required_permissions=["manage", "manage_data_sources"],
            allowed_modes=["training"],
            tags=["training", "lists", "structured-extraction", "agent-building"],
            examples=[
                {
                    "input": {
                        "name": "Contracts",
                        "description": "One row per customer contract found in the shared drive. Values come from the signed PDF, not the draft.",
                        "fields": [
                            {"name": "counterparty", "type": "string", "required": True, "description": "Legal name of the other party as written on the signature page."},
                            {"name": "annual_value", "type": "number", "unit": "USD", "description": "Total contract value per 12 months, excluding VAT."},
                            {"name": "currency", "type": "enum", "enum": ["USD", "EUR", "ILS"], "description": "Currency of annual_value."},
                            {"name": "renewal_date", "type": "date", "description": "Next renewal or expiry date."},
                            {"name": "auto_renew", "type": "boolean", "description": "True when the term renews automatically."},
                        ],
                        "key_field": "counterparty",
                        "require_evidence": True,
                        "reason": "The user wants every vendor contract's renewal terms in one table.",
                    },
                    "description": "Document extraction list with a key and mandatory source quotes.",
                }
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return CreateListInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return ListToolOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = CreateListInput(**(tool_input or {}))
        except ValidationError as e:
            yield end_event(
                ListToolOutput(success=False, status="invalid", errors=validation_errors(e)),
                "The list schema has problems — fix them and call create_list again: " + " | ".join(validation_errors(e)),
            )
            return
        except Exception as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return

        yield ToolStartEvent(type="tool.start", payload={
            "name": data.name,
            "fields": [{"name": f.name, "type": f.type} for f in data.fields],
            "key_field": data.key_field,
            "agent_id": data.agent_id,
        })

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        if not all([db, organization, user, report]):
            yield ToolErrorEvent(type="tool.error", payload={"error": "create_list requires an active report.", "code": "MISSING_CONTEXT"})
            return

        # ---- Owning agent (must be attached to this report and managed by the user) ----
        agents = await manageable_report_agents(db, report, user, organization)
        if not agents:
            yield end_event(
                ListToolOutput(success=False, status="permission_denied"),
                "Lists can only be created on agents you manage, and none of this report's agents is one. "
                "Ask an agent manager to add the list, or attach an agent you manage.",
            )
            return
        ds, why = pick_agent(agents, data.agent_id)
        if ds is None:
            choices = agent_choices(agents)
            names = ", ".join(f"{c['name']} (id {c['id']})" for c in choices)
            if why == "agent_not_found":
                msg = f"No agent with id {data.agent_id!r} is attached to this report and managed by you. Choose one of: {names}."
            else:
                msg = f"Several agents are attached — pass agent_id for the one that owns the list: {names}."
            yield end_event(ListToolOutput(success=False, status=why, agents=choices), msg)
            return

        # ---- Schema contract (same as the REST API) ----
        from app.schemas.agent_list import MAX_LISTS_PER_AGENT, ListSchemaIn
        from app.services.agent_lists import service as svc

        try:
            payload = ListSchemaIn(
                name=data.name,
                description=data.description or "",
                fields=[f.model_dump() for f in data.fields],
                key_field=data.key_field,
                require_evidence=data.require_evidence,
                allow_viewer_submissions=False,
                keep_human_edits=data.keep_human_edits,
            )
        except ValidationError as e:
            errs = validation_errors(e)
            yield end_event(
                ListToolOutput(success=False, status="invalid", errors=errs),
                "The list schema has problems — fix them and call create_list again: " + " | ".join(errs),
            )
            return

        existing = await svc.live_lists_for_agents(db, [ds.id])
        if len(existing) >= MAX_LISTS_PER_AGENT:
            yield end_event(
                ListToolOutput(success=False, status="limit_reached", agent_id=str(ds.id), agent_name=ds.name),
                f"Agent '{ds.name}' already has {MAX_LISTS_PER_AGENT} lists (the maximum). Edit or remove one first.",
            )
            return
        dup = next((l for l in existing if l.name.strip().casefold() == payload.name.casefold()), None)
        if dup is not None:
            # Name the duplicate's table the way every other surface does
            # (its name_agent), not after whichever agent we created through.
            dup_access = (await svc.access_for_lists(db, user, organization, [dup]))[str(dup.id)]
            yield end_event(
                output_for(dup, ds, status="duplicate_name", access=dup_access),
                f"Agent '{ds.name}' already has a list named '{dup.name}' (id {dup.id}). Use edit_list to change it, "
                "or pick a different name.",
            )
            return

        # ---- Review ----
        fields = [f.model_dump() for f in payload.fields]
        key_id = next((f["id"] for f in fields if f["name"] == payload.key_field or f["id"] == payload.key_field), None) if payload.key_field else None
        decision: Dict[str, Any] = {}
        async for ev in review(runtime_ctx, tool_name=TOOL_NAME, payload={
            "action": "create",
            "agent_id": str(ds.id),
            "agent_name": ds.name,
            "list_name": payload.name,
            "description": payload.description or "",
            "fields": card_fields(fields, key_id),
            "key_field": payload.key_field,
            "require_evidence": payload.require_evidence,
            "keep_human_edits": payload.keep_human_edits,
            "reason": data.reason,
        }):
            if isinstance(ev, dict):
                decision = ev
            else:
                yield ev

        if decision.get("timed_out") or not decision.get("approved"):
            status = "timed_out" if decision.get("timed_out") else "rejected"
            out, summary = declined_output(status, decision.get("feedback"), TOOL_NAME, "list")
            out.agent_id, out.agent_name, out.list_name = str(ds.id), ds.name, payload.name
            yield end_event(out, summary)
            return

        # ---- Create (own session: the service commits and may raise on a race) ----
        from app.models.data_source import DataSource as _DS
        from app.models.organization import Organization as _Org
        from app.models.user import User as _User

        maker = session_maker_from(runtime_ctx)
        try:
            async with maker() as s:
                s_org = await s.get(_Org, str(organization.id))
                s_user = await s.get(_User, str(user.id))
                s_ds = await s.get(_DS, str(ds.id))
                created = await svc.create_list(s, s_org, s_ds, payload, s_user)
                out = output_for(created, s_ds, status="created")
        except HTTPException as e:
            status = "limit_reached" if e.status_code == 400 else "duplicate_name" if e.status_code == 409 else "failed"
            yield end_event(ListToolOutput(success=False, status=status, agent_id=str(ds.id), agent_name=ds.name),
                            f"Could not create the list: {e.detail}")
            return
        except Exception as e:
            logger.exception("create_list: write failed")
            yield end_event(ListToolOutput(success=False, status="failed", agent_id=str(ds.id), agent_name=ds.name),
                            f"Could not create the list: {e}")
            return

        mark_lists_changed(runtime_ctx)
        try:
            from app.ee.audit.service import audit_service
            await audit_service.log(
                db=db, organization_id=str(organization.id), action="agent_list.created_by_agent",
                user_id=str(user.id), resource_type="data_source", resource_id=str(ds.id),
                details={"list_id": out.list_id, "name": out.list_name, "fields": len(out.fields),
                         "report_id": str(report.id)},
            )
        except Exception:
            pass

        summary = (
            f"Created list '{out.list_name}' on agent '{ds.name}' with {len(out.fields)} fields"
            + (f" (key: {out.key_field})" if out.key_field else "")
            + f". Its tool {out.tool_name} is now available in this session — extract records with it; "
            f"the rows are queryable as {out.table_name}."
        )
        yield end_event(out, summary, artifacts=[{"type": "agent_list", "id": out.list_id, "name": out.list_name}])
