"""Tests for the update_user_memory tool + the /users/me/instructions memory field.

Contracts:
- update_user_memory persists the full document to Membership.memory for the
  (user, org) pair (full rewrite, not append).
- An over-cap document is rejected and leaves the stored memory unchanged.
- An empty document clears the memory (sets it to NULL).
- The tool is available in chat/deep and hidden in training (allowed_modes).
- The self-service /users/me/instructions endpoint round-trips memory.

Run: cd backend && uv run pytest tests/e2e/test_user_memory_tool.py -v
"""
import asyncio

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.user import User
from app.schemas.organization_schema import MEMBERSHIP_MEMORY_MAX_LENGTH


def _run(coro):
    return asyncio.run(coro)


async def _run_tool(tool_input: dict, user_id: str, org_id: str):
    from app.ai.tools.implementations.update_user_memory import UpdateUserMemoryTool

    async with async_session_maker() as db:
        user = await db.get(User, user_id)
        organization = await db.get(Organization, org_id)
        runtime_ctx = {"db": db, "user": user, "organization": organization}
        events = []
        async for evt in UpdateUserMemoryTool().run_stream(tool_input, runtime_ctx):
            events.append(evt)
        return events


def _end(events):
    ends = [e for e in events if e.type == "tool.end"]
    assert ends, f"no tool.end in {[e.type for e in events]}"
    return ends[-1].payload


async def _memory_for(user_id: str, org_id: str):
    async with async_session_maker() as db:
        res = await db.execute(
            select(Membership.memory).where(
                Membership.user_id == user_id,
                Membership.organization_id == org_id,
            )
        )
        return res.scalar_one_or_none()


def _make_user_org(create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    who = whoami(token)
    return who["id"], who["organizations"][0]["id"], token


@pytest.mark.e2e
def test_update_user_memory_persists(create_user, login_user, whoami):
    user_id, org_id, _ = _make_user_org(create_user, login_user, whoami)

    payload = _end(_run(_run_tool(
        {"content": "Prefers ₪K. Likes cohort breakdowns.", "title": "Saved preference"},
        user_id, org_id,
    )))
    assert payload["output"]["success"] is True, payload
    assert _run(_memory_for(user_id, org_id)) == "Prefers ₪K. Likes cohort breakdowns."


@pytest.mark.e2e
def test_update_user_memory_is_full_rewrite(create_user, login_user, whoami):
    user_id, org_id, _ = _make_user_org(create_user, login_user, whoami)

    _run(_run_tool({"content": "line one"}, user_id, org_id))
    _run(_run_tool({"content": "line two only"}, user_id, org_id))
    # Second call replaces, not appends.
    assert _run(_memory_for(user_id, org_id)) == "line two only"


@pytest.mark.e2e
def test_update_user_memory_rejects_over_cap(create_user, login_user, whoami):
    user_id, org_id, _ = _make_user_org(create_user, login_user, whoami)

    _run(_run_tool({"content": "keep this"}, user_id, org_id))
    too_long = "x" * (MEMBERSHIP_MEMORY_MAX_LENGTH + 1)
    payload = _end(_run(_run_tool({"content": too_long}, user_id, org_id)))
    assert payload["output"]["success"] is False, payload
    # Rejected write must not clobber the existing memory.
    assert _run(_memory_for(user_id, org_id)) == "keep this"


@pytest.mark.e2e
def test_update_user_memory_empty_clears(create_user, login_user, whoami):
    user_id, org_id, _ = _make_user_org(create_user, login_user, whoami)

    _run(_run_tool({"content": "something"}, user_id, org_id))
    payload = _end(_run(_run_tool({"content": "   "}, user_id, org_id)))
    assert payload["output"]["success"] is True, payload
    assert _run(_memory_for(user_id, org_id)) is None


@pytest.mark.e2e
def test_update_user_memory_mode_gating():
    """chat/deep expose the tool; training must not."""
    from app.ai.registry import ToolRegistry

    r = ToolRegistry()
    for mode in ("chat", "deep"):
        names = {t["name"] for t in r.get_catalog_for_plan_type("action", mode=mode)}
        assert "update_user_memory" in names, f"missing in {mode}"
    training = {t["name"] for t in r.get_catalog_for_plan_type("action", mode="training")}
    assert "update_user_memory" not in training


@pytest.mark.e2e
def test_instructions_endpoint_roundtrips_memory(test_client, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}

    put = test_client.put(
        "/api/users/me/instructions",
        json={"note": "I'm the CFO", "memory": "Prefers USD. Concise."},
        headers=headers,
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["note"] == "I'm the CFO"
    assert body["memory"] == "Prefers USD. Concise."

    got = test_client.get("/api/users/me/instructions", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["memory"] == "Prefers USD. Concise."
