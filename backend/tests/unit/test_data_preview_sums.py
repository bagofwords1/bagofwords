"""Plain column sums on model-facing data previews.

Every multi-row preview the model sees gets a neutral ``column_sums`` block:
the exact sum over all rows of each numeric column, under the note
``sum = sum of all N rows``. Guards: no sum on identifier or year-like columns,
no block when the rows plainly hold a total row (a total label, or the
``k x max`` signature), and incomplete rows fail safe.

The report-body previews (``build_data_preview`` and the step UI preview) must
not change; the characterization test pins their structure.

Pure-Python: no DB, no LLM. Input comes from the real ``format_df_for_widget``.
"""
import json
import logging
import math
import re
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from app.ai import data_preview as dp
from app.ai import persisted_summary
from app.ai.code_execution.code_execution import StreamingCodeExecutor
from app.ai.data_preview import build_data_preview


def _formatted(df, max_rows=None):
    return StreamingCodeExecutor().format_df_for_widget(df, max_rows)


def _block(df, max_rows=None):
    """The column_sums block the model would see for *df*, or None."""
    formatted = _formatted(df, max_rows)
    out = dp.with_column_sums(build_data_preview(formatted), formatted)
    return out.get("column_sums")


def _sums(df, max_rows=None):
    block = _block(df, max_rows)
    return None if block is None else block["sum"]


def _assert_k_clean(df, cols):
    """No named non-negative column sits near k x max (k = 2, 3, 4) by chance.

    The 1% margin is wider than any S6 tolerance, so a control column can't
    match through rounding either.
    """
    for col in cols:
        values = [float(v) for v in df[col].dropna()]
        s, m = sum(values), max(values)
        for k in (2, 3, 4):
            assert abs(s - k * m) > 0.01 * m, f"{col} matches {k} x max"


def _dicts_with_key(obj, key):
    found = []
    if isinstance(obj, dict):
        if key in obj:
            found.append(obj)
        for v in obj.values():
            found.extend(_dicts_with_key(v, key))
    elif isinstance(obj, list):
        for v in obj:
            found.extend(_dicts_with_key(v, key))
    return found


def _is_in_order_subsequence(sub, seq):
    it = iter(seq)
    return all(any(x == y for y in it) for x in sub)


# ---------------------------------------------------------------------------
# Characterization: the report-body previews keep their shape (written and
# passed on the unedited tree; key lists and lengths captured there).
# ---------------------------------------------------------------------------

def _small_frame():
    return pd.DataFrame({
        "department": [f"D{i}" for i in range(12)],
        "budget_eur": [float(100 + i * 13) for i in range(12)],
        "actual_eur": [float(90 + i * 17) for i in range(12)],
    })


def _large_frame():
    rng = np.random.default_rng(7)
    return pd.DataFrame({f"m{j}": rng.random(1920) * 5000 for j in range(6)})


def test_report_body_previews_are_unchanged():
    small = _formatted(_small_frame())
    large = _formatted(_large_frame())
    # The large frame really overflows the preview budget, so the head+tail path runs.
    assert len(json.dumps(large["rows"]).encode()) > 48_000

    for formatted, n, p_keys, u_keys, u_len in (
        (small, 12,
         ["cells_truncated", "columns", "row_count", "rows", "truncated"],
         ["columns", "info", "rows"], 12),
        (large, 1920,
         ["cells_truncated", "columns", "note", "row_count", "rows", "truncated"],
         ["columns", "info", "rows", "total_rows", "truncated"], 20),
    ):
        p = build_data_preview(formatted)
        u = persisted_summary.build_step_context_summary(formatted)["ui_preview"]

        assert sorted(p) == p_keys
        assert sorted(u) == u_keys
        assert p["columns"] == formatted["columns"]
        assert p["row_count"] == n
        assert p["truncated"] is (n == 1920)

        if n == 12:
            assert p["rows"] == formatted["rows"]
        else:
            m = re.fullmatch(r"showing first (\d+) and last (\d+) of 1920 rows", p["note"])
            assert m, p["note"]
            head, tail = int(m.group(1)), int(m.group(2))
            assert p["rows"] == formatted["rows"][:head] + formatted["rows"][-tail:]

        assert _is_in_order_subsequence(u["rows"], formatted["rows"])
        assert len(u["rows"]) == u_len

        # Smoke check only: the UI preview keeps the step's own info object.
        assert sorted(u["info"]) == sorted(formatted["info"])
        for col, ci in formatted["info"]["column_info"].items():
            assert sorted(u["info"]["column_info"][col]) == sorted(ci)

        assert _dicts_with_key(p, "column_sums") == []
        assert _dicts_with_key(u, "column_sums") == []


# ---------------------------------------------------------------------------
# Sums over all rows
# ---------------------------------------------------------------------------

BUDGET_EUR = [160000.41, 101234.56, 189876.22, 140321.66, 82345.11, 150987.33,
              125432.88, 178654.44, 92345.66, 135678.99, 165432.11, 110987.55]
ACTUAL_EUR = [182345.67, 95321.45, 210987.33, 150432.98, 77654.32, 168943.55,
              132876.11, 199231.87, 88765.43, 143210.66, 176543.22, 121098.77]
