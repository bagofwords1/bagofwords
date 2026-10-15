"""Durable parent/child messaging for delegated sub-agents.

The tests create real reports through the fixtures and real pending child rows
through ``record_delegation``. Only the LLM boundary would be stubbed for a
full agent run; these service-contract checks do not need a provider.
"""
from __future__ import annotations

import asyncio
import re
import threading
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from tests.e2e.test_sub_agents import _all_text, _env, _wait_for


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def live_client():
    """Keep app background tasks on one event loop for a complete message flow."""
    from fastapi.testclient import TestClient

    from main import app

    with TestClient(app) as client:
        yield client


def _scripted_message_flow(*, milestones):
    """Stub only the provider boundary; app tools, waits, queue, and DB stay real."""
    from app.ai.llm.types import (
        MessageStopEvent,
        TextDeltaEvent,
        ToolUseCompleteEvent,
        ToolUseStartEvent,
        UsageEvent,
    )

    child_call_counts = {}
    parent_answer_emitted = False

    def _tool(name, args):
        call_id = f"tool_{uuid.uuid4().hex[:12]}"
        return [
            ToolUseStartEvent(id=call_id, name=name),
            ToolUseCompleteEvent(id=call_id, name=name, input=args),
        ]

    def _end(reason="end_turn"):
        return [
            MessageStopEvent(stop_reason=reason, raw_stop_reason=reason),
            UsageEvent(input_tokens=64, output_tokens=24),
        ]

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        nonlocal parent_answer_emitted
        system_text = system or ""
        text = _all_text(messages)
        tool_names = {
            getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
            for tool in (tools or [])
        }
        milestones.setdefault("llm_calls", []).append({
            "system": system_text[:180],
            "text": text[-1000:],
            "tools": sorted(name for name in tool_names if name),
        })
        events = []
        if "<sub_agent_results" in text:
            milestones["fan_in_seen"].set()
            events += [TextDeltaEvent(text="The child finished after receiving the date range.")] + _end()
        elif "Mode: Sub-agent" in system_text:
            child_call_counts[system_text] = child_call_counts.get(system_text, 0) + 1
            if 'kind="answer"' in text and "Use the most recent complete 14-day window." in text:
                milestones["child_received_answer"].set()
                events += _tool(
                    "finish_subtask",
                    {
                        "outcome": "done",
                        "confidence": 0.9,
                        "summary": "Used the parent's requested 14-day comparison window.",
                        "findings": [{"claim": "The comparison used the specified 14-day window."}],
                        "ruled_out": [],
                        "evidence": [],
                        "caveats": [],
                        "unknown": None,
                        "report_note_id": None,
                    },
                ) + _end("tool_use")
            else:
                assert "message_sub_agent" in tool_names, (
                    "A child planner must be offered message_sub_agent before it can ask the parent; "
                    f"offered tools were {sorted(name for name in tool_names if name)}"
                )
                milestones["child_asked_question"].set()
                events += _tool(
                    "message_sub_agent",
                    {
                        "recipient": "parent",
                        "kind": "question",
                        "body": "Which date range should I compare?",
                        "reply_to": None,
                    },
                ) + _end("tool_use")
        elif "DELEGATION (sub-agents" in system_text and "delegate" in tool_names:
            # Only the initial parent planner call delegates. Subsequent parent
            # turns receive the question through <agent_messages>.
            if "parent delegation emitted" not in milestones:
                milestones["parent delegation emitted"] = True
                events += _tool(
                    "delegate",
                    {
                        "goal": "Determine whether the recent conversion decline is real and report evidence.",
                        "context": "Use a stable, complete date window.",
                        "title": "Conversion decline check",
                    },
                ) + _end("tool_use")
            elif ('kind="question"' in text and "message_sub_agent" in tool_names
                  and not parent_answer_emitted):
                match = re.search(r'<message ([^>]*kind="question"[^>]*)>', text)
                assert match, text
                attrs = dict(re.findall(r'(message_id|sender|recipient|kind)="([^"]*)"', match.group(1)))
                milestones["parent_received_question"].set()
                parent_answer_emitted = True
                events += _tool(
                    "message_sub_agent",
                    {
                        "recipient": attrs["sender"],
                        "kind": "answer",
                        "body": "Use the most recent complete 14-day window.",
                        "reply_to": attrs["message_id"],
                    },
                ) + _end("tool_use")
            else:
                events += [TextDeltaEvent(text="The delegated check is underway.")] + _end()
        elif ('kind="question"' in text and "message_sub_agent" in tool_names
              and not parent_answer_emitted):
            match = re.search(r'<message ([^>]*kind="question"[^>]*)>', text)
            assert match, text
            attrs = dict(re.findall(r'(message_id|sender|recipient|kind)="([^"]*)"', match.group(1)))
            milestones["parent_received_question"].set()
            parent_answer_emitted = True
            events += _tool(
                "message_sub_agent",
                {
                    "recipient": attrs["sender"],
                    "kind": "answer",
                    "body": "Use the most recent complete 14-day window.",
                    "reply_to": attrs["message_id"],
                },
            ) + _end("tool_use")
        elif "message_sub_agent" in tool_names and milestones.get("parent_received_question"):
            events += [TextDeltaEvent(text="I sent the requested date range to the child.")] + _end()
        else:
            events += [TextDeltaEvent(text="The delegated check is underway.")] + _end()
        for event in events:
            yield event

    return scripted


def _held_parent_message_flow(*, release_parent, milestones):
    """Delegate, hold the live parent while a child asks, then answer after
    the next user prompt has been durably queued and the original turn drains.
    """
    from app.ai.llm.types import (
        MessageStopEvent,
        TextDeltaEvent,
        ToolUseCompleteEvent,
        ToolUseStartEvent,
        UsageEvent,
    )

    delegated = False
    held = False
    answered = False

    def _tool(name, args):
        call_id = f"tool_{uuid.uuid4().hex[:12]}"
        return [ToolUseStartEvent(id=call_id, name=name), ToolUseCompleteEvent(id=call_id, name=name, input=args)]

    def _end(reason="end_turn"):
        return [MessageStopEvent(stop_reason=reason, raw_stop_reason=reason), UsageEvent(input_tokens=64, output_tokens=24)]

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        nonlocal delegated, held, answered
        system_text = system or ""
        text = _all_text(messages)
        tool_names = {
            getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
            for tool in (tools or [])
        }
        tool_names.discard(None)
        milestones.setdefault("llm_calls", []).append({"system": system_text, "text": text, "tools": tool_names})
        events = []
        if "Mode: Sub-agent" in system_text:
            if 'kind="answer"' in text and "Use the most recent complete 14-day window." in text:
                milestones["child_received_answer"].set()
                events += _tool("finish_subtask", {
                    "outcome": "done", "confidence": 0.9,
                    "summary": "Used the parent's requested 14-day comparison window.",
                    "findings": [{"claim": "The comparison used the specified 14-day window."}],
                    "ruled_out": [], "evidence": [], "caveats": [],
                    "unknown": None, "report_note_id": None,
                }) + _end("tool_use")
            else:
                assert "message_sub_agent" in tool_names, (
                    "A child planner must be offered message_sub_agent before it can ask the parent; "
                    f"offered tools were {sorted(tool_names)}"
                )
                milestones["child_asked_question"].set()
                events += _tool("message_sub_agent", {
                    "recipient": "parent", "kind": "question",
                    "body": "Which date range should I compare?", "reply_to": None,
                }) + _end("tool_use")
        elif "<sub_agent_results" in text:
            milestones["fan_in_seen"].set()
            events += [TextDeltaEvent(text="The child finished after receiving the date range.")] + _end()
        elif "DELEGATION (sub-agents" in system_text and "delegate" in tool_names:
            if not delegated:
                delegated = True
                events += _tool("delegate", {
                    "goal": "Determine whether the recent conversion decline is real and report evidence.",
                    "context": "Use a stable, complete date window.",
                    "title": "Conversion decline check",
                }) + _end("tool_use")
            elif not held:
                held = True
                milestones["parent_held"].set()
                await asyncio.to_thread(release_parent.wait, 30)
                events += [TextDeltaEvent(text="I have finished the original turn; the queued prompt will continue the investigation.")] + _end()
            elif ('kind="question"' in text and "message_sub_agent" in tool_names and not answered):
                match = re.search(r'<message ([^>]*kind="question"[^>]*)>', text)
                assert match, text
                attrs = dict(re.findall(r'(message_id|sender|recipient|kind)="([^"]*)"', match.group(1)))
                answered = True
                milestones["parent_received_question"].set()
                events += _tool("message_sub_agent", {
                    "recipient": attrs["sender"], "kind": "answer",
                    "body": "Use the most recent complete 14-day window.",
                    "reply_to": attrs["message_id"],
                }) + _end("tool_use")
            else:
                events += [TextDeltaEvent(text="The child has its date range and has finished.")] + _end()
        else:
            events += [TextDeltaEvent(text="The queued parent prompt is underway.")] + _end()
        for event in events:
            yield event

    return scripted


