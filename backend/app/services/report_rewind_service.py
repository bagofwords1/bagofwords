"""Rewind a report conversation to before one of its prompts.

POST /api/reports/{id}/rewind removes a user prompt and everything after it
(its answer, later prompts and their answers, events, sub-agent rows, queued
prompts) from the conversation *and* from the agent's context, so the analyst
can take the conversation down a different path without starting a new
report. The removed prompt's text goes back to the composer for editing.

Removal is a soft delete (``Completion.deleted_at``). Rewound rows are hidden
from ORM reads by the listener in ``app.models.completion``, so the
transcript, the agent's history window, compaction, search and report lists
all stop seeing them without each needing its own filter. Observability
(the console) and the report Summary opt back in.

What is deliberately kept: widgets, queries, steps, visualizations and
dashboards built in the removed turns (they are report-scoped and may back a
published dashboard), and the user's memories (they belong to the user,
across reports; the confirmation says how many came from these turns).

What is undone with the turns: a rolling compaction summary or a /clear
boundary that covers a removed row (an earlier surviving /clear boundary stays
the hard cutoff), the token anchor, and check-ins the removed turns planned.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AppError, ErrorCode
from app.models.completion import Completion
from app.models.report import Report
from app.services.context_compaction_service import (
    COMPACTION_MESSAGE_TYPE,
    CLEAR_STARTING_RUN_GRACE_S,
    ContextCompactionService,
    context_compaction_service,
)
from app.settings.logging_config import get_logger

logger = get_logger(__name__)

# AgentCheckin.status_reason for a planned check-in cancelled by a rewind.
CHECKIN_REASON_REWOUND = "rewound"


async def _is_busy(db: AsyncSession, report_id: str) -> bool:
    """Anything still running on the report that a rewind could pull rows out
    from under: an agent run (or one starting — its rows exist before its
    AgentExecution does, the same grace /clear applies), a compaction, or a
    sub-agent run that has not finished (pending and waiting runs hold no
    execution slot, so the AgentExecution check alone misses them)."""
    from app.models.sub_agent import SubAgentRun, TERMINAL_RUN_STATUSES

    if context_compaction_service.is_running(report_id):
        return True
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
    if starting:
        return True
    open_sub_agents = await db.scalar(
        select(func.count(SubAgentRun.id)).where(
            SubAgentRun.report_id == report_id,
            SubAgentRun.status.notin_(TERMINAL_RUN_STATUSES),
        )
    )
    return bool(open_sub_agents)


async def _removed_ids(db: AsyncSession, report_id: str, target: Completion) -> list[str]:
    """The prompt, its own answer rows, every later prompt with its answers,
    and everything written after the prompt's answer started.

    Not simply ``created_at > prompt.created_at``: a prompt queued during an
    earlier run keeps its queue-time ``created_at`` when it is dispatched, so
    rows that earlier (surviving) run wrote while the prompt waited — steering
    messages, sub-agent rows — are newer than the prompt but belong to the
    earlier turn. Time-based removal therefore starts where the prompt's own
    answer starts; a prompt that was never queued is answered at once, so the
    two cutoffs coincide.
    """
    answer_started = await db.scalar(
        select(func.min(Completion.created_at)).where(
            Completion.report_id == report_id,
            Completion.parent_id == target.id,
            Completion.role == "system",
        )
    )
    time_cutoff = answer_started or target.created_at

    # Later prompts (queued ones included) — not steering rows (they belong to
    # the run they steered) and not sub-agent children (they belong to their
    # parent turn; children of removed turns go with the time cutoff).
    later_prompt_ids = select(Completion.id).where(
        Completion.report_id == report_id,
        Completion.role == "user",
        Completion.created_at > target.created_at,
        or_(Completion.message_type.is_(None), Completion.message_type != "steering"),
        or_(Completion.trigger_source.is_(None), Completion.trigger_source != "sub_agent"),
    )
    ids = (await db.execute(
        select(Completion.id).where(
            Completion.report_id == report_id,
            or_(
                Completion.id == target.id,
                Completion.parent_id == target.id,
                Completion.created_at > time_cutoff,
                Completion.id.in_(later_prompt_ids),
                Completion.parent_id.in_(later_prompt_ids),
            ),
        )
    )).scalars().all()
    return [str(i) for i in ids]


async def _visible_count(db: AsyncSession, completion_ids: list[str]) -> int:
    """How many of these rows the transcript shows (what the dialog counts)."""
    from app.services.report_message_search import visible_in_timeline

    if not completion_ids:
        return 0
    return int(await db.scalar(
        select(func.count(Completion.id)).where(
            Completion.id.in_(completion_ids),
            *visible_in_timeline(),
            or_(Completion.message_type.is_(None), Completion.message_type != COMPACTION_MESSAGE_TYPE),
        )
    ) or 0)


async def _repair_context_state(db: AsyncSession, report_id: str, removed: set[str]) -> None:
    """Drop compaction coverage that reaches into the removed rows, and the
    token anchor (it measured a prompt that included them)."""
    state = await ContextCompactionService.get_state(db, report_id)
    if state is None:
        return
    state.last_prompt_tokens = None
    state.last_prompt_estimate = None

    # A /clear boundary sits on a row; it goes when its row goes. Membership,
    # not timestamps, so legacy entries stored without completion_created_at
    # are judged correctly too.
    boundaries = [b for b in (state.clear_boundaries or []) if isinstance(b, dict)]
    kept = [b for b in boundaries if str(b.get("completion_id") or "") not in removed]
    if len(kept) != len(boundaries):
        state.clear_boundaries = kept or None

    # The summary covers every row at or before the watermark. It is stale if
    # the watermark row is removed or any removed row is at or before it.
    watermark_stale = False
    if state.covers_until_completion_id:
        if str(state.covers_until_completion_id) in removed:
            watermark_stale = True
        else:
            wm_created = await db.scalar(
                select(Completion.created_at)
                .where(Completion.id == str(state.covers_until_completion_id))
                .execution_options(include_rewound=True)
            )
            earliest_removed = await db.scalar(
                select(func.min(Completion.created_at))
                .where(Completion.id.in_(removed))
                .execution_options(include_rewound=True)
            )
            watermark_stale = (
                wm_created is None
                or (earliest_removed is not None and earliest_removed <= wm_created)
            )

    if watermark_stale:
        # The rolling summary folds in removed turns and cannot be trimmed, so
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


async def _cancel_checkins(db: AsyncSession, removed: list[str]) -> list[str]:
    """Mark planned check-ins of the removed turns cancelled (no commit).
    Returns their scheduler job ids, removed after the commit."""
    from app.models.agent_checkin import AgentCheckin, STATUS_CANCELLED, STATUS_PLANNED
    from app.services.checkin_service import job_id_for

    if not removed:
        return []
    rows = (await db.execute(
        select(AgentCheckin).where(
            AgentCheckin.status == STATUS_PLANNED,
            AgentCheckin.source_completion_id.in_(removed),
        )
    )).scalars().all()
    jobs = []
    for row in rows:
        jobs.append(row.job_id or job_id_for(row.id))
        row.status = STATUS_CANCELLED
        row.status_reason = CHECKIN_REASON_REWOUND
    return jobs


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
    rewindable prompt, 409 while anything is running on the report.
    """
    from app.services.sub_agent_locks import lock_report

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

    if not dry_run:
        # The lock run creation and queue dispatch take before they insert a
        # system row: from here to the commit no run can start on this report,
        # so the busy check below and the removal are one atomic step.
        await lock_report(db, report_id)

    if await _is_busy(db, report_id):
        # Nothing was written; the lock is released when the request's session
        # ends (it rolls back), so the caller's loaded objects stay usable.
        raise AppError.conflict(
            ErrorCode.REWIND_RUN_IN_PROGRESS,
            "An agent run is in progress on this report; try again when it finishes.",
        )

    removed_ids = await _removed_ids(db, report_id, target)
    visible_count = await _visible_count(db, removed_ids)
    memories_kept = await _memories_from(db, report, removed_ids)

    if dry_run:
        return {
            "status": "preview",
            "completion_id": str(target.id),
            "removed_count": len(removed_ids),
            "visible_removed_count": visible_count,
            "removed_ids": removed_ids,
            "memories_kept": memories_kept,
        }

    try:
        await db.execute(
            update(Completion)
            .where(Completion.id.in_(removed_ids), Completion.deleted_at.is_(None))
            .values(deleted_at=datetime.utcnow())
            .execution_options(synchronize_session=False)
        )
        await _repair_context_state(db, report_id, set(removed_ids))
        checkin_jobs = await _cancel_checkins(db, removed_ids)
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    if checkin_jobs:
        from app.services.checkin_service import checkin_service
        for job_id in checkin_jobs:
            checkin_service._remove_job(job_id)
    try:
        from app.services.completion_service import CompletionService
        CompletionService._estimate_cache.clear()
    except Exception:
        pass

    prompt = target.prompt if isinstance(target.prompt, dict) else {}
    logger.info(
        "[rewind] report=%s target=%s removed=%d checkins_cancelled=%d",
        report_id, target.id, len(removed_ids), len(checkin_jobs),
    )
    state = await ContextCompactionService.get_state(db, report_id)
    return {
        "status": "rewound",
        "completion_id": str(target.id),
        "removed_count": len(removed_ids),
        "visible_removed_count": visible_count,
        "removed_ids": removed_ids,
        "memories_kept": memories_kept,
        "checkins_cancelled": len(checkin_jobs),
        # Text only: the composer has no way to restore mention chips or
        # attached files, and the dialog says so.
        "prompt": {"content": prompt.get("content") or ""},
        **ContextCompactionService._state_payload(state),
    }
