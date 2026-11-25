"""Rewind — go back to before a prompt and remove everything after it.

What rewind promises:
  - the chosen prompt and every later row are gone from the agent's context:
    no detailed turns, and no rolling summary or /clear boundary that covers
    them; every earlier turn still reaches the agent
  - a summary that only covers surviving turns is kept; an earlier /clear
    boundary keeps hiding what it hid
  - rewound rows are hidden from ordinary ORM reads (relationship loads and
    counts included), with an explicit opt-out
  - only an idle report can be rewound, and only to a prompt a person sent
"""
from __future__ import annotations

from datetime import datetime, timedelta

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
from app.errors import AppError
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.report import Report
from app.services.report_rewind_service import rewind_report

pytestmark = pytest.mark.db

ASK = "Rewind topic ask {i}"


def _user_rows(rows):
    return [r for r in rows if r.role == "user"]


@pytest.mark.asyncio
@pytest.mark.parametrize("n_turns,target_ask", [(4, 0), (10, 2), (20, 6), (30, 14)])
async def test_rewind_removes_the_prompt_and_everything_after_from_the_agent(db, n_turns, target_ask):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, n_turns, content=ASK)
    target = rows[target_ask]
    assert target.role == "user"
    cut = target_ask

    result = await rewind_report(db, report, str(target.id))
    assert result["removed_count"] == n_turns - cut
    assert result["prompt"]["content"] == target.prompt["content"]

    _, rendered, text = await _render(db, org, report)
    for r in rows[cut:]:
        marker = r.prompt["content"] if r.role == "user" else f"Answer {rows.index(r)}."
        assert marker not in rendered and marker not in text
    for r in rows[:cut]:
        marker = r.prompt["content"] if r.role == "user" else f"Answer {rows.index(r)}."
        assert marker in rendered

    # A new turn after the rewind continues from the surviving history.
    await _add_turns(db, report, user, 2, start=500, content="Fresh path ask {i}")
    _, rendered, _ = await _render(db, org, report)
    assert "Fresh path ask 500" in rendered


@pytest.mark.asyncio
async def test_rewound_rows_are_hidden_from_ordinary_reads_with_an_opt_out(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 6)
    await rewind_report(db, report, str(rows[2].id))

    count = await db.scalar(select(func.count(Completion.id)).where(Completion.report_id == str(report.id)))
    assert count == 2
    assert await db.get(Completion, str(rows[4].id), populate_existing=True) is None

    fresh = (await db.execute(
        select(Report).where(Report.id == str(report.id)).execution_options(populate_existing=True)
    )).scalar_one()
    await db.refresh(fresh, ["completions"])
    assert {str(c.id) for c in fresh.completions} == {str(rows[0].id), str(rows[1].id)}

    all_rows = await db.scalar(
        select(func.count(Completion.id))
        .where(Completion.report_id == str(report.id))
        .execution_options(include_rewound=True)
    )
    assert all_rows == 6


@pytest.mark.asyncio
async def test_rewind_past_the_compaction_watermark_drops_the_summary(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 30, content=ASK)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        compacted = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert compacted["status"] == "compacted"
    state = await _state(db, report.id)
    wm = next(r for r in rows if str(r.id) == state.covers_until_completion_id)
    # A prompt at or before the watermark: the summary covers removed turns.
    target = [r for r in _user_rows(rows) if r.created_at <= wm.created_at][-1]

    result = await rewind_report(db, report, str(target.id))
    assert result["covers_until_completion_id"] is None
    assert result["tokens_compacted_total"] == 0
    state = await _state(db, report.id)
    assert state.summary_json == {}

    _, rendered, text = await _render(db, org, report)
    assert "<history_summary>" not in rendered
    assert target.prompt["content"] not in rendered and target.prompt["content"] not in text
    assert rows[0].prompt["content"] in rendered


@pytest.mark.asyncio
async def test_rewind_after_the_watermark_keeps_the_summary(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 30, content=ASK)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    before = await _state(db, report.id)
    watermark, summary = before.covers_until_completion_id, dict(before.summary_json)
    wm = next(r for r in rows if str(r.id) == watermark)
    target = [r for r in _user_rows(rows) if r.created_at > wm.created_at][-1]

    result = await rewind_report(db, report, str(target.id))
    assert result["covers_until_completion_id"] == watermark
    assert (await _state(db, report.id)).summary_json == summary
    _, rendered, _ = await _render(db, org, report)
    assert "<history_summary>" in rendered
    assert target.prompt["content"] not in rendered


