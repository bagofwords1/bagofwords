"""Rolling context compaction — service contract.

What the service promises, independent of when the governor calls it:
  - the opening exchange is never folded; opening_request comes from the DB
  - manual /compact keeps the last N user exchanges verbatim (default 2)
  - auto modes keep at least one exchange plus a token tail sized by the
    model that RECEIVES the prompt, not the summarizer
  - summary + watermark + totals move together, chunk by chunk
  - entity ids must survive verbatim (hallucinated ids dropped)
  - a summarizer that fails, times out or returns junk never blocks
    compaction: one corrective retry, then a deterministic summary that keeps
    user asks and ids; auto modes then back off from the LLM
  - a summary that would grow the history is never committed
  - focus instructions reach the summarizer as data
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import the same model modules alembic/env.py registers so Base.metadata
# knows the full mapped graph (a blanket pkgutil sweep trips over dead model
# modules that env.py deliberately omits).
_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 — test-only, mirrors env.py verbatim

from app.models.base import Base
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.models.organization import Organization
from app.models.report import Report
from app.models.report_context_state import ReportContextState
from app.models.tool_execution import ToolExecution
from app.models.user import User
from app.ai.persisted_summary import build_tool_context_summary

from app.services.context_compaction_service import (
    COMPACTION_MESSAGE_TYPE,
    MANUAL_KEEP_EXCHANGES,
    PROTECT_FIRST_N,
    ContextCompactionService,
    _enforce_summary_budget,
    _parse_summary_json,
    _validate_entities,
    render_summary_for_prompt,
)

pytestmark = pytest.mark.db


# ------------------------------------------------------------------ fixtures


@pytest_asyncio.fixture
async def db():
    # The Completion model declares sigkill nullable=False default=None (the
    # real schema, created by migrations, allows NULL). Relax the DDL flag so
    # metadata.create_all matches production behavior.
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_report(db: AsyncSession):
    from sqlalchemy import select
    user = User(name="U", email=f"u-{uuid.uuid4()}@x.dev", hashed_password="x")
    db.add(user)
    await db.flush()
    org = Organization(name=f"Org-{uuid.uuid4()}")
    db.add(org)
    await db.flush()
    report = Report(title="R", slug=f"r-{uuid.uuid4()}", user_id=str(user.id), organization_id=str(org.id))
    db.add(report)
    await db.commit()
    # Re-query the org the way production callers obtain it — settings is
    # lazy='joined', so a queried instance carries it (None here) instead of
    # lazy-loading in async context.
    org = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()
    return org, report, user


async def _add_turns(db, report, user, n, *, start=0, content="What is revenue for month {i}? id ref q-000"):
    """n alternating user/system completions with increasing created_at."""
    base = datetime(2026, 1, 1) + timedelta(minutes=start)
    rows = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "system"
        c = Completion(
            prompt={"content": content.format(i=start + i) if role == "user" else ""},
            completion={"content": f"Answer {start + i}. " + "Revenue rose in the north region; churn held flat. " * 6} if role == "system" else {},
            status="success",
            model="test",
            role=role,
            message_type="ai_completion",
            report_id=str(report.id),
            user_id=str(user.id) if role == "user" else None,
        )
        db.add(c)
        await db.flush()
        c.created_at = base + timedelta(minutes=i)
        rows.append(c)
    await db.commit()
    return rows


def _model(window: int | None = 1000):
    """LLM-model stand-in with an explicit context window. A tiny window keeps
    the token tail small so scope arithmetic is driven by the exchange rules."""
    m = MagicMock()
    m.context_window_tokens = window
    return m


def _mock_llm(response_json=None, *, side_effect=None):
    """Patch the LLM class the service imports; .inference returns canned
    JSON (or follows ``side_effect``)."""
    instance = MagicMock()
    if side_effect is not None:
        instance.inference.side_effect = side_effect
    else:
        instance.inference.return_value = json.dumps(response_json)
    cls = MagicMock(return_value=instance)
    return patch("app.ai.llm.LLM", cls), instance


SUMMARY = {
    "goal": "Track monthly revenue",
    "progress": {"done": ["revenue by month"], "in_progress": [], "blocked": []},
    "key_decisions": ["monthly grain"],
    "entities": [],
    "next_steps": ["quarterly rollup"],
    "critical_context": [],
}


def _manual_scope_size(n_turns: int, keep: int = MANUAL_KEEP_EXCHANGES) -> int:
    """Foldable rows for n alternating user/system turns under the manual
    rule: minus the opening exchange, minus the last ``keep`` exchanges."""
    return n_turns - PROTECT_FIRST_N - 2 * keep


def _svc():
    # Fresh instance per test — the module singleton carries per-report locks.
    return ContextCompactionService()


async def _state(db, report_id):
    """Fresh read of the state row (the service commits on its own path)."""
    return (await db.execute(
        select(ReportContextState)
        .where(ReportContextState.report_id == str(report_id))
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()


# ------------------------------------------------------------------ scope


@pytest.mark.asyncio
@pytest.mark.parametrize("n_turns,keep", [(12, None), (30, None), (30, 1), (40, 5)])
async def test_manual_keeps_last_exchanges_and_opening(db, n_turns, keep):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, n_turns)
    want_keep = keep or MANUAL_KEEP_EXCHANGES
    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True, keep=keep)

    assert result["status"] == "compacted"
    assert result["compacted_turns"] == _manual_scope_size(n_turns, want_keep)
    # The watermark is the last folded row: the turn right before the kept exchanges.
    assert result["covers_until_completion_id"] == str(rows[n_turns - 2 * want_keep - 1].id)
    prompt = instance.inference.call_args[0][0]
    assert f"Answer {n_turns - 1}" not in prompt               # kept tail stays out
    assert "What is revenue for month 0?" not in prompt         # opening exchange stays out
    state = await _state(db, report.id)
    assert "What is revenue for month 0?" in state.summary_json["opening_request"]


@pytest.mark.asyncio
async def test_nothing_to_compact_when_only_kept_exchanges_exist(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, PROTECT_FIRST_N + 2 * MANUAL_KEEP_EXCHANGES)
    result = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert result["status"] == "nothing_to_compact"
    assert await _state(db, report.id) is None


@pytest.mark.asyncio
async def test_auto_tail_is_sized_by_the_receiving_model(db):
    """The summarizer's window never sizes the tail: a small summarizer
    paired with a large receiving model keeps more history verbatim."""
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 60)
    small = _model(1000)

    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        tight = await _svc().compact(db, report, org, small, force=True, mode="auto_sync", budget_model=_model(1000))

    org2, report2, user2 = await _seed_report(db)
    await _add_turns(db, report2, user2, 60)
    llm_patch2, _ = _mock_llm(SUMMARY)
    with llm_patch2:
        roomy = await _svc().compact(db, report2, org2, small, force=True, mode="auto_sync", budget_model=_model(30_000))

    assert tight["status"] == "compacted"
    assert roomy.get("compacted_turns", 0) < tight["compacted_turns"]


@pytest.mark.asyncio
async def test_auto_always_keeps_the_newest_exchange(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 20)
    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(400), force=True, mode="auto_sync")
    assert result["status"] == "compacted"
    newest_user = rows[-2]
    prompt = instance.inference.call_args[0][0]
    assert _user_content(newest_user) not in prompt


def _user_content(c):
    return c.prompt["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [20_000, 200_000])
async def test_unforced_auto_skips_when_scope_is_negligible(db, window):
    """A background fold is pointless when the tail already covers (nearly)
    everything; it must not call the summarizer."""
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 10)
    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(window), force=False, mode="auto_bg")
    assert result["status"] in ("below_threshold", "nothing_to_compact"), result
    instance.inference.assert_not_called()
    assert await _state(db, report.id) is None


# ------------------------------------------------------------------ commit


@pytest.mark.asyncio
async def test_compact_advances_watermark_and_totals_without_marker_rows(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 30)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True)

    state = await _state(db, report.id)
    assert state.covers_until_completion_id == result["covers_until_completion_id"]
    assert state.covered_turns == result["compacted_turns"] > 0
    assert state.tokens_compacted_total > 0
    assert state.last_method == "llm" and state.degraded is False
    assert state.last_tokens_before >= state.last_tokens_after > 0
    assert state.summary_json["goal"] == SUMMARY["goal"]
    markers = (await db.execute(
        select(Completion).where(Completion.message_type == COMPACTION_MESSAGE_TYPE)
    )).scalars().all()
    assert markers == []
    # Immediately recompacting finds nothing: only kept exchanges remain.
    again = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert again["status"] == "nothing_to_compact"


@pytest.mark.asyncio
async def test_builder_renders_summary_opening_exchange_and_window(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 30)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        await _svc().compact(db, report, org, _model(), mode="manual", force=True)

    org_loaded = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()
    from app.ai.context.builders.message_context_builder import MessageContextBuilder
    builder = MessageContextBuilder(db, org_loaded, report)
    section = await builder.build(max_messages=40)
    rendered = section.render()

    assert "<history_summary>" in rendered and "Track monthly revenue" in rendered
    # Opening exchange renders in its own pinned block, ahead of the window.
    opening = rendered.split("<opening_exchange>")[1].split("</opening_exchange>")[0]
    assert "What is revenue for month 0?" in opening
    conversation = rendered.split("<conversation>")[1]
    assert "What is revenue for month 4?" not in rendered   # folded middle
    assert "Answer 29" in conversation                      # recent tail

    text = await builder.build_context(max_messages=40)
    assert "Track monthly revenue" in text
    assert "What is revenue for month 0?" in text
    assert "What is revenue for month 4?" not in text


@pytest.mark.asyncio
async def test_focus_instructions_reach_summarizer_as_data(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 20)
    focus = "focus on the churn numbers " + "x" * 2000
    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        await _svc().compact(db, report, org, _model(), mode="manual", force=True, instructions=focus)
    prompt = instance.inference.call_args[0][0]
    assert "focus on the churn numbers" in prompt
    # Capped, and quoted as data rather than spliced raw.
    assert "x" * 900 not in prompt
    assert json.dumps(focus[:20])[:-1] in prompt


# ------------------------------------------------------------------ resilience


@pytest.mark.asyncio
async def test_unparseable_output_is_retried_once(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 20)
    llm_patch, instance = _mock_llm(side_effect=["this is not json", json.dumps(SUMMARY)])
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert result["status"] == "compacted"
    assert result["method"] == "llm_retry"
    assert result["degraded"] is False
    assert instance.inference.call_count == 2
    assert "not valid JSON" in instance.inference.call_args_list[1][0][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError("LLM down"), ["junk", "still junk"]])
async def test_summarizer_failure_falls_back_without_losing_asks_or_ids(db, failure):
    org, report, user = await _seed_report(db)
    real_id = "3f0f3a52-aaaa-bbbb-cccc-00000000abcd"
    await _add_turns(db, report, user, 20, content="Build chart {i} query_id: " + real_id)
    llm_patch, _ = _mock_llm(side_effect=failure)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True)

    assert result["status"] == "compacted"
    assert result["method"] == "fallback" and result["degraded"] is True
    state = await _state(db, report.id)
    assert state.covers_until_completion_id == result["covers_until_completion_id"]
    rendered = render_summary_for_prompt(state.summary_json)
    assert "Build chart 2 query_id" in rendered          # a folded user ask, verbatim
    assert real_id in rendered                           # ids survive
    assert state.failure_count == 1 and state.cooldown_until is not None


@pytest.mark.asyncio
async def test_cooldown_skips_llm_in_auto_but_not_manual(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 20)
    llm_patch, _ = _mock_llm(side_effect=RuntimeError("down"))
    with llm_patch:
        await _svc().compact(db, report, org, _model(), mode="manual", force=True)

    await _add_turns(db, report, user, 20, start=100)
    llm_patch2, instance2 = _mock_llm(SUMMARY)
    with llm_patch2:
        auto = await _svc().compact(db, report, org, _model(), mode="auto_sync", force=True)
    assert auto["status"] == "compacted" and auto["method"] == "fallback"
    instance2.inference.assert_not_called()

    await _add_turns(db, report, user, 20, start=200)
    llm_patch3, instance3 = _mock_llm(SUMMARY)
    with llm_patch3:
        manual = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert manual["method"] == "llm"
    instance3.inference.assert_called()
    state = await _state(db, report.id)
    assert state.failure_count == 0 and state.cooldown_until is None


@pytest.mark.asyncio
async def test_large_scope_is_folded_in_chunks_oldest_first(db):
    org, report, user = await _seed_report(db)
    big = "Question {i}: " + ("detail " * 900)
    rows = await _add_turns(db, report, user, 24, content=big)
    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        # A 4k-window summarizer gets ~2k-token chunks.
        result = await _svc().compact(db, report, org, _model(4000), mode="manual", force=True, budget_model=_model(200_000))
    assert result["status"] == "compacted"
    assert result["chunks"] > 1
    assert instance.inference.call_count == result["chunks"]
    for call in instance.inference.call_args_list:
        assert len(call[0][0]) < 4000 * 3 + 6000  # bounded input (+ prompt scaffolding)
    assert result["covers_until_completion_id"] == str(rows[24 - 2 * MANUAL_KEEP_EXCHANGES - 1].id)


@pytest.mark.asyncio
async def test_summary_that_would_grow_history_is_refused(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 10, content="hi {i}")
    bloated = dict(SUMMARY)
    bloated["goal"] = "g " * 4000  # goal is never trimmed by the budget pass
    llm_patch, _ = _mock_llm(bloated)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(200_000), mode="manual", force=True)
    assert result["status"] == "refused_grow"
    assert await _state(db, report.id) is None


@pytest.mark.asyncio
async def test_hallucinated_entity_ids_are_dropped(db):
    org, report, user = await _seed_report(db)
    real_id = "3f0f3a52-aaaa-bbbb-cccc-000000000001"
    await _add_turns(
        db, report, user, 30,
        content="Chart for month {i} [query: " + real_id + "]",
    )
    summary = dict(SUMMARY)
    summary["entities"] = [
        {"type": "query", "id": real_id, "title": "Revenue", "state": "created"},
        {"type": "widget", "id": "11111111-dead-beef-0000-999999999999", "title": "Ghost", "state": "?"},
    ]
    llm_patch, _ = _mock_llm(summary)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert result["status"] == "compacted"
    state = await _state(db, report.id)
    assert [e["id"] for e in state.summary_json["entities"]] == [real_id]


@pytest.mark.asyncio
async def test_legacy_markers_excluded_from_scope(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 30)
    db.add(Completion(
        prompt={"content": ""},
        completion={"content": "Compacted 2 turns (~70 tokens)"},
        status="success", model="system", role="system",
        message_type=COMPACTION_MESSAGE_TYPE, report_id=str(report.id),
    ))
    await db.commit()
    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        first = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert first["compacted_turns"] == _manual_scope_size(30)
    assert "Compacted 2 turns" not in instance.inference.call_args[0][0]


@pytest.mark.asyncio
async def test_get_ui_state_can_compact_follows_manual_rule(db):
    org, report, user = await _seed_report(db)
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["can_compact"] is False and ui["tokens_compacted_total"] == 0

    await _add_turns(db, report, user, PROTECT_FIRST_N + 2 * MANUAL_KEEP_EXCHANGES)
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["can_compact"] is False  # only the opening + kept exchanges

    await _add_turns(db, report, user, 2, start=100)
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["can_compact"] is True


@pytest.mark.asyncio
async def test_prompt_anchor_round_trips(db):
    org, report, user = await _seed_report(db)
    await ContextCompactionService.record_prompt_anchor(db, str(report.id), 31_000, 22_000)
    state = await _state(db, report.id)
    assert ContextCompactionService.anchor_offset(state) == 9_000
    # An anchor-only state is not a compaction: nothing renders, no watermark.
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["covers_until_completion_id"] is None and ui["covered_turns"] == 0


# ------------------------------------------------------------------ parsing


def test_parse_summary_json_strips_fences_and_unknown_keys():
    parsed = _parse_summary_json('```json\n{"goal": "g", "junk": 1}\n```')
    assert parsed == {"goal": "g"}
    assert _parse_summary_json("not json at all {{{") in (None, {})


def test_validate_entities_requires_verbatim_id():
    s = {"entities": [{"id": "abc-1"}, {"id": "zzz-9"}]}
    out = _validate_entities(s, "digest mentioning abc-1 only")
    assert [e["id"] for e in out["entities"]] == ["abc-1"]


def test_summary_budget_enforcement_trims_entities_last():
    summary = {
        "goal": "g",
        "critical_context": ["c" * 400 for _ in range(30)],
        "pending_user_asks": ["a" * 200 for _ in range(10)],
        "entities": [{"type": "query", "id": f"id-{i}", "title": "t"} for i in range(30)],
    }
    out = _enforce_summary_budget(summary, 1500)
    assert len(out["entities"]) == 30
    assert len(out["critical_context"]) < 30


@pytest.mark.asyncio
async def test_auto_compaction_receives_exact_model_visible_tool_observation(db):
    """The rolling summarizer must see the same bounded result as the planner.

    A canonical tool output can use a different shape from its observation.
    Once this completion moves behind the watermark, the rolling summary is
    the only model-visible representation, so folding the output shape (or a
    summary-only digest) loses the exact facts the model previously saw.
    """
    org, report, user = await _seed_report(db)
    turns = await _add_turns(db, report, user, 30)
    folded_completion = turns[7]
    exact_fact = "status values: paid, refunded, cancelled"

    execution = AgentExecution(
        completion_id=str(folded_completion.id),
        organization_id=str(org.id),
        user_id=str(user.id),
        report_id=str(report.id),
        status="success",
    )
    db.add(execution)
    await db.flush()
    canonical_output = {
        "success": True,
        "execution_log": "canonical output uses a different field name",
    }
    tool_execution = ToolExecution(
        agent_execution_id=str(execution.id),
        tool_name="inspect_data",
        tool_action="research",
        arguments_json={"question": "Which order statuses exist?"},
        status="success",
        success=True,
        result_summary="Inspection finished",
        result_json=canonical_output,
        context_summary_json=build_tool_context_summary(
            "inspect_data",
            canonical_output,
            observation={
                "summary": "Inspection finished",
                "details": exact_fact,
                "success": True,
            },
        ),
    )
    db.add(tool_execution)
    await db.flush()
    instruction_text = (
        "When calculating net revenue, exclude refunded invoices consistently."
    )
    instruction_output = {
        "success": True,
        "message": "canonical instruction output omits the stored text",
    }
    instruction_execution = ToolExecution(
        agent_execution_id=str(execution.id),
        tool_name="create_instruction",
        tool_action="knowledge",
        arguments_json={"title": "Net revenue", "text": instruction_text},
        status="success",
        success=True,
        result_summary="Created instruction: Net revenue",
        result_json=instruction_output,
        context_summary_json=build_tool_context_summary(
            "create_instruction",
            instruction_output,
            observation={
                "summary": "Created instruction: Net revenue",
                "new_text": instruction_text,
                "success": True,
            },
        ),
    )
    db.add(instruction_execution)
    await db.flush()
    db.add_all([CompletionBlock(
        completion_id=str(folded_completion.id),
        agent_execution_id=str(execution.id),
        source_type="tool",
        tool_execution_id=str(tool_execution.id),
        block_index=0,
        title="Inspected order statuses",
        status="completed",
    ), CompletionBlock(
        completion_id=str(folded_completion.id),
        agent_execution_id=str(execution.id),
        source_type="tool",
        tool_execution_id=str(instruction_execution.id),
        block_index=1,
        title="Created net-revenue instruction",
        status="completed",
    )])
    await db.commit()

    compacted_summary = dict(SUMMARY)
    compacted_summary["critical_context"] = [exact_fact, instruction_text]
    llm_patch, instance = _mock_llm(compacted_summary)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), force=False, mode="auto_bg")

    assert result["status"] == "compacted"
    summarizer_prompt = instance.inference.call_args[0][0]
    assert exact_fact in summarizer_prompt, summarizer_prompt
    assert instruction_text in summarizer_prompt
    assert "Preserve exact non-personal facts needed for follow-ups" in summarizer_prompt
    assert "canonical output uses a different field name" not in summarizer_prompt
    assert "canonical instruction output omits the stored text" not in summarizer_prompt

    state = await ContextCompactionService.get_state(db, str(report.id))
    assert exact_fact in render_summary_for_prompt(state.summary_json)
    assert instruction_text in render_summary_for_prompt(state.summary_json)


@pytest.mark.asyncio
async def test_message_builder_does_not_hydrate_report_step_data(db):
    """Building bounded conversation history must not load the report graph.

    Step result rows are not part of message history; tool digests are loaded
    explicitly through completion blocks/tool executions. A mapper-level
    relationship cascade must therefore never touch ``steps`` here.
    """
    from sqlalchemy import select

    from app.ai.context.builders.message_context_builder import MessageContextBuilder
    from app.models.agent_execution import AgentExecution
    from app.models.completion_block import CompletionBlock
    from app.models.step import Step
    from app.models.tool_execution import ToolExecution
    from app.models.widget import Widget

    org, report, user = await _seed_report(db)
    report_id = str(report.id)
    widget = Widget(
        title="Historical result",
        slug=f"widget-{uuid.uuid4()}",
        report_id=report_id,
    )
    db.add(widget)
    await db.flush()
    db.add(Step(
        title="Historical step",
        slug=f"step-{uuid.uuid4()}",
        status="success",
        prompt="select value",
        code="select value",
        data={"columns": [{"field": "value"}], "rows": [{"value": i} for i in range(50)]},
        description="Stored query result that is unrelated to message rendering",
        type="table",
        widget_id=str(widget.id),
    ))
    turns = await _add_turns(db, report, user, 4)
    system_completion = next(c for c in reversed(turns) if c.role == "system")
    execution = AgentExecution(
        completion_id=str(system_completion.id),
        organization_id=str(org.id),
        user_id=str(user.id),
        report_id=report_id,
        status="success",
    )
    db.add(execution)
    await db.flush()
    tool_execution = ToolExecution(
        agent_execution_id=str(execution.id),
        tool_name="create_data",
        tool_action="query",
        arguments_json={},
        status="success",
        success=True,
        result_summary="Created a bounded result",
        result_json={
            "data": {
                "columns": [{"field": "value"}],
                "rows": [{"value": i} for i in range(50)],
            },
            "data_preview": {"rows": [{"value": 0}]},
        },
    )
    db.add(tool_execution)
    await db.flush()
    db.add(CompletionBlock(
        completion_id=str(system_completion.id),
        agent_execution_id=str(execution.id),
        source_type="tool",
        tool_execution_id=str(tool_execution.id),
        block_index=0,
        title="Created data",
        status="completed",
    ))
    await db.commit()

    # Force the builder's Completion query to materialize a fresh ORM graph;
    # otherwise the fixture session's identity map can hide eager-loader IO.
    db.expunge_all()
    org_loaded = (
        await db.execute(select(Organization).where(Organization.id == str(org.id)))
    ).unique().scalar_one()
    report_ref = type("ReportRef", (), {"id": report_id})()

    statements: list[str] = []

    def _record_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    sync_engine = db.bind.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _record_sql)
    try:
        section = await MessageContextBuilder(db, org_loaded, report_ref).build(max_messages=20)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _record_sql)

    rendered = section.render()
    assert "What is revenue" in rendered
    assert "50 rows × 1 cols" in rendered  # explicit tool digest is preserved
    assert not any("from steps" in statement for statement in statements), (
        "bounded message history hydrated report step data via an ORM relationship cascade"
    )


# ------------------------------------------------------------------ agent


@pytest.mark.asyncio
async def test_background_compaction_emits_sse_event(db):
    """_run_auto_compaction emits context.compacted on the live event queue
    when the service compacts, carrying what the divider needs."""
    import app.ai.agent_v2 as agent_v2_mod

    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 4)

    agent = object.__new__(agent_v2_mod.AgentV2)
    agent.report = report
    agent.report_id = str(report.id)
    agent.organization = org
    agent.model = _model()
    agent.small_model = _model()
    agent.system_completion = MagicMock(id="sys-1")
    agent.system_completion_id = "sys-1"
    agent.event_queue = MagicMock()
    agent.event_queue.put = AsyncMock()
    agent._history_reload_pending = False

    class _Maker:
        def __call__(self):
            return self
        async def __aenter__(self):
            return db
        async def __aexit__(self, *a):
            return False
    agent._session_maker = _Maker()

    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        await agent._run_auto_compaction(force=True, mode="auto_sync")
    agent.event_queue.put.assert_not_called()  # nothing foldable yet

    await _add_turns(db, report, user, 30, start=100)
    llm_patch2, _ = _mock_llm(SUMMARY)
    with llm_patch2:
        await agent._run_auto_compaction(force=True, mode="auto_bg")

    agent.event_queue.put.assert_called_once()
    event = agent.event_queue.put.call_args[0][0]
    assert event.event == "context.compacted"
    state = await _state(db, report.id)
    assert event.data["covers_until_completion_id"] == str(state.covers_until_completion_id)
    assert event.data["tokens_compacted_total"] > 0
    assert event.data["history_tokens_before"] > event.data["history_tokens_after"] > 0
    assert event.data["method"] == "llm"
    # A background fold leaves the in-run transcript to be swapped later.
    assert agent._history_reload_pending is True


async def _tool_completion(db, report, user, org, *, role_status, call_id, tool_name, minute, content="ok"):
    """A system completion that ran one tool (block + execution persisted)."""
    c = Completion(
        prompt={"content": ""}, completion={"content": content}, status=role_status,
        model="test", role="system", message_type="ai_completion", report_id=str(report.id),
    )
    db.add(c)
    await db.flush()
    c.created_at = datetime(2026, 1, 1) + timedelta(minutes=minute)
    ex = AgentExecution(completion_id=str(c.id), organization_id=str(org.id), user_id=str(user.id),
                        report_id=str(report.id), status="success" if role_status == "success" else "in_progress")
    db.add(ex)
    await db.flush()
    te = ToolExecution(agent_execution_id=str(ex.id), tool_name=tool_name, tool_action="query",
                       arguments_json={"q": call_id}, status="success", success=True,
                       result_summary=f"{tool_name} done", result_json={"success": True},
                       provider_call_id=call_id)
    db.add(te)
    await db.flush()
    db.add(CompletionBlock(completion_id=str(c.id), agent_execution_id=str(ex.id), source_type="tool",
                           tool_execution_id=str(te.id), block_index=0, title=tool_name, status="completed"))
    await db.commit()
    return c


def _call_ids(transcript):
    from app.ai.context.parts import ToolCallPart
    return [p.id for t in transcript.turns for p in t.parts if isinstance(p, ToolCallPart)]


@pytest.mark.asyncio
@pytest.mark.parametrize("current_calls", [1, 3])
async def test_mid_run_history_reload_keeps_each_call_once(db, current_calls):
    """Reloading history after a fold must not replay this run's own tool
    calls a second time (they already live in the run's transcript)."""
    from types import SimpleNamespace
    import app.ai.agent_v2 as agent_v2_mod
    from app.ai.context.builders.message_context_builder import MessageContextBuilder
    from app.ai.context.parts import ToolCallPart, ToolResultPart
    from app.ai.context.transcript import Transcript

    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 1)  # earlier ask
    await _tool_completion(db, report, user, org, role_status="success", call_id="call_history",
                           tool_name="create_data", minute=1)
    await _add_turns(db, report, user, 1, start=2)  # this run's ask
    current = await _tool_completion(db, report, user, org, role_status="in_progress",
                                     call_id="call_current_0", tool_name="create_data", minute=3)

    org_loaded = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()
    builder = MessageContextBuilder(db, org_loaded, report)
    builder.use_durable_transcript = True

    agent = object.__new__(agent_v2_mod.AgentV2)
    agent.system_completion_id = str(current.id)
    agent._is_sub_agent = False
    agent._history_reload_pending = True

    async def _refresh():
        await builder.build(max_messages=40)
    agent.context_hub = SimpleNamespace(
        message_builder=builder, refresh_warm=_refresh,
        get_view=lambda: SimpleNamespace(warm=SimpleNamespace(messages=None)),
    )
    # Live transcript: rehydrated history prefix + this run's calls (live objects).
    await builder.build(max_messages=40)
    durable = builder.get_durable_transcript()
    live_ids = [f"call_current_{i}" for i in range(current_calls)]
    agent.transcript = Transcript()
    agent.transcript.turns = [t for t in durable.turns
                              if not any(isinstance(p, ToolCallPart) and p.id.startswith("call_current") for p in t.parts)]
    agent.transcript.history_turn_count = len(agent.transcript.turns)
    agent.transcript.add_assistant_step(calls=[ToolCallPart(id=i, tool_name="create_data") for i in live_ids])
    agent.transcript.add_tool_results([ToolResultPart(call_id=i, tool_name="create_data", content="ok") for i in live_ids])

    await agent._reload_history_after_fold()

    ids = _call_ids(agent.transcript)
    assert sorted(ids) == sorted(set(ids)), ids
    assert "call_history" in ids
    assert [i for i in ids if i.startswith("call_current")] == live_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("n_turns", [30, 61])
