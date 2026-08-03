"""@mention parsing.

Pins the behaviour that `frontend/utils/mentions.ts` must mirror — the two
implementations are read by the same stored instruction text, so they have to
agree on where a mention starts and ends.
"""

import time

import pytest

from app.utils.mentions import (
    MentionMatcher,
    canonicalize_mentions,
    extract_mention_labels,
    format_mention,
    mention_needs_quotes,
    parse_mention_segments,
    resolve_mentions,
)


DICT = MentionMatcher([
    "Sales Orders",
    "Sales",
    "customers",
    "Microsoft Fabric / dbo.sales",
    "Total Revenue (USD)",
    "search_products",
    "הזמנות מכירה",
])


def chips(text, matcher=DICT):
    return [s.label for s in parse_mention_segments(text, matcher) if s.label is not None]


class TestMultiWordNames:
    """The bug: a name with spaces was truncated to its first word."""

    def test_multi_word_name_resolves_whole(self):
        assert chips("Use @Sales Orders for revenue.") == ["Sales Orders"]

    def test_without_dictionary_only_first_word(self):
        # Documents the fallback: with no known names there is nothing to
        # delimit against, so the identifier-shaped match is all that is left.
        assert chips("Use @Sales Orders for revenue.", None) == ["Sales"]

    def test_longest_match_wins_over_prefix_name(self):
        assert chips("@Sales Orders") == ["Sales Orders"]

    def test_shorter_name_when_longer_does_not_apply(self):
        assert chips("@Sales figures rose") == ["Sales"]

    def test_slashes_and_dots(self):
        assert chips("Join @Microsoft Fabric / dbo.sales here.") == [
            "Microsoft Fabric / dbo.sales"
        ]

    def test_parens(self):
        assert chips("Track @Total Revenue (USD) monthly.") == ["Total Revenue (USD)"]

    def test_rtl_name(self):
        assert chips("ראה @הזמנות מכירה בבקשה") == ["הזמנות מכירה"]


class TestDelimiting:
    def test_quoted_wins_without_dictionary(self):
        assert chips('Use @"Sales Orders" now.', None) == ["Sales Orders"]

    def test_quoted_unknown_name_still_parses(self):
        assert chips('Use @"Not A Table" now.') == ["Not A Table"]

    def test_canonical_casing_returned(self):
        assert chips("@sales orders rocks") == ["Sales Orders"]

    def test_word_boundary_protects_longer_token(self):
        # "Sales" is a known name but must not chip the head of "SalesForce".
        assert chips("@SalesForce is a CRM") == ["SalesForce"]

    def test_prose_is_not_swallowed(self):
        segs = parse_mention_segments("@Sales Orders are late.", DICT)
        rebuilt = "".join(f"[{s.label}]" if s.label else s.text for s in segs)
        assert rebuilt == "[Sales Orders] are late."

    def test_unknown_mention_falls_back_to_identifier(self):
        assert chips("@unknown_table matters") == ["unknown_table"]

    def test_segments_reconstruct_source_exactly(self):
        for text in [
            "Use @Sales Orders and @customers.",
            'Quoted @"Sales Orders" plus @search_products.',
            "no mentions at all",
            "email a@b and @@ and @",
        ]:
            segs = parse_mention_segments(text, DICT)
            assert "".join(s.text for s in segs) == text


class TestHelpers:
    def test_extract_labels_dedupes_case_insensitively(self):
        assert extract_mention_labels("@Sales Orders and @sales orders", DICT) == [
            "Sales Orders"
        ]

    @pytest.mark.parametrize(
        "name,expected",
        [
            ("Sales Orders", '@"Sales Orders"'),
            ("dbo.sales", '@"dbo.sales"'),
            ("fact-table", '@"fact-table"'),
            ("customers", "@customers"),
            ("search_products", "@search_products"),
        ],
    )
    def test_format_mention(self, name, expected):
        assert format_mention(name) == expected

    def test_mention_needs_quotes(self):
        assert mention_needs_quotes("Sales Orders")
        assert not mention_needs_quotes("customers")
        assert not mention_needs_quotes(None)