@pytest.mark.asyncio
async def test_rewind_drops_later_clear_boundaries_and_keeps_earlier_ones(db):
    org, report, user = await _seed_report(db)
    old = await _add_turns(db, report, user, 4, content="Old topic ask {i}")
    await _svc().clear(db, report)
    mid = await _add_turns(db, report, user, 4, start=100, content="Mid topic ask {i}")
    await _svc().clear(db, report)
    late = await _add_turns(db, report, user, 4, start=200, content="Late topic ask {i}")
    assert len((await _state(db, report.id)).clear_boundaries) == 2

    # Rewind into the middle section: the second boundary sat on removed
    # rows and goes; the first still hides the old topic.
    await rewind_report(db, report, str(mid[2].id))
    state = await _state(db, report.id)
    assert [b["completion_id"] for b in state.clear_boundaries] == [str(old[-1].id)]

    _, rendered, text = await _render(db, org, report)
    assert "Old topic" not in rendered and "Old topic" not in text
    assert "Mid topic ask 100" in rendered
    assert "Mid topic ask 102" not in rendered
    assert all(r.prompt.get("content", "") not in rendered for r in late if r.role == "user")


@pytest.mark.asyncio
async def test_rewind_is_refused_while_a_run_is_active(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 4)
    db.add(AgentExecution(
        completion_id=str(rows[-1].id), organization_id=str(org.id), user_id=str(user.id),
        report_id=str(report.id), status="in_progress",
    ))
    await db.commit()

    with pytest.raises(AppError) as exc:
        await rewind_report(db, report, str(rows[2].id))
    assert exc.value.status_code == 409
    count = await db.scalar(select(func.count(Completion.id)).where(Completion.report_id == str(report.id)))
    assert count == 4


@pytest.mark.asyncio
async def test_rewind_is_refused_while_a_run_is_starting(db):
    """A run's rows exist a moment before its AgentExecution does."""
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 4)
    rows[-1].status = "in_progress"
    rows[-1].created_at = datetime.utcnow() - timedelta(seconds=2)
    await db.commit()
    with pytest.raises(AppError) as exc:
        await rewind_report(db, report, str(rows[0].id))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_rewind_only_targets_a_prompt_of_this_report(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 4)
    _, other, other_user = await _seed_report(db)
    other_rows = await _add_turns(db, other, other_user, 2)

    with pytest.raises(AppError) as exc:
        await rewind_report(db, report, str(rows[1].id))  # an answer, not a prompt
    assert exc.value.status_code == 400
    with pytest.raises(AppError) as exc:
        await rewind_report(db, report, str(other_rows[0].id))
    assert exc.value.status_code == 404
    await rewind_report(db, report, str(rows[2].id))
    with pytest.raises(AppError) as exc:
        await rewind_report(db, report, str(rows[2].id))  # already rewound
    assert exc.value.status_code == 404


async def _memory(db, org, user, text, completion_id, status="active", seq=1):
    from app.models.memory_entry import MemoryEntry
    m = MemoryEntry(
        organization_id=str(org.id), user_id=str(user.id), seq=seq, handle=f"m{seq}",
        text=text, source="agent", status=status,
        evidence={"report_id": "r", "completion_id": str(completion_id), "quote": text},
    )
    db.add(m)
    await db.commit()
    return m


@pytest.mark.asyncio
async def test_dry_run_changes_nothing_and_counts_what_would_go(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 8)
    preview = await rewind_report(db, report, str(rows[4].id), dry_run=True)
    assert preview["removed_count"] == 4
    assert set(preview["removed_ids"]) == {str(r.id) for r in rows[4:]}
    count = await db.scalar(select(func.count(Completion.id)).where(Completion.report_id == str(report.id)))
    assert count == 8


