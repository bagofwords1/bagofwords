"""Unit tests for the overnight-learning pure helpers: settings parsing, the
night window, promotion gates and output parsing. No DB, no model."""
from __future__ import annotations

from datetime import datetime

import pytest

from app.ai.agents.dreams import agent_prompts as AP
from app.ai.agents.dreams import user_prompts as UP
from app.services.dreams import common as C
from app.services.dreams.agent_dream import Draft, group_gate
from app.services.dreams.user_dream import _parse_local


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


def test_master_switches_default_on_and_user_dream_needs_memory():
    assert C.agent_dreaming_enabled(_Settings()) is True
    assert C.user_dreaming_enabled(_Settings()) is True
    assert C.agent_dreaming_enabled(_Settings(enable_agent_dreaming=False)) is False
    assert C.user_dreaming_enabled(_Settings(enable_user_dreaming=False)) is False
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


def test_user_proposal_parses_memory_and_follow_ups_only():
    raw = (
        '{"memory": [{"op": "create", "text": "Board meeting", "event_start": "2026-10-09", "tags": ["board"]},'
        ' {"op": "delete", "handle": "m1"}, {"op": "forget", "handle": "m2"}],'
        ' "follow_ups": [{"report": "r1", "due_local": "2026-10-08T07:45", "note": "Refresh"},'
        ' {"report": "r2", "due_local": "2026-10-08T07:45", "note": ""}],'
        ' "open_threads": [{"report": "r1", "text": "ignored"}],'
        ' "habit": {"recurring": "h1", "intent": "ignored", "cadence": "daily", "time": "08:30"}}'
    )
    p = UP.parse(raw)
    assert [m.op for m in p.memory] == ["create", "forget"]
    assert [(f.report, f.due_local) for f in p.follow_ups] == [("r1", "2026-10-08T07:45")]
    assert set(UP.proposal_to_json(p)) == {"memory", "follow_ups", "summary"}
    assert UP.parse("[]") is None


# ── small helpers ─────────────────────────────────────────────────────────

def test_parse_local_converts_org_time_to_utc():
    # 07:45 in Jerusalem (UTC+3 in October) = 04:45 UTC.
    assert _parse_local("2026-10-08T07:45", "Asia/Jerusalem") == datetime(2026, 10, 8, 4, 45)
    assert _parse_local("2026-10-08", "UTC") == datetime(2026, 10, 8, 9, 0)
    assert _parse_local("next thursday", "UTC") is None
