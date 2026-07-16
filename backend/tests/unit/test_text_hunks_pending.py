"""Unit tests for the pending-review predicate `has_live_hunk_against_main`.

Two invariants matter here and are easy to break independently:

1. **Correctness** — the predicate must agree exactly with what the review pane
   renders. `has_live_hunk_against_main` (drives the "Pending review" badge /
   `/instructions/pending-changes`) and `rebased_hunks_against_main` (drives the
   tracked-changes diff / `/review-hunks`) are two computations of the same fact.
   If they disagree, an instruction shows "Pending review" with no changes to
   review (or vice-versa).

2. **Cheapness** — the predicate has an O(n) fast-path so the /agents pending
   sweep doesn't pay compute_hunks' ~O(n^2)-on-repeated-tokens word diff for
   every suggestion. The fast-path must never answer True when the authoritative
   diff would find nothing — hence the fuzz below cross-checks the two.
"""
import random

import pytest

from app.services.text_hunks import (
    has_live_hunk_against_main,
    rebased_hunks_against_main,
)


def _authoritative(base: str, proposed: str, main: str, rejected=None) -> bool:
    """The definition the predicate must match: at least one unrejected hunk that
    the review pane would actually render."""
    rejected = rejected or set()
    if proposed == base or proposed == main:
        return False
    return any(
        h["key"] not in rejected
        for h in rebased_hunks_against_main(base, proposed, main)
    )


def test_idempotent_insertion_is_not_pending():
    """The reported bug: a suggestion whose only change duplicates a token already
    at the anchor is a no-op against main — it must NOT read as pending."""
    for main, proposed in [
        ("alpha beta gamma", "alpha beta beta gamma"),
        ("Require a filter on queries", "Require a a filter on queries"),
        ("lookup by email", "lookup by email email"),
    ]:
        assert rebased_hunks_against_main(main, proposed, main) == []
        assert has_live_hunk_against_main(main, proposed, main) is False


def test_real_edits_are_pending():
    """A genuine change (new word, deletion, replacement) forked from current
    main is pending and renders hunks."""
    for main, proposed in [
        ("alpha beta gamma", "alpha beta filtered gamma"),   # insert new word
        ("alpha beta gamma", "alpha gamma"),                 # delete
        ("alpha beta gamma", "alpha BETA gamma"),            # replace
    ]:
        assert rebased_hunks_against_main(main, proposed, main) != []
        assert has_live_hunk_against_main(main, proposed, main) is True


def test_noop_against_main_never_pending():
    assert has_live_hunk_against_main("x", "x", "x") is False          # proposed == base
    assert has_live_hunk_against_main("a", "current", "current") is False  # proposed == main


@pytest.mark.parametrize("seed", range(25))
def test_predicate_matches_review_pane_fuzz(seed):
    """The badge predicate must equal the review-pane result for arbitrary edits,
    including the ambiguous repeated-token cases the fast-path must defer on.
    Covers both base==main (fresh suggestion) and base!=main (stale/drifted)."""
    rng = random.Random(seed)
    # Small, highly repetitive vocab (incl. punctuation/space-adjacent tokens) —
    # this is the fast-path's and difflib's worst case, so it exercises the
    # fall-through boundary hard.
    vocab = "the a filter on queries email name order prefix digit . , ( ) = ".split(" ")

    def rtext(n):
        return " ".join(rng.choice(vocab) for _ in range(n))

    for _ in range(400):
        base = rtext(rng.randint(0, 24))
        # base==main most of the time (the common fresh-suggestion case), but
        # sometimes drift main so the stale path is covered too.
        main = base if rng.random() < 0.7 else rtext(rng.randint(0, 24))
        toks = base.split(" ")
        for _ in range(rng.randint(1, 5)):
            r = rng.random()
            if toks and r < 0.4:
                toks.pop(rng.randrange(len(toks)))
            elif r < 0.7:
                toks.insert(rng.randrange(len(toks) + 1), rng.choice(vocab))
            elif toks:
                toks[rng.randrange(len(toks))] = rng.choice(vocab)
        proposed = " ".join(toks)

        assert (
            has_live_hunk_against_main(base, proposed, main)
            == _authoritative(base, proposed, main)
        ), f"badge/diff disagree: base={base!r} proposed={proposed!r} main={main!r}"
