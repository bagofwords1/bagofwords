"""Per-user (overlay) schema context: enrichment, and what must not cross over.

The overlay decides WHICH tables and columns a user may see. The canonical
catalog describes WHAT they are. Column descriptors were previously nulled for
overlay users, so measures lost their identity and hidden join keys lost their
flag for exactly the deployments that use per-user credentials.

Enriching from the canonical row fixes that, but the canonical row was indexed
by a system identity with broader reach — so these tests pin both halves: the
structural facts DO cross, and people/artefact metadata and unreachable join
targets do NOT.
"""
from __future__ import annotations

from app.ai.context.builders.schema_context_builder import _redact_source_metadata


class TestSourceMetadataRedaction:
    def test_owner_and_report_inventory_are_withheld(self):
        meta = {
            "powerbi": {
                "datasetId": "ds-1",
                "datasetName": "FleetOps",
                "configuredBy": "owner@example.com",
                "reports": [{"id": "r1", "name": "Exec Summary"}],
            }
        }

        out = _redact_source_metadata(meta)

        assert "configuredBy" not in out["powerbi"]
        assert "reports" not in out["powerbi"]
        # Everything the agent actually needs to query survives.
        assert out["powerbi"]["datasetId"] == "ds-1"
        assert out["powerbi"]["datasetName"] == "FleetOps"

    def test_input_is_not_mutated(self):
        """The same dict is served to the shared (system-identity) path, which
        is entitled to the full metadata — redaction must not leak across."""
        meta = {"powerbi": {"datasetId": "ds-1", "configuredBy": "owner@example.com"}}

        _redact_source_metadata(meta)

        assert meta["powerbi"]["configuredBy"] == "owner@example.com"

    def test_clean_metadata_passes_through_unchanged(self):
        meta = {"powerbi": {"datasetId": "ds-1"}}
        assert _redact_source_metadata(meta) is meta

    def test_non_dict_input_is_safe(self):
        assert _redact_source_metadata(None) is None
        assert _redact_source_metadata("nope") == "nope"


class TestOverlayColumnEnrichment:
    """The enrichment logic itself, exercised the way the builder applies it."""

    WHITELIST = (
        "role", "kind", "hidden", "is_partition", "relationship_key", "returns",
        "format_string", "data_category", "display_folder", "sort_by_column",
        "summarize_by", "contents",
    )

    @staticmethod
    def _enrich(canonical_meta):
        return {
            k: canonical_meta[k]
            for k in TestOverlayColumnEnrichment.WHITELIST
            if k in canonical_meta
        } or None

    def test_structural_descriptors_cross(self):
        """Without these a measure renders as an untyped column and a hidden
        join key looks like an ordinary report field."""
        meta = {"role": "measure", "returns": "Number", "expression": "SUM(f[x])"}

        out = self._enrich(meta)

        assert out["role"] == "measure"
        assert out["returns"] == "Number"

    def test_free_text_does_not_cross(self):
        """A measure expression can name tables the user cannot see, and the
        canonical row was indexed by a broader identity."""
        meta = {"role": "measure", "expression": "SUM(RestrictedTable[Salary])"}

        out = self._enrich(meta)

        assert "expression" not in out
        assert "SUM" not in str(out)

    def test_hidden_flag_crosses(self):
        assert self._enrich({"role": "column", "hidden": True})["hidden"] is True

    def test_semantic_formatting_and_sorting_cross(self):
        out = self._enrich({
            "format_string": "$#,0.00",
            "data_category": "Currency",
            "sort_by_column": "Month Number",
        })
        assert out == {
            "format_string": "$#,0.00",
            "data_category": "Currency",
            "sort_by_column": "Month Number",
        }

    def test_no_structural_keys_yields_none(self):
        assert self._enrich({"expression": "SUM(x)"}) is None


class TestForeignKeyVisibilityFilter:
    """Relationships come from the canonical row and can point at tables absent
    from this user's catalog — disclosing the table AND handing the agent a join
    target it cannot query."""

    @staticmethod
    def _filter(fks, visible):
        return [fk for fk in (fks or [])
                if (fk.get('references_name') if isinstance(fk, dict) else None) in visible]

    def test_join_to_invisible_table_is_dropped(self):
        fks = [
            {"references_name": "FleetOps/dim_vehicle"},
            {"references_name": "FleetOps/dim_restricted"},
        ]

        kept = self._filter(fks, {"FleetOps/fact_service_event", "FleetOps/dim_vehicle"})

        assert [fk["references_name"] for fk in kept] == ["FleetOps/dim_vehicle"]

    def test_all_visible_targets_survive(self):
        fks = [{"references_name": "a"}, {"references_name": "b"}]
        assert len(self._filter(fks, {"a", "b"})) == 2

    def test_empty_and_malformed_are_safe(self):
        assert self._filter(None, {"a"}) == []
        assert self._filter([{"no_ref": 1}], {"a"}) == []
