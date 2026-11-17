"""/clear — a fresh agent context that deletes nothing.

What /clear promises (BOW-48):
  - nothing at or before the clear boundary reaches the agent: no detailed
    turns, no rolling summary, and not the protected opening exchange
  - turns after it render normally, and the fresh context protects ITS OWN
    opening exchange (once the window overflows or a later compaction runs)
  - no completion is deleted or added; the boundary is state, one entry per
    clear, anchored on a visible row for the transcript divider
  - compaction stats restart from zero; a later compaction never quotes
    pre-clear content as the "opening request"
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

# Shared fixtures/helpers (names only — never import test_* functions, pytest
# would collect them twice).
from tests.unit.test_context_compaction import (  # noqa: F401 — `db` is a fixture
    SUMMARY,
    _add_turns,
    _mock_llm,
    _model,
    _seed_report,
    _state,
    _svc,
    db,
)
from app.models.completion import Completion
from app.models.organization import Organization
from app.ai.context.session_events import EVENT_ROLE

pytestmark = pytest.mark.db

OLD = "Old topic churn ask {i}"
NEW = "New topic pricing ask {i}"


async def _org(db, org):
    return (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()


async def _render(db, org, report, max_messages=40):
    from app.ai.context.builders.message_context_builder import MessageContextBuilder
    builder = MessageContextBuilder(db, await _org(db, org), report)
    section = await builder.build(max_messages=max_messages)
    text = await builder.build_context(max_messages=max_messages)
    return section, section.render(), text


def _assert_no_old_topic(*rendered: str):
    for r in rendered:
        assert "Old topic" not in r
        assert "<history_summary>" not in r
        assert "Track monthly revenue" not in r  # the pre-clear summary's goal


@pytest.mark.asyncio
@pytest.mark.parametrize("n_old", [2, 7, 30, 57])
async def test_clear_hides_every_earlier_turn_from_the_agent(db, n_old):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, n_old, content=OLD)
    result = await _svc().clear(db, report)
    assert result["status"] == "cleared"

    await _add_turns(db, report, user, 4, start=n_old + 10, content=NEW)
    section, rendered, text = await _render(db, org, report)

    _assert_no_old_topic(rendered, text)
    assert "<opening_exchange>" not in rendered
    assert f"New topic pricing ask {n_old + 10}" in rendered
    assert f"Answer {n_old + 13}" in rendered
    assert section.dropped_count == 0


@pytest.mark.asyncio
async def test_clear_after_compaction_drops_the_summary_and_stats(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 30, content=OLD)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        compacted = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert compacted["status"] == "compacted"

    result = await _svc().clear(db, report)
    assert result["status"] == "cleared"
    assert result["tokens_compacted_total"] == 0 and result["covered_turns"] == 0
    state = await _state(db, report.id)
    assert state.summary_json == {}

    await _add_turns(db, report, user, 2, start=100, content=NEW)
    _, rendered, text = await _render(db, org, report)
    _assert_no_old_topic(rendered, text)
    assert "New topic pricing ask 100" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("n_new", [42, 57])
async def test_overflowing_fresh_context_protects_its_own_opening_ask(db, n_new):
    """The live window's row cap pins the opening exchange; after /clear that
    must be the first post-clear ask, never the report's very first prompt."""
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 6, content=OLD)
    await _svc().clear(db, report)
    await _add_turns(db, report, user, n_new, start=50, content=NEW)

    section, rendered, text = await _render(db, org, report)
    assert section.dropped_count > 0
    _assert_no_old_topic(rendered, text)
    opening = rendered.split("<opening_exchange>")[1].split("</opening_exchange>")[0]
    assert "New topic pricing ask 50" in opening


@pytest.mark.asyncio
async def test_compaction_after_clear_never_folds_or_quotes_pre_clear_turns(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 10, content=OLD)
    await _svc().clear(db, report)
    await _add_turns(db, report, user, 30, start=50, content=NEW)

    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert result["status"] == "compacted"
    prompt = instance.inference.call_args[0][0]
    assert "Old topic" not in prompt
    # The fresh context's opening exchange is protected, not folded.
    assert "New topic pricing ask 50" not in prompt
    state = await _state(db, report.id)
    assert state.summary_json["opening_request"].startswith("New topic pricing ask 50")

    _, rendered, text = await _render(db, org, report)
    assert "Old topic" not in rendered and "Old topic" not in text
    opening = rendered.split("<opening_exchange>")[1].split("</opening_exchange>")[0]
    assert "New topic pricing ask 50" in opening


