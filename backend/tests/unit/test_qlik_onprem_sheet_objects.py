"""What the agent knows about a Qlik app's sheets, and what it can ask of them.

Driven through `QlikSenseOnPremClient`'s public surface (`get_schemas`,
`execute_query`) against `FakeQlikSite` — a fake QSEoW site that answers the
real Engine JSON-RPC methods over the WebSocket boundary. The demo app
(`FakeQlikSite.demo()`) has an approved sheet with every object shape (a chart
on a master measure, a KPI using a variable inside set analysis, a filter pane,
a container, a linked master visualization, a pivot table, a text box), a
community sheet, and one private sheet that must never reach the catalog.
"""
from __future__ import annotations

from typing import Dict, List

import pytest

from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.schemas.data_source_schema import DataSourceSummarySchema
from tests.fixtures.fake_qlik_engine import FakeQlikSite
from tests.unit.test_qlik_sense_onprem_client import make_client


APP = "Sales/Consumer Sales"


@pytest.fixture
def site():
    return FakeQlikSite.demo()


@pytest.fixture
def client(site, monkeypatch):
    c = make_client()
    site.install(monkeypatch, c)
    yield c
    c.close()


def _by_name(tables) -> Dict[str, object]:
    return {t.name: t for t in tables}


def _columns(table) -> Dict[str, object]:
    return {c.name: c for c in table.columns}


def _sheet(tables, title):
    return _by_name(tables)[f"{APP}/Sheets/{title}"]


def _all_text(tables) -> str:
    """Everything a catalog entry carries that could reach a prompt or a UI."""
    parts: List[str] = []
    for t in tables:
        parts.append(t.name)
        parts.append(repr(t.metadata_json))
        for c in t.columns:
            parts.extend([c.name, c.description or "", repr(c.metadata)])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Catalog: sheets and their objects
# ---------------------------------------------------------------------------


class TestSheetCatalog:
    def test_every_shared_sheet_is_catalogued_with_its_data_objects(self, client):
        tables = client.get_schemas()
        names = set(_by_name(tables))
        assert f"{APP}/Sheets/Revenue Overview" in names
        assert f"{APP}/Sheets/Regional Deep Dive" in names  # community sheet

        overview = _sheet(tables, "Revenue Overview")
        meta = overview.metadata_json["qlik_sense_onprem"]
        assert meta["objectType"] == "sheet"
        assert meta["sheetId"] == "sheet-overview"
        assert meta["objectCount"] == len(overview.columns) > 0

    def test_private_sheets_never_reach_the_catalog(self, client):
        """A sheet that is neither approved nor published is one user's own
        work; the catalog is read by every member of the connection."""
        tables = client.get_schemas()
        text = _all_text(tables)
        assert "Private scratch" not in text
        assert "Unreviewed margin" not in text
        assert "0.37" not in text
        assert not any("/Sheets/Private" in t.name for t in tables)

    def test_each_object_says_what_it_groups_by_and_computes(self, client):
        cols = _columns(_sheet(client.get_schemas(), "Revenue Overview"))
        kpi = cols["Current Year Revenue"]
        assert kpi.dtype == "kpi"
        assert "id=obj-cy-kpi" in kpi.description
        assert "Sum({<[Year]={$(vCurrentYear)}>} [Sales Quantity]*[Sales Price])" in kpi.description
        assert kpi.metadata["variables"] == ["vCurrentYear"]

        deep_dive = _columns(_sheet(client.get_schemas(), "Regional Deep Dive"))
        avg = deep_dive["Average Price by Group"]
        assert "Avg([Sales Price])" in avg.description
        assert "Product Group" in avg.description

    def test_master_item_references_resolve_to_the_items_name(self, client):
        """A chart on a master measure stores only the item's id; the agent
        needs the name to connect the chart to the governed definition."""
        chart = _columns(_sheet(client.get_schemas(), "Revenue Overview"))["Revenue by Region"]
        assert "master measure 'Revenue'" in chart.description
        assert "mm-revenue" not in chart.description
        assert "Revenue" in chart.metadata["masterRefs"]

    def test_inline_copy_of_a_master_measure_is_named_as_that_measure(self, client):
        """The linked master visualization repeats the master expression
        inline; it is folded into a reference rather than repeated."""
        cols = _columns(_sheet(client.get_schemas(), "Revenue Overview"))
        trend = cols["Revenue Trend"]
        assert trend.metadata["extendsId"] == "mv-trend"
        assert "Year" in trend.description
        assert "master measure 'Revenue'" in trend.description

    def test_layout_objects_are_folded_or_dropped(self, client):
        cols = _columns(_sheet(client.get_schemas(), "Revenue Overview"))
        # The filter pane lists its listboxes' fields (a master dimension by name).
        pane = next(c for c in cols.values() if c.dtype == "filterpane")
        assert pane.name == "Filter pane: Year, Region"
        assert pane.metadata["listFields"] == ["Year", "Region"]
        assert not any(c.dtype == "listbox" for c in cols.values())
        # A container's tab is catalogued, the container itself is not.
        assert cols["Orders by Year"].metadata["container"] == "Detail tabs"
        assert not any(c.dtype == "container" for c in cols.values())
        # A text box computes nothing.
        assert "About this sheet" not in cols

    def test_variables_and_master_visualizations_are_master_item_columns(self, client):
        master = _by_name(client.get_schemas())[f"{APP}/Master Items"]
        cols = _columns(master)
        assert cols["vCurrentYear"].dtype == "variable"
        assert "2024" in cols["vCurrentYear"].description
        assert "$(vCurrentYear)" in cols["vCurrentYear"].description
        assert cols["Revenue Trend"].dtype == "linechart"
        meta = master.metadata_json["qlik_sense_onprem"]
        assert meta["variableCount"] == 1
        assert meta["masterVisualizationCount"] == 1

    def test_sheet_objects_can_be_turned_off(self, site, monkeypatch):
        client = make_client(include_sheet_objects=False)
        site.install(monkeypatch, client)
        tables = client.get_schemas()
        assert not any("/Sheets/" in t.name for t in tables)
        assert "GetFullPropertyTree" not in site.methods()
        client.close()

    def test_the_rendered_schema_carries_objects_and_variables(self, client):
        """What reaches the agent is the rendered schema, not the Table objects."""
        tables = client.get_schemas()
        ctx = TablesSchemaContext(data_sources=[TablesSchemaContext.DataSource(
            info=DataSourceSummarySchema(id="1", name="Qlik", type="qlik_sense_onprem"),
            tables=tables,
        )])
        out = ctx.render("full")
        assert 'objectType="sheet"' in out
        assert 'sheetDescription="Top-line revenue by region and year"' in out
        assert "id=obj-rev-region" in out
        assert "Sum({&lt;[Year]={$(vCurrentYear)}&gt;}" in out or "Sum({<[Year]={$(vCurrentYear)}>}" in out
        assert 'name="vCurrentYear" dtype="variable"' in out