async def _pending_children(report_id: str, org_id: str, *, count: int = 2):
    """Create pending children through the same service used by `delegate`."""
    from app.ai.tools.schemas.sub_agents import DelegateInput
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.sub_agent_service import sub_agent_service

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        organization = await db.get(Organization, org_id)
        user = await db.get(User, report.user_id)
        rows = []
        for index in range(count):
            receipt = await sub_agent_service.record_delegation(
                db,
                organization=organization,
                user=user,
                report=report,
                parent_head_completion=None,
                parent_system_completion=None,
                parent_execution_id=None,
                tool_execution_id=None,
                plan_decision_id=None,
                data=DelegateInput(
                    goal=f"Check independent fact number {index} and report evidence.",
                    title=f"Message target {index}",
                ),
                settings=None,
                parent_mode="chat",
                parent_model=None,
                small_model=None,
            )
            rows.append(receipt)
        return rows


@pytest.mark.e2e
def test_messages_persist_deliver_and_enforce_parent_child_scope(
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
):
    """Messages survive sessions, are redeliverable for context, and stay in
    the owning parent/child relationship. Routine findings do not wake parent.
    """
    from app.models.sub_agent_message import SubAgentMessage
    from app.services.sub_agent_message_service import sub_agent_message_service
    from app.services.sub_agent_service import sub_agent_service

    report, _headers, org_id, token = _env(
        create_user,
        login_user,
        whoami,
        create_report,
        create_llm_provider_and_models,
        monkeypatch,
        title="Sub-agent messages",
    )
    child_a, child_b = _run(_pending_children(report["id"], org_id))
    other_report = create_report(
        title="Other report scope",
        user_token=token,
        org_id=org_id,
        data_sources=[],
    )

    async def _send_and_receive():
        async with async_session_maker() as db:
            parent_to_child = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=None,
                recipient=child_a["sub_agent_id"],
                kind="direction",
                body="Check the weekly baseline before drawing a conclusion.",
                reply_to=None,
                idempotency_key="parent-direction-once",
            )
            repeated = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=None,
                recipient=child_a["sub_agent_id"],
                kind="direction",
                body="Check the weekly baseline before drawing a conclusion.",
                reply_to=None,
                idempotency_key="parent-direction-once",
            )
            finding = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=child_a["sub_agent_id"],
                recipient="parent",
                kind="finding",
                body="The week-over-week baseline is stable.",
                reply_to=None,
                idempotency_key="child-finding-once",
            )
            stopped_direction = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=None,
                recipient=child_b["sub_agent_id"],
                kind="direction",
                body="Check the fallback window if the primary slice is incomplete.",
                reply_to=None,
                idempotency_key="direction-to-stopped-child",
            )
            stopped_child = await sub_agent_service.stop_sub_agent(
                db,
                child_b["sub_agent_id"],
                reason="Parent no longer needs this child.",
            )

            assert parent_to_child["message_id"] == repeated["message_id"]
            assert finding["status"] == "sent"
            assert stopped_child is not None and stopped_child.status == "interrupted"
            assert stopped_direction["status"] == "sent"

            # The first send persists before another session reads it.
            await db.commit()

        async with async_session_maker() as db:
            # Reading the parent's mailbox exposes its outgoing receipts but
            # cannot acknowledge delivery to a child. A stopped recipient's
            # receipt remains visible with its undelivered state.
            parent_inbox = await sub_agent_message_service.receive(
                db,
                organization_id=org_id,
                report_id=report["id"],
                recipient="parent",
            )
            parent_receipts = {m["message_id"]: m for m in parent_inbox}
            assert set(parent_receipts) == {
                parent_to_child["message_id"], finding["message_id"], stopped_direction["message_id"],
            }
            assert parent_receipts[parent_to_child["message_id"]]["status"] == "sent"
            assert parent_receipts[stopped_direction["message_id"]]["status"] == "undelivered"

            child_inbox = await sub_agent_message_service.receive(
                db,
                organization_id=org_id,
                report_id=report["id"],
                recipient=child_a["sub_agent_id"],
            )
            assert {m["message_id"] for m in child_inbox} == {parent_to_child["message_id"]}
            assert child_inbox[0]["body"] == "Check the weekly baseline before drawing a conclusion."
            assert parent_receipts[finding["message_id"]]["body"] == "The week-over-week baseline is stable."

            # Delivered messages remain available when the recipient rebuilds
            # its planning context on a later step.
            replay = await sub_agent_message_service.receive(
                db,
                organization_id=org_id,
                report_id=report["id"],
                recipient=child_a["sub_agent_id"],
            )
            assert {m["message_id"] for m in replay} == {parent_to_child["message_id"]}

            # The parent's retained outgoing receipt advances only because the
            # child received it, not because the parent rebuilt its context.
            parent_replay = await sub_agent_message_service.receive(
                db,
                organization_id=org_id,
                report_id=report["id"],
                recipient="parent",
            )
            receipts_by_id = {m["message_id"]: m for m in parent_replay}
            assert receipts_by_id[parent_to_child["message_id"]]["status"] == "delivered"
            assert receipts_by_id[stopped_direction["message_id"]]["status"] == "undelivered"

            messages = (await db.execute(
                select(SubAgentMessage).where(SubAgentMessage.report_id == report["id"])
            )).scalars().all()
            assert len(messages) == 3
            statuses = {m.id: m.status for m in messages}
            assert statuses[parent_to_child["message_id"]] == "delivered"
            assert statuses[finding["message_id"]] == "delivered"
            assert statuses[stopped_direction["message_id"]] == "undelivered"

            # Routine findings stay durable but do not create a parent wake.
            from app.models.completion import Completion
            wakes = (await db.execute(
                select(Completion).where(
                    Completion.report_id == report["id"],
                    Completion.role == "user",
                    Completion.trigger_source == "sub_agent_message",
                )
            )).scalars().all()
            assert wakes == []

            # A child may only communicate with its own parent, never a sibling.
            with pytest.raises(ValueError):
                await sub_agent_message_service.send(
                    db,
                    organization_id=org_id,
                    report_id=report["id"],
                    sender_sub_agent_id=child_a["sub_agent_id"],
                    recipient=child_b["sub_agent_id"],
                    kind="answer",
                    body="This must not reach a sibling.",
                    reply_to=None,
                    idempotency_key="sibling-message",
                )

            with pytest.raises(ValueError):
                await sub_agent_message_service.send(
                    db,
                    organization_id="org_" + uuid.uuid4().hex,
                    report_id=report["id"],
                    sender_sub_agent_id=None,
                    recipient=child_a["sub_agent_id"],
                    kind="direction",
                    body="Wrong organization.",
                    reply_to=None,
                    idempotency_key="cross-org-message",
                )

            with pytest.raises(ValueError):
                await sub_agent_message_service.send(
                    db,
                    organization_id=org_id,
                    report_id=other_report["id"],
                    sender_sub_agent_id=None,
                    recipient=child_a["sub_agent_id"],
                    kind="direction",
                    body="Wrong report.",
                    reply_to=None,
                    idempotency_key="cross-report-message",
                )

    _run(_send_and_receive())


