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

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tool_confirmation import ToolConfirmation

logger = logging.getLogger(__name__)

KIND_MCP_TOOL_POLICY = "mcp_tool_policy"

# How far a user's answer reaches (ToolConfirmation.scope).
SCOPE_ONCE = "once"
SCOPE_REPORT = "report"
SCOPE_ALWAYS = "always"
SCOPES = (SCOPE_ONCE, SCOPE_REPORT, SCOPE_ALWAYS)


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
        tool_execution_id: Optional[str] = None,
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
            tool_execution_id=str(tool_execution_id) if tool_execution_id else None,
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
        response: Optional[dict] = None,
        scope: Optional[str] = None,
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
                scope=scope if scope in SCOPES else ("always" if remember else "once"),
                response=response,
                resolved_by_user_id=str(user_id) if user_id else None,
                resolved_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        await db.commit()
        return await self.get(db, confirmation_id)

    async def report_decision(
        self, db: AsyncSession, *, report_id: str, user_id: str, connection_tool_id: str,
    ) -> Optional[bool]:
        """The user's latest "for this report" answer for one connection tool,
        or None. True = allow, False = deny. Only MCP tool-policy rows count;
        remote shell keeps its own per-command session rules."""
        if not (report_id and user_id and connection_tool_id):
            return None
        row = (await db.execute(
            select(ToolConfirmation.status)
            .where(
                ToolConfirmation.kind == KIND_MCP_TOOL_POLICY,
                ToolConfirmation.scope == SCOPE_REPORT,
                ToolConfirmation.report_id == str(report_id),
                ToolConfirmation.user_id == str(user_id),
                ToolConfirmation.resolved_by_user_id == str(user_id),
                ToolConfirmation.connection_tool_id == str(connection_tool_id),
                ToolConfirmation.status.in_(
                    [ToolConfirmation.STATUS_APPROVED, ToolConfirmation.STATUS_DENIED]
                ),
            )
            .order_by(ToolConfirmation.resolved_at.desc())
            .limit(1)
        )).first()
        if row is None:
            return None
        return row[0] == ToolConfirmation.STATUS_APPROVED

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
                scope = row.scope if row is not None else None
                resolved_by = row.resolved_by_user_id if row is not None else None
                extra = row.response if row is not None else None
                await session.rollback()
        except Exception as e:
            logger.warning(f"ToolConfirmation {confirmation_id}: poll failed: {e!r}")
            return None
        if status in (None, ToolConfirmation.STATUS_PENDING, ToolConfirmation.STATUS_EXPIRED):
            return None
        decision = {
            "approved": status == ToolConfirmation.STATUS_APPROVED,
            "remember": remember,
            "scope": scope or ("always" if remember else "once"),
            "resolved_by_user_id": str(resolved_by) if resolved_by else None,
        }
        if extra is not None:
            decision["response"] = extra
        return decision


async def review_states_for_running_tools(
    db: AsyncSession, running: list[tuple[str, str, str]]
) -> dict[str, dict]:
    """Review state of tool executions that are still running, for read paths.

    ``running`` is ``(tool_execution_id, system_completion_id, tool_name)``.
    The approval card is otherwise only known to the live SSE stream, so a
    reload — or someone viewing a shared conversation — saw a running tool
    with no hint that it was waiting on a person. Returns, per tool execution
    id, ``{"state": pending|approved|denied|expired, "row": ToolConfirmation}``
    from the latest confirmation that tool asked for.

    Matched by ``tool_execution_id``. The tool name is only a fallback for
    rows written before that column existed: an MCP / Custom API / SSH
    confirmation stores the inner tool name (``create_note``) while the
    execution is named ``mcp__<conn>__create_note`` or ``execute_mcp``, so a
    name match never found it and the card disappeared on reload.
    """
    if not running:
        return {}
    te_ids = {str(te) for te, _, _ in running if te}
    completion_ids = {c for _, c, _ in running if c}
    tool_names = {t for _, _, t in running if t}
    conditions = []
    if te_ids:
        conditions.append(ToolConfirmation.tool_execution_id.in_(te_ids))
    if completion_ids and tool_names:
        conditions.append(and_(
            ToolConfirmation.tool_execution_id.is_(None),
            ToolConfirmation.system_completion_id.in_(completion_ids),
            ToolConfirmation.tool_name.in_(tool_names),
        ))
    if not conditions:
        return {}
    rows = (await db.execute(
        select(ToolConfirmation)
        .where(or_(*conditions))
        .order_by(ToolConfirmation.created_at)
    )).scalars().all()
    by_te: dict[str, ToolConfirmation] = {}
    by_name: dict[tuple[str, str], ToolConfirmation] = {}
    for row in rows:  # ascending created_at: the last write wins = latest
        if row.tool_execution_id:
            by_te[str(row.tool_execution_id)] = row
        else:
            by_name[(str(row.system_completion_id), row.tool_name)] = row
    matched: dict[str, ToolConfirmation] = {}
    unmatched: dict[tuple[str, str], list[str]] = {}
    for te_id, completion_id, tool_name in running:
        row = by_te.get(str(te_id))
        if row is not None:
            matched[str(te_id)] = row
        else:
            unmatched.setdefault((str(completion_id), tool_name), []).append(str(te_id))
    # Name fallback (rows without an execution id) only when it is
    # unambiguous: with several running calls of the same tool in one
    # completion, the name cannot say which of them asked, and a guess would
    # put one call's approval card on another.
    for key, te_ids in unmatched.items():
        row = by_name.get(key)
        if row is not None and len(te_ids) == 1:
            matched[te_ids[0]] = row
    # The live tool.confirmation event names the connection ("… · Billing
    # API"); rebuild that for a pending MCP card too.
    conn_ids = {
        str(r.connection_id) for r in matched.values()
        if r.connection_id and r.status == ToolConfirmation.STATUS_PENDING
    }
    conn_names: dict[str, str] = {}
    if conn_ids:
        from app.models.connection import Connection
        conn_names = {
            str(cid): name for cid, name in (await db.execute(
                select(Connection.id, Connection.name).where(Connection.id.in_(conn_ids))
            )).all()
        }
    return {
        te_id: {
            "state": row.status,
            "row": row,
            "connection_name": conn_names.get(str(row.connection_id)) if row.connection_id else None,
        }
        for te_id, row in matched.items()
    }


def confirmation_payload(row: ToolConfirmation, connection_name: Optional[str] = None) -> dict:
    """The ``tool.confirmation`` event payload, rebuilt from the durable row so
    an approval card can render after a reload."""
    # Builtin-tool asks store the card payload itself; MCP asks store the
    # call's arguments, which the event nests under "arguments".
    base = dict(row.arguments or {}) if row.kind != KIND_MCP_TOOL_POLICY else {"arguments": row.arguments or {}}
    return {
        **base,
        "kind": row.kind,
        "confirmation_id": str(row.id),
        "tool_name": row.tool_name,
        "connection_id": row.connection_id,
        "connection_tool_id": row.connection_tool_id,
        "connection_name": connection_name,
        "tool_execution_id": row.tool_execution_id,
    }
