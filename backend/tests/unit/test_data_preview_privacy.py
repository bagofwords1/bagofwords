"""Unit tests for privacy gating of data previews / stats fed to the LLM.

Regression coverage for the `allow_llm_see_data` leak: when the org setting is
off, `df.describe()`-derived values (the verbatim `top` cell, raw numeric
min/max/percentiles) must NOT reach the LLM. Structural counts and derived
aggregates (mean/std/sum) and date ranges are still allowed.

Pure-Python: no DB, no LLM.
"""
import json

from app.ai.context.data_preview import build_data_preview, gate_stats_for_privacy


# A get_df_info()-shaped stats dict carrying the leaky describe() fields.
def _sample_info():
    return {
        "total_rows": 5,
        "total_columns": 3,
        "memory_usage": 999,
        "dtypes_count": {"int64": 1, "object": 1, "datetime64[ns]": 1},
        "column_info": {
            "email": {
                "dtype": "str", "non_null_count": 5, "null_count": 0,
                "unique_count": 4, "count": 5, "unique": 4,
                "top": "bob@secret.com", "freq": 2,
            },
            "salary": {
                "dtype": "int64", "non_null_count": 5, "null_count": 0,
                "unique_count": 4, "count": 5.0, "mean": 156800.0, "std": 104540.4,
                "min": 64000.0, "25%": 95000.0, "50%": 95000.0, "75%": 220000.0,
                "max": 310000.0,
            },
            "signup_date": {
                "dtype": "datetime64[ns]", "non_null_count": 5, "null_count": 0,
                "unique_count": 4, "count": 5,
                "min": "2020-01-09T00:00:00", "max": "2023-11-30T00:00:00",
                "mean": "2022-01-12T19:12:00",
            },
        },
    }


def test_gate_stats_drops_verbatim_top_value():
    gated = gate_stats_for_privacy(_sample_info())
    email = gated["column_info"]["email"]
    # Raw most-frequent cell value must be gone; structural counts remain.
    assert "top" not in email
    assert "freq" not in email
    assert email["unique_count"] == 4
    assert "bob@secret.com" not in json.dumps(gated)


def test_gate_stats_drops_raw_numeric_min_max_percentiles():
    gated = gate_stats_for_privacy(_sample_info())
    salary = gated["column_info"]["salary"]
    for raw_key in ("min", "max", "25%", "50%", "75%"):
        assert raw_key not in salary, f"raw value {raw_key} leaked"
    blob = json.dumps(gated)
    for raw_val in ("64000", "310000", "95000", "220000"):
        assert raw_val not in blob, f"raw salary value {raw_val} leaked"


def test_gate_stats_keeps_aggregates_and_derives_sum():
    salary = gate_stats_for_privacy(_sample_info())["column_info"]["salary"]
    assert salary["mean"] == 156800.0
    assert salary["std"] == 104540.4
    # sum is exact: mean * count
    assert salary["sum"] == 156800.0 * 5.0


def test_gate_stats_keeps_date_range():
    signup = gate_stats_for_privacy(_sample_info())["column_info"]["signup_date"]
    assert signup["min"] == "2020-01-09T00:00:00"
    assert signup["max"] == "2023-11-30T00:00:00"


def test_build_data_preview_hidden_omits_rows_and_gates_stats():
    formatted = {
        "columns": [{"field": "email"}, {"field": "salary"}],
        "rows": [{"email": "bob@secret.com", "salary": 310000}],
        "info": _sample_info(),
    }
    dp = build_data_preview(formatted, allow_llm_see_data=False)
    assert "rows" not in dp
    assert dp["row_count"] == 5
    blob = json.dumps(dp)
    assert "bob@secret.com" not in blob
    assert "310000" not in blob


def test_build_data_preview_visible_includes_full_rows_and_stats():
    formatted = {
        "columns": [{"field": "email"}, {"field": "salary"}],
        "rows": [{"email": "bob@secret.com", "salary": 310000}],
        "info": _sample_info(),
    }
    dp = build_data_preview(formatted, allow_llm_see_data=True)
    # When allowed, nothing is gated — full rows come through.
    assert dp["rows"] == formatted["rows"]


def test_gate_stats_non_dict_passthrough():
    assert gate_stats_for_privacy(None) is None
    assert gate_stats_for_privacy("nope") == "nope"
