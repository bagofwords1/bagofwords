"""Unit tests for readiness reporting (app/core/health.py).

These exercise the report-assembly contract with fake dependencies, so they
run without a database or a live scheduler. The HTTP wiring (status codes, the
endpoints being public) is covered in tests/e2e/test_health.py.
"""
import asyncio
import json

import pytest

from app.core import health


class _FakeSession:
    """Async-context-manager stand-in for a real SQLAlchemy session."""

    def __init__(self, raise_on_execute=None, sleep_on_execute=None):
        self._raise = raise_on_execute
        self._sleep = sleep_on_execute

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *args, **kwargs):
        if self._sleep is not None:
            await asyncio.sleep(self._sleep)
        if self._raise is not None:
            raise self._raise
        return object()


def _session_maker(raise_on_execute=None, sleep_on_execute=None):
    """Mimic async_session_maker: calling it yields a fresh session cm."""
    return lambda: _FakeSession(raise_on_execute, sleep_on_execute)


class _FakeScheduler:
    def __init__(self, running: bool):
        self.running = running


def test_uptime_seconds_is_a_nonnegative_float():
    u = health.uptime_seconds()
    assert isinstance(u, float)
    assert u >= 0


def test_worker_identity_names_this_process():
    import os

    ident = health.worker_identity()
    assert ident["hostname"]
    assert ident["pid"] == os.getpid()


@pytest.mark.asyncio
async def test_readiness_ok_when_all_checks_pass():
    report, healthy = await health.build_readiness_report(
        session_maker=_session_maker(),
        scheduler=_FakeScheduler(running=True),
        is_leader=True,
        version="1.2.3",
    )

    assert healthy is True
    assert report["status"] == "ok"
    assert report["version"] == "1.2.3"
    assert isinstance(report["uptime_seconds"], float)
    assert isinstance(report["timestamp"], str) and report["timestamp"]
    assert "hostname" in report["worker"] and "pid" in report["worker"]
    db = report["checks"]["database"]
    assert db["status"] == "ok"
    assert isinstance(db["latency_ms"], (int, float)) and db["latency_ms"] >= 0
    assert report["checks"]["scheduler"]["status"] == "ok"
    assert report["checks"]["scheduler"]["is_leader"] is True


@pytest.mark.asyncio
async def test_database_failure_makes_readiness_unhealthy():
    report, healthy = await health.build_readiness_report(
        session_maker=_session_maker(ConnectionError("boom")),
        scheduler=_FakeScheduler(running=True),
        is_leader=False,
        version="9.9.9",
    )

    assert healthy is False
    assert report["status"] == "down"
    db = report["checks"]["database"]
    assert db["status"] == "down"
    assert db["critical"] is True
    # The caller learns *what kind* of failure it was, not the raw details.
    assert db["error_type"] == "ConnectionError"


@pytest.mark.asyncio
async def test_database_error_does_not_leak_connection_details():
    # A real driver error carries the DSN — host, port, credentials. None of
    # that may reach the (unauthenticated) response body.
    secret = "postgresql://bow:sup3rsecret@db.internal:5432/bow"
    report, _ = await health.build_readiness_report(
        session_maker=_session_maker(RuntimeError(f"could not connect: {secret}")),
        scheduler=_FakeScheduler(running=True),
        is_leader=False,
        version="1.0.0",
    )

    serialized = json.dumps(report)
    assert secret not in serialized
    assert "sup3rsecret" not in serialized


@pytest.mark.asyncio
async def test_hung_database_times_out_instead_of_blocking(monkeypatch):
    # A DB that is up but hung must not make readiness block: the probe is
    # bounded, so it returns a fast "down" (TimeoutError) rather than hanging.
    monkeypatch.setattr(health, "_DB_CHECK_TIMEOUT_SECONDS", 0.05)

    report, healthy = await health.build_readiness_report(
        session_maker=_session_maker(sleep_on_execute=5),
        scheduler=_FakeScheduler(running=True),
        is_leader=False,
        version="1.0.0",
    )

    assert healthy is False
    db = report["checks"]["database"]
    assert db["status"] == "down"
    assert db["error_type"] == "TimeoutError"


@pytest.mark.asyncio
async def test_stopped_scheduler_is_reported_but_not_critical():
    # Scheduler down while the database is fine: the worker can still serve
    # traffic, so readiness stays healthy (200) and must not be pulled from the
    # Service. The failing scheduler is still surfaced in the report.
    report, healthy = await health.build_readiness_report(
        session_maker=_session_maker(),
        scheduler=_FakeScheduler(running=False),
        is_leader=False,
        version="1.0.0",
    )

    assert healthy is True
    assert report["status"] == "degraded"
    sched = report["checks"]["scheduler"]
    assert sched["status"] == "down"
    assert sched["critical"] is False
    assert sched["running"] is False
