"""LLM-facing schedule text: a one-time task is described by its instant,
never by the "@once" placeholder stored in its cron column."""
from datetime import datetime

import pytest

from app.models.scheduled_prompt import describe_schedule


@pytest.mark.parametrize("when", [datetime(2026, 10, 14, 16, 30), datetime(2027, 1, 1, 0, 5, 59)])
def test_one_time_is_described_by_its_instant(when):
    text = describe_schedule("@once", when)
    assert "@once" not in text
    assert when.strftime("%Y-%m-%dT%H:%M") in text
    assert "UTC" in text


@pytest.mark.parametrize("cron", ["0 9 * * 1", "*/15 * * * *", "30 7 1 * *"])
def test_recurring_is_described_by_its_cron(cron):
    assert describe_schedule(cron, None) == cron