async def test_fold_input_contains_every_folded_row(db, n_turns):
    """Every row behind the new watermark reaches the summarizer — none may be
    sliced away by the live-window message cap on the fold path."""
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, n_turns, content="Unique ask number {i} about revenue")
    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True,
                                      budget_model=_model(200_000))
    assert result["status"] == "compacted"
    prompts = "\n".join(c[0][0] for c in instance.inference.call_args_list)
    folded_user_turns = [i for i in range(PROTECT_FIRST_N, n_turns - 2 * MANUAL_KEEP_EXCHANGES) if i % 2 == 0]
    missing = [i for i in folded_user_turns if f"Unique ask number {i} about" not in prompts]
    assert missing == [], f"rows folded without reaching the summarizer: {missing}"


@pytest.mark.asyncio
@pytest.mark.parametrize("n_turns", [42, 57])
async def test_live_window_reports_rows_it_drops_and_keeps_the_opening_ask(db, n_turns):
    """Past the live window's row cap, rows are in neither the window nor a
    summary. The builder must say so (so the governor folds them) and keep the
    opening ask visible meanwhile."""
    from app.ai.context.builders.message_context_builder import MessageContextBuilder
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, n_turns, content="Short ask {i}")
    org_loaded = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()
    section = await MessageContextBuilder(db, org_loaded, report).build(max_messages=40)
    # A trailing user row is the ask being answered now, not history.
    trailing_user = (n_turns - 1) % 2 == 0
    assert section.dropped_count == n_turns - 40 - (1 if trailing_user else 0)
    rendered = section.render()
    assert "Short ask 0" in rendered.split("<opening_exchange>")[1]