REVENUE_4 = [100, 250, 400, 610]


def _department_table():
    return pd.DataFrame({
        "department": [f"Department {i}" for i in range(12)],
        "budget_eur": BUDGET_EUR,
        "actual_eur": ACTUAL_EUR,
    })


def test_sums_cover_all_rows_past_the_stored_cap():
    for seed in range(20):
        rng = np.random.default_rng(seed)
        units = rng.integers(0, 1000, size=1920)
        amount = rng.random(1920) * 5000
        df = pd.DataFrame({"units": units, "amount": amount})
        formatted = _formatted(df)
        assert len(formatted["rows"]) == 1000  # the stored rows stop at the cap

        block = _block(df)
        assert block is not None, f"seed {seed}"
        assert block["note"] == "sum = sum of all 1920 rows"
        assert block["sum"]["units"] == int(sum(int(u) for u in units))
        assert block["sum"]["units"] != sum(r["units"] for r in formatted["rows"])
        assert block["sum"]["amount"] == round(math.fsum(float(a) for a in amount), 2)


def test_complete_twelve_row_table_gets_sums():
    df = _department_table()
    _assert_k_clean(df, ["budget_eur", "actual_eur"])
    block = _block(df)
    assert block == {
        "note": "sum = sum of all 12 rows",
        "sum": {"budget_eur": 1633296.92, "actual_eur": 1747411.36},
    }


def test_single_and_empty_results_get_no_sums():
    one = pd.DataFrame({"revenue": [100]})
    empty = pd.DataFrame({"revenue": pd.Series([], dtype="int64")})
    assert _block(one) is None
    assert _block(empty) is None
    # Control: two rows, k-clean.
    two = pd.DataFrame({"revenue": [100, 250]})
    _assert_k_clean(two, ["revenue"])
    assert _sums(two) == {"revenue": 350}


def test_hidden_preview_is_returned_unchanged():
    df = _department_table()
    formatted = _formatted(df)
    hidden = build_data_preview(formatted, allow_llm_see_data=False)
    out = dp.with_column_sums(hidden, formatted)
    assert out is hidden
    assert "column_sums" not in out
    assert "sum" in out["stats"]["column_info"]["actual_eur"]
    # Control: the same data, visible.
    visible = dp.with_column_sums(build_data_preview(formatted), formatted)
    assert visible["column_sums"]["sum"]["actual_eur"] == 1747411.36


# ---------------------------------------------------------------------------
# Name guards
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "id", "dept_id", "customerId", "CUSTOMERID", "customerid", "zip_code", "sku",
    "invoice_no", "order_number", "region_key", "product_code", "מזהה_לקוח",
])
def test_identifier_columns_get_no_sum(name):
    df = pd.DataFrame({name: [11, 12, 13, 14], "revenue": REVENUE_4})
    _assert_k_clean(df, ["revenue"])
    assert _sums(df) == {"revenue": 1360}


@pytest.mark.parametrize("name", [
    "amount_paid", "AMOUNTPAID", "prepaid", "invalid", "covid", "num_shipments",
    "number_of_orders",
])
def test_id_like_suffix_exceptions_get_a_sum(name):
    df = pd.DataFrame({name: [5, 7, 9, 11], "revenue": REVENUE_4})
    _assert_k_clean(df, [name, "revenue"])
    assert _sums(df) == {name: 32, "revenue": 1360}


@pytest.mark.parametrize("name,values", [
    ("year", [2023, 2024, 2025, 2026]),
    ("fiscal_year", [2023, 2024, 2025, 2026]),
    ("month", [1, 2, 3, 4]),
    ("quarter", [1, 2, 3, 4]),
    ("week", [10, 11, 12, 13]),
    ("day", [5, 6, 7, 8]),
    ("hour", [8, 9, 10, 11]),
    ("שנה", [2023, 2024, 2025, 2026]),
    ("x", [2019, 2021, 2024, 2026]),
])
def test_year_like_columns_get_no_sum(name, values):
    df = pd.DataFrame({name: values, "revenue": REVENUE_4})
    _assert_k_clean(df, ["revenue"])
    assert _sums(df) == {"revenue": 1360}


def test_year_band_with_a_count_or_amount_hint_gets_a_sum():
    df = pd.DataFrame({
        "orders_2024": [1950, 1980, 2010, 2050],
        "cost_usd": [1960, 2000, 2020, 2040],
        "day_count": [3, 5, 8, 13],
        "x": [1950.5, 1990.0, 2020.0, 2050.0],
    })
    _assert_k_clean(df, ["orders_2024", "cost_usd", "day_count", "x"])
    assert _sums(df) == {"orders_2024": 7990, "cost_usd": 8020, "day_count": 29, "x": 8010.5}


def test_non_numeric_dtypes_get_no_sum():
    df = pd.DataFrame({
        "flag": [True, False, True, True],
        "when": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]),
        "lag": pd.to_timedelta([1, 2, 3, 4], unit="D"),
        "tier": pd.Categorical(["a", "b", "a", "c"]),
        "label": ["a", "b", "c", "d"],
        "revenue": REVENUE_4,
    })
    _assert_k_clean(df, ["revenue"])
    assert _sums(df) == {"revenue": 1360}