@pytest.mark.e2e
def test_child_question_creates_one_queued_parent_wake_and_terminal_child_cannot_send(
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
):
    """A question is a durable queued machine trigger; terminal children have
    no send path that could resume their old execution.
    """
    from app.ai.tools.schemas.sub_agents import FinishSubtaskInput
    from app.models.completion import Completion
    from app.models.sub_agent import SubAgentRun
    from app.services.sub_agent_message_service import sub_agent_message_service
    from app.services.sub_agent_service import sub_agent_service

    report, _headers, org_id, _token = _env(
        create_user,
        login_user,
        whoami,
        create_report,
        create_llm_provider_and_models,
        monkeypatch,
        title="Sub-agent questions",
    )
    child, other_child = _run(_pending_children(report["id"], org_id, count=2))

    async def _question_and_close():
        async with async_session_maker() as db:
            question = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=child["sub_agent_id"],
                recipient="parent",
                kind="question",
                body="Which date range should I use for the comparison?",
                reply_to=None,
                idempotency_key="child-question-once",
            )
            answer = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=None,
                recipient=child["sub_agent_id"],
                kind="answer",
                body="Use the most recent complete 14-day window.",
                reply_to=question["message_id"],
                idempotency_key="parent-answer-once",
            )
            # Retries with the same send identity return their original receipt;
            # a second distinct answer cannot silently race/override it.
            answer_retry = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=None,
                recipient=child["sub_agent_id"],
                kind="answer",
                body="Use the most recent complete 14-day window.",
                reply_to=question["message_id"],
                idempotency_key="parent-answer-once",
            )
            assert answer_retry["message_id"] == answer["message_id"]
            with pytest.raises(ValueError, match="already has an answer"):
                await sub_agent_message_service.send(
                    db,
                    organization_id=org_id,
                    report_id=report["id"],
                    sender_sub_agent_id=None,
                    recipient=child["sub_agent_id"],
                    kind="answer",
                    body="Actually use a 28-day window.",
                    reply_to=question["message_id"],
                    idempotency_key="parent-second-answer",
                )
            second_question = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=other_child["sub_agent_id"],
                recipient="parent",
                kind="question",
                body="Should I exclude the holiday weekend from the comparison?",
                reply_to=None,
                idempotency_key="second-child-question-once",
            )
            await db.commit()

        async with async_session_maker() as db:
            wakes = (await db.execute(
                select(Completion).where(
                    Completion.report_id == report["id"],
                    Completion.role == "user",
                    Completion.trigger_source == "sub_agent_message",
                )
            )).scalars().all()
            assert len(wakes) == 1
            wake = wakes[0]
            assert wake.role == "user" and wake.status == "queued"
            assert wake.message_type == "sub_agent_question"
            assert question["message_id"] and second_question["message_id"]
            parent_inbox = await sub_agent_message_service.receive(
                db,
                organization_id=org_id,
                report_id=report["id"],
                recipient="parent",
            )
            question_states = {m["message_id"]: m.get("question_state") for m in parent_inbox}
            assert question_states[question["message_id"]] == "answered"
            assert question_states[second_question["message_id"]] == "open"
            child_inbox = await sub_agent_message_service.receive(
                db,
                organization_id=org_id,
                report_id=report["id"],
                recipient=child["sub_agent_id"],
            )
            assert any(m["message_id"] == answer["message_id"] for m in child_inbox)

            row = await db.get(SubAgentRun, child["sub_agent_id"])
            result = FinishSubtaskInput(
                outcome="blocked",
                confidence=0.0,
                summary="The report owner has not chosen the comparison date range.",
                unknown="Which date range should be used?",
            ).model_dump()
            await sub_agent_service.record_finish(
                db,
                sub_agent_id=str(row.id),
                result=result,
            )
            other_row = await db.get(SubAgentRun, other_child["sub_agent_id"])
            await sub_agent_service.record_finish(
                db,
                sub_agent_id=str(other_row.id),
                result=result,
            )

            parent_inbox = await sub_agent_message_service.receive(
                db,
                organization_id=org_id,
                report_id=report["id"],
                recipient="parent",
            )
            question_states = {m["message_id"]: m.get("question_state") for m in parent_inbox}
            assert question_states[second_question["message_id"]] == "ended"

            with pytest.raises(ValueError):
                await sub_agent_message_service.send(
                    db,
                    organization_id=org_id,
                    report_id=report["id"],
                    sender_sub_agent_id=child["sub_agent_id"],
                    recipient="parent",
                    kind="question",
                    body="A terminal child cannot ask a follow-up.",
                    reply_to=question["message_id"],
                    idempotency_key="after-terminal",
                )

    _run(_question_and_close())


@pytest.mark.e2e
def test_question_wakes_coalesce_per_principal_not_across_report_members(
    test_client,
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
):
    """A report-wide queue can hold one question wake per actual requester.

    Questions from two users sharing a report must not coalesce onto the first
    user's wake: if that user later loses access, the other member still has a
    durable wake eligible to run under their own identity.
    """
    from app.ai.tools.schemas.sub_agents import DelegateInput
    from app.models.completion import Completion
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.sub_agent_message_service import sub_agent_message_service
    from app.services.sub_agent_service import sub_agent_service

    report, headers, org_id, _token = _env(
        create_user,
        login_user,
        whoami,
        create_report,
        create_llm_provider_and_models,
        monkeypatch,
        title="Sub-agent wake principals",
    )
    peer_email = f"message-peer-{uuid.uuid4().hex}@example.com"
    invite = test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": peer_email, "role": "member"},
        headers=headers,
    )
    assert invite.status_code == 200, invite.text
    peer_credentials = create_user(email=peer_email, password="test123")
    peer_token = login_user(peer_credentials["email"], peer_credentials["password"])
    user_b_id = whoami(peer_token)["id"]
    children_a = _run(_pending_children(report["id"], org_id, count=2))

    async def _prepare_peer_wave():
        async with async_session_maker() as db:
            peer = await db.get(User, user_b_id)
            organization = await db.get(Organization, org_id)
            report_row = await db.get(Report, report["id"])
            # Identity and membership come through the invite/register APIs.
            # Seed only a completed parent turn: the public delegation service
            # needs a distinct turn id to model this prior wave, while running
            # a whole parent LLM turn is outside this queue-coalescing test.
            parent_completion = Completion(
                report_id=report["id"], user_id=user_b_id, role="system", status="success",
                model="test-model", prompt={}, completion={}, turn_index=100,
            )
            db.add(parent_completion)
            await db.commit()
            await db.refresh(parent_completion)
            children = []
            for index in range(2):
                child = await sub_agent_service.record_delegation(
                    db,
                    organization=organization,
                    user=peer,
                    report=report_row,
                    parent_head_completion=None,
                    parent_system_completion=parent_completion,
                    parent_execution_id=None,
                    tool_execution_id=None,
                    plan_decision_id=None,
                    data=DelegateInput(
                        goal=f"Check peer fact {index} and report evidence.",
                        title=f"Peer message target {index}",
                    ),
                    settings=None,
                    parent_mode="chat",
                    parent_model=None,
                    small_model=None,
                )
                children.append(child)
            return children, user_b_id

    children_b, user_b_id = _run(_prepare_peer_wave())

    async def _send_and_assert():
        async with async_session_maker() as db:
            for child, body, key in (
                (children_a[0], "Which baseline should I use?", "question-a-one"),
                (children_a[1], "Should I exclude refunds?", "question-a-two"),
                (children_b[0], "Which region should I compare?", "question-b-one"),
            ):
                await sub_agent_message_service.send(
                    db,
                    organization_id=org_id,
                    report_id=report["id"],
                    sender_sub_agent_id=child["sub_agent_id"],
                    recipient="parent",
                    kind="question",
                    body=body,
                    reply_to=None,
                    idempotency_key=key,
                )

            wakes = (await db.execute(select(Completion).where(
                Completion.report_id == report["id"],
                Completion.role == "user",
                Completion.trigger_source == "sub_agent_message",
            ))).scalars().all()
            assert {wake.user_id for wake in wakes} == {parent_user_id, user_b_id}
            assert len(wakes) == 2

    async def _report_owner_id():
        async with async_session_maker() as db:
            row = await db.get(Report, report["id"])
            return str(row.user_id)

    parent_user_id = _run(_report_owner_id())
    _run(_send_and_assert())


