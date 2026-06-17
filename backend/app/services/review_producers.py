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
async def _changed_instructions_for_build(db, organization_id, build) -> List[Tuple[str, List[str]]]:
    """For a pending build, the instructions it actually changed vs main, each
    with the agent ids it's attached to (empty => global)."""
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
        ds_rows = (await db.execute(
            select(ids_assoc.c.data_source_id).where(ids_assoc.c.instruction_id == i)
        )).all()
        out.append((i, [str(d[0]) for d in ds_rows]))
    return out


async def emit_instruction_suggestions_for_build(db, organization_id: str, build, *,
                                                  why: Optional[str] = None) -> int:
    """Emit a suggestion item per (changed instruction × attached agent). Global
    instructions (no agent) emit a single global item (admin-only)."""
    from app.models.instruction import Instruction
    source = getattr(build, "source", "user")
    label = "AI" if source == "ai" else "Proposed"
    changed = await _changed_instructions_for_build(db, organization_id, build)
    n = 0
    for instruction_id, ds_ids in changed:
        instr = await db.get(Instruction, instruction_id)
        title = getattr(instr, "title", None) or "instruction"
        subject = {"kind": "instruction", "instruction_id": instruction_id, "build_id": str(build.id), "source": source}
        targets = ds_ids or [None]   # None => global
        for ds_id in targets:
            await review_service.emit(
                db, organization_id=organization_id, type=TYPE_INSTRUCTION_SUGGESTION,
                data_source_id=ds_id, severity=SEVERITY_INFO,
                title=f"{label} change · {title}",
                why=why or (f"{label} instruction change awaiting review."),
                group_key=f"instr:{instruction_id}",
                build_id=str(build.id), subject=subject,
            )
            n += 1
    return n


async def scan_instruction_suggestions(db: AsyncSession, organization_id: str) -> int:
    """Backfill/scan: emit suggestion items for all current non-admin/AI pending
    builds. (A pending 'user' build implies a non-admin author — admins
    auto-promote their own edits.)"""
    from app.models.instruction_build import InstructionBuild
    builds = (await db.execute(
        select(InstructionBuild).where(and_(
            InstructionBuild.organization_id == organization_id,
            InstructionBuild.is_main.is_(False),
            InstructionBuild.deleted_at.is_(None),
            InstructionBuild.status.in_(["draft", "pending_approval"]),
            InstructionBuild.source.in_(["user", "ai"]),
        ))
    )).scalars().all()
    n = 0
    for b in builds:
        n += await emit_instruction_suggestions_for_build(db, organization_id, b)
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


async def run_scans(db: AsyncSession, organization_id: str) -> dict:
    """Run all sweep-based producers for an org (slow queries, low confidence,
    instruction suggestions). Returns counts emitted."""
    return {
        "slow_query": await scan_slow_queries(db, organization_id),
        "low_confidence": await scan_low_confidence(db, organization_id),
        "instruction_suggestion": await scan_instruction_suggestions(db, organization_id),
    }
