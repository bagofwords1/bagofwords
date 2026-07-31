"""search_agents query matching — forgiving, describe_tables-style semantics.

Pins the behavior from the PR #837 review session where "sales"/"albums"
matched nothing against the Music Store agent (table "Album"): matching must
be case-insensitive, singular/plural-forgiving, and glob-capable. The
zero-match fallback (usage-ranked candidates instead of a dead end) is
covered by the e2e eval; here we pin the pure matching layer.
"""
from app.ai.tools.implementations.search_agents import compile_query_patterns


MUSIC_STORE_HAYSTACK = "\n".join([
    "Music Store",
    "Digital music store sales: artists, albums, tracks, customers, invoices",
    "Album Artist Track Invoice InvoiceLine Customer Genre MediaType Playlist",
])

BARE_HAYSTACK = "\n".join([
    "Music Store",
    "",  # no description
    "Album Artist Track Invoice InvoiceLine Customer Genre",
])


def _matches(queries, haystack) -> bool:
    pats = compile_query_patterns(queries)
    return any(p.search(haystack) for p in pats)


def test_plural_query_matches_singular_table_name():
    # the exact review-session failure: "albums" vs table "Album"
    assert _matches(["albums"], BARE_HAYSTACK)
    assert _matches(["invoices"], BARE_HAYSTACK)
    assert _matches(["customers"], BARE_HAYSTACK)


def test_case_insensitive_substring():
    assert _matches(["album"], BARE_HAYSTACK)
    assert _matches(["ALBUM"], BARE_HAYSTACK)
    assert _matches(["music store"], BARE_HAYSTACK)


def test_ies_plural_folds_to_y():
    haystack = "Sales Territory\nterritory-level quotas\nTerritory Quota"
    assert _matches(["territories"], haystack)


def test_glob_terms_match():
    assert _matches(["Invoice*"], BARE_HAYSTACK)
    assert _matches(["*genre*"], BARE_HAYSTACK)


def test_union_semantics_any_term_suffices():
    # "sales" alone misses the bare haystack; adding "albums" must match
    assert not _matches(["sales"], BARE_HAYSTACK)
    assert _matches(["sales", "albums"], BARE_HAYSTACK)
    # but "sales" does match when the description mentions it
    assert _matches(["sales"], MUSIC_STORE_HAYSTACK)


def test_no_match_returns_false_cleanly():
    assert not _matches(["kubernetes"], MUSIC_STORE_HAYSTACK)


def test_invalid_regex_terms_do_not_crash():
    assert _matches(["album(("], BARE_HAYSTACK) or True  # must not raise
    pats = compile_query_patterns(["((("])
    assert isinstance(pats, list)


# ---------------------------------------------------------------------------
# Match quality + short-term precision (PO-scan review session follow-ups)
# ---------------------------------------------------------------------------
from app.ai.tools.implementations.search_agents import match_quality
from app.ai.tools.implementations._file_tool_common import (
    mark_agent_used,
    note_enumeration,
)


PROCUREMENT_STRONG = "Procurement Files\nPurchase orders and invoices from vendors"
PROCUREMENT_WEAK = "PO 2026 Invoices Contracts"
MUSIC_STRONG = "Music Store\nSample music database with artists, albums and tracks"
MUSIC_WEAK = "Album Artist Track Invoice InvoiceLine Customer Genre"


def _pats(terms):
    return compile_query_patterns(terms)


def test_descriptive_hit_outranks_table_name_only_hit():
    pats = _pats(["purchase order", "PO", "invoices"])
    s_proc, h_proc = match_quality(pats, PROCUREMENT_STRONG, PROCUREMENT_WEAK)
    s_music, h_music = match_quality(pats, MUSIC_STRONG, MUSIC_WEAK)
    # procurement source: strong (descriptive fields), multi-term coverage
    assert s_proc == "strong" and h_proc >= 2
    # music db matches only via its Invoice table -> weak, fewer terms
    assert s_music == "weak"
    assert h_proc > h_music


def test_no_match_returns_none_strength():
    pats = _pats(["purchase order", "PO", "invoices"])
    assert match_quality(pats, "HR portal\npeople data", "Employee Salary") == (None, 0)


def test_short_terms_are_word_bounded():
    pats = _pats(["PO"])
    # "portal"/"people" must NOT match a 2-letter term
    assert not any(p.search("HR portal for people") for p in pats)
    assert any(p.search("PO 2026 archive") for p in pats)


def test_mark_agent_used_and_enumeration_guard():
    class _DS:
        id = "ds-1"

    ctx = {"used_agent_ids": set()}
    mark_agent_used(ctx, _DS())
    assert ctx["used_agent_ids"] == {"ds-1"}
    # missing set in ctx: silently a no-op
    mark_agent_used({}, _DS())

    ctx2 = {}
    assert note_enumeration(ctx2, "conn-a", 11) is None
    hint = note_enumeration(ctx2, "conn-a", 7)
    assert hint and "already enumerated" in hint and "11" in hint
    # a different connection starts fresh
    assert note_enumeration(ctx2, "conn-b", 3) is None
