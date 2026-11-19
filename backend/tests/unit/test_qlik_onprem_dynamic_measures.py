"""Dynamic charts: what a Qlik chart shows under each user choice.

A dynamic chart changes its measure when the user clicks a button, picks a
value in a variable input, or selects a value in a data island. The agent must
see every choice with its explicit formula, and must be able to run the chart
under the choice a question asks about — not the default.

Driven through `QlikSenseOnPremClient.get_schemas` / `execute_query` against
`FakeQlikSite.demo()`, whose "Dynamic Metrics" sheet has the three shapes:
a variable set by buttons and a variable input (read by a master measure with
`Pick(Match(vMetric, ...))`), "show column if" conditions on that variable, and
a chart whose formula comes from the selected row of a data island
(`$(=Only(MetricExpr))`). The fake Engine evaluates that choice logic with its
own implementation, independent of the client's resolver.
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.data_sources.clients._qix_common import expression_drivers, resolve_expression
from app.schemas.data_source_schema import DataSourceSummarySchema
from tests.fixtures.fake_qlik_engine import FakeQlikSite
from tests.unit.test_qlik_sense_onprem_client import make_client


APP = "Sales/Consumer Sales"
SHEET = f"{APP}/Sheets/Dynamic Metrics"


@pytest.fixture
def site():
    return FakeQlikSite.demo()


@pytest.fixture
def client(site, monkeypatch):
    c = make_client()
    site.install(monkeypatch, c)
    yield c
    c.close()


@pytest.fixture
def sheet(client):
    return next(t for t in client.get_schemas() if t.name == SHEET)


def _columns(table) -> Dict[str, object]:
    return {c.name: c for c in table.columns}


def _dynamic_columns(table) -> List[object]:
    return [c for c in table.columns if (c.metadata or {}).get("options")]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class TestCatalog:
    def test_switches_are_catalogued_with_what_they_set(self, sheet):
        switches = {c.name: c for c in sheet.columns if (c.metadata or {}).get("role") == "switch"}
        assert {"Metric", "Show sales", "Show units"} <= set(switches)
        input_sets = switches["Metric"].metadata["sets"]
        assert {(s["variable"], s["value"]) for s in input_sets} == {("vMetric", "Sales"), ("vMetric", "Quantity")}
        assert switches["Show units"].metadata["sets"] == [{"variable": "vMetric", "value": "Quantity"}]
        for column in switches.values():
            assert "vMetric" in column.description
            assert "options" not in column.metadata

    def test_a_chart_on_a_dynamic_master_measure_lists_each_choice_with_its_formula(self, sheet):
        chart = _columns(sheet)["Metric by Region"]
        options = {o["when"]: o for o in chart.metadata["options"]}
        assert set(options) == {"vMetric='Sales'", "vMetric='Quantity'"}
        assert options["vMetric='Quantity'"]["measures"][0]["expression"] == "Sum([Sales Quantity])"
        assert options["vMetric='Sales'"]["measures"][0]["expression"] == "Sum([Sales Quantity]*[Sales Price])"
        assert options["vMetric='Quantity'"]["run"] == {"variables": {"vMetric": "Quantity"}}
        # A label written as an expression on the choice is resolved too.
        assert options["vMetric='Quantity'"]["measures"][0]["label"] == "Units"
        assert options["vMetric='Sales'"]["measures"][0]["label"] == "Revenue"
        # The resolved formulas carry no choice logic left to guess about.
        for option in options.values():
            for measure in option["measures"]:
                assert "Pick" not in measure["expression"] and "vMetric" not in measure["expression"]

    def test_each_choice_lists_only_the_columns_it_shows(self, sheet):
        table = _columns(sheet)["Metric by Group"]
        shown = {o["when"]: {m.get("label") for m in o["measures"]} for o in table.metadata["options"]}
        assert shown["vMetric='Sales'"] == {"Revenue", "Orders"}
        assert shown["vMetric='Quantity'"] == {"Units", "Orders"}
        assert "shown only when vMetric='Quantity'" in table.description

    def test_a_data_island_choice_carries_the_formula_stored_for_it(self, site, sheet):
        chart = _columns(sheet)["Selected metric by Year"]
        island = next(iter(site.apps.values())).islands["MetricSelector"]
        options = {o["when"]: o for o in chart.metadata["options"]}
        assert len(options) == len(island)
        for row in island:
            option = options[f"Metric='{row['Metric']}'"]
            assert option["measures"][0]["expression"] == row["MetricExpr"]
            # Users select in the field the filter pane shows, not the formula field.
            assert option["run"] == {"filters": {"Metric": [row["Metric"]]}}

    def test_static_charts_get_no_options(self, client):
        tables = {t.name: t for t in client.get_schemas()}
        overview = tables[f"{APP}/Sheets/Revenue Overview"]
        for column in overview.columns:
            assert not (column.metadata or {}).get("options")
            assert "DYNAMIC" not in (column.description or "")

    def test_the_rendered_schema_tells_the_agent_how_to_run_each_choice(self, client):
        ctx = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="Qlik", type="qlik_sense_onprem"),
            tables=client.get_schemas(),
        )])
        out = ctx.render("full")
        assert "DYNAMIC" in out
        assert "object_id=&#x27;obj-dyn-bar&#x27;" in out or "object_id='obj-dyn-bar'" in out
        assert "vMetric" in out and "Quantity" in out


# ---------------------------------------------------------------------------
# Running a choice
# ---------------------------------------------------------------------------


class TestRunningAChoice:
    def test_every_listed_choice_returns_what_its_formula_returns(self, client, sheet):
        """For every dynamic object and every choice it lists, the call shown
        in the catalog returns exactly what the choice's explicit formula
        returns as a plain query — so the listed formula and the chart agree."""
        checked = 0
        for column in _dynamic_columns(sheet):
            object_id = column.metadata["objectId"]
            for option in column.metadata["options"]:
                run = option["run"]
                got = client.execute_query(app=APP, object_id=object_id, **run)
                dims = [d["field"] for d in option["dimensions"]]
                measures = [{"expr": m["expression"], "alias": m.get("label") or m["expression"]}
                            for m in option["measures"]]
                expected = client.execute_query(
                    app=APP, dimensions=dims, measures=measures, filters=run.get("filters"),
                )
                assert list(got.columns) == list(expected.columns), (object_id, option["when"])
                key = dims[0]
                got_rows = got.set_index(key).sort_index()
                expected_rows = expected.set_index(key).sort_index()
                assert got_rows.equals(expected_rows), (object_id, option["when"])
                checked += 1
        assert checked >= 6  # 2 + 2 variable choices, 2 island choices

    def test_choices_give_different_numbers(self, client):
        sales = client.execute_query(app=APP, object_id="obj-dyn-bar", variables={"vMetric": "Sales"})
        units = client.execute_query(app=APP, object_id="obj-dyn-bar", variables={"vMetric": "Quantity"})
        assert sales.iloc[:, 1].sum() != units.iloc[:, 1].sum()

    def test_without_a_choice_the_chart_shows_what_the_app_currently_shows(self, site, client):
        """The default run follows the variable's current value — and its
        "show column if" conditions are evaluated by the Engine."""
        current = next(iter(site.apps.values())).variables["vMetric"]
        default = client.execute_query(app=APP, object_id="obj-dyn-table")
        chosen = client.execute_query(app=APP, object_id="obj-dyn-table", variables={"vMetric": current})
        assert list(default.columns) == list(chosen.columns)
        assert default.equals(chosen)
        hidden = "Units" if current == "Sales" else "Revenue"
        assert hidden not in default.columns

    def test_a_choice_never_changes_the_app_for_anyone_else(self, site, client):
        before = client.execute_query(app=APP, object_id="obj-dyn-bar")
        client.execute_query(app=APP, object_id="obj-dyn-bar", variables={"vMetric": "Quantity"})
        after = client.execute_query(app=APP, object_id="obj-dyn-bar")
        assert before.equals(after)
        writes = {"SetStringValue", "SetNumValue", "SetProperties", "ApplyPatches", "DoSave", "SetVariableValue"}
        assert not writes.intersection(site.methods())

    def test_variables_apply_to_ad_hoc_queries_too(self, client):
        dynamic = "Pick(Match(vMetric,'Sales','Quantity'), Sum([Sales Quantity]*[Sales Price]), Sum([Sales Quantity]))"
        for value, plain in (("Quantity", "Sum([Sales Quantity])"), ("Sales", "Sum([Sales Quantity]*[Sales Price])")):
            got = client.execute_query(app=APP, dimensions=["Region Name"],
                                       measures=[{"expr": dynamic, "alias": "m"}], variables={"vMetric": value})
            expected = client.execute_query(app=APP, dimensions=["Region Name"],
                                            measures=[{"expr": plain, "alias": "m"}])
            assert got.equals(expected), value

    def test_hidden_columns_are_reported(self, client):
        df = client.execute_query(app=APP, object_id="obj-dyn-table", variables={"vMetric": "Quantity"})
        assert df.attrs.get("qlik_hidden_columns") == ["Revenue"]

    @pytest.mark.parametrize("variables", [["vMetric"], {"vMetric": ["Sales"]}, {"": "x"}, {"vMetric": True}])
    def test_malformed_variables_are_rejected(self, client, variables):
        with pytest.raises(ValueError, match="variables"):
            client.execute_query(app=APP, object_id="obj-dyn-bar", variables=variables)

    def test_search_takes_no_variables(self, client):
        with pytest.raises(ValueError):
            client.execute_query(app=APP, search="north", variables={"vMetric": "Sales"})


# ---------------------------------------------------------------------------
# The resolver's guarantees
# ---------------------------------------------------------------------------


class TestResolver:
    @pytest.mark.parametrize("expr", [
        "Sum([vMetric])",                               # a field that happens to share the name
        "Count({<Status={'vMetric'}>} [Order])",         # text, not a variable
        "Sum(vMetric.Amount)",                           # a qualified field
    ])
    def test_names_inside_quotes_and_fields_are_left_alone(self, expr):
        assert resolve_expression(expr, {"vMetric": "Quantity"}) == expr

    @pytest.mark.parametrize("expr", [
        "If(Sum([Sales]) > 0, Sum([Sales]), 0)",
        "Pick(Match(Region,'North','South'), 1, 2)",
        "Aggr(Sum([Sales]), [Region])",
    ])
    def test_branches_not_decided_by_a_choice_are_kept_as_written(self, expr):
        assert resolve_expression(expr, {"vMetric": "Quantity"}) == expr

    def test_values_are_quoted_the_way_qlik_quotes_them(self):
        resolved = resolve_expression("If(vName='O''Brien', 1, 2)", {"vName": "O'Brien"})
        assert resolved == "1"
        assert resolve_expression("If(vYear=2024, 1, 2)", {"vYear": 2024}) == "1"

    @pytest.mark.parametrize("outer, inner, expected", [
        ("a", "x", "Sum(A)"), ("a", "y", "Sum(B)"), ("b", "x", "Sum(C)"),
    ])
    def test_nested_choices_resolve_together(self, outer, inner, expected):
        expr = "If(vOuter='a', Pick(Match(vInner,'x','y'), Sum(A), Sum(B)), Sum(C))"
        assert resolve_expression(expr, {"vOuter": outer, "vInner": inner}) == expected

    def test_a_variable_holding_a_whole_formula_is_spliced_in(self):
        resolved = resolve_expression("$(vExpr)", {"vExpr": "Sum({<Year={2024}>} [Sales])"})
        assert resolved == "Sum({<Year={2024}>} [Sales])"

    @pytest.mark.parametrize("expr, fields", [
        ("$(=Only([Metric Expr]))", ["Metric Expr"]),
        ("$(=Only(MetricExpr)) / $(=MaxString([Divisor Expr]))", ["MetricExpr", "Divisor Expr"]),
        ("If(GetFieldSelections(Metric)='Units', Sum(A), Sum(B))", ["Metric"]),
    ])
    def test_selector_fields_are_found_inside_dollar_formulas(self, expr, fields):
        assert expression_drivers(expr)["fields"] == fields
