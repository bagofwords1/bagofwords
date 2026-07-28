"""Chart-spec derivation: the deterministic base plus validated LLM overrides.

The invariant this pins, for every case below:

    the emitted data_model references only columns that exist in the result,
    and it either renders or is a `table` — never a chart with nothing in it.

Historically each viz bug arrived as its own repro file, so each fix was as
narrow as the incident that prompted it. This is table-driven instead: a new
failure is a new row, and the invariant is asserted for all rows at once.

Background: docs/feedback-loops/infer-viz-empty-chart.md
"""

import pytest

from app.ai.tools.chart_spec import (
    apply_inference_overrides,
    build_chart_spec,
    build_final_data_model,
    ensure_renderable,
    column_fields,
    profile_columns,
    resolve_column,
)


# --- fixtures ---------------------------------------------------------------

def make_formatted(columns, rows, column_info=None):
    return {
        "columns": [{"headerName": c, "field": c} for c in columns],
        "rows": rows,
        "info": {
            "total_rows": len(rows),
            "total_columns": len(columns),
            "column_info": column_info or {},
        },
    }


# The field report: Hebrew sales-by-branch, one row per branch. Note the ASCII
# double quote inside "סה"כ מכר" — the gershayim that breaks naive JSON round
# trips.
BRANCH_COLS = ['שם סניף', 'סה"כ מכר', 'סה"כ כמות', 'מרווח', 'אחוז מרווח']
BRANCH_ROWS = [
    {'שם סניף': 'ראשון לציון', 'סה"כ מכר': 54224, 'סה"כ כמות': 16, 'מרווח': 4384, 'אחוז מרווח': 8.1},
    {'שם סניף': 'אשקלון בת הדר', 'סה"כ מכר': 37279, 'סה"כ כמות': 11, 'מרווח': 3014, 'אחוז מרווח': 8.1},
    {'שם סניף': 'תלפיות', 'סה"כ מכר': 33890, 'סה"כ כמות': 10, 'מרווח': 2740, 'אחוז מרווח': 8.1},
    {'שם סניף': 'נתניה', 'סה"כ מכר': 33890, 'סה"כ כמות': 10, 'מרווח': 2740, 'אחוז מרווח': 8.1},
    {'שם סניף': 'עד הלום', 'סה"כ מכר': 33890, 'סה"כ כמות': 10, 'מרווח': 2740, 'אחוז מרווח': 8.1},
    {'שם סניף': 'קרית אונו', 'סה"כ מכר': 30501, 'סה"כ כמות': 9, 'מרווח': 2466, 'אחוז מרווח': 8.1},
    {'שם סניף': 'פתח תקווה - סגולה', 'סה"כ מכר': 23723, 'סה"כ כמות': 7, 'מרווח': 1918, 'אחוז מרווח': 8.1},
    {'שם סניף': 'כפר סבא', 'סה"כ מכר': 13556, 'סה"כ כמות': 4, 'מרווח': 1096, 'אחוז מרווח': 8.1},
    {'שם סניף': 'באר שבע', 'סה"כ מכר': 3389, 'סה"כ כמות': 1, 'מרווח': 274, 'אחוז מרווח': 8.1},
]
BRANCHES = make_formatted(BRANCH_COLS, BRANCH_ROWS)

MONTHLY = make_formatted(
    ["month", "revenue"],
    [{"month": f"2025-{m:02d}", "revenue": 100 * m} for m in range(1, 13)],
)

# Granular: each month appears once per channel, so a bare chart would show
# first-row-wins unless a breakdown + aggregation are derived.
CHANNELS = make_formatted(
    ["month", "channel", "sales"],
    [
        {"month": f"2025-{m:02d}", "channel": c, "sales": m * 10 + i}
        for m in range(1, 7)
        for i, c in enumerate(["online", "retail", "wholesale"])
    ],
)

SINGLE = make_formatted(["total_hours"], [{"total_hours": 264.67}])


# --- the deterministic base -------------------------------------------------