@pytest.mark.asyncio
async def test_clear_deletes_nothing_and_records_one_boundary_per_clear(db):
    org, report, user = await _seed_report(db)
    first = await _add_turns(db, report, user, 4, content=OLD)

    async def _count():
        return (await db.execute(
            select(func.count(Completion.id)).where(Completion.report_id == str(report.id))
        )).scalar()

    before = await _count()
    r1 = await _svc().clear(db, report)
    assert await _count() == before
    assert [b["completion_id"] for b in r1["clear_boundaries"]] == [str(first[-1].id)]

    # Clearing again with nothing new is a no-op, not a second boundary.
    again = await _svc().clear(db, report)
    assert again["status"] == "nothing_to_clear"
    assert len(again["clear_boundaries"]) == 1

    second = await _add_turns(db, report, user, 2, start=30, content=NEW)
    r2 = await _svc().clear(db, report)
    assert [b["completion_id"] for b in r2["clear_boundaries"]] == [str(first[-1].id), str(second[-1].id)]
    assert all(b.get("cleared_at") for b in r2["clear_boundaries"])


@pytest.mark.asyncio
async def test_clear_on_an_empty_report_is_a_no_op(db):
    _, report, _ = await _seed_report(db)
    result = await _svc().clear(db, report)
    assert result["status"] == "nothing_to_clear"
    assert await _state(db, report.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("trailing", ["event", "sub_agent"])
async def test_boundary_cuts_after_trailing_rows_but_divider_anchors_on_a_visible_turn(db, trailing):
    """A silent event or sub-agent row newer than the last reply must still
    fall behind the boundary, while the divider sits after the visible reply."""
    org, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 4, content=OLD)
    extra = Completion(
        prompt={"content": "Old topic event: widget edited"},
        completion={"content": "Old topic sub-agent notes"} if trailing == "sub_agent" else {},
        status="success",
        model="test",
        role=EVENT_ROLE if trailing == "event" else "system",
        message_type="widget_edited" if trailing == "event" else "ai_completion",
        trigger_source="sub_agent" if trailing == "sub_agent" else None,
        report_id=str(report.id),
    )
    db.add(extra)
    await db.flush()
    extra.created_at = turns[-1].created_at + timedelta(seconds=30)
    await db.commit()

    result = await _svc().clear(db, report)
    boundary = result["clear_boundaries"][-1]
    assert boundary["completion_id"] == str(extra.id)
    assert boundary["anchor_completion_id"] == str(turns[-1].id)

    await _add_turns(db, report, user, 2, start=60, content=NEW)
    _, rendered, text = await _render(db, org, report)
    _assert_no_old_topic(rendered, text)


@pytest.mark.asyncio
async def test_instruction_matching_ignores_pre_clear_prompts(db):
    from app.ai.context.context_hub import ContextHub
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 6, content=OLD)
    await _svc().clear(db, report)
    await _add_turns(db, report, user, 2, start=40, content=NEW)

    hub = ContextHub.__new__(ContextHub)  # only _instruction_query is exercised
    hub.db, hub.report = db, report
    combined = await hub._instruction_query("what about discounts?")
    assert "Old topic" not in combined
    assert "New topic pricing ask 40" in combined and "discounts" in combined


