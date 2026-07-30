"""set_report_agents — focus the report on a chosen subset of agents.

ACTION tool. Sets ``report.focused_data_source_ids`` (the agents whose FULL
schema is rendered into context). Selection semantics:

  - **Auto** (no manual agent selection on the report): every accessible agent
    is already in scope — the tool only writes the focus, freely.
  - **Manual** (the user picked agents): focusing within the selection is free;
    focusing an agent OUTSIDE it means expanding the user's scope, so the run
    pauses for their approval (Allow/Deny card showing the agents + the
    model's ``reason``). Denied/timed out → the tool soft-fails and the run
    continues within the current scope.
  - **training**: the actor curates agents they manage — attach without
    confirmation (mirrors create_agent).

An empty list clears the focus (back to automatic selection).
"""
from typing import Any, AsyncIterator, Dict, List, Type
import logging

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.set_report_agents import (
    SetReportAgentsInput,
    SetReportAgentsOutput,
)
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


class SetReportAgentsTool(Tool):
    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="set_report_agents",
            description=(
                "ACTION: Focus this report on specific agents (data sources). Their full "
                "tables/tools schema is loaded into context from the next step on; the "
                "other agents stay listed in the thin <available_agents> roster. Use after "
                "search_agents to pin the agent you'll work with. Pass the agent ids to "
                "focus, or an empty list to clear the focus (revert to automatic "
                "selection). When the user has manually selected agents on this report, "
                "focusing an agent OUTSIDE their selection asks them for approval — "
                "always set `reason` with one short sentence they will see. In training "
                "mode this attaches agents you manage without asking."
            ),
            category="action",
            version="1.1.0",
            input_schema=SetReportAgentsInput.model_json_schema(),
            output_schema=SetReportAgentsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=False,
            required_permissions=[],
            tags=["agent", "data_source", "focus"],
            allowed_modes=["chat", "deep", "training"],
            examples=[
                {"input": {"agent_ids": ["<agent-id>"], "reason": "Need order-level revenue tables",
                           "title": "Focusing on the Sales agent"},
                 "description": "Focus a single agent found via search_agents."},
                {"input": {"agent_ids": []}, "description": "Clear the focus."},
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return SetReportAgentsInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return SetReportAgentsOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = SetReportAgentsInput(**(tool_input or {}))
        except Exception as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return

        yield ToolStartEvent(type="tool.start", payload={
            "agent_ids": data.agent_ids, "reason": data.reason, "title": data.title,
        })

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        mode = runtime_ctx.get("mode") or "chat"
        if not all([db, organization, report]):
            yield ToolErrorEvent(type="tool.error", payload={"error": "set_report_agents requires an active report.", "code": "MISSING_CONTEXT"})
            return

        try:
            from app.ai.tools.implementations.agent_focus_common import (
                report_selection_is_auto,
                user_can_focus_agent,
            )
            from app.models.data_source import DataSource
            from sqlalchemy import select

            requested = [str(x) for x in (data.agent_ids or [])]

            # Clear focus.
            if not requested:
                report.focused_data_source_ids = None
                db.add(report)
                await db.commit()
                msg = "Cleared the agent focus — back to automatic selection."
                out = SetReportAgentsOutput(success=True, focused_agent_ids=[], focused_agent_names=[], message=msg)
                yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                    "observation": {"summary": msg, "artifacts": []}})
                return

            is_auto = report_selection_is_auto(report)
            attached_by_id = {str(ds.id): ds for ds in (report.data_sources or [])}
            from app.ai.tools.implementations.agent_focus_common import signin_required_ids

            valid: List[Any] = []          # (ds, needs_attach)
            rejected: List[str] = []
            for did in requested:
                ds = attached_by_id.get(did)
                if ds is None:
                    from sqlalchemy.orm import selectinload
                    ds = (await db.execute(
                        select(DataSource)
                        .options(selectinload(DataSource.connections))
                        .where(
                            DataSource.id == did,
                            DataSource.organization_id == str(organization.id),
                        )
                    )).scalar_one_or_none()
                    if ds is None or not await user_can_focus_agent(db, organization, user, did, mode):
                        rejected.append(did)
                        continue
                    # Under Auto the agent is already in scope (resolution covers
                    # it) — attaching would silently convert Auto into a manual
                    # pin. Only manual/training reports actually attach.
                    valid.append((ds, not is_auto))
                else:
                    valid.append((ds, False))

            if not valid:
                msg = (
                    "None of the requested agents could be focused "
                    f"({'not found / not permitted' if rejected else 'no ids given'})."
                )
                out = SetReportAgentsOutput(success=False, rejected_ids=rejected, message=msg)
                yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                    "observation": {"summary": msg, "success": False, "artifacts": []}})
                return

            # Sign-in gate: an agent the user hasn't connected yet (user_required
            # auth, e.g. PowerBI OBO) would fail every query — don't focus it;
            # point the model at the user's Connect flow instead.
            _need = await signin_required_ids(db, [ds for ds, _ in valid], user)
            if _need:
                blocked = [getattr(ds, "name", str(ds.id)) for ds, _ in valid if str(ds.id) in _need]
                valid = [(ds, na) for ds, na in valid if str(ds.id) not in _need]
                rejected.extend([str(i) for i in _need])
                if not valid:
                    msg = (
                        f"Agent(s) {', '.join(blocked)} require the user to sign in first "
                        "(Connect from the agent selector). Tell the user to connect, then retry."
                    )
                    out = SetReportAgentsOutput(success=False, rejected_ids=rejected, message=msg)
                    yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                        "observation": {"summary": msg, "success": False, "artifacts": []}})
                    return

            # Manual-selection guard: expanding beyond the user's own pick needs
            # their approval (chat/deep only — training curates freely).
            expansion = [ds for ds, needs_attach in valid if needs_attach]
            if expansion and mode != "training":
                async for ev in self._confirm_expansion(runtime_ctx, expansion, data.reason):
                    if isinstance(ev, dict):
                        # sentinel: denial/timeout end-event payload
                        out = SetReportAgentsOutput(
                            success=False,
                            rejected_ids=[str(d.id) for d in expansion],
                            message=ev["summary"],
                        )
                        yield ToolEndEvent(type="tool.end", payload={
                            "output": {**out.model_dump(), "approval": ev.get("approval")},
                            "observation": {"summary": ev["summary"], "success": False, "artifacts": []},
                        })
                        return
                    yield ev

            # Apply: attach what needs attaching, then set the focus.
            valid_ids: List[str] = []
            valid_names: List[str] = []
            added_names: List[str] = []
            for ds, needs_attach in valid:
                if needs_attach:
                    try:
                        report.data_sources.append(ds)
                        added_names.append(getattr(ds, "name", "") or str(ds.id))
                    except Exception:
                        logger.exception("set_report_agents: attach failed for %s", ds.id)
                        rejected.append(str(ds.id))
                        continue
                valid_ids.append(str(ds.id))
                valid_names.append(getattr(ds, "name", "") or str(ds.id))

            report.focused_data_source_ids = valid_ids
            db.add(report)
            await db.commit()

            names = ", ".join(valid_names)
            msg = (
                f"Focused this report on {len(valid_ids)} agent(s): {names}. "
                "Their full schema is in context from the next step."
            )
            if rejected:
                msg += f" Skipped {len(rejected)} id(s) that weren't found or permitted."
            out = SetReportAgentsOutput(
                success=True, focused_agent_ids=valid_ids, focused_agent_names=valid_names,
                added_agent_names=added_names, rejected_ids=rejected, message=msg,
            )
            # Carry the focused agents' full schema in THIS observation so the
            # very next planner turn can act on it even if the rendered context
            # lags — the exact failure behind the search/focus retry loop.
            try:
                from app.ai.tools.implementations.agent_focus_common import render_agents_full
                detail = await render_agents_full(
                    db, organization, report, user, [ds for ds, _ in valid][:5]
                )
            except Exception:
                logger.exception("set_report_agents: schema render for observation failed")
                detail = ""
            summary = f"{msg}\n\n{detail}".strip() if detail else msg
            yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                "observation": {"summary": summary, "artifacts": [
                                    {"type": "agent_focus_set", "focused": valid_ids, "names": valid_names}]}})
        except Exception as e:
            logger.exception(f"set_report_agents failed: {e}")
            try:
                await db.rollback()
            except Exception:
                pass
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Failed to set focus: {e}", "code": "SET_FOCUS_FAILED"})

    async def _confirm_expansion(self, runtime_ctx: Dict[str, Any], expansion: List[Any], reason: str | None):
        """Pause for the user's Allow/Deny on adding agents beyond their manual
        selection. Yields tool events; yields a final dict sentinel when the
        expansion is NOT approved (denied / timed out / non-interactive run)."""
        from app.ai.tools.confirmation import KIND_BUILTIN_TOOL, stream_user_confirmation
        from app.services.tool_policy_service import ToolPolicyService

        names = [getattr(d, "name", "") or str(d.id) for d in expansion]
        label = ", ".join(names)

        if not ToolPolicyService.is_interactive_run(runtime_ctx):
            yield {
                "summary": (
                    f"Agent(s) {label} are outside this report's user-selected agents and this "
                    "run cannot ask for approval (non-interactive). Continue with the current "
                    "agents, or the user can add them from the agent selector."
                ),
                "approval": None,
            }
            return

        result: Dict[str, Any] = {}
        async for ev in stream_user_confirmation(
            runtime_ctx,
            kind=KIND_BUILTIN_TOOL,
            tool_name="set_report_agents",
            payload={
                "agent_ids": [str(d.id) for d in expansion],
                "agent_names": names,
                "agents": [
                    {
                        "id": str(d.id), "name": getattr(d, "name", "") or "",
                        "icon": getattr(d, "icon", None),
                        "type": getattr((list(getattr(d, "connections", None) or []) or [None])[0], "type", None),
                        "connector_key": ((getattr((list(getattr(d, "connections", None) or []) or [None])[0], "config", None) or {}).get("catalog_key") if isinstance(getattr((list(getattr(d, "connections", None) or []) or [None])[0], "config", None), dict) else None),
                    }
                    for d in expansion
                ],
                "reason": reason or "",
            },
            result=result,
        ):
            yield ev

        approval = {
            "approved": result.get("approved", False),
            "timed_out": result.get("timed_out", True),
            "resolved_by_name": result.get("resolved_by_name"),
        }
        if not result.get("approved"):
            cause = "the approval request timed out" if result.get("timed_out") else "the user declined"
            yield {
                "summary": (
                    f"Did not add agent(s) {label} — {cause}. Continue the task with the "
                    "currently selected agents; only retry if the user explicitly asks."
                ),
                "approval": approval,
            }
