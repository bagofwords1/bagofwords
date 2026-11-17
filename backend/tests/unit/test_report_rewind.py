"""Rewind — go back to an earlier prompt and remove everything after it (BOW-145).

What rewind promises:
  - the chosen prompt and every later completion leave the agent's history
    window, wherever the rewind point is, and stop matching any ordinary read
    of the report's completions; the rows still exist (soft delete)
  - turns before the rewind point are untouched and still reach the agent
  - a compaction summary that folded removed turns is dropped (no stale
    summary); one that ends before the rewind point is kept
  - a /clear boundary on a removed row is dropped; an earlier one still holds
  - only prompts the user sent are rewind points; blocked while a run is active
  - widgets built in removed turns survive (dashboards keep working)
"""
from __future__ import annotations

import pytest
from sqlalchemy import func, select

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
from tests.unit.test_context_clear import _render
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion, INCLUDE_DELETED_COMPLETIONS
from app.services.report_rewind_service import ReportRewindService, RewindError

pytestmark = pytest.mark.db

ASK = "Ask number {i} about revenue"


async def _visible_ids(db, report):
    return {str(r) for r in (await db.execute(
        select(Completion.id).where(Completion.report_id == str(report.id))
    )).scalars().all()}


async def _all_count(db, report):
    return (await db.execute(
        select(func.count(Completion.id))
        .where(Completion.report_id == str(report.id))
        .execution_options(**{INCLUDE_DELETED_COMPLETIONS: True})
    )).scalar()


@pytest.mark.asyncio
@pytest.mark.parametrize("n_turns,rewind_at", [(4, 0), (6, 2), (10, 4), (40, 30), (40, 38)])
async def test_rewind_removes_the_prompt_and_everything_after_it(db, n_turns, rewind_at):
    org, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, n_turns, content=ASK)
    target = turns[rewind_at]
    assert target.role == "user"

    result = await ReportRewindService().rewind(db, report, str(target.id))

    assert result["status"] == "rewound"
    assert result["removed_count"] == n_turns - rewind_at
    assert result["prompt"] == ASK.format(i=rewind_at)
    kept, removed = turns[:rewind_at], turns[rewind_at:]
    assert await _visible_ids(db, report) == {str(c.id) for c in kept}
    # Soft delete: the rows are still there for an audit.
    assert await _all_count(db, report) == n_turns

    _, rendered, text = await _render(db, org, report)
    for c in removed:
        if c.role == "user":
            assert f"Ask number {turns.index(c)} about" not in rendered
            assert f"Ask number {turns.index(c)} about" not in text
    for c in kept:
        if c.role == "user":
            assert f"Ask number {turns.index(c)} about" in rendered


@pytest.mark.asyncio
async def test_rewound_rows_are_hidden_from_relationship_loads_too(db):
    _, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 6, content=ASK)
    kept = {str(turns[0].id), str(turns[1].id)}
    await ReportRewindService().rewind(db, report, str(turns[2].id))
    from app.models.report import Report
    # Report.completions is a selectin relationship: reload it from the DB.
    fresh = (await db.execute(
        select(Report).where(Report.id == str(report.id)).execution_options(populate_existing=True)
    )).scalar_one()
    assert {str(c.id) for c in fresh.completions} == kept


@pytest.mark.asyncio
async def test_rewind_past_the_compaction_watermark_drops_the_stale_summary(db):
    org, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 30, content=ASK)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        compacted = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    watermark = compacted["covers_until_completion_id"]
    idx = next(i for i, c in enumerate(turns) if str(c.id) == watermark)
    # Rewind to a user prompt at or before the watermark.
    target = next(c for c in reversed(turns[: idx + 1]) if c.role == "user")

    result = await ReportRewindService().rewind(db, report, str(target.id))

    assert result["compaction"]["covers_until_completion_id"] is None
    assert result["compaction"]["tokens_compacted_total"] == 0
    state = await _state(db, report.id)
    assert state.summary_json == {}
    _, rendered, text = await _render(db, org, report)
    assert "<history_summary>" not in rendered
    assert "Track monthly revenue" not in rendered and "Track monthly revenue" not in text
    # Every surviving turn is detailed context again.
    for i, c in enumerate(turns[: turns.index(target)]):
        if c.role == "user":
            assert f"Ask number {i} about" in rendered