@pytest.mark.asyncio
async def test_live_window_reports_nothing_dropped_when_it_fits(db):
    from app.ai.context.builders.message_context_builder import MessageContextBuilder
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 30)
    org_loaded = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()
    section = await MessageContextBuilder(db, org_loaded, report).build(max_messages=40)
    assert section.dropped_count == 0
    assert "<opening_exchange>" not in section.render()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tail_rows", [10, 20])
async def test_window_fold_leaves_room_in_the_live_window(db, max_tail_rows):
    """A window-exhaustion fold keeps at most ~max_tail_rows verbatim even
    when the token tail would protect everything."""
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 60, content="Short ask {i}")
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="auto_sync", force=True,
                                      budget_model=_model(1_000_000), max_tail_rows=max_tail_rows)
    assert result["status"] == "compacted"
    kept = 60 - PROTECT_FIRST_N - result["compacted_turns"]
    assert 2 <= kept <= max_tail_rows


async def _add_event(db, report, kind, content, minute):
    from app.ai.context.session_events import EVENT_ROLE
    e = Completion(prompt={"content": content}, completion={}, status="success", model="event",
                   role=EVENT_ROLE, message_type=kind, report_id=str(report.id))
    db.add(e)
    await db.flush()
    e.created_at = datetime(2026, 1, 1) + timedelta(minutes=minute, seconds=30)
    await db.commit()
    return e


