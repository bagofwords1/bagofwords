"""Tool-call arguments damaged by a string quoted without the model's delimiter.

Gemma 4 served by vLLM (``--tool-call-parser gemma4``) delimits tool-call
strings with ``<|"|>``. When it quotes one with a backtick or plain quotes
instead, the parser returns valid JSON with the wrong shape: the next member
glued onto the damaged string under a garbage key (``page<|"|>,prompt``), or
quotes kept inside a value (``"'page'"``). Replaying a real create_artifact
request gave intact arguments in 3 of 8 calls; the rest failed validation
("prompt: Field required") or silently lost title and visualization_ids.

Contract: the damage names exactly what was lost, so it is undone against the
tool's own schema without changing any value's content; anything that does
not match exactly is returned as given, for validation to report.
"""
from __future__ import annotations

import pytest

from app.ai.llm.toolcall_args import UNPARSABLE_KEY, repair_mangled_arguments

D = '<|"|>'

SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "code": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "title": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "mode": {"type": "string", "enum": ["page", "slides"]},
        "tone": {"anyOf": [{"type": "string", "enum": ["formal", "casual"]}, {"type": "null"}]},
        "visualization_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["prompt"],
}
CODE = "<script type=\"text/babel\">\nfunction App() { return <div/>; }\n</script>"
VIZ = ["0b6b9a7e-5d0c-4a52-9a43-1f0f3c2b7d11"]


@pytest.mark.parametrize("closer", ["`", "</code>", "` "])
def test_member_swallowed_by_a_closing_backtick_is_restored(closer):
    damaged = {
        "code": CODE + closer + ",mode:",
        "slides" + D + ",prompt": "Quarterly deck for the board.",
        "title": "Q3 review",
        "visualization_ids": VIZ,
    }
    repaired, fields = repair_mangled_arguments(damaged, SCHEMA)

    assert repaired == {"code": CODE, "mode": "slides", "prompt": "Quarterly deck for the board.",
                        "title": "Q3 review", "visualization_ids": VIZ}
    assert fields == ["mode"]


@pytest.mark.parametrize("title", ["Revenue by region", "דשבורד בתי חולים", "  padded  "])
def test_key_lost_after_a_damaged_string_is_restored_verbatim(title):
    damaged = {"mode": "page", "prompt": "1. KPIs\n2. Ranking\n`,title:", title + D + ",visualization_ids": VIZ}
    repaired, fields = repair_mangled_arguments(damaged, SCHEMA)

    assert repaired["title"] == title
    assert repaired["prompt"] == "1. KPIs\n2. Ranking\n"
    assert repaired["visualization_ids"] == VIZ
    assert fields == ["title"]


def test_whitespace_in_restored_values_is_kept():
    # The value's own leading spaces and trailing newline are content.
    damaged = {"title": "T`,prompt:", "  indented\n" + D + ",mode": "page"}
    repaired, _ = repair_mangled_arguments(damaged, SCHEMA)
    assert repaired == {"title": "T", "prompt": "  indented\n", "mode": "page"}


def test_two_splits_in_one_call_are_both_restored():
    damaged = {"code": CODE + "`,mode:", "page" + D + ",prompt": "Albums per artist.`,,title:", "Albums" + D + ",visualization_ids": VIZ}
    repaired, fields = repair_mangled_arguments(damaged, SCHEMA)

    assert repaired == {"code": CODE, "mode": "page", "prompt": "Albums per artist.",
                        "title": "Albums", "visualization_ids": VIZ}
    assert sorted(fields) == ["mode", "title"]


@pytest.mark.parametrize("quoted, field, expected", [
    ("'slides'", "mode", "slides"), ('"page"', "mode", "page"), ("`slides`", "mode", "slides"),
    ("'casual'", "tone", "casual"),  # an Optional enum (anyOf) is a fixed choice too
])
def test_quotes_kept_around_a_fixed_choice_are_removed(quoted, field, expected):
    repaired, fields = repair_mangled_arguments({"prompt": "p", field: quoted}, SCHEMA)
    assert repaired[field] == expected and fields == [field]


@pytest.mark.parametrize("args", [
    {"prompt": "List the columns: a, b, title:", "code": CODE, "mode": "page", "title": "x"},
    {"prompt": "'Top 10' artists", "title": "'Quoted title'", "mode": "page"},
    {"prompt": "p", "mode": "' page '"},          # inside is not an allowed value as written
    {"prompt": "p", "mode": "'poster'"},           # not an allowed value at all
    {"code": CODE + "`,colour:", "page" + D + ",prompt": "p"},  # names no field of this tool
    {"code": CODE, "page" + D + ",prompt": "p"},   # garbage key with nothing naming what was lost
    {UNPARSABLE_KEY: True, "_raw": "{", "_error": "x"},
])
def test_anything_that_does_not_match_exactly_is_returned_as_given(args):
    repaired, fields = repair_mangled_arguments(dict(args), SCHEMA)
    assert repaired == args and fields == []


def test_without_a_schema_nothing_is_changed():
    damaged = {"code": CODE + "`,mode:", "page" + D + ",prompt": "p"}
    assert repair_mangled_arguments(dict(damaged), None) == (damaged, [])
