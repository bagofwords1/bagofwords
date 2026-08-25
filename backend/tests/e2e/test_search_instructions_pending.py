"""E2E tests for pending-edit visibility in search_instructions.

Instruction edits are staged as versions; the live row is only updated when the
build is promoted. So a search hit's `text` hides any unapproved suggestion —
including one the current session just staged. Without surfacing them the
knowledge harness re-proposes learnings that are already awaiting review in a
separate build, duplicating the review and risking an overwrite on approval.

Contract under test:

1. an edit staged by THIS session is reported with is_current_session=True
   (safe to stack onto, per edit_instruction's pending-version base),
2. an edit staged by a DIFFERENT pending build is reported with
   is_current_session=False and counted in the message,
3. the pending edit's `delta` shows what the staged version adds, while `text`
   still shows the unchanged live row,
4. an instruction with no staged edit has pending_edit=None.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"pending_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _stage_edit(instruction_id, addition, *, user_id, org_id, build_id=None):
    """Stage an additive edit, returning the build it landed in."""
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    from sqlalchemy import select
    from app.models.instruction import Instruction

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": "knowledge",
            "training_build_id": build_id,
        }
        # Anchored append: `old_text` must be real text, so anchor the current
        # body and repeat it verbatim ahead of the addition.
        current = (await db.execute(
            select(Instruction.text).where(Instruction.id == instruction_id)
        )).scalar()
        end = None
        async for evt in EditInstructionTool().run_stream(
            {"instruction_id": instruction_id, "old_text": current,
             "text": f"{current}\n{addition}",
             "evidence": "Session evidence for the staged edit."},
            ctx,
        ):
            if evt.type == "tool.error":
                pytest.fail(f"edit tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None and end.payload["output"]["success"] is True, end
        return ctx.get("training_build_id")


async def _search(query, *, user_id, org_id, build_id=None):
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.search_instructions import SearchInstructionsTool

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": "knowledge",
            "training_build_id": build_id,
        }
        end = None
        async for evt in SearchInstructionsTool().run_stream(
            {"query": [query], "limit": 20}, ctx
        ):
            if evt.type == "tool.error":
                pytest.fail(f"search tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None, "expected a tool.end event"
        return end.payload["output"]


def _hit(output, instruction_id):
    for item in output["instructions"]:
        if item["id"] == str(instruction_id):
            return item
    pytest.fail(f"instruction {instruction_id} not in results: {output}")


ORIGINAL = "Churn is measured over a rolling 30 day window for the churn metric."
ADDITION = "Trial accounts are excluded from the churn denominator."
DRAFT_ONLY_TEXT = "Archived workspaces are excluded from every rollup."


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pending_edit_from_this_session_is_marked_current(
    create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    build_id = await _stage_edit(instr["id"], ADDITION, user_id=user_id, org_id=org_id)
    output = await _search("churn", user_id=user_id, org_id=org_id, build_id=build_id)

    hit = _hit(output, instr["id"])
    pending = hit["pending_edit"]
    assert pending is not None, "staged edit was invisible to search"
    assert pending["is_current_session"] is True
    assert pending["build_id"] == str(build_id)
    assert pending["evidence"]

    # The staged addition is surfaced as a delta, but `text` stays the live row.
    assert ADDITION in pending["delta"]
    assert hit["text"] == ORIGINAL
    assert ADDITION not in hit["text"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pending_edit_from_another_build_is_flagged_and_counted(
    create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    # Staged by an earlier, separate harness run.
    other_build_id = await _stage_edit(
        instr["id"], ADDITION, user_id=user_id, org_id=org_id
    )

    # This session has no build of its own yet.
    output = await _search("churn", user_id=user_id, org_id=org_id, build_id=None)

    pending = _hit(output, instr["id"])["pending_edit"]
    assert pending is not None
    assert pending["is_current_session"] is False, (
        "another build's suggestion must not look like this session's"
    )
    assert pending["build_id"] == str(other_build_id)
    assert "do not re-propose" in output["message"].lower()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_instruction_without_staged_edit_has_no_pending_edit(
    create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    output = await _search("churn", user_id=user_id, org_id=org_id)

    assert _hit(output, instr["id"])["pending_edit"] is None
    assert "do not re-propose" not in (output["message"] or "").lower()


def _reject_everything(test_client, instruction_id, headers):
    """Reject every hunk the review surface currently shows, as the UI does."""
    review = test_client.get(
        f"/api/instructions/{instruction_id}/review-hunks", headers=headers
    )
    assert review.status_code == 200, review.json()
    review = review.json()
    resp = test_client.post(
        f"/api/instructions/{instruction_id}/hunks/reject-all",
        json={
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
            "hunks": [
                {"build_id": s["build_id"], "hunk_key": h["key"]}
                for s in review["suggestions"]
                for h in s["hunks"]
            ],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_rejected_edit_is_no_longer_reported_as_pending(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """A reject records its verdict on the BUILD and leaves the build `draft`,
    so "the build is pending" stopped meaning "this proposal awaits a decision".
    Search kept advertising rejected suggestions as pending edits, telling the
    agent a learning was already staged for review when the reviewer had thrown
    it away."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )
    build_id = await _stage_edit(instr["id"], ADDITION, user_id=user_id, org_id=org_id)

    _reject_everything(test_client, instr["id"], _auth(token, org_id))

    output = await _search("churn", user_id=user_id, org_id=org_id, build_id=build_id)
    hit = _hit(output, instr["id"])
    assert hit["pending_edit"] is None, "a rejected suggestion is not awaiting review"
    assert hit["text"] == ORIGINAL
    assert "do not re-propose" not in (output["message"] or "").lower()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_orphaned_rejected_draft_is_not_reported_as_existing(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """The shape left behind by every reject that predates retirement: a draft
    row whose only proposal was settled, absent from the main build and from
    every build-scoped surface — yet search handed it to the agent, which then
    reported the rejected suggestion as an existing instruction and declined to
    re-create it."""
    import os
    import json as _json
    from sqlalchemy import create_engine, text as _sql

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    from tests.e2e.test_instruction import _inject_new_instruction_suggestion
    iid, bid = _inject_new_instruction_suggestion(org_id, DRAFT_ONLY_TEXT)

    # Stamp the verdict the way a reject does, without retiring the row.
    url = os.environ["TEST_DATABASE_URL"]
    sync_url = url.replace("sqlite+aiosqlite:", "sqlite:").replace(
        "postgresql+asyncpg:", "postgresql:"
    )
    engine = create_engine(sync_url)
    try:
        with engine.begin() as conn:
            vid = conn.execute(
                _sql("SELECT instruction_version_id FROM build_contents"
                     " WHERE build_id=:b AND instruction_id=:i"),
                {"b": bid, "i": iid},
            ).scalar()
            # The injected row has no author; the own-drafts branch this
            # guards is scoped to the caller's own drafts, so claim it.
            conn.execute(
                _sql("UPDATE instructions SET user_id=:u WHERE id=:i"),
                {"u": str(user_id), "i": iid},
            )
            conn.execute(
                _sql("UPDATE instruction_builds SET rejected_hunks=:r WHERE id=:b"),
                {"b": bid, "r": _json.dumps([
                    {"instruction_id": iid, "key": "__settled__", "action": "settle",
                     "main_version_id": None, "proposed_version_id": str(vid)},
                ])},
            )
    finally:
        engine.dispose()

    output = await _search("archived", user_id=user_id, org_id=org_id)
    assert str(iid) not in {item["id"] for item in output["instructions"]}, (
        "a rejected draft must not be offered to the agent as an existing instruction"
    )
