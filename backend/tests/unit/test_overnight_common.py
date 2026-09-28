"""Unit tests for the overnight-learning pure helpers: settings parsing, the
night window, promotion gates, recurring-ask detection, output parsing and
cron derivation. No DB, no model."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.ai.agents.dreams import agent_prompts as AP
from app.ai.agents.dreams import user_prompts as UP
from app.services.dreams import common as C
from app.services.dreams.agent_dream import Draft, group_gate
from app.services.dreams.briefing import cron_for
from app.services.dreams.user_dream import _parse_local, _valid_cadence, detect_recurring, normalize_intent


class _Cfg:
    def __init__(self, value):
        self.value = value


class _Settings:
    def __init__(self, **cfg):
        self.cfg = cfg
        self.config = {"timezone": cfg.pop("timezone")} if "timezone" in cfg else {}

    def get_config(self, key):
        return _Cfg(self.cfg[key]) if key in self.cfg else None


# ── settings ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (30, 30), (0, 0), (7, 7), ("14", 14), (-5, 0), (10_000_000, C.MAX_EXPIRY_DAYS), ("x", C.DEFAULT_EXPIRY_DAYS),
    (None, C.DEFAULT_EXPIRY_DAYS),
])
def test_expiry_days_parses_and_clamps(raw, expected):
    s = _Settings() if raw is None else _Settings(ai_suggestion_expiry_days=raw)
    assert C.expiry_days(s) == expected


def test_master_switches_default_off_and_user_dream_needs_memory():
    assert C.agent_dreaming_enabled(_Settings()) is False
    assert C.user_dreaming_enabled(_Settings()) is False
    assert C.agent_dreaming_enabled(_Settings(enable_agent_dreaming=True)) is True
    assert C.user_dreaming_enabled(_Settings(enable_user_dreaming=True)) is True  # memory defaults on
    assert C.user_dreaming_enabled(_Settings(enable_user_dreaming=True, enable_user_memory=False)) is False


def test_org_timezone_falls_back_to_utc_on_invalid():
    assert C.org_timezone(_Settings(timezone="Asia/Jerusalem")) == "Asia/Jerusalem"
    assert C.org_timezone(_Settings(timezone="Not/AZone")) == "UTC"
    assert C.org_timezone(None) == "UTC"


# ── night window ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("tz,utc_hour,user_in,agent_in", [
    ("UTC", 0, False, False),
    ("UTC", 1, True, False),
    ("UTC", 2, True, False),
    ("UTC", 3, False, True),
    ("UTC", 4, False, True),
    ("UTC", 5, False, False),
    # Jerusalem is UTC+3 in early October: 23:00 UTC = 02:00 local (user window).
    ("Asia/Jerusalem", 23, True, False),
    # New York is UTC-4 in October: 07:00 UTC = 03:00 local (agent window).
    ("America/New_York", 7, False, True),
])
def test_windows_are_org_local(tz, utc_hour, user_in, agent_in):
    now = datetime(2026, 10, 6, utc_hour, 30)
    assert C.in_window(now, tz, C.USER_WINDOW) is user_in
    assert C.in_window(now, tz, C.AGENT_WINDOW) is agent_in


def test_local_date_follows_the_org_timezone():
    now = datetime(2026, 10, 6, 23, 30)  # UTC
    assert C.local_date(now, "UTC") == "2026-10-06"
    assert C.local_date(now, "Asia/Jerusalem") == "2026-10-07"


# ── promotion gates ────────────────────────────────────────────────────────

def _draft(key, user, day):
    return Draft(key=key, build_id="b", build_number=1, instruction_id=f"i{key}", version_id="v", text="t",
                 title="t", user_id=user, day=day, created_at=datetime(2026, 10, 1))


@pytest.mark.parametrize("users,days,promotable", [
    (["u1", "u2", "u3"], ["2026-10-01", "2026-10-02", "2026-10-02"], True),
    (["u1", "u2", "u3", "u4"], ["2026-10-01", "2026-10-03", "2026-10-03", "2026-10-03"], True),
    (["u1", "u2", "u2"], ["2026-10-01", "2026-10-02", "2026-10-03"], False),   # 2 users
    (["u1", "u2", "u3"], ["2026-10-02", "2026-10-02", "2026-10-02"], False),   # 1 day
    ([None, None, None], ["2026-10-01", "2026-10-02", "2026-10-03"], False),   # unknown authors
])
def test_group_gate_needs_enough_distinct_users_and_days(users, days, promotable):
    drafts = [_draft(f"d{i}", u, d) for i, (u, d) in enumerate(zip(users, days))]
    assert group_gate(drafts)["promotable"] is promotable


# ── recurring asks ─────────────────────────────────────────────────────────

def _asks(text, dates, report="r"):
    return [{"text": text, "created_at": d, "report_id": report} for d in dates]


def test_recurring_needs_distinct_weeks_not_just_repeats():
    monday = datetime(2026, 9, 7, 7, 0)
    same_week = [monday + timedelta(hours=h) for h in (0, 1, 2, 3)]
    assert detect_recurring(_asks("Weekly pipeline by region", same_week), "UTC") == []

    weekly = [monday + timedelta(weeks=w) for w in range(3)]
    out = detect_recurring(_asks("Weekly pipeline by region", weekly), "UTC")
    assert len(out) == 1
    assert out[0]["weeks"] == 3 and out[0]["weekday"] == "mon" and out[0]["hour"] == 7


def test_recurring_groups_rephrasings_but_not_different_asks():
    monday = datetime(2026, 9, 7, 8, 0)
    asks = (
        _asks("weekly pipeline by region", [monday])
        + _asks("pipeline by region this week please", [monday + timedelta(weeks=1)])
        + _asks("show me the weekly pipeline by region", [monday + timedelta(weeks=2)])
        + _asks("churn for enterprise customers", [monday + timedelta(weeks=w) for w in (0, 1)])
    )
    out = detect_recurring(asks, "UTC")
    assert [o["count"] for o in out] == [3]
    assert normalize_intent("Revenue 2026 by region") == normalize_intent("revenue by region 2025")


# ── parsing ───────────────────────────────────────────────────────────────

def test_agent_proposal_parses_and_drops_malformed_entries():
    raw = (
        '```json\n{"groups": [{"draft_ids": ["d1","d2"], "title": "Net revenue", "text": "Revenue means net.",'
        ' "load_mode": "weird"}, {"draft_ids": [], "text": "x"}, {"draft_ids": ["d3"], "text": ""}],'
        ' "feedback_edits": [{"live_id": "L2", "text": "Better."}, {"live_id": "L3"}],'
        ' "archive": [{"live_id": "L7", "why": "obsolete"}, {"why": "no id"}], "summary": "s"}\n```'
    )
    p = AP.parse(raw)
    assert [g.draft_ids for g in p.groups] == [["d1", "d2"]]
    assert p.groups[0].load_mode == "intelligent"
    assert [e.live_id for e in p.feedback_edits] == ["L2"]
    assert [a.live_id for a in p.archive] == ["L7"]
    assert AP.parse("not json") is None


def test_user_proposal_parses_ops_and_ignores_unknown():
    raw = (
        '{"memory": [{"op": "create", "text": "Board meeting", "event_start": "2026-10-09", "tags": ["board"]},'
        ' {"op": "delete", "handle": "m1"}, {"op": "forget", "handle": "m2"}],'
        ' "open_threads": [{"report": "r1", "text": "Churn by plan"}, {"report": "r2"}],'
        ' "follow_ups": [{"report": "r1", "due_local": "2026-10-08T07:45", "note": "Refresh"}],'
        ' "habit": {"recurring": "h1", "intent": "Pipeline", "cadence": "weekly:mon", "time": "08:30"}}'
    )
    p = UP.parse(raw)
    assert [m.op for m in p.memory] == ["create", "forget"]
    assert [t.report for t in p.open_threads] == ["r1"]
    assert p.follow_ups[0].due_local == "2026-10-08T07:45"
    assert p.habit.cadence == "weekly:mon"
    assert UP.parse("[]") is None


# ── small helpers ─────────────────────────────────────────────────────────

def test_parse_local_converts_org_time_to_utc():
    # 07:45 in Jerusalem (UTC+3 in October) = 04:45 UTC.
    assert _parse_local("2026-10-08T07:45", "Asia/Jerusalem") == datetime(2026, 10, 8, 4, 45)
    assert _parse_local("2026-10-08", "UTC") == datetime(2026, 10, 8, 9, 0)
    assert _parse_local("next thursday", "UTC") is None


@pytest.mark.parametrize("cadence,ok", [
    ("daily", True), ("weekly:mon", True), ("weekly:sun", True), ("weekly:funday", False), ("hourly", False),
])
def test_valid_cadence(cadence, ok):
    assert _valid_cadence(cadence) is ok


@pytest.mark.parametrize("cadence,time,cron", [
    ("weekly:mon", "08:30", "30 8 * * 1"),
    ("weekly:sun", "07:05", "5 7 * * 0"),
    ("daily", "09:00", "0 9 * * *"),
])
def test_cron_for_offer(cadence, time, cron):
    assert cron_for(cadence, time) == cron
