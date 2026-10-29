"""Pure memory rules: normalization, tags, the rule-vs-fact test, content
filters, expiry, legacy splitting, signals and direct-edit detection.

Contract: the same text normalizes / expires / gets refused the same way for
every writer (agent tools, user API, migration) — see app/services/memory_rules.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import memory_rules as R


# --- normalization -----------------------------------------------------------

@pytest.mark.parametrize("a,b", [
    ("Prefers the number first.", "prefers the number first"),
    ("  Prefers   the NUMBER first!! ", "Prefers the number, first"),
    ("Amounts in €M, one decimal", "amounts in m one decimal"),
])
def test_normalize_text_variants_collapse(a, b):
    assert R.normalize_text(a) == R.normalize_text(b)


def test_normalize_text_keeps_non_latin_letters():
    assert R.normalize_text("מעדיף  מספרים קודם!") == "מעדיף מספרים קודם"


@pytest.mark.parametrize("raw", ["Board Deck", "board_deck", " board--deck ", "BOARD deck", "board-deck"])
def test_tag_normalization_to_single_slug(raw):
    assert R.normalize_tag(raw) == "board-deck"


def test_normalize_tags_merges_exact_slugs_and_keeps_order():
    assert R.normalize_tags(["EMEA", "Board Deck", "board_deck", "emea", ""]) == ["emea", "board-deck"]


def test_object_tags_keep_prefix_and_id():
    assert R.normalize_tag("Agent:ABC-123") == "agent:abc-123"
    assert R.normalize_tag("data source:ds1") == "data_source:ds1"
    assert R.is_object_tag("report:r1")
    assert not R.is_object_tag("emea")


# --- memory vs instructions boundary ----------------------------------------

# One test: a rule about how to answer or compute is an instruction; a fact
# about the user is memory. Rules come in many shapes — definitions, filters,
# conventions, formatting, and how-to-answer preferences phrased as facts.
@pytest.mark.parametrize("text", [
    # definitions / metric logic / required filters (org rules)
    "Active customers are those who paid in the last 90 days",
    "Active customer = paid invoice in the last 90 days",
    "Revenue means net revenue excluding VAT",
    "Churn is defined as no login for 60 days",
    "ARR is calculated as MRR times 12",
    "Always filter out test accounts",
    "Exclude internal accounts from every metric",
    "Use orders.created_at for order date",
    "EMEA includes Turkey",
    "The column status means order state",
    "You must always exclude refunded orders",
    "Qualified leads are leads with a demo booked",
    # how-to-answer rules (personal or org conventions)
    "Prefers the number first, then one line of context",
    "Amounts in €M, one decimal",
    "Show amounts in USD",
    "Prefers money amounts in thousands like $2.3K",
    "Wants the SQL shown",
    "Likes bullet summaries for execs",
    "Dates as DD/MM",
    "Never use emojis",
])
def test_rules_are_flagged(text):
    assert R.looks_like_rule(text), text


@pytest.mark.parametrize("text", [
    "When I say my region I mean EMEA",
    "\"my region\" = EMEA",
    "Uses “my region” to mean Germany.",
    "By 'the board deck' I mean report Q3 Board Pack",
    "Finance, owns EMEA revenue reporting; presents to the CFO monthly",
    "Board meeting",
    "Out of office",
    "Leads the Q3 churn project",
    "Q3 churn project due Oct 15",
    "Follows weekly net revenue retention for EMEA",
    "Tracks weekly signups for the APAC launch",
    "Preparing the Q4 budget review",
    "Their region is EMEA",
    "Presents the monthly numbers to the board",
])
def test_facts_are_not_flagged(text):
    assert R.looks_like_rule(text) is None, text


# --- content filters ---------------------------------------------------------

@pytest.mark.parametrize("text", [
    "my key is sk-proj-abcdefghijklmnopqrstuvwxyz0123",
    "password: hunter22",
    "AWS key AKIAABCDEFGHIJKLMNOP",
    "token = ghp_abcdefghijklmnopqrstuvwxyz0123456789",
])
def test_secrets_detected(text):
    assert R.contains_secret(text)


@pytest.mark.parametrize("text", [
    "Prefers the number first", "Board meeting 2026-10-09", "Uses 4 decimal places", "Q3 2026 board pack",
])
def test_ordinary_text_not_secret(text):
    assert not R.contains_secret(text)


def test_health_details_are_sensitive_but_availability_is_not():
    assert R.contains_sensitive("Out for surgery next week")
    assert not R.contains_sensitive("Out of office 2026-10-13 to 10-17")


# --- expiry (computed on read) -----------------------------------------------

def _e(**kw):
    base = dict(expires_at=None, event_start=None, event_end=None, last_seen_at=None, source="agent")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize("hours_after_end,expired", [(0, False), (23, False), (24, True), (120, True)])
def test_timed_date_hidden_one_day_after_end(hours_after_end, expired):
    end = datetime(2026, 10, 9, 14, 30)
    e = _e(event_start=end - timedelta(hours=1), event_end=end)
    assert R.is_expired(e, end + timedelta(hours=hours_after_end)) is expired


@pytest.mark.parametrize("day,expired", [(9, False), (10, False), (11, True)])
def test_date_only_fact_lasts_its_day_then_one_more(day, expired):
    e = _e(event_start=datetime(2026, 10, 9))
    assert R.is_expired(e, datetime(2026, 10, day, 12, 0) if day != 11 else datetime(2026, 10, 11)) is expired


def test_range_visible_for_whole_duration():
    start, end = datetime(2026, 10, 13), datetime(2026, 10, 17)
    e = _e(event_start=start, event_end=end)
    for d in range(0, 5):
        assert not R.is_expired(e, start + timedelta(days=d, hours=18))
    assert not R.is_expired(e, datetime(2026, 10, 18, 12))  # the day after: "yesterday"
    assert R.is_expired(e, datetime(2026, 10, 19))


@pytest.mark.parametrize("source", ["agent", "migration"])
@pytest.mark.parametrize("age,expired", [(30, False), (89, False), (90, True), (200, True)])
def test_undated_agent_facts_go_stale_after_last_seen(source, age, expired):
    seen = datetime(2026, 9, 1)
    e = _e(last_seen_at=seen, source=source)
    assert R.is_expired(e, seen + timedelta(days=age)) is expired


def test_user_confirmed_undated_facts_never_expire_but_explicit_expiry_wins():
    assert not R.is_expired(_e(last_seen_at=datetime(2000, 1, 1), source="user"), datetime(2030, 1, 1))
    e = _e(source="user", expires_at=datetime(2026, 1, 1))
    assert R.is_expired(e, datetime(2026, 1, 2))


def test_parse_when_rejects_non_iso():
    with pytest.raises(R.MemoryValidationError) as ei:
        R.parse_when("next thursday")
    assert ei.value.code == "memory.invalid_date"
    assert R.parse_when("2026-10-09") == datetime(2026, 10, 9)
    assert R.parse_when("2026-10-09T10:00:00Z") == datetime(2026, 10, 9, 10, 0)


# --- quotes / direct edit --------------------------------------

def test_best_quote_is_users_sentence_and_bounded():
    msg = "Thanks. Please keep it shorter and put the number first. Also check Q3." + " pad" * 100
    q = R.best_quote(msg, "Prefers the number first")
    assert "number first" in q and len(q) <= R.MAX_QUOTE_CHARS


@pytest.mark.parametrize("msg,expected", [
    ("Forget that I report in €M", True),
    ("Please change it: I report to the board in USD now, not €M", True),
    ("What was revenue in €M last month?", False),        # mentions it, doesn't ask to change it
    ("forget it, show me churn instead", False),          # edit verb, but about something else
])
def test_direct_edit_request_requires_verb_and_reference(msg, expected):
    assert R.is_direct_edit_request(msg, "Reports amounts to the board in €M", ["currency"]) is expected


# --- noticing: fact vs rule signals behind the <memory_hint> ----------------

@pytest.mark.parametrize("msg", [
    "I'm the head of FP&A and I present to the CFO monthly",
    "When I say my region I mean EMEA",
    "Board meeting next Thursday",
    "I'm off the week of 2026-10-12",
    "I'm working on the Q3 churn project",
    "I track weekly NRR for EMEA",
    "Remember that the launch is in November",
])
def test_fact_signals_detected(msg):
    assert R.fact_signals(msg)


@pytest.mark.parametrize("msg", [
    "Way too long. Keep it shorter: number first.",
    "Show amounts in thousands with one decimal",
    "From now on give me bullets",
])
def test_rule_signals_detected_and_not_counted_as_facts(msg):
    assert R.rule_signals(msg)


@pytest.mark.parametrize("msg", [
    "What were the top 5 countries by revenue?",
    "How many tracks per genre?",
    "Revenue by month for 2025",
])
def test_no_signals_in_ordinary_questions(msg):
    assert R.fact_signals(msg) == [] and R.rule_signals(msg) == []


@pytest.mark.parametrize("old,new,expected", [
    ("Leads the Q3 churn project", "Leads the Q3 churn project; also owns the APAC launch plan", True),
    ("Board meeting.", "board meeting, then the offsite with the regional leads", True),
    ("Leads the Q3 churn project", "Leads the Q4 churn project", False),        # a change, not an append
    ("Leads the Q3 churn project", "Leads the Q3 churn project now", False),    # too small to be a new fact
    ("Board meeting", "Board meeting", False),
])
def test_appends_new_fact(old, new, expected):
    assert R.appends_new_fact(old, new) is expected

