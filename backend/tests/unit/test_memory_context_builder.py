"""MemoryContextBuilder: tiers, budgets, matching and the index line.

Contract: however many entries a user has, the rendered <memory> body stays
within budget (~2,200 chars); the always tier carries style/role/preferences
plus events in the [-2d, +21d] window; the matched tier surfaces entries whose
text/aliases/tags overlap the turn or that carry an object tag present in the
turn; the index line accounts for everything left out.
"""
import random
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


def _entry(seq, section, text, *, tags=(), aliases=None, source="agent", seen=1, start=None, end=None,
           last_seen=None, expires_at=None):
    return SimpleNamespace(
        id=str(uuid.uuid4()), seq=seq, handle=f"m{seq}", section=section, text=text, tags=list(tags),
        aliases=aliases, source=source, seen_count=seen, event_start=start, event_end=end,
        expires_at=expires_at, last_seen_at=last_seen or NOW - timedelta(days=1), status="active",
    )


def _population(n, seed=7):
    rnd = random.Random(seed)
    sections = ["style", "role", "vocabulary", "events", "focus", "preferences"]
    out = []
    for i in range(1, n + 1):
        sec = sections[i % len(sections)]
        start = NOW + timedelta(days=rnd.randint(-40, 60)) if sec == "events" else None
        out.append(_entry(
            i, sec, f"Fact number {i} about topic{i % 17} with some descriptive words to fill the line",
            tags=[f"topic{i % 17}", f"area{i % 5}"], aliases=[f"alias{i}"] if sec == "vocabulary" else None,
            seen=rnd.randint(1, 9), start=start,
        ))
    return out


@pytest.mark.parametrize("n", [0, 10, 200])
def test_budget_holds_for_any_population(n):
    entries = _population(n)
    kw = extract_keywords("show topic3 topic5 area2 alias44 region churn " * 3, unicode=True)
    ctx = build_memory_context(entries, now=NOW, keywords=kw, object_tags=[], user_name="Dana")
    assert ctx.chars <= BLOCK_BUDGET
    if n == 0:
        assert ctx.body == "" and ctx.injected == []
    else:
        assert ctx.injected, "some entries must be injected"
        assert len([i for i in ctx.injected if i.tier == "matched"]) <= MATCHED_MAX_ENTRIES


def test_header_states_memory_holds_no_definitions_or_rules():
    ctx = build_memory_context([_entry(1, "style", "Prefers the number first")], now=NOW, user_name="Dana")
    first = ctx.body.splitlines()[:2]
    assert "Personal context about Dana" in first[0]
    assert "definitions and rules are in <instructions>" in "\n".join(first)


def test_always_tier_contents_and_event_window():
    style = _entry(1, "style", "Prefers the number first")
    role = _entry(2, "role", "Owns EMEA revenue reporting")
    pref = _entry(3, "preferences", "Wants the SQL shown")
    soon = _entry(4, "events", "Board meeting", start=NOW + timedelta(days=3))
    far = _entry(5, "events", "Offsite", start=NOW + timedelta(days=40))
    just_ended = _entry(6, "events", "QBR", start=datetime(2026, 10, 5))  # yesterday, all-day
    vocab = _entry(7, "vocabulary", "\"my region\" = EMEA", aliases=["my region"])
    focus = _entry(8, "focus", "Investigating Q3 churn")
    ctx = build_memory_context([style, role, pref, soon, far, just_ended, vocab, focus], now=NOW)
    always = {i.handle for i in ctx.injected if i.tier == "always"}
    assert always == {"m1", "m2", "m3", "m4", "m6"}
    # Nothing matched without keywords; the rest is accounted for in the index.
    assert "Also remembered (not shown)" in ctx.index_line
    assert "vocabulary" in ctx.index_line and "focus" in ctx.index_line


def test_events_render_absolute_date_and_relative_hint():
    soon = _entry(4, "events", "Board meeting", start=datetime(2026, 10, 9))
    ctx = build_memory_context([soon], now=NOW)
    assert "[m4] event: Fri 2026-10-09 (in 3 days): Board meeting" in ctx.body


