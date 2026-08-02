"""Snowflake semantic view discovery.

`DESC SEMANTIC VIEW` returns four kinds of row: TABLE, RELATIONSHIP, DIMENSION
and METRIC/FACT. The client read only the last group, so two things were lost:

  - the logical alias -> base table mapping (and primary keys), leaving every
    dimension and metric looking like it came from one flat object
  - every relationship, so nothing indicated the view's logical tables joined

Both matter because `SEMANTIC_VIEW(view DIMENSIONS ... METRICS ...)` slices a
metric on one logical table by a dimension on another — the agent needs to know
that is legitimate.

Row shapes here are copied from live output (2026-08-02), including the JSON
string encoding of key lists.
"""
from __future__ import annotations

from app.data_sources.clients.snowflake_client import SnowflakeClient
from app.ai.context.sections.tables_schema_section import _render_semantic_model_xml
from app.ai.prompt_formatters import Table as PromptTable


LOGICAL_TABLES = {
    "DEPOTS": {
        "BASE_TABLE_DATABASE_NAME": "DEMO_SEMANTIC_DB",
        "BASE_TABLE_SCHEMA_NAME": "BOW_FIXTURES",
        "BASE_TABLE_NAME": "DIM_DEPOT",
        "PRIMARY_KEY": '["DEPOT_KEY"]',
    },
    "SERVICES": {
        "BASE_TABLE_SCHEMA_NAME": "BOW_FIXTURES",
        "BASE_TABLE_NAME": "FACT_SERVICE_EVENT",
        "PRIMARY_KEY": '["EVENT_KEY"]',
    },
}

RELATIONSHIPS = {
    "SERVICES_TO_DEPOT": {
        "FROM": "SERVICES",
        "TABLE": "SERVICES",
        "REF_TABLE": "DEPOTS",
        "FOREIGN_KEY": '["DEPOT_KEY"]',
        "REF_KEY": '["DEPOT_KEY"]',
    },
}


class TestKeyListParsing:
    def test_json_array_string_is_parsed(self):
        assert SnowflakeClient._key_list('["DEPOT_KEY"]') == ["DEPOT_KEY"]

    def test_composite_key_is_parsed(self):
        assert SnowflakeClient._key_list('["A","B"]') == ["A", "B"]

    def test_non_json_falls_back_to_raw(self):
        assert SnowflakeClient._key_list("DEPOT_KEY") == ["DEPOT_KEY"]

    def test_empty_is_empty(self):
        assert SnowflakeClient._key_list(None) == []
        assert SnowflakeClient._key_list("") == []


class TestSemanticModelMetadata:
    def test_logical_tables_map_to_base_tables(self):
        model = SnowflakeClient._build_semantic_model_meta(LOGICAL_TABLES, {})

        by_alias = {t["alias"]: t for t in model["tables"]}
        assert by_alias["DEPOTS"]["base_table"] == "BOW_FIXTURES.DIM_DEPOT"
        assert by_alias["SERVICES"]["base_table"] == "BOW_FIXTURES.FACT_SERVICE_EVENT"

    def test_primary_keys_are_captured(self):
        model = SnowflakeClient._build_semantic_model_meta(LOGICAL_TABLES, {})
        by_alias = {t["alias"]: t for t in model["tables"]}
        assert by_alias["DEPOTS"]["primary_key"] == ["DEPOT_KEY"]

    def test_relationships_resolve_both_sides(self):
        model = SnowflakeClient._build_semantic_model_meta(LOGICAL_TABLES, RELATIONSHIPS)

        assert model["relationships"] == [{
            "name": "SERVICES_TO_DEPOT",
            "from_table": "SERVICES",
            "from_columns": ["DEPOT_KEY"],
            "to_table": "DEPOTS",
            "to_columns": ["DEPOT_KEY"],
        }]

    def test_incomplete_relationship_is_skipped(self):
        """A half-described join would point the agent at nothing."""
        model = SnowflakeClient._build_semantic_model_meta(
            LOGICAL_TABLES, {"BROKEN": {"TABLE": "SERVICES"}}  # no REF_TABLE
        )
        assert model.get("relationships") in (None, [])

    def test_empty_input_yields_empty_model(self):
        assert SnowflakeClient._build_semantic_model_meta({}, {}) == {}


class TestSemanticModelRendering:
    @staticmethod
    def _table_with_model():
        model = SnowflakeClient._build_semantic_model_meta(LOGICAL_TABLES, RELATIONSHIPS)
        return PromptTable(
            name="BOW_FIXTURES.FLEET_OPS_SV",
            columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"type": "semantic_view", "semantic_model": model},
        )

    def test_joins_reach_the_prompt(self):
        out = _render_semantic_model_xml(self._table_with_model())

        assert "<semantic_model>" in out
        assert 'from_table="SERVICES"' in out
        assert 'to_table="DEPOTS"' in out
        assert 'from_columns="DEPOT_KEY"' in out

    def test_base_tables_reach_the_prompt(self):
        out = _render_semantic_model_xml(self._table_with_model())
        assert 'base_table="BOW_FIXTURES.DIM_DEPOT"' in out
        assert 'primary_key="EVENT_KEY"' in out

    def test_non_semantic_table_renders_nothing(self):
        plain = PromptTable(name="public.orders", columns=[], pks=[], fks=[], is_active=True)
        assert _render_semantic_model_xml(plain) == ""

    def test_malformed_metadata_is_safe(self):
        broken = PromptTable(
            name="x", columns=[], pks=[], fks=[], is_active=True,
            metadata_json={"semantic_model": "not-a-dict"},
        )
        assert _render_semantic_model_xml(broken) == ""
