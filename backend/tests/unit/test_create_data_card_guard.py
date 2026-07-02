"""Unit tests for the single-value-card guard in create_data.

Covers the production failure class: a count/metric_card over a melted
``Metric | Value`` display table (label column + shared value column, values
pre-formatted as strings) must never render an arbitrary cell — the guard
repairs what it can deterministically and demotes to table otherwise.
"""
import pytest

from app.ai.tools.implementations.create_data import (
    ensure_single_value_card_renderable,
    finalize_inferred_data_model,
    _parse_numeric_like,
    _pick_value_column,
)


def _formatted(columns, rows, column_info=None):
    return {
        "columns": [{"headerName": c, "field": c} for c in columns],
        "rows": rows,
        "info": {
            "total_rows": len(rows),
            "column_info": column_info or {c: {"dtype": "object"} for c in columns},
        },
    }


MELTED = _formatted(
    ["מדד", "ערך"],
    [
        {"מדד": "תאריך", "ערך": "2025"},
        {"מדד": "מכר", "ערך": "₪4,125.04"},
        {"מדד": "כמות", "ערך": "442"},
        {"מדד": "מספר חשבוניות", "ערך": "80"},
    ],
)


class TestParseNumericLike:
    def test_formatted_currency(self):
        assert _parse_numeric_like("₪29,134,139") == 29134139.0
        assert _parse_numeric_like("$1,234.56") == 1234.56
        assert _parse_numeric_like("4,125.04") == 4125.04

    def test_plain_numbers(self):
        assert _parse_numeric_like(42) == 42.0
        assert _parse_numeric_like(4.5) == 4.5
        assert _parse_numeric_like("80") == 80.0
        assert _parse_numeric_like("-12.5") == -12.5
        assert _parse_numeric_like("45.2%") == 45.2

    def test_rejects_non_numbers(self):
        assert _parse_numeric_like("תאריך") is None
        assert _parse_numeric_like("23.06.2026") is None
        assert _parse_numeric_like("2025-12-22 00:00:00") is None
        assert _parse_numeric_like(None) is None
        assert _parse_numeric_like(True) is None
        assert _parse_numeric_like("") is None
        assert _parse_numeric_like("1.2.3") is None


class TestPickValueColumn:
    def test_single_column(self):
        assert _pick_value_column([{"total": "x"}], ["total"]) == "total"

    def test_single_numeric_like_column(self):
        rows = MELTED["rows"]
        assert _pick_value_column(rows, ["מדד", "ערך"]) == "ערך"

    def test_multi_numeric_single_row_prefers_non_time(self):
        rows = [{"year": 2025, "revenue": 100.0}]
        assert _pick_value_column(rows, ["year", "revenue"]) == "revenue"

    def test_multi_numeric_multi_row_is_ambiguous(self):
        rows = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        assert _pick_value_column(rows, ["a", "b"]) is None