def _revenue_info(**overrides):
    ci = {"dtype": "int64", "count": 4.0, "mean": 340.0, "min": 100.0, "max": 610.0,
          "non_null_count": 4}
    ci.update(overrides)
    return ci


def test_malformed_info_gets_no_sum_and_no_error():
    good = _revenue_info()
    info = {
        "total_rows": 4,
        "column_info": {
            "revenue": good,
            "nan_mean": _revenue_info(mean=float("nan")),
            "inf_mean": _revenue_info(mean=float("inf")),
            "no_count": {k: v for k, v in good.items() if k != "count"},
            "bool_mean": _revenue_info(mean=True),
            "only_in_info": _revenue_info(),
        },
    }
    columns = [
        {"field": "revenue"}, {"field": "nan_mean"}, {"field": "inf_mean"},
        {"field": "no_count"}, {"field": "bool_mean"}, {"field": "only_in_columns"},
        {"headerName": "no field"}, 42,
    ]
    assert dp.column_sums(info, columns, 4) == {
        "note": "sum = sum of all 4 rows", "sum": {"revenue": 1360},
    }
    # Plain string entries are fields; missing or empty columns fall back to info order.
    assert dp.column_sums(info, ["revenue"], 4)["sum"] == {"revenue": 1360}
    fallback = {"revenue": 1360, "only_in_info": 1360}
    # Two non-negative columns matching nothing: the k x max check clears.
    assert dp.column_sums(info, None, 4)["sum"] == fallback
    assert dp.column_sums(info, [], 4)["sum"] == fallback
    # Nothing usable at all.
    assert dp.column_sums(None, columns, 4) is None
    assert dp.column_sums("info", columns, 4) is None
    assert dp.column_sums({"column_info": "x"}, columns, 4) is None
    assert dp.column_sums({"column_info": {"revenue": "x"}}, columns, 4) is None
    preview = {"row_count": 4, "rows": []}
    assert dp.with_column_sums(preview, "data") is preview
    assert dp.with_column_sums(preview, None) is preview
    assert dp.with_column_sums(None, {"info": info}) is None
    assert dp.with_column_sums({"row_count": "4"}, {"info": info, "columns": columns}) == {"row_count": "4"}
    # Control: the same well-formed column through with_column_sums.
    out = dp.with_column_sums(preview, {"info": info, "columns": columns})
    assert out["column_sums"]["sum"] == {"revenue": 1360}


def test_numeric_columns_without_a_guard_are_summed():
    """ADR 3: true but possibly meaningless sums are an accepted, labelled risk."""
    df = pd.DataFrame({
        "margin_pct": [12.5, 8.25, 15.0, 22.75, 9.5, 11.0],
        "avg_cost": [45.2, 61.8, 38.4, 52.6, 70.1, 49.9],
        "rank": [1, 2, 3, 4, 5, 6],
        "window_total": [1000] * 6,
        "region_total": [300, 300, 700, 700, 700, 700],
        "customer_count": [14, 22, 9, 31, 17, 25],
        "mrr": [1200.0, 850.5, 2300.0, 1750.25, 990.0, 1430.0],
        "variance_pct": [-12.5, 4.0, 18.25, -3.5, 7.0, -1.25],
        "pct_of_target": [120, 95, 150, 80, 110, 130],
    })
    _assert_k_clean(df, [c for c in df.columns if c != "variance_pct"])
    assert _sums(df) == {
        "margin_pct": 79,
        "avg_cost": 318,
        "rank": 21,
        "window_total": 6000,
        "region_total": 3400,
        "customer_count": 118,
        "mrr": 8520.75,
        "variance_pct": 12,
        "pct_of_target": 685,
    }


# ---------------------------------------------------------------------------
# Total rows
# ---------------------------------------------------------------------------

def _signed_regions(last_label):
    return pd.DataFrame({
        "region": ["North", "South", "West", last_label],
        "amount": [-50, 120, 75, 145],
    })


@pytest.mark.parametrize("label", [
    "Total", "TOTAL", "Total:", "Total EMEA", "EMEA total", "Grand total", "grand_total",
    "Subtotal", "All", 'סה"כ', "סה״כ", "סך הכל", "Average", "AVG", "Mean:", "ממוצע",
])
def test_total_label_drops_the_block(label):
    assert _block(_signed_regions(label)) is None
    # Control: the same frame without the label row.
    control = _signed_regions(label).iloc[:3].reset_index(drop=True)
    assert _sums(control) == {"amount": 145}


@pytest.mark.parametrize("cell", [
    "Totally Organic", "Totalizer", "Above average", "Mean Time To Repair",
    "A long description that mentions the total once but is far too long to be a label",
])
def test_label_lookalikes_keep_the_block(cell):
    assert _sums(_signed_regions(cell)) == {"amount": 290}


REVENUE_COST = {"revenue": [100, 250, 400, 610], "cost": [40, 90, 130, 220]}


def _detail_rows():
    return pd.DataFrame({
        "region": ["X", "X", "Y", "Y"],
        "country": ["p", "q", "p", "q"],
        "city": ["c1", "c2", "c3", "c4"],
        **REVENUE_COST,
    })


