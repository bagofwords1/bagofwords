"""MemoryContextBuilder: tiers, budgets, matching and the index line.

Contract: however many facts a user has, the rendered <memory> body stays
within budget (~2,200 chars); the always tier carries dated facts in the
[-2d, +21d] window and then the strongest undated facts; the matched tier
surfaces facts whose text/aliases/tags overlap the turn or that carry an
object tag present in the turn; the index line accounts for everything left
out; the header says memory holds no rules.
"""
import random
import re
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.ai.context.builders.memory_context_builder import (
    ALWAYS_BUDGET,
    MATCHED_MAX_ENTRIES,
    build_memory_context,
)
from app.ai.context.keyword_match import extract_keywords

NOW = datetime(2026, 10, 6, 9, 0)
BLOCK_BUDGET = 2300  # ~2,200 target + header slack


def _entry(seq, text, *, tags=(), aliases=None, source="agent", seen=1, date=None, end=None,
           last_seen=None, expires_at=None):
    return SimpleNamespace(
        id=str(uuid.uuid4()), seq=seq, handle=f"m{seq}", text=text, tags=list(tags),
        aliases=aliases, source=source, seen_count=seen, event_start=date, event_end=end,
        expires_at=expires_at, last_seen_at=last_seen or NOW - timedelta(days=1), status="active",
    )


def _population(n, seed=7):
    rnd = random.Random(seed)
    out = []
    for i in range(1, n + 1):
        date = NOW + timedelta(days=rnd.randint(-40, 60)) if i % 5 == 0 else None
        out.append(_entry(
            i, f"Fact number {i} about topic{i % 17} with some descriptive words to fill the line",
            tags=[f"topic{i % 17}", f"area{i % 5}"], aliases=[f"alias{i}"] if i % 7 == 0 else None,
            seen=rnd.randint(1, 9), date=date,
        ))
    return out


@pytest.mark.parametrize("n", [0, 10, 200])
def test_budget_holds_for_any_population(n):
    kw = extract_keywords("show topic3 topic5 area2 alias42 region churn " * 3, unicode=True)
    ctx = build_memory_context(_population(n), now=NOW, keywords=kw, object_tags=[], user_name="Dana")
    assert ctx.chars <= BLOCK_BUDGET
    if n == 0:
        assert ctx.body == "" and ctx.injected == []
    else:
        assert ctx.injected, "some facts must be injected"
        assert len([i for i in ctx.injected if i.tier == "matched"]) <= MATCHED_MAX_ENTRIES


def test_header_says_memory_holds_facts_not_rules():
    ctx = build_memory_context([_entry(1, "Leads the Q3 churn project")], now=NOW, user_name="Dana")
    head = "\n".join(ctx.body.splitlines()[:2])
    assert "Facts about Dana" in head
    assert "No rules here" in head and "<instructions>" in head


def test_always_tier_takes_dated_facts_in_window_first_then_undated():
    soon = _entry(1, "Board meeting", date=NOW + timedelta(days=3))
    far = _entry(2, "Offsite", date=NOW + timedelta(days=40))
    just_ended = _entry(3, "QBR", date=datetime(2026, 10, 5))  # yesterday, all-day
    role = _entry(4, "Owns EMEA revenue reporting")
    project = _entry(5, "Leads the Q3 churn project")
    ctx = build_memory_context([role, far, project, soon, just_ended], now=NOW)
    always = [i.handle for i in ctx.injected if i.tier == "always"]
    assert always[:2] == ["m3", "m1"]                 # dated, by date
    assert set(always[2:]) == {"m4", "m5"}            # then undated
    assert "m2" not in always                          # outside the 21-day window
    assert "1 more" in ctx.index_line


def test_dated_fact_renders_absolute_date_and_relative_hint():
    ctx = build_memory_context([_entry(4, "Board meeting", date=datetime(2026, 10, 9))], now=NOW)
    assert "[m4] Board meeting — Fri 2026-10-09 (in 3 days)" in ctx.body


