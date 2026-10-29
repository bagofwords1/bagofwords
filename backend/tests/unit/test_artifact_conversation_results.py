"""The results an artifact conversation shows its viewer (``bow.conversations``).

A ``create_data`` result reaches the app as a capped table plus, only when it
validates against the real columns, a chart suggestion. These tests drive the
pure surface of ``services/artifact_conversation_results.py``:
``shape_result`` (caps, typing, value fidelity, redaction, chart validation,
the per-result byte budget), ``result_line`` (the wire bytes) and
``fit_message`` (the per-message byte budget).
"""
from __future__ import annotations

import json
import logging
import math
import re

import pytest

from app.services.artifact_conversation_results import (
    MAX_CELL_CHARS,
    MAX_COLUMNS,
    MAX_MESSAGE_RESULT_BYTES,
    MAX_RESULT_BYTES,
    MAX_HEADER_CHARS,
    MAX_ROWS,
    budget_lost,
    fit_message,
    result_line,
    shape_result,
)


def _data(columns, rows, *, dtypes=None, total_rows=None):
    """A Step.data grid in the shape create_data stores (format_df_for_widget)."""
    info = {"total_rows": len(rows) if total_rows is None else total_rows, "total_columns": len(columns),
            "column_info": {c: {"dtype": d} for c, d in (dtypes or {}).items()}}
    return {"rows": rows, "columns": [{"headerName": c, "field": c} for c in columns], "info": info}


def _v2(view):
    return {"version": "v2", "view": view}


def _shape(data, view=None, **kw):
    return shape_result(step_id="step-1", block_id="block-1", title="Revenue", data=data, view=view, **kw)


def _types(result):
    return {c["field"]: c["type"] for c in result["columns"]}


MONTHS = ["2024-01", "2024-02", "2024-03", "2024-04"]
BY_MONTH = _data(["month", "revenue"], [{"month": m, "revenue": r} for m, r in zip(MONTHS, [10.5, 12, 7.25, 30])],
                 dtypes={"month": "object", "revenue": "float64"})
BAR = _v2({"type": "bar_chart", "x": "month", "y": "revenue"})


# ── the table ───────────────────────────────────────────────────────────

def test_a_result_carries_the_table_and_its_identity():
    r = _shape(BY_MONTH, BAR)
    assert (r["id"], r["block"], r["title"]) == ("step-1", "block-1", "Revenue")
    assert r["columns"] == [{"field": "month", "headerName": "month", "type": "string"},
                            {"field": "revenue", "headerName": "revenue", "type": "number"}]
    assert r["rows"] == [{"month": "2024-01", "revenue": 10.5}, {"month": "2024-02", "revenue": 12},
                         {"month": "2024-03", "revenue": 7.25}, {"month": "2024-04", "revenue": 30}]
    assert (r["row_count"], r["column_count"], r["truncated"]) == (4, 2, False)


def test_a_zero_row_result_keeps_its_columns_and_has_no_chart():
    r = _shape(_data(["month", "revenue"], [], dtypes={"revenue": "float64"}), BAR)
    assert r["rows"] == [] and r["row_count"] == 0 and r["truncated"] is False
    assert [c["field"] for c in r["columns"]] == ["month", "revenue"]
    assert r["chart"] is None


def test_rows_and_columns_are_capped_and_the_totals_reported():
    cols = [f"c{i}" for i in range(35)]
    rows = [{c: i * 100 + j for j, c in enumerate(cols)} for i in range(250)]
    r = _shape(_data(cols, rows, dtypes={c: "int64" for c in cols}), _v2({"type": "bar_chart", "x": "c0", "y": "c1"}))
    assert len(r["rows"]) == MAX_ROWS == 200 and len(r["columns"]) == MAX_COLUMNS == 30
    assert (r["row_count"], r["column_count"], r["truncated"]) == (250, 35, True)
    assert all(set(row) == set(cols[:30]) for row in r["rows"])
    assert r["rows"][199]["c29"] == 199 * 100 + 29
    assert r["chart"] is None  # a partial chart misleads


def test_the_stored_total_counts_rows_the_step_did_not_keep():
    # The org row limit stored 1000 of 5000 rows.
    rows = [{"n": i} for i in range(1000)]
    r = _shape(_data(["n"], rows, dtypes={"n": "int64"}, total_rows=5000))
    assert r["row_count"] == 5000 and len(r["rows"]) == 200 and r["truncated"] is True