CANDIDATES = [
    {"id": "t1", "type": "datasource_table", "name": "Sales Orders"},
    {"id": "t2", "type": "datasource_table", "name": "customers"},
    {"id": "r1", "type": "metadata_resource", "name": "Revenue Model"},
    {"id": "c1", "type": "connection_tool", "name": "search_products"},
]


class TestResolveMentions:
    def test_resolves_multi_word_to_object(self):
        refs = resolve_mentions("Use @Sales Orders with @customers.", CANDIDATES)
        assert [(r["object_type"], r["object_id"], r["display_text"]) for r in refs] == [
            ("datasource_table", "t1", "Sales Orders"),
            ("datasource_table", "t2", "customers"),
        ]

    def test_relation_type_is_mention(self):
        refs = resolve_mentions("@Revenue Model", CANDIDATES)
        assert refs[0]["relation_type"] == "mention"

    def test_unknown_mentions_are_dropped(self):
        assert resolve_mentions("@nothing_here at all", CANDIDATES) == []

    def test_deduplicates_repeat_mentions(self):
        refs = resolve_mentions("@Sales Orders then @Sales Orders again", CANDIDATES)
        assert len(refs) == 1

    def test_resolves_quoted_form(self):
        refs = resolve_mentions('Use @"Sales Orders".', CANDIDATES)
        assert refs[0]["object_id"] == "t1"

    def test_empty_candidates_resolve_nothing(self):
        assert resolve_mentions("@Sales Orders", []) == []


class TestCanonicalizeMentions:
    def test_quotes_multi_word_names(self):
        assert canonicalize_mentions(
            "Use @Sales Orders now.", CANDIDATES
        ) == 'Use @"Sales Orders" now.'

    def test_fixes_casing(self):
        assert canonicalize_mentions("@sales orders", CANDIDATES) == '@"Sales Orders"'

    def test_leaves_identifiers_bare(self):
        assert canonicalize_mentions("@customers rock", CANDIDATES) == "@customers rock"

    def test_leaves_unresolvable_text_untouched(self):
        text = "@not_a_table and plain @words here"
        assert canonicalize_mentions(text, CANDIDATES) == text

    def test_is_idempotent(self):
        once = canonicalize_mentions("Use @Sales Orders now.", CANDIDATES)
        assert canonicalize_mentions(once, CANDIDATES) == once


class TestScale:
    """Lookup must not degrade on agents carrying thousands of tables.

    The trie makes a match cost O(len(name)) instead of O(number of names); this
    pins that so a future 'simplification' back to a linear scan is caught here
    rather than as UI jank on a large agent.
    """

    NAMES = [
        f"Enterprise Data Warehouse Fact Table Sales Orders Region {i}"
        for i in range(5000)
    ]

    def test_resolves_correctly_at_5k_names(self):
        matcher = MentionMatcher(self.NAMES + ["customers"])
        text = "Use @Enterprise Data Warehouse Fact Table Sales Orders Region 4999 daily."
        assert chips(text, matcher) == [
            "Enterprise Data Warehouse Fact Table Sales Orders Region 4999"
        ]

    def test_parse_cost_is_independent_of_dictionary_size(self):
        doc = " ".join(
            f"Use @Enterprise Data Warehouse Fact Table Sales Orders Region {i * 250} "
            "joining on customer_id before aggregation."
            for i in range(20)
        )
        big = MentionMatcher(self.NAMES)
        small = MentionMatcher(["customers", "Sales Orders"])

        assert len([s for s in parse_mention_segments(doc, big) if s.label]) == 20

        def timed(matcher, iters=50):
            start = time.perf_counter()
            for _ in range(iters):
                parse_mention_segments(doc, matcher)
            return (time.perf_counter() - start) / iters

        timed(big, 5)  # warm up
        big_ms = timed(big) * 1000
        small_ms = timed(small) * 1000

        # 2500x the names must not cost anywhere near 2500x the time. Generous
        # bound so the test is not flaky on a loaded CI box; a linear scan
        # regression lands orders of magnitude above it.
        assert big_ms < max(small_ms * 10, 5.0), (
            f"5k-name parse took {big_ms:.3f} ms vs {small_ms:.3f} ms for 2 names"
        )
