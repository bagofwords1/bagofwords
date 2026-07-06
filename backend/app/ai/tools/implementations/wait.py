"""Wait Tool — pause the current task, then automatically resume after a delay.

The agent calls this when the right next move is to wait for real-world time to
pass before retrying — an ETL/refresh in progress, a rate limit, "try again in
30 minutes". It ends the current turn (like clarify) and arms a one-shot resume
job via ``WaitService``. When the timer elapses, the agent is re-run on this
report with ``reason`` as the instruction and full history reloaded.

This is NOT a scheduled task: it's one-shot, ephemeral, and fires exactly once.
For recurring work use ``create_scheduled_task``.
"""

from typing import AsyncIterator, Dict, Any, Type
import logging

from pydantic import BaseModel

from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata
from app.ai.tools.schemas import WaitInput, WaitOutput
from app.ai.tools.schemas.events import (
    ToolEvent,
    ToolStartEvent,
    ToolEndEvent,
    ToolErrorEvent,
)

logger = logging.getLogger(__name__)


def _format_delay(minutes: int) -> str:
    """Human phrase for a minute count: '30 minutes', '2 hours', '1 hour 30 minutes'."""
    minutes = int(minutes)
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours, mins = divmod(minutes, 60)
    parts = [f"{hours} hour{'s' if hours != 1 else ''}"]
    if mins:
        parts.append(f"{mins} minute{'s' if mins != 1 else ''}")
    return " ".join(parts)


class WaitTool(Tool):
    """Pause the task and auto-resume after a delay (one-shot)."""

    @property
    def metadata(self) -> ToolMetadata:
        return ToolMetadata(
            name="wait",
            description=(
                "ACTION: Pause the CURRENT task and automatically resume it after a "
                "delay. Use this — instead of failing or asking the user — when the "
                "only sensible next step is to let real-world time pass and then "
                "retry: a data refresh / ETL still running, a rate limit, an "
                "external job you must poll later, or an explicit 'try again in N "
                "minutes'.\n\n"
                "How it works: this ENDS the current turn (nothing else runs during "
                "the wait — no polling, no partial work). After 'delay_minutes' the "
                "agent is resumed on this report with 'reason' as the instruction, "
                "and the full conversation history is reloaded, so you continue "
                "exactly where you left off.\n\n"
                "'delay_minutes' is in MINUTES — convert hours yourself ('2 hours' -> "
                "120). 'reason' must be a complete, self-contained instruction for "
                "what to do on resume (no user is present then). On resume the agent "
                "RUNS your 'reason', so put the 'do it again' / 'retry' action INSIDE "
                "'reason' — a single wait call handles the whole pause-then-redo. You "
                "do NOT also need create_scheduled_task for the follow-up.\n\n"
                "NOT for recurring work — 'every morning', 'each week', 'on the 1st' — "
                "those repeat on a calendar cadence, use create_scheduled_task. Use "
                "wait only for a SINGLE pause-and-retry of the task you're on right "
                "now (even when the user says 'and do it again' after the delay — one "
                "'again' after one wait is one-shot, not recurring)."
            ),
            category="action",
            version="1.0.0",
            input_schema=WaitInput.model_json_schema(),
            output_schema=WaitOutput.model_json_schema(),
            max_retries=1,
            timeout_seconds=15,
            idempotent=False,
            required_permissions=[],
            tags=["wait", "pause", "retry", "delay", "action"],
            examples=[
                {
                    "input": {
                        "delay_minutes": 30,
                        "reason": "Re-run the daily sales export — the source table was still refreshing.",
                    },
                    "description": "Retry a query in 30 minutes because the data wasn't ready.",
                },
                {
                    "input": {
                        "delay_minutes": 120,
                        "reason": "Check whether the overnight import finished and rebuild the dashboard.",
                    },
                    "description": "Wait 2 hours for an external job, then continue.",
                },
            ],
        )

    @property
    def input_model(self) -> Type[BaseModel]:
        return WaitInput

    @property
    def output_model(self) -> Type[BaseModel]:
        return WaitOutput

    async def run_stream(
        self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]
    ) -> AsyncIterator[ToolEvent]:
        try:
            data = WaitInput(**tool_input)
        except Exception as e:
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Invalid input: {str(e)}", "code": "INVALID_INPUT"},
            )
            return

        yield ToolStartEvent(
            type="tool.start",
            payload={"delay_minutes": data.delay_minutes, "reason": data.reason},
        )

        report = runtime_ctx.get("report")
        user = runtime_ctx.get("user")
        organization = runtime_ctx.get("organization")

        if not report or not user or not organization:
            msg = "Cannot wait: no active report context."
            yield ToolEndEvent(
                type="tool.end",
                payload={
                    "output": WaitOutput(status="error").model_dump(),
                    "observation": {"summary": msg, "success": False, "artifacts": []},
                },
            )
            return

        try:
            from app.services.wait_service import wait_service

            armed = wait_service.schedule_wait(
                report_id=str(report.id),
                user_id=str(user.id),
                organization_id=str(organization.id),
                reason=data.reason,
                delay_minutes=data.delay_minutes,
            )
        except Exception as e:
            logger.exception("Failed to arm wait: %s", e)
            yield ToolErrorEvent(
                type="tool.error",
                payload={"error": f"Failed to schedule wait: {str(e)}", "code": "SCHEDULE_FAILED"},
            )
            return

        pretty = _format_delay(data.delay_minutes)
        user_msg = f"Waiting {pretty}, then I'll pick this back up automatically."

        output = WaitOutput(
            status="scheduled",
            job_id=armed["job_id"],
            wake_at=armed["wake_at"],
            delay_minutes=data.delay_minutes,
            reason=data.reason,
        )

        # analysis_complete=True ends the turn cleanly (same terminal path as
        # clarify). final_answer becomes the user-facing message for this turn.
        yield ToolEndEvent(
            type="tool.end",
            payload={
                "output": output.model_dump(),
                "observation": {
                    "summary": f"Paused for {pretty}; will resume at {armed['wake_at']}.",
                    "artifacts": [],
                    "analysis_complete": True,
                    "final_answer": user_msg,
                    "success": True,
                },
            },
        )
