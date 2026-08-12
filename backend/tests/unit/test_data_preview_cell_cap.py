"""Unit tests for cell-width capping in data previews / stats.

Regression coverage for a real context-window blowout: a `create_data` result of
1 row x 5 cols, where one cell held a ~2 MB JSON payload, produced a single tool
observation of ~1.1M tokens and killed the turn with a provider
context-window error.

The preview already had a 48 KB *row* budget, but two doors bypassed it:
  1. the head loop admits the first row unconditionally, so one wide row escaped
     the budget entirely;
  2. `stats` (df.describe-derived `top`/`min`/`max`) echoed the same raw cell and
     was only sanitized when `allow_llm_see_data` was off.

Pure-Python: no DB, no LLM.
"""
import json

from app.ai.data_preview import (
    DEFAULT_PREVIEW_BUDGET_BYTES,
    MAX_CELL_CHARS,
    build_data_preview,
    clamp_stats,
    gate_stats_for_privacy,
)

HUGE = "x" * 2_000_000


def _sz(obj) -> int:
    return len(json.dumps(obj, default=str, ensure_ascii=False))


def _wide_cell_formatted():
    """The shape that caused the incident: 1 row, 5 cols, one giant cell."""
    return {
        "rows": [{
            "payload_json": HUGE,
            "payload_row_count": 2240,
            "payload_min_month": "2021-01",
            "payload_max_month": "2025-12",
            "payload_description": "full dashboard payload",
        }],
        "columns": [{"field": f, "headerName": f} for f in (
            "payload_json", "payload_row_count", "payload_min_month",
            "payload_max_month", "payload_description",
        )],
        "info": {
            "total_rows": 1,
            "total_columns": 5,
            "column_info": {
                "payload_json": {"dtype": "str", "count": 1, "unique": 1,
                                 "top": HUGE, "freq": 1},
                "payload_row_count": {"dtype": "int64", "count": 1, "mean": 2240.0},
            },
        },
    }


def test_single_wide_row_stays_within_budget():
    """The unconditional first row must not smuggle a 2 MB cell past the budget."""
    preview = build_data_preview(_wide_cell_formatted())
    assert _sz(preview) < DEFAULT_PREVIEW_BUDGET_BYTES
    # The incident payload was ~2.25 MB; anything in that league means no cap.
    assert _sz(preview) < 50_000


def test_wide_cell_is_clipped_but_row_and_columns_survive():
    """Clipping is per-cell: the row, all column names and the count remain."""
    preview = build_data_preview(_wide_cell_formatted())
    row = preview["rows"][0]
    assert set(row) == {
        "payload_json", "payload_row_count", "payload_min_month",
        "payload_max_month", "payload_description",
    }
    assert preview["row_count"] == 1
    assert len(row["payload_json"]) < len(HUGE)
    assert row["payload_json"].startswith("xxx")
    assert "truncated" in row["payload_json"]
    # Narrow cells in the same row are untouched.
    assert row["payload_row_count"] == 2240
    assert row["payload_min_month"] == "2021-01"


def test_clipping_is_disclosed_to_the_model():
    """A clipped cell the planner reads as complete is worse than a small one."""
    preview = build_data_preview(_wide_cell_formatted())
    assert preview["cells_truncated"] is True
    assert "clipped" in (preview.get("note") or "")


def test_stats_do_not_echo_the_raw_cell():
    """describe()'s `top` reproduces a verbatim cell — cap it on the visible path too."""
    info = _wide_cell_formatted()["info"]
    capped = clamp_stats(info)
    assert _sz(capped) < 50_000
    assert len(capped["column_info"]["payload_json"]["top"]) < len(HUGE)
    # Structure and aggregates are preserved.
    assert capped["total_rows"] == 1
    assert capped["column_info"]["payload_row_count"]["mean"] == 2240.0


def test_privacy_path_also_capped():
    """allow_llm_see_data=False still returns stats — they must be capped too."""
    preview = build_data_preview(_wide_cell_formatted(), allow_llm_see_data=False)
    assert preview["data_hidden"] is True
    assert "rows" not in preview
    assert _sz(preview) < 50_000
    # gate_stats_for_privacy drops `top` entirely; capping must not resurrect it.
    assert "top" not in preview["stats"]["column_info"]["payload_json"]


