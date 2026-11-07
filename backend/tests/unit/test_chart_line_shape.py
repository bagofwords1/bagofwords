"""Line and area charts are drawn straight unless a saved view says otherwise.

A smoothed curve draws values between points that are not in the data (a flat
month shown as a dip), so new chart views and generated artifact code default
to straight lines. Views saved before that keep the setting they stored.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.schemas.view_schema import AreaChartView, LineChartView, ViewSchema  # noqa: E402
from app.services.artifact_codegen import generate_echart_option_code  # noqa: E402


@pytest.mark.parametrize("view_cls", [LineChartView, AreaChartView])
def test_new_line_and_area_views_are_straight(view_cls):
    stored = ViewSchema(view=view_cls(x="month", y=["revenue", "orders"])).model_dump(exclude_none=True)
    # Stored explicitly, so a later default change can't redraw this chart.
    assert stored["view"]["smooth"] is False


@pytest.mark.parametrize("chart_type", ["line_chart", "area_chart"])
@pytest.mark.parametrize("smooth", [True, False])
def test_saved_view_keeps_its_stored_shape(chart_type, smooth):
    saved = {"version": "v2", "view": {"type": chart_type, "x": "day", "y": "total", "smooth": smooth}}
    assert ViewSchema.model_validate(saved).view.smooth is smooth


@pytest.mark.parametrize("chart_type", ["line_chart", "area_chart"])
@pytest.mark.parametrize("group_by", [None, "region"])
def test_generated_artifact_chart_code_is_straight(chart_type, group_by):
    data_model = {
        "type": chart_type,
        "series": [{"name": "Sales", "key": "week", "value": "amount"}],
        "group_by": group_by,
    }
    code = generate_echart_option_code(data_model, 2)
    assert "type: 'line'" in code
    assert "smooth: true" not in code
    assert "smooth: false" in code