@pytest.mark.e2e
def test_open_question_reserves_final_message_slot_for_answer(
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
):
    """At the 32-message cap, neither a new question nor a parent direction
    can consume the final slot needed to deliver the answer to an open question.
    """
    from app.models.sub_agent_message import SubAgentMessage
    from app.services.sub_agent_message_service import sub_agent_message_service

    report, _headers, org_id, _token = _env(
        create_user,
        login_user,
        whoami,
        create_report,
        create_llm_provider_and_models,
        monkeypatch,
        title="Sub-agent message budget",
    )
    child, = _run(_pending_children(report["id"], org_id, count=1))

    async def _exercise_budget():
        async with async_session_maker() as db:
            # Fill to 30 through the public service; the next slot is available
            # for the child's question and the final slot must remain for answer.
            for index in range(30):
                await sub_agent_message_service.send(
                    db,
                    organization_id=org_id,
                    report_id=report["id"],
                    sender_sub_agent_id=None,
                    recipient=child["sub_agent_id"],
                    kind="direction",
                    body=f"Parent direction {index + 1}.",
                    reply_to=None,
                    idempotency_key=f"budget-direction-{index + 1}",
                )

            question = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=child["sub_agent_id"],
                recipient="parent",
                kind="question",
                body="Which date range should I use?",
                reply_to=None,
                idempotency_key="budget-question",
            )

            # A parent direction cannot spend the slot reserved for the open
            # question's answer.
            with pytest.raises(ValueError):
                await sub_agent_message_service.send(
                    db,
                    organization_id=org_id,
                    report_id=report["id"],
                    sender_sub_agent_id=None,
                    recipient=child["sub_agent_id"],
                    kind="direction",
                    body="Use a different comparison window.",
                    reply_to=None,
                    idempotency_key="budget-direction-while-open",
                )

            answer = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report["id"],
                sender_sub_agent_id=None,
                recipient=child["sub_agent_id"],
                kind="answer",
                body="Use the most recent complete 14-day window.",
                reply_to=question["message_id"],
                idempotency_key="budget-answer",
            )
            messages = (await db.execute(select(SubAgentMessage).where(
                SubAgentMessage.sub_agent_id == child["sub_agent_id"],
            ))).scalars().all()
            assert answer["status"] == "sent"
            assert len(messages) == 32
            assert sum(message.kind == "answer" for message in messages) == 1
            assert sum(message.kind == "direction" for message in messages) == 30

    _run(_exercise_budget())


@pytest.mark.e2e
def test_late_finish_cannot_replace_a_stopped_child_record(
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
):
    """A finish arriving after stop preserves the stop result and status."""
    from app.models.sub_agent import SubAgentRun
    from app.services.sub_agent_service import sub_agent_service

    report, _headers, org_id, _token = _env(
        create_user,
        login_user,
        whoami,
        create_report,
        create_llm_provider_and_models,
        monkeypatch,
        title="Stopped child late finish",
    )
    child, = _run(_pending_children(report["id"], org_id, count=1))

    async def _stop_then_finish():
        async with async_session_maker() as db:
            stopped = await sub_agent_service.stop_sub_agent(
                db,
                child["sub_agent_id"],
                reason="Parent stopped this child.",
            )
            assert stopped is not None and stopped.status == "interrupted"
            stop_record = dict(stopped.result_json or {})
            assert stop_record.get("outcome") == "partial"

            result = {
                "outcome": "done",
                "confidence": 1.0,
                "summary": "A late provider response must not replace the stop record.",
                "findings": [],
                "ruled_out": [],
                "evidence": [],
                "caveats": [],
                "unknown": None,
                "report_note_id": None,
            }
            with pytest.raises(ValueError):
                await sub_agent_service.record_finish(
                    db,
                    sub_agent_id=child["sub_agent_id"],
                    result=result,
                )

            row = await db.get(SubAgentRun, child["sub_agent_id"])
            await db.refresh(row)
            assert row.status == "interrupted"
            assert row.exit_reason == "stopped"
            assert row.result_json == stop_record

    _run(_stop_then_finish())


@pytest.mark.e2e
def test_real_child_question_parent_answer_and_child_resume(
    live_client,
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
):
    """A child question moves through the real planner/tool/queue path, the
    parent answers it, and the same child execution resumes to finish.
    """
    from app.ai.llm.llm import LLM
    from app.models.completion import Completion
    from app.models.sub_agent import SubAgentRun

    milestones = {
        name: threading.Event()
        for name in (
            "child_asked_question",
            "parent_received_question",
            "child_received_answer",
            "fan_in_seen",
        )
    }
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    monkeypatch.setattr(LLM, "inference_stream_v2", _scripted_message_flow(milestones=milestones))

    async def plain_stream(self, prompt, **kwargs):
        yield "Sub-agent question flow"

    monkeypatch.setattr(LLM, "inference_stream", plain_stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **kw: "ok")
    report, headers, org_id, _token = _env(
        create_user,
        login_user,
        whoami,
        create_report,
        create_llm_provider_and_models,
        monkeypatch,
        title="Sub-agent question flow",
    )

    response = live_client.post(
        f"/api/reports/{report['id']}/completions",
        json={"prompt": {"content": "Check the conversion decline.", "widget_id": None, "step_id": None, "mentions": [{}]}},
        headers=headers,
        # Match the established live-client e2e path: keep the parent turn
        # synchronous while the child launcher/child run stay on the same
        # persistent TestClient event loop.
        params={"background": False},
    )
    assert response.status_code == 200, response.text
    assert milestones["child_asked_question"].wait(timeout=12), (
        "child never asked its question; "
        f"calls={milestones.get('llm_calls')}; "
        f"completion={live_client.get(f'/api/reports/{report['id']}/completions', headers=headers).json()}; "
        f"sub_agents={live_client.get(f'/api/reports/{report['id']}/sub-agents', headers=headers).json()}"
    )
    assert milestones["parent_received_question"].wait(timeout=60), "parent never received the queued question"
    assert milestones["child_received_answer"].wait(timeout=60), "child did not resume with the parent's answer"
    assert milestones["fan_in_seen"].wait(timeout=60), "finished child did not trigger wave fan-in"

    children = live_client.get(f"/api/reports/{report['id']}/sub-agents", headers=headers)
    assert children.status_code == 200, children.text
    runs = [run for wave in children.json()["waves"] for run in wave["sub_agents"]]
    assert len(runs) == 1, runs
    child = runs[0]
    assert child["status"] == "finished" and child["outcome"] == "done", child

    async def _assert_queue_and_same_execution():
        async with async_session_maker() as db:
            wakes = (await db.execute(
                select(Completion).where(
                    Completion.report_id == report["id"],
                    Completion.role == "user",
                    Completion.trigger_source == "sub_agent_message",
                )
            )).scalars().all()
            assert len(wakes) == 1
            wake = wakes[0]
            assert wake.status == "success" and wake.message_type == "sub_agent_question"
            replies = (await db.execute(
                select(Completion).where(Completion.parent_id == wake.id, Completion.role == "system")
            )).scalars().all()
            assert len(replies) == 1 and replies[0].status == "success", [
                (str(reply.id), reply.status, reply.completion)
                for reply in replies
            ]
            row = await db.get(SubAgentRun, child["sub_agent_id"])
            assert row is not None and row.child_execution_id
            assert row.status == "finished"
            from app.models.agent_execution import AgentExecution
            execution = await db.get(AgentExecution, row.child_execution_id)
            assert execution is not None and execution.completed_at is not None

    _run(_assert_queue_and_same_execution())