def _with_grand_total():
    df = _detail_rows()
    total = {"region": None, "country": None, "city": None, "revenue": 1360, "cost": 480}
    return pd.concat([df, pd.DataFrame([total])], ignore_index=True)


def _with_rollup_one_level():
    df = _detail_rows()[["region", "country", "revenue", "cost"]]
    extra = [
        {"region": "X", "country": None, "revenue": 350, "cost": 130},
        {"region": "Y", "country": None, "revenue": 1010, "cost": 350},
        {"region": None, "country": None, "revenue": 1360, "cost": 480},
    ]
    return pd.concat([df, pd.DataFrame(extra)], ignore_index=True)


def _with_rollup_two_levels():
    df = _detail_rows()
    extra = [
        {"region": "X", "country": "p", "city": None, "revenue": 100, "cost": 40},
        {"region": "X", "country": "q", "city": None, "revenue": 250, "cost": 90},
        {"region": "Y", "country": "p", "city": None, "revenue": 400, "cost": 130},
        {"region": "Y", "country": "q", "city": None, "revenue": 610, "cost": 220},
        {"region": "X", "country": None, "city": None, "revenue": 350, "cost": 130},
        {"region": "Y", "country": None, "city": None, "revenue": 1010, "cost": 350},
        {"region": None, "country": None, "city": None, "revenue": 1360, "cost": 480},
    ]
    return pd.concat([df, pd.DataFrame(extra)], ignore_index=True)


def _with_loc_total():
    df = _detail_rows()
    df.loc["Total"] = df.sum(numeric_only=True)
    return df


@pytest.mark.parametrize("build", [
    _with_grand_total, _with_rollup_one_level, _with_rollup_two_levels, _with_loc_total,
])
def test_unlabelled_total_rows_drop_the_block(build):
    assert _block(build()) is None
    # Control: the detail rows alone.
    control = _detail_rows()
    _assert_k_clean(control, ["revenue", "cost"])
    assert _sums(control) == {"revenue": 1360, "cost": 480}


# Unrounded true values; each ends in ...44 / ...36, so 2-decimal rounding
# leaves a real, non-zero error in every sum.
TRUE_ACTUAL = [182345.6744, 95321.4544, 210987.3344, 150432.9844, 77654.3244, 168943.5544,
               132876.1144, 199231.8744, 88765.4344, 143210.6644, 176543.2244, 121098.7744]
TRUE_BUDGET = [160000.4136, 101234.5636, 189876.2236, 140321.6636, 82345.1136, 150987.3336,
               125432.8836, 178654.4436, 92345.6636, 135678.9936, 165432.1136, 110987.5536]
# Whole-unit case: values in the thousands, each .4 or .3 off a whole number.
TRUE_SHIPPED = [4523.4, 3891.4, 5210.4, 4876.4, 3345.4, 4998.4,
                5102.4, 3777.4, 4412.4, 4689.4, 3956.4, 5321.4]
TRUE_HANDLED = [2210.3, 1987.3, 2456.3, 2310.3, 1876.3, 2398.3,
                2501.3, 1934.3, 2123.3, 2276.3, 2045.3, 2487.3]


def _rounded_details(digits=2):
    return pd.DataFrame({
        "region": [f"R{i // 4}" for i in range(12)],
        "country": [f"C{i}" for i in range(12)],
        "actual_eur": [round(v, digits) for v in TRUE_ACTUAL],
        "budget_eur": [round(v, digits) for v in TRUE_BUDGET],
    })


def _round_sum_rollup_k3():
    # SELECT region, country, ROUND(SUM(x), 2) ... GROUP BY ROLLUP(region, country)
    df = _rounded_details()
    extra = []
    for g in range(3):
        extra.append({
            "region": f"R{g}", "country": None,
            "actual_eur": round(sum(TRUE_ACTUAL[g * 4:g * 4 + 4]), 2),
            "budget_eur": round(sum(TRUE_BUDGET[g * 4:g * 4 + 4]), 2),
        })
    extra.append({"region": None, "country": None,
                  "actual_eur": round(sum(TRUE_ACTUAL), 2),
                  "budget_eur": round(sum(TRUE_BUDGET), 2)})
    return pd.concat([df, pd.DataFrame(extra)], ignore_index=True), 3


def _round_sum_grand_total_k2():
    df = _rounded_details()
    total = {"region": None, "country": None,
             "actual_eur": round(sum(TRUE_ACTUAL), 2),
             "budget_eur": round(sum(TRUE_BUDGET), 2)}
    return pd.concat([df, pd.DataFrame([total])], ignore_index=True), 2


def _df_round_two_after_loc_total():
    df = pd.DataFrame({
        "region": [f"R{i // 4}" for i in range(12)],
        "actual_eur": TRUE_ACTUAL,
        "budget_eur": TRUE_BUDGET,
    })
    df.loc["Total"] = df.sum(numeric_only=True)
    return df.round(2), 2


