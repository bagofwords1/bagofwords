"""Rolling context compaction — service invariants (Hermes geometry).

Covers the critical set from docs/design/agent-v2-compaction.md:
  - budgets derive from the model window (conversation → trigger → tail → summary cap)
  - protected tail is token-measured with a completion-count floor
  - the opening exchange is never folded; opening_request is set programmatically
  - watermark advance is atomic with summary + totals
  - entity ids must survive verbatim (hallucinated ids dropped)
  - fail-open: summarizer errors leave state untouched
  - compaction markers are excluded from future scope
  - message context builder renders summary + head + post-watermark window
  - the build-time trigger emits context.compacted from the background task
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
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Import the same model modules alembic/env.py registers so Base.metadata
# knows the full mapped graph (a blanket pkgutil sweep trips over dead model
# modules that env.py deliberately omits).
_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 — test-only, mirrors env.py verbatim

from app.models.base import Base
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.report import Report
from app.models.report_context_state import ReportContextState
from app.models.user import User

from app.services.context_compaction_service import (
    COMPACTION_MESSAGE_TYPE,
    DEFAULT_MODEL_WINDOW,
    MESSAGES_WINDOW,
    PROTECT_FIRST_N,
    PROTECT_LAST_MIN,
    ContextCompactionService,
    compaction_budgets,
    _enforce_summary_budget,
    _parse_summary_json,
    _validate_entities,
    render_summary_for_prompt,
)


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
    org = Organization(name="Org")
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
            completion={"content": f"Answer {start + i}"} if role == "system" else {},
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
    """LLM-model stand-in with an explicit context window. window=1000 makes
    the token tail budget (~12 tokens) negligible, so the PROTECT_LAST_MIN
    count floor binds and scope arithmetic is deterministic."""
    m = MagicMock()
    m.context_window_tokens = window
    return m


def _mock_llm(response_json: dict):
    """Patch the LLM class the service imports; .inference returns canned JSON."""
    instance = MagicMock()
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

# Foldable rows given N total turns: minus protected head, minus tail floor.
def _scope_size(n_turns: int) -> int:
    return n_turns - PROTECT_FIRST_N - PROTECT_LAST_MIN


def _svc():
    # Fresh instance per test — the module singleton carries per-report locks.
    return ContextCompactionService()


# ------------------------------------------------------------------ geometry


def test_budgets_derive_from_model_window():
    b = compaction_budgets(_model(200_000))
    assert b == {
        "window": 200_000,
        "conversation_tokens": 25_000,
        "trigger_tokens": 12_500,
        "tail_tokens": 2_500,
        "summary_max_tokens": 10_000,
    }
    # No window → default
    assert compaction_budgets(_model(None))["window"] == DEFAULT_MODEL_WINDOW
    # Small windows keep the summary floor
    assert compaction_budgets(_model(20_000))["summary_max_tokens"] == 2_000


def test_summary_budget_enforcement_trims_entities_last():
    summary = {
        "goal": "g",
        "opening_request": "first ask",
        "critical_context": [f"note {i} " * 30 for i in range(50)],
        "entities": [{"type": "query", "id": f"id-{i}", "title": "t"} for i in range(30)],
    }
    out = _enforce_summary_budget(summary, 300)
    assert out["goal"] == "g" and out["opening_request"] == "first ask"
    assert len(out["critical_context"]) < 50
    assert len(out["entities"]) >= 10  # ids sacrificed last, never below 10


# ------------------------------------------------------------------ service


@pytest.mark.asyncio
async def test_nothing_to_compact_below_protected_floor(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, PROTECT_FIRST_N + PROTECT_LAST_MIN)
    result = await _svc().compact(db, report, org, _model(), force=True)
    assert result["status"] == "nothing_to_compact"
    assert await ContextCompactionService.get_state(db, str(report.id)) is None


@pytest.mark.asyncio
async def test_token_tail_protects_more_than_count_floor(db):
    """With a big window the token tail budget (2.5k) swallows a tiny
    conversation entirely — nothing to fold even when forced."""
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 40)
    result = await _svc().compact(db, report, org, _model(200_000), force=True)
    assert result["status"] == "nothing_to_compact"


@pytest.mark.asyncio
async def test_auto_below_threshold_but_force_compacts(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 20)
    svc = _svc()

    # window=5000 → trigger ~312 tokens (above this tiny conversation's ~190)
    # while the tail budget (~62 tokens) is under the 12-row floor, so scope
    # exists but the auto threshold doesn't fire.
    model = _model(5000)
    result = await svc.compact(db, report, org, model, force=False)
    assert result["status"] == "below_threshold"

    # Force skips the threshold, not the protected tail
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        result = await svc.compact(db, report, org, model, force=True)
    assert result["status"] == "compacted"
    assert result["compacted_turns"] == _scope_size(20)


@pytest.mark.asyncio
async def test_compact_advances_watermark_totals_and_marker(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 30)
    n_scope = _scope_size(30)  # 16
    scope_expected = rows[PROTECT_FIRST_N : PROTECT_FIRST_N + n_scope]

    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, _model(), force=True)

    assert result["status"] == "compacted"
    state = await ContextCompactionService.get_state(db, str(report.id))
    assert state is not None
    assert state.covers_until_completion_id == str(scope_expected[-1].id)
    assert state.covered_turns == n_scope
    assert state.tokens_compacted_total > 0
    assert result["tokens_compacted_total"] == state.tokens_compacted_total
    # The result exposes the watermark so the UI can anchor the divider
    assert result["covers_until_completion_id"] == str(scope_expected[-1].id)
    assert state.summary_json["goal"] == SUMMARY["goal"]
    # Opening request captured programmatically from the first user turn
    assert "What is revenue for month 0?" in state.summary_json["opening_request"]

    # No marker completions — the divider is state-derived, not an event row
    from sqlalchemy import select
    markers = (await db.execute(
        select(Completion).where(
            Completion.report_id == str(report.id),
            Completion.message_type == COMPACTION_MESSAGE_TYPE,
        )
    )).scalars().all()
    assert markers == []

    # The summarizer saw the scope digests — not the protected head, not the tail
    prompt_sent = instance.inference.call_args[0][0]
    assert "Answer 3" in prompt_sent
    assert "Answer 1\"" not in prompt_sent  # head system turn stays out
    assert "Answer 19" not in prompt_sent   # protected tail stays out

    # Immediately recompacting finds nothing (tail is protected)
    again = await _svc().compact(db, report, org, _model(), force=True)
    assert again["status"] == "nothing_to_compact"


@pytest.mark.asyncio
async def test_builder_renders_summary_head_and_window(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 30)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        await _svc().compact(db, report, org, _model(), force=True)

    from sqlalchemy import select
    org_loaded = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()
    from app.ai.context.builders.message_context_builder import MessageContextBuilder
    builder = MessageContextBuilder(db, org_loaded, report)
    section = await builder.build(max_messages=MESSAGES_WINDOW)
    rendered = section.render()

    # head (2) + protected tail (12), summary attached
    assert len(section.items) == PROTECT_FIRST_N + PROTECT_LAST_MIN
    assert "<history_summary>" in rendered
    assert "Track monthly revenue" in rendered
    assert "What is revenue for month 0?" in rendered      # protected head, minified
    assert "What is revenue for month 4?" not in rendered  # folded middle
    assert "Answer 29" in rendered                          # recent tail

    # build_context (legacy string path) behaves the same
    text = await builder.build_context(max_messages=MESSAGES_WINDOW)
    assert "Track monthly revenue" in text
    assert "[Opening exchange]" in text
    assert "What is revenue for month 0?" in text
    assert "What is revenue for month 4?" not in text


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
        result = await _svc().compact(db, report, org, _model(), force=True)
    assert result["status"] == "compacted"
    state = await ContextCompactionService.get_state(db, str(report.id))
    ids = [e["id"] for e in state.summary_json["entities"]]
    assert ids == [real_id]
    assert real_id in render_summary_for_prompt(state.summary_json)


@pytest.mark.asyncio
async def test_summarizer_failure_is_fail_open(db):
    org, report, user = await _seed_report(db)
    report_id = str(report.id)  # captured before compact — its rollback expires ORM objects
    await _add_turns(db, report, user, 30)

    instance = MagicMock()
    instance.inference.side_effect = RuntimeError("LLM down")
    with patch("app.ai.llm.LLM", MagicMock(return_value=instance)):
        result = await _svc().compact(db, report, org, _model(), force=True)

    assert result["status"] == "error"
    assert await ContextCompactionService.get_state(db, report_id) is None
    from sqlalchemy import select
    markers = (await db.execute(
        select(Completion).where(Completion.message_type == COMPACTION_MESSAGE_TYPE)
    )).scalars().all()
    assert markers == []


@pytest.mark.asyncio
async def test_legacy_markers_excluded_from_scope(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 30)
    # Legacy marker row from an early build — must never be folded or counted
    legacy = Completion(
        prompt={"content": ""},
        completion={"content": "Compacted 2 turns (~70 tokens)"},
        status="success", model="system", role="system",
        message_type=COMPACTION_MESSAGE_TYPE, report_id=str(report.id),
    )
    db.add(legacy)
    await db.commit()

    llm_patch, instance = _mock_llm(SUMMARY)
    svc = _svc()
    with llm_patch:
        first = await svc.compact(db, report, org, _model(), force=True)
    assert first["compacted_turns"] == _scope_size(30)
    assert "Compacted 2 turns" not in instance.inference.call_args[0][0]

    await _add_turns(db, report, user, 20, start=100)
    llm_patch2, instance2 = _mock_llm(SUMMARY)
    with llm_patch2:
        second = await svc.compact(db, report, org, _model(), force=True)
    assert second["status"] == "compacted"
    # post-watermark: 12 kept + 20 new = 32 rows (legacy marker excluded,
    # head behind the watermark already) → fold all but the 12-row tail floor
    assert second["compacted_turns"] == 20
    state = await ContextCompactionService.get_state(db, str(report.id))
    assert state.covered_turns == _scope_size(30) + 20
    assert "Compacted 2 turns" not in instance2.inference.call_args[0][0]


@pytest.mark.asyncio
async def test_get_ui_state_can_compact(db):
    org, report, user = await _seed_report(db)
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["can_compact"] is False
    assert ui["tokens_compacted_total"] == 0

    await _add_turns(db, report, user, PROTECT_FIRST_N + PROTECT_LAST_MIN + 1)
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["can_compact"] is True


# ------------------------------------------------------------------ parsing


def test_parse_summary_json_strips_fences_and_unknown_keys():
    parsed = _parse_summary_json('```json\n{"goal": "g", "junk": 1}\n```')
    assert parsed == {"goal": "g"}
    assert _parse_summary_json("not json at all {{{") in (None, {})


def test_validate_entities_requires_verbatim_id():
    s = {"entities": [{"id": "abc-1"}, {"id": "zzz-9"}]}
    out = _validate_entities(s, "digest mentioning abc-1 only")
    assert [e["id"] for e in out["entities"]] == ["abc-1"]


# ------------------------------------------------------------------ agent


@pytest.mark.asyncio
async def test_background_compaction_emits_sse_event(db):
    """_run_auto_compaction (the build-time trigger's background body) emits
    context.compacted on the live event queue when the service compacts."""
    import app.ai.agent_v2 as agent_v2_mod

    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 10)

    agent = object.__new__(agent_v2_mod.AgentV2)
    agent.report = report
    agent.organization = org
    agent.model = _model()
    agent.small_model = _model()
    agent.system_completion = MagicMock(id="sys-1")
    agent.event_queue = MagicMock()
    agent.event_queue.put = AsyncMock()

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
        await agent._run_auto_compaction()

    # 10 turns are inside head+tail protection → nothing folds → no event
    agent.event_queue.put.assert_not_called()

    # Cross the threshold and re-run
    await _add_turns(db, report, user, 20, start=100)
    llm_patch2, _ = _mock_llm(SUMMARY)
    with llm_patch2:
        await agent._run_auto_compaction()

    agent.event_queue.put.assert_called_once()
    event = agent.event_queue.put.call_args[0][0]
    assert event.event == "context.compacted"
    # Watermark id so the page can anchor the divider without a reload
    state = await ContextCompactionService.get_state(db, str(report.id))
    assert event.data["covers_until_completion_id"] == str(state.covers_until_completion_id)
    assert event.data["tokens_compacted_total"] > 0


def test_maybe_schedule_compaction_triggers_once():
    """The build-time detector schedules at most one background task per run
    and only when the rendered window crosses the budget."""
    import asyncio
    import app.ai.agent_v2 as agent_v2_mod

    agent = object.__new__(agent_v2_mod.AgentV2)
    agent._compaction_attempted = False
    agent._compaction_task = None
    agent.model = _model(200_000)
    agent.small_model = _model(200_000)
    agent._run_auto_compaction = AsyncMock()

    def _view(rendered: str, n_items: int):
        messages = MagicMock()
        messages.render.return_value = rendered
        messages.items = [MagicMock()] * n_items
        view = MagicMock()
        view.warm.messages = messages
        return view

    async def _run():
        # Under budget: no task
        agent._maybe_schedule_compaction(_view("small window", 4))
        assert agent._compaction_task is None and agent._compaction_attempted is False

        # Over token budget (12.5k tokens ≈ 50k chars): schedules once
        agent._maybe_schedule_compaction(_view("x" * 60_000, 20))
        assert agent._compaction_attempted is True
        assert agent._compaction_task is not None
        await agent._compaction_task

        # Second trigger in the same run is a no-op
        first_task = agent._compaction_task
        agent._maybe_schedule_compaction(_view("x" * 60_000, 20))
        assert agent._compaction_task is first_task

    asyncio.run(_run())
    agent._run_auto_compaction.assert_awaited_once()
