"""Rehydrate native tool turns from the normalized report audit records.

There is deliberately no second transcript table. ``Completion`` owns user
and assistant text, ``PlanDecision``/``CompletionBlock`` own turn grouping,
and ``ToolExecution`` owns call identity plus the bounded result projection.
This module only joins those already-canonical records into the in-memory
``Transcript`` consumed by PromptBuilderV3.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from types import SimpleNamespace
from typing import Any

from app.ai.context.parts import (
    Outcome,
    ToolCallPart,
    ToolResultPart,
    estimate_tokens,
)
from app.ai.context.transcript import Transcript
from app.ai.persisted_summary import (
    build_tool_context_summary,
    tool_context_for_replay,
)


def _outcome(status: Any) -> Outcome:
    value = str(status or "").lower()
    if value in {"success", "completed"}:
        return Outcome.SUCCESS
    if value in {"cancelled", "canceled", "stopped", "interrupted"}:
        return Outcome.INTERRUPTED
    if value in {"error", "failed", "denied"}:
        return Outcome.DENIED if value == "denied" else Outcome.FAILED
    # A row staged before dispatch/result and encountered by a later
    # completion cannot be classified safely. Never claim it failed or retry
    # it automatically: the side effect may have happened before the process
    # died.
    return Outcome.OUTCOME_UNKNOWN


def _result_content(tool_execution: Any, outcome: Outcome) -> tuple[str, str | None]:
    if outcome is Outcome.OUTCOME_UNKNOWN:
        body = {
            "summary": (
                "The previous process ended before this call recorded a result. "
                "Its outcome is unknown; inspect current state before deciding "
                "whether a retry is safe."
            ),
            "status": "outcome_unknown",
        }
    else:
        body = getattr(tool_execution, "context_summary_json", None)
        body = tool_context_for_replay(body)
        if not isinstance(body, dict):
            raw_projection = getattr(tool_execution, "result_json", None)
            body = build_tool_context_summary(
                getattr(tool_execution, "tool_name", None), raw_projection
            )
        if not isinstance(body, dict):
            body = {}
        if getattr(tool_execution, "result_summary", None) and "summary" not in body:
            body = {"summary": tool_execution.result_summary, **body}
        if getattr(tool_execution, "error_message", None) and "error" not in body:
            body = {**body, "error": tool_execution.error_message}
        if not body:
            body = {"summary": "The call completed without a stored result body."}

    content = json.dumps(body, default=str, ensure_ascii=False, separators=(",", ":"))
    digest = getattr(tool_execution, "result_summary", None)
    if not digest and isinstance(body, dict):
        digest = body.get("summary")
    return content, str(digest) if digest else None


def build_durable_transcript(
    *,
    completions: list[Any],
    blocks_by_completion: dict[str, list[Any]],
    tool_exec_by_id: dict[str, Any],
) -> tuple[Transcript, set[str]]:
    """Build ordered, paired tool turns for the detailed message window.

    Parallel calls sharing one plan decision become one assistant turn and one
    user result turn. Legacy rows receive stable ids derived from their primary
    key, so report reloads are deterministic even before the new columns exist.
    """
    transcript = Transcript()
    represented: set[str] = set()

    for completion in completions:
        if getattr(completion, "role", None) != "system":
            continue
        blocks = blocks_by_completion.get(str(completion.id), [])
        groups: OrderedDict[tuple[str, str], list[tuple[Any, Any]]] = OrderedDict()
        blocked_execution_ids: set[str] = set()
        for block in blocks:
            execution_id = str(getattr(block, "tool_execution_id", "") or "")
            tool_execution = tool_exec_by_id.get(execution_id)
            if not execution_id or tool_execution is None:
                continue
            blocked_execution_ids.add(execution_id)
            decision_id = str(getattr(block, "plan_decision_id", "") or "")
            # Standalone tools must not collapse into one synthetic parallel
            # turn merely because they all have plan_decision_id=NULL.
            group_id = decision_id or f"standalone:{execution_id}"
            group_key = (str(getattr(block, "agent_execution_id", "") or ""), group_id)
            groups.setdefault(group_key, []).append((block, tool_execution))

        # A process can die after the decision+intent transaction but before
        # its display block is written. Include those rows too; they are the
        # most important ones to surface as outcome_unknown on the follow-up.
        unblocked_executions = [
            (execution_id, tool_execution)
            for execution_id, tool_execution in tool_exec_by_id.items()
            if execution_id not in blocked_execution_ids
            and str(getattr(tool_execution, "completion_id", "") or "")
            == str(completion.id)
        ]
        # SQL row order is not a contract. A completion can contain more than
        # one blockless decision if a worker dies while blocks are being
        # persisted, so give those groups a stable order independent of the
        # database query plan.
        unblocked_executions.sort(
            key=lambda pair: (
                str(getattr(pair[1], "started_at", "") or ""),
                getattr(pair[1], "action_index", None) is None,
                int(getattr(pair[1], "action_index", 0) or 0),
                pair[0],
            )
        )
        for execution_id, tool_execution in unblocked_executions:
            decision_id = str(
                getattr(tool_execution, "plan_decision_id", "") or ""
            )
            group_id = decision_id or f"standalone:{execution_id}"
            agent_execution_id = str(
                getattr(tool_execution, "agent_execution_id", "") or ""
            )
            synthetic_block = SimpleNamespace(
                block_index=1_000_000
                + int(getattr(tool_execution, "action_index", 0) or 0),
            )
            groups.setdefault((agent_execution_id, group_id), []).append(
                (synthetic_block, tool_execution)
            )

        for pairs in groups.values():
            pairs.sort(
                key=lambda pair: (
                    getattr(pair[1], "action_index", None) is None,
                    getattr(pair[1], "action_index", 0) or 0,
                    getattr(pair[0], "block_index", 0) or 0,
                )
            )
            calls: list[ToolCallPart] = []
            results: list[ToolResultPart] = []
            for _block, tool_execution in pairs:
                execution_id = str(tool_execution.id)
                represented.add(execution_id)
                call_id = (
                    getattr(tool_execution, "provider_call_id", None)
                    or f"legacy_call_{execution_id}"
                )
                tool_name = getattr(tool_execution, "tool_name", None) or "unknown_tool"
                calls.append(
                    ToolCallPart(
                        id=str(call_id),
                        tool_name=str(tool_name),
                        args=getattr(tool_execution, "arguments_json", None) or {},
                        signature=getattr(tool_execution, "provider_signature", None),
                        provider_name=getattr(tool_execution, "provider_name", None),
                    )
                )
                outcome = _outcome(getattr(tool_execution, "status", None))
                content, digest = _result_content(tool_execution, outcome)
                results.append(
                    ToolResultPart(
                        call_id=str(call_id),
                        tool_name=str(tool_name),
                        outcome=outcome,
                        content=content,
                        digest=digest,
                        tokens=estimate_tokens(content),
                    )
                )
            if calls:
                # Assistant narration remains in MessagesSection beside the
                # user dialogue. Only native call/result parts move here, so
                # prompt text is not duplicated.
                transcript.add_assistant_step(calls=calls)
                transcript.add_tool_results(results)

    transcript.repair()
    return transcript, represented
