"""Dynamic choices must survive variable indirection and reach agent context."""
import pytest

from tests.fixtures.fake_qlik_engine import FakeQlikSite
from tests.unit.test_qlik_sense_onprem_client import make_client
from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema


@pytest.fixture(params=[0, 1, 2], ids=["direct", "one-wrapper", "two-wrappers"])
def scenario(request, monkeypatch):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    expression = (
        "Pick(Match(vMetric,'Sales','Quantity'), "
        "Sum([Sales Quantity]*[Sales Price]), Sum([Sales Quantity]))"
    )
    for depth in range(request.param):
        name = f"vWrappedMetric{depth}"
        app.variables[name] = expression
        expression = f"$({name})"
    chart = app.find_object("obj-dyn-bar")["props"]
    chart["qHyperCubeDef"]["qMeasures"] = [
        {"qDef": {"qDef": expression, "qLabel": "Metric"}}
    ]
    client = make_client()
    site.install(monkeypatch, client)
    yield client
    client.close()


def test_catalog_exposes_transitive_switch_choices(scenario):
    sheet = next(t for t in scenario.get_schemas()
                 if t.name.endswith("/Sheets/Dynamic Metrics"))
    chart = next(c for c in sheet.columns if c.name == "Metric by Region")
    choices = {
        o["run"]["variables"].get("vMetric")
        for o in (chart.metadata or {}).get("options", [])
        if "variables" in o["run"]
    }
    assert {"Sales", "Quantity"} <= choices


def test_override_reaches_transitive_variable_dependencies(scenario):
    query = {"app": "Sales/Consumer Sales", "object_id": "obj-dyn-bar"}
    before = scenario.execute_query(**query)
    actual = scenario.execute_query(**query, variables={"vMetric": "Quantity"})
    expected = scenario.execute_query(
        app=query["app"], dimensions=["Region Name"],
        measures=[{"expr": "Sum([Sales Quantity])", "alias": "Metric"}],
    )
    # Verify the fixture distinguishes the choices before testing the result.
    assert not before.equals(expected)
    assert actual.set_index("Region Name").sort_index().equals(
        expected.set_index("Region Name").sort_index()
    ), f"Override returned {actual.to_dict('list')}; expected {expected.to_dict('list')}"
    assert scenario.execute_query(**query).equals(before)


@pytest.mark.parametrize("count", [2, 8])
def test_every_catalogued_choice_reaches_agent_context(monkeypatch, count):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    choices = [f"Choice{i}" for i in range(1, count + 1)]
    app.find_object("var-metric")["props"]["alternatives"] = [
        {"value": value, "label": f"Metric option {i}"}
        for i, value in enumerate(choices, 1)
    ]
    for button, value in zip(["btn-sales", "btn-qty"], choices):
        app.find_object(button)["props"]["actions"][0]["value"] = value
    expression = (
        "Pick(Match(vMetric," + ",".join(repr(c) for c in choices) + "),"
        + ",".join(f"Sum([Sales Quantity])*{i}" for i in range(1, count + 1)) + ")"
    )
    app.find_object("obj-dyn-bar")["props"]["qHyperCubeDef"]["qMeasures"] = [
        {"qDef": {"qDef": expression, "qLabel": "Metric"}}
    ]
    client = make_client()
    site.install(monkeypatch, client)
    try:
        sheet = next(t for t in client.get_schemas()
                     if t.name.endswith("/Sheets/Dynamic Metrics"))
        chart = next(c for c in sheet.columns if c.name == "Metric by Region")
        context = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="Qlik", type="qlik_sense_onprem"),
            tables=[sheet],
        )])
        line = next(line for line in context.render("full").splitlines()
                    if 'name="Metric by Region"' in line)
        assert len(chart.metadata["options"]) == count
        missing = [o["when"] for o in chart.metadata["options"]
                   if f"when {o['when']}" not in line]
        assert not missing, f"Choices stored but absent from the agent's chart context: {missing}"
    finally:
        client.close()


