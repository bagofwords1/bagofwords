"""A page of charts with no answer above them gets one layout note per turn.

On unfamiliar data Haiku often stacked equal chart cards with no KPI row or
summary. The note is soft: it is returned once per turn, so a page that
should open with its charts still builds on the next attempt.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.tools.implementations import _artifact_refs  # noqa: E402
from app.ai.tools.implementations._artifact_refs import focal_point_errors  # noqa: E402

V11 = {"runtime": {"version": 11}}
CHARTS = '<TrendChart viz={v} x="m" y="r" /><RankChart viz={v} label="c" value="r" />'


def page(head="", charts=CHARTS):
    return f"function App() {{ return (<div><PageHeader title=\"Sales\" />{head}{charts}</div>); }}"


def test_a_stack_of_charts_with_no_answer_gets_the_note():
    errs = focal_point_errors(page(), V11)
    assert len(errs) == 1 and errs[0].startswith("[layout]")


def test_an_answer_above_the_charts_passes():
    for head in ('<KPICard title="Revenue" value="$1.2M" />',
                 "<p>Revenue rose 12% to $1.2M, led by Canada.</p>",
                 '<div className="text-3xl font-semibold">{total}</div>'):
        assert focal_point_errors(page(head), V11) == [], head


def test_one_chart_older_runtimes_and_kpis_defined_elsewhere_pass():
    assert focal_point_errors(page(charts='<TrendChart viz={v} x="m" y="r" />'), V11) == []
    assert focal_point_errors(page(), {"runtime": {"version": 10}}) == []
    kpis_below = page() + "\nfunction Kpis() { return <KPICard title=\"x\" value=\"1\" />; }"
    assert focal_point_errors(kpis_below, V11) == []


def test_the_note_is_given_once_per_turn():
    _artifact_refs._LAYOUT_NOTED.clear()
    assert len(focal_point_errors(page(), V11, once_key="turn-1")) == 1
    assert focal_point_errors(page(), V11, once_key="turn-1") == [], "the same turn builds on the next attempt"
    assert len(focal_point_errors(page(), V11, once_key="turn-2")) == 1, "a new turn gets the note again"


def test_an_edit_answers_only_for_removing_the_focal_point():
    without = page()
    with_kpi = page('<KPICard title="Revenue" value="$1.2M" />')
    assert focal_point_errors(without, V11, previous_code=without) == [], "the page already opened with charts"
    assert len(focal_point_errors(without, V11, previous_code=with_kpi)) == 1, "the edit removed the answer"
