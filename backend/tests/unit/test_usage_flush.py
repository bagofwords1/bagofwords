"""A failed usage flush after a query or entity run or preview is reported, not lost.

Metering is persisted after the run, in a `finally`; if persisting it fails,
the run's own result or error must still reach the caller, so the flush
error is not raised. It is logged at WARNING with the run's usage source and
the error's type and frames, so lost metering is visible to an operator; never
its message, which for a DB error quotes the event's SQL and bound values.
"""

from __future__ import annotations

import logging

import pytest

from app.services.query_service import flush_usage


class _UsageStoreDown:
    """A usage context whose store rejects the write (the DB boundary)."""

    def __init__(self, source: str):
        self.source = source

    async def flush(self) -> None:
        raise RuntimeError("usage store unavailable")


@pytest.fixture
def warnings_seen(caplog, monkeypatch):
    # Migrations disable loggers that already exist; these must be live.
    for name in ("app.services.entity_service", "app.services.query_service"):
        monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.WARNING)
    return caplog


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["query_preview", "entity_run", "entity_viewer_run", "entity_preview", "entity_code_preview"])
async def test_a_failed_usage_flush_is_logged_with_its_source_and_not_raised(warnings_seen, source):
    await flush_usage(_UsageStoreDown(source))

    records = [r for r in warnings_seen.records if r.levelno == logging.WARNING and source in r.getMessage()]
    assert len(records) == 1, [r.getMessage() for r in warnings_seen.records]
    assert "RuntimeError" in records[0].getMessage()


class _UsageInsertRejected:
    """A usage context whose event insert fails at the database, the way
    SQLAlchemy reports it: the error text quotes the statement and its bound
    parameters, and a usage event's metadata carries the rendered SQL."""

    source = "entity_run"

    async def flush(self) -> None:
        from sqlalchemy.exc import IntegrityError

        raise IntegrityError(
            "INSERT INTO usage_events (metric, usage_metadata) VALUES (?, ?)",
            ("data_queries", '{"sql": "SELECT salary FROM staff WHERE email = \'someone@example.com\'"}'),
            Exception("NOT NULL constraint failed: usage_events.connection_id"),
        )


@pytest.mark.asyncio
async def test_a_failed_usage_flush_logs_no_statement_or_parameters(warnings_seen):
    """The warning names the source and the error type; the statement, its
    bound values and the driver message stay out of the log."""
    await flush_usage(_UsageInsertRejected())

    records = [r for r in warnings_seen.records if r.levelno == logging.WARNING]
    assert len(records) == 1, [r.getMessage() for r in warnings_seen.records]
    logged = warnings_seen.text
    for content in ("someone@example.com", "SELECT salary", "INSERT INTO usage_events", "NOT NULL constraint"):
        assert content not in logged, logged
    assert "entity_run" in records[0].getMessage()
    assert "IntegrityError" in records[0].getMessage()


@pytest.mark.asyncio
async def test_no_usage_context_logs_nothing(warnings_seen):
    await flush_usage(None)
    assert [r for r in warnings_seen.records if r.levelno >= logging.WARNING] == []
