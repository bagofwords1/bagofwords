"""Matching a miscopied visualization id to the one that was meant (_viz_ids).

A model copies a 36-character id out of an earlier tool result and gets a
character wrong. The matcher takes the id only when exactly one candidate is a
near miss; a guess between two charts, or a match to something unrelated, is
worse than the error it replaces.
"""
import random
import sys
import uuid
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.tools.implementations._doc_markdown import (  # noqa: E402
    extract_viz_placeholders, rewrite_viz_placeholders,
)
from app.ai.tools.implementations._viz_ids import (  # noqa: E402
    MAX_EDIT_DISTANCE, MIN_PREFIX_CHARS, apply_id_corrections, closest_viz_id,
    correct_code_viz_refs, normalize_viz_id,
)

HEX = "0123456789abcdef"


def _ids(n=6):
    return [str(uuid.uuid4()) for _ in range(n)]


def _insert(vid, rng):
    at = rng.randrange(len(vid) + 1)
    return vid[:at] + rng.choice(HEX) + vid[at:]


def _drop(vid, rng):
    at = rng.choice([i for i, ch in enumerate(vid) if ch != "-"])
    return vid[:at] + vid[at + 1:]


def _swap(vid, rng):
    at = rng.choice([i for i, ch in enumerate(vid) if ch != "-"])
    return vid[:at] + rng.choice([h for h in HEX if h != vid[at]]) + vid[at + 1:]


SLIPS = {"inserted character": _insert, "dropped character": _drop, "wrong character": _swap}


@pytest.mark.parametrize("slip", SLIPS)
@pytest.mark.parametrize("seed", range(25))
def test_a_one_character_slip_finds_the_id_that_was_meant(slip, seed):
    rng = random.Random(f"{slip}-{seed}")
    ids = _ids()
    meant = rng.choice(ids)
    assert closest_viz_id(SLIPS[slip](meant, rng), ids) == meant


@pytest.mark.parametrize("seed", range(25))
def test_slips_up_to_the_limit_still_match(seed):
    rng = random.Random(seed)
    ids = _ids()
    meant = rng.choice(ids)
    wrong = meant
    for _ in range(MAX_EDIT_DISTANCE):
        wrong = rng.choice(list(SLIPS.values()))(wrong, rng)
    assert closest_viz_id(wrong, ids) == meant


@pytest.mark.parametrize("seed", range(25))
def test_an_unrelated_id_matches_nothing(seed):
    ids = [str(uuid.UUID(int=random.Random(f"a{seed}-{i}").getrandbits(128))) for i in range(40)]
    stranger = str(uuid.UUID(int=random.Random(f"b{seed}").getrandbits(128)))
    assert closest_viz_id(stranger, ids) is None


def test_an_id_too_damaged_to_trust_matches_nothing():
    rng = random.Random(7)
    meant = str(uuid.uuid4())
    wrong = meant
    for _ in range(MAX_EDIT_DISTANCE + 4):
        wrong = _swap(wrong, rng)
    # Each swap picks a position at random, so make sure enough differ.
    assert sum(a != b for a, b in zip(wrong, meant)) > MAX_EDIT_DISTANCE
    assert closest_viz_id(wrong, [meant]) is None


def test_a_slip_that_fits_two_candidates_is_not_guessed():
    a = "3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"
    b = "3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5c"  # one character from `a`
    between = "3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5d"  # one character from both
    assert closest_viz_id(between, [a, b]) is None
    assert closest_viz_id(between, [a]) == a


@pytest.mark.parametrize("length", [MIN_PREFIX_CHARS, 13, 23])
def test_a_cut_off_id_matches_the_one_id_it_starts(length):
    ids = _ids()
    assert closest_viz_id(ids[2][:length], ids) == ids[2]


def test_a_cut_off_id_shared_by_two_candidates_or_too_short_is_not_guessed():
    a = "3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"
    b = "3f2a1b4c-0000-4f70-8a9b-0c1d2e3f4a5b"
    assert closest_viz_id("3f2a1b4c", [a, b]) is None
    assert closest_viz_id(a[:MIN_PREFIX_CHARS - 1], [a]) is None
    assert closest_viz_id("", [a]) is None


@pytest.mark.parametrize("wrap", [
    "{}", " {} ", "`{}`", '"{}"', "{{{}}}", "viz_id: {}", "viz:{}", "visualization_id={}", "ID: {}",
])
def test_an_id_has_one_spelling(wrap):
    vid = str(uuid.uuid4())
    assert normalize_viz_id(wrap.format(vid)) == vid
    assert normalize_viz_id(wrap.format(vid.upper())) == vid
    assert normalize_viz_id(wrap.format(vid.replace("-", ""))) == vid


def test_text_that_is_not_an_id_is_only_trimmed():
    assert normalize_viz_id("  Sales-Chart ") == "sales-chart"
    assert normalize_viz_id(None) == ""


def test_corrections_rewrite_whole_ids_only():
    right = "3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"
    cut = right[:8]
    code = f'const a = vizById("{cut}"); const b = vizById("{right}"); const c = "{cut}0";'
    fixed = apply_id_corrections(code, {cut: right})
    # The cut-off id is completed; the full id that starts with it, and a
    # longer token that merely contains it, are untouched.
    assert fixed == f'const a = vizById("{right}"); const b = vizById("{right}"); const c = "{cut}0";'
    assert apply_id_corrections(code, {}) == code
    assert apply_id_corrections(None, {cut: right}) is None


def test_a_miscopied_reference_in_code_is_pointed_at_the_payload_id():
    rng = random.Random(3)
    kept, other = _ids(2)
    wrong = _insert(kept, rng)
    code = f'const sales = vizById("{wrong}"); const o = vizById("{other}");'
    assert correct_code_viz_refs(code, [kept, other]) == (
        f'const sales = vizById("{kept}"); const o = vizById("{other}");'
    )


def test_a_reference_to_no_payload_id_is_left_for_the_reference_check():
    kept = str(uuid.uuid4())
    stranger = str(uuid.uuid4())
    code = f'const gone = vizById("{stranger}");'
    assert correct_code_viz_refs(code, [kept]) == code
    assert correct_code_viz_refs(code, []) == code


def test_a_doc_placeholder_is_rewritten_but_a_quoted_example_is_not():
    rng = random.Random(5)
    right = str(uuid.uuid4())
    wrong = _drop(right, rng)
    markdown = f"# Report\n\n{{{{viz:{wrong}}}}}\n\n```\n{{{{viz:{wrong}}}}}\n```\n"
    fixed = rewrite_viz_placeholders(markdown, {wrong: right})
    assert extract_viz_placeholders(fixed) == [right]
    assert f"```\n{{{{viz:{wrong}}}}}\n```" in fixed
    assert rewrite_viz_placeholders(markdown, {}) == markdown