@pytest.mark.asyncio
async def test_memories_saved_in_removed_turns_are_kept_and_counted(db):
    from app.models.memory_entry import MemoryEntry
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 8)
    await _memory(db, org, user, "Kept fact from an earlier turn", rows[0].id, seq=1)
    await _memory(db, org, user, "Fact from a removed turn", rows[4].id, seq=2)
    await _memory(db, org, user, "Another removed-turn fact", rows[6].id, seq=3)
    await _memory(db, org, user, "", rows[6].id, status="forgotten", seq=4)

    preview = await rewind_report(db, report, str(rows[4].id), dry_run=True)
    assert preview["memories_kept"] == 2
    result = await rewind_report(db, report, str(rows[4].id))
    assert result["memories_kept"] == 2

    active = await db.scalar(
        select(func.count(MemoryEntry.id)).where(
            MemoryEntry.user_id == str(user.id), MemoryEntry.status == "active"
        )
    )
    assert active == 3


# ---------------------------------------------------------------- review fixes


@pytest.mark.asyncio
async def test_a_legacy_clear_boundary_before_the_rewind_point_survives(db):
    """Boundaries stored before completion_created_at existed are judged by
    their row, not dropped for lacking a timestamp."""
    org, report, user = await _seed_report(db)
    old = await _add_turns(db, report, user, 4, content="Old topic ask {i}")
    await _svc().clear(db, report)
    state = await _state(db, report.id)
    legacy = dict(state.clear_boundaries[0])
    legacy.pop("completion_created_at", None)
    state.clear_boundaries = [legacy]
    await db.commit()
    mid = await _add_turns(db, report, user, 4, start=100, content="Mid topic ask {i}")

    await rewind_report(db, report, str(mid[2].id))

    state = await _state(db, report.id)
    assert [b["completion_id"] for b in state.clear_boundaries] == [str(old[-1].id)]
    _, rendered, text = await _render(db, org, report)
    assert "Old topic" not in rendered and "Old topic" not in text
    assert "Mid topic ask 100" in rendered


@pytest.mark.asyncio
async def test_rewinding_a_prompt_that_was_queued_keeps_the_earlier_runs_late_rows(db):
    """A queued prompt keeps its queue-time created_at when dispatched; rows
    the earlier run wrote while it waited belong to the earlier turn."""
    org, report, user = await _seed_report(db)
    t0 = datetime(2026, 3, 1, 9, 0)

    def row(role, minutes, **kw):
        c = Completion(
            prompt={"content": kw.pop("ask", "")} if role == "user" else {},
            completion={"content": kw.pop("answer", "")} if role == "system" else {},
            status="success", model="test", role=role, report_id=str(report.id),
            message_type=kw.pop("message_type", "ai_completion"),
            user_id=str(user.id) if role == "user" else None, **kw,
        )
        c.created_at = t0 + timedelta(minutes=minutes)
        db.add(c)
        return c

    a_user = row("user", 0, ask="Run A ask")
    await db.flush()
    a_sys = row("system", 0.01, answer="Answer A", parent_id=a_user.id)
    await db.flush()
    b_user = row("user", 1, ask="Queued B ask")                       # queued while A ran
    await db.flush()
    steer = row("user", 2, ask="Steer A", message_type="steering", parent_id=a_sys.id)
    child = row("user", 3, ask="A child", trigger_source="sub_agent")
    c_user = row("user", 4, ask="Queued C ask")                       # queued after B
    await db.flush()
    b_sys = row("system", 10, answer="Answer B", parent_id=b_user.id)  # dispatched after A
    c_sys = row("system", 20, answer="Answer C", parent_id=c_user.id)
    await db.commit()

    result = await rewind_report(db, report, str(b_user.id))

    removed = set(result["removed_ids"])
    assert removed == {str(b_user.id), str(b_sys.id), str(c_user.id), str(c_sys.id)}
    kept = {str(r.id) for r in (await db.execute(
        select(Completion).where(Completion.report_id == str(report.id))
    )).scalars().all()}
    assert {str(a_user.id), str(a_sys.id), str(steer.id), str(child.id)} <= kept


@pytest.mark.asyncio
async def test_the_token_anchor_is_reset_even_without_compaction(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 10)
    await _svc().record_prompt_anchor(db, str(report.id), 12345, 10000)
    assert (await _state(db, report.id)).last_prompt_tokens == 12345

    await rewind_report(db, report, str(rows[4].id))

    state = await _state(db, report.id)
    assert state.last_prompt_tokens is None and state.last_prompt_estimate is None


