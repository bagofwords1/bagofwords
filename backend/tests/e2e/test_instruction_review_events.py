"""E2E: accepting/rejecting a suggestion reports the verdict back to the
conversation that produced it.

An AI suggestion is authored inside a report, but it is reviewed somewhere else
entirely (the Knowledge Explorer). Without a session event the agent never
learns what happened to it — so it re-suggests a rejected rule, and treats an
accepted one as still unconfirmed. ``instruction_accepted`` / ``instruction_
rejected`` close that loop the same way ``llm_changed`` does for a model switch:
a silent ``role='event'`` completion on the originating report, read on the
agent's next natural turn.

Contract:
1. accepting emits instruction_accepted on the suggestion's report,
2. rejecting emits instruction_rejected carrying the rejected text, so the
   "do not re-suggest it" signal is specific,
3. accepting N hunks of one suggestion is ONE verdict, not N notifications,
4. a suggestion with no originating report (user/git build) emits nothing,
5. the events are LLM-visible — they reach the agent's message context.
"""
import uuid
from types import SimpleNamespace

import pytest

from app.ai.context.session_events import (
    EVENT_ROLE,
    INSTRUCTION_ACCEPTED,
    INSTRUCTION_REJECTED,
    is_event_kind_llm_visible,
)

ORIGINAL = "Revenue excludes cancelled orders."
ADDITION = "- Refunded orders are excluded from revenue as well."


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _new_admin(create_user, login_user, whoami):
    email = f"revev_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email=email, password="test123")
    me = whoami(token)
    return token, me["id"], me["organizations"][0]["id"]


async def _report_with_execution(org_id, user_id):
    """A report plus the AgentExecution a suggestion would be authored under."""
    from app.dependencies import async_session_maker
    from app.models.report import Report
    from app.models.completion import Completion
    from app.models.agent_execution import AgentExecution

    async with async_session_maker() as db:
        report = Report(title="Training session", slug=f"train-{uuid.uuid4().hex[:8]}",
                        organization_id=str(org_id), user_id=str(user_id))
        db.add(report)
        await db.commit()
        await db.refresh(report)

        # A COMPLETE turn — user ask plus the assistant reply. The context
        # builder drops a trailing user message as an in-progress turn, so a
        # half-turn fixture renders as "No conversation history available".
        db.add(Completion(
            prompt={"content": "teach me"}, completion={"content": ""},
            report_id=str(report.id), role="user", status="success",
            turn_index=1, user_id=str(user_id),
        ))
        completion = Completion(
            prompt={"content": ""}, completion={"content": "noted"},
            report_id=str(report.id), role="system", status="success",
            turn_index=1, user_id=str(user_id),
        )
        db.add(completion)
        await db.commit()
        await db.refresh(completion)

        execution = AgentExecution(
            report_id=str(report.id), completion_id=str(completion.id),
            organization_id=str(org_id), status="success",
        )
        db.add(execution)
        await db.commit()
        await db.refresh(execution)
        return str(report.id), str(execution.id)


async def _stage_suggestion(instruction_id, org_id, user_id, *, execution_id, text):
    """Stage an AI edit into a draft build bound to ``execution_id``."""
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    async with async_session_maker() as db:
        build = await BuildService().get_or_create_draft_build(
            db, org_id=str(org_id), source="ai", user_id=str(user_id),
            agent_execution_id=execution_id,
        )
        build_id = str(build.id)
        ctx = {
            "db": db, "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id), "mode": "training",
            "training_build_id": build_id, "agent_execution_id": execution_id,
        }
        async for evt in EditInstructionTool().run_stream(
            # Anchored append: repeat the anchor verbatim, then add.
            {"instruction_id": instruction_id, "old_text": ORIGINAL,
             "text": f"{ORIGINAL}\n{text}", "evidence": "User confirmed."}, ctx,
        ):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")
        return build_id


async def _events(report_id, kind=None):
    from app.dependencies import async_session_maker
    from sqlalchemy import select
    from app.models.completion import Completion

    async with async_session_maker() as db:
        rows = (await db.execute(
            select(Completion)
            .where(Completion.report_id == str(report_id),
                   Completion.role == EVENT_ROLE)
            .order_by(Completion.created_at)
        )).scalars().all()
    return [r for r in rows if kind is None or r.message_type == kind]


def _review(test_client, iid, token, org_id):
    r = test_client.get(f"/api/instructions/{iid}/review-hunks",
                        headers=_auth(token, org_id))
    assert r.status_code == 200, r.text
    return r.json()


