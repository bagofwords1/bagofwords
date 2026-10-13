"""delegate — launch a bounded background sub-agent for one self-contained goal.

Returns a spawn receipt only. Children start after the whole planner batch is
recorded (SubAgentService.launch_pending) and report back as records in one
fan-in machine turn per wave. See docs/design/sub-agents.md.
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict, Type

from pydantic import BaseModel, ValidationError

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas.events import ToolEndEvent, ToolErrorEvent, ToolEvent, ToolStartEvent
from app.ai.tools.schemas.sub_agents import DelegateInput, DelegateOutput

logger = logging.getLogger(__name__)


class DelegateTool(Tool):

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="delegate",
            description=(
                "ACTION: Launch a background SUB-AGENT — a separate bounded agent that investigates ONE "
                "self-contained goal on its own and reports back later as a typed record (outcome, "
                "confidence, summary, findings, ruled_out, evidence ids, caveats). Use it when a request "
                "splits into 3+ INDEPENDENT sub-questions — one hypothesis per candidate cause in a "
                "root-cause ask, or the same check across many sources. Do NOT use it for a single "
                "query, for the final answer, or to avoid reading context you already have.\n\n"
                "Children are STATELESS: they know nothing of this conversation. Put everything in "
                "`goal` (what to test, window, what to report) and `context` (definitions, verified "
                "names, baseline numbers, prior query ids). Batch independent goals as several delegate "
                "calls in ONE response; they form one wave. After launching, write ONE line to the user "
                "and END YOUR TURN — results arrive in a later turn as <sub_agent_results>; never wait, "
                "poll, or redo the work yourself. The user can keep chatting meanwhile. Each child gets "
                "this conversation's agents (narrow with agent_ids), the schemas and instructions, "
                "read-only notes, and NO way to ask the user — an unanswerable goal returns "
                "'inconclusive'/'blocked' with a named unknown."
            ),
            category="action",
            version="1.0.0",
            input_schema=DelegateInput.model_json_schema(),
            output_schema=DelegateOutput.model_json_schema(),
            max_retries=0,
            timeout_seconds=30,
            idempotent=False,
            required_permissions=[],
            allowed_modes=["chat", "training"],
            observation_policy="always",
            digest_keys=["sub_agent_id", "wave_id", "title", "status", "record"],
            tags=["sub-agent", "delegate", "background", "parallel", "rca"],
            examples=[
                {
                    "input": {
                        "goal": "Did the 2026-09-10 premium price change reduce paid orders? Compare paid orders per day for sku_group='premium' 14 days before vs after 2026-09-10; report the delta and whether it could explain a 15% total drop.",
                        "context": "Metric = rows in orders with status='paid', daily grain. Baseline ~790 paid/day in early September. Table orders(date, region, platform, app_version, payment_provider, status, amount); price_changes(date, sku_group, old_price, new_price).",
                        "title": "Pricing change hypothesis",
                    },
                    "description": "one hypothesis of a root-cause fan-out",
                }
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return DelegateInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return DelegateOutput

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[ToolEvent]:
        try:
            data = DelegateInput(**(tool_input or {}))
        except ValidationError as e:
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Invalid input: {e}", "code": "INVALID_INPUT"})
            return

        yield ToolStartEvent(type="tool.start", payload={"title": data.title, "profile": data.profile})

        from app.services.sub_agent_service import sub_agent_service, sub_agents_enabled
        settings = runtime_ctx.get("settings")
        if not sub_agents_enabled(settings):
            yield ToolEndEvent(type="tool.end", payload={
                "output": DelegateOutput(status="refused", title=data.title, profile=data.profile,
                                         refused="Sub-agents are disabled for this organization.").model_dump(),
                "observation": {"summary": "Sub-agents are disabled for this organization; do the work yourself.", "success": False},
            })
            return
        if (runtime_ctx.get("mode") or "chat") == "subagent":
            yield ToolEndEvent(type="tool.end", payload={
                "output": DelegateOutput(status="refused", title=data.title, profile=data.profile,
                                         refused="A sub-agent cannot delegate.").model_dump(),
                "observation": {"summary": "You are a sub-agent and cannot delegate; solve the goal yourself.", "success": False},
            })
            return

        db = runtime_ctx.get("db")
        report = runtime_ctx.get("report")
        organization = runtime_ctx.get("organization")
        user = runtime_ctx.get("user")
        head = runtime_ctx.get("head_completion")
        system = runtime_ctx.get("system_completion")
        plan_decision_id = None
        tool_execution_id = runtime_ctx.get("tool_call_id")
        try:
            if tool_execution_id and db is not None:
                from app.models.tool_execution import ToolExecution
                te = await db.get(ToolExecution, str(tool_execution_id))
                plan_decision_id = getattr(te, "plan_decision_id", None) if te is not None else None
        except Exception:
            plan_decision_id = None

        try:
            receipt = await sub_agent_service.record_delegation(
                db, organization=organization, user=user, report=report,
                parent_head_completion=head, parent_system_completion=system,
                parent_execution_id=runtime_ctx.get("agent_execution_id"),
                tool_execution_id=str(tool_execution_id) if tool_execution_id else None,
                plan_decision_id=str(plan_decision_id) if plan_decision_id else None,
                data=data, settings=settings, parent_mode=runtime_ctx.get("mode"),
                parent_model=runtime_ctx.get("model"), small_model=runtime_ctx.get("small_model"),
            )
        except Exception as e:
            logger.exception("delegate: could not record sub-agent")
            yield ToolErrorEvent(type="tool.error", payload={"error": f"Could not start a sub-agent: {e}", "code": "SUB_AGENT_ERROR"})
            return

        out = DelegateOutput(**{k: v for k, v in receipt.items() if k in DelegateOutput.model_fields})
        if receipt.get("status") == "refused":
            obs = {"summary": f"Sub-agent '{data.title}' NOT started: {receipt.get('refused')}", "success": False,
                   "wave_id": receipt.get("wave_id"), "status": "refused"}
        else:
            agents = ", ".join(a.get("name") or a.get("id") for a in receipt.get("agents") or []) or "all attached agents"
            summary = (
                f"Started sub-agent '{data.title}' ({receipt['sub_agent_id']}) in wave {receipt['wave_id']} "
                f"[profile={data.profile}, model={receipt.get('model') or 'default'}, agents={agents}, "
                f"max_steps={receipt.get('max_steps')}]. Its record arrives in a later turn; write one line "
                f"to the user and end this turn."
            )
            if receipt.get("refused"):
                summary += f" Note: {receipt['refused']}."
            obs = {"summary": summary, "success": True, "sub_agent_id": receipt["sub_agent_id"],
                   "wave_id": receipt["wave_id"], "title": data.title, "status": "running",
                   "profile": data.profile}
        yield ToolEndEvent(type="tool.end", payload={"output": {**out.model_dump(), "max_steps": receipt.get("max_steps")}, "observation": obs})
