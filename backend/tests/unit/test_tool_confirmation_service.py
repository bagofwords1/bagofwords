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

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py



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


# ---------------------------------------------------------------------------
# Review state for read paths (reload / shared conversation)
# ---------------------------------------------------------------------------

async def _ask(svc, *, completion_id, tool_name, kind="builtin_tool", arguments=None) -> str:
    cid = str(uuid4())
    async with async_session_maker() as db:
        await svc.create(
            db, confirmation_id=cid, kind=kind, tool_name=tool_name,
            system_completion_id=completion_id, arguments=arguments or {},
        )
    return cid


@pytest.mark.asyncio
async def test_running_tool_review_state_follows_its_latest_confirmation():
    from app.services.tool_confirmation_service import (
        confirmation_payload, review_states_for_running_tools,
    )
    svc = ToolConfirmationService()
    comp = str(uuid4())
    # First proposal was declined, the revised one is pending.
    first = await _ask(svc, completion_id=comp, tool_name="create_demo_dataset", arguments={"dataset_name": "v1"})
    async with async_session_maker() as db:
        await svc.resolve(db, confirmation_id=first, approved=False, remember=False, user_id=None)
    pending = await _ask(svc, completion_id=comp, tool_name="create_demo_dataset", arguments={"dataset_name": "v2"})

    te_id = str(uuid4())
    async with async_session_maker() as db:
        states = await review_states_for_running_tools(db, [(te_id, comp, "create_demo_dataset")])
        assert states[te_id]["state"] == "pending"
        payload = confirmation_payload(states[te_id]["row"])
    assert payload["confirmation_id"] == pending and payload["dataset_name"] == "v2"

    async with async_session_maker() as db:
        await svc.resolve(db, confirmation_id=pending, approved=True, remember=False, user_id=None)
        states = await review_states_for_running_tools(db, [(te_id, comp, "create_demo_dataset")])
    assert states[te_id]["state"] == "approved"


@pytest.mark.asyncio
async def test_review_state_is_scoped_to_completion_and_tool():
    from app.services.tool_confirmation_service import review_states_for_running_tools
    svc = ToolConfirmationService()
    comp, other = str(uuid4()), str(uuid4())
    await _ask(svc, completion_id=other, tool_name="create_demo_dataset")
    await _ask(svc, completion_id=comp, tool_name="set_report_agents")
    async with async_session_maker() as db:
        states = await review_states_for_running_tools(db, [(str(uuid4()), comp, "create_demo_dataset")])
    assert states == {}


@pytest.mark.asyncio
async def test_mcp_confirmation_payload_nests_call_arguments():
    from app.services.tool_confirmation_service import (
        KIND_MCP_TOOL_POLICY, confirmation_payload, review_states_for_running_tools,
    )
    svc = ToolConfirmationService()
    comp = str(uuid4())
    await _ask(svc, completion_id=comp, tool_name="send_message", kind=KIND_MCP_TOOL_POLICY, arguments={"text": "hi"})
    te_id = str(uuid4())
    async with async_session_maker() as db:
        states = await review_states_for_running_tools(db, [(te_id, comp, "send_message")])
        payload = confirmation_payload(states[te_id]["row"])
    assert payload["arguments"] == {"text": "hi"} and "text" not in payload
