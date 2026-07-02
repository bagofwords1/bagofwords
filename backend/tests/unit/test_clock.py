"""Unit tests for the planner clock module (weekday + work-week convention).

The per-turn ``<time>`` block must spell out the weekday of "today" and the
week's date range, because LLMs are unreliable at deriving either from a bare
``YYYY-MM-DD`` — the root cause of the "wrong day of week / wrong week" bugs,
especially for Sunday-start (Hebrew/Arabic) work weeks.
"""
from datetime import date

from app.ai.agents.planner.clock import (
    resolve_first_weekday,
    time_block,
    current_time_str,
    _week_range,
)


def test_week_range_sunday_start():
    # 2026-07-02 is a Thursday. A Sunday-start week runs Sun 06-28 .. Sat 07-04.
    start, end = _week_range(date(2026, 7, 2), 6)
    assert start == date(2026, 6, 28)
    assert end == date(2026, 7, 4)


def test_week_range_monday_start():
    start, end = _week_range(date(2026, 7, 2), 0)
    assert start == date(2026, 6, 29)
    assert end == date(2026, 7, 5)


def test_week_range_saturday_start():
    start, end = _week_range(date(2026, 7, 2), 5)
    assert start == date(2026, 6, 27)
    assert end == date(2026, 7, 3)


def test_resolve_first_weekday_auto_from_locale():
    # Hebrew/Arabic -> Sunday (6); everything else -> Monday/ISO (0).
    assert resolve_first_weekday(None, "he") == 6
    assert resolve_first_weekday(None, "he-IL") == 6
    assert resolve_first_weekday("auto", "ar") == 6
    assert resolve_first_weekday(None, "en") == 0
    assert resolve_first_weekday(None, None) == 0


def test_resolve_first_weekday_explicit_overrides_locale():
    # An explicit org setting wins over the locale-derived default.
    assert resolve_first_weekday("sunday", "en") == 6
    assert resolve_first_weekday("monday", "he") == 0
    assert resolve_first_weekday("saturday", "en") == 5


def test_resolve_first_weekday_unknown_value_falls_back():
    assert resolve_first_weekday("garbage", "en") == 0
    assert resolve_first_weekday("garbage", "he") == 6


def test_time_block_includes_weekday_and_convention():
    block = time_block("UTC", "auto", "he")
    assert block.startswith("<time>")
    assert block.endswith("</time>")
    # Weekday name and the org's week-start convention are both present.
    assert "week starts on Sunday" in block
    assert "(Sun)" in block and "(Sat)" in block
    # One of the seven weekday names labels "now".
    assert any(d in block for d in
               ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])


def test_current_time_str_includes_weekday():
    s = current_time_str("UTC")
    assert "timezone: UTC" in s
    assert "week starts on Monday" in s  # en/default


def test_invalid_timezone_falls_back_without_error():
    # Bad tz must not raise; it falls back to server-local and still renders.
    block = time_block("Not/AZone", "monday", "en")
    assert "<time>" in block
    assert "week starts on Monday" in block
