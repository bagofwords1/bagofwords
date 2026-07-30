"""Regression tests for ToolConfirmationService.poll_decision.

The waiting 'ask' run polls the tool_confirmations row every few seconds on a
fresh short-lived session. poll_decision used to read ``row.status`` AFTER
``session.rollback()`` and after the ``async with`` block closed the session —
rollback expires every loaded instance (regardless of expire_on_commit), so the
very first poll raised DetachedInstanceError ("Instance <ToolConfirmation> is
not bound to a Session"), the tool errored out after its retry, and the
Allow/Deny buttons in the report did nothing no matter what the user clicked.
"""

from uuid import uuid4

import pytest

from app.dependencies import async_session_maker
from app.services.tool_confirmation_service import ToolConfirmationService


async def _create_pending(svc: ToolConfirmationService) -> str:
    cid = str(uuid4())
    async with async_session_maker() as db:
        await svc.create(
            db,
            confirmation_id=cid,
            tool_name="echo",
            arguments={"message": "hi"},
        )
    return cid


@pytest.mark.asyncio
async def test_poll_decision_pending_returns_none_without_raising():
    svc = ToolConfirmationService()
    cid = await _create_pending(svc)
    assert await svc.poll_decision(cid) is None


@pytest.mark.asyncio
async def test_poll_decision_reads_resolved_row_after_its_session_closed():
    """The other-worker path: the decision is only visible via the DB row."""
    svc = ToolConfirmationService()
    cid = await _create_pending(svc)

    # Resolve on a separate session, the way the approval POST's worker does.
    resolver = str(uuid4())
    async with async_session_maker() as db:
        await svc.resolve(
            db, confirmation_id=cid, approved=True, remember=True, user_id=resolver
        )

    assert await svc.poll_decision(cid) == {
        "approved": True, "remember": True, "resolved_by_user_id": resolver,
    }


@pytest.mark.asyncio
async def test_poll_decision_denied_and_expired():
    svc = ToolConfirmationService()

    denied = await _create_pending(svc)
    async with async_session_maker() as db:
        await svc.resolve(
            db, confirmation_id=denied, approved=False, remember=False, user_id=None
        )
    assert await svc.poll_decision(denied) == {
        "approved": False, "remember": False, "resolved_by_user_id": None,
    }

    expired = await _create_pending(svc)
    async with async_session_maker() as db:
        await svc.expire(db, expired)
    assert await svc.poll_decision(expired) is None

    assert await svc.poll_decision(str(uuid4())) is None