def _payload(review, build_id=None):
    suggestions = review["suggestions"]
    if build_id is not None:
        suggestions = [s for s in suggestions if str(s["build_id"]) == str(build_id)]
    return {
        "against_main_build_id": review["main_build_id"],
        "against_main_version_id": review["main_version_id"],
        "hunks": [
            {"build_id": s["build_id"], "hunk_key": h["key"]}
            for s in suggestions for h in s["hunks"]
        ],
    }


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_accept_emits_event_on_the_originating_report(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]
    report_id, exec_id = await _report_with_execution(org_id, user_id)
    await _stage_suggestion(iid, org_id, user_id, execution_id=exec_id, text=ADDITION)

    review = _review(test_client, iid, token, org_id)
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        headers=_auth(token, org_id),
        json=_payload(review),
    )
    assert r.status_code == 200, r.text

    events = await _events(report_id, INSTRUCTION_ACCEPTED)
    assert len(events) == 1, f"expected exactly one verdict event, got {len(events)}"
    ev = events[0]
    meta = ev.prompt["meta"]
    assert meta["instruction_id"] == iid
    assert "accepted" in ev.prompt["content"].lower()
    # An instruction can carry several suggestions, so the verdict must name
    # exactly which one it settled — build, proposed version, and hunks.
    assert meta["build_id"], meta
    assert meta["version_id"], meta
    assert meta["version_number"], meta
    assert meta["hunk_keys"] and meta["count"] == len(meta["hunk_keys"]), meta
    assert f"v{meta['version_number']}" in ev.prompt["content"], ev.prompt["content"]
    # Silent: an event must not start a turn.
    assert ev.status == "success" and ev.completion.get("content") == ""


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_reject_emits_event_carrying_the_rejected_text(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]
    report_id, exec_id = await _report_with_execution(org_id, user_id)
    await _stage_suggestion(iid, org_id, user_id, execution_id=exec_id, text=ADDITION)

    review = _review(test_client, iid, token, org_id)
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        headers=_auth(token, org_id), json=_payload(review),
    )
    assert r.status_code == 200, r.text

    events = await _events(report_id, INSTRUCTION_REJECTED)
    assert len(events) == 1, f"expected one rejection event, got {len(events)}"
    content = events[0].prompt["content"]
    assert "do not re-suggest" in content.lower()
    # The signal has to be specific, not just "something was rejected".
    assert "Refunded orders" in content, content


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_multi_hunk_accept_is_one_verdict_not_many(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]
    report_id, exec_id = await _report_with_execution(org_id, user_id)
    # Several separate additions -> several hunks in one suggestion.
    await _stage_suggestion(
        iid, org_id, user_id, execution_id=exec_id,
        text="- One.\n- Two.\n- Three.\n- Four.",
    )
    review = _review(test_client, iid, token, org_id)
    assert review["suggestions"], "expected a pending suggestion"

    r = test_client.post(
        f"/api/instructions/{iid}/hunks/accept-all",
        headers=_auth(token, org_id),
        json=_payload(review),
    )
    assert r.status_code == 200, r.text
    events = await _events(report_id, INSTRUCTION_ACCEPTED)
    assert len(events) == 1, f"one verdict per suggestion, got {len(events)}"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_suggestion_without_a_report_emits_nothing(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """A user- or git-authored build has no conversation to inform; reviewing it
    must not manufacture one."""
    from app.dependencies import async_session_maker
    from app.services.build_service import BuildService
    from app.ai.tools.implementations.edit_instruction import EditInstructionTool

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]
    report_id, _exec_id = await _report_with_execution(org_id, user_id)

    async with async_session_maker() as db:
        build = await BuildService().get_or_create_draft_build(
            db, org_id=str(org_id), source="user", user_id=str(user_id),
        )
        ctx = {"db": db, "user": SimpleNamespace(id=user_id),
               "organization": SimpleNamespace(id=org_id), "mode": "training",
               "training_build_id": str(build.id)}
        async for evt in EditInstructionTool().run_stream(
            {"instruction_id": iid, "old_text": ORIGINAL,
             "text": f"{ORIGINAL}\n{ADDITION}", "evidence": "Manual."}, ctx,
        ):
            if evt.type == "tool.error":
                pytest.fail(f"tool errored: {evt.payload}")

    review = _review(test_client, iid, token, org_id)
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        headers=_auth(token, org_id), json=_payload(review),
    )
    assert r.status_code == 200, r.text
    assert await _events(report_id) == [], "no report to notify — expected silence"