def test_alias_match_puts_shorthand_in_matched_tier():
    many = [_entry(i, f"Undated filler fact {i} " + "x" * 90, seen=9) for i in range(10, 30)]
    shorthand = _entry(7, "\"my region\" = EMEA", aliases=["my region"], tags=["emea"])
    other = _entry(8, "\"the board deck\" = Q3 Board Pack", tags=["board-deck"])
    kw = extract_keywords("revenue in my region last quarter", unicode=True)
    ctx = build_memory_context(many + [shorthand, other], now=NOW, keywords=kw)
    matched = {i.handle: i for i in ctx.injected if i.tier == "matched"}
    assert "m7" in matched and "m8" not in matched
    assert "region" in (matched["m7"].reason or "")
    assert "← matched" in ctx.body


def test_object_tag_matches_when_agent_in_turn_and_outranks_keywords():
    fill = [_entry(i, f"Undated filler fact {i} " + "x" * 90, seen=9) for i in range(10, 30)]
    agent_id = "ds-" + uuid.uuid4().hex[:6]
    tied = _entry(1, "Reviews this agent's numbers every Monday", tags=[f"agent:{agent_id}"])
    keyword = _entry(2, "Investigating churn in revenue", tags=["churn"])
    unrelated = _entry(3, "Planning the offsite", tags=["offsite"])
    kw = extract_keywords("churn revenue", unicode=True)
    ctx = build_memory_context(fill + [keyword, tied, unrelated], now=NOW, keywords=kw,
                               object_tags=[f"agent:{agent_id}"])
    matched = [i.handle for i in ctx.injected if i.tier == "matched"]
    assert matched[0] == "m1" and "m2" in matched and "m3" not in matched
    ctx2 = build_memory_context(fill + [keyword, tied], now=NOW, keywords=set(), object_tags=["agent:other"])
    assert "m1" not in {i.handle for i in ctx2.injected if i.tier == "matched"}


def test_index_line_counts_what_was_left_out():
    ctx = build_memory_context(_population(200), now=NOW)
    hidden = ctx.total_entries - len(ctx.injected)
    assert hidden > 0
    assert int(re.search(r"(\d+) more", ctx.index_line).group(1)) == hidden
    assert len(ctx.index_line) <= 200


def test_index_line_lists_tags_in_use_even_when_all_shown():
    ctx = build_memory_context([_entry(1, "Leads the Q3 churn project", tags=["q3-churn"])], now=NOW)
    assert "q3-churn (1)" in ctx.index_line


def test_handles_are_stable_across_renders():
    entries = _population(60)
    kw = extract_keywords("topic3 area1", unicode=True)
    a = build_memory_context(entries, now=NOW, keywords=kw)
    b = build_memory_context(list(reversed(entries)), now=NOW, keywords=kw)
    assert [(i.id, i.handle) for i in a.injected] == [(i.id, i.handle) for i in b.injected]
    for i in a.injected:
        assert f"[{i.handle}]" in a.body


def test_expired_facts_are_not_injected():
    past = _entry(1, "Old review", date=NOW - timedelta(days=10))
    stale = _entry(2, "Q2 pricing work", last_seen=NOW - timedelta(days=120))
    ctx = build_memory_context([past, stale], now=NOW, keywords={"pricing", "review"})
    assert ctx.injected == [] and ctx.body == ""


def test_undated_ranking_prefers_user_confirmed_then_seen_count_within_budget():
    many = [_entry(i, f"Fact {i} " + "x" * 120, seen=i) for i in range(1, 30)]
    mine = _entry(99, "Typed by the user " + "y" * 120, source="user", seen=1)
    ctx = build_memory_context(many + [mine], now=NOW)
    always = [i.handle for i in ctx.injected if i.tier == "always"]
    assert always[0] == "m99" and always[1] == "m29"
    body_always = "\n".join(l for l in ctx.body.splitlines() if l.startswith("[m"))
    assert len(body_always) <= ALWAYS_BUDGET
