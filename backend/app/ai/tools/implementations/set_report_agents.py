"""set_report_agents — focus the report on a chosen subset of agents.

ACTION tool. Sets ``report.focused_data_source_ids`` (the agents whose FULL
schema is rendered into context) and attaches any not-yet-attached agent to the
report so it renders. Follows search_agents: discover the right agent, then keep
it focused. Permission scope matches the mode — training focuses agents the actor
MANAGES; chat/deep focuses agents the actor can access. An empty list clears the
focus and reverts to the automatic roster/seed behavior.
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
                "other attached agents stay listed in the thin <available_agents> roster. "
                "Use after search_agents to pin the agent you'll work with. Pass the "
                "agent ids to focus, or an empty list to clear the focus (revert to the "
                "automatic selection). In training mode this also attaches an agent you "
                "manage to the report."
            ),
            category="action",
            version="1.0.0",
            input_schema=SetReportAgentsInput.model_json_schema(),
            output_schema=SetReportAgentsOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=20,
            idempotent=False,
            required_permissions=[],
            tags=["agent", "data_source", "focus"],
            allowed_modes=["chat", "deep", "training"],
            examples=[
                {"input": {"agent_ids": ["<agent-id>"], "title": "Focusing on the Sales agent"},
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

        yield ToolStartEvent(type="tool.start", payload={"agent_ids": data.agent_ids, "title": data.title})

        db = runtime_ctx.get("db")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        report = runtime_ctx.get("report")
        mode = runtime_ctx.get("mode") or "chat"
        if not all([db, organization, report]):
            yield ToolErrorEvent(type="tool.error", payload={"error": "set_report_agents requires an active report.", "code": "MISSING_CONTEXT"})
            return

        try:
            from app.ai.tools.implementations.agent_focus_common import user_can_focus_agent
            from app.models.data_source import DataSource
            from sqlalchemy import select

            requested = [str(x) for x in (data.agent_ids or [])]

            # Clear focus.
            if not requested:
                report.focused_data_source_ids = None
                db.add(report)
                await db.commit()
                msg = "Cleared the agent focus — all attached agents are back in scope (automatic selection)."
                out = SetReportAgentsOutput(success=True, focused_agent_ids=[], focused_agent_names=[], message=msg)
                yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                    "observation": {"summary": msg, "artifacts": []}})
                return

            attached_by_id = {str(ds.id): ds for ds in (report.data_sources or [])}
            valid_ids: List[str] = []
            valid_names: List[str] = []
            rejected: List[str] = []

            for did in requested:
                ds = attached_by_id.get(did)
                if ds is None:
                    # Not attached yet — must exist in the org AND be focusable.
                    ds = (await db.execute(
                        select(DataSource).where(
                            DataSource.id == did,
                            DataSource.organization_id == str(organization.id),
                        )
                    )).scalar_one_or_none()
                    if ds is None:
                        rejected.append(did)
                        continue
                    if not await user_can_focus_agent(db, organization, user, did, mode):
                        rejected.append(did)
                        continue
                    # Attach so its schema renders (persists to the report roster).
                    try:
                        report.data_sources.append(ds)
                    except Exception:
                        logger.exception("set_report_agents: attach failed for %s", did)
                        rejected.append(did)
                        continue
                else:
                    if not await user_can_focus_agent(db, organization, user, did, mode):
                        rejected.append(did)
                        continue
                valid_ids.append(str(ds.id))
                valid_names.append(getattr(ds, "name", "") or str(ds.id))

            if not valid_ids:
                msg = (
                    "None of the requested agents could be focused "
                    f"({'not found / not permitted' if rejected else 'no ids given'})."
                )
                out = SetReportAgentsOutput(success=False, rejected_ids=rejected, message=msg)
                yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                    "observation": {"summary": msg, "success": False, "artifacts": []}})
                return

            report.focused_data_source_ids = valid_ids
            db.add(report)
            await db.commit()

            names = ", ".join(valid_names)
            msg = (
                f"Focused this report on {len(valid_ids)} agent(s): {names}. "
                "Their full schema is in context from the next step; other agents remain in the roster."
            )
            if rejected:
                msg += f" Skipped {len(rejected)} id(s) that weren't found or permitted."
            out = SetReportAgentsOutput(
                success=True, focused_agent_ids=valid_ids, focused_agent_names=valid_names,
                rejected_ids=rejected, message=msg,
            )
            yield ToolEndEvent(type="tool.end", payload={"output": out.model_dump(),
                                "observation": {"summary": msg, "artifacts": [
                                    {"type": "agent_focus_set", "focused": valid_ids, "names": valid_names}]}})
        except Exception as e:
            logger.exception(f"set_report_agents failed: {e}")
            try:
                await db.rollback()
            except Exception:
                pass
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Failed to set focus: {e}", "code": "SET_FOCUS_FAILED"})
