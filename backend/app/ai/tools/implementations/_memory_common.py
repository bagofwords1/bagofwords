"""Shared plumbing for the user-memory tools (create/edit/search_memory)."""
from __future__ import annotations

import contextlib
from typing import Any, Dict, Optional

from app.ai.tools.schemas.events import ToolEndEvent
from app.services.memory_rules import best_quote
from app.services.memory_service import is_memory_enabled

# Boundary text carried by every memory tool description (and mirrored in
# create_instruction's description) so the model routes each fact to the
# right system.
BOUNDARY = (
    "Test: is it a rule for how to answer or compute, or a fact about the user? Rules are "
    "instructions (org-wide, or this user's personal instructions); only facts are memory."
)


def is_machine_turn(head_completion) -> bool:
    """Scheduled runs, webhook deliveries, wait wakes, check-ins and eval runs
    are machine turns: the prompt was not typed by the user in this turn."""
    if head_completion is None:
        return False
    return bool(
        getattr(head_completion, "trigger_source", None)
        or getattr(head_completion, "scheduled_prompt_id", None)
        or getattr(head_completion, "webhook_id", None)
    )


def gate(runtime_ctx: Dict[str, Any]) -> Optional[str]:
    """Why memory tools can't run here, or None. The catalog already hides
    them in these cases; this is defense in depth."""
    if not is_memory_enabled(runtime_ctx.get("settings")):
        return "User memory is turned off for this organization."
    if runtime_ctx.get("mode") not in (None, "chat"):
        return "Memory tools are only available in normal chat turns."
    if is_machine_turn(runtime_ctx.get("head_completion")):
        return "Memory tools are not available in scheduled or automated runs."
    if runtime_ctx.get("user") is None or runtime_ctx.get("organization") is None:
        return "No user in this run, so there is no personal memory to use."
    return None


def user_message(runtime_ctx: Dict[str, Any]) -> str:
    head = runtime_ctx.get("head_completion")
    prompt = getattr(head, "prompt", None) if head is not None else None
    if isinstance(prompt, dict):
        return str(prompt.get("content") or "")
    return str(prompt or "")


def evidence_for(runtime_ctx: Dict[str, Any], text: str) -> Dict[str, Any]:
    """Provenance filled by code, never by the model: the report, the turn,
    and a ≤200-char quote of the user's own words."""
    report = runtime_ctx.get("report")
    head = runtime_ctx.get("head_completion")
    ev: Dict[str, Any] = {}
    if report is not None and getattr(report, "id", None):
        ev["report_id"] = str(report.id)
    if head is not None and getattr(head, "id", None):
        ev["completion_id"] = str(head.id)
    quote = best_quote(user_message(runtime_ctx), text)
    if quote:
        ev["quote"] = quote
    return ev


def record(runtime_ctx: Dict[str, Any], kind: str, item: Dict[str, Any]) -> None:
    """Append to the run's memory trace (refusals, writes) — persisted on the
    agent execution and shown in full only to the memory's owner."""
    trace = runtime_ctx.get("memory_trace")
    if isinstance(trace, dict):
        trace.setdefault(kind, []).append(item)


@contextlib.asynccontextmanager
async def memory_session(runtime_ctx: Dict[str, Any]):
    """A short-lived session so parallel memory calls in one batch never
    share (and interleave on) the agent's session."""
    maker = runtime_ctx.get("session_maker")
    if maker is None:
        yield runtime_ctx.get("db")
        return
    async with maker() as session:
        yield session


def fail(summary_prefix: str, message: str, code: str = "validation_error", **extra) -> ToolEndEvent:
    return ToolEndEvent(
        type="tool.end",
        payload={
            "output": {"success": False, "error": message, **extra},
            "observation": {
                "summary": f"{summary_prefix}: {message}",
                "error": {"type": code, "message": message},
            },
        },
    )
