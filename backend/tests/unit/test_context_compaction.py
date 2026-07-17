"""Rolling context compaction — service invariants.

Covers the critical set from docs/design/agent-v2-compaction.md:
  - watermark advance is atomic with summary + totals
  - entity ids must survive verbatim (hallucinated ids dropped)
  - fail-open: summarizer errors leave state untouched
  - compaction markers are excluded from future scope
  - the recent tail is never summarized (force skips thresholds, not scope)
  - message context builder renders summary + post-watermark window only
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
    KEEP_RECENT_TURNS,
    ContextCompactionService,
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


# ------------------------------------------------------------------ helpers


def _svc():
    # Fresh instance per test — the module singleton carries per-report locks.
    return ContextCompactionService()


# ------------------------------------------------------------------ tests


@pytest.mark.asyncio
async def test_nothing_to_compact_below_recent_tail(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, KEEP_RECENT_TURNS)  # all inside the kept tail
    result = await _svc().compact(db, report, org, MagicMock(), force=True)
    assert result["status"] == "nothing_to_compact"
    assert await ContextCompactionService.get_state(db, str(report.id)) is None


@pytest.mark.asyncio
async def test_auto_below_threshold_but_force_compacts(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, KEEP_RECENT_TURNS + 2)  # tiny scope, low tokens
    svc = _svc()

    result = await svc.compact(db, report, org, MagicMock(), force=False)
    assert result["status"] == "below_threshold"

    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        result = await svc.compact(db, report, org, MagicMock(), force=True)
    assert result["status"] == "compacted"
    assert result["compacted_turns"] == 2


@pytest.mark.asyncio
async def test_compact_advances_watermark_totals_and_marker(db):
    org, report, user = await _seed_report(db)
    rows = await _add_turns(db, report, user, 10)
    scope_expected = rows[: 10 - KEEP_RECENT_TURNS]

    llm_patch, instance = _mock_llm(SUMMARY)
    with llm_patch:
        result = await _svc().compact(db, report, org, MagicMock(), force=True)

    assert result["status"] == "compacted"
    state = await ContextCompactionService.get_state(db, str(report.id))
    assert state is not None
    assert state.covers_until_completion_id == str(scope_expected[-1].id)
    assert state.covered_turns == len(scope_expected)
    assert state.tokens_compacted_total > 0
    assert result["tokens_compacted_total"] == state.tokens_compacted_total
    assert state.summary_json["goal"] == SUMMARY["goal"]

    # Marker completion persisted for the transcript divider
    from sqlalchemy import select
    markers = (await db.execute(
        select(Completion).where(
            Completion.report_id == str(report.id),
            Completion.message_type == COMPACTION_MESSAGE_TYPE,
        )
    )).scalars().all()
    assert len(markers) == 1
    assert "Compacted 4 turns" in markers[0].completion["content"]

    # The summarizer saw the scope digests, not the kept tail
    prompt_sent = instance.inference.call_args[0][0]
    assert "Answer 1" in prompt_sent
    assert "Answer 9" not in prompt_sent

    # Immediately recompacting finds nothing (tail is protected)
    again = await _svc().compact(db, report, org, MagicMock(), force=True)
    assert again["status"] == "nothing_to_compact"


@pytest.mark.asyncio
async def test_builder_renders_summary_and_post_watermark_window_only(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 10)
    llm_patch, _ = _mock_llm(SUMMARY)
    with llm_patch:
        await _svc().compact(db, report, org, MagicMock(), force=True)

    from sqlalchemy import select
    org_loaded = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()
    from app.ai.context.builders.message_context_builder import MessageContextBuilder
    builder = MessageContextBuilder(db, org_loaded, report)
    section = await builder.build(max_messages=20)
    rendered = section.render()

    assert "<history_summary>" in rendered
    assert "Track monthly revenue" in rendered
    # Covered turns are gone from the detailed window; kept tail remains
    assert "What is revenue for month 0?" not in rendered
    assert "Answer 9" in rendered
    # Marker completions never render as conversation turns
    assert "Compacted 4 turns" not in rendered.replace("history_summary", "")

    # build_context (legacy string path) behaves the same
    text = await builder.build_context(max_messages=20)
    assert "Track monthly revenue" in text
    assert "What is revenue for month 0?" not in text


@pytest.mark.asyncio
async def test_hallucinated_entity_ids_are_dropped(db):
    org, report, user = await _seed_report(db)
    real_id = "3f0f3a52-aaaa-bbbb-cccc-000000000001"
    await _add_turns(
        db, report, user, 10,
        content="Chart for month {i} [query: " + real_id + "]",
    )
    summary = dict(SUMMARY)
    summary["entities"] = [
        {"type": "query", "id": real_id, "title": "Revenue", "state": "created"},
        {"type": "widget", "id": "11111111-dead-beef-0000-999999999999", "title": "Ghost", "state": "?"},
    ]
    llm_patch, _ = _mock_llm(summary)
    with llm_patch:
        result = await _svc().compact(db, report, org, MagicMock(), force=True)
    assert result["status"] == "compacted"
    state = await ContextCompactionService.get_state(db, str(report.id))
    ids = [e["id"] for e in state.summary_json["entities"]]
    assert ids == [real_id]
    assert real_id in render_summary_for_prompt(state.summary_json)


@pytest.mark.asyncio
async def test_summarizer_failure_is_fail_open(db):
    org, report, user = await _seed_report(db)
    report_id = str(report.id)  # captured before compact — its rollback expires ORM objects
    await _add_turns(db, report, user, 10)

    instance = MagicMock()
    instance.inference.side_effect = RuntimeError("LLM down")
    with patch("app.ai.llm.LLM", MagicMock(return_value=instance)):
        result = await _svc().compact(db, report, org, MagicMock(), force=True)

    assert result["status"] == "error"
    assert await ContextCompactionService.get_state(db, report_id) is None
    from sqlalchemy import select
    markers = (await db.execute(
        select(Completion).where(Completion.message_type == COMPACTION_MESSAGE_TYPE)
    )).scalars().all()
    assert markers == []


@pytest.mark.asyncio
async def test_markers_excluded_from_future_scope(db):
    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 10)
    llm_patch, _ = _mock_llm(SUMMARY)
    svc = _svc()
    with llm_patch:
        first = await svc.compact(db, report, org, MagicMock(), force=True)
    assert first["compacted_turns"] == 4

    await _add_turns(db, report, user, 7, start=100)
    llm_patch2, instance2 = _mock_llm(SUMMARY)
    with llm_patch2:
        second = await svc.compact(db, report, org, MagicMock(), force=True)
    assert second["status"] == "compacted"
    # post-watermark: 6 kept + 7 new = 13 turns (marker excluded) → scope 7
    assert second["compacted_turns"] == 7
    state = await ContextCompactionService.get_state(db, str(report.id))
    assert state.covered_turns == 11
    # The marker text never reaches the summarizer
    assert "Compacted 4 turns" not in instance2.inference.call_args[0][0]


@pytest.mark.asyncio
async def test_get_ui_state_can_compact(db):
    org, report, user = await _seed_report(db)
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["can_compact"] is False
    assert ui["tokens_compacted_total"] == 0

    await _add_turns(db, report, user, KEEP_RECENT_TURNS + 1)
    ui = await ContextCompactionService.get_ui_state(db, str(report.id))
    assert ui["can_compact"] is True


def test_parse_summary_json_strips_fences_and_unknown_keys():
    parsed = _parse_summary_json('```json\n{"goal": "g", "junk": 1}\n```')
    assert parsed == {"goal": "g"}
    assert _parse_summary_json("not json at all {{{") in (None, {})


def test_validate_entities_requires_verbatim_id():
    s = {"entities": [{"id": "abc-1"}, {"id": "zzz-9"}]}
    out = _validate_entities(s, "digest mentioning abc-1 only")
    assert [e["id"] for e in out["entities"]] == ["abc-1"]


@pytest.mark.asyncio
async def test_auto_compaction_emits_sse_event(db):
    """_run_auto_compaction emits context.compacted on the live event queue
    when the service compacts, carrying the marker + totals the UI renders."""
    import app.ai.agent_v2 as agent_v2_mod
    from app.services.context_compaction_service import context_compaction_service

    org, report, user = await _seed_report(db)
    await _add_turns(db, report, user, 10)

    agent = object.__new__(agent_v2_mod.AgentV2)
    agent.report = report
    agent.organization = org
    agent.model = MagicMock()
    agent.small_model = MagicMock()
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

    # force=False with only 10 tiny turns → below threshold → no event
    agent.event_queue.put.assert_not_called()

    # Cross the count threshold and re-run
    await _add_turns(db, report, user, 20, start=100)
    llm_patch2, _ = _mock_llm(SUMMARY)
    with llm_patch2:
        await agent._run_auto_compaction()

    agent.event_queue.put.assert_called_once()
    event = agent.event_queue.put.call_args[0][0]
    assert event.event == "context.compacted"
    assert event.data["marker_id"]
    assert "Compacted" in event.data["content"]
    assert event.data["tokens_compacted_total"] > 0
