"""Steering pickup contract: what the agent saves and acks when it picks a
steer up ("Send now" while it runs).

The chat renders a steer above the step that acted on it, from the saved
``steering_loop_index`` (after reload / on share links) or from the SSE ack
(live). These tests pin that both sources always agree:
- a step that will act on the steer -> saved + acked with that position;
- saving failed -> acked without a position (live falls back like a reload);
- no step will act on it (step limit) -> neither saved nor acked.

AgentV2 is built without __init__ and no LLM is involved. The contract tests
stub the stamp/SSE boundaries; the stamp tests (marked db) run the real UPDATE
and check it reports success only when a row was actually saved.
"""
import asyncio
import uuid

import pytest

from app.ai.agent_v2 import AgentV2


def _agent(stamp_ok=True):
    agent = AgentV2.__new__(AgentV2)
    agent.system_completion_id = "sys-1"
    agent._steering_pending = {}
    agent._steering_seen_ids = set()
    agent._steering_texts = []
    agent.stamps, agent.acks = [], []

    async def fake_stamp(ids, applied_at, loop_index):
        agent.stamps.append((list(ids), loop_index))
        return stamp_ok

    async def fake_emit(event):
        agent.acks.append(event)

    agent._stamp_steering_applied = fake_stamp
    agent._emit_sse_event = fake_emit
    return agent


def _collect(agent, loop_index):
    return asyncio.run(agent._collect_steering_messages(applies_to_loop_index=loop_index, poll_db=False))


@pytest.mark.parametrize("loop_index", [0, 3])
def test_pickup_is_saved_and_acked_with_the_acting_step(loop_index):
    agent = _agent()
    agent._steering_pending = {"s1": "use last quarter", "s2": "and in EUR"}

    texts = _collect(agent, loop_index)

    assert sorted(texts) == ["and in EUR", "use last quarter"]
    assert agent.stamps == [(["s1", "s2"], loop_index)]
    assert len(agent.acks) == 1
    data = agent.acks[0].data
    assert sorted(data["ids"]) == ["s1", "s2"]
    assert data["loop_index"] == loop_index
    assert data["applied_at"]


def test_ack_carries_no_position_when_saving_failed():
    agent = _agent(stamp_ok=False)
    agent._steering_pending = {"s1": "use last quarter"}

    _collect(agent, 2)

    assert len(agent.acks) == 1
    assert agent.acks[0].data["ids"] == ["s1"]
    assert "loop_index" not in agent.acks[0].data
    assert "applied_at" not in agent.acks[0].data


def test_no_acting_step_leaves_the_steer_pending():
    agent = _agent()
    agent._steering_pending = {"s1": "use last quarter"}

    texts = _collect(agent, None)

    # The planner still sees it, but nothing claims it was applied.
    assert texts == ["use last quarter"]
    assert agent.stamps == []
    assert agent.acks == []


def test_a_steer_is_picked_up_once():
    agent = _agent()
    agent._steering_pending = {"s1": "use last quarter"}
    _collect(agent, 1)
    agent._steering_pending = {"s1": "use last quarter"}  # re-announced by the bus

    assert _collect(agent, 2) == []
    assert agent.stamps == [(["s1"], 1)]
    assert len(agent.acks) == 1


# --- the real stamp, against a database -------------------------------------

async def _seed_steer(steer_id):
    from app.dependencies import async_session_maker
    from app.models.completion import Completion
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User

    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Steer Org {suffix}")
        db.add(org)
        await db.flush()
        user = User(name="Steer User", email=f"steer-{suffix}@example.com", hashed_password="x",
                    is_active=True, is_superuser=False, is_verified=True)
        db.add(user)
        await db.flush()
        report = Report(title="Steer", slug=f"steer-{suffix}", status="draft",
                        user_id=user.id, organization_id=org.id)
        db.add(report)
        await db.flush()
        system = Completion(prompt=None, completion={"content": ""}, status="in_progress",
                            role="system", report_id=report.id, turn_index=0)
        db.add(system)
        await db.flush()
        db.add(Completion(id=steer_id, prompt={"content": "use last quarter"}, completion=None,
                          status="success", role="user", message_type="steering",
                          parent_id=system.id, report_id=report.id, user_id=user.id, turn_index=1))
        await db.commit()
        return str(system.id)


def _db_agent():
    from app.dependencies import async_session_maker

    agent = AgentV2.__new__(AgentV2)
    agent._session_maker = async_session_maker
    agent._writes = None
    agent._use_single_write_session = lambda: False
    return agent


async def _read_stamp(steer_id):
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.completion import Completion

    async with async_session_maker() as db:
        row = (await db.execute(
            select(Completion.steering_applied_at, Completion.steering_loop_index)
            .where(Completion.id == steer_id)
        )).one()
        return row.steering_applied_at, row.steering_loop_index


@pytest.mark.db
def test_stamp_saves_the_position_and_reports_success():
    from datetime import datetime

    steer_id = str(uuid.uuid4())

    async def run():
        await _seed_steer(steer_id)
        agent = _db_agent()
        saved = await agent._stamp_steering_applied([steer_id], datetime(2026, 1, 1, 10, 0, 0), 2)
        return saved, await _read_stamp(steer_id)

    saved, (applied_at, loop_index) = asyncio.run(run())
    assert saved is True
    assert loop_index == 2
    assert applied_at is not None


@pytest.mark.db
def test_stamp_that_matches_no_row_reports_unsaved():
    """The steer row is not visible yet (its insert has not committed) or is
    not a steering row: nothing is updated, so nothing may be acked as saved."""
    from datetime import datetime

    async def run():
        await _seed_steer(str(uuid.uuid4()))
        agent = _db_agent()
        return await agent._stamp_steering_applied([str(uuid.uuid4())], datetime(2026, 1, 1), 1)

    assert asyncio.run(run()) is False
