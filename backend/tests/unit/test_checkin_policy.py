"""Unit tests for agent check-in guardrails that need no database.

Covers the time rules (due-window clamp, working-window shift incl. DST) and
the planner/judge output contracts. DB-backed limits (pending/weekly/daily
caps, access) are exercised end to end in tests/e2e/test_agent_checkins.py.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.ai.agents.checkins.judge import parse_judge_output
from app.ai.agents.checkins.planner import parse_planner_output
from app.services import checkin_policy as policy


def _local(dt_utc: datetime, tz: str) -> datetime:
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz))


def _utc_of_local(y, m, d, hh, mm, tz: str) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).replace(tzinfo=None)


# ── due window clamp ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("hours", [0, 0.5, 1.99])
def test_due_below_floor_is_raised_to_two_hours(hours):
    now = datetime(2026, 10, 6, 10, 0)
    assert policy.clamp_due(now, now + timedelta(hours=hours)) == now + policy.MIN_DUE


@pytest.mark.parametrize("days", [14.01, 30, 365])
def test_due_above_ceiling_is_lowered_to_fourteen_days(days):
    now = datetime(2026, 10, 6, 10, 0)
    assert policy.clamp_due(now, now + timedelta(days=days)) == now + policy.MAX_DUE


@pytest.mark.parametrize("hours", [2, 30, 14 * 24])
def test_due_inside_window_is_unchanged(hours):
    now = datetime(2026, 10, 6, 10, 0)
    assert policy.clamp_due(now, now + timedelta(hours=hours)) == now + timedelta(hours=hours)


# ── working window ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Jerusalem", "Asia/Tokyo"])
def test_friday_1759_local_is_allowed(tz):
    due = _utc_of_local(2026, 10, 9, 17, 59, tz)  # Fri 2026-10-09
    assert policy.shift_into_working_window(due, tz) == due


@pytest.mark.parametrize("tz", ["UTC", "America/New_York", "Asia/Jerusalem", "Asia/Tokyo"])
@pytest.mark.parametrize("seed", [0, 1, 7, 42])
def test_friday_1801_moves_to_monday_morning(tz, seed):
    due = _utc_of_local(2026, 10, 9, 18, 1, tz)
    shifted = _local(policy.shift_into_working_window(due, tz, rng=random.Random(seed)), tz)
    assert shifted.date().isoformat() == "2026-10-12"  # Monday
    minutes = shifted.hour * 60 + shifted.minute
    assert 9 * 60 <= minutes <= 9 * 60 + policy.MAX_JITTER_MINUTES


@pytest.mark.parametrize("hh,mm", [(0, 0), (6, 30), (8, 59)])
def test_early_weekday_moves_to_same_day_nine(hh, mm):
    tz = "Europe/Berlin"
    due = _utc_of_local(2026, 10, 7, hh, mm, tz)  # Wednesday
    shifted = _local(policy.shift_into_working_window(due, tz, rng=random.Random(3)), tz)
    assert shifted.date().isoformat() == "2026-10-07"
    assert 9 <= shifted.hour <= 10


def test_weekend_moves_to_monday():
    tz = "Asia/Jerusalem"
    for day in (10, 11):  # Sat, Sun
        due = _utc_of_local(2026, 10, day, 12, 0, tz)
        shifted = _local(policy.shift_into_working_window(due, tz, rng=random.Random(1)), tz)
        assert shifted.weekday() == 0


def test_dst_boundary_lands_on_local_nine():
    """US DST ends Sun 2026-11-01: Friday is UTC-4, Monday is UTC-5. The window
    start must be 09:00 *local* Monday (14:00 UTC), not 09:00 EDT (13:00 UTC)."""
    tz = "America/New_York"
    due = _utc_of_local(2026, 10, 30, 19, 0, tz)  # Fri evening, EDT
    rng = random.Random(0)
    shifted_utc = policy.shift_into_working_window(due, tz, rng=rng)
    local = _local(shifted_utc, tz)
    assert local.date().isoformat() == "2026-11-02"
    assert local.utcoffset() == timedelta(hours=-5)
    assert datetime(2026, 11, 2, 14, 0) <= shifted_utc <= datetime(2026, 11, 2, 14, 0) + timedelta(minutes=policy.MAX_JITTER_MINUTES)


def test_plan_due_at_is_clamped_then_shifted():
    tz = "UTC"
    now = datetime(2026, 10, 9, 17, 0)  # Fri 17:00
    due = policy.plan_due_at(now, 0.1, tz, rng=random.Random(0))  # clamps to 19:00 → Monday
    assert due.weekday() == 0 and due >= now + policy.MIN_DUE


# ── settings readers ────────────────────────────────────────────────────────

class _Settings:
    def __init__(self, **cfg):
        self.config = cfg

    def get_config(self, key):
        from app.schemas.organization_settings_schema import FeatureConfig
        if key in self.config:
            return FeatureConfig(name=key, description="", value=self.config[key])
        return None


def test_feature_on_by_default():
    from app.schemas.organization_settings_schema import OrganizationSettingsConfig
    assert OrganizationSettingsConfig().enable_agent_checkins.value is True
    assert policy.feature_enabled(None) is True
    assert policy.feature_enabled(_Settings()) is True
    assert policy.feature_enabled(_Settings(enable_agent_checkins=False)) is False


@pytest.mark.parametrize("weekly,daily", [(1, 5), (3, 50), (10, 1)])
def test_caps_read_from_settings(weekly, daily):
    s = _Settings(checkins_max_per_user_per_week=weekly, checkins_max_runs_per_org_per_day=daily)
    assert policy.weekly_cap(s) == weekly
    assert policy.org_daily_cap(s) == daily


# ── planner output contract ─────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "not json at all",
    "{\"propose\": true, \"due_in_hours\": 24}",                     # missing note
    "{\"propose\": true, \"note\": \"   \", \"due_in_hours\": 24}",  # blank note
    "{\"propose\": true, \"note\": \"check X\"}",                     # missing due
    "{\"propose\": \"maybe\"}",
    "[1, 2, 3]",
    "",
])
def test_invalid_planner_output_means_no_checkin(raw):
    assert parse_planner_output(raw) is None


def test_planner_output_accepts_fenced_json():
    raw = "```json\n{\"propose\": true, \"due_in_hours\": 72, \"note\": \"Re-run churn\", \"reason\": \"waits on refresh\"}\n```"
    out = parse_planner_output(raw)
    assert out is not None and out.propose and out.due_in_hours == 72 and out.note == "Re-run churn"


def test_planner_decline_is_valid():
    out = parse_planner_output('{"propose": false, "reason": "one-off lookup"}')
    assert out is not None and out.propose is False


# ── judge output contract ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    '{"decision": "run"}',                      # no reason
    '{"decision": "run", "reason": "   "}',     # blank reason
    '{"decision": "maybe", "reason": "x"}',
    'garbage',
    '',
])
def test_judge_without_valid_reason_counts_as_skip(raw):
    out = parse_judge_output(raw)
    assert out.decision == "skip"
    assert out.invalid is True
    assert out.reason  # the stored reason is never empty


@pytest.mark.parametrize("decision", ["run", "skip", "RUN", " Skip "])
def test_judge_valid_decisions(decision):
    out = parse_judge_output('{"decision": "%s", "reason": "refresh landed Oct 1", "focus": "churn by plan"}' % decision)
    assert out.decision == decision.strip().lower()
    assert out.invalid is False
    assert out.reason == "refresh landed Oct 1"


def test_judge_cannot_self_report_valid():
    out = parse_judge_output('{"decision": "run", "reason": "x", "invalid": true}')
    assert out.invalid is False
