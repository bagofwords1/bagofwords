import pytest

from app.services.text_hunks import (
    RebasedHunkCache,
    has_live_hunk_against_main,
    rebased_hunks_against_main,
)

CASES = [
    ("alpha beta gamma", "alpha BETA gamma", "alpha beta gamma"),
    ("alpha beta gamma", "alpha BETA gamma", "alpha BETA gamma"),
    ("alpha beta gamma", "alpha BETA gamma", "prefix alpha beta gamma suffix"),
    ("rule one\nrule two\n", "rule one\nrule three\n", "rule one\nrule TWO\n"),
    ("same token " * 80, "same token " * 40 + "changed token " + "same token " * 39, "same token " * 80),
    ("", "new instruction", ""),
]


@pytest.mark.parametrize(("base", "proposed", "main"), CASES)
def test_live_hunk_boolean_matches_full_hunk_contract(base, proposed, main):
    hunks = rebased_hunks_against_main(base, proposed, main)
    rejection_sets = [set()]
    if hunks:
        rejection_sets.extend([
            {hunks[0]["key"]},
            {hunk["key"] for hunk in hunks},
        ])

    for rejected in rejection_sets:
        expected = any(hunk["key"] not in rejected for hunk in hunks)
        assert has_live_hunk_against_main(base, proposed, main, rejected) is expected


def test_request_local_cache_preserves_hunks_and_keys():
    cache = RebasedHunkCache()

    for _ in range(2):
        for base, proposed, main in CASES:
            expected = rebased_hunks_against_main(base, proposed, main)
            actual = rebased_hunks_against_main(base, proposed, main, cache=cache)
            assert actual == expected

    assert cache.intents
    assert cache.alignments