@pytest.mark.parametrize("mode", ["object", "expression"])
@pytest.mark.parametrize("selector, wrapper", [("vSelection", "vFormula"), ("vMetricChoice", "vMetricFormula")])
def test_evaluated_formula_variables_switch_without_writing_app(monkeypatch, mode, selector, wrapper):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    app.variables.update({
        selector: "1",
        wrapper: f"=Pick($({selector}), 'Sum([Sales Quantity]*[Sales Price])', 'Sum([Sales Quantity])')",
    })
    app.find_object("obj-dyn-bar")["props"]["qHyperCubeDef"]["qMeasures"] = [
        {"qDef": {"qDef": f"=$({wrapper})", "qLabel": "Metric"}}
    ]
    client = make_client()
    site.install(monkeypatch, client)
    try:
        query = {"app": "Sales/Consumer Sales"}
        if mode == "object":
            query["object_id"] = "obj-dyn-bar"
        else:
            query.update(dimensions=["Region Name"], measures=[{"expr": f"$({wrapper})", "alias": "Metric"}])
        before = client.execute_query(**query)
        actual = client.execute_query(**query, variables={selector: 2})
        expected = client.execute_query(
            app=query["app"], dimensions=["Region Name"],
            measures=[{"expr": "Sum([Sales Quantity])", "alias": "Metric"}],
        )
        assert not before.equals(expected)
        assert actual.equals(expected)
        assert client.execute_query(**query).equals(before)
        assert not {"SetStringValue", "SetNumValue", "SetProperties", "ApplyPatches", "DoSave"}.intersection(site.methods())
    finally:
        client.close()


@pytest.mark.parametrize("count", [3, 8, 14])
def test_explicit_pick_branches_are_available_without_ui_switch(monkeypatch, count):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    app.variables.update({"vChoice": "1", "vFormula": "Pick($(vChoice)," + ",".join(
        "Sum([Sales Quantity])" for _ in range(count)) + ")"})
    app.find_object("obj-dyn-bar")["props"]["qHyperCubeDef"]["qMeasures"] = [
        {"qDef": {"qDef": "$(vFormula)", "qLabel": "Metric"}}
    ]
    client = make_client()
    site.install(monkeypatch, client)
    try:
        chart = next(c for t in client.get_schemas() for c in t.columns if c.name == "Metric by Region")
        assert {o["run"]["variables"]["vChoice"] for o in chart.metadata["options"]} == set(range(1, count + 1))
        assert all("UI choice not verified" in o["label"] for o in chart.metadata["options"])
    finally:
        client.close()


@pytest.mark.parametrize("issue", ["unused", "cycle", "too_deep"])
def test_unresolvable_overrides_raise_instead_of_returning_default(monkeypatch, issue):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    expression = "Sum([Sales Quantity])"
    if issue == "cycle":
        app.variables.update({"vA": "$(vB)", "vB": "$(vA) + $(vMetric)"})
        expression = "$(vA)"
    elif issue == "too_deep":
        for i in range(40):
            app.variables[f"vLayer{i}"] = f"$(vLayer{i+1})" if i < 39 else "$(vMetric)"
        expression = "$(vLayer0)"
    app.find_object("obj-dyn-bar")["props"]["qHyperCubeDef"]["qMeasures"] = [
        {"qDef": {"qDef": expression, "qLabel": "Metric"}}
    ]
    client = make_client()
    site.install(monkeypatch, client)
    try:
        with pytest.raises(ValueError):
            client.execute_query(app="Sales/Consumer Sales", object_id="obj-dyn-bar", variables={"vMetric": "Quantity"})
    finally:
        client.close()


def test_referenced_variables_survive_catalog_limits(monkeypatch):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    # Seed via the external-service fixture: many irrelevant variables precede
    # the formula dependency, which must remain complete and discoverable.
    originals = dict(app.variables)
    app.variables.clear()
    app.variables.update({f"vUnrelated{i}": "0" for i in range(120)})
    app.variables.update(originals)
    expression = "Sum({<[Region Name]={'" + "N" * 350 + "'}>} [Sales Quantity])"
    app.variables["vRequiredFormula"] = expression
    app.find_object("obj-dyn-bar")["props"]["qHyperCubeDef"]["qMeasures"] = [
        {"qDef": {"qDef": "$(vRequiredFormula)", "qLabel": "Metric"}}
    ]
    client = make_client()
    site.install(monkeypatch, client)
    try:
        master = next(t for t in client.get_schemas() if t.name.endswith("/Master Items"))
        variable = next(c for c in master.columns if c.name == "vRequiredFormula")
        assert expression in variable.description
    finally:
        client.close()