class TestCardGuard:
    def test_melted_with_empty_series_demotes_to_table(self):
        # The exact reproduced production state: inference returned nothing.
        dm = finalize_inferred_data_model("metric_card", {"type": "table", "series": []})
        out = ensure_single_value_card_renderable(dm, MELTED)
        assert out["type"] == "table"

    def test_melted_with_matching_series_name_stays_card_with_filter(self):
        dm = {
            "type": "metric_card",
            "series": [{"name": "מכר", "value": "ערך"}],
        }
        out = ensure_single_value_card_renderable(dm, MELTED)
        assert out["type"] == "metric_card"
        assert out["filters"] == [{"column": "מדד", "operator": "equals", "value": "מכר"}]

    def test_melted_with_valid_filter_stays_card(self):
        dm = {
            "type": "count",
            "series": [{"name": "Sales", "value": "ערך"}],
            "filters": [{"column": "מדד", "operator": "equals", "value": "מכר"}],
        }
        out = ensure_single_value_card_renderable(dm, MELTED)
        assert out["type"] == "count"
        assert out["filters"] == [{"column": "מדד", "operator": "equals", "value": "מכר"}]

    def test_filter_matching_zero_rows_is_dropped_and_demotes(self):
        dm = {
            "type": "metric_card",
            "series": [{"name": "לא קיים", "value": "ערך"}],
            "filters": [{"column": "מדד", "operator": "equals", "value": "לא קיים"}],
        }
        out = ensure_single_value_card_renderable(dm, MELTED)
        assert out["type"] == "table"
        assert not out.get("filters")

    def test_single_row_wide_numeric_stays_card(self):
        formatted = _formatted(
            ["TotalSales", "TotalQuantity"],
            [{"TotalSales": 4125.04, "TotalQuantity": 442}],
            {"TotalSales": {"dtype": "float64"}, "TotalQuantity": {"dtype": "int64"}},
        )
        dm = {"type": "metric_card", "series": [{"name": "Total Sales", "value": "TotalSales"}]}
        out = ensure_single_value_card_renderable(dm, formatted)
        assert out["type"] == "metric_card"
        assert out["series"][0]["value"] == "TotalSales"

    def test_hallucinated_value_column_repaired_on_single_row(self):
        formatted = _formatted(
            ["Label", "Revenue"],
            [{"Label": "Total", "Revenue": 99.5}],
        )
        dm = {"type": "metric_card", "series": [{"name": "Revenue", "value": "revnue_typo"}]}
        out = ensure_single_value_card_renderable(dm, formatted)
        assert out["type"] == "metric_card"
        assert out["series"][0]["value"] == "Revenue"

    def test_aggregation_over_numeric_entity_table_stays_card(self):
        formatted = _formatted(
            ["customer", "revenue"],
            [{"customer": "Acme", "revenue": 100.0}, {"customer": "Bob", "revenue": 50.0}],
        )
        dm = {
            "type": "metric_card",
            "series": [{"name": "Revenue", "value": "revenue", "aggregation": "sum"}],
        }
        out = ensure_single_value_card_renderable(dm, formatted)
        assert out["type"] == "metric_card"

    def test_aggregation_over_date_mixed_column_demotes(self):
        # Melted table whose value column mixes a date string with metrics —
        # summing them would be garbage; the spurious aggregation must not
        # keep the card alive.
        formatted = _formatted(
            ["מדד", "ערך"],
            [
                {"מדד": "תאריך", "ערך": "23.06.2026"},
                {"מדד": "מכר", "ערך": "₪29,134,139"},
                {"מדד": "כמות", "ערך": "2,889,903"},
            ],
        )
        dm = {
            "type": "count",
            "series": [{"name": "KPI", "value": "ערך", "aggregation": "sum"}],
        }
        out = ensure_single_value_card_renderable(dm, formatted)
        assert out["type"] == "table"

    def test_time_series_multi_row_keeps_legacy_card(self):
        formatted = _formatted(
            ["month", "revenue"],
            [{"month": "2025-06", "revenue": 900}, {"month": "2025-05", "revenue": 800}],
        )
        dm = {"type": "metric_card", "series": [{"name": "Revenue", "value": "revenue"}]}
        out = ensure_single_value_card_renderable(dm, formatted)
        assert out["type"] == "metric_card"

    def test_datetime_dtype_column_keeps_legacy_card(self):
        formatted = _formatted(
            ["d", "v"],
            [{"d": "2025-06-01", "v": 900}, {"d": "2025-05-01", "v": 800}],
            {"d": {"dtype": "datetime64[ns]"}, "v": {"dtype": "int64"}},
        )
        dm = {"type": "metric_card", "series": [{"name": "V", "value": "v"}]}
        out = ensure_single_value_card_renderable(dm, formatted)
        assert out["type"] == "metric_card"

    def test_entity_table_without_selector_demotes(self):
        formatted = _formatted(
            ["customer", "revenue"],
            [{"customer": "Acme", "revenue": 100.0}, {"customer": "Bob", "revenue": 50.0}],
        )
        dm = {"type": "metric_card", "series": [{"name": "Revenue", "value": "revenue"}]}
        out = ensure_single_value_card_renderable(dm, formatted)
        assert out["type"] == "table"

    def test_non_card_types_untouched(self):
        dm = {"type": "bar_chart", "series": []}
        assert ensure_single_value_card_renderable(dm, MELTED) is dm

    def test_empty_rows_untouched(self):
        dm = {"type": "count", "series": []}
        out = ensure_single_value_card_renderable(dm, _formatted(["a"], []))
        assert out["type"] == "count"

    def test_sparkline_series_keeps_card(self):
        formatted = _formatted(
            ["label", "v"],
            [{"label": "a", "v": 1}, {"label": "b", "v": 2}],
        )
        dm = {
            "type": "metric_card",
            "series": [{"name": "V", "value": "v", "sparkline_column": "v"}],
        }
        out = ensure_single_value_card_renderable(dm, formatted)
        assert out["type"] == "metric_card"