def test_rows_are_projected_to_the_kept_fields():
    data = {"columns": [{"field": "a", "headerName": "A"}, {"field": None, "headerName": "b"}],
            "rows": [{"a": 1, "b": 2, "extra": 3}, "not a row", {"a": 4}], "info": {}}
    r = _shape(data)
    assert [(c["field"], c["headerName"]) for c in r["columns"]] == [("a", "A"), ("b", "b")]
    assert r["rows"] == [{"a": 1, "b": 2}, {"a": 4, "b": None}]
    assert r["row_count"] == 3  # no stored total: the stored rows


# ── values and types (D-5: no conversion) ──────────────────────────────

def test_numeric_object_columns_are_numbers_with_their_values_unchanged():
    # Postgres numeric: dtype object, stored as JSON numbers by pandas.
    rows = [{"total": 12.5}, {"total": 1.23456789e+19}, {"total": None}, {"total": 3}]
    r = _shape(_data(["total"], rows, dtypes={"total": "object"}))
    assert _types(r) == {"total": "number"}
    assert [x["total"] for x in r["rows"]] == [12.5, 1.23456789e+19, None, 3]


def test_an_int_beyond_safe_javascript_range_turns_its_column_into_strings():
    rows = [{"id": 7}, {"id": 9007199254740993}, {"id": None}]
    r = _shape(_data(["id"], rows, dtypes={"id": "int64"}))
    assert _types(r) == {"id": "string"}
    assert [x["id"] for x in r["rows"]] == ["7", "9007199254740993", None]


def test_the_largest_safe_int_stays_a_number():
    r = _shape(_data(["id"], [{"id": 2 ** 53 - 1}, {"id": -(2 ** 53 - 1)}], dtypes={"id": "int64"}))
    assert _types(r) == {"id": "number"} and [x["id"] for x in r["rows"]] == [2 ** 53 - 1, -(2 ** 53 - 1)]


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_a_non_finite_float_becomes_null_and_the_line_is_strict_json(bad):
    r = _shape(_data(["x", "tags"], [{"x": bad, "tags": [1.0, bad]}, {"x": 2.0, "tags": []}],
                     dtypes={"x": "float64"}))
    assert r["rows"][0] == {"x": None, "tags": [1.0, None]}
    parsed = json.loads(result_line(r), parse_constant=lambda c: pytest.fail(f"non-strict JSON {c}"))
    assert parsed["rows"][0]["x"] is None


@pytest.mark.parametrize("values", [
    ["02134", "007", "0044 20 7946 0958", "12345678901234567890"],
    ["1", "2", "3"],
])
def test_digit_strings_stay_strings_byte_for_byte(values):
    r = _shape(_data(["code"], [{"code": v} for v in values], dtypes={"code": "object"}))
    assert _types(r) == {"code": "string"}
    assert [x["code"] for x in r["rows"]] == values


def test_a_column_mixing_numbers_and_numeric_strings_is_a_string_column_unchanged():
    values = [1, "2", 3.5, "04"]
    r = _shape(_data(["v"], [{"v": v} for v in values], dtypes={"v": "object"}))
    assert _types(r) == {"v": "string"} and [x["v"] for x in r["rows"]] == values


def test_types_follow_the_stored_dtype_or_the_values():
    rows = [{"flag": True, "when": "2024-01-31T00:00:00.000", "d": "2024-02-01", "n": 1, "empty": None, "b": False},
            {"flag": False, "when": "2024-02-29T10:30:00.000Z", "d": "2024-03-01", "n": 2.5, "empty": None, "b": True}]
    r = _shape(_data(["flag", "when", "d", "n", "empty", "b"], rows,
                     dtypes={"flag": "bool", "when": "datetime64[ns]", "n": "object"}))
    assert _types(r) == {"flag": "boolean", "when": "datetime", "d": "datetime", "n": "number",
                         "empty": "string", "b": "boolean"}
    # Booleans are never numbers.
    assert _types(_shape(_data(["x"], [{"x": True}, {"x": 1}]))) == {"x": "string"}


def test_a_datetime_column_holding_a_non_date_string_is_a_string_column():
    def redact(s):
        return "[MASKED]" if s.startswith("2024-02") else s

    rows = [{"when": "2024-01-31T00:00:00"}, {"when": "2024-02-29T00:00:00"}]
    r = _shape(_data(["when"], rows, dtypes={"when": "datetime64[ns]"}), redact=redact)
    assert _types(r) == {"when": "string"} and r["rows"][1]["when"] == "[MASKED]"


