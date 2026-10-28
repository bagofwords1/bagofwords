"""budget_artifact_read: what a bounded read still tells the model.

Trimming a read to fit the transport must not cost the planner the two things
it needs afterwards: a description of what it just read, and arguments that
reach the rest. These are contracts of the budget itself, so they are checked
on the real ``ReadArtifactInput`` rather than through a seeded artifact (the
tool-level view lives in tests/e2e/test_read_artifact_model_view.py).
"""
import json

import pytest

from app.ai.context.transcript import MAX_RESULT_CHARS
from app.ai.tools.artifact_read_budget import (
    MODEL_READ_MAX_CHARS,
    budget_artifact_read,
)
from app.ai.tools.implementations.read_artifact import GREP_LINE_CLIP
from app.ai.tools.schemas.read_artifact import ReadArtifactInput

ARTIFACT_ID = "11111111-2222-3333-4444-555555555555"

# What read_artifact's own summary carries into the observation: the planner
# reads the counts and the window note out of it, and it is the only field
# that survives tier decay for this tool (no digest_keys declared).
READ_SUMMARY = (
    "Read artifact 'Executive dashboard' (page, v7, 209,549 chars / 3,000 lines, "
    "read_mode={mode}) — {note}. The slice is verbatim — safe to quote in SEARCH blocks."
)


def _request(**kwargs):
    return ReadArtifactInput(artifact_id=ARTIFACT_ID, **kwargs)


def _identity(mode, note, **extra):
    return {
        "summary": READ_SUMMARY.format(mode=mode, note=note),
        "artifact_id": ARTIFACT_ID,
        "title": "Executive dashboard",
        "mode": "page",
        "read_mode": mode,
        "total_lines": 3000,
        "total_chars": 209_549,
        "version": 7,
        **extra,
    }


def _oversized_source(lines=4000):
    """Source whose escaped size exceeds any source budget."""
    return "\n".join(f'  <div className="cell-{i}">שלום עולם</div>' for i in range(lines))


# ─── the read's own description survives being bounded ───────────────────────
# Replacing the summary (rather than adding to it) left the planner knowing
# only that *something* was partial — not which lines, not how many matches.

@pytest.mark.parametrize("mode,note,observation,request_kwargs", [
    (
        "range", "showing lines 1-4000 of 3000",
        {"code": _oversized_source(), "start_line": 1, "end_line": 4000, "truncated": True},
        {"offset": 1, "limit": 2000},
    ),
    (
        "full", "code is 209,549 chars",
        {"code": _oversized_source()},
        {},
    ),
    (
        "grep", "180 match(es) for pattern 'className'",
        {
            "matches": [
                {"line_no": i + 1, "line": "x" * 400, "before": [], "after": [],
                 "context_start_line": i + 1}
                for i in range(180)
            ],
            "grep_total_matches": 180,
        },
        {"grep_pattern": "className", "max_matches": 200},
    ),
    (
        "outline", "too large to return in full",
        {"outline": _oversized_source()},
        {},
    ),
])
def test_a_bounded_read_still_describes_what_it_read(mode, note, observation, request_kwargs):
    original = _identity(mode, note, **observation)
    result = budget_artifact_read(dict(original), _request(**request_kwargs))

    assert result.get("error") is None
    # Something was in fact trimmed, or this case proves nothing.
    assert result.get("next_read"), "expected a partial read for this case"
    # Every word the read said about itself is still there …
    assert original["summary"] in result["summary"]
    # … and the planner is additionally told the view is partial.
    assert len(result["summary"]) > len(original["summary"])
    assert "next_read" in result["summary"]


def test_a_read_that_fits_is_described_exactly_as_it_was():
    original = _identity("range", "showing lines 1-2 of 3000", code="a\nb\n",
                         start_line=1, end_line=2, truncated=False)
    result = budget_artifact_read(dict(original), _request(offset=1, limit=2))

    assert result["summary"] == original["summary"]
    assert "next_read" not in result
    assert result["code"] == "a\nb\n"


# ─── a continuation always advances ──────────────────────────────────────────
# A page that returns no match while reporting more to come would hand back
# the arguments it was called with, and the planner would read it forever.