def test_many_wide_columns_cannot_blow_the_budget():
    """200 columns each at the cell cap would exceed the budget via the first row."""
    row = {f"c{i}": HUGE for i in range(200)}
    preview = build_data_preview({
        "rows": [row],
        "columns": [{"field": f"c{i}"} for i in range(200)],
        "info": {"total_rows": 1, "total_columns": 200},
    })
    assert _sz(preview) < DEFAULT_PREVIEW_BUDGET_BYTES * 2
    assert len(preview["rows"][0]) == 200


def test_normal_results_are_unchanged():
    """The common case must not regress: no clipping, no note, all rows kept."""
    formatted = {
        "rows": [{"id": i, "name": f"customer {i}", "revenue": i * 10.5}
                 for i in range(20)],
        "columns": [{"field": f} for f in ("id", "name", "revenue")],
        "info": {"total_rows": 20, "total_columns": 3},
    }
    preview = build_data_preview(formatted)
    assert preview["truncated"] is False
    assert preview["cells_truncated"] is False
    assert preview.get("note") is None
    assert preview["rows"] == formatted["rows"]


def test_cell_cap_boundary():
    """A cell exactly at the cap is kept verbatim; one char over is clipped."""
    at = build_data_preview({
        "rows": [{"v": "y" * MAX_CELL_CHARS}], "columns": [{"field": "v"}],
        "info": {"total_rows": 1},
    })
    assert at["rows"][0]["v"] == "y" * MAX_CELL_CHARS
    assert at["cells_truncated"] is False

    over = build_data_preview({
        "rows": [{"v": "y" * (MAX_CELL_CHARS + 1)}], "columns": [{"field": "v"}],
        "info": {"total_rows": 1},
    })
    assert over["cells_truncated"] is True
    assert len(over["rows"][0]["v"]) < MAX_CELL_CHARS + len("…[truncated 1 chars]") + 1


def test_row_budget_truncation_still_works():
    """Cell capping must not disturb the existing head+tail row truncation."""
    formatted = {
        "rows": [{"id": i, "blob": "z" * 500} for i in range(1000)],
        "columns": [{"field": "id"}, {"field": "blob"}],
        "info": {"total_rows": 1000, "total_columns": 2},
    }
    preview = build_data_preview(formatted)
    assert preview["truncated"] is True
    assert preview["row_count"] == 1000
    assert len(preview["rows"]) < 1000
    assert _sz(preview) < DEFAULT_PREVIEW_BUDGET_BYTES * 2
    # head+tail: first and last source rows both represented
    assert preview["rows"][0]["id"] == 0
    assert preview["rows"][-1]["id"] == 999


def test_query_section_preview_table_clamps_cells():
    """The queries section had the same rows[:5]-but-unbounded-cell shape.

    It renders at ~156k tokens for one wide cell. It does not currently reach
    the planner prompt, but it is built and rendered on every warm refresh.
    """
    from app.ai.context.builders.query_context_builder import _preview_table

    cols = ["payload_json", "payload_row_count"]
    rendered = _preview_table(cols, [{"payload_json": HUGE, "payload_row_count": 2240}])
    assert len(rendered) < 5_000
    assert "truncated" in rendered
    # Header, separator and the narrow cell all survive.
    assert "payload_json | payload_row_count" in rendered
    assert "2240" in rendered


def test_query_section_preview_table_normal_case_unchanged():
    from app.ai.context.builders.query_context_builder import _preview_table

    rows = [{"id": i, "name": f"c{i}"} for i in range(3)]
    rendered = _preview_table(["id", "name"], rows)
    assert "truncated" not in rendered
    for r in rows:
        assert f"{r['id']} | {r['name']}" in rendered


def test_non_string_cells_are_left_alone():
    """Numbers/bools/None must keep their type — the planner reasons on them."""
    preview = build_data_preview({
        "rows": [{"n": 42, "f": 1.5, "b": True, "z": None}],
        "columns": [{"field": f} for f in ("n", "f", "b", "z")],
        "info": {"total_rows": 1},
    })
    row = preview["rows"][0]
    assert row == {"n": 42, "f": 1.5, "b": True, "z": None}
    assert preview["cells_truncated"] is False
