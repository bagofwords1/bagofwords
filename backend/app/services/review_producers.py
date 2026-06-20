"""Producers for the Review feed.

Each function detects a condition and emits (deduped) review items on a single
occurrence, fired from the event that caused it. All attribution to an agent
goes through
``report_data_source_association`` (a report can belong to several agents → fan
out one item per agent, so training/eval fired from the item stays agent-scoped).
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review_item import (
    TYPE_SLOW_QUERY, TYPE_LOW_CONFIDENCE, TYPE_INSTRUCTION_SUGGESTION, TYPE_SCHEMA_CHANGED,
    SEVERITY_WARNING, SEVERITY_INFO,
)
from app.services.review_service import review_service

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 24 * 7


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
        respect_dismissal=True, resurface_after_hours=DEFAULT_WINDOW_HOURS,
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
