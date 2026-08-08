"""E2E: the review verdict is reported, not inferred — and tools return the
text they stored.

Two related gaps, both "the answer was recorded and the consumer re-derived it":

1. The tool block decided Accepted/Rejected by comparing the live instruction
   text to the proposal, calling any mismatch a rejection. That is three states
   collapsed into one label (rejected / not yet reviewed / accepted-then-main-
   moved-on), and it defaulted to "rejected" on a failed fetch.
   GET /instructions/{id}/builds/{bid}/verdict answers from the recorded
   per-hunk actions instead.

2. Neither tool returned the resulting text, so after its first edit a model had
   nothing accurate to anchor the next one on: the instructions context renders
   the LIVE row, while a second edit in the same build applies to the PENDING
   version. Both tools now put the stored text in their success observation.
"""
import uuid
from types import SimpleNamespace

import pytest


ORIGINAL = "Revenue excludes cancelled orders. Use net amounts for all metrics."


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"verdict_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _run_tool(tool, tool_input, *, user_id, org_id, mode="training"):
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": mode,
        }
        end = None
        async for evt in tool.run_stream(tool_input, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                end = evt
        assert end is not None
        return end.payload["output"], end.payload.get("observation") or {}


def _verdict(test_client, iid, bid, token, org_id):
    r = test_client.get(f"/api/instructions/{iid}/builds/{bid}/verdict",
                        headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_edit_observation_returns_the_resulting_text(
    create_global_instruction, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")

    output, observation = await _run_tool(
        EditInstructionTool(),
        {"instruction_id": instr["id"],
         "old_text": "Use net amounts for all metrics.",
         "text": "Use net amounts (excluding VAT) for all metrics.",
         "evidence": "User clarified."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    # What the model sees next turn must be the WHOLE resulting instruction,
    # not the snippet it passed in — that is what the next anchor matches on.
    assert observation["new_text"] == output["new_text"]
    assert "excluding VAT" in observation["new_text"]
    assert observation["new_text"].startswith("Revenue excludes cancelled orders.")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_a_second_edit_can_anchor_on_the_returned_text(
    create_global_instruction, create_user, login_user, whoami
):
    """The point of returning it: the follow-up edit anchors on text the model
    was handed, rather than on the pre-edit live row it can still see."""
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")

    from tests.utils.real_execution import make_agent_execution
    exec_id = await make_agent_execution(org_id, user_id)
    async with async_session_maker() as db:
        build = await BuildService().get_or_create_draft_build(
            db, org_id=str(org_id), source="ai", user_id=str(user_id),
            agent_execution_id=exec_id)
        build_id = str(build.id)

    async def edit(tool_input):
        from app.dependencies import async_session_maker as maker
        async with maker() as db:
            ctx = {"db": db, "user": SimpleNamespace(id=user_id),
                   "organization": SimpleNamespace(id=org_id), "mode": "training",
                   "training_build_id": build_id}
            end = None
            async for evt in EditInstructionTool().run_stream(tool_input, ctx):
                if evt.type == "tool.error":
                    pytest.fail(f"tool errored: {evt.payload}")
                if evt.type == "tool.end":
                    end = evt
            return end.payload["output"], end.payload["observation"]

    _out1, obs1 = await edit({
        "instruction_id": instr["id"],
        "old_text": "Use net amounts for all metrics.",
        "text": "Use net amounts (excluding VAT) for all metrics.",
        "evidence": "User clarified."})

    # Anchor the SECOND edit on a snippet that only exists in the first edit's
    # result. Against the live row (unchanged until promotion) this would miss.
    out2, _ = await edit({
        "instruction_id": instr["id"],
        "old_text": "(excluding VAT)",
        "text": "(excluding VAT and refunds)",
        "evidence": "User clarified further."})
    assert out2["success"] is True, out2
    assert "excluding VAT and refunds" in out2["new_text"]
    assert "excluding VAT)" in obs1["new_text"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_create_returns_the_stored_text(
    create_user, login_user, whoami
):
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    text = "Exclude internal test accounts from every customer metric."
    output, observation = await _run_tool(
        CreateInstructionTool(),
        {"text": text, "category": "general", "confidence": 0.9,
         "evidence": "User confirmed."},
        user_id=user_id, org_id=org_id,
    )
    assert output["success"] is True, output
    assert output["text"] == text, "the block renders this, not the raw input"
    assert observation["new_text"] == text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_verdict_reports_pending_accepted_and_rejected(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]

    output, _ = await _run_tool(
        EditInstructionTool(),
        {"instruction_id": iid,
         "old_text": "Use net amounts for all metrics.",
         "text": "Use net amounts (excluding VAT) for all metrics.",
         "evidence": "User clarified."},
        user_id=user_id, org_id=org_id,
    )
    bid = output["build_id"]

    # Unreviewed is PENDING — the old inference called this "rejected", which
    # is the bug that started this.
    assert _verdict(test_client, iid, bid, token, org_id)["status"] == "pending"

    review = test_client.get(f"/api/instructions/{iid}/review-hunks",
                             headers=_auth(token, org_id)).json()
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        headers=_auth(token, org_id),
        json={
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
            "hunks": [
                {"build_id": s["build_id"], "hunk_key": h["key"]}
                for s in review["suggestions"] for h in s["hunks"]
            ],
        },
    )
    assert r.status_code == 200, r.text

    v = _verdict(test_client, iid, bid, token, org_id)
    assert v["status"] == "accepted", v
    assert v["accepted_hunks"] >= 1


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_verdict_reports_rejected_after_a_rejection(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]

    output, _ = await _run_tool(
        EditInstructionTool(),
        {"instruction_id": iid,
         "old_text": "Use net amounts for all metrics.",
         "text": "Use gross amounts for all metrics.",
         "evidence": "User clarified."},
        user_id=user_id, org_id=org_id,
    )
    bid = output["build_id"]

    review = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=_auth(token, org_id)
    ).json()
    suggestion = next(s for s in review["suggestions"] if s["build_id"] == bid)
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        headers=_auth(token, org_id),
        json={
            "against_main_build_id": review["main_build_id"],
            "against_main_version_id": review["main_version_id"],
            "hunks": [
                {"build_id": bid, "hunk_key": h["key"]}
                for h in suggestion["hunks"]
            ],
        },
    )
    assert r.status_code == 200, r.text

    v = _verdict(test_client, iid, bid, token, org_id)
    assert v["status"] == "rejected", v
    assert v["rejected_hunks"] >= 1
