"""Fast, key-free root-cause check for the "metric breakdown collapses to a
single line" bug.

Context
-------
When a user asks for a metric *broken down by a dimension* (e.g. "GMV by
category", "sales by genre"), ``create_data`` generates SQL that correctly
GROUPs BY the dimension, so the result is granular: many rows per x-axis
value (one per category). The chart, however, renders a single total line.

The breakdown is lost inside ``create_data``'s post-execution visualization
inference. The inference LLM is prompted to emit ``group_by`` as a **string**
(see ``implementations/create_data.py`` — the prompt examples and OUTPUT
FORMAT all use ``"group_by": "column_name_or_null"``). That candidate is then
fed straight into ``DataModel(**candidate_json)``, but ``DataModel.group_by``
is typed ``Optional[List[str]]``. Pydantic v2 does not coerce a bare ``str``
into ``List[str]`` — it raises — and the surrounding ``except`` resets the
whole candidate to ``{"type": "table", "series": []}``, throwing away
``group_by`` (and the series) entirely.

This module pins that mechanism deterministically, with no LLM and no
network. It is the *inner* loop of the feedback cycle documented in
``REPRO-viz-breakdown.md``; the *outer* loop is the end-to-end agent repro in
``tests/e2e/test_repro_viz_breakdown.py``.

When the fix lands, ``test_planner_string_group_by_survives`` flips from
xfail to pass (strict=True will then flag the stale marker for removal).
"""

import pytest
from pydantic import ValidationError

from app.ai.tools.schemas.create_data_model import DataModel


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
        # The production code swallows the ValidationError and falls back to a
        # bare table — dropping series AND group_by.
        return {"type": "table", "series": []}


def test_datamodel_rejects_string_group_by():
    """Schema-level proof: a string group_by (what the planner emits) is
    rejected by the List[str] field."""
    with pytest.raises(ValidationError):
        DataModel(
            type="line_chart",
            series=[{"name": "Sales", "key": "month", "value": "total"}],
            group_by="genre",
        )
    # A list, by contrast, validates fine.
    dm = DataModel(
        type="line_chart",
        series=[{"name": "Sales", "key": "month", "value": "total"}],
        group_by=["genre"],
    )
    assert dm.group_by == ["genre"]


def test_create_data_construction_drops_the_breakdown():
    """End-to-end of the inline snippet: the planner's string group_by causes
    the entire candidate to collapse to a table, so the breakdown is lost."""
    built = _build_candidate_like_create_data(PLANNER_CANDIDATE)
    # Bug signature: type downgraded to table, series + group_by gone.
    assert built.get("group_by") in (None, [], "")
    assert not built.get("series")
    assert built.get("type") == "table"


@pytest.mark.xfail(
    strict=True,
    reason="BUG: planner emits group_by as a string, which DataModel "
    "(List[str]) rejects; the breakdown is dropped. Remove this marker when "
    "create_data normalizes group_by before/while building the DataModel.",
)
def test_planner_string_group_by_survives():
    """Desired behavior (currently failing): the dimension the planner chose
    must survive into the final data model so the chart can render one line
    per category."""
    built = _build_candidate_like_create_data(PLANNER_CANDIDATE)
    assert built.get("type") == "line_chart"
    assert built.get("series"), "series should be preserved"
    gb = built.get("group_by")
    assert gb, "group_by must survive"
    # Normalized to the column name regardless of str/list representation.
    flat = gb[0] if isinstance(gb, list) else gb
    assert flat == "genre"
