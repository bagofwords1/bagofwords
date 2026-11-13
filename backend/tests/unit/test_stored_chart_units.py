"""New stored charts carry units and open crowded ranges on the latest window.

The inferred `display` (currency, percent) reaches the value axis of line,
bar and area charts, and a chart over the cap (24 periods with several series,
60 with one) is stored with showDataZoom. Both survive being read back.
Without either, the view is what it was.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.tools.implementations.create_data import build_view_from_data_model  # noqa: E402
from app.schemas.view_schema import ViewSchema  # noqa: E402


def build(chart_type="line_chart", display=None, rows=None, group_by=None):
    dm = {"type": chart_type, "series": [{"name": "Revenue", "key": "month", "value": "revenue"}]}
    if display:
        dm["display"] = display
    if group_by:
        dm["group_by"] = group_by
    view = build_view_from_data_model(dm, title="Revenue", palette_theme="default",
                                      available_columns=["month", "revenue", "rep"], rows=rows)
    return ViewSchema.model_validate(view.model_dump(exclude_none=True)).view


@pytest.mark.parametrize("chart_type", ["line_chart", "bar_chart", "area_chart"])
def test_currency_reaches_the_value_axis(chart_type):
    v = build(chart_type, display={"format": "currency", "currency": "usd"})
    assert (v.axisY.format, v.axisY.currency) == ("currency", "USD")


def test_percent_and_plain_numbers():
    assert build(display={"format": "percent"}).axisY.format == "percent"
    plain = build()
    assert plain.axisY.format is None and plain.axisY.currency is None


def months(n, reps=("A",)):
    return [{"month": f"{2020 + i // 12}-{i % 12 + 1:02d}", "rep": r, "revenue": i} for i in range(n) for r in reps]


def test_several_series_over_24_periods_open_zoomed():
    assert build(rows=months(60, ("A", "B", "C")), group_by="rep").showDataZoom is True
    assert build(rows=months(24, ("A", "B", "C")), group_by="rep").showDataZoom is False


def test_one_series_is_zoomed_only_over_60_points():
    assert build(rows=months(60)).showDataZoom is False
    assert build(rows=months(90)).showDataZoom is True


def test_no_rows_keeps_the_old_view():
    assert build().showDataZoom is False