def _df_round_whole_units():
    df = pd.DataFrame({
        "site": [f"S{i}" for i in range(12)],
        "shipped": TRUE_SHIPPED,
        "handled": TRUE_HANDLED,
    })
    df.loc["Total"] = df.sum(numeric_only=True)
    return df.round(0), 2


def _assert_rounding_error_is_real(df, cols, k, unit):
    """At least one column misses the exact r4 check, and all stay within the S6 bound."""
    missed = False
    n = len(df)
    for col in cols:
        values = [float(v) for v in df[col]]
        s, m = math.fsum(values), max(values)
        err = abs(s - k * m)
        assert err <= 0.5 * unit * (n + 3), f"{col}: {err} outside the S6 bound"
        missed = missed or err > 1e-9 * abs(s)
    assert missed, "fixture has no rounding error; it would not test the tolerance"


@pytest.mark.parametrize("build,cols,unit", [
    (_round_sum_rollup_k3, ["actual_eur", "budget_eur"], 0.01),
    (_round_sum_grand_total_k2, ["actual_eur", "budget_eur"], 0.01),
    (_df_round_two_after_loc_total, ["actual_eur", "budget_eur"], 0.01),
    (_df_round_whole_units, ["shipped", "handled"], 1),
])
def test_rounded_total_rows_drop_the_block(build, cols, unit):
    df, k = build()
    assert df[cols[0]].dtype == "float64"
    _assert_rounding_error_is_real(df, cols, k, unit)
    assert _block(df) is None
    # Control: the same frame without the total and subtotal rows.
    if unit == 1:
        control = pd.DataFrame({"site": [f"S{i}" for i in range(12)],
                                "shipped": TRUE_SHIPPED, "handled": TRUE_HANDLED}).round(0)
    else:
        control = _rounded_details()
    _assert_k_clean(control, cols)
    assert set(_sums(control)) == set(cols)


def test_rounding_tolerance_bounds():
    # Named exemptions from _assert_k_clean: these three sit on the edges on purpose.
    def one_column(values):
        return pd.DataFrame({"item": ["a", "b", "c"], "amount": values})

    # A: 7 vs 2 x 4 is 1 away; the 1% cap (0.04) binds, so the block stays.
    assert _sums(one_column([1.0, 2.0, 4.0])) == {"amount": 7}
    # B: 5999 vs 2 x 2999 is 1 away, inside the rounding term 0.5 x 1 x (3 + 3) = 3.
    assert _block(one_column([1000.0, 2000.0, 2999.0])) is None
    # C: 5990 vs 2 x 2990 is 10 away: inside 1% of max (29.9), outside the rounding term (3).
    assert _sums(one_column([1000.0, 2000.0, 2990.0])) == {"amount": 5990}


def test_lone_k_max_match_among_several_columns_keeps_the_block():
    # Named exemption: item_count [1, 2, 3] matches 2 x max on purpose.
    df = pd.DataFrame({"item_count": [1, 2, 3], "revenue": [100, 250, 410]})
    _assert_k_clean(df, ["revenue"])
    assert _sums(df) == {"item_count": 6, "revenue": 760}


def test_single_non_negative_column_matching_k_max_drops_the_block():
    df = pd.DataFrame({"item": ["a", "b", "c"], "qty": [1, 2, 3]})
    assert _block(df) is None
    # Control.
    control = pd.DataFrame({"item": ["a", "b", "c"], "qty": [1, 2, 4]})
    _assert_k_clean(control, ["qty"])
    assert _sums(control) == {"qty": 7}


def test_k_max_maxima_on_different_rows_keep_the_block():
    # Named exemption: both columns match on purpose, their maxima on different rows.
    df = pd.DataFrame({"a": [1, 2, 3], "b": [3, 2, 1]})
    assert _sums(df) == {"a": 6, "b": 6}


# ---------------------------------------------------------------------------
# Incomplete rows: a missing label proves nothing; k x max decides
# ---------------------------------------------------------------------------

def _big_frame(seed=11, n=1920):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "item": [f"item {i}" for i in range(n)],
        "units": rng.integers(1, 1000, size=n),
        "amount": np.round(rng.random(n) * 5000, 2),
    })


def _incomplete_total_label():
    df = _big_frame()
    with_label = df.copy()
    with_label.loc[10, "item"] = "Total"
    return with_label, df


def _incomplete_total_row_past_stored():
    details = _big_frame(n=1919)
    total = {"item": None, "units": int(details["units"].sum()),
             "amount": float(details["amount"].sum())}
    return pd.concat([details, pd.DataFrame([total])], ignore_index=True), details


def _incomplete_rounded_total_row_past_stored():
    rng = np.random.default_rng(23)
    true_amount = rng.random(1919) * 5000
    true_cost = rng.random(1919) * 3000
    details = pd.DataFrame({
        "item": [f"item {i}" for i in range(1919)],
        "amount": np.round(true_amount, 2),
        "cost": np.round(true_cost, 2),
    })
    total = {"item": None, "amount": round(float(true_amount.sum()), 2),
             "cost": round(float(true_cost.sum()), 2)}
    df = pd.concat([details, pd.DataFrame([total])], ignore_index=True)
    _assert_rounding_error_is_real(df, ["amount", "cost"], 2, 0.01)
    return df, details