def test_verdict_kinds_reach_the_agent_context_and_the_timeline():
    """The whole point is context: these must not be LLM-hidden. They are also
    UI-visible — the verdict is reached outside the conversation, so the strip
    is the only place a reader of the report learns about it. The frontend
    mirrors EVENT_UI_VISIBLE by hand (pages/reports/[id]/index.vue), so this
    pins the backend half of that contract."""
    from app.ai.context.session_events import is_event_kind_ui_visible
    assert is_event_kind_llm_visible(INSTRUCTION_ACCEPTED)
    assert is_event_kind_llm_visible(INSTRUCTION_REJECTED)
    assert is_event_kind_ui_visible(INSTRUCTION_ACCEPTED)
    assert is_event_kind_ui_visible(INSTRUCTION_REJECTED)


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_two_suggestions_produce_distinguishable_verdicts(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """Two pending suggestions on one instruction, resolved differently: each
    verdict must name its own build and version, or the agent cannot tell which
    of its proposals was rejected."""
    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]
    report_id, exec_a = await _report_with_execution(org_id, user_id)
    build_a = await _stage_suggestion(
        iid, org_id, user_id, execution_id=exec_a, text="- Alpha rule.")

    # A second turn in the SAME conversation — its own execution, its own build.
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution
    from app.models.completion import Completion
    async with async_session_maker() as db:
        db.add(Completion(prompt={"content": "and another"}, completion={"content": ""},
                          report_id=report_id, role="user", status="success",
                          turn_index=2, user_id=str(user_id)))
        c_b = Completion(prompt={"content": ""}, completion={"content": "ok"},
                         report_id=report_id, role="system", status="success",
                         turn_index=2, user_id=str(user_id))
        db.add(c_b)
        await db.commit()
        await db.refresh(c_b)
        ex_b = AgentExecution(report_id=report_id, completion_id=str(c_b.id),
                              organization_id=str(org_id), status="success")
        db.add(ex_b)
        await db.commit()
        await db.refresh(ex_b)
        exec_b = str(ex_b.id)
    build_b = await _stage_suggestion(
        iid, org_id, user_id, execution_id=exec_b, text="- Beta rule.")
    assert build_a != build_b

    review = _review(test_client, iid, token, org_id)
    assert len(review["suggestions"]) == 2, review

    # Reject only suggestion A.
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        headers=_auth(token, org_id), json=_payload(review, build_a),
    )
    assert r.status_code == 200, r.text

    events = await _events(report_id, INSTRUCTION_REJECTED)
    assert len(events) == 1, f"expected one rejection, got {len(events)}"
    meta = events[0].prompt["meta"]
    assert meta["build_id"] == build_a, meta
    assert "Alpha" in events[0].prompt["content"], events[0].prompt["content"]
    assert "Beta" not in events[0].prompt["content"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_verdict_event_renders_into_the_agent_message_context(
    test_client, create_global_instruction, create_user, login_user, whoami
):
    """The DB row is not the deliverable — the agent has to SEE it. Build the
    real message context for the report and assert the verdict is in it."""
    from app.dependencies import async_session_maker
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.organization import Organization
    from app.models.report import Report
    from app.ai.context.builders.message_context_builder import MessageContextBuilder

    token, user_id, org_id = _new_admin(create_user, login_user, whoami)
    instr = create_global_instruction(
        text=ORIGINAL, user_token=token, org_id=org_id, status="published")
    iid = instr["id"]
    report_id, exec_id = await _report_with_execution(org_id, user_id)
    await _stage_suggestion(iid, org_id, user_id, execution_id=exec_id, text=ADDITION)

    review = _review(test_client, iid, token, org_id)
    r = test_client.post(
        f"/api/instructions/{iid}/hunks/reject-all",
        headers=_auth(token, org_id), json=_payload(review),
    )
    assert r.status_code == 200, r.text

    async with async_session_maker() as db:
        org = (await db.execute(
            select(Organization).where(Organization.id == str(org_id))
        )).scalars().first()
        report = (await db.execute(
            select(Report).options(
                selectinload(Report.files), selectinload(Report.data_sources)
            ).where(Report.id == report_id)
        )).scalars().first()
        text = await MessageContextBuilder(db, org, report).build_context(max_messages=20)

    assert "do not re-suggest" in text.lower(), text[-1500:]
    assert "Refunded orders" in text, text[-1500:]
