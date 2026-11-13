"""TrendChart/RankChart field props are checked against the viz's columns.

A wrong literal field on the viz's own rows is a repairable wiring error. A
chart on derived rows, an expression prop or an unresolved viz is never
flagged: a false error there would burn repair calls, and for planner-authored
pages stop the page from being saved.
"""
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.ai.tools.implementations._artifact_refs import chart_prop_errors  # noqa: E402

VID = "3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"
DATA = {
    "runtime": {"version": 11},
    "visualizations": [{"id": VID, "title": "Revenue", "columns": [{"field": "month"}, {"field": "rep"}, {"field": "revenue"}]}],
}


def page(chart):
    return f'const sales = vizById("{VID}");\nfunction App() {{ return (<div>{chart}</div>); }}'


def test_a_wrong_field_on_the_viz_rows_is_flagged():
    errs = chart_prop_errors(page('<TrendChart viz={sales} x="month" y="sales" unit="currency" />'), DATA)
    assert len(errs) == 1
    assert "sales" in errs[0]
    assert all(col in errs[0] for col in ("month", "rep", "revenue"))


def test_correct_fields_pass():
    assert chart_prop_errors(page('<TrendChart viz={sales} x="month" y="revenue" series="rep" additive />'), DATA) == []


def test_filtered_rows_of_the_same_viz_are_checked():
    code = page('<RankChart viz={sales} rows={filterRows(sales.rows)} label="region" value="revenue" />')
    assert len(chart_prop_errors(code, DATA)) == 1
    code = page('<RankChart viz={sales} rows={sales.rows} label="rep" value="revenue" />')
    assert chart_prop_errors(code, DATA) == []


def test_derived_rows_are_never_flagged():
    code = page('<RankChart viz={sales} rows={byRep} label="rep" value="total" onClick={p => setSel(p.label)} />')
    assert chart_prop_errors(code, DATA) == []


def test_expression_props_and_unknown_viz_are_skipped():
    assert chart_prop_errors(page('<TrendChart viz={sales} x={xField} y="revenue" />'), DATA) == []
    other = '<TrendChart viz={vizById("9f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b")} x="nope" y="revenue" />'
    assert chart_prop_errors(page(other), DATA) == []


def test_an_inline_viz_and_an_arrow_before_the_fields_are_parsed():
    code = page(f'<TrendChart onClick={{p => go(p.x)}} viz={{vizById("{VID}")}} x="day" y="revenue" />')
    errs = chart_prop_errors(code, DATA)
    assert len(errs) == 1 and "day" in errs[0]


def test_older_runtimes_are_not_checked():
    code = page('<TrendChart viz={sales} x="month" y="sales" />')
    assert chart_prop_errors(code, {**DATA, "runtime": {"version": 10}}) == []


def test_an_identifier_bound_to_two_vizzes_is_skipped():
    other = "9f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b"
    data = {**DATA, "visualizations": DATA["visualizations"] + [{"id": other, "title": "Other", "columns": [{"field": "day"}, {"field": "orders"}]}]}
    code = (
        f'function A() {{ const viz = vizById("{VID}"); return <TrendChart viz={{viz}} x="month" y="revenue" />; }}\n'
        f'function B() {{ const viz = vizById("{other}"); return <TrendChart viz={{viz}} x="day" y="orders" />; }}'
    )
    assert chart_prop_errors(code, data) == []


def test_a_component_parameter_with_the_same_name_is_skipped():
    # `viz` here is the component's own argument, not the outer binding.
    code = (
        f'const viz = vizById("{VID}");\n'
        'function Headcount({ viz }) { return <TrendChart viz={viz} x="day" y="count" />; }\n'
        'const Card = (viz) => <RankChart viz={viz} label="team" value="count" />;\n'
        'function App() { return <Headcount viz={viz} />; }'
    )
    assert chart_prop_errors(code, DATA) == []


def test_a_wrong_field_is_still_flagged_next_to_unrelated_parameters():
    code = page('<TrendChart viz={sales} x="month" y="sales" />') + '\nconst fmt = (n) => n.toFixed(1);'
    assert len(chart_prop_errors(code, DATA)) == 1


def test_the_edit_gate_runs_every_page_check_the_create_gate_runs():
    """A page edited mechanically must pass the same deterministic checks as a new one."""
    import re
    impl = BACKEND_ROOT / "app" / "ai" / "tools" / "implementations"

    def checks(path):
        src = path.read_text(encoding="utf-8")
        imported = set()
        for m in re.finditer(r"from app\.ai\.tools\.implementations\._artifact_refs import ([^\n]+)", src):
            imported.update(n.strip() for n in m.group(1).split(","))
        return {n for n in imported if n.endswith("_errors") and re.search(re.escape(n) + r"\(", src)}

    create, edit = checks(impl / "create_artifact.py"), checks(impl / "edit_artifact.py")
    assert "chart_prop_errors" in create
    assert create <= edit, f"edit_artifact skips {sorted(create - edit)}"
