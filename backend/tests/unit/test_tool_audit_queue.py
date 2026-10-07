"""Tool-audit queue durability (docs/design/audit-log-streams.md, WP0 / Loop A0).

Contract: every ``log_tool_audit`` call that has an organization ends up as
exactly one ``audit_logs`` row, even when

- a burst far exceeds the queue's capacity while the database is slow,
- the database fails transiently and then recovers,
- the database is unreachable through shutdown (events are spilled to disk
  and replayed into the table on the next start).

Only the database boundary is stubbed (latency / failure injection around the
real session maker); the queue, worker and audit service run for real.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import app.dependencies as deps
import app.ee.audit.tool_audit as ta
from app.ee.audit.models import AuditLog
from app.models.organization import Organization

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py



_real_session_maker = deps.async_session_maker


class _SlowFlakySession:
    """Wraps a real AsyncSession; commit can be delayed and/or made to fail."""

    def __init__(self, inner, ctl):
        self._inner = inner
        self._ctl = ctl

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def commit(self):
        if self._ctl.latency:
            await asyncio.sleep(self._ctl.latency)
        if self._ctl.fail_remaining != 0:
            if self._ctl.fail_remaining > 0:
                self._ctl.fail_remaining -= 1
            await self._inner.rollback()
            raise ConnectionError("injected database outage")
        self._ctl.commits += 1
        await self._inner.commit()

    async def __aenter__(self):
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._inner.__aexit__(*exc)


@pytest.fixture
def db_ctl(monkeypatch, tmp_path):
    ctl = SimpleNamespace(latency=0.0, fail_remaining=0, commits=0)

    def maker():
        return _SlowFlakySession(_real_session_maker(), ctl)

    monkeypatch.setattr(deps, "async_session_maker", maker)
    monkeypatch.setenv("BOW_AUDIT_SPILL_DIR", str(tmp_path / "spill"))
    ta._reset_stats_for_tests()
    yield ctl
    ta._reset_stats_for_tests()


async def _org_id() -> str:
    async with _real_session_maker() as s:
        org = Organization(name=f"audit-q-{uuid.uuid4().hex[:8]}")
        s.add(org)
        await s.commit()
        return str(org.id)


def _ctx(org_id: str):
    return {"organization": SimpleNamespace(id=org_id), "user": None, "agent_execution_id": "run-1"}


async def _count(org_id: str) -> int:
    async with _real_session_maker() as s:
        return (await s.execute(
            select(func.count(AuditLog.id)).where(AuditLog.organization_id == org_id)
        )).scalar()


async def _burst(org_id: str, n: int) -> None:
    await asyncio.gather(*[
        ta.log_tool_audit(_ctx(org_id), "tool.data_queried", "data_source", None, {"i": i})
        for i in range(n)
    ])


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_size,multiplier,latency", [(20, 10, 0.01), (50, 6, 0.005)])
async def test_burst_beyond_queue_capacity_with_slow_db_loses_nothing(
    db_ctl, monkeypatch, queue_size, multiplier, latency
):
    monkeypatch.setattr(ta, "_QUEUE_MAXSIZE", queue_size)
    db_ctl.latency = latency
    org_id = await _org_id()
    await ta.start_tool_audit_worker()
    try:
        n = queue_size * multiplier
        await _burst(org_id, n)
        await ta.drain_tool_audit_queue(timeout=60)
    finally:
        await ta.stop_tool_audit_worker(timeout=60)

    stats = ta.get_tool_audit_queue_stats()
    assert stats["dropped"] == 0
    assert await _count(org_id) == n


@pytest.mark.asyncio
async def test_transient_db_failure_is_retried_until_written(db_ctl):
    db_ctl.fail_remaining = 2
    org_id = await _org_id()
    await ta.start_tool_audit_worker()
    try:
        await _burst(org_id, 7)
        await ta.drain_tool_audit_queue(timeout=30)
    finally:
        await ta.stop_tool_audit_worker(timeout=30)

    assert await _count(org_id) == 7
    assert ta.get_tool_audit_queue_stats()["dropped"] == 0


@pytest.mark.asyncio
async def test_db_outage_through_shutdown_spills_then_replays_exactly_once(db_ctl):
    db_ctl.fail_remaining = -1  # down for good
    org_id = await _org_id()
    await ta.start_tool_audit_worker()
    await _burst(org_id, 9)
    await ta.stop_tool_audit_worker(timeout=2)

    assert await _count(org_id) == 0
    assert ta.get_tool_audit_queue_stats()["spilled"] == 9

    db_ctl.fail_remaining = 0  # database is back on the next start
    replayed = await ta.replay_spilled_tool_audit_events()
    assert replayed == 9
    assert await _count(org_id) == 9
    # A second replay finds nothing left: no double-writes.
    assert await ta.replay_spilled_tool_audit_events() == 0
    assert await _count(org_id) == 9


@pytest.mark.asyncio
async def test_events_without_an_organization_are_skipped_not_counted_as_lost(db_ctl):
    await ta.start_tool_audit_worker()
    try:
        await ta.log_tool_audit({"organization": None}, "tool.data_queried")
        await ta.drain_tool_audit_queue(timeout=10)
    finally:
        await ta.stop_tool_audit_worker(timeout=10)
    stats = ta.get_tool_audit_queue_stats()
    assert stats["enqueued"] == 0 and stats["dropped"] == 0


async def _wait_for(pred, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await pred():
            return True
        await asyncio.sleep(0.05)
    return await pred()


@pytest.mark.asyncio
async def test_spill_is_replayed_without_a_restart_once_the_db_recovers(db_ctl, monkeypatch):
    """An outage spills events to disk; when the database comes back while
    the service keeps running, they reach the table — no restart needed."""
    monkeypatch.setattr(ta, "_RETRY_DELAYS_SECONDS", (0.01,))
    monkeypatch.setattr(ta, "_REPLAY_MIN_INTERVAL_SECONDS", 0.0)
    org_id = await _org_id()
    await ta.start_tool_audit_worker()
    try:
        db_ctl.fail_remaining = -1
        await _burst(org_id, 3)
        assert await _wait_for(lambda: _stat_at_least("spilled", 3))
        assert await _count(org_id) == 0

        db_ctl.fail_remaining = 0  # recovers; the process keeps running
        await ta.log_tool_audit(_ctx(org_id), "tool.data_queried", "data_source", None, {"after": True})
        assert await _wait_for(lambda: _count_is(org_id, 4))
    finally:
        await ta.stop_tool_audit_worker(timeout=10)
    assert ta.get_tool_audit_queue_stats()["replayed"] == 3


@pytest.mark.asyncio
async def test_scheduled_replay_recovers_spill_with_no_new_traffic(db_ctl, monkeypatch):
    org_id = await _org_id()
    db_ctl.fail_remaining = -1
    await ta.start_tool_audit_worker()
    await _burst(org_id, 2)
    await ta.stop_tool_audit_worker(timeout=2)
    assert await _count(org_id) == 0

    db_ctl.fail_remaining = 0
    await ta.scheduled_spill_replay()  # the periodic job; no restart, no new events
    assert await _count(org_id) == 2
    await ta.scheduled_spill_replay()  # nothing left: no double writes
    assert await _count(org_id) == 2


async def _stat_at_least(key, n):
    return ta.get_tool_audit_queue_stats()[key] >= n


async def _count_is(org_id, n):
    return await _count(org_id) == n
