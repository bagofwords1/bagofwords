"""What actually reaches the prompt.

Discovery can capture metadata and persistence can store it, and the agent can
still never see it: the renderer is the last gate, and it drops any column
metadata key not on an allowlist. That gap is invisible to tests asserting on
DB contents — which is why these assert on RENDERED TEXT instead.

Two renderers exist and they had diverged:
  render_combined()  primary — used by describe_tables and the planner
  render()           fallback — used whenever a focused schema context is absent

The fallback computed pks/fks and then never emitted them, so on that path the
agent saw columns with no indication the tables could be joined.
"""
from __future__ import annotations

import pytest

from app.ai.context.sections.tables_schema_section import TablesSchemaContext
from app.ai.prompt_formatters import (
    Table as PromptTable,
    TableColumn as PromptTableColumn,
    ForeignKey as PromptForeignKey,
)
from app.schemas.data_source_schema import DataSourceSummarySchema


def _fact_table() -> PromptTable:
    """A fact table shaped like a real semantic model: hidden join keys,
    measures with return types, relationships to dimensions."""
    return PromptTable(
        name="FleetOps/fact_service_event",
        columns=[
            PromptTableColumn(name="svc_depot_key", dtype="Integer",
                              metadata={"role": "column", "hidden": True}),
            PromptTableColumn(name="parts_cost", dtype="Number",
                              metadata={"role": "column"}),
            PromptTableColumn(name="Total Parts Cost", dtype="measure",
                              metadata={"role": "measure", "returns": "Number"}),
        ],
        pks=[],
        fks=[
            PromptForeignKey(
                column=PromptTableColumn(name="svc_depot_key", dtype="Integer"),
                references_name="FleetOps/dim_depot",
                references_column=PromptTableColumn(name="depot_key", dtype="Integer"),
            )
        ],
        is_active=True,
        metadata_json={"powerbi": {"datasetId": "ds-1", "tableName": "fact_service_event"}},
    )


def _ctx(table: PromptTable) -> TablesSchemaContext:
    ds = TablesSchemaContext.DataSource(
        info=DataSourceSummarySchema(id="ds-1", name="Fleet", type="powerbi"),
        tables=[table],
    )
    ctx = TablesSchemaContext()
    ctx.data_sources = [ds]
    return ctx


def _both_renderings(table: PromptTable):
    ctx = _ctx(table)
    return {
        "primary": ctx.render_combined(top_k_per_ds=10, index_limit=50),
        "fallback": ctx.render(),
    }


@pytest.mark.parametrize("path", ["primary", "fallback"])
class TestEveryRendererCarriesTheMetadata:
    """Both paths must agree — the agent does not choose which one it gets."""

    def test_relationships_are_emitted(self, path):
        out = _both_renderings(_fact_table())[path]
        assert "<fk " in out, f"{path}: no relationships in prompt"
        assert 'ref_table="FleetOps/dim_depot"' in out
        assert 'ref_column="depot_key"' in out

    def test_measures_are_identifiable(self, path):
        out = _both_renderings(_fact_table())[path]
        assert 'role="measure"' in out

    def test_measure_return_type_is_emitted(self, path):
        """A measure is invoked by name and its definition is usually
        unreadable, so the return type is the only clue to what it yields."""
        out = _both_renderings(_fact_table())[path]
        assert 'returns="Number"' in out

    def test_hidden_join_key_is_emitted_and_marked(self, path):
        """Hidden columns are where join keys live. They must appear (so the
        agent can join) and be marked (so it doesn't offer them as fields)."""
        out = _both_renderings(_fact_table())[path]
        assert "svc_depot_key" in out
        assert 'hidden="true"' in out

    def test_booleans_render_as_xml_not_python(self, path):
        out = _both_renderings(_fact_table())[path]
        assert 'hidden="True"' not in out


class TestRowLevelSecurityFlag:
    def test_rls_surfaces_when_set(self):
        """RLS filtering is undetectable from results — a filtered query returns
        200 with fewer rows — so this flag is the only signal that totals are
        scoped to the current user."""
        t = _fact_table()
        t.metadata_json = {"powerbi": {"datasetId": "ds-1", "rowLevelSecurity": True}}
        for out in _both_renderings(t).values():
            assert 'rowLevelSecurity="true"' in out

    def test_absent_when_not_row_secured(self):
        for out in _both_renderings(_fact_table()).values():
            assert "rowLevelSecurity" not in out


class TestNoRegressionForPlainTables:
    def test_table_without_metadata_still_renders(self):
        plain = PromptTable(
            name="public.orders",
            columns=[PromptTableColumn(name="id", dtype="int")],
            pks=[], fks=[], is_active=True,
        )
        for path, out in _both_renderings(plain).items():
            assert "public.orders" in out, path
            assert "<fk " not in out, f"{path}: invented a relationship"
            assert "hidden=" not in out, f"{path}: invented a hidden flag"