@pytest.mark.asyncio
async def test_planned_checkins_of_removed_turns_are_cancelled_and_others_kept(db):
    from app.models.agent_checkin import AgentCheckin, STATUS_PLANNED, STATUS_CANCELLED
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 8)

    def checkin(source):
        c = AgentCheckin(
            organization_id=str(org.id), user_id=str(user.id), report_id=str(report.id),
            source_completion_id=str(source.id), status=STATUS_PLANNED,
        )
        db.add(c)
        return c

    early, late = checkin(rows[1]), checkin(rows[5])
    await db.commit()

    result = await rewind_report(db, report, str(rows[4].id))
    assert result["checkins_cancelled"] == 1
    await db.refresh(early)
    await db.refresh(late)
    assert early.status == STATUS_PLANNED
    assert late.status == STATUS_CANCELLED and late.status_reason == "rewound"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "waiting", "running"])
async def test_rewind_is_refused_while_a_sub_agent_run_is_unfinished(db, status):
    from app.models.sub_agent import SubAgentRun, SubAgentWave
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 4)
    wave = SubAgentWave(organization_id=str(org.id), report_id=str(report.id))
    db.add(wave)
    await db.flush()
    db.add(SubAgentRun(wave_id=wave.id, organization_id=str(org.id), report_id=str(report.id), status=status))
    await db.commit()

    with pytest.raises(AppError) as exc:
        await rewind_report(db, report, str(rows[2].id))
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_a_parent_added_in_this_session_does_not_lazy_load_rewound_rows(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 6)
    await rewind_report(db, report, str(rows[2].id))
    # `report` was added in this session (never loaded through a filtered
    # SELECT); its relationship load must still apply the rule.
    db.expire(report, ["completions"])
    await db.refresh(report, ["completions"])
    assert {str(c.id) for c in report.completions} == {str(rows[0].id), str(rows[1].id)}


@pytest.mark.asyncio
async def test_opt_outs_see_rewound_rows(db):
    from app.models.completion import rewound_completions_visible, sees_rewound_completions
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 6)
    await rewind_report(db, report, str(rows[2].id))
    stmt = select(func.count(Completion.id)).where(Completion.report_id == str(report.id))

    assert await db.scalar(stmt) == 2
    with rewound_completions_visible():
        assert await db.scalar(stmt) == 6

    @sees_rewound_completions
    class Reader:
        async def count(self):
            return await db.scalar(stmt)

    assert await Reader().count() == 6
    assert await db.scalar(stmt) == 2  # the opt-out does not leak out


@pytest.mark.asyncio
async def test_dry_run_counts_transcript_rows(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 8)
    preview = await rewind_report(db, report, str(rows[2].id), dry_run=True)
    assert preview["visible_removed_count"] == 6


@pytest.mark.asyncio
async def test_the_report_summary_still_lists_what_rewound_turns_built(db):
    """Rewind keeps instructions/queries/lists; the Summary keeps showing them."""
    from app.models.completion_block import CompletionBlock
    from app.models.tool_execution import ToolExecution
    from app.services.report_service import ReportService
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 6)
    answer = rows[3]  # a system row of the turn that will be rewound
    ae = AgentExecution(completion_id=str(answer.id), organization_id=str(org.id),
                        user_id=str(user.id), report_id=str(report.id), status="success")
    db.add(ae)
    await db.flush()
    te = ToolExecution(agent_execution_id=ae.id, tool_name="create_instruction",
                       arguments_json={"text": "Use net revenue"}, status="success", success=True,
                       result_json={"success": True, "instruction_id": "instr-1", "text": "Use net revenue"})
    db.add(te)
    await db.flush()
    db.add(CompletionBlock(completion_id=str(answer.id), source_type="tool", title="t",
                           status="completed", tool_execution_id=te.id))
    await db.commit()

    async def instruction_items():
        summary = await ReportService().get_report_summary(db, str(report.id), org)
        return summary.get("instructions") if isinstance(summary, dict) else summary.instructions

    before = await instruction_items()
    assert before, "seeded instruction tool run should be on the Summary"
    await rewind_report(db, report, str(rows[2].id))
    assert len(await instruction_items()) == len(before)
