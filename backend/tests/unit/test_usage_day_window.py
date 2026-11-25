"""The daily usage window: midnight to midnight in the organization's timezone.

Pure date arithmetic — no database. The invariants are the ones every daily
cap relies on: the window always contains "now", it starts at local midnight,
it is as long as the local day (23/25 hours across DST), and consecutive days
tile with no gap or overlap.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.usage_policy_service import (
    METRIC_LLM_TOKENS,
    WINDOW_DAILY,
    WINDOW_MONTHLY,
    build_limit_exceeded,
    current_day_window,
    current_month_window,
    daily_metric,
)

ZONES = ["UTC", "Asia/Jerusalem", "America/Los_Angeles", "Pacific/Kiritimati", "Asia/Kolkata"]
INSTANTS = [
    datetime(2026, 11, 23, 0, 0),
    datetime(2026, 11, 23, 21, 59),
    datetime(2026, 12, 31, 23, 30),
    datetime(2026, 3, 8, 9, 30),    # US DST starts (LA)
    datetime(2026, 10, 25, 1, 30),  # EU/Israel DST ends
]


@pytest.mark.parametrize("tz_name", ZONES)
@pytest.mark.parametrize("now", INSTANTS)
def test_window_contains_now_and_starts_at_local_midnight(tz_name, now):
    tz = ZoneInfo(tz_name)
    start, end = current_day_window(tz_name, now)

    assert start <= now < end
    local_start = start.replace(tzinfo=timezone.utc).astimezone(tz)
    local_end = end.replace(tzinfo=timezone.utc).astimezone(tz)
    assert (local_start.hour, local_start.minute) == (0, 0)
    assert (local_end.hour, local_end.minute) == (0, 0)
    assert local_end.date() - local_start.date() == timedelta(days=1)
    # The next day's window starts exactly where this one ends.
    assert current_day_window(tz_name, end)[0] == end


def test_window_length_follows_the_local_day_across_dst():
    # 2026-03-08 is 23 hours long in Los Angeles; 2026-11-01 is 25.
    short_start, short_end = current_day_window("America/Los_Angeles", datetime(2026, 3, 8, 20, 0))
    long_start, long_end = current_day_window("America/Los_Angeles", datetime(2026, 11, 1, 20, 0))
    assert short_end - short_start == timedelta(hours=23)
    assert long_end - long_start == timedelta(hours=25)


def test_unknown_or_missing_timezone_falls_back_to_utc():
    now = datetime(2026, 11, 23, 15, 0)
    expected = (datetime(2026, 11, 23), datetime(2026, 11, 24))
    assert current_day_window(None, now) == expected
    assert current_day_window("Not/AZone", now) == expected


def test_daily_counters_never_share_a_key_with_monthly_ones():
    # In a UTC org the day window of the 1st starts at the month window's
    # instant; only the metric name keeps the two counters apart.
    first = datetime(2026, 12, 1, 8, 0)
    assert current_day_window("UTC", first)[0] == current_month_window(first)[0]
    assert daily_metric(METRIC_LLM_TOKENS) != METRIC_LLM_TOKENS


def test_daily_error_names_the_window_and_when_it_resets():
    now = datetime(2026, 11, 23, 16, 48)  # 18:48 in Jerusalem
    exc = build_limit_exceeded(
        METRIC_LLM_TOKENS, WINDOW_DAILY, limit=100, used=100, requested=5,
        tz_name="Asia/Jerusalem", now=now,
    )
    assert exc.window == WINDOW_DAILY
    assert exc.resets_at == current_day_window("Asia/Jerusalem", now)[1]
    assert exc.detail.startswith("Daily LLM token quota exceeded.")
    assert "00:00 Asia/Jerusalem" in exc.detail and "(in 5h 12m)" in exc.detail

    monthly = build_limit_exceeded(
        METRIC_LLM_TOKENS, WINDOW_MONTHLY, limit=100, used=100, requested=5, now=now,
    )
    assert monthly.detail == "Monthly LLM token quota exceeded."
    assert monthly.resets_at == current_month_window(now)[1]
