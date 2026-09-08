"""Denormalised diagnosis columns on ``agent_executions``.

One idempotent function, ``refresh_rollup``, recomputes every rollup column
for one run from its sources (completions, feedback, tool executions, usage
records). It is called from four places — run finish, feedback write, judge
score write, and a usage record written after the run finished — and the
backfill script is the same function over every run. There is no second write
path to keep consistent.

Nothing here runs on the hot path *during* a run.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.completion_feedback import CompletionFeedback
from app.models.llm_usage_record import LLMUsageRecord
from app.models.tool_execution import ToolExecution

logger = logging.getLogger(__name__)

TEXT_LIMIT = 2000
# Usage scopes that carry the planner's own model (vs. judges, coders, ...).
_PRIMARY_SCOPES = ("planner", "agent")
_PLATFORMS = {"slack", "teams", "email", "mcp", "api", "web"}


def _prompt_text(value) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("content") or value.get("text") or ""
    text = str(value).strip()
    return text[:TEXT_LIMIT] if text else None


def _error_text(error_json) -> Optional[str]:
    if not error_json:
        return None
    if isinstance(error_json, dict):
        msg = error_json.get("message") or error_json.get("error") or error_json.get("detail")
        text = str(msg) if msg else str(error_json)
    else:
        text = str(error_json)
    text = text.strip()
    return text[:TEXT_LIMIT] if text else None


def _platform(user_c: Optional[Completion], sys_c: Optional[Completion], config_json) -> str:
    for c in (user_c, sys_c):
        p = getattr(c, "external_platform", None) if c is not None else None
        if p:
            p = str(p).lower()
            return p if p in _PLATFORMS else "api"
    source = (config_json or {}).get("source") if isinstance(config_json, dict) else None
    if source:
        source = str(source).lower()
        return source if source in _PLATFORMS else "api"
    return "web"


def _row_tokens(r) -> int:
    total = int(r.prompt_tokens or 0) + int(r.completion_tokens or 0)
    # Anthropic reports cached tokens separately; OpenAI/Azure fold them into
    # prompt_tokens (mirrors ConsoleService._row_total_tokens_expr).
    if (r.provider_type or "") == "anthropic":
        total += int(r.cache_read_tokens or 0) + int(r.cache_creation_tokens or 0)
    return total


def _rollup_values(ae, head, user_c, sys_c, fb, tool_counts, usage_rows, cost_is_partial, turn_index, now) -> dict:
    """The rollup columns for one run, from already-loaded sources. Shared by
    the single-run refresh and the batched backfill so they cannot drift."""
    prompt_tokens = completion_tokens = total_tokens = 0
    total_cost: Optional[float] = None
    primary_model_id = primary_provider = None
    if usage_rows:
        total_cost = 0.0
        best = None
        best_key = (-1, -1)
        for r in usage_rows:
            prompt_tokens += int(r.prompt_tokens or 0)
            completion_tokens += int(r.completion_tokens or 0)
            total_tokens += _row_tokens(r)
            total_cost += float(r.total_cost_usd or 0)
            key = (1 if (r.scope or "") in _PRIMARY_SCOPES else 0, _row_tokens(r))
            if key > best_key:
                best_key, best = key, r
        if best is not None:
            primary_model_id = best.model_id
            primary_provider = best.provider_type
    else:
        # Pre-attribution runs: the planner's own token count is all we have.
        tu = ae.token_usage_json if isinstance(ae.token_usage_json, dict) else {}
        prompt_tokens = int(tu.get("prompt_tokens") or 0)
        completion_tokens = int(tu.get("completion_tokens") or 0)
        total_tokens = int(tu.get("total_tokens") or 0) or (prompt_tokens + completion_tokens)
        cost_is_partial = bool(total_tokens)
    feedback_direction, feedback_message = fb if fb else (0, None)
    tool_count, failed_tool_count = tool_counts
    return dict(
        prompt_text=_prompt_text(getattr(head, "prompt", None)) if head is not None else None,
        error_text=_error_text(ae.error_json),
        platform=_platform(user_c, sys_c, ae.config_json),
        feedback_direction=feedback_direction,
        feedback_message=feedback_message,
        judge_response_score=getattr(head, "response_score", None) if head is not None else None,
        judge_instructions_score=getattr(head, "instructions_effectiveness", None) if head is not None else None,
        judge_context_score=getattr(head, "context_effectiveness", None) if head is not None else None,
        primary_model_id=primary_model_id,
        primary_provider=primary_provider,
        total_cost_usd=total_cost,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        tool_count=tool_count,
        failed_tool_count=failed_tool_count,
        turn_index=turn_index,
        cost_is_partial=cost_is_partial,
        rollup_at=now,
    )


async def refresh_rollup(db: AsyncSession, agent_execution_id: str, *, commit: bool = True) -> Optional[AgentExecution]:
    """Recompute every rollup column for one run. Safe to call any number of times."""
    ae = await db.get(AgentExecution, str(agent_execution_id))
    if ae is None:
        return None

    sys_c = await db.get(Completion, ae.completion_id) if ae.completion_id else None
    user_c = None
    if sys_c is not None and sys_c.parent_id:
        user_c = await db.get(Completion, sys_c.parent_id)
    head = user_c or sys_c
    completion_ids = [c.id for c in (sys_c, user_c) if c is not None]

    # --- feedback (latest wins) ------------------------------------------
    feedback_direction = 0
    feedback_message = None
    if completion_ids:
        fb = (await db.execute(
            select(CompletionFeedback.direction, CompletionFeedback.message)
            .where(CompletionFeedback.completion_id.in_(completion_ids))
            .order_by(CompletionFeedback.created_at.desc())
            .limit(1)
        )).first()
        if fb is not None:
            feedback_direction = int(fb.direction or 0)
            feedback_message = (fb.message or None)

    # --- tool counts -------------------------------------------------------
    tool_row = (await db.execute(
        select(
            func.count(ToolExecution.id),
            func.coalesce(func.sum(case((ToolExecution.status == "error", 1), else_=0)), 0),
        ).where(ToolExecution.agent_execution_id == ae.id)
    )).one()
    tool_count = int(tool_row[0] or 0)
    failed_tool_count = int(tool_row[1] or 0)

    # --- usage: by run id first, then by report + time window --------------
    usage_rows = (await db.execute(
        select(LLMUsageRecord).where(LLMUsageRecord.agent_execution_id == ae.id)
    )).scalars().all()
    cost_is_partial = False
    if not usage_rows and ae.report_id and ae.started_at:
        window_end = ae.completed_at or datetime.utcnow()
        usage_rows = (await db.execute(
            select(LLMUsageRecord).where(
                LLMUsageRecord.agent_execution_id.is_(None),
                LLMUsageRecord.report_id == ae.report_id,
                LLMUsageRecord.created_at >= ae.started_at,
                LLMUsageRecord.created_at <= window_end,
            )
        )).scalars().all()
        cost_is_partial = bool(usage_rows)

    # --- turn index --------------------------------------------------------
    turn_index = None
    if ae.report_id and ae.created_at is not None:
        turn_index = int((await db.execute(
            select(func.count(AgentExecution.id)).where(
                AgentExecution.report_id == ae.report_id,
                AgentExecution.is_eval_run == False,  # noqa: E712
                or_(
                    AgentExecution.created_at < ae.created_at,
                    and_(AgentExecution.created_at == ae.created_at, AgentExecution.id <= ae.id),
                ),
            )
        )).scalar() or 1)

    values = _rollup_values(ae, head, user_c, sys_c, (feedback_direction, feedback_message) if completion_ids else None,
                            (tool_count, failed_tool_count), usage_rows, cost_is_partial, turn_index, datetime.utcnow())
    # Targeted UPDATE by id: never touches the hot-path columns and never
    # races a concurrent writer of status/timings on the same row.
    await db.execute(update(AgentExecution).where(AgentExecution.id == ae.id).values(**values))
    if commit:
        await db.commit()
    for k, v in values.items():
        setattr(ae, k, v)
    return ae


async def agent_execution_ids_for_completion(db: AsyncSession, completion_id: str) -> List[str]:
    """Runs anchored on a completion — either directly (the system completion)
    or through it (the user completion whose child system completion the run
    is on). Feedback and judge scores may land on either."""
    child_ids = select(Completion.id).where(Completion.parent_id == str(completion_id))
    rows = (await db.execute(
        select(AgentExecution.id).where(
            or_(
                AgentExecution.completion_id == str(completion_id),
                AgentExecution.completion_id.in_(child_ids),
            )
        )
    )).scalars().all()
    return [str(r) for r in rows]


async def refresh_for_completion(db: AsyncSession, completion_id: str) -> None:
    """Best-effort hook for feedback / judge writers. Never raises."""
    try:
        for ae_id in await agent_execution_ids_for_completion(db, completion_id):
            await refresh_rollup(db, ae_id)
    except Exception as exc:  # noqa: BLE001 — a rollup must never break the caller
        logger.warning("diagnosis rollup for completion %s failed: %s", completion_id, exc)
        try:
            await db.rollback()
        except Exception:  # pragma: no cover
            pass


async def refresh_after_run(db: AsyncSession, agent_execution_id: str) -> None:
    """Hook for the run-finish path. Never raises."""
    try:
        await refresh_rollup(db, agent_execution_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("diagnosis rollup for run %s failed: %s", agent_execution_id, exc)
        try:
            await db.rollback()
        except Exception:  # pragma: no cover
            pass


async def refresh_if_finished(db: AsyncSession, agent_execution_id: str) -> None:
    """Hook for usage records that land after the run finished (late judge
    calls). A run still in progress is rolled up at its own finish."""
    try:
        finished = (await db.execute(
            select(AgentExecution.completed_at).where(AgentExecution.id == str(agent_execution_id))
        )).scalar()
        if finished is not None:
            await refresh_rollup(db, agent_execution_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("diagnosis late rollup for run %s failed: %s", agent_execution_id, exc)
        try:
            await db.rollback()
        except Exception:  # pragma: no cover
            pass


async def refresh_rollups_bulk(db: AsyncSession, agent_execution_ids: List[str]) -> int:
    """``refresh_rollup`` for many runs with a handful of set-based reads
    instead of ~7 round trips per run. Same values, same UPDATE; used by the
    backfill. Returns the number of runs updated."""
    ids = [str(i) for i in agent_execution_ids]
    if not ids:
        return 0
    runs = (await db.execute(select(AgentExecution).where(AgentExecution.id.in_(ids)))).scalars().all()
    if not runs:
        return 0

    sys_ids = [r.completion_id for r in runs if r.completion_id]
    sys_by_id = {c.id: c for c in (await db.execute(select(Completion).where(Completion.id.in_(sys_ids)))).scalars().all()} if sys_ids else {}
    parent_ids = [c.parent_id for c in sys_by_id.values() if c.parent_id]
    user_by_id = {c.id: c for c in (await db.execute(select(Completion).where(Completion.id.in_(parent_ids)))).scalars().all()} if parent_ids else {}

    all_cids = list(sys_by_id.keys()) + list(user_by_id.keys())
    feedback: Dict[str, Tuple[int, Optional[str]]] = {}
    if all_cids:
        for cid, direction, message in (await db.execute(
            select(CompletionFeedback.completion_id, CompletionFeedback.direction, CompletionFeedback.message)
            .where(CompletionFeedback.completion_id.in_(all_cids))
            .order_by(CompletionFeedback.created_at.asc())
        )).all():
            feedback[str(cid)] = (int(direction or 0), message or None)  # latest wins

    tool_counts: Dict[str, Tuple[int, int]] = {}
    for ae_id, total, failed in (await db.execute(
        select(
            ToolExecution.agent_execution_id,
            func.count(ToolExecution.id),
            func.coalesce(func.sum(case((ToolExecution.status == "error", 1), else_=0)), 0),
        ).where(ToolExecution.agent_execution_id.in_(ids)).group_by(ToolExecution.agent_execution_id)
    )).all():
        tool_counts[str(ae_id)] = (int(total or 0), int(failed or 0))

    usage: Dict[str, list] = {}
    for r in (await db.execute(select(LLMUsageRecord).where(LLMUsageRecord.agent_execution_id.in_(ids)))).scalars().all():
        usage.setdefault(str(r.agent_execution_id), []).append(r)

    # Window attribution for runs with no attributed records: one query per report.
    windowed: Dict[str, list] = {}
    unattributed = [r for r in runs if str(r.id) not in usage and r.report_id and r.started_at]
    if unattributed:
        report_ids = sorted({r.report_id for r in unattributed})
        rows = (await db.execute(
            select(LLMUsageRecord).where(
                LLMUsageRecord.agent_execution_id.is_(None),
                LLMUsageRecord.report_id.in_(report_ids),
            )
        )).scalars().all()
        by_report: Dict[str, list] = {}
        for rec in rows:
            by_report.setdefault(str(rec.report_id), []).append(rec)
        for r in unattributed:
            end = r.completed_at or datetime.utcnow()
            windowed[str(r.id)] = [rec for rec in by_report.get(str(r.report_id), []) if rec.created_at and r.started_at <= rec.created_at <= end]

    # Turn index: rank runs within each report (non-eval), for every report on the page.
    report_ids = sorted({r.report_id for r in runs if r.report_id})
    turn: Dict[str, int] = {}
    if report_ids:
        ordered = (await db.execute(
            select(AgentExecution.id, AgentExecution.report_id)
            .where(AgentExecution.report_id.in_(report_ids), AgentExecution.is_eval_run == False)  # noqa: E712
            .order_by(AgentExecution.report_id, AgentExecution.created_at.asc(), AgentExecution.id.asc())
        )).all()
        counter: Dict[str, int] = {}
        for ae_id, rid in ordered:
            counter[rid] = counter.get(rid, 0) + 1
            turn[str(ae_id)] = counter[rid]

    now = datetime.utcnow()
    updated = 0
    for ae in runs:
        sys_c = sys_by_id.get(ae.completion_id)
        user_c = user_by_id.get(sys_c.parent_id) if sys_c is not None and sys_c.parent_id else None
        head = user_c or sys_c
        fb = None
        for c in (user_c, sys_c):
            if c is not None and str(c.id) in feedback:
                fb = feedback[str(c.id)]
        rows = usage.get(str(ae.id))
        partial = False
        if not rows:
            rows = windowed.get(str(ae.id)) or []
            partial = bool(rows)
        values = _rollup_values(ae, head, user_c, sys_c, fb, tool_counts.get(str(ae.id), (0, 0)), rows, partial,
                                turn.get(str(ae.id)) if ae.is_eval_run is False or ae.is_eval_run == 0 else None, now)
        await db.execute(update(AgentExecution).where(AgentExecution.id == ae.id).values(**values))
        updated += 1
    return updated


async def backfill(
    db: AsyncSession,
    *,
    only_missing: bool = True,
    batch_size: int = 500,
    organization_id: Optional[str] = None,
    progress=None,
) -> int:
    """Roll up every run (or every run without a rollup) in set-based batches.
    Resumable: a run is stamped ``rollup_at`` as it is done, so a re-run with
    ``only_missing`` picks up where it stopped."""
    q = select(AgentExecution.id).order_by(AgentExecution.created_at.asc(), AgentExecution.id.asc())
    if only_missing:
        q = q.where(AgentExecution.rollup_at.is_(None))
    if organization_id:
        q = q.where(AgentExecution.organization_id == organization_id)
    ids = [str(r) for r in (await db.execute(q)).scalars().all()]
    done = 0
    for i in range(0, len(ids), batch_size):
        done += await refresh_rollups_bulk(db, ids[i:i + batch_size])
        await db.commit()
        if progress:
            progress(done, len(ids))
    return done
