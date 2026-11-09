"""New charts leave their category labels to the renderer; saved charts keep theirs.

A chart that stored `interval=0` asked for every label, so a 60-month chart
printed 60 dates. "Not stored" means automatic, and it has to survive being read
back through the schema, or the default would quietly restore the old behaviour.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import pytest  # noqa: E402

from app.ai.tools.implementations.create_data import build_view_from_data_model  # noqa: E402
from app.schemas.view_schema import ViewSchema  # noqa: E402


def _new_chart(chart_type, title="Revenue"):
    data_model = {"type": chart_type, "series": [{"name": "Revenue", "key": "month", "value": "revenue"}]}
    return build_view_from_data_model(data_model, title=title, palette_theme="default",
                                      available_columns=["month", "revenue"])


@pytest.mark.parametrize("chart_type", ["line_chart", "bar_chart", "area_chart"])
def test_new_charts_store_no_label_spacing(chart_type):
    stored = _new_chart(chart_type).model_dump(exclude_none=True)
    axis = stored["view"]["axisX"]
    assert "interval" not in axis and "rotate" not in axis


@pytest.mark.parametrize("chart_type", ["line_chart", "bar_chart", "area_chart"])
def test_automatic_labels_survive_being_read_back(chart_type):
    stored = _new_chart(chart_type).model_dump(exclude_none=True)
    axis = ViewSchema.model_validate(stored).view.axisX
    assert axis.interval is None and axis.rotate is None


@pytest.mark.parametrize("rotate,interval", [(45, 0), (0, 3), (90, 1)])
def test_a_saved_chart_keeps_its_label_spacing(rotate, interval):
    saved = {"version": "v2", "view": {"type": "line_chart", "x": "m", "y": "v", "axisX": {"rotate": rotate, "interval": interval}}}
    axis = ViewSchema.model_validate(saved).view.axisX
    assert (axis.rotate, axis.interval) == (rotate, interval)
