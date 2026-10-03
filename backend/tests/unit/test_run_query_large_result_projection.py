"""A run_query result past the preview cap stays identifiable as a slice.

The serialized tool card is bounded at UI_TOOL_PREVIEW_ROWS and marked
`truncated`, and the frontend hydrates on that flag. That is what made the
PR #1135 review's P1 reachable: a run over the cap refetched the query's
default step — the saved values — on reload, expand and export, while the
header still named the requested params.

The frontend now re-requests its own slice instead (utils/viewerRunHydration
+ tests/unit/viewerRunHydration.mjs). For that to be possible at all, the
bounded projection has to keep saying WHICH values these rows answer, however
big the result was. That is what this pins.

Run:
    cd backend && uv run pytest tests/unit/test_run_query_large_result_projection.py -v
"""
from app.ai.persisted_summary import UI_TOOL_PREVIEW_ROWS, build_tool_context_summary


def _result(n_rows: int, applied: dict | None = None) -> dict:
    rows = [{"region": "DE", "revenue": i} for i in range(n_rows)]
    return {
        "success": True,
        "query_id": "q1",
        "step_id": "s1",
        "title": "Revenue by region",
        "applied_params": applied if applied is not None else {"region": "DE"},
        "cached": False,
        "data": {"rows": rows, "columns": [{"field": "region"}, {"field": "revenue"}]},
        "data_preview": {
            "rows": rows,
            "columns": [{"field": "region"}, {"field": "revenue"}],
            "row_count": n_rows,
        },
        "data_model": {"type": "table"},
    }


def test_result_over_the_cap_is_truncated_and_still_names_its_values():
    n = UI_TOOL_PREVIEW_ROWS + 5
    projection = build_tool_context_summary("run_query", _result(n))

    preview = projection["data_preview"]
    assert len(preview["rows"]) == UI_TOOL_PREVIEW_ROWS, "the cap is real"
    assert preview["truncated"] is True, "…and it is what the frontend hydrates on"
    assert preview["row_count"] == n, "the true size survives for the row label"

    # The reason a truncated card can be refreshed correctly at all.
    assert projection["applied_params"] == {"region": "DE"}
    assert projection["query_id"] == "q1"

    # The full payload never rides along into context.
    assert "data" not in projection


def test_a_small_result_is_not_marked_truncated():
    projection = build_tool_context_summary("run_query", _result(3))
    preview = projection["data_preview"]

    assert len(preview["rows"]) == 3
    assert preview.get("truncated") is not True, (
        "under the cap nothing refetches — which is why the P1 bug stayed "
        "invisible in a 1-row live check"
    )
    assert projection["applied_params"] == {"region": "DE"}


def test_an_all_rows_slice_keeps_its_null_value():
    """`{"region": None}` means 'all regions' — a real answer, not an absent
    param. Dropping it would make a default-values run indistinguishable from
    one whose provenance was lost."""
    projection = build_tool_context_summary(
        "run_query", _result(UI_TOOL_PREVIEW_ROWS + 1, applied={"region": None})
    )
    assert projection["applied_params"] == {"region": None}
    assert projection["data_preview"]["truncated"] is True
