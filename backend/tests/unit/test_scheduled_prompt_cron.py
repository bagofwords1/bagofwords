"""Regression tests for cron day-of-week handling.

Standard cron numbers weekdays 0=Sun..6=Sat, but APScheduler's numeric
day_of_week is 0=Mon..6=Sun. Feeding the raw number to APScheduler shifted
every weekday by one (a Sunday "0" task fired on Monday). We translate the
day-of-week to APScheduler's weekday NAMES at registration time; the stored
cron string stays in standard convention. These tests pin both the translation
and the resulting real fire day.
"""
from datetime import datetime

import pytest
from apscheduler.triggers.cron import CronTrigger

from app.core.scheduler import cron_dow_to_apscheduler
from app.services.scheduled_prompt_service import _parse_cron_expression

# 2026-06-06 is a Saturday — a fixed reference for "next fire" assertions.
SATURDAY_MIDNIGHT = datetime(2026, 6, 6, 0, 0, 0)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", "sun"),
        ("1", "mon"),
        ("6", "sat"),
        ("7", "sun"),
        ("*", "*"),
        ("1-5", "mon-fri"),
        ("0,6", "sun,sat"),
        ("mon", "mon"),
        ("MON", "mon"),
        ("*/2", "*/2"),  # step left untouched
        ("", ""),
    ],
)
def test_cron_dow_to_apscheduler(raw, expected):
    assert cron_dow_to_apscheduler(raw) == expected


def _next_weekday(cron: str) -> str:
    params = _parse_cron_expression(cron)
    assert params is not None
    trigger = CronTrigger(**params)
    nxt = trigger.get_next_fire_time(None, SATURDAY_MIDNIGHT)
    return nxt.strftime("%A")


@pytest.mark.parametrize(
    "cron,weekday",
    [
        ("0 9 * * 0", "Sunday"),    # standard-cron Sunday must fire Sunday
        ("0 9 * * 1", "Monday"),
        ("0 9 * * 6", "Saturday"),
        ("0 9 * * 1-5", "Monday"),  # weekdays: next after Saturday is Monday
        ("0 9 * * 0,6", "Saturday"),  # next after Saturday-midnight is Saturday 09:00
    ],
)
def test_parsed_cron_fires_on_expected_day(cron, weekday):
    assert _next_weekday(cron) == weekday
