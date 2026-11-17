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


# A chart without a viz has no ⓘ (and without rows it draws an error box), so
# the page check asks for the viz its rows come from.

def test_a_chart_without_a_viz_is_flagged():
    for chart in (
        '<TrendChart rows={byMonth} x="month" y="total" unit="currency" />',
        '<RankChart rows={byRep}\n  label="rep" value="total" onClick={p => setSel(p.label)} />',
        '<TrendChart x="month" y="revenue" />',
    ):
        errs = chart_prop_errors(page(chart), DATA)
        assert len(errs) == 1, chart
        assert errs[0].startswith("[chart] <" + chart[1:chart.index(" ")] + ">")
        assert "viz={vizById(" in errs[0]


def test_a_chart_with_a_viz_prop_or_a_spread_is_not_flagged_for_its_viz():
    for chart in (
        '<TrendChart viz={sales} rows={byMonth} x="month" y="total" />',
        f'<RankChart viz={{vizById("{VID}")}} label="rep" value="revenue" />',
        '<TrendChart viz={vizById("9f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b")} x="day" y="orders" />',
        '<TrendChart {...common} x="month" y="revenue" />',
    ):
        assert chart_prop_errors(page(chart), DATA) == [], chart


def test_a_chart_named_in_a_comment_or_a_string_is_not_flagged():
    code = page(
        '{/* <TrendChart rows={old} x="month" y="total" /> */}'
        '<p title="<RankChart> ranks">x</p>'
    ) + '\n// <RankChart rows={r} label="a" value="b" />'
    assert chart_prop_errors(code, DATA) == []


def test_an_edit_flags_only_the_charts_without_a_viz_it_adds():
    before = page('<TrendChart rows={byMonth} x="month" y="total" />')
    kept = page('<h1>Sales</h1><TrendChart rows={byMonth} x="month" y="total" />')
    assert chart_prop_errors(kept, DATA, previous_code=before) == []
    added = page('<TrendChart rows={byMonth} x="month" y="total" /><RankChart rows={byRep} label="rep" value="total" />')
    errs = chart_prop_errors(added, DATA, previous_code=before)
    assert len(errs) == 1 and errs[0].startswith("[chart] <RankChart>")
    twice = page('<TrendChart rows={byMonth} x="month" y="total" /><TrendChart rows={byMonth} x="month" y="total" />')
    assert len(chart_prop_errors(twice, DATA, previous_code=before)) == 1


def test_older_runtimes_are_not_checked_for_a_viz():
    assert chart_prop_errors(page('<TrendChart rows={r} x="m" y="v" />'), {**DATA, "runtime": {"version": 10}}) == []


def test_a_page_without_visualizations_may_chart_its_own_rows():
    # Static and resource-backed pages have no viz to pass.
    for chart in (
        '<TrendChart rows={entries} x="day" y="count" />',
        '<SectionCard title="Votes"><RankChart rows={tally} label="option" value="votes" /></SectionCard>',
    ):
        for data in ({**DATA, "visualizations": []}, {"runtime": {"version": 11}}):
            assert chart_prop_errors(page(chart), data) == [], (chart, data)


def test_a_chart_inside_a_card_still_needs_its_viz():
    errs = chart_prop_errors(page(
        '<SectionCard viz={sales} rows={filtered} title="Revenue">'
        '<TrendChart rows={filtered} x="month" y="revenue" /></SectionCard>'
    ), DATA)
    assert len(errs) == 1 and errs[0].startswith("[chart] <TrendChart>")
    # Inside a card that shows the viz's ⓘ the chart is not without one.
    assert "so it has no ⓘ" not in errs[0]


def test_a_chart_of_resource_or_constant_rows_may_pass_a_null_viz():
    # Rows read with records.collection(...) or written as constants come from no
    # visualization; viz={null} draws them with no ⓘ instead of borrowing another query's.
    for chart in (
        '<TrendChart viz={null} rows={entries} x="day" y="count" />',
        '<SectionCard title="Votes"><RankChart viz={null} rows={tally} label="option" value="votes" /></SectionCard>',
    ):
        assert chart_prop_errors(page(chart), DATA) == [], chart


def test_the_error_offers_a_null_viz_for_rows_from_no_visualization():
    errs = chart_prop_errors(page('<RankChart rows={tally} label="option" value="votes" />'), DATA)
    assert len(errs) == 1, errs
    assert "viz={vizById(" in errs[0]
    assert "viz={null}" in errs[0] and "resource" in errs[0] and "constants" in errs[0], errs[0]


def test_the_authoring_docs_offer_a_null_viz_only_for_resource_or_constant_rows():
    from app.ai.tools.implementations._sandbox_context import SANDBOX_RUNTIME_PROMPT
    from app.ai.tools.implementations.edit_artifact_legacy import EDIT_VIZ_PROP_RULE

    lines = [ln for ln in SANDBOX_RUNTIME_PROMPT.splitlines() if "viz={null}" in ln]
    assert len(lines) == 1, lines
    assert "resource" in lines[0] and "constants" in lines[0], lines[0]
    # The legacy (MCP) edit prompt makes the same offer.
    assert "viz={null}" in EDIT_VIZ_PROP_RULE, EDIT_VIZ_PROP_RULE
    assert "resource" in EDIT_VIZ_PROP_RULE and "constants" in EDIT_VIZ_PROP_RULE, EDIT_VIZ_PROP_RULE