def test_alias_match_puts_vocabulary_in_matched_tier():
    vocab = _entry(7, "vocabulary", "\"my region\" = EMEA", aliases=["my region"], tags=["emea"])
    other = _entry(8, "vocabulary", "\"the board deck\" = Q3 Board Pack", tags=["board-deck"])
    kw = extract_keywords("revenue in my region last quarter", unicode=True)
    ctx = build_memory_context([vocab, other], now=NOW, keywords=kw)
    matched = {i.handle: i for i in ctx.injected if i.tier == "matched"}
    assert set(matched) == {"m7"}
    assert "region" in (matched["m7"].reason or "")
    assert "← matched" in ctx.body


def test_object_tag_matches_when_agent_in_turn_and_outranks_keywords():
    agent_id = "ds-" + uuid.uuid4().hex[:6]
    tied = _entry(1, "vocabulary", "Prefers weekly granularity with this agent", tags=[f"agent:{agent_id}"])
    keyword = _entry(2, "focus", "Investigating churn in revenue", tags=["churn"])
    unrelated = _entry(3, "focus", "Planning the offsite", tags=["offsite"])
    kw = extract_keywords("churn revenue", unicode=True)
    ctx = build_memory_context([keyword, tied, unrelated], now=NOW, keywords=kw, object_tags=[f"agent:{agent_id}"])
    matched = [i.handle for i in ctx.injected if i.tier == "matched"]
    assert matched[0] == "m1" and "m2" in matched and "m3" not in matched
    # Without the agent in the turn the object-tagged entry is not matched.
    ctx2 = build_memory_context([keyword, tied], now=NOW, keywords=set(), object_tags=["agent:other"])
    assert "m1" not in {i.handle for i in ctx2.injected}


def test_index_line_counts_what_was_left_out():
    entries = _population(200)
    ctx = build_memory_context(entries, now=NOW)
    shown = len(ctx.injected)
    hidden_live = ctx.total_entries - shown
    counted = sum(int(tok) for tok in __import__("re").findall(r"(\d+) [a-z]+", ctx.index_line.split("·")[0]))
    assert counted == hidden_live > 0
    assert len(ctx.index_line) <= 200


def test_index_line_lists_tags_in_use_even_when_all_shown():
    ctx = build_memory_context([_entry(1, "style", "Short answers", tags=["brevity"])], now=NOW)
    assert "brevity (1)" in ctx.index_line


def test_handles_are_stable_across_renders():
    entries = _population(60)
    kw = extract_keywords("topic3 area1", unicode=True)
    a = build_memory_context(entries, now=NOW, keywords=kw)
    b = build_memory_context(list(reversed(entries)), now=NOW, keywords=kw)
    assert [(i.id, i.handle) for i in a.injected] == [(i.id, i.handle) for i in b.injected]
    for i in a.injected:
        assert f"[{i.handle}]" in a.body


def test_expired_entries_are_not_injected():
    past = _entry(1, "events", "Old review", start=NOW - timedelta(days=10))
    stale = _entry(2, "focus", "Q2 pricing", last_seen=NOW - timedelta(days=45))
    ctx = build_memory_context([past, stale], now=NOW, keywords={"pricing", "review"})
    assert ctx.injected == [] and ctx.body == ""


def test_always_tier_prefers_user_authored_then_seen_count_within_budget():
    many = [_entry(i, "preferences", f"Preference {i} " + "x" * 120, seen=i) for i in range(1, 30)]
    mine = _entry(99, "preferences", "Typed by the user " + "y" * 120, source="user", seen=1)
    ctx = build_memory_context(many + [mine], now=NOW)
    always = [i.handle for i in ctx.injected if i.tier == "always"]
    assert always[0] == "m99"
    assert always[1] == "m29"  # then highest seen_count
    body_always = "\n".join(l for l in ctx.body.splitlines() if l.startswith("[m"))
    assert len(body_always) <= ALWAYS_BUDGET