def test_a_long_text_cell_is_cut_and_the_line_stays_within_budget():
    r = _shape(_data(["note", "n"], [{"note": "x" * 300_000, "n": 1}], dtypes={"n": "int64"}))
    assert r["rows"][0]["note"] == "x" * MAX_CELL_CHARS + "…"
    assert len(result_line(r)) <= MAX_RESULT_BYTES


def test_a_cut_cell_marks_the_result_truncated_and_drops_the_chart():
    label = "y" * (MAX_CELL_CHARS + 50)
    data = _data(["label", "n"], [{"label": label, "n": 1}, {"label": "short", "n": 2}], dtypes={"n": "int64"})
    r = _shape(data, _v2({"type": "bar_chart", "x": "label", "y": "n"}))
    assert r["rows"][0]["label"] == "y" * MAX_CELL_CHARS + "…"
    assert r["truncated"] is True and r["chart"] is None  # a chart of cut labels misleads
    # Only the title being long is not a cut of the data.
    whole = shape_result(step_id="s", block_id=None, title="t" * 5000, data=BY_MONTH, view=BAR)
    assert whole["truncated"] is False and whole["chart"] is not None


def test_long_header_names_are_cut_so_a_zero_row_result_always_fits():
    cols = [f"c{i}" for i in range(MAX_COLUMNS)]
    data = {"columns": [{"field": c, "headerName": c + " " + "h" * 20_000} for c in cols],
            "rows": [{c: "v" for c in cols}], "info": {"total_rows": 1}}
    r = _shape(data, redact=lambda s: s)
    assert len(result_line(r)) <= MAX_RESULT_BYTES
    assert all(len(c["headerName"]) <= MAX_HEADER_CHARS + 1 for c in r["columns"])
    assert r["columns"][3]["headerName"].startswith("c3 hhh") and r["rows"] == [{c: "v" for c in cols}]


def test_a_grid_that_cannot_fit_even_without_rows_is_refused():
    # Row keys (fields) are never cut; when they alone exceed the budget the
    # result cannot be shaped, and the caller skips it (and says so).
    cols = [f"c{i}" + "f" * 9000 for i in range(MAX_COLUMNS)]
    data = {"columns": [{"field": c, "headerName": "h"} for c in cols], "rows": [], "info": {"total_rows": 0}}
    with pytest.raises(ValueError):
        _shape(data)
    with pytest.raises(ValueError):
        _shape(BY_MONTH, BAR, max_bytes=40)


def test_a_long_title_is_cut():
    r = shape_result(step_id="s", block_id=None, title="t" * 5000, data=BY_MONTH, view=None)
    assert r["title"] == "t" * MAX_CELL_CHARS + "…" and r["block"] is None


# ── the per-result byte budget ─────────────────────────────────────────

def test_wide_text_rows_are_trimmed_to_the_result_budget():
    rows = [{"a": f"{i:04d}" + "a" * 896, "b": f"{i:04d}" + "b" * 896, "n": i} for i in range(200)]
    data = _data(["a", "b", "n"], rows, dtypes={"n": "int64"})
    r = _shape(data, _v2({"type": "bar_chart", "x": "a", "y": "n"}))
    line = result_line(r)
    assert len(line) <= MAX_RESULT_BYTES == 262144
    assert 0 < len(r["rows"]) < 200 and r["truncated"] is True and r["chart"] is None
    assert r["row_count"] == 200
    # The rows kept are the first ones, untouched.
    assert r["rows"] == rows[:len(r["rows"])]


def test_a_smaller_budget_trims_further():
    r = _shape(BY_MONTH, BAR, max_bytes=len(result_line(_shape(BY_MONTH, BAR))) - 1)
    assert r["chart"] is None
    assert len(result_line(r)) <= len(result_line(_shape(BY_MONTH, BAR))) - 1


# ── redaction (injected display redactor) ──────────────────────────────

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def _stub_redact(s: str) -> str:
    return EMAIL.sub("[EMAIL]", s).replace("9007199254740993", "[ID]")


