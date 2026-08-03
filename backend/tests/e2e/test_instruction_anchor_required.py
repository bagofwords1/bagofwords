"""E2E: the edit anchor is mandatory, and rewriting is a deliberate act.

`edit_instruction` used to accept `text` with no `old_text` and treat it as
"replace the whole instruction" — meaning the call with the FEWEST arguments
was the most destructive one. Every mainstream edit tool does the opposite
(Claude Code's Edit requires `old_string`; aider requires both halves of a
SEARCH/REPLACE block; unified diffs carry context on both sides), and puts
whole-file replacement behind a separately named act.

Observed cost of the old shape: asked to add one PDF-only rule, a model omitted
`old_text` and rewrote every bullet plus category, load_mode and confidence —
37 changes for a one-line request — because one call that always applies beat
several anchored calls that can miss.

Contract:
1. `text` without `old_text` is rejected (anchor_required); nothing is staged,
2. the rejection tells the model how to retry AND surfaces the current text,
3. anchored replace and append still work,
4. `replace_entire_text: true` still allows a deliberate rewrite in training,
5. knowledge mode refuses it even with the flag,
6. metadata-only edits (no `text`) need no anchor.
"""
import uuid
from types import SimpleNamespace

import pytest


ORIGINAL = (
    "Revenue excludes cancelled orders.\n"
    "- Use net amounts for all revenue metrics.\n"
    "- Attribute sales to reps via the support rep foreign key."
)


def _new_admin(create_user, login_user, whoami):
    email = f"anchor_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _run_edit(tool_input, *, user_id, org_id, mode="training"):
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


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_text_without_anchor_is_rejected(
    create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")

    output, observation = await _run_edit(
        {"instruction_id": instr["id"],
         "text": "Treat this as a PDF-only source. Only read PDF files.",
         "evidence": "User asked for PDFs only."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "anchor_required", output
    # Actionable: how to retry, and what the text currently says.
    assert "old_text" in output["message"]
    assert "replace_entire_text" in output["message"]
    assert observation.get("current_text") == ORIGINAL
    # Nothing staged — the rewrite never happened.
    assert output.get("version_number") is None
    assert output.get("new_text") is None


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_anchored_replace_and_append_still_work(
    create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")

    output, _ = await _run_edit(
        {"instruction_id": instr["id"],
         "old_text": "Use net amounts for all revenue metrics.",
         "text": "Use net amounts (excluding VAT) for all revenue metrics.",
         "evidence": "User clarified net excludes VAT."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    assert "excluding VAT" in output["new_text"]
    # Untouched sentences stay untouched — that is the whole point.
    assert "Attribute sales to reps via the support rep foreign key." in output["new_text"]
    assert "Revenue excludes cancelled orders." in output["new_text"]

    # Anchored append — the only way to add. "" is not an anchor.
    tail = "- Attribute sales to reps via the support rep foreign key."
    output, _ = await _run_edit(
        {"instruction_id": instr["id"], "old_text": tail,
         "text": f"{tail}\n- Refunded orders are excluded as well.",
         "evidence": "User clarified refunds."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    assert "Refunded orders are excluded as well." in output["new_text"]
    assert output["new_text"].startswith(ORIGINAL.split("\n")[0])


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_explicit_flag_allows_a_deliberate_rewrite_in_training(
    create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")

    rewritten = "Revenue is recognised on delivery, net of VAT and refunds."
    output, _ = await _run_edit(
        {"instruction_id": instr["id"], "text": rewritten,
         "replace_entire_text": True,
         "evidence": "User asked to rewrite this instruction."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    assert output["new_text"] == rewritten


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_knowledge_mode_refuses_the_flag(
    create_global_instruction, create_user, login_user, whoami
):
    """The harness edits with no human in the loop at author time, so it must
    not be able to discard curated content even deliberately."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")

    output, _ = await _run_edit(
        {"instruction_id": instr["id"], "text": "Something entirely different.",
         "replace_entire_text": True, "evidence": "Harness rewrite."},
        user_id=user_id, org_id=org_id, mode="knowledge",
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "full_replace_not_allowed", output


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_metadata_only_edit_needs_no_anchor(
    create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")

    output, _ = await _run_edit(
        {"instruction_id": instr["id"], "confidence": 0.95,
         "evidence": "User confirmed the rule."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_empty_anchor_is_not_an_anchor(
    create_global_instruction, create_user, login_user, whoami
):
    """`old_text: ""` used to mean "append". It was the last sentinel overload
    on this parameter — one field meaning three operations depending on whether
    it was a real string, empty, or absent — and standard edit tools have no
    such convention. One rule now: the anchor is real text."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")

    output, observation = await _run_edit(
        {"instruction_id": instr["id"], "old_text": "",
         "text": "- Refunded orders are excluded as well.",
         "evidence": "User clarified refunds."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is False
    assert output["rejected_reason"] == "anchor_required", output
    # Tells the model how to append instead of just saying no.
    assert "anchor the last sentence" in output["message"]
    assert observation.get("current_text") == ORIGINAL
