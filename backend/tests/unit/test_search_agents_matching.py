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
