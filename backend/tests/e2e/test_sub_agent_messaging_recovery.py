"""Recovery paths for parent/child sub-agent messaging.

Each test here encodes a contract the question/answer flow promises when
something goes wrong around it: a child that was answered but has no slot,
a parent wake that fails for a runtime reason, a wave without a principal,
a finish replay after its note disappeared, and the queue dispatcher
retiring abandoned turns. Real fixtures, real services, real DB; only the
LLM boundary is ever stubbed.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.dependencies import async_session_maker
from tests.e2e.test_sub_agent_messages import _pending_children, live_client  # noqa: F401  # fixture re-export
from tests.e2e.test_sub_agents import _env


def _run(coro):
    return asyncio.run(coro)


async def _ask(db, *, org_id, report_id, child_id, body="Which window?", key=None):
    from app.services.sub_agent_message_service import sub_agent_message_service
    return await sub_agent_message_service.send(
        db, organization_id=org_id, report_id=report_id, sender_sub_agent_id=child_id,
        recipient="parent", kind="question", body=body, idempotency_key=key or f"q-{uuid.uuid4().hex[:8]}",
    )


async def _answer(db, *, org_id, report_id, child_id, question_id, body="Last 14 days."):
    from app.services.sub_agent_message_service import sub_agent_message_service
    return await sub_agent_message_service.send(
        db, organization_id=org_id, report_id=report_id, sender_sub_agent_id=None,
        recipient=child_id, kind="answer", body=body, reply_to=question_id,
        idempotency_key=f"a-{uuid.uuid4().hex[:8]}",
    )


async def _wakes(db, report_id, *, role="user"):
    from app.models.completion import Completion
    return (await db.execute(select(Completion).where(
        Completion.report_id == report_id, Completion.role == role,
        Completion.trigger_source == "sub_agent_message",
    ).order_by(Completion.turn_index))).scalars().all()


# ── answered child without a slot ──────────────────────────────────────────

@pytest.mark.e2e
def test_answered_child_waiting_for_a_slot_is_not_reported_as_needing_an_answer(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    """`status='waiting'` covers two different states. Once the parent has
    answered, every surface must say the child waits for a slot, not for the
    parent — otherwise the parent re-answers and the send is rejected."""
    from app.models.sub_agent import SubAgentRun
    from app.services.sub_agent_service import sub_agent_service
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Waiting for a slot")
    (child,) = _run(_pending_children(report["id"], org_id, count=1))

    async def check():
        async with async_session_maker() as db:
            # Only the child's own run loop moves a row to 'waiting'; there is
            # no API that produces that state without an LLM, so set it here.
            await db.execute(update(SubAgentRun).where(SubAgentRun.id == child["sub_agent_id"])
                             .values(status="waiting", started_at=datetime.utcnow()))
            await db.commit()
            question = await _ask(db, org_id=org_id, report_id=report["id"], child_id=child["sub_agent_id"])
            before = await sub_agent_service.snapshot(db, child["sub_agent_id"], report_id=report["id"])
            assert before["status"] == "waiting" and before["waiting_for"] == "answer"
            listed = await sub_agent_service.list_for_report(db, report["id"])
            assert listed["background_tasks"][0]["waiting_for"] == "answer"

            await _answer(db, org_id=org_id, report_id=report["id"], child_id=child["sub_agent_id"],
                          question_id=question["message_id"])
            after = await sub_agent_service.snapshot(db, child["sub_agent_id"], report_id=report["id"])
            assert after["status"] == "waiting" and after["waiting_for"] == "slot"
            assert after.get("elapsed_s") is not None, "a blocked child must still report how long it has run"
            listed = await sub_agent_service.list_for_report(db, report["id"])
            assert listed["background_tasks"][0]["waiting_for"] == "slot"
    _run(check())


@pytest.mark.e2e
def test_read_sub_agent_tells_the_parent_an_answered_child_waits_for_a_slot(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    from app.ai.tools.implementations.read_sub_agent import ReadSubAgentTool
    from app.models.report import Report
    from app.models.sub_agent import SubAgentRun
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="read_sub_agent while waiting")
    (child,) = _run(_pending_children(report["id"], org_id, count=1))

    async def read(db):
        events = []
        async for ev in ReadSubAgentTool().run_stream(
            {"sub_agent_id": child["sub_agent_id"]},
            {"db": db, "report": await db.get(Report, report["id"])},
        ):
            events.append(ev)
        end = [e for e in events if e.type == "tool.end"]
        assert end, [e.payload for e in events]
        return end[-1].payload["observation"]["summary"].lower()

    async def check():
        async with async_session_maker() as db:
            await db.execute(update(SubAgentRun).where(SubAgentRun.id == child["sub_agent_id"])
                             .values(status="waiting", started_at=datetime.utcnow()))  # see test above
            await db.commit()
            question = await _ask(db, org_id=org_id, report_id=report["id"], child_id=child["sub_agent_id"])
            assert "answer" in await read(db)
            await _answer(db, org_id=org_id, report_id=report["id"], child_id=child["sub_agent_id"],
                          question_id=question["message_id"])
            summary = await read(db)
            assert "slot" in summary and "answer it" not in summary, summary
    _run(check())


# ── parent wake failures ───────────────────────────────────────────────────

async def _dispatch_queued_wake(report_id):
    """Run the queue dispatcher's agent entry point on the pending wake, the
    way start_next_queued_if_idle does once it claims the head."""
    from app.models.completion import Completion
    from app.services.completion_service import CompletionService
    async with async_session_maker() as db:
        (head,) = await _wakes(db, report_id)
        head.status = "success"
        system_row = Completion(
            prompt=None, completion={"content": ""}, model=head.model, user_id=head.user_id,
            trigger_source=head.trigger_source, report_id=head.report_id, parent_id=head.id,
            turn_index=head.turn_index + 1, message_type="table", role="system", status="in_progress",
        )
        db.add(system_row)
        await db.commit()
        head_id, system_id = str(head.id), str(system_row.id)
    await CompletionService()._run_dispatched_agent(report_id, head_id, system_id)
    return head_id, system_id


@pytest.mark.e2e
@pytest.mark.parametrize("failure", ["no_model", "provider_error"])
def test_any_failed_question_wake_releases_the_blocked_children(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch, failure,
):
    """A question wake that cannot run for *any* reason must not leave the
    asking child idle until the wave deadline. The child whose question is
    open ends with the failure; siblings without an open question continue."""
    from app.models.completion import Completion
    from app.models.sub_agent import SubAgentRun
    from app.services.completion_service import CompletionService
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title=f"Wake fails: {failure}")
    asking, independent = _run(_pending_children(report["id"], org_id, count=2))

    if failure == "no_model":
        async def no_model(self, *a, **kw):
            return None, None, None
        monkeypatch.setattr(CompletionService, "_resolve_completion_models", no_model)
    else:
        from app.ai.agent_v2 import AgentV2

        async def boom(self, *a, **kw):
            raise RuntimeError("provider returned 529 overloaded")
        monkeypatch.setattr(AgentV2, "main_execution", boom)

    async def check():
        async with async_session_maker() as db:
            await _ask(db, org_id=org_id, report_id=report["id"], child_id=asking["sub_agent_id"])
        _, system_id = await _dispatch_queued_wake(report["id"])
        async with async_session_maker() as db:
            system_row = await db.get(Completion, system_id)
            assert system_row.status == "error", system_row.completion
            child = await db.get(SubAgentRun, asking["sub_agent_id"])
            assert child.status == "failed", (child.status, child.error_message)
            assert child.error_message and child.result_json and child.envelope_json
            other = await db.get(SubAgentRun, independent["sub_agent_id"])
            assert other.status == "pending"
            (event,) = await _wakes(db, report["id"], role="external")
            assert event.status == "error"
    _run(check())


# ── the wake is a visible machine turn ─────────────────────────────────────

@pytest.mark.e2e
def test_question_wake_is_a_visible_machine_event_with_a_hidden_trigger(
    live_client, create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    """Same three-row idiom as every other machine turn: the timeline shows an
    event entry while the hidden trigger stays out of it, and an answered-
    elsewhere wake closes its event when the dispatcher discards it."""
    from app.services.completion_service import CompletionService
    report, headers, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Visible question wake")
    child_a, child_b = _run(_pending_children(report["id"], org_id, count=2))

    async def ask_both():
        async with async_session_maker() as db:
            qa = await _ask(db, org_id=org_id, report_id=report["id"], child_id=child_a["sub_agent_id"])
            qb = await _ask(db, org_id=org_id, report_id=report["id"], child_id=child_b["sub_agent_id"])
            triggers = await _wakes(db, report["id"])
            events = await _wakes(db, report["id"], role="external")
            assert len(triggers) == 1 and triggers[0].status == "queued"
            assert len(events) == 1 and events[0].status == "in_progress", "one coalesced wake → one event"
            assert events[0].message_type == triggers[0].message_type == "sub_agent_question"
            assert (events[0].prompt or {}).get("meta", {}).get("sub_agent_id") == child_a["sub_agent_id"]
            return qa, qb
    qa, qb = _run(ask_both())

    listing = live_client.get(f"/api/reports/{report['id']}/completions", headers=headers)
    assert listing.status_code == 200, listing.text
    rows = listing.json()["completions"]
    machine = [c for c in rows if c.get("trigger_source") == "sub_agent_message"]
    assert [c["role"] for c in machine] == ["external"], machine

    async def answer_then_dispatch():
        async with async_session_maker() as db:
            for child, q in ((child_a, qa), (child_b, qb)):
                await _answer(db, org_id=org_id, report_id=report["id"], child_id=child["sub_agent_id"],
                              question_id=q["message_id"])
        await CompletionService().start_next_queued_if_idle(report["id"])
        async with async_session_maker() as db:
            (trigger,) = await _wakes(db, report["id"])
            (event,) = await _wakes(db, report["id"], role="external")
            assert trigger.status == "cancelled"
            assert event.status == "success", "a discarded wake must not keep its event spinning"
    _run(answer_then_dispatch())


# ── waves without a principal ──────────────────────────────────────────────

@pytest.mark.e2e
def test_wave_without_a_principal_still_coalesces_wakes_and_releases_its_children(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    from app.models.sub_agent import SubAgentRun, SubAgentWave
    from app.services.sub_agent_service import sub_agent_service
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Wave with no principal")
    child_a, child_b = _run(_pending_children(report["id"], org_id, count=2))

    async def check():
        async with async_session_maker() as db:
            row = await db.get(SubAgentRun, child_a["sub_agent_id"])
            # The column is nullable and the dispatcher handles a missing
            # principal explicitly; nothing in the API produces it, so set it.
            await db.execute(update(SubAgentWave).where(SubAgentWave.id == row.wave_id).values(user_id=None))
            await db.commit()
            await _ask(db, org_id=org_id, report_id=report["id"], child_id=child_a["sub_agent_id"])
            await _ask(db, org_id=org_id, report_id=report["id"], child_id=child_b["sub_agent_id"])
            assert len(await _wakes(db, report["id"])) == 1, "one principal (even none) → one queued wake"

            await sub_agent_service.fail_waiting_children_for_parent(
                db, report_id=report["id"], user_id=None, reason="no principal")
            for child in (child_a, child_b):
                await db.refresh(row := await db.get(SubAgentRun, child["sub_agent_id"]))
                assert row.status == "failed", (child["title"], row.status)
    _run(check())


# ── finish replay after the note is gone ───────────────────────────────────

@pytest.mark.e2e
def test_finish_replay_is_idempotent_after_its_note_disappears(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    from app.ai.tools.schemas.sub_agents import FinishSubtaskInput
    from app.models.note import Note
    from app.services.sub_agent_service import sub_agent_service
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Finish replay")
    (child,) = _run(_pending_children(report["id"], org_id, count=1))

    async def check():
        async with async_session_maker() as db:
            note = Note(report_id=report["id"], organization_id=org_id, title="Write-up", content="numbers")
            db.add(note)
            await db.commit()
            record = FinishSubtaskInput(outcome="done", confidence=0.8, summary="The question is settled by the data.",
                                        report_note_id=str(note.id)).model_dump()
            first = await sub_agent_service.record_finish(db, sub_agent_id=child["sub_agent_id"], result=record)
            assert first.status == "finished" and first.report_note_id == str(note.id)
            await db.delete(note)
            await db.commit()
            # The tool retries with the identical call: it must be accepted,
            # not reported as a conflicting second record.
            again = await sub_agent_service.record_finish(db, sub_agent_id=child["sub_agent_id"], result=record)
            assert again.status == "finished" and again.exit_reason == first.exit_reason
            different = {**record, "summary": "A different conclusion."}
            with pytest.raises(ValueError):
                await sub_agent_service.record_finish(db, sub_agent_id=child["sub_agent_id"], result=different)
    _run(check())


# ── stale turn recovery in the dispatcher ──────────────────────────────────

@pytest.mark.e2e
def test_dispatcher_retires_abandoned_turns_even_while_another_turn_is_live(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    from app.models.completion import Completion
    from app.services import completion_service as cs
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="Stale turn recovery")

    async def check():
        async with async_session_maker() as db:
            stale_at = datetime.utcnow() - timedelta(seconds=cs._COMPLETION_STALE_AFTER_SECONDS * 4)
            # Two in-progress system rows: a worker that died long ago and a
            # live run. Only a crash leaves this shape, so build it directly.
            stale = Completion(report_id=report["id"], role="system", status="in_progress",
                               completion={"content": ""}, turn_index=1, message_type="table",
                               created_at=stale_at, updated_at=stale_at)
            live = Completion(report_id=report["id"], role="system", status="in_progress",
                              completion={"content": ""}, turn_index=3, message_type="table")
            db.add_all([stale, live])
            await db.commit()
            stale_id, live_id = str(stale.id), str(live.id)
        await cs.CompletionService().start_next_queued_if_idle(report["id"])
        async with async_session_maker() as db:
            assert (await db.get(Completion, live_id)).status == "in_progress"
            retired = await db.get(Completion, stale_id)
            assert retired.status == "error", "stale recovery must survive the early return"
            assert (retired.completion or {}).get("error_type") == "stale_execution"
    _run(check())


# ── no mailbox work without delegations ────────────────────────────────────

@pytest.mark.e2e
def test_parent_planning_skips_mailbox_queries_until_the_report_has_delegated(
    create_user, login_user, whoami, create_report, create_llm_provider_and_models, monkeypatch,
):
    from app.services.sub_agent_service import sub_agent_service
    report, _, org_id, _ = _env(create_user, login_user, whoami, create_report,
        create_llm_provider_and_models, monkeypatch, title="No waves yet")

    async def check():
        async with async_session_maker() as db:
            assert await sub_agent_service.has_waves(db, report["id"]) is False
        await _pending_children(report["id"], org_id, count=1)
        async with async_session_maker() as db:
            assert await sub_agent_service.has_waves(db, report["id"]) is True
    _run(check())
