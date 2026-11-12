"""A failed usage flush after a query or entity run or preview is reported, not lost.

Metering is persisted after the run, in a `finally`; if persisting it fails,
the run's own result or error must still reach the caller, so the flush
error is not raised. It is logged at WARNING with its traceback and the run's
usage source, so lost metering is visible to an operator.
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
    assert records[0].exc_info and records[0].exc_info[0] is RuntimeError


@pytest.mark.asyncio
async def test_no_usage_context_logs_nothing(warnings_seen):
    await flush_usage(None)
    assert [r for r in warnings_seen.records if r.levelno >= logging.WARNING] == []
