"""E2E: the training Summary reports each instruction's real review state.

Every training message runs as its own agent execution and stages into its own
draft build, and each draft starts as a copy of ALL of main. Accepting a change
publishes a NEW build and leaves the draft's rows in place. So "is the
instruction in the newest draft" answers nothing about review state — the
Summary used to treat it as the answer and showed an unreviewed create from an
earlier message as Accepted, and an accepted edit as Pending, indefinitely.

The tools run for real (no LLM); only the conversation rows that record the
calls (report, completions, agent executions, tool executions) are written
directly, because no API produces agent tool calls without a model.
"""
import uuid
from types import SimpleNamespace

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"sumstate_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _new_training_report(user_id, org_id):
    from app.dependencies import async_session_maker
    from app.models.report import Report

    async with async_session_maker() as db:
        report = Report(
            title="Training session", slug=f"train-{uuid.uuid4().hex[:8]}",
            status="active", mode="training", user_id=user_id, organization_id=org_id,
        )
        db.add(report)
        await db.commit()
        return str(report.id)


async def _run_message(report_id, tool, tool_input, *, user_id, org_id):
    """One training message: its own completion + agent execution, one tool
    call, recorded the way the agent records it."""
    from app.dependencies import async_session_maker
    from app.models.completion import Completion
    from app.models.completion_block import CompletionBlock
    from app.models.tool_execution import ToolExecution
    from app.models.agent_execution import AgentExecution

    async with async_session_maker() as db:
        completion = Completion(
            prompt={"content": "teach"}, completion={}, status="success",
            role="ai_system", message_type="ai_completion", report_id=report_id,
            user_id=user_id,
        )
        db.add(completion)
        await db.flush()
        ae = AgentExecution(
            completion_id=completion.id, organization_id=org_id, user_id=user_id,
            report_id=report_id, status="success",
        )
        db.add(ae)
        await db.commit()
        completion_id, ae_id = str(completion.id), str(ae.id)

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
            "mode": "training",
            "agent_execution_id": ae_id,
        }
        output = None
        async for evt in tool.run_stream(tool_input, ctx):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
            if evt.type == "tool.end":
                output = evt.payload["output"]
        assert output and output.get("success"), output

    async with async_session_maker() as db:
        te = ToolExecution(
            agent_execution_id=ae_id, tool_name=tool.name, status="success",
            success=True, arguments_json=dict(tool_input), result_json=output,
        )
        db.add(te)
        await db.flush()
        db.add(CompletionBlock(
            completion_id=completion_id, agent_execution_id=ae_id,
            source_type="tool", tool_execution_id=te.id, block_index=0,
            title=tool.name, status="complete",
        ))
        await db.commit()
    return output


def _states(test_client, report_id, token, org_id):
    r = test_client.get(f"/api/reports/{report_id}/summary", headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    return {i["instruction_id"]: i["state"] for i in r.json()["instructions"]}


def _resolve_all(test_client, iid, action, token, org_id):
    review = test_client.get(
        f"/api/instructions/{iid}/review-hunks", headers=_auth(token, org_id)
    ).json()
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/{action}-all",
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


async def _create_and_edit(create_first, report_id, existing_id, *, user_id, org_id):
    """A new instruction and an edit to an existing one, in separate messages."""
    from app.ai.tools.implementations.create_instruction import CreateInstructionTool
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    async def create():
        return await _run_message(
            report_id, CreateInstructionTool(),
            {"text": f"Join invoice lines to invoices by invoice_id ({uuid.uuid4().hex[:6]}).",
             "category": "code_gen", "confidence": 0.9, "evidence": "User said so."},
            user_id=user_id, org_id=org_id,
        )

    async def edit():
        return await _run_message(
            report_id, EditInstructionTool(),
            {"instruction_id": existing_id,
             "old_text": "Use net amounts for all metrics.",
             "text": "Use net amounts (excluding VAT) for all metrics.",
             "evidence": "User clarified."},
            user_id=user_id, org_id=org_id,
        )

    if create_first:
        created = await create()
        await edit()
    else:
        await edit()
        created = await create()
    return created["instruction_id"]


EXISTING = "Revenue excludes cancelled orders. Use net amounts for all metrics."


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("create_first", [True, False], ids=["create-then-edit", "edit-then-create"])
async def test_summary_state_tracks_review_across_messages(
    create_first, test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    existing = create_global_instruction(
        text=EXISTING, user_token=token, org_id=org_id, status="published")["id"]
    report_id = await _new_training_report(user_id, org_id)
    new_id = await _create_and_edit(create_first, report_id, existing, user_id=user_id, org_id=org_id)

    # Nothing reviewed yet: both are pending, whichever message staged them.
    assert _states(test_client, report_id, token, org_id) == {new_id: "pending", existing: "pending"}

    # Accept the edit: it is live, so it is accepted; the create is untouched.
    _resolve_all(test_client, existing, "accept", token, org_id)
    assert _states(test_client, report_id, token, org_id) == {new_id: "pending", existing: "accepted"}

    # Reject the create (deletes the never-published instruction): it stays in
    # the list, as rejected — never as accepted.
    r = test_client.delete(f"/api/instructions/{new_id}", headers=_auth(token, org_id))
    assert r.status_code in (200, 204), r.text
    assert _states(test_client, report_id, token, org_id) == {new_id: "rejected", existing: "accepted"}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_summary_state_rejected_edit_and_accepted_create(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    existing = create_global_instruction(
        text=EXISTING, user_token=token, org_id=org_id, status="published")["id"]
    report_id = await _new_training_report(user_id, org_id)
    new_id = await _create_and_edit(True, report_id, existing, user_id=user_id, org_id=org_id)

    _resolve_all(test_client, existing, "reject", token, org_id)
    # A create is accepted the way its chat card does it.
    build_id = test_client.get(
        f"/api/reports/{report_id}/summary", headers=_auth(token, org_id)
    ).json()["instructions"]
    build_id = next(i["build_id"] for i in build_id if i["instruction_id"] == new_id)
    r = test_client.post(
        f"/api/instructions/{new_id}/accept-staged",
        json={"build_id": build_id}, headers=_auth(token, org_id),
    )
    assert r.status_code == 200, r.text

    assert _states(test_client, report_id, token, org_id) == {new_id: "accepted", existing: "rejected"}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_summary_lists_only_this_conversations_instructions(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """A training draft carries every live instruction; only the ones this
    conversation's tools touched belong in its Summary."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    existing = create_global_instruction(
        text=EXISTING, user_token=token, org_id=org_id, status="published")["id"]
    bystander = create_global_instruction(
        text="Fiscal year starts in April.", user_token=token, org_id=org_id,
        status="published")["id"]
    report_id = await _new_training_report(user_id, org_id)
    new_id = await _create_and_edit(True, report_id, existing, user_id=user_id, org_id=org_id)

    states = _states(test_client, report_id, token, org_id)
    assert set(states) == {new_id, existing}
    assert bystander not in states