@pytest.mark.asyncio
@pytest.mark.parametrize("llm_fails", [True, False])
async def test_durable_events_survive_compaction_with_or_without_the_summarizer(db, llm_fails):
    """Feedback and rejections have no other home once folded: they must be in
    the stored summary whether the LLM summarized or the fallback ran."""
    from app.ai.context.session_events import EVENT_DURABLE
    kind = sorted(EVENT_DURABLE)[0]
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 20)
    await _add_event(db, report, kind, "User rejected the suggestion to exclude refunds", minute=5)
    llm_patch, _ = (_mock_llm(side_effect=RuntimeError("down")) if llm_fails else _mock_llm(SUMMARY))
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert result["status"] == "compacted"
    state = await _state(db, report.id)
    rendered = render_summary_for_prompt(state.summary_json)
    assert "User rejected the suggestion to exclude refunds" in rendered

    # And it is still there after a later fold that does not see the event again.
    await _add_turns(db, report, user, 20, start=100)
    llm_patch2, _ = _mock_llm(SUMMARY)
    with llm_patch2:
        await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    state = await _state(db, report.id)
    assert "User rejected the suggestion to exclude refunds" in render_summary_for_prompt(state.summary_json)


@pytest.mark.asyncio
@pytest.mark.parametrize("model_feedback", [["only the newest: REJECTION_B"], ["REJECTION_B", "something else"]])
async def test_earlier_feedback_survives_a_summarizer_that_returns_a_subset(db, model_feedback):
    """A later fold whose summarizer returns a NON-empty feedback list that
    omits an older rejection must not erase it: its event row is already
    behind the previous watermark, so the stored list is its only home."""
    from app.ai.context.session_events import EVENT_DURABLE
    kind = sorted(EVENT_DURABLE)[0]
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 20)
    await _add_event(db, report, kind, "REJECTION_A: do not exclude refunds", minute=5)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        await _svc().compact(db, report, org, _model(), mode="manual", force=True)

    await _add_turns(db, report, user, 20, start=100)
    await _add_event(db, report, kind, "REJECTION_B: do not drop cancelled orders", minute=105)
    second = dict(SUMMARY, user_feedback=model_feedback)
    llm_patch2, _ = _mock_llm(second)
    with llm_patch2:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True)
    assert result["status"] == "compacted"
    rendered = render_summary_for_prompt((await _state(db, report.id)).summary_json)
    assert "REJECTION_A: do not exclude refunds" in rendered
    assert "REJECTION_B: do not drop cancelled orders" in rendered


