"""E2E tests for edit_instruction anchor semantics (anti-destructive edits).

The knowledge harness edits instructions autonomously, so a text edit must be
surgical: it either appends (``old_text: ""``) or replaces an anchored snippet
(``old_text: "<exact snippet>"``). Whole-text replacement (``text`` without
``old_text``) stays available to training mode, where a human curates the
result, and is rejected in knowledge mode. Contract under test:

1. append preserves the existing text verbatim and adds a paragraph,
2. an anchored replace changes only the anchored snippet (whitespace
   differences in the anchor do not fail the match),
3. a missing anchor is rejected with the current text surfaced for retry,
   and nothing is staged,
4. an ambiguous anchor (multiple matches) is rejected,
5. full replacement is rejected in knowledge mode and allowed in training
   mode,
6. live instruction text never changes before promotion (edits are staged).

The generality-gate LLM is absent in these tests, so the gate fails open —
gate behavior itself is covered in test_instruction_overfit.py.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"anchor_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _run_edit(tool_input, *, user_id, org_id, mode="knowledge"):
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": mode,
        }
        end = None
        async for evt in EditInstructionTool().run_stream(tool_input, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None, "expected a tool.end event"
        return end.payload["output"], end.payload.get("observation") or {}


def _ai_suggestions(test_client, iid, token, org_id):
    review = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=_auth(token, org_id)
    ).json()
    return [s for s in review["suggestions"] if s["source"] == "ai"]


ORIGINAL = "Revenue excludes cancelled orders. Use net amounts for all revenue metrics."


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_append_preserves_existing_text(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    addition = "Refunded orders are excluded from revenue as well."
    output, _ = await _run_edit(
        {"instruction_id": instr["id"], "old_text": "", "text": addition,
         "evidence": "User clarified: refunds never count."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    # Existing content survives verbatim; the addition lands after it.
    assert output["new_text"].startswith(ORIGINAL)
    assert addition in output["new_text"]
    assert output["previous_text"] == ORIGINAL

    # Edit is staged as an AI suggestion; live row untouched until promotion.
    assert _ai_suggestions(test_client, instr["id"], token, org_id)
    live = test_client.get(
        f"/api/instructions/{instr['id']}", headers=_auth(token, org_id)
    ).json()
    assert live["text"] == ORIGINAL


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_anchored_replace_changes_only_the_snippet(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    output, _ = await _run_edit(
        {
            "instruction_id": instr["id"],
            "old_text": "excludes cancelled orders",
            "text": "excludes cancelled and refunded orders",
            "evidence": "User corrected: refunded orders excluded too.",
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    assert output["new_text"] == (
        "Revenue excludes cancelled and refunded orders. "
        "Use net amounts for all revenue metrics."
    )


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_anchor_matches_across_whitespace_differences(
    create_global_instruction, create_user, login_user, whoami
):
    """Instructions are prose; a spacing/newline mismatch in the anchor must
    not fail the edit."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text="Revenue excludes\n  cancelled   orders. Use net amounts.",
        user_token=token, org_id=org_id, status="published",
    )

    output, _ = await _run_edit(
        {
            "instruction_id": instr["id"],
            "old_text": "Revenue excludes cancelled orders.",
            "text": "Revenue excludes cancelled and refunded orders.",
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    assert "cancelled and refunded" in output["new_text"]
    assert "Use net amounts." in output["new_text"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_missing_anchor_rejected_with_current_text_and_nothing_staged(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )

    output, observation = await _run_edit(
        {
            "instruction_id": instr["id"],
            "old_text": "this snippet does not exist anywhere",
            "text": "replacement",
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "anchor_not_found"
    # The current text is surfaced so the planner's retry can anchor on it.
    assert observation.get("current_text") == ORIGINAL
    assert _ai_suggestions(test_client, instr["id"], token, org_id) == []


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_ambiguous_anchor_rejected(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text="Use net revenue. Always report net revenue in dashboards.",
        user_token=token, org_id=org_id, status="published",
    )

    output, _ = await _run_edit(
        {
            "instruction_id": instr["id"],
            "old_text": "net revenue",
            "text": "gross revenue",
        },
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "ambiguous_anchor"
    assert _ai_suggestions(test_client, instr["id"], token, org_id) == []


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_full_replace_rejected_in_knowledge_mode_allowed_in_training(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published"
    )
    rewrite = "Completely new instruction text that drops everything else."

    # Knowledge mode (autonomous harness): rejected, nothing staged.
    output, observation = await _run_edit(
        {"instruction_id": instr["id"], "text": rewrite},
        user_id=user_id, org_id=org_id, mode="knowledge",
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "full_replace_not_allowed"
    assert observation.get("current_text") == ORIGINAL
    assert _ai_suggestions(test_client, instr["id"], token, org_id) == []

    # Training mode (human in the loop): full replace still works.
    output, _ = await _run_edit(
        {"instruction_id": instr["id"], "text": rewrite},
        user_id=user_id, org_id=org_id, mode="training",
    )
    assert output["success"] is True, output
    assert output["new_text"] == rewrite