def _incomplete_all_signed():
    rng = np.random.default_rng(5)
    df = pd.DataFrame({
        "item": [f"item {i}" for i in range(1920)],
        "variance": rng.integers(-500, 500, size=1920),
        "delta": np.round(rng.random(1920) * 200 - 100, 2),
    })
    control = df.copy()
    control["units"] = rng.integers(1, 1000, size=1920)
    return df, control


@pytest.mark.parametrize("build", [
    _incomplete_total_label,
    _incomplete_total_row_past_stored,
    _incomplete_rounded_total_row_past_stored,
    _incomplete_all_signed,
])
def test_incomplete_rows_rely_on_the_k_max_check(build):
    df, control = build()
    assert len(_formatted(df)["rows"]) < len(df)
    assert _block(df) is None
    # Control: the same frame without the label / total row (or with a non-negative column).
    _assert_k_clean(control, [c for c in control.columns
                              if c != "item" and control[c].min() >= 0])
    assert _block(control) is not None


def test_label_scan_stops_at_the_row_cap():
    def frame(label_at):
        df = _big_frame(n=1500)
        df.loc[label_at, "item"] = "Total Logistics"
        return df

    late = frame(1200)
    _assert_k_clean(late, ["units", "amount"])
    formatted = _formatted(late, max_rows=1500)
    assert len(formatted["rows"]) == 1500  # every row stored
    assert _block(late, max_rows=1500) is not None
    assert _block(frame(800), max_rows=1500) is None


def test_queries_section_input_shape():
    def section_block(df):
        formatted = _formatted(df)
        columns = [{"field": c["field"]} for c in formatted["columns"]]
        return dp.column_sums(formatted["info"], columns, len(df), formatted["rows"][:5])

    # 12-row step whose 12th row is an unlabelled non-negative total.
    details = pd.DataFrame({"dept": [f"D{i}" for i in range(11)],
                            "budget_eur": BUDGET_EUR[:11], "actual_eur": ACTUAL_EUR[:11]})
    total = {"dept": None, "budget_eur": float(details["budget_eur"].sum()),
             "actual_eur": float(details["actual_eur"].sum())}
    with_total = pd.concat([details, pd.DataFrame([total])], ignore_index=True)
    assert section_block(with_total) is None
    _assert_k_clean(details, ["budget_eur", "actual_eur"])
    assert section_block(details) is not None

    # The same with rows rounded to 2 decimals and a rounded NULL-label total.
    rounded_details = pd.DataFrame({"dept": [f"D{i}" for i in range(11)],
                                    "actual_eur": [round(v, 2) for v in TRUE_ACTUAL[:11]],
                                    "budget_eur": [round(v, 2) for v in TRUE_BUDGET[:11]]})
    rounded_total = {"dept": None, "actual_eur": round(sum(TRUE_ACTUAL[:11]), 2),
                     "budget_eur": round(sum(TRUE_BUDGET[:11]), 2)}
    rounded = pd.concat([rounded_details, pd.DataFrame([rounded_total])], ignore_index=True)
    _assert_rounding_error_is_real(rounded, ["actual_eur", "budget_eur"], 2, 0.01)
    assert section_block(rounded) is None
    _assert_k_clean(rounded_details, ["actual_eur", "budget_eur"])
    assert section_block(rounded_details) is not None

    # 12-row step, signed columns only: the check can't run.
    signed = pd.DataFrame({"dept": [f"D{i}" for i in range(12)],
                           "variance": [(-1) ** i * (100 + 7 * i) for i in range(12)]})
    assert section_block(signed) is None

    # 4-row step (complete within 5 preview rows), signed, Total label.
    assert section_block(_signed_regions("Total")) is None
    assert section_block(_signed_regions("Total").iloc[:3].reset_index(drop=True)) == {
        "note": "sum = sum of all 3 rows", "sum": {"amount": 145},
    }


# ---------------------------------------------------------------------------
# Keys, labels, rounding, caps, note, log
# ---------------------------------------------------------------------------

def test_integer_column_labels_match_live_and_stored():
    df = pd.DataFrame({"region": ["N", "S", "W"], 2024: [1200, 3400, 5100], 2025: [900, 2600, 4300]})
    _assert_k_clean(df, [2024, 2025])
    formatted = _formatted(df)
    assert 2024 in formatted["info"]["column_info"]  # live info is keyed by the raw label
    stored = json.loads(json.dumps(formatted))
    assert "2024" in stored["info"]["column_info"]
    expected = {"note": "sum = sum of all 3 rows", "sum": {"2024": 9700, "2025": 7800}}
    for data in (formatted, stored):
        out = dp.with_column_sums(build_data_preview(data), data)
        assert out["column_sums"] == expected


def test_guards_read_field_not_header_name():
    df = pd.DataFrame({"revenue": REVENUE_4, "dept_id": [11, 12, 13, 14]})
    formatted = _formatted(df)
    headers = {"revenue": "מזהה", "dept_id": "מחלקה"}
    for c in formatted["columns"]:
        c["headerName"] = headers[c["field"]]
    out = dp.with_column_sums(build_data_preview(formatted), formatted)
    assert out["column_sums"]["sum"] == {"revenue": 1360}