@pytest.mark.e2e
@pytest.mark.parametrize("last_step", [False, True])
def test_accepted_direction_during_finish_planning_is_consumed_before_finish(
    live_client,
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
    last_step,
):
    """A direction accepted after a child's planner snapshot cannot vanish
    when an in-flight finish_subtask races to close that child.
    """
    from app.ai.llm.llm import LLM
    from app.ai.llm.types import (
        MessageStopEvent,
        TextDeltaEvent,
        ToolUseCompleteEvent,
        ToolUseStartEvent,
        UsageEvent,
    )
    from app.models.sub_agent import SubAgentRun
    from app.models.sub_agent_message import SubAgentMessage
    from app.services.sub_agent_message_service import sub_agent_message_service

    finish_boundary = threading.Event()
    release_finish = threading.Event()
    child_received_direction = threading.Event()
    child_finished = threading.Event()
    parent_delegated = False
    child_calls = 0

    def _tool(name, args):
        call_id = f"tool_{uuid.uuid4().hex[:12]}"
        return [ToolUseStartEvent(id=call_id, name=name), ToolUseCompleteEvent(id=call_id, name=name, input=args)]

    def _end(reason="end_turn"):
        return [MessageStopEvent(stop_reason=reason, raw_stop_reason=reason), UsageEvent(input_tokens=64, output_tokens=24)]

    def _finish(summary):
        return _tool("finish_subtask", {
            "outcome": "done",
            "confidence": 0.9,
            "summary": summary,
            "findings": [{"claim": "The requested comparison was completed."}],
            "ruled_out": [],
            "evidence": [],
            "caveats": [],
            "unknown": None,
            "report_note_id": None,
        }) + _end("tool_use")

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        nonlocal parent_delegated, child_calls
        system_text = system or ""
        text = _all_text(messages)
        tool_names = {
            getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
            for tool in (tools or [])
        }
        events = []
        if "Mode: Sub-agent" in system_text:
            child_calls += 1
            if child_calls < (3 if last_step else 2):
                assert "message_sub_agent" in tool_names, f"child tools did not include messaging: {tool_names}"
                events += _tool("message_sub_agent", {
                    "recipient": "parent",
                    "kind": "finding",
                    "body": "Initial scan is complete; I am checking the final comparison.",
                    "reply_to": None,
                }) + _end("tool_use")
            elif child_calls == (3 if last_step else 2):
                # This LLM request was planned before the parent direction was
                # sent. Keep the provider response in flight to reproduce the
                # race at the finish_subtask boundary.
                finish_boundary.set()
                await asyncio.to_thread(release_finish.wait, 30)
                events += _finish("Finished without the newly accepted direction.")
            else:
                assert "Use the weekday-only comparison window." in text, (
                    "child reached its next finish decision without receiving the accepted direction; "
                    f"planner input was {text[-2000:]}"
                )
                child_received_direction.set()
                events += _finish("Applied the parent's weekday-only comparison window.")
                child_finished.set()
        elif "DELEGATION (sub-agents" in system_text and "delegate" in tool_names:
            if not parent_delegated:
                parent_delegated = True
                events += _tool("delegate", {
                    "goal": "Check whether the comparison holds after excluding weekend data.",
                    "context": "Check the comparison with the weekdays-only slice before reporting.",
                    "title": "Weekday comparison check",
                    "max_steps": 3 if last_step else 10,
                }) + _end("tool_use")
            else:
                events += [TextDeltaEvent(text="The delegated comparison is running.")] + _end()
        elif "<sub_agent_results" in text:
            events += [TextDeltaEvent(text="The child applied the weekday-only direction.")] + _end()
        else:
            events += [TextDeltaEvent(text="The delegated comparison is running.")] + _end()
        for event in events:
            yield event

    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    monkeypatch.setattr(LLM, "inference_stream_v2", scripted)

    async def plain_stream(self, prompt, **kwargs):
        yield "Direction during finish-planning race"

    monkeypatch.setattr(LLM, "inference_stream", plain_stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **kw: "ok")
    report, headers, org_id, _token = _env(
        create_user,
        login_user,
        whoami,
        create_report,
        create_llm_provider_and_models,
        monkeypatch,
        title="Direction during finish-planning race",
    )
    report_id = report["id"]

    initial = live_client.post(
        f"/api/reports/{report_id}/completions",
        json={"prompt": {"content": "Check the comparison for this report.", "mentions": [{}]}},
        headers=headers,
        params={"background": False},
    )
    assert initial.status_code == 200, initial.text
    assert finish_boundary.wait(timeout=20), "child did not reach its in-flight finish-planning boundary"

    async def _send_direction():
        async with async_session_maker() as db:
            child = (await db.execute(select(SubAgentRun).where(
                SubAgentRun.report_id == report_id,
                SubAgentRun.status == "running",
            ))).scalar_one()
            receipt = await sub_agent_message_service.send(
                db,
                organization_id=org_id,
                report_id=report_id,
                sender_sub_agent_id=None,
                recipient=str(child.id),
                kind="direction",
                body="Use the weekday-only comparison window.",
                reply_to=None,
                idempotency_key="direction-during-finish-planning",
            )
            return str(child.id), receipt

    child_id, receipt = _run(_send_direction())
    assert receipt["status"] == "sent", receipt
    release_finish.set()

    try:
        if not last_step:
            assert child_received_direction.wait(timeout=20), (
                "an accepted direction was not supplied to a later planner step before finish_subtask; "
                f"child_calls={child_calls}"
            )
            assert child_finished.wait(timeout=20), "child did not finish after receiving the direction"

        async def _read_terminal_and_message_status():
            async with async_session_maker() as db:
                row = await db.get(SubAgentRun, child_id)
                message = (await db.execute(select(SubAgentMessage).where(
                    SubAgentMessage.idempotency_key == "direction-during-finish-planning",
                ))).scalar_one()
                if last_step and row.status == "finished":
                    assert row.result_json["summary"] == "Finished without the newly accepted direction."
                    assert row.result_json["findings"]
                    assert row.result_json["caveats"]
                return (row.status if row is not None else None), message.status

        expected = ("finished", "undelivered" if last_step else "delivered")

        def _terminal_and_delivered():
            statuses = _run(_read_terminal_and_message_status())
            return statuses if statuses == expected else None

        final_statuses = _wait_for(
            _terminal_and_delivered,
            timeout_s=20,
            what="child finish and delivery of the accepted direction",
        )
        assert final_statuses == expected, final_statuses
    finally:
        release_finish.set()


