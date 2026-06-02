"""Unit tests for app.utils.datetimes.ensure_naive_utc.

Regression coverage for the PostgreSQL/asyncpg bug where timezone-aware
datetimes written to naive TIMESTAMP WITHOUT TIME ZONE columns raise
DataError: can't subtract offset-naive and offset-aware datetimes.
"""
from datetime import datetime, timezone, timedelta

from app.utils.datetimes import ensure_naive_utc


def test_none_passthrough():
    assert ensure_naive_utc(None) is None


def test_naive_passthrough_unchanged():
    dt = datetime(2026, 6, 2, 10, 0, 0)
    result = ensure_naive_utc(dt)
    assert result is dt
    assert result.tzinfo is None


def test_utc_aware_becomes_naive():
    dt = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone.utc)
    result = ensure_naive_utc(dt)
    assert result.tzinfo is None
    assert result == datetime(2026, 6, 2, 10, 0, 0)


def test_offset_aware_converted_to_utc_then_stripped():
    # +02:00 wall clock -> 08:00 UTC, naive
    dt = datetime(2026, 6, 2, 10, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    result = ensure_naive_utc(dt)
    assert result.tzinfo is None
    assert result == datetime(2026, 6, 2, 8, 0, 0)


def test_now_utc_becomes_naive():
    result = ensure_naive_utc(datetime.now(timezone.utc))
    assert result.tzinfo is None