def test_redaction_covers_strings_made_from_large_ints_nested_cells_headers_and_title():
    data = {"columns": [{"field": "id", "headerName": "id"}, {"field": "contact", "headerName": "Owner a@b.io"},
                        {"field": "tags", "headerName": "tags"}, {"field": "n", "headerName": "n"}],
            "rows": [{"id": 9007199254740993, "contact": "write to ann@example.com", "tags": ["x", {"k": "bo@x.org"}],
                      "n": 5},
                     {"id": 12, "contact": "nobody", "tags": [], "n": 6}],
            "info": {"total_rows": 2, "column_info": {"id": {"dtype": "int64"}, "n": {"dtype": "int64"}}}}
    r = shape_result(step_id="s", block_id="b", title="Report for cy@z.net", data=data, view=None,
                     redact=_stub_redact)
    assert r["rows"][0] == {"id": "[ID]", "contact": "write to [EMAIL]", "tags": ["x", {"k": "[EMAIL]"}], "n": 5}
    assert r["rows"][1]["id"] == "12"
    assert _types(r)["id"] == "string"
    assert [c["field"] for c in r["columns"]] == ["id", "contact", "tags", "n"]  # row keys, never masked
    assert r["columns"][1]["headerName"] == "Owner [EMAIL]" and r["title"] == "Report for [EMAIL]"
    body = result_line(r).decode()
    assert "9007199254740993" not in body and "@" not in body


def test_redaction_runs_before_the_cut_so_no_raw_prefix_survives():
    cell = "x" * (MAX_CELL_CHARS - 5) + "ann@example.com"
    r = _shape(_data(["c"], [{"c": cell}]), redact=_stub_redact)
    assert "ann@" not in r["rows"][0]["c"] and r["rows"][0]["c"].endswith("[EMAIL]")


def test_without_a_redactor_values_are_untouched():
    r = _shape(_data(["c"], [{"c": "ann@example.com"}]))
    assert r["rows"][0]["c"] == "ann@example.com"


# ── the chart suggestion (validated, else omitted) ─────────────────────

def test_a_valid_bar_view_suggests_a_bar_chart():
    assert _shape(BY_MONTH, BAR)["chart"] == {"type": "bar", "x": "month", "y": ["revenue"], "series": None}


def test_line_area_and_multi_series_views():
    data = _data(["month", "a", "b"], [{"month": m, "a": i, "b": i * 2} for i, m in enumerate(MONTHS)],
                 dtypes={"a": "int64", "b": "int64"})
    assert _shape(data, _v2({"type": "line_chart", "x": "month", "y": ["a", "b"]}))["chart"] == \
        {"type": "line", "x": "month", "y": ["a", "b"], "series": None}
    assert _shape(data, _v2({"type": "area_chart", "x": "month", "y": "a"}))["chart"]["type"] == "area"


def test_a_line_view_grouped_by_a_column_with_unique_pairs_carries_the_series():
    rows = [{"month": m, "genre": g, "sales": i} for i, (m, g) in enumerate(
        [(m, g) for m in MONTHS[:2] for g in ("Rock", "Jazz")])]
    data = _data(["month", "genre", "sales"], rows, dtypes={"sales": "int64"})
    chart = _shape(data, _v2({"type": "line_chart", "x": "month", "y": "sales", "groupBy": "genre"}))["chart"]
    assert chart == {"type": "line", "x": "month", "y": ["sales"], "series": "genre"}
    # A series with two value columns is not drawable without guessing.
    data2 = _data(["month", "genre", "sales", "units"], [{**r, "units": 1} for r in rows],
                  dtypes={"sales": "int64", "units": "int64"})
    assert _shape(data2, _v2({"type": "line_chart", "x": "month", "y": ["sales", "units"],
                               "groupBy": "genre"}))["chart"] is None


def test_pie_and_scatter_views():
    pie = _data(["genre", "sales"], [{"genre": g, "sales": s} for g, s in [("Rock", 5), ("Jazz", 3)]],
                dtypes={"sales": "int64"})
    assert _shape(pie, _v2({"type": "pie_chart", "category": "genre", "value": "sales"}))["chart"] == \
        {"type": "pie", "x": "genre", "y": ["sales"], "series": None}
    pts = _data(["w", "h"], [{"w": 1, "h": 2}, {"w": 1, "h": 3}], dtypes={"w": "int64", "h": "float64"})
    assert _shape(pts, _v2({"type": "scatter_plot", "x": "w", "y": "h"}))["chart"] == \
        {"type": "scatter", "x": "w", "y": ["h"], "series": None}  # duplicate x is fine for points
    assert _shape(pie, _v2({"type": "scatter_plot", "x": "genre", "y": "sales"}))["chart"] is None


