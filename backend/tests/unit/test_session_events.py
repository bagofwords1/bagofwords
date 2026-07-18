"""Session events — the silent ledger (role='event' completions).

Validates the design agreed in the message-context discussion:

  - SessionEventService.emit inserts a role='event' completion and does NOT
    increment turn_index (a silent event does not start a turn).
  - The message context builder renders LLM-visible events interleaved by
    timestamp between the turns they fall between.
  - Events do NOT consume the max_messages turn budget (a burst can't push
    real turns out of the detailed window).
  - LLM-hidden kinds (pure audit, e.g. export_downloaded) never reach context.
  - Consecutive same-kind events collapse to one line with a ×N count.
  - Compaction: ephemeral events are dropped at the watermark; durable events
    (feedback, rejections) are folded into the summary digest.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 — test-only, mirrors env.py verbatim

from app.models.base import Base
from app.models.completion import Completion
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User

from app.ai.context.builders.message_context_builder import MessageContextBuilder
from app.ai.context.session_events import (
    EVENT_ROLE,
    EXPORT_DOWNLOADED,
    FEEDBACK_GIVEN,
    FILE_UPLOADED,
    LLM_CHANGED,
    is_event_kind_durable,
    is_event_kind_llm_visible,
    is_event_kind_ui_visible,
)
from app.services.session_event_service import SessionEventService


# ------------------------------------------------------------------ fixtures

@pytest_asyncio.fixture
async def db():
    Completion.__table__.c.sigkill.nullable = True
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_report(db: AsyncSession):
    user = User(name="U", email=f"u-{uuid.uuid4()}@x.dev", hashed_password="x")
    db.add(user)
    await db.flush()
    org = Organization(name="Org")
    db.add(org)
    await db.flush()
    report = Report(title="R", slug=f"r-{uuid.uuid4()}", user_id=str(user.id), organization_id=str(org.id))
    db.add(report)
    await db.commit()
    org = (await db.execute(select(Organization).where(Organization.id == str(org.id)))).unique().scalar_one()
    return org, report, user


async def _add_turn(db, report, user, *, role, content, minute):
    base = datetime(2026, 1, 1)
    c = Completion(
        prompt={"content": content} if role == "user" else {"content": ""},
        completion={"content": content} if role == "system" else {},
        status="success",
        model="test",
        role=role,
        message_type="ai_completion",
        turn_index=minute,
        report_id=str(report.id),
        user_id=str(user.id) if role == "user" else None,
    )
    db.add(c)
    await db.flush()
    c.created_at = base + timedelta(minutes=minute)
    await db.commit()
    return c


async def _stamp(db, completion, minute):
    completion.created_at = datetime(2026, 1, 1) + timedelta(minutes=minute)
    await db.commit()


# ------------------------------------------------------------------ emit

@pytest.mark.asyncio
async def test_emit_inserts_event_without_incrementing_turn(db):
    org, report, user = await _seed_report(db)
    await _add_turn(db, report, user, role="user", content="hi", minute=0)
    sys = await _add_turn(db, report, user, role="system", content="hello", minute=1)

    ev = await SessionEventService.emit(
        db, report=report, user=user, kind=LLM_CHANGED,
        meta={"from": "claude-opus", "to": "gpt-5"},
    )
    assert ev.role == EVENT_ROLE
    assert ev.message_type == LLM_CHANGED
    assert ev.completion == {"content": ""}
    # turn_index is the last turn's index — NOT incremented past it.
    assert ev.turn_index == sys.turn_index
    assert ev.prompt["meta"]["from"] == "claude-opus"
    assert ev.prompt["meta"]["to"] == "gpt-5"
    # The actor is recorded in meta; the text is an impersonal announcement.
    assert ev.prompt["meta"]["actor"] == "U"
    assert ev.prompt["content"] == "Model was switched from claude-opus to gpt-5"


# ------------------------------------------------------------------ policy maps

def test_policy_maps():
    # UI: files & shares visible; model change & feedback hidden.
    assert is_event_kind_ui_visible(FILE_UPLOADED) is True
    assert is_event_kind_ui_visible(LLM_CHANGED) is False
    assert is_event_kind_ui_visible(FEEDBACK_GIVEN) is False
    # LLM: everything except pure audit.
    assert is_event_kind_llm_visible(FEEDBACK_GIVEN) is True
    assert is_event_kind_llm_visible(EXPORT_DOWNLOADED) is False
    # Durable: feedback survives compaction; model change does not.
    assert is_event_kind_durable(FEEDBACK_GIVEN) is True
    assert is_event_kind_durable(LLM_CHANGED) is False


# ------------------------------------------------------------------ context render

@pytest.mark.asyncio
async def test_event_renders_interleaved_in_context(db):
    org, report, user = await _seed_report(db)
    await _add_turn(db, report, user, role="user", content="show me revenue", minute=0)
    await _add_turn(db, report, user, role="system", content="here it is", minute=1)
    ev = await SessionEventService.emit(
        db, report=report, user=user, kind=FEEDBACK_GIVEN,
        meta={"direction": -1, "message": "wrong window"},
    )
    await _stamp(db, ev, 2)
    await _add_turn(db, report, user, role="user", content="try by region", minute=3)
    # A following system turn so the "try by region" user turn isn't dropped as
    # the trailing in-progress message.
    await _add_turn(db, report, user, role="system", content="regional view", minute=4)

    builder = MessageContextBuilder(db, org, report)

    text = await builder.build_context(max_messages=20)
    assert "show me revenue" in text
    assert "Event (" in text
    assert "thumbed down" in text and "wrong window" in text
    # Ordering: event sits between the assistant answer and the next ask.
    assert text.index("here it is") < text.index("Event (") < text.index("try by region")

    # Object path renders a role='event' MessageItem too.
    section = await builder.build(max_messages=20)
    roles = [i.role for i in section.items]
    assert "event" in roles


@pytest.mark.asyncio
async def test_hidden_kind_never_reaches_context(db):
    org, report, user = await _seed_report(db)
    await _add_turn(db, report, user, role="user", content="q", minute=0)
    await _add_turn(db, report, user, role="system", content="a", minute=1)
    ev = await SessionEventService.emit(
        db, report=report, user=user, kind=EXPORT_DOWNLOADED, meta={"format": "pdf"},
    )
    await _stamp(db, ev, 2)

    text = await (MessageContextBuilder(db, org, report)).build_context(max_messages=20)
    assert "export" not in text.lower()
    assert "Event (" not in text


@pytest.mark.asyncio
async def test_events_do_not_consume_turn_budget(db):
    """A burst of events must not push real turns out of the window."""
    org, report, user = await _seed_report(db)
    # 4 conversational turns.
    for i in range(4):
        role = "user" if i % 2 == 0 else "system"
        await _add_turn(db, report, user, role=role, content=f"turn-{i}", minute=i)
    # 10 events interleaved after the last turn.
    for j in range(10):
        ev = await SessionEventService.emit(
            db, report=report, user=user, kind=FILE_UPLOADED,
            meta={"filename": f"f{j}.csv"},
        )
        await _stamp(db, ev, 4 + j)

    # max_messages=4 must still render all 4 conversational turns despite the
    # 10 events (events are fetched by time-range, not as window slots).
    text = await (MessageContextBuilder(db, org, report)).build_context(max_messages=4)
    for i in range(4):
        assert f"turn-{i}" in text, f"turn-{i} was pushed out of the window by events"


@pytest.mark.asyncio
async def test_consecutive_same_kind_events_collapse(db):
    org, report, user = await _seed_report(db)
    await _add_turn(db, report, user, role="user", content="q", minute=0)
    await _add_turn(db, report, user, role="system", content="a", minute=1)
    for j in range(3):
        ev = await SessionEventService.emit(
            db, report=report, user=user, kind=LLM_CHANGED,
            meta={"to": f"model-{j}"},
        )
        await _stamp(db, ev, 2 + j)

    text = await (MessageContextBuilder(db, org, report)).build_context(max_messages=20)
    # Collapsed into a single ×3 line, keeping the latest value.
    assert "(×3)" in text
    assert "model-2" in text
    assert text.count("Event (") == 1


# ------------------------------------------------------------------ compaction

@pytest.mark.asyncio
async def test_compaction_folds_durable_drops_ephemeral(db):
    """When the builder digests a fold scope, durable events (feedback) render
    into the summary; ephemeral events (model change) are dropped."""
    org, report, user = await _seed_report(db)
    u = await _add_turn(db, report, user, role="user", content="old question", minute=0)
    s = await _add_turn(db, report, user, role="system", content="old answer", minute=1)
    durable = await SessionEventService.emit(
        db, report=report, user=user, kind=FEEDBACK_GIVEN,
        meta={"direction": -1, "message": "off base"},
    )
    await _stamp(db, durable, 2)
    ephemeral = await SessionEventService.emit(
        db, report=report, user=user, kind=LLM_CHANGED, meta={"to": "gpt-5"},
    )
    await _stamp(db, ephemeral, 3)

    # The compaction fold path passes explicit completion_ids (the scope). Only
    # the durable event should render; the ephemeral one is skipped.
    builder = MessageContextBuilder(db, org, report)
    section = await builder.build(
        completion_ids=[str(u.id), str(s.id), str(durable.id), str(ephemeral.id)]
    )
    rendered = section.render()
    assert "off base" in rendered          # durable feedback folded
    assert "gpt-5" not in rendered         # ephemeral model change dropped
    event_items = [i for i in section.items if i.role == "event"]
    assert len(event_items) == 1


# ------------------------------------------------------------------ real hook

@pytest.mark.asyncio
async def test_feedback_service_hook_emits_events(db):
    """The real CompletionFeedbackService hook emits given/changed/removed
    events keyed to the completion, and they render in context."""
    from types import SimpleNamespace
    from app.models.completion_feedback import CompletionFeedback
    from app.services.completion_feedback_service import CompletionFeedbackService
    from app.ai.context.session_events import (
        FEEDBACK_GIVEN, FEEDBACK_CHANGED, FEEDBACK_REMOVED,
    )

    org, report, user = await _seed_report(db)
    await _add_turn(db, report, user, role="user", content="q", minute=0)
    sys = await _add_turn(db, report, user, role="system", content="a", minute=1)

    svc = CompletionFeedbackService()
    fb = CompletionFeedback(
        user_id=str(user.id), completion_id=str(sys.id),
        organization_id=str(org.id), direction=-1, message="wrong",
    )

    # given → changed → removed
    await svc._emit_feedback_event(db, sys, fb, user, changed=False)
    fb.direction = 1
    await svc._emit_feedback_event(db, sys, fb, user, changed=True)
    await svc._emit_feedback_event(db, sys, fb, user, changed=False, removed=True)

    rows = (await db.execute(
        select(Completion).where(
            Completion.report_id == str(report.id),
            Completion.role == EVENT_ROLE,
        ).order_by(Completion.created_at.asc())
    )).scalars().all()
    kinds = [r.message_type for r in rows]
    assert kinds == [FEEDBACK_GIVEN, FEEDBACK_CHANGED, FEEDBACK_REMOVED]
    # Each event targets the completion it was about.
    assert all(r.prompt.get("target_id") == str(sys.id) for r in rows)
