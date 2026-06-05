"""Regression guard for the "metric breakdown collapses to a single line" bug.

History
-------
When a user asked for a metric *broken down by a dimension* (e.g. "GMV by
category", "sales by genre"), ``create_data`` generated SQL that correctly
GROUPed BY the dimension — so the result was granular, with many rows per
x-axis value — yet the chart rendered a single total line.

The breakdown was lost in ``create_data``'s post-execution visualization
inference. The inference LLM is prompted to emit ``group_by`` as a **string**
(see ``implementations/create_data.py`` — the prompt examples and OUTPUT
FORMAT all use ``"group_by": "column_name_or_null"``). That candidate was fed
into ``DataModel(**candidate_json)``, but ``DataModel.group_by`` was typed
``Optional[List[str]]``. Pydantic v2 does not coerce a bare ``str`` into
``List[str]`` — it raised — and the surrounding ``except`` reset the whole
candidate to ``{"type": "table", "series": []}``, discarding ``group_by`` (and
the series) entirely.

Fix
---
``DataModel.group_by`` now accepts ``Optional[Union[str, List[str]]]`` so the
planner's string validates, and ``normalize_group_by`` flattens whatever shape
to the single column name every renderer expects.

This module pins that fixed behavior deterministically — no LLM, no network.
It is the *inner* loop of the feedback cycle in ``REPRO-viz-breakdown.md``; the
*outer* loop is the end-to-end agent repro in
``tests/e2e/test_repro_viz_breakdown.py``.
"""

import pytest

from app.ai.tools.schemas.create_data_model import DataModel, normalize_group_by
from app.ai.tools.implementations.create_data import _extract_json_object


# The literal shape Haiku returned in the end-to-end repro: fenced JSON with a
# trailing prose "Rationale" block. A bare json.loads() throws on this, which is
# what dropped the series + group_by before the schema was ever reached.
LLM_FENCED_WITH_PROSE = (
    "```json\n"
    '{\n  "type": "line_chart",\n'
    '  "series": [{"name": "Total Sales", "key": "InvoiceMonth", "value": "TotalSales"}],\n'
    '  "group_by": "GenreName"\n}\n'
    "```\n\n"
    "**Rationale:**\n\n1. Line chart is correct because the user asked for trends.\n"
    "2. group_by GenreName yields one line per genre."
)


@pytest.mark.parametrize(
    "raw",
    [
        '{"type": "line_chart", "series": [], "group_by": "GenreName"}',  # plain
        '```json\n{"type": "line_chart", "group_by": "GenreName"}\n```',   # fenced
        '```\n{"type": "bar_chart"}\n```',                                  # bare fence
        LLM_FENCED_WITH_PROSE,                                              # fence + prose
        'Here you go:\n{"type": "pie_chart", "series": []}\nHope that helps!',  # prose around
    ],
)
def test_extract_json_object_handles_fences_and_prose(raw):
    obj = _extract_json_object(raw)
    assert isinstance(obj, dict) and obj.get("type")


def test_extract_json_object_recovers_breakdown():
    obj = _extract_json_object(LLM_FENCED_WITH_PROSE)
    assert obj["type"] == "line_chart"
    assert obj["series"] and obj["series"][0]["value"] == "TotalSales"
    assert obj["group_by"] == "GenreName"


def test_extract_json_object_returns_none_on_garbage():
    assert _extract_json_object("no json here") is None
    assert _extract_json_object("") is None
    assert _extract_json_object(None) is None


# A representative candidate exactly as the viz-inference LLM is prompted to
# emit it: a granular line chart with the breakdown dimension as a *string*.
PLANNER_CANDIDATE = {
    "type": "line_chart",
    "series": [{"name": "Sales", "key": "month", "value": "total", "aggregation": "sum"}],
    "group_by": "genre",  # <-- string, per the inference prompt's OUTPUT FORMAT
}


def _build_candidate_like_create_data(candidate_json: dict) -> dict:
    """Mirror the exact construction in
    ``CreateDataTool._infer_visualization_model_traced``.

    Keeping this in lock-step with the implementation is the whole point: if
    the production snippet changes shape, update this helper so the test keeps
    describing real behavior.
    """
    keep = {
        k: v
        for k, v in candidate_json.items()
        if k in {"type", "series", "group_by", "sort", "limit", "filters"}
    }
    try:
        return DataModel(**keep).model_dump()
    except Exception:
        # Pre-fix fallback: a ValidationError here dropped series AND group_by.
        return {"type": "table", "series": []}


def test_datamodel_accepts_planner_group_by_string():
    """The planner's single-column string must validate (it used to raise)."""
    dm = DataModel(
        type="line_chart",
        series=[{"name": "Sales", "key": "month", "value": "total"}],
        group_by="genre",
    )
    assert dm.group_by == "genre"


def test_datamodel_accepts_group_by_list():
    """A list (as other tools emit) still validates."""
    dm = DataModel(
        type="line_chart",
        series=[{"name": "Sales", "key": "month", "value": "total"}],
        group_by=["genre"],
    )
    assert dm.group_by == ["genre"]


def test_create_data_construction_preserves_breakdown():
    """The construction the tool performs must keep the chart type, the
    series, and the breakdown dimension — i.e. one line per category."""
    built = _build_candidate_like_create_data(PLANNER_CANDIDATE)
    assert built.get("type") == "line_chart"
    assert built.get("series"), "series must be preserved"
    gb = built.get("group_by")
    assert gb, "group_by must survive"
    assert normalize_group_by(gb) == "genre"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("genre", "genre"),
        (["genre"], "genre"),
        (["genre", "country"], "genre"),  # charts render a single dimension
        ([], None),
        ("", None),
        (None, None),
        (123, None),
    ],
)
def test_normalize_group_by(value, expected):
    assert normalize_group_by(value) == expected