@pytest.mark.e2e
def test_parent_direction_preserves_tools_and_fresh_steering(
    live_client,
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
):
    """After a correction, the parent can continue independent work or message
    other children. Fresh steering still takes priority without replaying sends.
    """
    from app.ai.llm.llm import LLM
    from app.ai.llm.types import (
        MessageStopEvent,
        TextDeltaEvent,
        ToolUseCompleteEvent,
        ToolUseStartEvent,
        UsageEvent,
    )
    from app.models.sub_agent import SubAgentRun
    from app.services.sub_agent_message_service import sub_agent_message_service

    child_at_provider = threading.Event()
    release_child = threading.Event()
    handoff_planner = threading.Event()
    release_handoff_planner = threading.Event()
    fresh_steer_planner = threading.Event()
    child_received_direction = threading.Event()
    child_finished = threading.Event()
    parent_delegated = False
    parent_direction_sent = False
    parent_direction_calls = 0
    parent_handoff_planner_calls = 0
    parent_handoff_tool_names = None
    child_calls = 0
    child_id = None

    def _tool(name, args):
        call_id = f"tool_{uuid.uuid4().hex[:12]}"
        return [ToolUseStartEvent(id=call_id, name=name), ToolUseCompleteEvent(id=call_id, name=name, input=args)]

    def _end(reason="end_turn"):
        return [MessageStopEvent(stop_reason=reason, raw_stop_reason=reason), UsageEvent(input_tokens=64, output_tokens=24)]

    def _finish(summary):
        return _tool("finish_subtask", {
            "outcome": "done",
            "confidence": 0.9,
            "summary": summary,
            "findings": [{"claim": "The weekday comparison was completed."}],
            "ruled_out": [],
            "evidence": [],
            "caveats": [],
            "unknown": None,
            "report_note_id": None,
        }) + _end("tool_use")

    async def scripted(self, *, model_id=None, messages, system=None, tools=None, **kwargs):
        nonlocal parent_delegated, parent_direction_sent, parent_direction_calls
        nonlocal parent_handoff_planner_calls, parent_handoff_tool_names, child_calls
        system_text = system or ""
        text = _all_text(messages)
        tool_names = {
            getattr(tool, "name", None) or (tool.get("name") if isinstance(tool, dict) else None)
            for tool in (tools or [])
        }
        tool_names.discard(None)
        events = []
        if "Mode: Sub-agent" in system_text:
            child_calls += 1
            if child_calls == 1:
                assert "finish_subtask" in tool_names
                child_at_provider.set()
                await asyncio.to_thread(release_child.wait, 30)
                # Return a non-terminal tool-use response that was prepared
                # before the parent's direction was delivered. The following
                # planning step must incorporate it.
                events += _tool("message_sub_agent", {
                    "recipient": "parent",
                    "kind": "finding",
                    "body": "The initial slice is checked.",
                    "reply_to": None,
                }) + _end("tool_use")
            else:
                assert "Use a weekday-only comparison for the child." in text, (
                    "child did not receive the parent's direction before finishing: " + text[-1500:]
                )
                child_received_direction.set()
                events += _finish("Applied the weekday-only comparison direction.")
                child_finished.set()
        elif "DELEGATION (sub-agents" in system_text:
            if not parent_delegated and "delegate" in tool_names:
                parent_delegated = True
                events += _tool("delegate", {
                    "goal": "Check the comparison with weekday-only data and report the result.",
                    "context": "The parent may send a correction while you are working.",
                    "title": "Weekday-only comparison",
                }) + _end("tool_use")
            elif "Fresh steer: split weekday and weekend results." in text:
                assert "message_sub_agent" in tool_names, (
                    "a new user steer must reopen the normal parent tool catalog"
                )
                fresh_steer_planner.set()
                events += [TextDeltaEvent(text="I will split the results by weekday and weekend.")] + _end()
            elif "Please direct the child to use a weekday-only comparison." in text:
                if not parent_direction_sent:
                    assert child_id is not None
                    assert "message_sub_agent" in tool_names
                    parent_direction_calls += 1
                    events += _tool("message_sub_agent", {
                        "recipient": child_id,
                        "kind": "direction",
                        "body": "Use a weekday-only comparison for the child.",
                        "reply_to": None,
                    }) + _end("tool_use")
                    parent_direction_sent = True
                else:
                    parent_handoff_planner_calls += 1
                    parent_handoff_tool_names = sorted(tool_names)
                    handoff_planner.set()
                    await asyncio.to_thread(release_handoff_planner.wait, 30)
                    events += [TextDeltaEvent(text="I sent the weekday-only correction; the child result will arrive automatically.")] + _end()
            else:
                events += [TextDeltaEvent(text="The child is checking the requested comparison.")] + _end()
        else:
            events += [TextDeltaEvent(text="The child is checking the requested comparison.")] + _end()
        for event in events:
            yield event

    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    monkeypatch.setattr(LLM, "inference_stream_v2", scripted)

    async def plain_stream(self, prompt, **kwargs):
        yield "Parent correction handoff"

    monkeypatch.setattr(LLM, "inference_stream", plain_stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **kw: "ok")
    report, headers, org_id, _token = _env(
        create_user,
        login_user,
        whoami,
        create_report,
        create_llm_provider_and_models,
        monkeypatch,
        title="Parent correction handoff",
    )
    report_id = report["id"]

    initial = live_client.post(
        f"/api/reports/{report_id}/completions",
        json={"prompt": {"content": "Check the comparison for this report.", "mentions": [{}]}},
        headers=headers,
        params={"background": False},
    )
    assert initial.status_code == 200, initial.text
    assert child_at_provider.wait(timeout=20), "child did not reach its held planner request"

    async def _get_running_child():
        async with async_session_maker() as db:
            row = (await db.execute(select(SubAgentRun).where(
                SubAgentRun.report_id == report_id,
                SubAgentRun.status == "running",
            ))).scalar_one()
            return str(row.id)

    child_id = _run(_get_running_child())
    correction = live_client.post(
        f"/api/reports/{report_id}/completions",
        json={"prompt": {"content": "Please direct the child to use a weekday-only comparison.", "mentions": [{}]}},
        headers=headers,
        params={"background": True},
    )
    assert correction.status_code == 200, correction.text
    parent_system = next(c for c in correction.json()["completions"] if c["role"] == "system")
    assert handoff_planner.wait(timeout=30), "parent did not reach its post-send planner step"
    assert {"message_sub_agent", "read_sub_agent"} <= set(parent_handoff_tool_names), (
        "a direction must not disable other messages or independent parent work"
    )

    # A new user steer on this same execution must interrupt the response
    # and take priority while keeping tools available on the next planner call.
    steer = live_client.post(
        f"/api/completions/{parent_system['id']}/steer",
        json={"content": "Fresh steer: split weekday and weekend results."},
        headers=headers,
    )
    assert steer.status_code == 200 and steer.json()["status"] == "steered", steer.text
    release_handoff_planner.set()
    assert fresh_steer_planner.wait(timeout=20), "fresh steer did not reach the parent with tools available"
    assert parent_direction_calls == 1
    assert parent_handoff_planner_calls == 1

    release_child.set()
    try:
        assert child_received_direction.wait(timeout=20), "child did not receive the accepted direction"
        assert child_finished.wait(timeout=20), "child did not finish after applying the direction"

        async def _final_statuses():
            async with async_session_maker() as db:
                row = await db.get(SubAgentRun, child_id)
                messages = await sub_agent_message_service.list_for_child(
                    db,
                    organization_id=org_id,
                    report_id=report_id,
                    sub_agent_id=child_id,
                )
                return row.status if row else None, [m for m in messages if m["kind"] == "direction"]

        def _direction_delivered_after_finish():
            status, directions = _run(_final_statuses())
            return (status, directions) if status == "finished" and len(directions) == 1 else None

        child_status, directions = _wait_for(
            _direction_delivered_after_finish,
            timeout_s=20,
            what="child finish after one accepted parent direction",
        )
        assert child_status == "finished"
        assert directions[0]["body"] == "Use a weekday-only comparison for the child."
        assert directions[0]["status"] == "delivered"
    finally:
        release_child.set()
        release_handoff_planner.set()


