"""Regression tests for the poisoned-shared-session cascade.

Reproduces the field failure:

    Agent failed: This Session's transaction has been rolled back due to a
    previous exception during flush ... (raised as a result of Query-invoked
    autoflush) ... UPDATE agent_executions SET latest_seq=$1, updated_at=$2
    WHERE agent_executions.id = $3 ... current transaction is aborted.

Mechanism: a broad ``except`` swallows a failed ORM flush WITHOUT rolling back,
leaving the shared AsyncSession in pending-rollback state. ``next_seq`` then
bumps ``agent_execution.latest_seq`` in memory (streaming path, no commit), so
the row is dirty. The next query autoflushes that dirty row and crashes far
from the real cause.

``app.core.session_hygiene.rollback_if_poisoned`` heals the session at the
swallow boundary; the agent wires it in via ``_recover_poisoned_session`` (loop
rescue + knowledge-harness dispatch).
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import PendingRollbackError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import main  # noqa: F401 — registers every SQLAlchemy mapper before create_all

from app.models.base import Base
from app.models.agent_execution import AgentExecution
from app.project_manager import ProjectManager
from app.core.session_hygiene import rollback_if_poisoned


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as session:
        yield session
    await engine.dispose()


async def _seed_execution(db) -> AgentExecution:
    ae = AgentExecution(
        id=str(uuid.uuid4()),
        completion_id=f"seed-{uuid.uuid4()}",  # sqlite FK enforcement off by default
        status="in_progress",
        latest_seq=0,
        is_eval_run=False,
    )
    db.add(ae)
    await db.commit()
    await db.refresh(ae)
    return ae


async def _poison_via_swallowed_flush(db):
    """Add an invalid row and flush; swallow the failure WITHOUT rolling back —
    the buggy pattern. Leaves the session in pending-rollback state."""
    try:
        db.add(AgentExecution(
            id=str(uuid.uuid4()),
            completion_id=None,   # NOT NULL -> the flush raises
            status="in_progress",
            latest_seq=0,
            is_eval_run=False,
        ))
        await db.flush()
    except Exception:
        pass  # swallow, no rollback  <-- the bug


@pytest.mark.asyncio
async def test_rollback_if_poisoned_noop_when_healthy(db):
    assert db.is_active is True
    assert await rollback_if_poisoned(db) is False
    assert db.is_active is True


@pytest.mark.asyncio
async def test_rollback_if_poisoned_heals_poisoned_session(db):
    await _seed_execution(db)
    await _poison_via_swallowed_flush(db)
    assert db.is_active is False  # poisoned
    assert await rollback_if_poisoned(db) is True
    assert db.is_active is True   # healed


@pytest.mark.asyncio
async def test_latest_seq_autoflush_cascade_reproduces_without_heal(db):
    """The exact field crash: poisoned session + dirty latest_seq -> the next
    query's autoflush raises PendingRollbackError."""
    pm = ProjectManager()
    ae = await _seed_execution(db)
    await _poison_via_swallowed_flush(db)

    with pytest.raises(PendingRollbackError) as exc_info:
        await pm.next_seq(db, ae)                 # in-memory bump -> row dirty
        await db.execute(select(AgentExecution))  # autoflush -> boom
    assert "previous exception during flush" in str(exc_info.value)


@pytest.mark.asyncio
async def test_heal_before_post_analysis_avoids_cascade(db):
    """With the boundary heal (+ reload of the expired instance), the same
    post-swallow DB work succeeds."""
    pm = ProjectManager()
    ae = await _seed_execution(db)
    await _poison_via_swallowed_flush(db)

    healed = await rollback_if_poisoned(db)
    assert healed is True
    await db.refresh(ae)  # rollback expired instances; reload before attr access

    seq = await pm.next_seq(db, ae)
    assert seq == 1
    rows = (await db.execute(select(AgentExecution))).scalars().all()
    assert len(rows) == 1  # only the seeded row; the poison row never committed