@pytest.mark.parametrize("view", [
    None,
    {"type": "table"},
    {"chartType": "bar", "x": "month"},  # legacy
    _v2({"type": "table"}),
    _v2({"type": "count", "value": "revenue"}),
    _v2({"type": "metric_card", "value": "revenue"}),
    _v2({"type": "heatmap", "x": "month", "y": "month", "value": "revenue"}),
])
def test_views_that_are_not_charts_suggest_none(view):
    r = _shape(BY_MONTH, view)
    assert r["chart"] is None and len(r["rows"]) == 4


@pytest.mark.parametrize("view", [
    _v2({"type": "bar_chart", "x": "revenue", "y": "month"}),         # y on a string column
    _v2({"type": "bar_chart", "x": "missing", "y": "revenue"}),       # x not a column
    _v2({"type": "bar_chart", "x": "month", "y": ["revenue", "nope"]}),
    _v2({"type": "bar_chart", "x": "month", "y": "month"}),            # x is a value axis too
    _v2({"type": "bar_chart", "x": "month", "y": "revenue", "groupBy": "month"}),
    _v2({"type": "bar_chart", "x": "month", "y": "revenue", "groupBy": "nope"}),
    _v2({"type": "pie_chart", "category": "month", "value": "revenue", "aggregation": "sum"}),
    _v2({"type": "bar_chart", "x": "month", "y": "revenue",
         "seriesStyles": [{"key": "revenue", "aggregation": "sum"}]}),
    _v2({"type": "line_chart", "x": "month", "y": "revenue",
         "defaultFilters": [{"column": "month", "operator": "=", "value": "2024-01"}]}),
])
def test_an_invalid_chart_is_omitted_and_the_table_kept(view):
    r = _shape(BY_MONTH, view)
    assert r["chart"] is None and r["rows"] == _shape(BY_MONTH, None)["rows"]


@pytest.mark.parametrize("view", [{"type": ["bar"]}, {"version": "v2", "type": 5}])
def test_a_view_that_cannot_be_read_keeps_the_table_without_a_chart(view):
    r = _shape(BY_MONTH, view)
    assert r["chart"] is None and r["rows"] == _shape(BY_MONTH, None)["rows"] and r["truncated"] is False


def test_a_skipped_chart_is_logged_with_its_step(caplog, monkeypatch):
    name = "app.services.artifact_conversation_results"
    monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.WARNING, logger=name)
    shape_result(step_id="step-7f3a", block_id="b", title="t", data=BY_MONTH, view={"type": ["bar"]})
    logged = [r for r in caplog.records if r.name == name and r.exc_info]
    assert len(logged) == 1 and "step-7f3a" in logged[0].getMessage()


def test_a_failed_title_redaction_is_logged_with_its_org_and_tool_execution(caplog, monkeypatch):
    from app.services.artifact_conversation_results import Redaction

    def boom(_text):
        raise RuntimeError("bad rule")

    name = "app.services.artifact_conversation_results"
    monkeypatch.setattr(logging.getLogger(name), "disabled", False)
    caplog.set_level(logging.WARNING, logger=name)
    assert Redaction(boom, True, organization_id="org-91c2").title("Ask ann", tool_execution_id="te-5d0e") is None
    logged = [r for r in caplog.records if r.name == name and r.exc_info]
    assert len(logged) == 1 and "org-91c2" in logged[0].getMessage() and "te-5d0e" in logged[0].getMessage()


def test_interval_and_categorical_dtypes_are_not_numbers():
    rows = [{"bucket": "(0, 10]", "tier": "gold", "n": 3}, {"bucket": "(10, 20]", "tier": "silver", "n": 4}]
    r = _shape(_data(["bucket", "tier", "n"], rows,
                     dtypes={"bucket": "interval[int64, right]", "tier": "category", "n": "Int64"}))
    assert _types(r) == {"bucket": "string", "tier": "string", "n": "number"}
    for dtype in ("int8", "uint16", "float32", "Float64", "UInt64"):
        assert _types(_shape(_data(["v"], [{"v": 1}], dtypes={"v": dtype}))) == {"v": "number"}


def test_a_series_style_with_only_a_color_keeps_the_chart():
    view = _v2({"type": "bar_chart", "x": "month", "y": "revenue", "seriesStyles": [{"key": "revenue", "color": "#f00"}]})
    assert _shape(BY_MONTH, view)["chart"]["type"] == "bar"


