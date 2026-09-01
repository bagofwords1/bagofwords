"""Tests for OfficeJsResultService — the durable half of the Office.js bridge.

The tool that dispatches an excel_action and the HTTP POST that returns its
result usually land on different uvicorn workers, so the pending call is an
``officejs_pending_results`` row that any worker can resolve; the waiting tool
polls it. These tests exercise the cross-worker path (resolve on one session,
poll on another) and the user/completion binding that stops other org members
from forging a result into a running completion.
"""

from uuid import uuid4

import pytest

from app.dependencies import async_session_maker
from app.services.officejs_result_service import (
    OfficeJsResultService,
    RESOLVE_ALREADY,
    RESOLVE_FORBIDDEN,
    RESOLVE_NOT_FOUND,
    RESOLVE_OK,
)


async def _create_pending(svc: OfficeJsResultService, *, completion_id: str, user_id: str) -> str:
    tool_call_id = str(uuid4())
    assert await svc.create_pending(
        tool_call_id=tool_call_id,
        completion_id=completion_id,
        user_id=user_id,
        timeout_seconds=55,
    )
    return tool_call_id


@pytest.mark.asyncio
async def test_poll_pending_returns_none():
    svc = OfficeJsResultService()
    tcid = await _create_pending(svc, completion_id=str(uuid4()), user_id=str(uuid4()))
    assert await svc.poll_result(tcid) is None
    await svc.discard(tcid)


@pytest.mark.asyncio
async def test_resolve_then_poll_cross_session():
    """The other-worker path: the result is only visible via the DB row."""
    svc = OfficeJsResultService()
    completion_id, user_id = str(uuid4()), str(uuid4())
    tcid = await _create_pending(svc, completion_id=completion_id, user_id=user_id)

    result = {"success": True, "return_value": {"wrote_to": "Sheet1!A6:B7"}, "error": None,
              "logs": [], "ranges_touched": ["Sheet1!A6:B7"]}
    async with async_session_maker() as db:
        assert await svc.resolve(
            db, tool_call_id=tcid, completion_id=completion_id, user_id=user_id, result=result
        ) == RESOLVE_OK

    polled = await svc.poll_result(tcid)
    assert polled is not None
    assert polled["success"] is True
    assert polled["return_value"] == {"wrote_to": "Sheet1!A6:B7"}
    await svc.discard(tcid)


@pytest.mark.asyncio
async def test_resolve_rejects_wrong_completion_and_wrong_user():
    svc = OfficeJsResultService()
    completion_id, user_id = str(uuid4()), str(uuid4())
    tcid = await _create_pending(svc, completion_id=completion_id, user_id=user_id)

    async with async_session_maker() as db:
        # A result POSTed via a different completion id must not resolve it.
        assert await svc.resolve(
            db, tool_call_id=tcid, completion_id=str(uuid4()), user_id=user_id,
            result={"success": True},
        ) == RESOLVE_NOT_FOUND
        # Another org member (different user) must not be able to forge one.
        assert await svc.resolve(
            db, tool_call_id=tcid, completion_id=completion_id, user_id=str(uuid4()),
            result={"success": True},
        ) == RESOLVE_FORBIDDEN
        # Still pending — the legitimate responder can then resolve it.
        assert await svc.resolve(
            db, tool_call_id=tcid, completion_id=completion_id, user_id=user_id,
            result={"success": True},
        ) == RESOLVE_OK
        # Exactly once: a second POST reports already-resolved.
        assert await svc.resolve(
            db, tool_call_id=tcid, completion_id=completion_id, user_id=user_id,
            result={"success": False},
        ) == RESOLVE_ALREADY
    await svc.discard(tcid)


@pytest.mark.asyncio
async def test_discard_makes_late_results_unresolvable():
    svc = OfficeJsResultService()
    completion_id, user_id = str(uuid4()), str(uuid4())
    tcid = await _create_pending(svc, completion_id=completion_id, user_id=user_id)
    await svc.discard(tcid)
    assert await svc.poll_result(tcid) is None
    async with async_session_maker() as db:
        assert await svc.resolve(
            db, tool_call_id=tcid, completion_id=completion_id, user_id=user_id,
            result={"success": True},
        ) == RESOLVE_NOT_FOUND


@pytest.mark.asyncio
async def test_unknown_tool_call_id_not_found():
    svc = OfficeJsResultService()
    async with async_session_maker() as db:
        assert await svc.resolve(
            db, tool_call_id=str(uuid4()), completion_id=str(uuid4()), user_id=str(uuid4()),
            result={"success": True},
        ) == RESOLVE_NOT_FOUND
    assert await svc.poll_result(str(uuid4())) is None