@pytest.mark.parametrize("streaming", [False, True], ids=["direct", "stream"])
@pytest.mark.e2e
def test_user_submit_racing_child_question_is_queued_and_drains_once(
    streaming,
    live_client,
    create_user,
    login_user,
    whoami,
    create_report,
    create_llm_provider_and_models,
    monkeypatch,
):
    """A user submit during an active parent turn is durable in the queue for
    both direct and streaming endpoints; it drains once after the held turn,
    handles the child's question, and leaves no duplicate question wake run.
    """
    from app.ai.llm.llm import LLM
    from app.models.completion import Completion
    from app.models.sub_agent_message import SubAgentMessage

    release_parent = threading.Event()
    milestones = {
        name: threading.Event()
        for name in (
            "parent_held", "child_asked_question", "parent_received_question",
            "child_received_answer", "fan_in_seen",
        )
    }
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "dummy-local-provider-boundary")
    monkeypatch.setattr(
        LLM, "inference_stream_v2",
        _held_parent_message_flow(release_parent=release_parent, milestones=milestones),
    )

    async def plain_stream(self, prompt, **kwargs):
        yield "Sub-agent queue race"

    monkeypatch.setattr(LLM, "inference_stream", plain_stream)
    monkeypatch.setattr(LLM, "inference", lambda self, prompt, *a, **kw: "ok")
    report, headers, org_id, _token = _env(
        create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch,
        title=f"Sub-agent queue race {'stream' if streaming else 'direct'}",
    )
    report_id = report["id"]

    try:
        initial = live_client.post(
            f"/api/reports/{report_id}/completions",
            json={"prompt": {"content": "Check the conversion decline.", "mentions": [{}]}},
            headers=headers,
            params={"background": True},
        )
        assert initial.status_code == 200, initial.text
        initial_system = next(c for c in initial.json()["completions"] if c["role"] == "system")
        assert milestones["parent_held"].wait(timeout=15), "parent did not reach the held planner barrier"
        assert milestones["child_asked_question"].wait(timeout=20), "child did not ask while the parent was held"

        async def _question_wake():
            async with async_session_maker() as db:
                return (await db.execute(select(Completion).where(
                    Completion.report_id == report_id,
                    Completion.role == "user",
                    Completion.trigger_source == "sub_agent_message",
                ))).scalars().all()

        wakes = _wait_for(lambda: _run(_question_wake()), timeout_s=15, what="durable question wake")
        assert len(wakes) == 1 and wakes[0].status == "queued"
        wake_id = str(wakes[0].id)

        path = f"/api/reports/{report_id}/completions" + ("?stream=true" if streaming else "")
        user_submit = live_client.post(
            path,
            json={"queue_if_busy": True, "prompt": {"content": "Use the queued date-window follow-up.", "mentions": [{}]}},
            headers=headers,
        )
        assert user_submit.status_code == 200, user_submit.text
        if streaming:
            assert user_submit.headers["content-type"].startswith("application/json")
        queued = [c for c in user_submit.json()["completions"] if c["role"] == "user"]
        assert len(queued) == 1 and queued[0]["status"] == "queued", user_submit.json()
        queued_id = queued[0]["id"]

        async def _assert_only_original_parent_is_running():
            async with async_session_maker() as db:
                active = (await db.execute(select(Completion).where(
                    Completion.report_id == report_id,
                    Completion.role == "system",
                    Completion.status == "in_progress",
                    (Completion.trigger_source.is_(None)) | (Completion.trigger_source != "sub_agent"),
                ))).scalars().all()
                return active

        active_parent_systems = _run(_assert_only_original_parent_is_running())
        assert [str(c.id) for c in active_parent_systems] == [initial_system["id"]]

        # Releasing the held completion drains the child question wake and the
        # durable user prompt serially. The wake answers the child question;
        # the prompt still gets its own single parent turn afterward.
        release_parent.set()
        assert milestones["parent_received_question"].wait(timeout=45), "parent queue did not receive the question"
        assert milestones["child_received_answer"].wait(timeout=45), "child did not resume after queued parent answer"
        assert milestones["fan_in_seen"].wait(timeout=45), "child completion did not fan in"

        def _user_prompt_drained():
            response = live_client.get(f"/api/reports/{report_id}/completions?limit=100", headers=headers)
            assert response.status_code == 200, response.text
            rows = response.json()["completions"]
            head = next((c for c in rows if c["id"] == queued_id), None)
            reply = next((c for c in rows if c["role"] == "system" and c["parent_id"] == queued_id), None)
            return (head, reply) if head and reply and head["status"] == "success" and reply["status"] == "success" else None

        drained = _wait_for(_user_prompt_drained, timeout_s=45, what="queued user turn to drain")
        assert drained[0]["prompt"]["content"] == "Use the queued date-window follow-up."

        async def _assert_wake_dispatched_once():
            async with async_session_maker() as db:
                wake = await db.get(Completion, wake_id)
                wake_replies = (await db.execute(select(Completion).where(
                    Completion.parent_id == wake_id,
                    Completion.role == "system",
                ))).scalars().all()
                messages = (await db.execute(select(SubAgentMessage).where(
                    SubAgentMessage.report_id == report_id,
                    SubAgentMessage.kind.in_(("question", "answer")),
                ))).scalars().all()
                return wake.status, wake_replies, [(m.kind, m.reply_to) for m in messages]

        wake_state, wake_replies, messages = _wait_for(
            lambda: (lambda state: state if state[0] == "success" and state[1]
                     and state[1][0].status == "success" else None)(_run(_assert_wake_dispatched_once())),
            timeout_s=30,
            what="question wake to run exactly once",
        )
        assert wake_state == "success"
        assert len(wake_replies) == 1
        assert [kind for kind, _ in messages].count("question") == 1
        assert [kind for kind, _ in messages].count("answer") == 1
    finally:
        release_parent.set()