def test_the_error_quotes_the_whole_tag():
    tag = '<RankChart rows={byRep} label="rep" value="total" />'
    errs = chart_prop_errors(page(tag), DATA)
    assert len(errs) == 1 and tag in errs[0], errs


def test_text_with_an_apostrophe_before_a_chart_does_not_hide_it():
    for line in (
        "<p>Don't forget Q1</p><TrendChart rows={byMonth} x=\"month\" y=\"total\" />",
        "<h2>Rep's ranking</h2> <RankChart rows={byRep} label=\"rep\" value=\"total\" />",
        '<p>The "best" month</p><TrendChart rows={byMonth} x="month" y="total" />',
        "<a href=\"x\">see http://example.test</a><TrendChart rows={byMonth} x=\"month\" y=\"total\" />",
    ):
        errs = chart_prop_errors(page(line), DATA)
        assert len(errs) == 1 and errs[0].startswith("[chart]"), line


def test_tags_named_in_strings_comments_and_regexes_are_not_charts():
    code = page(
        "{/* <TrendChart rows={old} x=\"m\" y=\"v\" /> */}"
        "<p title=\"<RankChart> ranks\">{'<TrendChart rows={r} />'}</p>"
    ) + (
        "\nconst help = `use <RankChart rows={r} label=\"a\" value=\"b\" /> here`;"
        "\nconst clean = s => s.replace(/'/g, '');"
        "\n// <RankChart rows={r} label=\"a\" value=\"b\" />"
    )
    assert chart_prop_errors(code, DATA) == []


def test_a_comment_inside_an_attribute_does_not_hide_the_attributes_after_it():
    # An apostrophe in a comment is not a string quote.
    for handler in (
        "onClick={p => { /* don't navigate */ select(p.label); }}",
        "onClick={p => {\n  // don't navigate\n  select(p.label);\n}}",
        "onClick={p => { /* \"quoted\" */ select(p.label); }}",
    ):
        assert chart_prop_errors(page(f'<RankChart {handler} viz={{sales}} label="rep" value="revenue" />'), DATA) == [], handler
        errs = chart_prop_errors(page(f'<RankChart {handler} viz={{sales}} label="rep" value="sales" />'), DATA)
        assert len(errs) == 1 and 'value="sales"' in errs[0], (handler, errs)
        assert chart_prop_errors(page(f'<TrendChart {handler} {{...common}} x="month" y="revenue" />'), DATA) == [], handler
    # A regex with slashes in it is not a comment either.
    url = "onClick={p => open(p.url.replace(/^https?:\\/\\//, ''))}"
    assert chart_prop_errors(page(f'<RankChart {url} viz={{sales}} label="rep" value="revenue" />'), DATA) == []
    # Nor is the closing tag of an element written in an attribute.
    assert chart_prop_errors(page('<RankChart format={v => <b>{v}</b>} viz={sales} label="rep" value="revenue" />'), DATA) == []


NESTED_JSX = (
    "format={v => <a>see http://x</a>}",                     # // in nested text
    "format={v => <a>see /* the */ notes</a>}",              # /* in nested text
    "format={v => <b>Don't {v}</b>}",                        # an apostrophe in nested text
    "format={v => <b title=\"it's\">{v + '%'} isn't</b>}",   # nested {…} of its own
    "format={v => <>Don't {v}</>}",                          # a fragment
    "format={v => <span>a <i>won't</i> b</span>}",           # elements nested in nested text
    "format={v => v > 1 ? <i>it's</i> : null}",              # after a comparison
)


def test_jsx_written_in_an_attribute_does_not_hide_the_attributes_after_it():
    # JSX text is not code: a // or /* or an apostrophe in it starts nothing.
    for attr in NESTED_JSX:
        assert chart_prop_errors(page(f'<RankChart {attr} viz={{sales}} label="rep" value="revenue" />'), DATA) == [], attr
        errs = chart_prop_errors(page(f'<RankChart {attr} viz={{sales}} label="rep" value="nope" />'), DATA)
        assert len(errs) == 1 and 'value="nope"' in errs[0], (attr, errs)
        errs = chart_prop_errors(page(f'<RankChart {attr} rows={{byRep}} label="rep" value="total" />'), DATA)
        assert len(errs) == 1 and errs[0].startswith("[chart] <RankChart> has no viz prop"), (attr, errs)
        assert chart_prop_errors(page(f'<TrendChart {attr} {{...common}} x="month" y="revenue" />'), DATA) == [], attr


