"""Unit tests for app.services.suggestion_merge.

`covers()` decides whether a pending suggestion is already fully contained in
another text (main, or a later sibling suggestion), which is how the review
surfaces skip already-applied proposals and collapse intermediate snapshots.

It used to answer that with a character-level `difflib.SequenceMatcher`, which is
quadratic and degenerates on repetitive text — one call on a customer's 20k-char
instruction took 66.7s and was 100% of `GET /api/instructions/{id}`. It is now a
linear subsequence scan. These tests pin both halves of that change: the answers
must not move, and the pathological input must stay fast.
"""
import difflib
import random
import time

import pytest

from app.services.suggestion_merge import covers, superseded_by_containment


def _covers_difflib(small: str, big: str) -> bool:
    """The pre-change implementation, kept here as the differential oracle."""
    small = small or ""
    big = big or ""
    if small == big:
        return False
    if not small:
        return True
    sm = difflib.SequenceMatcher(None, small, big, autojunk=False)
    return not any(tag in ("delete", "replace") for tag, *_ in sm.get_opcodes())


RULE = (
    "- Table HR_FACT_{i}: one row per employee per day. Columns employee_id, "
    "work_date, hours_worked, absence_code, cost_center. Exclude approved_flag = 0.\n"
)


def _doc(n_lines: int) -> str:
    """A system-prompt shaped instruction: many near-identical rule lines. This
    repetition is what makes the old character diff degenerate."""
    return "".join(RULE.format(i=i) for i in range(n_lines))


# ---------------------------------------------------------------- contract ---

def test_pure_insertion_is_covered():
    base = _doc(5)
    assert covers(base, base + "\nAlso cap the lookback window to 24 months.\n")


def test_insertion_in_the_middle_is_covered():
    base = _doc(5)
    cut = len(base) // 2
    assert covers(base, base[:cut] + "\nNew mid-document rule.\n" + base[cut:])


def test_identical_text_is_not_covered():
    # Nothing was added, so `big` is not a *cumulative* superset.
    assert covers(_doc(4), _doc(4)) is False


def test_deletion_is_not_covered():
    base = _doc(6)
    assert covers(base, base[:200] + base[400:]) is False


def test_replacement_is_not_covered():
    base = _doc(6)
    assert covers(base, base[:200] + "TOTALLY DIFFERENT TEXT" + base[400:]) is False


def test_reordering_is_not_covered():
    lines = _doc(8).splitlines(True)
    assert covers("".join(lines), "".join(reversed(lines))) is False


def test_longer_small_is_not_covered():
    assert covers(_doc(6), _doc(2)) is False


def test_empty_and_none_handling():
    assert covers("", "anything") is True      # empty proposal is in everything
    assert covers("", "") is False             # ...but not in nothing
    assert covers(None, None) is False
    assert covers("abc", None) is False


# ------------------------------------------------------------ differential ---

@pytest.mark.parametrize("seed", [1, 7, 11, 23])
def test_matches_the_previous_difflib_implementation(seed):
    """Randomised edits across every shape the review surfaces produce: the new
    scan must agree with the old alignment on all of them."""
    rng = random.Random(seed)
    for _ in range(120):
        base = _doc(rng.randint(3, 20))
        kind = rng.choice(["insert", "append", "delete", "replace", "reorder", "identical"])
        if kind == "insert":
            p = rng.randrange(len(base))
            big = base[:p] + "\nCap the lookback window to 24 months.\n" + base[p:]
        elif kind == "append":
            big = base + "\nAppended rule.\n"
        elif kind == "delete":
            p = rng.randrange(0, max(1, len(base) - 200))
            big = base[:p] + base[p + 150:]
        elif kind == "replace":
            p = rng.randrange(0, max(1, len(base) - 200))
            big = base[:p] + "DIFFERENT TEXT HERE" + base[p + 150:]
        elif kind == "reorder":
            lines = base.splitlines(True)
            rng.shuffle(lines)
            big = "".join(lines)
        else:
            big = base
        assert covers(base, big) == _covers_difflib(base, big), f"disagreement on {kind}"


def test_never_reports_covered_where_difflib_reported_uncovered_the_other_way():
    """The one-way guarantee the swap rests on: an alignment with no
    delete/replace leaves every character of `small` in an `equal` block, so
    difflib=True implies subsequence=True. Drift can therefore only ever go the
    other direction (difflib wrongly saying False), never towards *hiding* less."""
    rng = random.Random(99)
    for _ in range(200):
        base = _doc(rng.randint(2, 12))
        p = rng.randrange(len(base))
        big = base[:p] + rng.choice(["\nX\n", "", " extra "]) + base[p:]
        if _covers_difflib(base, big):
            assert covers(base, big)


# -------------------------------------------------------------------- perf ---

def test_pathological_pair_is_not_quadratic():
    """The customer shape: two ~20k-char repetitive instructions differing by a
    rewritten section. The old implementation took tens of seconds on this pair
    (66.7s on their real text) with the request holding the event loop."""
    small = _doc(140)
    big = small[:9000] + "REWRITTEN SECTION " * 20 + small[9600:]
    started = time.perf_counter()
    assert covers(small, big) is False
    assert time.perf_counter() - started < 1.0


# ---------------------------------------------------- superseded_by_containment ---

def test_intermediate_snapshot_is_superseded_by_the_cumulative_one():
    base = "Lorem ipsum."
    a = base + " lorem"
    b = base + " lorem hello"
    assert superseded_by_containment({"a": (a, base), "b": (b, base)}) == {"a"}


def test_deletion_only_suggestion_is_never_dropped():
    """`a` removes text rather than adding it, so it is not an intermediate even
    if a longer unrelated sibling happens to contain its text."""
    base = "Alpha beta gamma."
    a = "Alpha gamma."                      # a deletion over its own base
    b = "Alpha gamma. Plus more content."   # contains `a`, but `a` isn't additive
    assert superseded_by_containment({"a": (a, base), "b": (b, base)}) == set()


def test_unrelated_siblings_are_both_kept():
    base = "Shared preamble."
    a = base + " alpha"
    b = base + " beta"
    assert superseded_by_containment({"a": (a, base), "b": (b, base)}) == set()
