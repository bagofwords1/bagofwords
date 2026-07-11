"""Buffered data-plane usage metering (queries / bytes).

Contract under test (see docs/feedback-loops/agent-latency-deep-dive.md):
sandboxed `execute_query` must never issue a blocking DB write from the
code-exec worker thread. Enforcement is a cached read; the usage record is
buffered on the UsageLimitContext and persisted by `flush()`. Previously each
query paid up to 2 x 30s of SQLite busy_timeout on metering writes that were
then skipped anyway.

Covers:
- execute_query returns promptly while another connection holds the SQLite
  writer lock (the 60s-stall regression), and the usage event is buffered
- flush() persists usage_events + aggregated usage_counters
- quota enforcement raises when over the limit (including buffered pending)
- derived contexts (for_source) route buffers to the root, keeping their label
- a failed flush re-credits the buffer so a later flush can retry
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid

import pytest
from sqlalchemy import select

import app.services.usage_policy_service as ups
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.usage_policy import UsageCounter, UsageEvent
from app.models.user import User
from app.services.usage_policy_service import (
    METRIC_DATA_BYTES,
    METRIC_DATA_QUERIES,
    UsageLimitContext,
    UsageLimitExceeded,
)


async def _seed_org_user(db):
    org = Organization(name=f"Org-{uuid.uuid4().hex[:8]}")
    db.add(org)
    await db.flush()
    user = User(
        name="U",
        email=f"metering-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        is_verified=True,
    )
    db.add(user)
    await db.flush()
    ids = (str(org.id), str(user.id))
    await db.commit()
    return ids


def _ctx(org_id: str, user_id: str, loop=None) -> UsageLimitContext:
    ctx = UsageLimitContext(
        organization_id=org_id,
        user_id=user_id,
        source="agent",
        source_ref_id="run-1",
        session_maker=async_session_maker,
    )
    ctx.loop = loop
    return ctx


@pytest.fixture(autouse=True)
def _usage_limits_on(monkeypatch):
    # Buffering is gated on the usage_limits feature; force it on so the
    # tests exercise the metering path regardless of the test license.
    monkeypatch.setattr(ups, "has_feature", lambda name: True)
    yield


class _StubClient:
    """Stands in for a data-source client behind TrackedClient."""

    def __init__(self):
        self._bow_connection_id = str(uuid.uuid4())
        self._bow_client_key = "stub:1"
        self._bow_connection_name = "Stub"
        self._bow_data_source_id = None
        self._bow_data_source_name = None

    def execute_query(self, query, *args, **kwargs):
        return [{"a": 1}, {"a": 2}]


def _tracked(client, ctx):
    from app.ai.code_execution.code_execution import QueryCapturingClientWrapper

    return QueryCapturingClientWrapper(client, [], [], usage_context=ctx, client_key="stub:1")


@pytest.mark.asyncio
async def test_execute_query_buffers_without_blocking_on_writer_lock():
    """The 60s-stall regression: with the SQLite writer lock held by another
    connection, a metered execute_query must still return promptly (it only
    reads + buffers), and the usage event must be waiting in the buffer."""
    test_url = os.environ.get("TEST_DATABASE_URL", "")
    if not test_url.startswith("sqlite"):
        pytest.skip("writer-lock stall is SQLite-specific")
    db_path = test_url.replace("sqlite:///", "")

    async with async_session_maker() as db:
        org_id, user_id = await _seed_org_user(db)

    ctx = _ctx(org_id, user_id, loop=asyncio.get_running_loop())
    tracked = _tracked(_StubClient(), ctx)

    raw = sqlite3.connect(db_path)
    try:
        raw.execute("PRAGMA busy_timeout = 100")
        raw.execute("BEGIN IMMEDIATE")  # hold the writer lock

        start = time.monotonic()
        # Same shape as production: the sandbox runs execute_query on a
        # worker thread while the event loop stays free.
        result = await asyncio.to_thread(tracked.execute_query, "select 1")
        elapsed = time.monotonic() - start
    finally:
        raw.rollback()
        raw.close()

    assert result == [{"a": 1}, {"a": 2}]
    # Old behavior waited out busy_timeout (30s per metering write). Generous
    # bound: anything near seconds means a write leaked back into this path.
    assert elapsed < 5.0, f"metered query stalled {elapsed:.1f}s with writer lock held"
    assert len(ctx._pending_data_events) == 2  # query event + bytes event
    metrics = {e["metric"] for e in ctx._pending_data_events}
    assert metrics == {METRIC_DATA_QUERIES, METRIC_DATA_BYTES}


@pytest.mark.asyncio
async def test_flush_persists_buffered_events_and_counters():
    async with async_session_maker() as db:
        org_id, user_id = await _seed_org_user(db)

    ctx = _ctx(org_id, user_id)
    conn_id = str(uuid.uuid4())
    ctx.add_data_query(conn_id, {"sql": "select 1"})
    ctx.add_data_query(conn_id, {"sql": "select 2"})
    ctx.add_data_bytes(conn_id, 512, {"rows": 4})

    await ctx.flush()
    assert ctx._pending_data_events == []

    async with async_session_maker() as db:
        events = (
            await db.execute(
                select(UsageEvent).where(UsageEvent.organization_id == org_id)
            )
        ).scalars().all()
        by_metric = {}
        for e in events:
            by_metric.setdefault(e.metric, []).append(e)
        assert len(by_metric[METRIC_DATA_QUERIES]) == 2
        assert len(by_metric[METRIC_DATA_BYTES]) == 1
        assert by_metric[METRIC_DATA_BYTES][0].amount == 512
        assert all(e.scope_ref_id == conn_id for e in events)

        counters = (
            await db.execute(
                select(UsageCounter).where(UsageCounter.organization_id == org_id)
            )
        ).scalars().all()
        used = {c.metric: c.used for c in counters}
        assert used[METRIC_DATA_QUERIES] == 2
        assert used[METRIC_DATA_BYTES] == 512


@pytest.mark.asyncio
async def test_check_data_query_enforces_limit(monkeypatch):
    async with async_session_maker() as db:
        org_id, user_id = await _seed_org_user(db)

    class _Limits:
        policy_ids = []

        def query_limit_for_connection(self, connection_id):
            return 2

        def data_bytes_limit_for_connection(self, connection_id):
            return None

    async def _fake_resolve(db, o, u):
        return _Limits()

    used = {"value": 0}

    async def _fake_counter_used(db, **kw):
        return used["value"]

    monkeypatch.setattr(ups.usage_policy_service, "resolve_effective_limits", _fake_resolve)
    monkeypatch.setattr(ups.usage_policy_service, "_get_counter_used", _fake_counter_used)

    ctx = _ctx(org_id, user_id)
    conn_id = str(uuid.uuid4())

    # Under the limit: passes.
    await ctx.check_data_query(conn_id)

    # At the limit in the DB counter: the next query would exceed -> raises.
    used["value"] = 2
    ctx._data_cache.clear()  # force a fresh load of the new counter value
    with pytest.raises(UsageLimitExceeded):
        await ctx.check_data_query(conn_id)

    # Buffered-but-unflushed usage counts toward the projection too.
    used["value"] = 1
    ctx._data_cache.clear()
    ctx.add_data_query(conn_id, None)  # no cache entry yet -> event only
    await ctx.check_data_query(conn_id)  # 1 used + 1 = 2 <= 2 passes (pending not yet cached)
    ctx.add_data_query(conn_id, None)  # cache entry exists now -> pending=1
    with pytest.raises(UsageLimitExceeded):
        await ctx.check_data_query(conn_id)  # 1 used + 1 pending + 1 = 3 > 2


@pytest.mark.asyncio
async def test_derived_context_routes_buffers_to_root():
    async with async_session_maker() as db:
        org_id, user_id = await _seed_org_user(db)

    root = _ctx(org_id, user_id)
    child = root.for_source("create_data", "tool-call-9")

    conn_id = str(uuid.uuid4())
    child.add_data_query(conn_id, None)
    child.add_tokens(123, {"model": "m"})

    # Buffers live on the root; the child's source labels the event.
    assert len(root._pending_data_events) == 1
    assert root._pending_data_events[0]["source"] == "create_data"
    assert child._pending_data_events == []
    assert root.pending_tokens == 123

    await root.flush()
    async with async_session_maker() as db:
        event = (
            await db.execute(
                select(UsageEvent).where(
                    UsageEvent.organization_id == org_id,
                    UsageEvent.metric == METRIC_DATA_QUERIES,
                )
            )
        ).scalars().one()
        assert event.source == "create_data"
        assert event.source_ref_id == "tool-call-9"


@pytest.mark.asyncio
async def test_failed_flush_recredits_buffer(monkeypatch):
    async with async_session_maker() as db:
        org_id, user_id = await _seed_org_user(db)

    ctx = _ctx(org_id, user_id)
    ctx.add_data_query(str(uuid.uuid4()), None)

    async def _boom(context, events):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(ups.usage_policy_service, "record_data_usage_with_context", _boom)
    with pytest.raises(RuntimeError):
        await ctx.flush()
    # Event re-credited; a later flush (with the DB back) persists it.
    assert len(ctx._pending_data_events) == 1
    monkeypatch.undo()
    await ctx.flush()
    assert ctx._pending_data_events == []