class TestLineage:
    def test_lineage_is_read_from_the_engines_wrapped_reply(self, client):
        """GetLineage answers {"qLineage": [...]}; the sources must survive."""
        tables = client.get_schemas()
        data_table = _by_name(tables)[f"{APP}/Sales"]
        assert data_table.metadata_json["qlik_sense_onprem"]["lineageSources"] == ["lib://Demo/sales.qvd"]
        master = _by_name(tables)[f"{APP}/Master Items"]
        assert master.metadata_json["qlik_sense_onprem"]["lineage"] == [{"source": "lib://Demo/sales.qvd"}]


# ---------------------------------------------------------------------------
# Queries: an existing object's data
# ---------------------------------------------------------------------------


class TestObjectQuery:
    def test_object_query_returns_the_charts_own_numbers(self, client):
        """Same dimensions, measures and sort as the chart (descending by the
        master measure), equal to the hand-written cube."""
        df = client.execute_query(app=APP, object_id="obj-rev-region")
        assert list(df.columns) == ["Region Name", "Revenue"]
        assert list(df["Revenue"]) == sorted(df["Revenue"], reverse=True)

        expected = client.execute_query(
            app=APP, dimensions=["Region Name"],
            measures=[{"expr": "Sum([Sales Quantity]*[Sales Price])", "alias": "Revenue"}],
        )
        assert dict(zip(df["Region Name"], df["Revenue"], strict=True)) == dict(zip(expected["Region Name"], expected["Revenue"], strict=True))

    def test_filters_apply_to_an_object_query(self, client):
        everything = client.execute_query(app=APP, object_id="obj-rev-region")
        one_year = client.execute_query(app=APP, object_id="obj-rev-region", filters={"Year": [2023]})
        assert one_year["Revenue"].sum() < everything["Revenue"].sum()
        expected = client.execute_query(
            app=APP, dimensions=["Region Name"], filters={"Year": [2023]},
            measures=[{"expr": "Sum([Sales Quantity]*[Sales Price])", "alias": "Revenue"}],
        )
        assert one_year["Revenue"].sum() == expected["Revenue"].sum()

    def test_a_kpi_with_a_variable_in_set_analysis_is_computed(self, client):
        df = client.execute_query(app=APP, object_id="obj-cy-kpi")
        assert list(df.columns) == ["CY Revenue"]
        manual = client.execute_query(
            app=APP, filters={"Year": [2024]},
            measures=[{"expr": "Sum([Sales Quantity]*[Sales Price])", "alias": "x"}],
        )
        assert df.iloc[0]["CY Revenue"] == manual.iloc[0]["x"]

    def test_a_pivot_table_is_read_as_flat_rows(self, client):
        """Pivot cubes cannot be paged with GetHyperCubeData; the object is
        read in straight mode with the same dimensions and measures."""
        df = client.execute_query(app=APP, object_id="obj-mix")
        assert list(df.columns) == ["Product Group", "Region Name", "Quantity"]
        assert len(df) == 3 * 4

    def test_a_linked_master_visualization_is_queryable_through_the_sheet_copy(self, client):
        df = client.execute_query(app=APP, object_id="obj-trend-link")
        assert list(df.columns) == ["Year", "Revenue"]
        assert sorted(df["Year"]) == ["2023", "2024"] or sorted(df["Year"]) == [2023, 2024]

    def test_a_listbox_returns_its_values(self, client):
        df = client.execute_query(app=APP, object_id="lb-region")
        assert sorted(df.iloc[:, 0]) == ["Midwest", "Northeast", "South", "West"]

    def test_a_container_points_at_its_children(self, client):
        with pytest.raises(ValueError, match="obj-orders-year"):
            client.execute_query(app=APP, object_id="obj-tabs")

    def test_an_unknown_object_is_a_clear_error(self, client):
        with pytest.raises(ValueError, match="not found"):
            client.execute_query(app=APP, object_id="no-such-object")

    def test_object_queries_take_no_cube_definition(self, client):
        with pytest.raises(ValueError, match="object_id"):
            client.execute_query(app=APP, object_id="obj-rev-region", dimensions=["Year"])

    def test_object_queries_run_as_the_acting_user(self, site, client):
        client.execute_query(app=APP, object_id="obj-rev-region")
        assert site.sessions[-1].user == "UserDirectory=DEMO; UserId=app.owner"


