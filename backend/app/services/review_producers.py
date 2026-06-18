"""Producers for the Review feed.

Each function detects a condition and emits (deduped) review items. Scans
recompute absolute counts over a window; event emitters fire on a single
occurrence. All attribution to an agent goes through
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

SLOW_QUERY_MS = 90_000               # > 90s is "slow"
QUERY_TOOLS = ("create_data", "read_query")  # tools that run a data query
DEFAULT_WINDOW_HOURS = 24 * 7


# ── slow queries ────────────────────────────────────────────────────────────
async def scan_slow_queries(db: AsyncSession, organization_id: str, *,
                            window_hours: int = DEFAULT_WINDOW_HOURS,
                            threshold_ms: int = SLOW_QUERY_MS) -> int:
    from app.models.tool_execution import ToolExecution
    from app.models.agent_execution import AgentExecution
    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association as assoc
    since = datetime.utcnow() - timedelta(hours=window_hours)
    rows = (await db.execute(
        select(assoc.c.data_source_id, func.count(ToolExecution.id))
        .select_from(ToolExecution)
        .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
        .join(Report, Report.id == AgentExecution.report_id)
        .join(assoc, assoc.c.report_id == Report.id)
        .where(and_(
            Report.organization_id == organization_id,
            ToolExecution.duration_ms.isnot(None),
            ToolExecution.duration_ms > threshold_ms,
            ToolExecution.tool_name.in_(QUERY_TOOLS),
            ToolExecution.created_at >= since,
        ))
        .group_by(assoc.c.data_source_id)
    )).all()
    n = 0
    secs = threshold_ms // 1000
    days = max(1, window_hours // 24)
    for ds_id, count in rows:
        count = int(count)
        await review_service.emit(
            db, organization_id=organization_id, type=TYPE_SLOW_QUERY,
            data_source_id=str(ds_id), severity=SEVERITY_WARNING,
            title=f"{count} slow quer{'y' if count == 1 else 'ies'} (>{secs}s)",
            why=f"{count} data quer{'y' if count == 1 else 'ies'} on this agent ran over {secs}s in the last {days}d. Consider a guardrail instruction or an index.",
            group_key=f"slow_query:{ds_id}", occurrences=count,
            subject={"kind": "query_group", "threshold_ms": threshold_ms, "window_hours": window_hours},
        )
        n += 1
    return n


# ── low confidence (response_score < 3, reusing the console definition) ──────
async def scan_low_confidence(db: AsyncSession, organization_id: str, *,
                              window_hours: int = DEFAULT_WINDOW_HOURS) -> int:
    from app.models.completion import Completion
    from app.models.report import Report
    from app.models.report_data_source_association import report_data_source_association as assoc
    since = datetime.utcnow() - timedelta(hours=window_hours)
    rows = (await db.execute(
        select(assoc.c.data_source_id, func.count(Completion.id))
        .select_from(Completion)
        .join(Report, Report.id == Completion.report_id)
        .join(assoc, assoc.c.report_id == Report.id)
        .where(and_(
            Report.organization_id == organization_id,
            Completion.response_score.isnot(None),
            Completion.response_score < 3,
            Completion.created_at >= since,
        ))
        .group_by(assoc.c.data_source_id)
    )).all()
    n = 0
    days = max(1, window_hours // 24)
    for ds_id, count in rows:
        count = int(count)
        await review_service.emit(
            db, organization_id=organization_id, type=TYPE_LOW_CONFIDENCE,
            data_source_id=str(ds_id), severity=SEVERITY_WARNING,
            title=f"{count} low-confidence answer{'' if count == 1 else 's'}",
            why=f"{count} answer{' was' if count == 1 else 's were'} scored below 3/5 on this agent in the last {days}d. Run training to close the gaps.",
            group_key=f"low_confidence:{ds_id}", occurrences=count,
            subject={"kind": "low_confidence", "window_hours": window_hours},
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
    contents = (await db.execute(
        select(BuildContent.instruction_id, BuildContent.instruction_version_id)
        .where(BuildContent.build_id == str(build.id))
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


async def scan_instruction_suggestions(db: AsyncSession, organization_id: str) -> int:
    """Backfill/scan: emit suggestion items for all current non-admin/AI pending
    builds. (A pending 'user' build implies a non-admin author — admins
    auto-promote their own edits.)

    Two passes so each instruction's item carries a *real* change count =
    number of distinct pending builds that changed it."""
    from collections import Counter
    from app.models.instruction_build import InstructionBuild
    from app.models.build_content import BuildContent
    builds = (await db.execute(
        select(InstructionBuild).where(and_(
            InstructionBuild.organization_id == organization_id,
            InstructionBuild.is_main.is_(False),
            InstructionBuild.deleted_at.is_(None),
            InstructionBuild.status.in_(["draft", "pending_approval"]),
            InstructionBuild.source.in_(["user", "ai"]),
        ))
    )).scalars().all()
    # Supersede chained edits: build (instruction_id, version_id) pairs that are
    # the base of some pending build. A pending build whose own version is one
    # of these is an intermediate snapshot (v16 in v15->v16->v17) covered by the
    # leaf, so we exclude it from counts and emission.
    from collections import defaultdict
    from app.models.instruction_version import InstructionVersion
    from app.services.suggestion_merge import superseded_by_containment
    base_ids = [str(b.base_build_id) for b in builds if getattr(b, "base_build_id", None)]
    superseded_pairs = set()
    base_text_lookup: dict = {}   # (base_build_id, instruction_id) -> base text
    if base_ids:
        base_rows = (await db.execute(
            select(BuildContent.build_id, BuildContent.instruction_id,
                   BuildContent.instruction_version_id, InstructionVersion.text)
            .join(InstructionVersion, InstructionVersion.id == BuildContent.instruction_version_id)
            .where(BuildContent.build_id.in_(base_ids))
        )).all()
        for b_id, i_id, v_id, t in base_rows:
            superseded_pairs.add((str(i_id), str(v_id)))
            base_text_lookup[(str(b_id), str(i_id))] = t or ""
    # Per-(build, instruction) version + text, so we can compare only REAL
    # changes for content supersede below (never inherited/base text — that would
    # wrongly drop a deletion-only suggestion).
    base_build_of = {str(b.id): (str(b.base_build_id) if getattr(b, "base_build_id", None) else None) for b in builds}
    pend_build_ids = [str(b.id) for b in builds]
    content_lookup: dict = {}   # (build_id, instruction_id) -> (version_id, text)
    if pend_build_ids:
        ct_rows = (await db.execute(
            select(BuildContent.build_id, BuildContent.instruction_id,
                   BuildContent.instruction_version_id, InstructionVersion.text)
            .join(InstructionVersion, InstructionVersion.id == BuildContent.instruction_version_id)
            .where(BuildContent.build_id.in_(pend_build_ids))
        )).all()
        for b_id, i_id, v_id, t in ct_rows:
            content_lookup[(str(b_id), str(i_id))] = (str(v_id), t or "")

    # Pass 1a: which instructions each build really changed (structural supersede
    # only) — the basis for the content-supersede comparison.
    changed_by_build = {}
    for b in builds:
        changed_by_build[str(b.id)] = await _changed_instructions_for_build(
            db, organization_id, b, superseded_pairs
        )

    # Content-level supersede: sibling builds that don't chain structurally but
    # whose text is a cumulative superset of one another (e.g. "+lorem" vs
    # "+lorem +hello" from two separate chat turns). Compare only real-changed
    # versions per instruction; keep the maximal (leaf) one, mark the rest
    # superseded so they're neither counted nor emitted twice.
    by_instr: dict = defaultdict(dict)   # instr -> {version_id: (text, base_text)}
    for bid, changed in changed_by_build.items():
        base_bid = base_build_of.get(bid)
        for instr, _ds in changed:
            vt = content_lookup.get((bid, instr))
            if vt:
                base_text = base_text_lookup.get((base_bid, instr), "") if base_bid else ""
                by_instr[instr][vt[0]] = (vt[1], base_text)
    for instr, vmap in by_instr.items():
        for vid in superseded_by_containment(vmap):
            superseded_pairs.add((instr, vid))

    # Pass 1b: recount with content supersede applied (a build superseded for an
    # instruction drops out of both the count and emission).
    counts: Counter = Counter()
    for b in builds:
        kept = [(instr, ds) for (instr, ds) in changed_by_build[str(b.id)]
                if (instr, content_lookup.get((str(b.id), instr), (None,))[0]) not in superseded_pairs]
        changed_by_build[str(b.id)] = kept
        for instruction_id, _ds in kept:
            counts[instruction_id] += 1
    # Pass 2: emit with the real count.
    n = 0
    for b in builds:
        n += await emit_instruction_suggestions_for_build(db, organization_id, b, occurrences_map=counts, superseded_pairs=superseded_pairs)
    # Auto-resolve any active suggestion items whose instruction no longer has a
    # fresh pending change (accepted, rejected, or superseded by a newer state).
    from datetime import datetime
    from app.models.review_item import ReviewItem, ACTIVE_STATUSES, STATUS_RESOLVED
    active_ids = set(counts.keys())
    open_items = (await db.execute(
        select(ReviewItem).where(and_(
            ReviewItem.organization_id == organization_id,
            ReviewItem.type == TYPE_INSTRUCTION_SUGGESTION,
            ReviewItem.status.in_(list(ACTIVE_STATUSES)),
            ReviewItem.deleted_at.is_(None),
        ))
    )).scalars().all()
    stale = 0
    for it in open_items:
        gk = it.group_key or ""
        iid = gk[len("instr:"):] if gk.startswith("instr:") else None
        if iid and iid not in active_ids:
            it.status = STATUS_RESOLVED
            it.verified_at = datetime.utcnow()
            stale += 1
    if stale:
        await db.commit()
    return n


# ── schema changed (event emitter, called from the table-change trigger) ─────
async def emit_schema_changed(db, organization_id: str, data_source_id: str, *,
                              summary: Optional[str] = None, agent_name: Optional[str] = None) -> None:
    await review_service.emit(
        db, organization_id=organization_id, type=TYPE_SCHEMA_CHANGED,
        data_source_id=str(data_source_id), severity=SEVERITY_WARNING,
        title="Connection schema changed",
        why=summary or "Tables or columns changed on this connection. Re-run evals or training to keep instructions accurate.",
        group_key=f"schema_changed:{data_source_id}",
        subject={"kind": "schema", "summary": summary},
    )


def build_training_brief(item) -> str:
    """A per-event 'focus brief' seeded into the training run, built from the
    Review item's type + context. Threaded to the reliability loop's training
    seam (and ready for the knowledge-harness implementation)."""
    t = item.type
    n = item.group_count or 1
    why = (item.why or "").strip()
    if t == TYPE_LOW_CONFIDENCE:
        return (f"Focus: review the {n} low-confidence agent run(s) on this agent and propose "
                f"instructions that fix the recurring gaps that caused the low scores. {why}")
    if t == TYPE_SLOW_QUERY:
        return (f"Focus: {n} data query/queries on this agent ran over the latency budget. "
                f"Diagnose the common cause and propose guardrail instructions (e.g. require a "
                f"filter, avoid full scans) to prevent it. {why}")
    if t == TYPE_SCHEMA_CHANGED:
        return (f"Focus: this agent's connection schema changed. {why} "
                f"Update affected table/column references and instructions so answers stay correct.")
    if t == TYPE_QUERY_ERROR:
        return (f"Focus: {n} data query/queries on this agent errored. {why} "
                f"Propose instructions/fixes so the agent forms valid queries.")
    return why or "Review recent runs on this agent and propose instructions that improve accuracy."


async def run_scans(db: AsyncSession, organization_id: str) -> dict:
    """Run all sweep-based producers for an org (slow queries, low confidence,
    instruction suggestions). Returns counts emitted."""
    return {
        "slow_query": await scan_slow_queries(db, organization_id),
        "low_confidence": await scan_low_confidence(db, organization_id),
        "instruction_suggestion": await scan_instruction_suggestions(db, organization_id),
    }
