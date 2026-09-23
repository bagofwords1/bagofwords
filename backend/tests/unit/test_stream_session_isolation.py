"""Streaming must survive a rollback of the agent's shared session.

A rollback expires every instance in an AsyncSession, and reading an expired
column lazy-loads — MissingGreenlet ("greenlet_spawn has not been called")
under asyncio. Streaming paths (SSE sequence numbers, coder reasoning
snapshots) run concurrently with sibling tools on that shared session, so they
must neither depend on its loaded objects nor write through it.
"""

# Mapper registration intentionally runs before the app-model imports below.
# ruff: noqa: E402

from __future__ import annotations

import asyncio
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

_env_src = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
for _stmt in re.findall(r"^from app\.models\S* import \([^)]*\)|^from app\.models[^\n]+", _env_src, re.M):
    exec(_stmt)  # noqa: S102 — test-only, mirrors alembic/env.py

from app.ai.agent_v2 import AgentV2
from app.ai.llm.types import ReasoningCompleteEvent, ReasoningDeltaEvent
from app.models.agent_execution import AgentExecution
from app.models.base import Base
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User
from app.project_manager import ProjectManager


@pytest_asyncio.fixture
async def run_ctx(tmp_path):
    Completion.__table__.c.sigkill.nullable = True
    # File-backed so independent sessions see the same database.
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'stream.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Direct model writes: an in-flight agent run with a streaming tool block
    # is not a state the HTTP API can produce without a live LLM.
    async with maker() as db:
        user = User(name="Streamer", email=f"stream-{uuid.uuid4()}@example.com", hashed_password="x")
        org = Organization(name=f"Stream org {uuid.uuid4()}")
        db.add_all([user, org])
        await db.flush()
        report = Report(title="r", slug=f"r-{uuid.uuid4()}", user_id=str(user.id), organization_id=str(org.id))
        db.add(report)
        await db.flush()
        completion = Completion(
            prompt={}, completion={}, status="in_progress", model="m", role="system",
            report_id=str(report.id), main_router="x",
        )
        db.add(completion)
        await db.flush()
        execution = AgentExecution(completion_id=str(completion.id), status="in_progress", latest_seq=0)
        db.add(execution)
        await db.flush()
        block = CompletionBlock(
            completion_id=str(completion.id), agent_execution_id=str(execution.id),
            source_type="decision", block_index=0, title="Create Data", status="in_progress",
            reasoning="Planner reasoning.",
        )
        db.add(block)
        await db.commit()
        ids = SimpleNamespace(completion=str(completion.id), execution=str(execution.id), block=str(block.id))

    yield SimpleNamespace(maker=maker, ids=ids)
    await engine.dispose()


def _agent(db, execution, maker, completion_id):
    events = []

    async def emit(ev):
        events.append(ev)

    agent = SimpleNamespace(
        db=db,
        _session_maker=maker,
        _tool_db_lock=asyncio.Lock(),
        project_manager=ProjectManager(),
        current_execution=execution,
        system_completion_id=completion_id,
        _emit_sse_event=emit,
    )
    return agent, events


@pytest.mark.asyncio
@pytest.mark.parametrize("before, after", [(1, 1), (3, 5), (17, 2)])
async def test_stream_seq_keeps_increasing_across_shared_session_rollback(run_ctx, before, after):
    pm = ProjectManager()
    async with run_ctx.maker() as db:
        execution = await db.get(AgentExecution, run_ctx.ids.execution)
        seqs = [await pm.next_seq(db, execution) for _ in range(before)]

        await db.rollback()  # expires `execution`, discards uncommitted latest_seq
        assert inspect(execution).expired_attributes

        seqs += [await pm.next_seq(db, execution) for _ in range(after)]

    assert seqs == list(range(1, before + after + 1))


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_after", [None, 0, 4])
async def test_coder_reasoning_stream_is_isolated_from_shared_session(run_ctx, rollback_after):
    ids = run_ctx.ids
    async with run_ctx.maker() as db:
        execution = await db.get(AgentExecution, ids.execution)
        shared_block = await db.get(CompletionBlock, ids.block)
        agent, events = _agent(db, execution, run_ctx.maker, ids.completion)
        callback = AgentV2._coder_reasoning_callback(agent, ids.block)

        chunks = [f"step {i} of the coder's plan. " for i in range(8)]
        for i, chunk in enumerate(chunks):
            if i == rollback_after:
                # A sibling tool / best-effort path rolls the shared session back.
                await db.rollback()
            await callback(ReasoningDeltaEvent(text=chunk))
        await callback(ReasoningCompleteEvent(text=""))

        # The stream wrote nothing through the shared session (the in-memory
        # seq counter is flushed later by the agent's own commits)...
        assert not db.new
        assert shared_block not in db.sync_session.dirty
        # ...and kept its sequence strictly increasing through the rollback.
        seqs = [ev.seq for ev in events]
        assert seqs and all(b > a for a, b in zip(seqs, seqs[1:]))
        streamed = None if rollback_after is not None else shared_block

    async with run_ctx.maker() as fresh:
        persisted = (await fresh.get(CompletionBlock, ids.block)).reasoning
    assert persisted.startswith("Planner reasoning.")
    assert all(chunk.strip() in persisted for chunk in chunks)
    if streamed is not None:
        # A still-loaded shared copy mirrors the row rather than going stale.
        assert inspect(streamed).dict["reasoning"] == persisted