class TestBuildChartSpec:
    def test_branches_bar_chart(self):
        spec = build_chart_spec(BRANCHES, "bar_chart")
        assert spec["type"] == "bar_chart"
        assert spec["series"][0]["key"] == 'שם סניף'
        assert spec["series"][0]["value"] == 'סה"כ מכר'
        # One row per branch: no breakdown, no aggregation to invent.
        assert "group_by" not in spec
        assert "aggregation" not in spec["series"][0]

    def test_time_series_prefers_the_date_axis(self):
        spec = build_chart_spec(MONTHLY, "line_chart")
        assert spec["series"][0]["key"] == "month"
        assert spec["series"][0]["value"] == "revenue"

    def test_detects_breakdown_and_aggregation(self):
        spec = build_chart_spec(CHANNELS, "bar_chart")
        assert spec["series"][0]["key"] == "month"
        assert spec["series"][0]["value"] == "sales"
        assert spec["group_by"] == "channel"

    def test_granular_rows_get_an_aggregation(self):
        granular = make_formatted(
            ["day", "amount"],
            [{"day": "2025-01-01", "amount": i} for i in range(20)]
            + [{"day": "2025-01-02", "amount": i} for i in range(20)],
        )
        spec = build_chart_spec(granular, "bar_chart")
        assert spec["series"][0]["aggregation"] == "sum"

    def test_single_value_card(self):
        spec = build_chart_spec(SINGLE, "metric_card")
        assert spec["series"][0]["value"] == "total_hours"

    def test_multi_row_card_over_time_gets_a_sparkline(self):
        spec = build_chart_spec(MONTHLY, "metric_card")
        assert spec["series"][0]["value"] == "revenue"
        assert spec["series"][0]["sparkline_x"] == "month"

    def test_pie_chart(self):
        spec = build_chart_spec(BRANCHES, "pie_chart")
        assert spec["series"][0]["key"] == 'שם סניף'
        assert spec["series"][0]["value"] == 'סה"כ מכר'

    def test_scatter_uses_two_numerics(self):
        spec = build_chart_spec(BRANCHES, "scatter_plot")
        assert spec["series"][0]["x"] == 'סה"כ מכר'
        assert spec["series"][0]["y"] == 'סה"כ כמות'

    def test_table_is_untouched(self):
        assert build_chart_spec(BRANCHES, "table") == {"type": "table", "series": []}

    def test_id_columns_are_not_measures(self):
        f = make_formatted(
            ["customer_id", "name", "revenue"],
            [{"customer_id": 1, "name": "a", "revenue": 10}, {"customer_id": 2, "name": "b", "revenue": 20}],
        )
        spec = build_chart_spec(f, "bar_chart")
        assert spec["series"][0]["value"] == "revenue"

    def test_no_numeric_column_demotes_via_renderability(self):
        f = make_formatted(["a", "b"], [{"a": "x", "b": "y"}])
        spec, reason = ensure_renderable(build_chart_spec(f, "bar_chart"), column_fields(f))
        assert spec["type"] == "table"
        assert reason


# --- column classification --------------------------------------------------

class TestProfiling:
    def test_classifies_branch_columns(self):
        by_name = {p.name: p for p in profile_columns(BRANCHES)}
        assert by_name['שם סניף'].categorical
        assert by_name['סה"כ מכר'].numeric
        assert by_name['אחוז מרווח'].numeric

    def test_year_is_temporal_not_a_measure(self):
        f = make_formatted(["year", "revenue"], [{"year": 2024, "revenue": 5}, {"year": 2025, "revenue": 7}])
        by_name = {p.name: p for p in profile_columns(f)}
        assert by_name["year"].temporal
        assert not by_name["year"].numeric
        assert build_chart_spec(f, "bar_chart")["series"][0]["value"] == "revenue"

    def test_hebrew_date_column_name_is_temporal(self):
        f = make_formatted(["תאריך", "מכירות"], [{"תאריך": "2025-01-01", "מכירות": 5}])
        assert {p.name: p for p in profile_columns(f)}["תאריך"].temporal


class TestResolveColumn:
    @pytest.mark.parametrize("candidate,expected", [
        ('סה"כ מכר', 'סה"כ מכר'),          # exact
        ('סהכ מכר', 'סה"כ מכר'),           # gershayim dropped
        ('סה״כ מכר', 'סה"כ מכר'),          # gershayim swapped for U+05F4
        ('  שם סניף  ', 'שם סניף'),         # padded
        ("total_sales", None),               # transliterated: rejected
        ("none", None),                      # nullish token
        ("column_name_or_null", None),       # the prompt's own placeholder
        (None, None),
        ([], None),
    ])
    def test_resolution(self, candidate, expected):
        assert resolve_column(candidate, BRANCH_COLS) == expected

    def test_case_insensitive_for_ascii(self):
        assert resolve_column("REVENUE", ["revenue"]) == "revenue"


# --- the eight field failures, end to end -----------------------------------

# Each entry is a real shape an inference model has produced. `expect_value` is
# the y column the final chart must draw; None means "the deterministic default
# survives". Before this module existed, every one of these except the first
# produced an empty chart.
LLM_OUTPUTS = [
    pytest.param(
        {"type": "bar_chart", "series": [{"name": "מכר", "key": 'שם סניף', "value": 'סה"כ מכר'}]},
        'סה"כ מכר', None, id="happy-path",
    ),
    pytest.param(
        None, 'סה"כ מכר', None, id="unparseable-json-unescaped-quote",
    ),
    pytest.param(
        {"type": "bar_chart", "series": [{"name": "מכר", "key": 'שם סניף', "value": 'מרווח'}],
         "group_by": "column_name_or_null"},
        'מרווח', "group_by", id="group_by-echoes-prompt-placeholder",
    ),
    pytest.param(
        {"type": "bar_chart", "series": [{"name": "מכר", "key": 'שם סניף', "value": 'מרווח'}],
         "group_by": "none"},
        'מרווח', "group_by", id="group_by-none",
    ),
    pytest.param(
        {"type": "bar_chart", "series": [{"name": "S", "key": 'שם סניף', "value": ['סה"כ מכר', 'מרווח']}]},
        'סה"כ מכר', None, id="value-as-list-two-measures",
    ),
    pytest.param(
        {"type": "bar_chart", "series": [{"name": "S", "key": 'שם סניף', "value": 'מרווח'}],
         "filters": [{"field": 'שם סניף', "op": "equals", "value": "נתניה"}]},
        'מרווח', None, id="filter-field-op-aliases",
    ),
    pytest.param(
        {"type": "bar", "series": [{"name": "S", "key": 'שם סניף', "value": 'מרווח'}]},
        'מרווח', None, id="type-alias-bar",
    ),
    pytest.param(
        {"type": "bar_chart", "series": [{"name": "Total Sales", "key": "branch_name", "value": "total_sales"}]},
        'סה"כ מכר', "value", id="transliterated-column-names",
    ),
    pytest.param(
        {"type": "bar_chart", "series": {"name": "S", "key": 'שם סניף', "value": 'מרווח'}},
        'מרווח', None, id="series-as-object-not-list",
    ),
    pytest.param(
        {"type": "bar_chart", "series": [{"name": "S", "key": 'שם סניף', "value": 'מרווח', "aggregation": "total"}]},
        'מרווח', "aggregation", id="invalid-aggregation",
    ),
]