def test_a_chart_drawn_from_rows_is_not_said_to_draw_an_error_box():
    # rows= after a // comment between attributes is still a rows prop.
    for chart in (
        '<RankChart label="rep"\n  // from the tally\n  rows={tally} value="votes" />',
        '<RankChart label="rep" /* it\'s the tally */ rows={tally} value="votes" />',
        '<RankChart rows={tally} label="rep" value="votes" />',
    ):
        errs = chart_prop_errors(page(chart), DATA)
        assert len(errs) == 1 and errs[0].startswith("[chart] <RankChart> has no viz prop"), (chart, errs)
        assert "error box" not in errs[0], (chart, errs)
    errs = chart_prop_errors(page('<RankChart label="rep"\n  // rows={tally}\n  value="votes" />'), DATA)
    assert len(errs) == 1 and "error box" in errs[0], errs


def test_a_comment_between_attributes_is_skipped():
    for chart in (
        "<TrendChart viz={sales} /* it's monthly */ x=\"day\" y=\"revenue\" />",
        "<TrendChart viz={sales}\n  // it's monthly\n  x=\"day\" y=\"revenue\" />",
    ):
        errs = chart_prop_errors(page(chart), DATA)
        assert len(errs) == 1 and 'x="day"' in errs[0], (chart, errs)
    assert chart_prop_errors(page("<RankChart /* who's first */ viz={sales} label=\"rep\" value=\"revenue\" />"), DATA) == []


def test_a_chart_right_after_a_comment_is_still_checked():
    def lines(*body):
        return f'const sales = vizById("{VID}");\nfunction App() {{\n  return (\n' + "\n".join(body) + "\n  );\n}"

    for comment in ("// show the trend", "/* show the trend */", "// it's the trend", "/* one */ // two"):
        errs = chart_prop_errors(lines(f"    {comment}", '    <TrendChart viz={sales} x="day" y="revenue" />'), DATA)
        assert len(errs) == 1 and 'x="day"' in errs[0], (comment, errs)
        errs = chart_prop_errors(lines(f"    {comment}", '    <RankChart rows={byRep} label="rep" value="total" />'), DATA)
        assert len(errs) == 1 and errs[0].startswith("[chart] <RankChart> has no viz prop"), (comment, errs)
    # A comment does not turn a comparison into a tag.
    assert chart_prop_errors(lines("    total /* all */ <TrendChart.length"), DATA) == []


def test_a_page_in_its_script_tag_is_checked_like_bare_code():
    wrapped = '<script type="text/babel">\n' + page("<p>It's here</p><TrendChart rows={r} x=\"m\" y=\"v\" />") + "\n</script>"
    assert len(chart_prop_errors(wrapped, DATA)) == 1
    wrapped_ok = '<script type="text/babel">\n' + page('<TrendChart viz={sales} x="month" y="revenue" />') + "\n</script>"
    assert chart_prop_errors(wrapped_ok, DATA) == []


def test_the_authoring_docs_ask_for_the_viz_the_page_check_requires():
    """The prompts must not teach a chart the page check then rejects."""
    from app.ai.tools.implementations._sandbox_context import SANDBOX_RUNTIME_PROMPT
    from app.ai.tools.implementations.edit_artifact_legacy import EDIT_NEW_CHARTS_RULE, EDIT_VIZ_PROP_RULE

    lines = SANDBOX_RUNTIME_PROMPT.splitlines()
    provenance = next(ln for ln in lines if "Pass viz={vizById(" in ln)
    assert "TrendChart" in provenance and "RankChart" in provenance, provenance
    assert "no visualizations" in SANDBOX_RUNTIME_PROMPT, "the exception for pages without visualizations is not stated"
    first = next(i for i, ln in enumerate(lines) if ln.startswith("CHARTS:"))
    last = next(i for i, ln in enumerate(lines) if ln.startswith("OTHER CHARTS:"))
    # Every sentence about a chart in a card, or about filtered rows, keeps the viz.
    for line in (ln for ln in lines[first:last] if "SectionCard" in ln or "rows={filterRows(" in ln):
        assert "viz" in line, line
    # The legacy (MCP) edit prompt states the same rule.
    for rule in (EDIT_VIZ_PROP_RULE, EDIT_NEW_CHARTS_RULE):
        assert "TrendChart" in rule and "RankChart" in rule and "SectionCard" in rule and "viz=" in rule, rule


def test_the_edit_gate_runs_every_page_check_the_create_gate_runs():
    """A page edited mechanically must pass the same deterministic checks as a new one."""
    import re
    impl = BACKEND_ROOT / "app" / "ai" / "tools" / "implementations"

    def checks(path):
        import ast
        src = path.read_text(encoding="utf-8")
        imported = {
            alias.name
            for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("_artifact_refs")
            for alias in node.names
        }
        return {n for n in imported if n.endswith("_errors") and re.search(re.escape(n) + r"\(", src)}

    create, edit = checks(impl / "create_artifact.py"), checks(impl / "edit_artifact.py")
    assert {"chart_prop_errors", "file_reference_errors"} <= create
    assert create <= edit, f"edit_artifact skips {sorted(create - edit)}"
