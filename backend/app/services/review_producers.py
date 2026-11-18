"""Producers for the Review feed.

Each function detects a condition and emits (deduped) review items on a single
occurrence, fired from the event that caused it. All attribution to an agent
goes through
``report_data_source_association`` (a report can belong to several agents → fan
out one item per agent, so training/eval fired from the item stays agent-scoped).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review_item import (
    TYPE_SLOW_QUERY, TYPE_LOW_CONFIDENCE, TYPE_INSTRUCTION_SUGGESTION, TYPE_SCHEMA_CHANGED,
    SEVERITY_WARNING, SEVERITY_INFO,
)
from app.services.review_service import review_service

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 24 * 7
SLOW_QUERY_MS = 90_000                        # > 90s is "slow"
QUERY_TOOLS = ("create_data", "read_query", "run_query")  # tools that run a data query
LOW_CONFIDENCE_MIN_OCCURRENCES = 5            # only notify after the 5th low score in the window


async def _org_and_data_sources_for_report(db, report_id) -> Tuple[Optional[str], List[str]]:
    """Resolve a report to its org + the data sources (agents) it's attached to.
    A report can belong to several agents → callers fan one item out per agent."""
    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association as assoc
    report = await db.get(Report, str(report_id))
    if report is None:
        return None, []
    ds_rows = (await db.execute(
        select(assoc.c.data_source_id).where(assoc.c.report_id == str(report_id))
    )).all()
    return str(report.organization_id), [str(d[0]) for d in ds_rows]


# ── slow query (event emitter, fired when a query tool finishes) ─────────────
async def emit_slow_query_for_tool_execution(db, te) -> int:
    """Bump a per-agent slow-query item when a data-query tool ran over the
    latency budget. Fired once per slow execution from ``ToolExecutionService``;
    ``group_count`` accumulates occurrences until the item is resolved/dismissed.

    The cheap guards (tool name + duration, both already on ``te``) short-circuit
    before any DB work, so the common fast-query path costs nothing."""
    if te is None or te.tool_name not in QUERY_TOOLS:
        return 0
    if te.duration_ms is None or te.duration_ms <= SLOW_QUERY_MS:
        return 0
    from app.models.agent_execution import AgentExecution
    ae = await db.get(AgentExecution, str(te.agent_execution_id))
    if ae is None or not ae.report_id or getattr(ae, "is_eval_run", False):
        return 0  # eval/test runs are synthetic — don't surface them in the feed
    org_id, ds_ids = await _org_and_data_sources_for_report(db, ae.report_id)
    org_id = (str(ae.organization_id) if ae.organization_id else None) or org_id
    if not org_id or not ds_ids:
        return 0
    secs = SLOW_QUERY_MS // 1000
    actual = int(te.duration_ms // 1000)
    n = 0
    for ds_id in ds_ids:
        await review_service.emit(
            db, organization_id=org_id, type=TYPE_SLOW_QUERY,
            data_source_id=ds_id, severity=SEVERITY_WARNING,
            title=f"Slow data queries (>{secs}s)",
            why=f"A data query on this agent ran {actual}s, over the {secs}s budget. Consider a guardrail instruction or an index.",
            group_key=f"slow_query:{ds_id}",
            # `i18n` carries the interpolation params so the frontend can
            # re-render title/body from the catalogue in the viewer's locale
            # (the stored English text stays the fallback).
            subject={"kind": "query_group", "threshold_ms": SLOW_QUERY_MS,
                     "i18n": {"secs": secs, "actual": actual}},
            source_run_id=str(te.agent_execution_id),
            respect_dismissal=True, resurface_after_hours=DEFAULT_WINDOW_HOURS,
        )
        n += 1
    return n


# ── low confidence (event emitter, fired when the judge scores a completion) ──
async def _recent_low_confidence_count(db, data_source_id: str) -> int:
    """Count distinct completions scored below 3/5 on this agent within the
    rolling window. Drives the 'notify only after N in a week' floor — the
    completions table is the ledger, so no per-event state is needed."""
    from app.models.completion import Completion
    from app.models.report_data_source_association import report_data_source_association as assoc
    window_start = datetime.utcnow() - timedelta(hours=DEFAULT_WINDOW_HOURS)
    return (await db.execute(
        select(func.count(func.distinct(Completion.id)))
        .join(assoc, assoc.c.report_id == Completion.report_id)
        .where(and_(
            assoc.c.data_source_id == str(data_source_id),
            Completion.response_score.isnot(None),
            Completion.response_score < 3,
            Completion.created_at >= window_start,
        ))
    )).scalar() or 0


async def emit_low_confidence_for_completion(db, completion) -> int:
    """Surface a per-agent low-confidence item once an agent accumulates
    ``LOW_CONFIDENCE_MIN_OCCURRENCES`` answers scored below 3/5 within the
    rolling week window. Fired once per low score from the judge-score persist
    path; below the floor the low score is tracked silently (it lives in the
    completions table) and no item/notification is emitted. ``group_count``
    reflects the windowed occurrence count."""
    score = getattr(completion, "response_score", None)
    if score is None or score >= 3:
        return 0
    # Skip eval/test runs — synthetic, shouldn't surface in the feed.
    from app.models.agent_execution import AgentExecution
    ae = (await db.execute(
        select(AgentExecution).where(AgentExecution.completion_id == str(completion.id)).limit(1)
    )).scalar_one_or_none()
    if ae is not None and getattr(ae, "is_eval_run", False):
        return 0
    org_id, ds_ids = await _org_and_data_sources_for_report(db, completion.report_id)
    if not org_id or not ds_ids:
        return 0
    n = 0
    for ds_id in ds_ids:
        recent = await _recent_low_confidence_count(db, ds_id)
        if recent < LOW_CONFIDENCE_MIN_OCCURRENCES:
            continue  # below the weekly floor — track silently, don't notify yet
        await review_service.emit(
            db, organization_id=org_id, type=TYPE_LOW_CONFIDENCE,
            data_source_id=ds_id, severity=SEVERITY_WARNING,
            title="Low-confidence answers",
            why=f"{recent} answers on this agent were scored below 3/5 in the past week. Improve it in training mode to close the gaps.",
            group_key=f"low_confidence:{ds_id}",
            subject={"kind": "low_confidence", "i18n": {"n": recent}},
            occurrences=recent,
            respect_dismissal=True, resurface_after_hours=DEFAULT_WINDOW_HOURS,
        )
        n += 1
    return n


# ── instruction suggestions (non-admin user edits + AI/harness) ─────────────
async def _changed_instructions_for_build(db, organization_id, build, superseded_pairs=None) -> List[Tuple[str, List[str]]]:
    """For a pending build, the instructions it actually changed vs main, each
    with the agent ids it's attached to (empty => global).

    ``superseded_pairs`` (set of (instruction_id, version_id)) lets the caller
    drop intermediate snapshots of a chained edit (v15->v16->v17): a build whose
    version is another pending build's base is covered by the leaf, so it's
    excluded to avoid duplicated/overlapping diff hunks."""
    from app.models.instruction_build import InstructionBuild
    from app.models.build_content import BuildContent
    from app.models.instruction import instruction_data_source_association as ids_assoc
    # main version per instruction
    main_rows = (await db.execute(
        select(BuildContent.instruction_id, BuildContent.instruction_version_id)
        .join(InstructionBuild, InstructionBuild.id == BuildContent.build_id)
        .where(and_(
            InstructionBuild.organization_id == organization_id,
            InstructionBuild.is_main.is_(True),
            InstructionBuild.deleted_at.is_(None),
        ))
    )).all()
    main_v = {str(i): str(v) for i, v in main_rows}
    # base version per instruction (what this build forked from)
    base_v = {}
    if getattr(build, "base_build_id", None):
        base_rows = (await db.execute(
            select(BuildContent.instruction_id, BuildContent.instruction_version_id)
            .where(BuildContent.build_id == str(build.base_build_id))
        )).all()
        base_v = {str(i): str(v) for i, v in base_rows}
    # Only the rows this build actually changes. A build snapshots every
    # instruction in the org, so without this filter the loop below walked the
    # whole workspace (and materialized main's and the base's full snapshots
    # above) to find the handful of genuinely changed rows. `is_change` records
    # exactly that at write time — see BuildContent.is_change. The per-row
    # `changed` test is kept as-is so the result set is unchanged for any legacy
    # row whose flag predates the column's backfill.
    contents = (await db.execute(
        select(BuildContent.instruction_id, BuildContent.instruction_version_id)
        .where(and_(
            BuildContent.build_id == str(build.id),
            BuildContent.is_change.is_(True),
        ))
    )).all()
    out: List[Tuple[str, List[str]]] = []
    for i, v in contents:
        i, v = str(i), str(v)
        base = base_v.get(i)
        changed = (base != v) if base is not None else (main_v.get(i) != v)
        if not changed:
            continue
        # A stale sibling (base behind current) is still a real pending change:
        # the per-instruction review rebases its intended change onto current,
        # so we keep surfacing it instead of excluding on freshness.
        # Intermediate snapshot of a chained edit — the leaf build covers it.
        if superseded_pairs and (i, v) in superseded_pairs:
            continue
        ds_rows = (await db.execute(
            select(ids_assoc.c.data_source_id).where(ids_assoc.c.instruction_id == i)
        )).all()
        out.append((i, [str(d[0]) for d in ds_rows]))
    return out


async def emit_instruction_suggestions_for_build(db, organization_id: str, build, *,
                                                  why: Optional[str] = None,
                                                  occurrences_map: Optional[dict] = None,
                                                  superseded_pairs=None) -> int:
    """Emit a suggestion item per (changed instruction × attached agent). Global
    instructions (no agent) emit a single global item (admin-only).

    ``occurrences_map`` (instruction_id -> count of distinct pending builds
    touching it) sets the item's change count; it is *set*, never incremented,
    so re-scans on every feed load don't inflate it."""
    from app.models.instruction import Instruction
    from app.models.review_item import ReviewItem, STATUS_DISMISSED
    source = getattr(build, "source", "user")
    label = "AI" if source == "ai" else "Proposed"
    changed = await _changed_instructions_for_build(db, organization_id, build, superseded_pairs)
    n = 0
    for instruction_id, ds_ids in changed:
        # Respect a prior dismissal of THIS build's suggestion for this
        # instruction — don't resurrect it on the next scan.
        gk = f"instr:{instruction_id}"
        dismissed = (await db.execute(
            select(ReviewItem.id).where(and_(
                ReviewItem.organization_id == organization_id,
                ReviewItem.type == TYPE_INSTRUCTION_SUGGESTION,
                ReviewItem.group_key == gk,
                ReviewItem.build_id == str(build.id),
                ReviewItem.status == STATUS_DISMISSED,
                ReviewItem.deleted_at.is_(None),
            )).limit(1)
        )).scalar_one_or_none()
        if dismissed is not None:
            continue
        instr = await db.get(Instruction, instruction_id)
        title = getattr(instr, "title", None) or "instruction"
        subject = {"kind": "instruction", "instruction_id": instruction_id, "build_id": str(build.id), "source": source}
        if why is None:
            # Canonical body only — a caller-supplied `why` is custom copy and
            # must survive untranslated (variantless lookup finds no plain
            # `body` key for this type, so the frontend falls back to it).
            subject["i18n"] = {"variant": "ai" if source == "ai" else "proposed"}
        count = (occurrences_map or {}).get(instruction_id, 1)
        targets = ds_ids or [None]   # None => global
        for ds_id in targets:
            await review_service.emit(
                db, organization_id=organization_id, type=TYPE_INSTRUCTION_SUGGESTION,
                data_source_id=ds_id, severity=SEVERITY_INFO,
                title=title,
                why=why or (f"{label} instruction change awaiting review."),
                group_key=f"instr:{instruction_id}",
                build_id=str(build.id), subject=subject, occurrences=count,
            )
            n += 1
    return n


