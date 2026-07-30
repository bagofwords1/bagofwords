"""Durable store for mid-run tool approvals ('ask' policy).

The app serves requests from several uvicorn workers (``start.sh`` passes
``--workers``), so the worker streaming a completion is usually *not* the worker
that receives the user's Allow/Deny POST. A pending approval therefore cannot
live in process memory: it is a ``tool_confirmations`` row, which any worker can
read and write.

The in-process registry (``app.ai.tools.confirmation``) is still used alongside
this, purely as a same-worker fast path so a local click wakes the run instantly
instead of on the next poll.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_confirmation import ToolConfirmation

logger = logging.getLogger(__name__)

KIND_MCP_TOOL_POLICY = "mcp_tool_policy"


class ToolConfirmationService:
    async def create(
        self,
        db: AsyncSession,
        *,
        confirmation_id: str,
        kind: str = KIND_MCP_TOOL_POLICY,
        organization_id: Optional[str] = None,
        report_id: Optional[str] = None,
        system_completion_id: Optional[str] = None,
        head_completion_id: Optional[str] = None,
        user_id: Optional[str] = None,
        connection_id: Optional[str] = None,
        connection_tool_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        arguments: Optional[dict] = None,
        timeout_seconds: float = 240.0,
    ) -> ToolConfirmation:
        """Insert the pending row. Commits — the responder needs to see it."""
        row = ToolConfirmation(
            id=str(confirmation_id),
            kind=kind,
            status=ToolConfirmation.STATUS_PENDING,
            organization_id=str(organization_id) if organization_id else None,
            report_id=str(report_id) if report_id else None,
            system_completion_id=str(system_completion_id) if system_completion_id else None,
            head_completion_id=str(head_completion_id) if head_completion_id else None,
            user_id=str(user_id) if user_id else None,
            connection_id=str(connection_id) if connection_id else None,
            connection_tool_id=str(connection_tool_id) if connection_tool_id else None,
            tool_name=tool_name,
            arguments=arguments or {},
            remember=False,
            expires_at=datetime.utcnow() + timedelta(seconds=timeout_seconds),
        )
        db.add(row)
        await db.commit()
        logger.info(
            f"ToolConfirmation {confirmation_id}: persisted (tool={tool_name}, "
            f"completion={system_completion_id})"
        )
        return row

    async def get(self, db: AsyncSession, confirmation_id: str) -> Optional[ToolConfirmation]:
        """Read the row, bypassing the identity map so a poll sees other
        workers' writes."""
        result = await db.execute(
            select(ToolConfirmation)
            .where(ToolConfirmation.id == str(confirmation_id))
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    def may_respond(self, row: ToolConfirmation, *, completion_id: str, user_id: str) -> bool:
        """The card must be addressed to one of the run's completions, and only
        the user who started the run may answer it."""
        completions = {
            c for c in (row.system_completion_id, row.head_completion_id) if c
        }
        if completions and str(completion_id) not in completions:
            return False
        if row.user_id and str(user_id) != str(row.user_id):
            return False
        return True

    async def resolve(
        self,
        db: AsyncSession,
        *,
        confirmation_id: str,
        approved: bool,
        remember: bool,
        user_id: Optional[str],
    ) -> Optional[ToolConfirmation]:
        """Record the decision. Idempotent: a row that is already resolved keeps
        its first decision, so a double click (or a retry after a dropped
        response) reports the same outcome instead of erroring.

        Returns the row as it stands after the call, or None if it is gone.
        """
        status = (
            ToolConfirmation.STATUS_APPROVED if approved else ToolConfirmation.STATUS_DENIED
        )
        # Conditional update so two workers racing the same click produce one
        # decision without a read-modify-write window.
        await db.execute(
            update(ToolConfirmation)
            .where(
                ToolConfirmation.id == str(confirmation_id),
                ToolConfirmation.status == ToolConfirmation.STATUS_PENDING,
            )
            .values(
                status=status,
                remember=bool(remember),
                resolved_by_user_id=str(user_id) if user_id else None,
                resolved_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        await db.commit()
        return await self.get(db, confirmation_id)

    async def expire(self, db: AsyncSession, confirmation_id: str) -> None:
        """Mark a still-pending row expired once the run stops waiting."""
        try:
            await db.execute(
                update(ToolConfirmation)
                .where(
                    ToolConfirmation.id == str(confirmation_id),
                    ToolConfirmation.status == ToolConfirmation.STATUS_PENDING,
                )
                .values(status=ToolConfirmation.STATUS_EXPIRED, updated_at=datetime.utcnow())
            )
            await db.commit()
        except Exception as e:  # best-effort bookkeeping
            logger.warning(f"ToolConfirmation {confirmation_id}: expire failed: {e!r}")
            try:
                await db.rollback()
            except Exception:
                pass

    async def poll_decision(self, confirmation_id: str) -> Optional[dict]:
        """Check for a decision on its own short-lived session.

        Deliberately not the agent's session: the tool has already committed and
        released it (SQLite has a single writer), and a poll must not re-open a
        transaction that the resolving request then has to wait behind.
        """
        from app.dependencies import async_session_maker

        try:
            async with async_session_maker() as session:
                row = await self.get(session, confirmation_id)
                # Read the columns while the row is still attached. rollback()
                # expires every loaded instance regardless of expire_on_commit,
                # so touching row.status after it (or after the session closes)
                # raises DetachedInstanceError — which killed the waiting run on
                # its first poll and made the Allow/Deny buttons appear dead.
                status = row.status if row is not None else None
                remember = bool(row.remember) if row is not None else False
                resolved_by = row.resolved_by_user_id if row is not None else None
                await session.rollback()
        except Exception as e:
            logger.warning(f"ToolConfirmation {confirmation_id}: poll failed: {e!r}")
            return None
        if status in (None, ToolConfirmation.STATUS_PENDING, ToolConfirmation.STATUS_EXPIRED):
            return None
        return {
            "approved": status == ToolConfirmation.STATUS_APPROVED,
            "remember": remember,
            "resolved_by_user_id": str(resolved_by) if resolved_by else None,
        }