async def _add_child_rows(db, report, user, minute, goal="CHILD_GOAL internal sub-agent task"):
    """A hidden sub-agent exchange on the parent report (trigger_source='sub_agent')."""
    rows = []
    for j, role in enumerate(("user", "system")):
        c = Completion(
            prompt={"content": goal if role == "user" else ""},
            completion={"content": "child answer"} if role == "system" else {},
            status="success", model="test", role=role, message_type="ai_completion",
            report_id=str(report.id), user_id=str(user.id) if role == "user" else None,
            trigger_source="sub_agent",
        )
        db.add(c)
        await db.flush()
        c.created_at = datetime(2026, 1, 1) + timedelta(minutes=minute, seconds=10 + j)
        rows.append(c)
    await db.commit()
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("llm_fails", [False, True])
async def test_sub_agent_rows_never_count_as_parent_turns(db, llm_fails):
    """keep-N, can_compact, the folded scope and the fallback's user asks must
    see only the parent conversation — never hidden sub-agent completions."""
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 20)
    # Child exchanges interleaved inside the last parent exchanges.
    for m in (15, 17, 18):
        await _add_child_rows(db, report, user, minute=m)
    llm_patch, instance = (_mock_llm(side_effect=RuntimeError("down")) if llm_fails else _mock_llm(SUMMARY))
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), mode="manual", force=True, keep=2)
    assert result["status"] == "compacted"
    # Exactly the parent rows minus the opening exchange and the last 2 parent exchanges.
    assert result["compacted_turns"] == _manual_scope_size(20, 2)
    if not llm_fails:
        assert "CHILD_GOAL" not in instance.inference.call_args[0][0]
    state = await _state(db, report.id)
    assert "CHILD_GOAL" not in render_summary_for_prompt(state.summary_json)

    # can_compact counts parent exchanges only: 2 parent exchanges left + many child rows.
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["can_compact"] is False