def _row(report, *, role, content, status="success", message_type="ai_completion"):
    return Completion(
        prompt={"content": content} if role != "system" else {},
        completion={"content": content} if role == "system" else {},
        status=status, model="test", role=role, message_type=message_type,
        report_id=str(report.id),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("n_queued", [1, 3])
async def test_a_prompt_queued_before_clear_is_part_of_the_fresh_context(db, n_queued):
    """Queued prompts run AFTER /clear, so they belong to the fresh context —
    even when the run wrote a later row (here a session event) after they were
    queued, which makes that row the boundary."""
    org, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 4, content=OLD)
    queued = [_row(report, role="user", content=f"New topic queued ask {i}", status="queued") for i in range(n_queued)]
    event = _row(report, role=EVENT_ROLE, content="Old topic event: widget edited", message_type="widget_edited")
    db.add_all([*queued, event])
    await db.flush()
    for i, q in enumerate(queued):
        q.created_at = turns[-1].created_at + timedelta(seconds=10 + i)
    event.created_at = turns[-1].created_at + timedelta(seconds=30)
    await db.commit()

    result = await _svc().clear(db, report)
    assert result["status"] == "cleared"
    assert result["clear_boundaries"][-1]["completion_id"] == str(event.id)

    # The dispatcher starts each queued prompt in the order it was queued.
    for q in queued:
        await db.refresh(q)
    assert [q.created_at for q in queued] == sorted(q.created_at for q in queued)
    for q in queued:
        q.status = "success"
    # The turn being answered is not part of the rendered history, so a later
    # ask stands in for it.
    current = _row(report, role="user", content="New topic current ask")
    db.add(current)
    await db.flush()
    current.created_at = queued[-1].created_at + timedelta(minutes=1)
    await db.commit()

    _, rendered, text = await _render(db, org, report)
    _assert_no_old_topic(rendered, text)
    for i in range(n_queued):
        assert f"New topic queued ask {i}" in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("age_s, expected", [(1, "busy"), (3600, "cleared")])
async def test_clear_waits_for_a_run_that_is_just_starting(db, age_s, expected):
    """A prompt sent while /clear is in flight has its rows before its agent
    execution: clearing then would put the boundary on that very turn. A long
    stale in_progress row (a crashed run) must not block /clear forever."""
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 4, content=OLD)
    ask = _row(report, role="user", content="New topic ask sent during clear")
    reply = _row(report, role="system", content="", status="in_progress")
    db.add_all([ask, reply])
    await db.flush()
    started = datetime.utcnow() - timedelta(seconds=age_s)
    ask.created_at, reply.created_at = started, started + timedelta(milliseconds=1)
    await db.commit()

    result = await _svc().clear(db, report)
    assert result["status"] == expected
    assert bool(result["clear_boundaries"]) == (expected == "cleared")


@pytest.mark.asyncio
async def test_clear_drops_the_pre_clear_token_anchor(db):
    """The anchor calibrates the next run from the last prompt's real size;
    after /clear that prompt no longer describes the context."""
    from app.services.context_compaction_service import ContextCompactionService
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 6, content=OLD)
    await ContextCompactionService.record_prompt_anchor(db, str(report.id), 90_000, 60_000)
    assert ContextCompactionService.anchor_offset(await _state(db, report.id)) == 30_000

    await _svc().clear(db, report)
    assert ContextCompactionService.anchor_offset(await _state(db, report.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["boundary_row_gone", "written_before_the_timestamp"])
async def test_the_cutoff_survives_without_the_boundary_row_and_reads_older_entries(db, entry):
    """The cutoff is stored on the boundary entry, so it holds when the
    boundary completion can no longer be found; entries written before the
    timestamp was stored still resolve through the row."""
    from app.services.context_compaction_service import ContextCompactionService
    org, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 6, content=OLD)
    await _svc().clear(db, report)

    state = await _state(db, report.id)
    boundary = dict(state.clear_boundaries[-1])
    if entry == "boundary_row_gone":
        boundary["completion_id"] = str(uuid.uuid4())
    else:
        boundary.pop("completion_created_at")
    state.clear_boundaries = [boundary]
    await db.commit()

    cutoff = await ContextCompactionService.clear_boundary_created_at(db, str(report.id))
    assert cutoff == turns[-1].created_at
    await _add_turns(db, report, user, 50, start=60, content=NEW)
    section, rendered, text = await _render(db, org, report)
    assert section.dropped_count > 0
    _assert_no_old_topic(rendered, text)


@pytest.mark.asyncio
async def test_clear_boundaries_are_capped_keeping_the_newest(db):
    from app.services.context_compaction_service import MAX_CLEAR_BOUNDARIES
    org, report, user = await _seed_report(db)
    last = None
    for i in range(MAX_CLEAR_BOUNDARIES + 3):
        last = (await _add_turns(db, report, user, 2, start=10 * i, content=NEW))[-1]
        result = await _svc().clear(db, report)
        assert result["status"] == "cleared"
    assert len(result["clear_boundaries"]) == MAX_CLEAR_BOUNDARIES
    assert result["clear_boundaries"][-1]["completion_id"] == str(last.id)
