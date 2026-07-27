"""E2E tests for search_instructions in CHAT mode.

Chat mode differs from training/knowledge:
  - scope is FORCED to the report's data sources (agent-supplied
    data_source_ids are ignored); no report context → refusal
  - published-only (the training draft build is never merged in)
  - compact output: text is a ~140-char snippet, not the full body
"""
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_SQLITE_DB = (Path(__file__).resolve().parent.parent / "config" / "chinook.sqlite").resolve()


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"searchchat_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


def _create(test_client, token, org_id, **fields):
    resp = test_client.post(
        "/api/instructions", json={"status": "published", **fields}, headers=_auth(token, org_id)
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


async def _run(tool_input, *, user_id, org_id, mode="chat", scope_ds_ids=None, with_report=True):
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.search_instructions import SearchInstructionsTool

    tool = SearchInstructionsTool()
    async with async_session_maker() as db:
        # user must be a real ORM user for the service permission checks
        from sqlalchemy import select
        from app.models.user import User
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        from app.models.organization import Organization
        org = (await db.execute(select(Organization).where(Organization.id == org_id))).scalar_one()
        ctx = {"db": db, "user": user, "organization": org, "mode": mode}
        if with_report:
            ctx["report"] = SimpleNamespace(
                id=str(uuid.uuid4()),
                data_sources=[SimpleNamespace(id=d) for d in (scope_ds_ids or [])],
            )
        end = None
        async for evt in tool.run_stream(tool_input, ctx):
            if evt.type == "tool.end":
                end = evt
            if evt.type == "tool.error":
                return {"success": False, "error": evt.payload}
        assert end is not None
        return end.payload["output"]


def test_search_instructions_allows_chat_mode():
    from app.ai.tools.implementations.search_instructions import SearchInstructionsTool

    md = SearchInstructionsTool().metadata
    assert "chat" in md.allowed_modes
    assert "training" in md.allowed_modes


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_mode_returns_snippets_not_full_text(create_user, login_user, whoami, test_client):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    long_body = "Refund handling rule. " + "Detail sentence about refunds. " * 30
    inst = _create(
        test_client, token, org_id,
        text=long_body, title="Refunds", load_mode="intelligent",
    )

    out = await _run({"query": ["refund"], "limit": 10}, user_id=uid, org_id=org_id)
    assert out["success"] is True, out
    hits = [i for i in out["instructions"] if i["id"] == inst["id"]]
    assert hits, out
    assert len(hits[0]["text"]) <= 140
    assert hits[0]["text"].endswith("…")
    assert "read_instruction" in (out["message"] or "")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_mode_without_report_is_refused(create_user, login_user, whoami, test_client):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    out = await _run({"query": ["anything"]}, user_id=uid, org_id=org_id, with_report=False)
    assert out["success"] is False, out
    assert "report session" in (out["message"] or "")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_chat_mode_forces_report_scope(
    create_user, login_user, whoami, test_client, create_data_source
):
    """Agent-supplied data_source_ids are ignored in chat mode — the report's
    scope wins, so a rule attached to an out-of-scope data source stays hidden."""
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    ds_a = create_data_source(
        name="ds_a", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )
    ds_b = create_data_source(
        name="ds_b", type="sqlite", config={"database": str(_SQLITE_DB)},
        credentials={}, user_token=token, org_id=org_id,
    )
    inst = _create(
        test_client, token, org_id,
        text="Rule about margins, scoped to ds_a.", title="Margins",
        load_mode="intelligent", data_source_ids=[ds_a["id"]],
    )

    # Report scoped to ds_b; the agent tries to sneak ds_a into the input.
    out = await _run(
        {"query": ["margins"], "data_source_ids": [ds_a["id"]]},
        user_id=uid, org_id=org_id, scope_ds_ids=[ds_b["id"]],
    )
    assert out["success"] is True, out
    assert not any(i["id"] == inst["id"] for i in out["instructions"]), (
        "chat mode must ignore agent-supplied data_source_ids"
    )

    # Same call from a report scoped to ds_a finds it.
    out_a = await _run(
        {"query": ["margins"]}, user_id=uid, org_id=org_id, scope_ds_ids=[ds_a["id"]],
    )
    assert any(i["id"] == inst["id"] for i in out_a["instructions"]), out_a


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_training_mode_keeps_full_text(create_user, login_user, whoami, test_client):
    token, uid, org_id = _new_admin(create_user, login_user, whoami)
    long_body = "Margin rule. " + "More margin detail. " * 30
    inst = _create(
        test_client, token, org_id, text=long_body, title="Margins", load_mode="intelligent",
    )
    out = await _run({"query": ["margin"]}, user_id=uid, org_id=org_id, mode="training")
    assert out["success"] is True, out
    hits = [i for i in out["instructions"] if i["id"] == inst["id"]]
    assert hits, out
    assert hits[0]["text"] == long_body