@pytest.mark.asyncio
async def test_first_state_creation_race_keeps_both_anchor_and_summary(db):
    """The end-of-run anchor write can create the state row while a first
    fold is summarizing; the fold must update that row, not fail on a second
    INSERT."""
    org, report, user = await _seed_report(db)
    report_id = str(report.id)
    await _add_turns(db, report, user, 20)
    svc = _svc()
    original = svc._summarize

    async def _summarize_after_anchor_write(llm, prompt, timeout_s):
        maker = async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)
        async with maker() as other:
            await ContextCompactionService.record_prompt_anchor(other, report_id, 31_000, 22_000)
        return await original(llm, prompt, timeout_s)

    svc._summarize = _summarize_after_anchor_write
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        result = await svc.compact(db, report, org, _model(), mode="manual", force=True)
    assert result["status"] == "compacted", result
    state = await _state(db, report_id)
    assert ContextCompactionService.anchor_offset(state) == 9_000
    assert state.summary_json.get("goal") == SUMMARY["goal"]
    assert state.covers_until_completion_id == result["covers_until_completion_id"]


@pytest.mark.asyncio
async def test_tightened_overflow_budget_lets_a_second_fold_make_progress(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 60)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        first = await _svc().compact(db, report, org, _model(), mode="overflow", force=True,
                                     budget_model=_model(20_000))
        again = await _svc().compact(db, report, org, _model(), mode="overflow", force=True,
                                     budget_model=_model(20_000))
        tighter = await _svc().compact(db, report, org, _model(), mode="overflow", force=True,
                                       budget_model=_model(20_000), budget_factor=0.2)
    assert first["status"] == "compacted"
    assert again["status"] == "nothing_to_compact"
    assert tighter["status"] == "compacted" and tighter["compacted_turns"] > 0
