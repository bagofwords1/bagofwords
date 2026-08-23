"""Seed a run's transcript with tool results from earlier completions.

Tool results live in memory for exactly one completion: ``ObservationContextBuilder``
is constructed per run, so the moment a new user turn starts, everything the
previous run learned is gone from the model's view and only a one-line digest
survives in the rendered conversation. A run that was interrupted loses it the
same way -- not because the kill discarded anything, but because the boundary
does.

This module replays the recent past as real tool_use/tool_result turns, from the
bounded ``context_summary_json`` column each tool now writes when it finishes.
The transcript's existing decay ladder then governs cost: hydrated results are
oldest, so ``fit_to_budget`` demotes them first, and nothing here changes the
token ceiling.

Reads one narrow column set -- never ``result_json``, whose row-heavy payloads
take seconds to parse -- so hydration stays off the latency path.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import lazyload

from app.ai.context.parts import (
    Outcome,
    TextPart,
    ToolCallPart,
    ToolResultPart,
    Turn,
    estimate_tokens,
)
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

# Completions to look back over. Bounded independently of the token budget so a
# quiet report cannot drag in arbitrarily old work.
HYDRATION_COMPLETIONS = 5
# Ceiling on what hydration may add. Whichever limit binds first wins, so a
# tool-heavy history cannot blow up the prompt before the ladder even runs.
HYDRATION_TOKEN_BUDGET = 20_000

_SUCCESS_STATUSES = frozenset({"success", "completed"})


def enabled() -> bool:
    """On by default; ``BOW_TRANSCRIPT_HYDRATION=0`` restores the pre-hydration
    behaviour (prior turns visible only as conversation digests). The switch
    exists because this changes what every multi-turn request carries."""
    flag = os.environ.get("BOW_TRANSCRIPT_HYDRATION")
    if flag is None or flag == "":
        return True
    return flag.strip().lower() in ("1", "true", "yes", "on")


def _content_for(tool_name: str, summary_json: Any, result_summary: Optional[str]) -> str:
    """The model-visible body for one historical result.

    Prefers the persisted observation (what the model actually saw), then the
    row-heavy projection, then the one-line summary. Never falls back to
    result_json: that payload is the tool's *output*, a different shape, and
    re-deriving from it is exactly the drift this replaces.
    """
    if isinstance(summary_json, dict):
        observation = summary_json.get("observation")
        if isinstance(observation, dict) and observation:
            return json.dumps(observation, default=str)
        projection = {k: v for k, v in summary_json.items() if k != "version"}
        if projection:
            return json.dumps(projection, default=str)
    return result_summary or ""


async def hydrate_transcript(
    db,
    *,
    report_id: str,
    exclude_completion_ids: set[str],
    completions: int = HYDRATION_COMPLETIONS,
    token_budget: int = HYDRATION_TOKEN_BUDGET,
) -> dict:
    """Build replayable turns from earlier completions' tool results.

    Returns ``turns`` (chronological, to be spliced between the static context
    and the ask) and ``completion_ids`` -- the completions the transcript now
    owns. The caller must stop the conversation renderer from digesting those
    same executions, or every result appears twice in two different shapes.
    """
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    from app.models.tool_execution import ToolExecution

    stats = {"completion_ids": set(), "turns": [], "results": 0, "tokens": 0}
    if not enabled():
        return stats

    try:
        rows = (await db.execute(
            select(Completion.id, Completion.created_at)
            .options(lazyload("*"))
            .filter(Completion.report_id == report_id)
            .filter(Completion.role == "system")
            .filter(Completion.status != "queued")
            .filter(Completion.deleted_at.is_(None))
            .order_by(Completion.created_at.desc())
            .limit(completions + len(exclude_completion_ids))
        )).all()
    except Exception:
        logger.warning("[hydration] completion lookup failed", exc_info=True)
        return stats

    ordered = [
        (str(cid), created_at) for cid, created_at in rows
        if str(cid) not in exclude_completion_ids
    ][:completions]
    if not ordered:
        return stats

    try:
        block_rows = (await db.execute(
            select(
                CompletionBlock.completion_id,
                CompletionBlock.block_index,
                ToolExecution.id,
                ToolExecution.tool_name,
                ToolExecution.arguments_json,
                ToolExecution.status,
                ToolExecution.result_summary,
                ToolExecution.error_message,
                ToolExecution.context_summary_json,
            )
            .join(ToolExecution, ToolExecution.id == CompletionBlock.tool_execution_id)
            .filter(CompletionBlock.completion_id.in_([cid for cid, _ in ordered]))
            .order_by(CompletionBlock.block_index.asc())
        )).all()
    except Exception:
        logger.warning("[hydration] tool execution lookup failed", exc_info=True)
        return stats

    by_completion: dict[str, list] = {}
    for row in block_rows:
        by_completion.setdefault(str(row[0]), []).append(row)

    # Walk newest-first so the budget keeps the most recent history, then flip
    # back to chronological before splicing in.
    hydrated: list[Turn] = []
    spent = 0
    covered: set[str] = set()

    for cid, created_at in ordered:  # ordered is newest-first
        rows_for = by_completion.get(cid) or []
        if not rows_for:
            continue
        calls, results = [], []
        batch_tokens = 0
        for row in rows_for:
            (_, _, te_id, tool_name, arguments_json, status,
             result_summary, error_message, summary_json) = row
            call_id = f"te_{te_id}"
            content = _content_for(tool_name, summary_json, result_summary)
            if not content and not error_message:
                continue
            if status in _SUCCESS_STATUSES:
                outcome = Outcome.SUCCESS
            elif status == "error":
                outcome = Outcome.FAILED
            else:
                # Never ran to completion -- say so rather than let a gap read
                # as a silent success.
                outcome = Outcome.INTERRUPTED
                content = content or (
                    error_message or "The call was interrupted before it produced a result."
                )
            args = arguments_json if isinstance(arguments_json, dict) else {}
            calls.append(ToolCallPart(id=call_id, tool_name=tool_name, args=args))
            part = ToolResultPart(
                call_id=call_id,
                tool_name=tool_name,
                outcome=outcome,
                content=content,
                digest=result_summary or None,
                tokens=estimate_tokens(content),
            )
            results.append(part)
            batch_tokens += part.tokens

        if not calls:
            continue
        if spent + batch_tokens > token_budget and hydrated:
            logger.info(
                "[hydration] stopped at budget: %d/%d tokens, %d completions covered",
                spent, token_budget, len(covered),
            )
            break

        label = "[earlier turn"
        try:
            label += f" — {created_at.strftime('%H:%M')}"
        except Exception:
            pass
        label += "]"

        # Prepend (newest processed first, so each older batch goes in front).
        hydrated[0:0] = [
            Turn(role="user", parts=[TextPart(text=label)]),
            Turn(role="assistant", parts=list(calls)),
            Turn(role="user", parts=list(results)),
        ]
        spent += batch_tokens
        covered.add(cid)
        stats["results"] += len(results)

    if not hydrated:
        return stats

    stats.update({
        "completion_ids": covered,
        "turns": hydrated,
        "tokens": spent,
    })
    logger.info(
        "[hydration] seeded %d turns / %d results / ~%d tokens from %d completion(s)",
        len(hydrated), stats["results"], spent, len(covered),
    )
    return stats