@pytest.mark.e2e
@pytest.mark.parametrize("can_review", [True, False])
def test_finish_preserves_records_and_reports_unread_messages(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models,
    monkeypatch, can_review,
):
    from app.ai.tools.schemas.sub_agents import FinishSubtaskInput
    from app.models.sub_agent import SubAgentRun
    from app.services.sub_agent_message_service import sub_agent_message_service
    from app.services.sub_agent_service import PendingSubAgentMessages, sub_agent_service

    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Bounded finish records")
    child, = _run(_pending_children(report["id"], org_id, count=1))
    result = FinishSubtaskInput(outcome="partial", confidence=0.7,
        summary="Verified the monthly totals; regional split remains uncertain.",
        caveats=["Regional attribution is incomplete."]).model_dump()

    async def check():
        async with async_session_maker() as db:
            await sub_agent_message_service.send(db, organization_id=org_id,
                report_id=report["id"], sender_sub_agent_id=None,
                recipient=child["sub_agent_id"], kind="direction",
                body="Include the regional split.", idempotency_key="late-direction")
            if can_review:
                with pytest.raises(PendingSubAgentMessages):
                    await sub_agent_service.record_finish(db, sub_agent_id=child["sub_agent_id"], result=result)
                await sub_agent_message_service.receive(db, organization_id=org_id,
                    report_id=report["id"], recipient=child["sub_agent_id"])
            row = await sub_agent_service.record_finish(db, sub_agent_id=child["sub_agent_id"],
                result=result, **({} if can_review else {"can_review_messages": False}))
            assert row.result_json["summary"] == result["summary"]
            assert row.result_json["caveats"][:1] == result["caveats"]
            assert len(row.result_json["caveats"]) == (1 if can_review else 2)
            messages = await sub_agent_message_service.list_for_child(db,
                organization_id=org_id, report_id=report["id"], sub_agent_id=child["sub_agent_id"])
            assert messages[0]["status"] == ("delivered" if can_review else "undelivered")
            saved = dict(row.result_json)
            replay = await sub_agent_service.record_finish(db, sub_agent_id=child["sub_agent_id"], result=result)
            assert replay.result_json == saved
            with pytest.raises(ValueError):
                await sub_agent_service.record_finish(db, sub_agent_id=child["sub_agent_id"],
                    result={**result, "summary": "A different, unrecorded finding."})
            persisted = await db.get(SubAgentRun, child["sub_agent_id"], populate_existing=True)
            assert persisted.result_json == saved
            await db.rollback()
            from app.ai.tools.implementations.finish_subtask import FinishSubtaskTool
            events = [event async for event in FinishSubtaskTool().run_stream(
                {**result, "summary": "A conflicting retry must not claim acceptance."},
                {"sub_agent_id": child["sub_agent_id"]},
            )]
            errors = [event.payload for event in events if event.type == "tool.error"]
            assert errors and errors[-1]["code"] == "FINISH_CONFLICT"
    _run(check())


@pytest.mark.e2e
def test_mailbox_locking_does_not_change_report_or_wave_activity(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    from app.models.report import Report
    from app.models.sub_agent import SubAgentRun, SubAgentWave
    from app.services.sub_agent_message_service import sub_agent_message_service

    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Stable activity timestamps")
    child, = _run(_pending_children(report["id"], org_id, count=1))
    async def check():
        async with async_session_maker() as db:
            run = await db.get(SubAgentRun, child["sub_agent_id"])
            wave = await db.get(SubAgentWave, run.wave_id)
            owner = await db.get(Report, report["id"])
            before = (owner.updated_at, owner.last_activity_at, wave.updated_at, run.updated_at)
            await sub_agent_message_service.send(db, organization_id=org_id,
                report_id=report["id"], sender_sub_agent_id=None, recipient=run.id,
                kind="direction", body="Check all available regions.", idempotency_key="activity-check")
            await sub_agent_message_service.receive(db, organization_id=org_id,
                report_id=report["id"], recipient=run.id)
            for row in (owner, wave, run):
                await db.refresh(row)
            assert (owner.updated_at, owner.last_activity_at, wave.updated_at, run.updated_at) == before
    _run(check())


@pytest.mark.e2e
@pytest.mark.parametrize("ending", ["sigkill", "error", "budget"])
def test_prepared_findings_survive_interrupted_message_review(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models,
    monkeypatch, ending,
):
    from app.ai.tools.schemas.sub_agents import FinishSubtaskInput
    from app.models.sub_agent import SubAgentRun
    from app.services.sub_agent_message_service import sub_agent_message_service
    from app.services.sub_agent_service import PendingSubAgentMessages, sub_agent_service

    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Interrupted message review")
    child, = _run(_pending_children(report["id"], org_id, count=1))
    result = FinishSubtaskInput(outcome="partial", confidence=0.6,
        summary="Returning customers account for the measured change.",
        findings=[{"claim": "The returning-customer cohort explains the difference."}]).model_dump()

    async def check():
        async with async_session_maker() as db:
            await sub_agent_message_service.send(db, organization_id=org_id,
                report_id=report["id"], sender_sub_agent_id=None,
                recipient=child["sub_agent_id"], kind="direction",
                body="Include the latest complete quarter.", idempotency_key="late-quarter")
            with pytest.raises(PendingSubAgentMessages):
                await sub_agent_service.record_finish(db, sub_agent_id=child["sub_agent_id"], result=result)
        await sub_agent_service.finalize_child(sub_agent_id=child["sub_agent_id"],
            agent_status=ending, last_text="", steps_used=3, tokens=None,
            execution_id=None, steps_exhausted=ending == "budget")
        async with async_session_maker() as db:
            row = await db.get(SubAgentRun, child["sub_agent_id"])
            assert row.result_json["summary"] == result["summary"]
            assert row.result_json["findings"] == result["findings"]
            assert row.result_json["caveats"]
            assert row.envelope_json
    _run(check())


@pytest.mark.e2e
def test_failed_parent_authorization_closes_only_its_blocked_children(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    from app.models.sub_agent import SubAgentRun, SubAgentWave
    from app.services.sub_agent_message_service import sub_agent_message_service
    from app.services.sub_agent_service import sub_agent_service
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Parent access lost")
    independent, blocked = _run(_pending_children(report["id"], org_id, count=2))

    async def check():
        async with async_session_maker() as db:
            await sub_agent_message_service.send(db, organization_id=org_id,
                report_id=report["id"], sender_sub_agent_id=blocked["sub_agent_id"],
                recipient="parent", kind="question", body="Which source is authoritative?",
                idempotency_key="needs-parent")
            child = await db.get(SubAgentRun, blocked["sub_agent_id"])
            wave = await db.get(SubAgentWave, child.wave_id)
            await sub_agent_service.fail_waiting_children_for_parent(db, report_id=report["id"],
                user_id="unrelated-principal", reason="Access revoked")
            await db.refresh(child)
            assert child.status == "pending"
            await sub_agent_service.fail_waiting_children_for_parent(db, report_id=report["id"],
                user_id=wave.user_id, reason="Access revoked")
            await db.refresh(child)
            assert child.status == "failed"
            assert child.error_message and child.result_json and child.envelope_json
            other = await db.get(SubAgentRun, independent["sub_agent_id"])
            assert other.status == "pending"
            assert not await sub_agent_message_service.has_open_questions(db, report["id"])
    _run(check())


@pytest.mark.e2e
def test_parent_mailbox_bounds_history_without_losing_live_questions(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    from app.ai.tools.schemas.sub_agents import FinishSubtaskInput
    from app.services.sub_agent_message_service import sub_agent_message_service
    from app.services.sub_agent_service import sub_agent_service
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Bounded historical context")
    ended, live = _run(_pending_children(report["id"], org_id, count=2))

    async def check():
        async with async_session_maker() as db:
            question = await sub_agent_message_service.send(db, organization_id=org_id,
                report_id=report["id"], sender_sub_agent_id=live["sub_agent_id"], recipient="parent",
                kind="question", body="Which attribution definition should I apply?", idempotency_key="older-live-question")
            for index in range(20):
                await sub_agent_message_service.send(db, organization_id=org_id,
                    report_id=report["id"], sender_sub_agent_id=ended["sub_agent_id"], recipient="parent",
                    kind="finding", body=f"Checked partition {index}.", idempotency_key=f"historical-{index}")
            await sub_agent_service.record_finish(db, sub_agent_id=ended["sub_agent_id"],
                result=FinishSubtaskInput(outcome="done", confidence=0.8, summary="Partition checks complete.").model_dump())
            context = await sub_agent_message_service.receive(db, organization_id=org_id,
                report_id=report["id"], recipient="parent")
            history = await sub_agent_message_service.list_for_child(db, organization_id=org_id,
                report_id=report["id"], sub_agent_id=ended["sub_agent_id"])
            assert len(history) == 20
            assert len([m for m in context if m["sub_agent_id"] == ended["sub_agent_id"]]) <= 8
            assert any(m["message_id"] == question["message_id"] and m["question_state"] == "open" for m in context)
    _run(check())