# ---------------------------------------------------------------------------
# Queries: value search and expression validation
# ---------------------------------------------------------------------------


class TestSearch:
    def test_search_names_the_field_and_the_exact_spelling(self, client):
        df = client.execute_query(app=APP, search="north")
        assert list(df.columns) == ["field", "value", "field_match_count"]
        assert ("Region Name", "Northeast") in set(zip(df["field"], df["value"], strict=True))

    def test_search_can_be_limited_to_fields(self, client):
        df = client.execute_query(app=APP, search="20", fields=["Year"])
        assert set(df["field"]) == {"Year"}
        assert set(df["value"]) == {"2023", "2024"}

    def test_no_match_is_an_empty_frame_not_an_error(self, client):
        df = client.execute_query(app=APP, search="zzz-not-a-value")
        assert df.empty and list(df.columns) == ["field", "value", "field_match_count"]

    @pytest.mark.parametrize("kwargs", [
        {"search": "  "},
        {"search": "north", "dimensions": ["Year"]},
        {"search": "north", "object_id": "obj-rev-region"},
    ])
    def test_search_rejects_mixed_or_empty_requests(self, client, kwargs):
        with pytest.raises(ValueError):
            client.execute_query(app=APP, **kwargs)


class TestExpressionCheck:
    @pytest.mark.parametrize("measure, bad", [
        ("Sum([Sales Qty])", "Sales Qty"),
        ("Sum({<[Regoin]={'West'}>} [Sales Quantity])", "Regoin"),
        ("Count(distinct [order number])", "order number"),
    ])
    def test_an_unknown_field_in_a_measure_is_an_error_not_zeros(self, client, measure, bad):
        """Qlik evaluates an unknown field as nothing — the cube would return 0."""
        with pytest.raises(ValueError, match="unknown field") as exc:
            client.execute_query(app=APP, dimensions=["Region Name"], measures=[{"expr": measure, "alias": "x"}])
        assert bad in str(exc.value)

    def test_an_unknown_dimension_is_an_error(self, client):
        with pytest.raises(ValueError, match="unknown field.*Regoin"):
            client.execute_query(app=APP, dimensions=["Regoin"], measures=[{"expr": "Sum([Sales Quantity])"}])

    @pytest.mark.parametrize("measures", [
        [{"expr": "Sum({<[Year]={$(vCurrentYear)}>} [Sales Quantity]*[Sales Price])", "alias": "CY"}],
        [{"expr": "Sum([Sales Quantity])", "alias": "Qty"}, {"expr": "Count(distinct [Order Number])", "alias": "Orders"}],
        [{"expr": "Avg([Sales Price])"}],
    ])
    def test_valid_expressions_are_not_rejected(self, client, measures):
        df = client.execute_query(app=APP, dimensions=["Product Group"], measures=measures)
        assert len(df) == 3

    def test_queries_still_run_when_the_engine_cannot_check(self, site, client, monkeypatch):
        from tests.fixtures import fake_qlik_engine

        monkeypatch.delattr(fake_qlik_engine.EngineSession, "_doc_CheckExpression")
        df = client.execute_query(app=APP, dimensions=["Region Name"], measures=[{"expr": "Sum([Sales Quantity])"}])
        assert len(df) == 4