def test_duplicate_categories_would_need_aggregation_so_no_chart():
    data = _data(["month", "revenue"], [{"month": "2024-01", "revenue": 1}, {"month": "2024-01", "revenue": 2}],
                 dtypes={"revenue": "int64"})
    assert _shape(data, BAR)["chart"] is None
    assert _shape(data, _v2({"type": "pie_chart", "category": "month", "value": "revenue"}))["chart"] is None


def test_a_value_column_cut_by_the_column_cap_has_no_chart():
    cols = [f"c{i}" for i in range(31)]
    data = _data(cols, [{c: 1 for c in cols}], dtypes={c: "int64" for c in cols})
    data["info"]["total_rows"] = 1
    assert _shape(data, _v2({"type": "bar_chart", "x": "c0", "y": "c30"}))["chart"] is None


# ── the wire line ──────────────────────────────────────────────────────

def test_the_result_line_is_one_ascii_json_line_typed_result():
    r = _shape(_data(["name"], [{"name": "Zoë — 東京"}]))
    line = result_line(r)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert all(b < 128 for b in line)  # bytes == the broker's UTF-16 code units
    assert json.loads(line) == {"type": "result", **r}


# ── the per-message byte budget ────────────────────────────────────────

def _big(step_id, n_rows=200):
    rows = [{"t": f"{i:05d}" + "q" * 1495, "u": f"{i:05d}" + "r" * 1495} for i in range(n_rows)]
    return shape_result(step_id=step_id, block_id=f"b-{step_id}", title=step_id,
                        data=_data(["t", "u"], rows), view=None)


def test_large_results_share_the_message_budget():
    # Each result is capped at MAX_RESULT_BYTES, so two always fit 2x that;
    # the third is what the message budget must trim or drop.
    results = [_big("one"), _big("two"), _big("three")]
    assert all(200_000 < len(result_line(r)) <= MAX_RESULT_BYTES for r in results)
    kept, left = fit_message(results, MAX_MESSAGE_RESULT_BYTES)
    total = sum(len(result_line(k)) for k in kept)
    assert total <= MAX_MESSAGE_RESULT_BYTES == 524288
    assert left == MAX_MESSAGE_RESULT_BYTES - total
    assert kept[:2] == results[:2]  # order applied: the first ones are untouched
    if len(kept) == 3:
        assert kept[2]["id"] == "three" and kept[2]["truncated"] is True and kept[2]["chart"] is None
        assert len(kept[2]["rows"]) < len(results[2]["rows"])
    # A running budget already partly spent (the stream's case) trims the second.
    kept, _ = fit_message(results[:2], MAX_MESSAGE_RESULT_BYTES - 100_000)
    assert sum(len(result_line(k)) for k in kept) <= MAX_MESSAGE_RESULT_BYTES - 100_000
    assert kept[0] == results[0] and kept[1]["truncated"] is True and kept[1]["chart"] is None


def test_results_that_fit_pass_unchanged_and_one_that_cannot_fit_is_dropped():
    small = _shape(BY_MONTH, BAR)
    kept, left = fit_message([small], 10_000)
    assert kept == [small] and left == 10_000 - len(result_line(small))
    kept, left = fit_message([small], 20)  # not even the zero-row form fits
    assert kept == [] and left == 20
    # Re-trimmed to the remainder: no chart first, then fewer rows.
    budget = len(result_line(small)) - 5
    kept, _ = fit_message([small], budget)
    assert len(kept) == 1 and kept[0]["chart"] is None and len(result_line(kept[0])) <= budget
    # Dropping the chart was enough: the table is whole, so it is not truncated.
    assert kept[0]["rows"] == small["rows"] and kept[0]["truncated"] is False
    budget = len(result_line({**small, "chart": None})) - 5
    kept, _ = fit_message([small], budget)
    assert len(kept) == 1 and len(kept[0]["rows"]) < len(small["rows"]) and kept[0]["truncated"] is True
    assert len(result_line(kept[0])) <= budget


def test_budget_lost_names_a_dropped_result_or_one_cut_to_no_rows():
    small = _shape(BY_MONTH, BAR)
    other = {**small, "id": "step-2"}
    empty = _shape(_data(["month", "revenue"], []), None)
    assert budget_lost([small, other], [small, other]) is False
    assert budget_lost([small, other], [small]) is True                      # dropped
    assert budget_lost([small], [{**small, "rows": [], "truncated": True}]) is True  # cut to nothing
    assert budget_lost([small], [{**small, "rows": small["rows"][:1], "truncated": True}]) is False
    assert budget_lost([empty], [empty]) is False                            # it had no rows to lose
