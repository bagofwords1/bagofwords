"""A raw <EChart> line or bar chart is flagged where TrendChart / RankChart fit.

On unfamiliar data the models fell back to raw charts, and the defects the
components fix (hidden category names, mixed number formats) came back. Other
chart types, and a bar-and-line target chart, stay allowed; an edit is only
held to the charts it adds.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.tools.implementations._artifact_refs import raw_chart_errors  # noqa: E402

V11 = {"runtime": {"version": 11}}


def page(option):
    return f"function App() {{ const o = {option}; return <EChart option={{o}} />; }}"


def test_a_raw_line_or_bar_chart_is_flagged():
    for kind in ("line", "bar"):
        errs = raw_chart_errors(page(f"{{ xAxis: {{}}, series: [{{ type: '{kind}', data: [1, 2] }}] }}"), V11)
        assert len(errs) == 1 and "TrendChart" in errs[0] and "RankChart" in errs[0]


def test_series_built_with_map_are_seen():
    code = page("{ series: names.map(n => ({ name: n, type: \"line\", data: rows.map(r => r[n]) })) }")
    assert len(raw_chart_errors(code, V11)) == 1


def test_other_chart_types_and_target_charts_are_allowed():
    allowed = [
        "{ series: [{ type: 'scatter', data: [[1, 2]] }] }",
        "{ series: [{ type: 'pie', data: [] }] }",
        "{ series: [{ type: 'bar', data: [3] }, { type: 'line', data: [2] }] }",  # actual vs target
        "{ tooltip: { axisPointer: { type: 'line' } }, series: [{ type: 'heatmap', data: [] }] }",
        # a scatter with a fitted line, a candlestick with a moving average: mixed, not a line chart
        "{ series: [{ type: 'scatter', data: [[1, 2]] }, { type: 'line', data: [[0, 1], [2, 3]] }] }",
        "{ series: [{ type: 'candlestick', data: [] }, { type: 'line', name: 'MA5', data: [] }] }",
    ]
    for option in allowed:
        assert raw_chart_errors(page(option), V11) == [], option


def test_pages_without_echart_and_older_runtimes_are_not_checked():
    assert raw_chart_errors("const o = { series: [{ type: 'line' }] };", V11) == []
    assert raw_chart_errors(page("{ series: [{ type: 'line' }] }"), {"runtime": {"version": 10}}) == []


def test_an_edit_is_held_only_to_the_charts_it_adds():
    before = page("{ series: [{ type: 'line', data: [1] }] }")
    unrelated = before.replace("return", "const title = 'x'; return")
    assert raw_chart_errors(unrelated, V11, previous_code=before) == []
    added = before + "\nfunction B() { return <EChart option={{ series: [{ type: 'bar', data: [1] }] }} />; }"
    assert len(raw_chart_errors(added, V11, previous_code=before)) == 1