@pytest.mark.parametrize("style", ["horizontal", "vertical", None, {"label": "Choose units"}])
def test_switch_choices_do_not_depend_on_style_shape(monkeypatch, style):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    app.find_object("var-metric")["props"].update(style=style, title="")
    client = make_client()
    site.install(monkeypatch, client)
    try:
        switches = [c for t in client.get_schemas() for c in t.columns
                    if (c.metadata or {}).get("role") == "switch"]
        assert any({s.get("value") for s in c.metadata.get("sets", [])} == {"Sales", "Quantity"}
                   for c in switches)
    finally:
        client.close()


def test_expression_valued_controls_only_advertise_resolved_literal_choices(monkeypatch):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    app.find_object("var-metric")["props"]["alternatives"] = [
        {"value": {"qStringExpression": {"qExpr": "'Quantity'"}},
         "label": {"qStringExpression": {"qExpr": "'Units'"}}},
        {"value": {"qStringExpression": {"qExpr": "=If(vMetric='Sales','Quantity','Sales')"}}},
    ]
    client = make_client()
    site.install(monkeypatch, client)
    try:
        sheet = next(t for t in client.get_schemas() if t.name.endswith("/Sheets/Dynamic Metrics"))
        switch = next(c for c in sheet.columns if c.name == "Metric")
        assert any(s.get("value") == "Quantity" and s.get("label") == "Units"
                   for s in switch.metadata["sets"])
        assert any(s.get("valueExpression") and "value" not in s for s in switch.metadata["sets"])
        chart = next(c for c in sheet.columns if c.name == "Metric by Region")
        for option in chart.metadata["options"]:
            assert all(isinstance(v, (str, int, float)) for v in option["run"]["variables"].values())
            client.execute_query(app="Sales/Consumer Sales", object_id="obj-dyn-bar", **option["run"])
    finally:
        client.close()


@pytest.mark.parametrize("count", [8, 14])
def test_large_dynamic_formulas_are_referenced_without_repeating_them_per_choice(monkeypatch, count):
    site = FakeQlikSite.demo()
    app = next(iter(site.apps.values()))
    choices = [f"Metric{i}" for i in range(count)]
    app.find_object("var-metric")["props"]["alternatives"] = [
        {"value": choice, "label": choice} for choice in choices
    ]
    for button, value in zip(["btn-sales", "btn-qty"], choices):
        app.find_object(button)["props"]["actions"][0]["value"] = value
    long_formula = "Sum([Sales Quantity])" + "+0" * 600
    definition = ("Pick(Match(vMetric," + ",".join(repr(c) for c in choices) + "),"
                  + ",".join(f"({long_formula})*{i+1}" for i in range(count)) + ")")
    app.variables["vSharedFormula"] = definition
    app.find_object("obj-dyn-bar")["props"]["qHyperCubeDef"]["qMeasures"] = [
        {"qDef": {"qDef": "$(vSharedFormula)", "qLabel": "Metric"}}
    ]
    client = make_client()
    site.install(monkeypatch, client)
    try:
        tables = client.get_schemas()
        chart = next(c for t in tables for c in t.columns
                     if (c.metadata or {}).get("objectId") == "obj-dyn-bar")
        variable = next(c for t in tables for c in t.columns if c.name == "vSharedFormula")
        assert definition in variable.description
        assert "vSharedFormula" in chart.description
        assert len(chart.description) < len(definition) / 2
        for option in chart.metadata["options"]:
            assert f"when {option['when']}" in chart.description
            assert option["run"]["variables"]["vMetric"] in chart.description
    finally:
        client.close()
