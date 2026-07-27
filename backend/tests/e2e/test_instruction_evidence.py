"""E2E tests for AI-suggestion evidence.

The knowledge harness (agent v2, mode="knowledge") and training mode both
capture instructions through the create_instruction / edit_instruction tools.
Each tool call carries a brief `evidence` sentence; the contract under test:

1. evidence is persisted on the staged (proposed) instruction version,
2. for EDIT suggestions (a diff against a live instruction), GET
   /instructions/{id}/review-hunks returns it per suggestion, so the Knowledge
   Explorer renders it in the per-hunk provenance card,
3. for NEW AI instructions (no diff — the whole text is the suggestion), GET
   /instructions/{id} returns it on the detail, next to source + author,
4. over-long evidence is clamped, never rejected (a tool call must not fail
   because the model wrote a paragraph).

Tools are exercised directly via run_stream against the real test DB — same
pattern as test_read_instruction_tool.py.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"evidence_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _run_tool(tool, tool_input, *, user_id, org_id, extra_ctx=None):
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": "knowledge",
            **(extra_ctx or {}),
        }
        end = None
        async for evt in tool.run_stream(tool_input, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None, "expected a tool.end event"
        return end.payload["output"], ctx


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_create_instruction_evidence_surfaces_on_detail(
    test_client, create_user, login_user, whoami
):
    """A NEW AI instruction has no diff against main (the whole text is the
    suggestion), so its evidence must surface on the instruction detail —
    the Knowledge Explorer shows it next to the source + author row."""
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    evidence = "inspect_data: orders.status includes cancelled/refunded."

    output, _ = await _run_tool(
        CreateInstructionTool(),
        {
            "text": "Exclude cancelled and refunded orders from revenue calculations.",
            "title": "Revenue exclusions",
            "category": "code_gen",
            "confidence": 0.9,
            "evidence": evidence,
            "load_mode": "intelligent",
        },
        user_id=user_id,
        org_id=org_id,
    )
    assert output["success"] is True, output
    iid = output["instruction_id"]

    detail = test_client.get(
        f"/api/instructions/{iid}", headers=_auth(token, org_id)
    ).json()
    assert detail["evidence"] == evidence


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_edit_instruction_evidence_surfaces_in_review_hunks(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """An AI edit_instruction call stamps its evidence on the staged version;
    review-hunks returns it for that suggestion. A published instruction with
    no AI activity has no evidence to show."""
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text="Count customers with COUNT DISTINCT customer_id.",
        user_token=token, org_id=org_id, status="published",
    )
    iid = instr["id"]

    # Baseline: no pending suggestions, and a user-authored instruction
    # carries no evidence.
    review = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=_auth(token, org_id)
    ).json()
    assert review["suggestions"] == []
    detail = test_client.get(
        f"/api/instructions/{iid}", headers=_auth(token, org_id)
    ).json()
    assert detail["evidence"] is None

    evidence = "User confirmed: only active accounts count as customers."
    output, _ = await _run_tool(
        EditInstructionTool(),
        {
            "instruction_id": iid,
            "text": "Count customers with COUNT DISTINCT customer_id, active accounts only.",
            "evidence": evidence,
        },
        user_id=user_id,
        org_id=org_id,
    )
    assert output["success"] is True, output

    review = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=_auth(token, org_id)
    ).json()
    ai_suggestions = [s for s in review["suggestions"] if s["source"] == "ai"]
    assert ai_suggestions, "AI edit should surface a pending suggestion"
    assert ai_suggestions[0]["evidence"] == evidence
    assert ai_suggestions[0]["hunks"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_overlong_evidence_is_clamped_not_rejected(
    test_client, create_user, login_user, whoami
):
    """Evidence longer than the display budget must not fail the tool call —
    it is clamped to a brief string and still surfaced."""
    from app.ai.tools.implementations.create_instruction import (
        CreateInstructionTool,
        MAX_EVIDENCE_LENGTH,
    )

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    long_evidence = "Observed while exploring the schemas: " + "x" * 400

    output, _ = await _run_tool(
        CreateInstructionTool(),
        {
            "text": "Amounts in the transactions table are stored in cents; divide by 100 for display.",
            "category": "code_gen",
            "confidence": 0.85,
            "evidence": long_evidence,
        },
        user_id=user_id,
        org_id=org_id,
    )
    assert output["success"] is True, output
    iid = output["instruction_id"]

    detail = test_client.get(
        f"/api/instructions/{iid}", headers=_auth(token, org_id)
    ).json()
    ev = detail["evidence"]
    assert ev is not None
    assert len(ev) <= MAX_EVIDENCE_LENGTH
    assert ev.startswith("Observed while exploring")


def test_clamp_evidence_normalizes_blank_and_long_values():
    """Pure contract of the clamp helper: blank -> None, short -> unchanged,
    long -> bounded."""
    from app.ai.tools.implementations.create_instruction import (
        clamp_evidence,
        MAX_EVIDENCE_LENGTH,
    )

    assert clamp_evidence(None) is None
    assert clamp_evidence("   ") is None
    assert clamp_evidence("inspect_data: cents.") == "inspect_data: cents."
    clamped = clamp_evidence("y" * (MAX_EVIDENCE_LENGTH * 2))
    assert len(clamped) <= MAX_EVIDENCE_LENGTH
    assert clamped.endswith("…")