def test_rounding():
    def info_for(sum_value, dtype="float64"):
        return {"dtype": dtype, "count": 2.0, "mean": sum_value / 2,
                "min": 0.0, "max": sum_value * 0.7, "non_null_count": 2}

    info = {"column_info": {
        "a": info_for(2248271.369999),
        "b": info_for(42000.0000001),
        "c": info_for(0.000123456),
        "d": info_for(123456789012.6),
    }}
    block = dp.column_sums(info, None, 2)
    assert block["sum"] == {"a": 2248271.37, "b": 42000, "c": 0.0001235, "d": 123456789013}
    assert isinstance(block["sum"]["b"], int)
    assert isinstance(block["sum"]["d"], int)

    rng = np.random.default_rng(3)
    big = rng.integers(0, 10**9, size=1920)
    df = pd.DataFrame({"bytes_sent": big})
    exact = sum(int(v) for v in big)
    got = _sums(df)["bytes_sent"]
    assert isinstance(got, int) and got == exact


def test_cap_and_size():
    names = [f"amount_col{i:02d}" for i in range(14)]
    assert all(len(n) == 12 for n in names)
    df = pd.DataFrame({n: [100 + i, 250 + i, 410 + i] for i, n in enumerate(names)})
    _assert_k_clean(df, names)
    block = _block(df)
    assert list(block["sum"]) == names[:dp.SUMS_MAX_COLUMNS]
    assert dp.SUMS_MAX_COLUMNS == 10
    assert dp.SUMS_ROW_SCAN_LIMIT == 1000
    assert len(json.dumps(block).encode()) <= 600


def test_note_is_neutral():
    df = pd.DataFrame({"units": np.arange(1920) % 997 + 1})
    block = _block(df)
    assert block["note"] == "sum = sum of all 1920 rows"
    assert set(block) == {"note", "sum"}
    lowered = block["note"].casefold()
    for word in ("use", "instead", "total"):
        assert word not in lowered


def test_added_block_is_logged_with_step_and_counts_only(monkeypatch, caplog):
    monkeypatch.setattr(logging.getLogger("app.ai.data_preview"), "disabled", False)
    caplog.set_level(logging.INFO, logger="app.ai.data_preview")
    df = _department_table()
    df["headcount"] = [12, 7, 15, 9, 4, 11, 8, 14, 5, 10, 13, 6]
    _assert_k_clean(df, ["headcount"])
    formatted = _formatted(df)

    out = dp.with_column_sums(build_data_preview(formatted), formatted, step_id="s-1")
    assert len(out["column_sums"]["sum"]) == 3
    records = [r for r in caplog.records if r.name == "app.ai.data_preview"]
    assert [r.getMessage() for r in records] == ["column sums added: step=s-1 rows=12 columns=3"]
    message = records[0].getMessage()
    for name in ("budget_eur", "actual_eur", "headcount", "department"):
        assert name not in message
    for value in out["column_sums"]["sum"].values():
        assert str(value) not in message

    caplog.clear()
    hidden = build_data_preview(formatted, allow_llm_see_data=False)
    dp.with_column_sums(hidden, formatted, step_id="s-2")
    one_row = _formatted(pd.DataFrame({"revenue": [100]}))
    dp.with_column_sums(build_data_preview(one_row), one_row, step_id="s-3")
    assert [r for r in caplog.records if r.name == "app.ai.data_preview"] == []


# ---------------------------------------------------------------------------
# P1b: Decimal (object-dtype) columns, summed exactly from complete rows
# ---------------------------------------------------------------------------

def _decimals(*texts):
    return [Decimal(t) for t in texts]


# 12 Decimal amounts whose exact sum is 2266089.37.
DECIMAL_AMOUNTS = _decimals(
    "182345.67", "95321.45", "210987.33", "150432.98", "77654.32", "168943.55",
    "132876.11", "199231.87", "88765.43", "143210.66", "176543.22", "639776.78",
)
CLEAN_DECIMALS = _decimals("100.10", "250.25", "400.40", "610.61")


def _decimal_frame(columns):
    """A frame built from raw rows, as the Redshift/Databricks/ClickHouse clients do."""
    names = list(columns)
    rows = list(zip(*columns.values()))
    return pd.DataFrame(rows, columns=names)


def test_decimal_column_gets_exact_sum_from_complete_rows():
    df = _decimal_frame({
        "department": [f"Department {i}" for i in range(12)],
        "actual_eur": DECIMAL_AMOUNTS,
    })
    assert df["actual_eur"].dtype == object
    _assert_k_clean(df, ["actual_eur"])
    formatted = _formatted(df)
    out = dp.with_column_sums(build_data_preview(formatted), formatted)
    assert out["column_sums"] == {
        "note": "sum = sum of all 12 rows",
        "sum": {"actual_eur": float(sum(DECIMAL_AMOUNTS))},
    }
    assert out["column_sums"]["sum"]["actual_eur"] == 2266089.37
    stored = json.loads(json.dumps(formatted))
    again = dp.with_column_sums(build_data_preview(stored), stored)
    assert again["column_sums"] == out["column_sums"]