@pytest.mark.asyncio
async def test_rewind_after_the_watermark_keeps_the_summary(db):
    org, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 30, content=ASK)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        compacted = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    watermark = compacted["covers_until_completion_id"]
    target = turns[-2]  # the newest prompt, well past the watermark
    assert target.role == "user"

    result = await ReportRewindService().rewind(db, report, str(target.id))

    assert result["compaction"]["covers_until_completion_id"] == watermark
    state = await _state(db, report.id)
    assert state.summary_json
    _, rendered, _ = await _render(db, org, report)
    assert "<history_summary>" in rendered
    assert f"Ask number {len(turns) - 2} about" not in rendered


@pytest.mark.asyncio
async def test_clear_boundary_on_a_removed_turn_is_dropped_and_an_earlier_one_holds(db):
    org, report, user = await _seed_report(db)
    old = await _add_turns(db, report, user, 4, content="Old topic churn ask {i}")
    first_clear = await _svc().clear(db, report)
    mid = await _add_turns(db, report, user, 4, start=20, content=ASK)
    await _svc().clear(db, report)
    await _add_turns(db, report, user, 2, start=40, content="Later ask {i}")

    result = await ReportRewindService().rewind(db, report, str(mid[2].id))

    boundaries = result["compaction"]["clear_boundaries"]
    assert [b["completion_id"] for b in boundaries] == [first_clear["clear_boundaries"][0]["completion_id"]]
    _, rendered, text = await _render(db, org, report)
    assert "Old topic" not in rendered and "Old topic" not in text
    assert "Later ask" not in rendered
    assert "Ask number 22 about" not in rendered
    assert "Ask number 20 about" in rendered


@pytest.mark.asyncio
async def test_rewinding_past_every_clear_restores_the_earlier_history(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 4, content="Early ask {i}")
    after = await _add_turns(db, report, user, 2, start=20, content=ASK)
    await _svc().clear(db, report)  # boundary sits on after[-1]

    result = await ReportRewindService().rewind(db, report, str(after[0].id))

    assert result["compaction"]["clear_boundaries"] == []
    assert result["compaction"]["covers_until_completion_id"] is None
    _, rendered, _ = await _render(db, org, report)
    assert "Early ask 0" in rendered and "Early ask 2" in rendered


@pytest.mark.asyncio
async def test_only_a_sent_prompt_is_a_rewind_point(db):
    _, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 4, content=ASK)
    with pytest.raises(RewindError) as e:
        await ReportRewindService().rewind(db, report, str(turns[1].id))  # an agent reply
    assert e.value.status_code == 400
    with pytest.raises(RewindError) as e:
        await ReportRewindService().rewind(db, report, "00000000-0000-0000-0000-000000000000")
    assert e.value.status_code == 404
    assert len(await _visible_ids(db, report)) == 4


@pytest.mark.asyncio
async def test_rewind_is_blocked_while_a_run_is_active(db):
    org, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 4, content=ASK)
    db.add(AgentExecution(
        completion_id=str(turns[-1].id), organization_id=str(org.id), user_id=str(user.id),
        report_id=str(report.id), status="in_progress",
    ))
    await db.commit()
    with pytest.raises(RewindError) as e:
        await ReportRewindService().rewind(db, report, str(turns[0].id))
    assert e.value.status_code == 409
    assert len(await _visible_ids(db, report)) == 4


@pytest.mark.asyncio
async def test_widgets_built_in_removed_turns_survive(db):
    from app.models.widget import Widget
    _, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 4, content=ASK)
    widget = Widget(title="Revenue", slug="revenue-w", status="published", report_id=str(report.id))
    db.add(widget)
    await db.commit()

    widget_id = str(widget.id)
    await ReportRewindService().rewind(db, report, str(turns[0].id))

    w = (await db.execute(
        select(Widget).where(Widget.id == widget_id).execution_options(populate_existing=True)
    )).scalar_one()
    assert w.deleted_at is None