@pytest.mark.parametrize("match_offset", [0, 7, 150])
@pytest.mark.parametrize("char", ["x", "ש", "😀"], ids=["ascii", "hebrew", "astral"])
def test_a_grep_page_carries_a_match_and_advances(match_offset, char):
    line = char * GREP_LINE_CLIP            # 1, 6 and 12 escaped chars each
    total = match_offset + 40
    # 20 context lines either side, each a long generated-JSX line: more than
    # any source budget, so this match can only be returned without context.
    context = [char * 5_000] * 20
    matches = [
        {"line_no": match_offset + i + 1, "line": line, "before": context,
         "after": context, "context_start_line": match_offset + i + 1}
        for i in range(40)
    ]
    request = _request(grep_pattern="needle", before=20, after=20,
                       max_matches=200, match_offset=match_offset)
    result = budget_artifact_read(
        _identity("grep", f"{total} match(es)", matches=matches, grep_total_matches=total),
        request,
    )

    assert result.get("error") is None
    assert result["matches"], "a page must carry at least one match"
    # Whatever fits, the page accounts for every match it did not return.
    assert result["grep_truncated"] is (match_offset + len(result["matches"]) < total)
    if result["grep_truncated"]:
        assert result["next_read"]["match_offset"] > match_offset, "continuation must advance"
    # The match text itself is never sacrificed to fit — only its context.
    assert result["matches"][0]["line"] == line
    assert result["matches"][0]["context_omitted"] is True


@pytest.mark.parametrize("identity_chars", [0, 20_000, 60_000, 79_000, 81_000, 82_000,
                                            82_500, 83_000, 83_500, 84_000, 95_000])
def test_a_grep_page_never_reports_more_without_a_way_to_reach_it(identity_chars):
    """However little room identity metadata leaves, no silent dead end."""
    matches = [
        {"line_no": i + 1, "line": "😀" * GREP_LINE_CLIP, "before": [], "after": [],
         "context_start_line": i + 1}
        for i in range(30)
    ]
    observation = _identity("grep", "30 match(es)", matches=matches, grep_total_matches=30)
    # Pad an identity field, not one of SECONDARY_KEYS: optional context is
    # dropped to make room, identity is what squeezes the source budget.
    observation["title"] = "z" * identity_chars
    result = budget_artifact_read(observation, _request(grep_pattern="needle", max_matches=200))

    if result.get("error"):
        # An explicit failure is a legitimate answer; a successful-looking
        # empty page that repeats the same call is not.
        assert not result.get("matches")
        return
    assert result["matches"]
    if result.get("grep_truncated"):
        assert result["next_read"]["match_offset"] >= len(result["matches"])


# ─── the budget is tied to the transport, not to a copy of its number ────────

def test_the_budget_leaves_headroom_under_the_transcript_cap():
    # transcript.to_model_messages cuts a rendered result at MAX_RESULT_CHARS
    # with no regard for JSON structure. Lower that cap and this must follow.
    assert MODEL_READ_MAX_CHARS < MAX_RESULT_CHARS


def test_a_bounded_observation_fits_the_transport():
    result = budget_artifact_read(
        _identity("range", "showing lines 1-4000 of 3000",
                  code=_oversized_source(20_000), start_line=1, end_line=20_000,
                  truncated=True,
                  visualization_profiles=[{"rows": [{"מותג": "א" * 500}] * 40}] * 30),
        _request(offset=1, limit=2000),
    )
    assert len(json.dumps(result, default=str)) < MAX_RESULT_CHARS


@pytest.mark.parametrize("identity_chars", [0, 60_000, 83_000, 85_000])
def test_a_narrow_budget_still_returns_source(identity_chars):
    """Grep needs room for a whole match; a code read is trimmed, not refused."""
    observation = _identity("range", "showing lines 1-4000 of 3000",
                            code=_oversized_source(), start_line=1, end_line=4000,
                            truncated=True)
    observation["title"] = "z" * identity_chars
    result = budget_artifact_read(observation, _request(offset=1, limit=2000))

    if result.get("error"):
        # Only when there is no room left for a usable window at all.
        assert identity_chars >= 84_000
        return
    assert result["code"], "a code read is prefix-trimmed, never refused for width"
    assert _oversized_source().startswith(result["code"])
    assert result["next_read"]["offset"] >= 1