@pytest.mark.parametrize("inferred,expect_value,expect_dropped", LLM_OUTPUTS)
def test_chart_always_renders(inferred, expect_value, expect_dropped):
    dm, meta = build_final_data_model("bar_chart", inferred, BRANCHES)

    assert dm["type"] == "bar_chart", f"demoted unexpectedly: {meta.get('demoted')}"
    assert dm["series"], "chart type with no series is an empty canvas"
    assert dm["series"][0]["key"] == 'שם סניף'
    assert dm["series"][0]["value"] == expect_value

    # The invariant: nothing in the spec names a column that isn't in the data.
    for entry in dm["series"]:
        for field in ("key", "value"):
            assert entry.get(field) in BRANCH_COLS
    assert dm.get("group_by") in (None, *BRANCH_COLS)

    if expect_dropped:
        assert any(expect_dropped in d for d in meta["dropped"]), meta


def test_two_measures_produce_two_series():
    inferred = {"type": "bar_chart",
                "series": [{"name": "S", "key": 'שם סניף', "value": ['סה"כ מכר', 'מרווח']}]}
    dm, _ = build_final_data_model("bar_chart", inferred, BRANCHES)
    assert [s["value"] for s in dm["series"]] == ['סה"כ מכר', 'מרווח']


def test_valid_group_by_is_honored():
    inferred = {"type": "bar_chart",
                "series": [{"name": "S", "key": "month", "value": "sales"}],
                "group_by": "channel"}
    dm, meta = build_final_data_model("bar_chart", inferred, CHANNELS)
    assert dm["group_by"] == "channel"
    assert not meta["dropped"]


def test_group_by_naming_an_all_empty_column_is_dropped():
    f = make_formatted(
        ["branch", "segment", "sales"],
        [{"branch": "a", "segment": "", "sales": 1}, {"branch": "b", "segment": "", "sales": 2}],
    )
    dm, meta = build_final_data_model(
        "bar_chart",
        {"series": [{"key": "branch", "value": "sales"}], "group_by": "segment"},
        f,
    )
    assert "group_by" not in dm
    assert any("segment" in d for d in meta["dropped"])


def test_no_inference_at_all_still_renders():
    dm, meta = build_final_data_model("bar_chart", None, BRANCHES)
    assert dm["series"][0]["value"] == 'סה"כ מכר'
    assert meta["source"] == "deterministic"


def test_unrenderable_request_demotes_to_table():
    # heatmap needs two categorical axes; this result has one.
    dm, meta = build_final_data_model("heatmap", None, MONTHLY)
    assert dm["type"] == "table"
    assert meta["demoted"]


def test_filters_are_normalized_from_aliases():
    inferred = {"series": [{"key": 'שם סניף', "value": 'מרווח'}],
                "filters": [{"field": 'שם סניף', "op": "equals", "value": "נתניה"}]}
    dm, _ = build_final_data_model("bar_chart", inferred, BRANCHES)
    assert dm["filters"] == [{"column": 'שם סניף', "operator": "equals", "value": "נתניה"}]


def test_filter_on_unknown_column_is_dropped():
    inferred = {"series": [{"key": 'שם סניף', "value": 'מרווח'}],
                "filters": [{"column": "region", "operator": "equals", "value": "x"}]}
    dm, meta = build_final_data_model("bar_chart", inferred, BRANCHES)
    assert not dm.get("filters")
    assert any("region" in d for d in meta["dropped"])


@pytest.mark.parametrize("junk", [
    {}, {"series": []}, {"series": [{}]}, {"series": [{"value": None}]},
    {"series": "nonsense"}, {"group_by": 42}, {"filters": "no"},
    {"series": [{"key": 123, "value": 456}]},
])
def test_junk_inference_never_breaks_the_chart(junk):
    dm, _ = build_final_data_model("bar_chart", junk, BRANCHES)
    assert dm["type"] == "bar_chart"
    assert dm["series"][0]["value"] == 'סה"כ מכר'
