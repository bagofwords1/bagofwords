"""Rewind a report conversation to before one of its prompts.

POST /api/reports/{id}/rewind removes a user prompt and everything after it
(answers, later prompts, events, sub-agent rows, queued prompts) from the
conversation *and* from the agent's context, so the analyst can take the
conversation down a different path without starting a new report. The
removed prompt's text goes back to the composer for editing.

Removal is a soft delete (``Completion.deleted_at``). Rewound rows are hidden
from every ORM read by the listener in ``app.models.completion``, so the
transcript, the agent's history window, compaction, search and report lists
all stop seeing them without each needing its own filter.

What is deliberately kept: widgets, queries, steps, visualizations and
dashboards built in the removed turns. They are report-scoped and may back a
published dashboard; the dashboard keeps working after a rewind.

The compaction state is repaired: a rolling summary or a /clear boundary that
covers any removed turn is dropped (an earlier surviving /clear boundary is
kept as the hard cutoff), so nothing from the removed turns comes back through
the summary.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models.completion import Completion
from app.models.report import Report
from app.services.context_compaction_service import (
    CLEAR_STARTING_RUN_GRACE_S,
    ContextCompactionService,
    context_compaction_service,
)
from app.settings.logging_config import get_logger

logger = get_logger(__name__)


def _parse_iso(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


async def _is_busy(db: AsyncSession, report_id: str) -> bool:
    """A run is active, or one is starting (its rows exist before its
    AgentExecution does — the same grace /clear applies)."""
    if await ContextCompactionService.is_report_busy(db, report_id):
        return True
    starting = await db.scalar(
        select(func.count(Completion.id)).where(
            Completion.report_id == report_id,
            Completion.role == "system",
            Completion.status == "in_progress",
            Completion.created_at > datetime.utcnow() - timedelta(seconds=CLEAR_STARTING_RUN_GRACE_S),
        )
    )
    return bool(starting)


async def _repair_context_state(db: AsyncSession, report_id: str, cutoff: datetime) -> None:
    """Drop compaction coverage that reaches into the removed turns."""
    state = await ContextCompactionService.get_state(db, report_id)
    if state is None:
        return

    boundaries = [b for b in (state.clear_boundaries or []) if isinstance(b, dict)]
    kept = [
        b for b in boundaries
        if (ts := _parse_iso(b.get("completion_created_at"))) is not None and ts < cutoff
    ]
    boundaries_changed = len(kept) != len(boundaries)

    # The watermark row is rewound -> it no longer loads (hidden like every
    # other rewound row), so a missing row means the summary covers removed
    # turns.
    watermark_stale = False
    if state.covers_until_completion_id:
        wm_created = await db.scalar(
            select(Completion.created_at).where(Completion.id == state.covers_until_completion_id)
        )
        watermark_stale = wm_created is None or wm_created >= cutoff

    if not (boundaries_changed or watermark_stale):
        return

    state.clear_boundaries = kept or None
    if watermark_stale:
        # The rolling summary folds in removed turns; it cannot be trimmed, so
        # it goes. The surviving turns are back in the detailed window and
        # compact again when they need to. A surviving /clear boundary stays
        # the hard cutoff, exactly as /clear left it.
        latest_kept = kept[-1] if kept else None
        state.summary_json = {}
        state.covers_until_completion_id = (
            str(latest_kept["completion_id"]) if latest_kept and latest_kept.get("completion_id") else None
        )
        state.covered_turns = 0
        state.tokens_compacted_total = 0
        state.last_compaction_at = None
        state.last_method = None
        state.degraded = False
        state.last_tokens_before = None
        state.last_tokens_after = None
        state.failure_count = 0
        state.cooldown_until = None
    # The token anchor measured a prompt that included the removed turns.
    state.last_prompt_tokens = None
    state.last_prompt_estimate = None


async def _memories_from(db: AsyncSession, report: Report, completion_ids: list[str]) -> int:
    """How many of the report owner's active memories were saved from these
    completions. Rewind keeps them (memory is the user's, across reports);
    the confirmation only says so."""
    from app.models.memory_entry import MemoryEntry

    if not completion_ids:
        return 0
    wanted = set(completion_ids)
    evidences = (await db.execute(
        select(MemoryEntry.evidence).where(
            MemoryEntry.organization_id == str(report.organization_id),
            MemoryEntry.user_id == str(report.user_id),
            MemoryEntry.status == "active",
            MemoryEntry.deleted_at.is_(None),
        )
    )).scalars().all()
    return sum(
        1 for ev in evidences
        if isinstance(ev, dict) and str(ev.get("completion_id") or "") in wanted
    )


async def rewind_report(db: AsyncSession, report: Report, completion_id: str, *, dry_run: bool = False) -> dict:
    """Remove the user prompt ``completion_id`` and everything after it.

    ``dry_run`` validates and reports what would be removed (and how many
    memories came from those turns) without changing anything.

    Raises AppError 404 for an unknown prompt, 400 for a row that is not a
    rewindable prompt, 409 while a run is active on the report.
    """
    report_id = str(report.id)
    target = (await db.execute(
        select(Completion).where(
            Completion.id == str(completion_id),
            Completion.report_id == report_id,
        )
    )).scalars().first()
    if target is None:
        raise AppError.not_found(ErrorCode.REWIND_TARGET_NOT_FOUND, "Message not found in this report")
    if (
        target.role != "user"
        or target.status == "queued"
        or (target.message_type or "") == "steering"
        or target.trigger_source is not None
        or target.webhook_id is not None
    ):
        raise AppError.bad_request(ErrorCode.REWIND_INVALID_TARGET, "Only a prompt you sent can be rewound to")

    if await _is_busy(db, report_id) or context_compaction_service.is_running(report_id):
        raise AppError.conflict(
            ErrorCode.REWIND_RUN_IN_PROGRESS,
            "An agent run is in progress on this report; try again when it finishes.",
        )

    cutoff = target.created_at
    # Everything after the prompt, the prompt itself, and its own answer rows
    # (parent_id) even when a bulk insert gave them the prompt's timestamp.
    removed_ids = [str(i) for i in (await db.execute(
        select(Completion.id).where(
            Completion.report_id == report_id,
            (Completion.created_at > cutoff)
            | (Completion.id == target.id)
            | (Completion.parent_id == target.id),
        )
    )).scalars().all()]
    memories_kept = await _memories_from(db, report, removed_ids)

    if dry_run:
        return {
            "status": "preview",
            "completion_id": str(target.id),
            "removed_count": len(removed_ids),
            "removed_ids": removed_ids,
            "memories_kept": memories_kept,
        }

    now = datetime.utcnow()
    await db.execute(
        update(Completion)
        .where(Completion.id.in_(removed_ids), Completion.deleted_at.is_(None))
        .values(deleted_at=now)
        .execution_options(synchronize_session=False)
    )
    await _repair_context_state(db, report_id, cutoff)
    await db.commit()

    try:
        from app.services.completion_service import CompletionService
        CompletionService._estimate_cache.clear()
    except Exception:
        pass

    prompt = target.prompt if isinstance(target.prompt, dict) else {}
    logger.info("[rewind] report=%s target=%s removed=%d", report_id, target.id, len(removed_ids))
    state = await ContextCompactionService.get_state(db, report_id)
    return {
        "status": "rewound",
        "completion_id": str(target.id),
        "removed_count": len(removed_ids),
        "removed_ids": removed_ids,
        "memories_kept": memories_kept,
        "prompt": {
            "content": prompt.get("content") or "",
            "mentions": prompt.get("mentions") or [],
            "mode": prompt.get("mode"),
        },
        **ContextCompactionService._state_payload(state),
    }