@pytest.mark.parametrize("bad_cells", [
    ["abc", Decimal("2.50"), Decimal("3.75"), Decimal("4.10")],
    ["1.10", Decimal("2.50"), Decimal("3.75"), Decimal("4.10")],
    [True, Decimal("2.50"), False, Decimal("4.10")],
    [Decimal("1e15"), Decimal("2.50"), Decimal("3.75"), Decimal("4.10")],
])
def test_object_column_with_a_non_number_gets_no_sum(bad_cells):
    df = _decimal_frame({"other_amount": bad_cells, "amount": CLEAN_DECIMALS})
    assert df["other_amount"].dtype == object
    assert df["amount"].dtype == object
    _assert_k_clean(df, ["amount"])
    # Control: the clean Decimal column in the same frame gets its sum.
    assert _sums(df) == {"amount": 1361.36}


def test_decimal_column_follows_the_guards_and_total_row_checks():
    amount = _decimals("1200.50", "830.25", "1475.00", "990.75",
                       "1610.10", "720.40", "1335.65", "1050.35")
    cost = _decimals("410.20", "380.05", "515.75", "295.40",
                     "602.10", "330.90", "455.55", "388.15")
    guarded = _decimal_frame({
        "dept_id": _decimals(*[str(101 + i) for i in range(8)]),
        "x": _decimals(*[str(2019 + i) for i in range(8)]),
        "amount": amount,
    })
    _assert_k_clean(guarded, ["amount"])
    # Control: the Decimal amount column is summed; the id and year-like ones are not.
    assert _sums(guarded) == {"amount": 9213}

    details = _decimal_frame({"item": [f"i{i}" for i in range(8)], "amount": amount, "cost": cost})
    total = pd.DataFrame([{"item": None, "amount": sum(amount), "cost": sum(cost)}])
    with_total = pd.concat([details, total], ignore_index=True)
    assert with_total["amount"].dtype == object
    assert _block(with_total) is None
    # Control: the same frame without the total row.
    _assert_k_clean(details, ["amount", "cost"])
    assert _sums(details) == {"amount": 9213, "cost": 3378.1}


def test_decimal_column_past_the_stored_cap_gets_no_sum():
    rng = np.random.default_rng(13)
    cents = rng.integers(0, 500_000, size=1920)
    df = _decimal_frame({
        "amount": [Decimal(int(c)) / 100 for c in cents],
        "weight": rng.random(1920) * 100,
    })
    assert df["amount"].dtype == object
    _assert_k_clean(df, ["weight"])
    sums = _sums(df)
    # Control: the float column is still summed over all 1,920 rows.
    assert sums == {"weight": round(math.fsum(float(w) for w in df["weight"]), 2)}


def test_read_sql_shaped_frame_sums_through_info():
    rows = [(f"Department {i}", amount) for i, amount in enumerate(DECIMAL_AMOUNTS)]
    df = pd.DataFrame.from_records(rows, columns=["department", "actual_eur"], coerce_float=True)
    assert df["actual_eur"].dtype == "float64"
    assert _sums(df) == {"actual_eur": 2266089.37}


# ---------------------------------------------------------------------------
# Precision and failure bounds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dtype", ["int64", "float64"])
def test_numeric_sum_past_float_precision_gets_no_sum(dtype):
    # mean x count loses whole units once the sum nears 2**53 (~9e15).
    rng = np.random.default_rng(29)
    df = pd.DataFrame({
        "amount": rng.integers(10**12, 10**13, size=1500).astype(dtype),
        "units": np.arange(1500) % 100 + 1,  # 1..100 fifteen times: 75750
    })
    assert df["amount"].sum() > 1e15
    _assert_k_clean(df, ["units"])
    # Control: the additive column in the same frame keeps its sum.
    assert _sums(df) == {"units": 75750}


def _huge_object_data():
    info = {"total_rows": 3, "column_info": {
        "amount": {"dtype": "object", "non_null_count": 3},
    }}
    rows = [{"amount": 10**400}, {"amount": 5}, {"amount": 7}]
    return {"info": info, "columns": [{"field": "amount"}], "rows": rows}


def test_object_cell_past_float_range_declines_without_raising():
    data = _huge_object_data()
    assert dp.column_sums(data["info"], data["columns"], 3, data["rows"]) is None
    preview = {"row_count": 3, "rows": []}
    assert dp.with_column_sums(preview, data, step_id="s-9") is preview


def test_column_sums_failure_returns_preview_and_logs_type_only(monkeypatch, caplog):
    monkeypatch.setattr(logging.getLogger("app.ai.data_preview"), "disabled", False)
    caplog.set_level(logging.INFO, logger="app.ai.data_preview")

    def boom(*args, **kwargs):
        raise ValueError("amount 4242")

    monkeypatch.setattr(dp, "column_sums", boom)
    formatted = _formatted(_department_table())
    preview = build_data_preview(formatted)

    assert dp.with_column_sums(preview, formatted, step_id="s-7") is preview
    records = [r for r in caplog.records if r.name == "app.ai.data_preview"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.WARNING, "column sums skipped: step=s-7 error=ValueError"),
    ]
