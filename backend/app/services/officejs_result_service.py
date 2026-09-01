"""Durable store for pending Office.js tool results.

The app serves requests from several uvicorn workers (``start.sh`` passes
``--workers``), so the worker streaming a completion is usually *not* the
worker that receives the taskpane's ``POST /tool-results/{tool_call_id}``. A
pending Office.js call therefore cannot live in process memory alone: it is an
``officejs_pending_results`` row, which any worker can read and write.

The in-process registry (``app.ai.tools.officejs_registry``) is still used
alongside this, purely as a same-worker fast path so a local result wakes the
waiting tool instantly instead of on the next poll.

The row also carries the initiating ``user_id`` and the run's system
``completion_id``: pending tool_call_ids are visible to every viewer of the
report's SSE stream, so without this binding any org member could forge a
"spreadsheet result" into a running completion.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.officejs_pending_result import OfficeJsPendingResult

logger = logging.getLogger(__name__)

# resolve() outcomes — the endpoint maps these to HTTP statuses.
RESOLVE_OK = "ok"
RESOLVE_NOT_FOUND = "not_found"
RESOLVE_FORBIDDEN = "forbidden"
RESOLVE_ALREADY = "already_resolved"


class OfficeJsResultService:
    async def create_pending(
        self,
        *,
        tool_call_id: str,
        completion_id: Optional[str],
        user_id: Optional[str],
        timeout_seconds: float,
    ) -> bool:
        """Insert the pending row on a short-lived session and commit — the
        resolving worker needs to see it. Returns False (and logs) on failure
        so the caller can degrade to the in-memory fast path only."""
        from app.dependencies import async_session_maker

        try:
            async with async_session_maker() as session:
                row = OfficeJsPendingResult(
                    tool_call_id=str(tool_call_id),
                    status=OfficeJsPendingResult.STATUS_PENDING,
                    completion_id=str(completion_id) if completion_id else None,
                    user_id=str(user_id) if user_id else None,
                    expires_at=datetime.utcnow() + timedelta(seconds=timeout_seconds),
                )
                session.add(row)
                await session.commit()
            return True
        except Exception as e:
            logger.warning(
                "OfficeJsPendingResult %s: persist failed (%r) — falling back to "
                "same-worker-only resolution.",
                tool_call_id, e,
            )
            return False

    async def resolve(
        self,
        db: AsyncSession,
        *,
        tool_call_id: str,
        completion_id: str,
        user_id: Optional[str],
        result: Dict[str, Any],
    ) -> str:
        """Validate the responder against the row's binding and record the
        result. Returns one of the RESOLVE_* outcomes."""
        row = (
            await db.execute(
                select(OfficeJsPendingResult)
                .where(OfficeJsPendingResult.tool_call_id == str(tool_call_id))
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            return RESOLVE_NOT_FOUND
        # The action was issued for one specific system completion; a result
        # POSTed via any other completion id is not acceptable.
        if row.completion_id and str(completion_id) != str(row.completion_id):
            return RESOLVE_NOT_FOUND
        if row.user_id and (user_id is None or str(user_id) != str(row.user_id)):
            return RESOLVE_FORBIDDEN
        if row.status != OfficeJsPendingResult.STATUS_PENDING:
            return RESOLVE_ALREADY

        # Conditional update so two racing POSTs produce one result without a
        # read-modify-write window.
        outcome = await db.execute(
            update(OfficeJsPendingResult)
            .where(
                OfficeJsPendingResult.tool_call_id == str(tool_call_id),
                OfficeJsPendingResult.status == OfficeJsPendingResult.STATUS_PENDING,
            )
            .values(
                status=OfficeJsPendingResult.STATUS_RESOLVED,
                result=result,
                resolved_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        await db.commit()
        return RESOLVE_OK if outcome.rowcount else RESOLVE_ALREADY

    async def poll_result(self, tool_call_id: str) -> Optional[Dict[str, Any]]:
        """Check for a resolved result on its own short-lived session.

        Deliberately not the agent's session: a poll must not re-open a
        transaction that the resolving request then has to wait behind (see
        ToolConfirmationService.poll_decision for the same pattern).
        """
        from app.dependencies import async_session_maker

        try:
            async with async_session_maker() as session:
                row = (
                    await session.execute(
                        select(OfficeJsPendingResult)
                        .where(OfficeJsPendingResult.tool_call_id == str(tool_call_id))
                        .execution_options(populate_existing=True)
                    )
                ).scalar_one_or_none()
                # Read columns while attached (rollback expires instances).
                status = row.status if row is not None else None
                result = dict(row.result) if row is not None and row.result else None
                await session.rollback()
        except Exception as e:
            logger.warning("OfficeJsPendingResult %s: poll failed: %r", tool_call_id, e)
            return None
        if status != OfficeJsPendingResult.STATUS_RESOLVED:
            return None
        return result or {}

    async def discard(self, tool_call_id: str) -> None:
        """Delete the row once the tool stops waiting (result consumed, timed
        out, or cancelled). A late POST then 404s, same as before. Best-effort."""
        from app.dependencies import async_session_maker

        try:
            async with async_session_maker() as session:
                await session.execute(
                    delete(OfficeJsPendingResult).where(
                        OfficeJsPendingResult.tool_call_id == str(tool_call_id)
                    )
                )
                await session.commit()
        except Exception as e:
            logger.warning("OfficeJsPendingResult %s: discard failed: %r", tool_call_id, e)