# ── schema changed (event emitter, called from the table-change trigger) ─────
SCHEMA_HISTORY_MAX = 10   # earlier changes kept on the active item, newest first


async def emit_schema_changed(db, organization_id: str, data_source_id: str, *,
                              summary: Optional[str] = None, agent_name: Optional[str] = None,
                              connection_name: Optional[str] = None,
                              connection_id: Optional[str] = None,
                              diff: Optional[dict] = None) -> None:
    """Emit (or refresh) the agent's schema-change item.

    ``diff`` is the structured change from ``schema_diff.compute_schema_diff``.
    While the item is still open, a new change replaces the current diff and
    pushes the previous one onto ``history``, so an admin who looks later sees
    every change, not only the last.
    """
    from app.models.review_item import ReviewItem, ACTIVE_STATUSES

    group_key = f"schema_changed:{data_source_id}"
    history: list = []
    existing = (await db.execute(
        select(ReviewItem).where(and_(
            ReviewItem.organization_id == organization_id,
            ReviewItem.data_source_id == str(data_source_id),
            ReviewItem.type == TYPE_SCHEMA_CHANGED,
            ReviewItem.group_key == group_key,
            ReviewItem.status.in_(list(ACTIVE_STATUSES)),
            ReviewItem.deleted_at.is_(None),
        )).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        prev = existing.subject_json or {}
        prev_diff = prev.get("diff") or {}
        seen = existing.last_seen_at or existing.created_at
        history.append({
            "at": seen.isoformat() if seen else None,
            "connection": (prev.get("connection") or {}).get("name"),
            "total": prev_diff.get("total"),
            "counts": prev_diff.get("counts"),
            "summary": existing.why,
        })
        history.extend(prev.get("history") or [])
        history = history[:SCHEMA_HISTORY_MAX]

    if agent_name and connection_name:
        title, title_variant = f"{agent_name}: schema changed on {connection_name}", "named"
    elif agent_name:
        title, title_variant = f"{agent_name}: schema changed", "agent"
    else:
        title, title_variant = "Connection schema changed", None

    i18n: dict = {}
    if title_variant:
        i18n.update({"title_variant": title_variant, "agent": agent_name, "connection": connection_name})
    if diff:
        counts = diff.get("counts") or {}
        i18n.update({"variant": "counts", "added": counts.get("added", 0),
                     "removed": counts.get("removed", 0), "modified": counts.get("modified", 0),
                     "total": diff.get("total", 0)})
    elif summary:
        # A custom summary is dynamic copy — the "custom" variant has no
        # catalogue entry, so the frontend keeps the summary text as emitted
        # while still localizing the title.
        i18n["variant"] = "custom"

    subject = {
        "kind": "schema", "summary": summary,
        "connection": {"id": connection_id, "name": connection_name} if (connection_id or connection_name) else None,
        "diff": diff,
        "history": history,
        # The same sync started the agent-reliability loop (evals → training);
        # the inbox points the admin at the agent's evals view for its outcome.
        "reliability_check": {"started_at": datetime.utcnow().isoformat()},
        "i18n": i18n,
    }
    await review_service.emit(
        db, organization_id=organization_id, type=TYPE_SCHEMA_CHANGED,
        data_source_id=str(data_source_id), severity=SEVERITY_WARNING,
        title=title,
        why=summary or "Tables or columns changed on this connection. Review it in training mode to keep instructions accurate.",
        group_key=group_key,
        subject=subject,
        # Every schema change is a new event, not a recurrence of the old one:
        # marking the previous change as handled must not silence the next
        # (slow queries / low confidence keep their dismissal window).
        respect_dismissal=False,
    )
