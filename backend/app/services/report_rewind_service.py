"""Rewind a report chat to an earlier prompt (BOW-145).

POST /api/reports/{report_id}/rewind {completion_id}

The chosen user prompt and everything after it — replies, silent session
events, sub-agent rows, queued prompts — are soft-deleted. Soft-deleted
completions are hidden from every ORM read (see the `do_orm_execute` hook in
app.models.completion), so they leave the transcript and the agent's context
at once and do not come back after a reload. The prompt text is returned so
the composer can offer it again to edit and resend.

What it deliberately keeps (v1): widgets, queries, steps, artifacts and
dashboards built in the removed turns. They are report-scoped and may back a
published dashboard; removing a turn must not break one.

The compaction state is repaired in the same transaction:
  - a summary whose watermark was removed quotes the removed turns, so it is
    dropped; the watermark falls back to the newest surviving /clear boundary
    (or none), and every surviving turn is detailed context again;
  - /clear boundaries that sat on removed rows are dropped;
  - the provider token anchor measured a request that included the removed
    turns, so it is reset.

Idle-only, like /compact and /clear: the route returns 409 while an agent run
(or a compaction) is active on the report.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.models.mention import Mention
from app.models.plan import Plan
from app.models.report import Report
from app.services.context_compaction_service import (
    COMPACTION_MESSAGE_TYPE,
    ContextCompactionService,
    context_compaction_service,
)
from app.settings.logging_config import get_logger

logger = get_logger(__name__)


class RewindError(Exception):
    """A rewind request that cannot be honoured; `status_code` maps to HTTP."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def is_rewindable(c: Completion) -> bool:
    """Only a top-level prompt the user typed can be a rewind point: not a
    machine trigger, a sub-agent row, a steering bubble or a queued prompt."""
    return (
        c.role == "user"
        and c.status != "queued"
        and (c.message_type or "") not in ("steering", COMPACTION_MESSAGE_TYPE)
        and not getattr(c, "trigger_source", None)
        and not getattr(c, "webhook_id", None)
    )


def _prompt_text(c: Completion) -> str:
    p = c.prompt
    if isinstance(p, dict):
        return str(p.get("content") or "")
    return str(p or "")


class ReportRewindService:
    async def removal_ids(self, db: AsyncSession, report_id: str, target: Completion) -> list[str]:
        """Every completion of the report at or after the rewind point."""
        rows = (await db.execute(
            select(Completion.id)
            .where(Completion.report_id == str(report_id))
            .where(Completion.created_at >= target.created_at)
        )).scalars().all()
        return [str(r) for r in rows]

    async def rewind(self, db: AsyncSession, report: Report, completion_id: str) -> dict:
        report_id = str(report.id)
        target = (await db.execute(
            select(Completion)
            .where(Completion.id == str(completion_id))
            .where(Completion.report_id == report_id)
        )).scalar_one_or_none()
        if target is None:
            raise RewindError(404, "Message not found")
        if not is_rewindable(target):
            raise RewindError(400, "Only a prompt you sent can be rewound to")

        lock = context_compaction_service._lock_for(report_id)
        if lock.locked():
            raise RewindError(409, "Context compaction is in progress on this report; try again when it finishes.")
        async with lock:
            if await ContextCompactionService.is_report_busy(db, report_id):
                raise RewindError(409, "An agent run is in progress on this report; try again when it finishes.")

            ids = await self.removal_ids(db, report_id, target)
            removed = set(ids)
            now = datetime.utcnow()
            prompt = _prompt_text(target)
            try:
                await db.execute(
                    update(Completion)
                    .where(Completion.id.in_(ids))
                    .values(deleted_at=now)
                    .execution_options(synchronize_session=False)
                )
                for model in (CompletionBlock, Mention, Plan):
                    await db.execute(
                        update(model)
                        .where(model.completion_id.in_(ids))
                        .where(model.deleted_at.is_(None))
                        .values(deleted_at=now)
                        .execution_options(synchronize_session=False)
                    )
                state = await ContextCompactionService.get_state(db, report_id)
                if state is not None:
                    self._repair_state(state, removed)
                await db.commit()
            except Exception:
                await db.rollback()
                raise

        try:
            from app.services.completion_service import CompletionService
            CompletionService._estimate_cache.clear()
        except Exception:
            pass

        logger.info("[rewind] report=%s target=%s removed=%d", report_id, completion_id, len(ids))
        return {
            "status": "rewound",
            "completion_id": str(completion_id),
            "removed_count": len(ids),
            "removed_completion_ids": ids,
            "prompt": prompt,
            "compaction": ContextCompactionService._state_payload(state),
        }

    @staticmethod
    def _repair_state(state, removed: set[str]) -> None:
        boundaries = [
            b for b in (state.clear_boundaries or [])
            if isinstance(b, dict) and str(b.get("completion_id")) not in removed
        ]
        if len(boundaries) != len(state.clear_boundaries or []):
            state.clear_boundaries = boundaries

        watermark = state.covers_until_completion_id
        if watermark and str(watermark) in removed:
            # The summary folded removed turns: drop it. A surviving /clear
            # boundary is still a hard cutoff, so the watermark falls back to
            # it (that is exactly what /clear writes); otherwise no watermark.
            surviving_clear: Optional[str] = ContextCompactionService.latest_clear_boundary_id(state)
            state.summary_json = {}
            state.covers_until_completion_id = surviving_clear
            state.covered_turns = 0
            state.tokens_compacted_total = 0
            state.last_compaction_at = None
            state.last_method = None
            state.degraded = False
            state.last_tokens_before = None
            state.last_tokens_after = None
            state.failure_count = 0
            state.cooldown_until = None

        # The provider-measured request size included the removed turns.
        state.last_prompt_tokens = None
        state.last_prompt_estimate = None


report_rewind_service = ReportRewindService()
