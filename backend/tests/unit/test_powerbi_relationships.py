"""Power BI relationship + hidden-column discovery.

Regression cover for the failure where the agent told users that a fact table
"has no field identifying an employee" and that the tables could not be joined —
while the semantic model had an active many-to-one relationship between them,
on a column the model marked hidden.

Two independent defects produced that:
  1. Relationships were only ever read from the Admin Scanner API, which needs
     tenant-admin scope. Every non-admin deployment (all OBO/delegated
     identities; service principals without the Fabric admin-portal settings)
     fell back to COLUMNSTATISTICS, which returns relationships=[] by
     construction — so `fks` was empty and the agent read that as "no joins".
  2. The admin-scan parser dropped every column marked `isHidden`. Hiding a
     surrogate/foreign key once a relationship covers the join is the standard
     modelling convention, so this removed exactly the join keys — the columns
     are queryable in DAX, `isHidden` is a report-authoring flag.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.data_sources.clients.powerbi_client import PowerBIClient


def _mk_client() -> PowerBIClient:
    c = PowerBIClient(
        tenant_id="t", client_id="c", client_secret="s", access_token="tok",
    )
    c._http = MagicMock()
    return c


# A model shaped like the one that triggered the report: a fact table joining a
# dimension on a hidden surrogate key.
RELATIONSHIPS_DF = pd.DataFrame([
    {
        "FromTable": "fact_meals", "FromColumn": "fk_entity",
        "ToTable": "dim_entity", "ToColumn": "primary_key",
        "IsActive": True, "CrossFilteringBehavior": "OneDirection",
    },
])


class TestRelationshipsViaDax:
    def test_parses_relationships_from_info_view(self):
        c = _mk_client()
        c._execute_dax_internal = MagicMock(return_value=RELATIONSHIPS_DF)

        rels = c._get_relationships_via_dax("ws", "ds")

        assert rels == [{
            "fromTable": "fact_meals", "fromColumn": "fk_entity",
            "toTable": "dim_entity", "toColumn": "primary_key",
            "crossFilteringBehavior": "OneDirection",
        }]
        assert c._info_functions_supported is True

    def test_inactive_relationships_are_dropped(self):
        """The engine ignores them unless a query opts in with USERELATIONSHIP,
        so presenting them as joinable would invite silently wrong results."""
        c = _mk_client()
        df = RELATIONSHIPS_DF.copy()
        df["IsActive"] = False
        c._execute_dax_internal = MagicMock(return_value=df)

        assert c._get_relationships_via_dax("ws", "ds") == []

    def test_string_false_is_treated_as_inactive(self):
        """executeQueries serializes booleans inconsistently across models."""
        c = _mk_client()
        df = RELATIONSHIPS_DF.copy()
        df["IsActive"] = "False"
        c._execute_dax_internal = MagicMock(return_value=df)

        assert c._get_relationships_via_dax("ws", "ds") == []

    def test_unsupported_endpoint_disables_further_attempts(self):
        """INFO functions are documented as unsupported on the JSON
        executeQueries endpoint. Cost of finding that out must be one request
        for the whole crawl, not one per dataset."""
        c = _mk_client()
        c._execute_dax_internal = MagicMock(
            side_effect=RuntimeError("DAX query failed: HTTP 400 unsupported function INFO.VIEW.RELATIONSHIPS")
        )

        assert c._get_relationships_via_dax("ws", "ds1") == []
        assert c._info_functions_supported is False

        assert c._get_relationships_via_dax("ws", "ds2") == []
        assert c._execute_dax_internal.call_count == 1  # not retried

    @pytest.mark.parametrize("status", ["HTTP 401", "HTTP 403", "HTTP 404"])
    def test_per_dataset_denial_does_not_disable_the_feature(self, status):
        """One RLS-protected model must not cost every other model in the
        tenant its relationships."""
        c = _mk_client()
        c._execute_dax_internal = MagicMock(
            side_effect=RuntimeError(f"DAX query failed: {status} forbidden")
        )

        assert c._get_relationships_via_dax("ws", "ds1") == []
        assert c._info_functions_supported is None  # still untried, not disabled

        c._execute_dax_internal = MagicMock(return_value=RELATIONSHIPS_DF)
        assert len(c._get_relationships_via_dax("ws", "ds2")) == 1

    def test_discovery_survives_relationship_failure(self):
        c = _mk_client()
        c._execute_dax_internal = MagicMock(side_effect=Exception("boom"))

        assert c._get_relationships_via_dax("ws", "ds") == []  # no raise


class TestRelationshipDaxShape:
    def test_uses_the_info_view_family_not_bare_info(self):
        """Measured against a live tenant (2026-08-02): on the JSON
        executeQueries endpoint, bare INFO.RELATIONSHIPS() / INFO.TABLES() /
        INFO.COLUMNS() return HTTP 400, while every INFO.VIEW.* equivalent
        returns 200. The whole non-admin relationship path depends on that
        distinction, so pin it — swapping in the bare form silently reverts
        Power BI to join-less schemas."""
        dax = PowerBIClient._RELATIONSHIPS_DAX

        assert "INFO.VIEW.RELATIONSHIPS()" in dax
        assert "INFO.RELATIONSHIPS()" not in dax.replace("INFO.VIEW.RELATIONSHIPS()", "")
        # Every field the parser reads must be projected by the query.
        for field in ("FromTable", "FromColumn", "ToTable", "ToColumn",
                      "IsActive", "CrossFilteringBehavior"):
            assert field in dax


class TestHiddenColumns:
    def test_admin_scan_keeps_hidden_columns(self):
        """The join key is hidden in the model; dropping it is what made the
        agent report that the fact table had no key to the dimension."""
        c = _mk_client()
        dataset = {
            "id": "ds1",
            "tables": [{
                "name": "fact_meals",
                "columns": [
                    {"name": "fk_entity", "dataType": "Int64", "isHidden": True},
                    {"name": "meal_qty", "dataType": "Double"},
                ],
            }],
            "relationships": [],
        }

        tables, _ = c._parse_admin_scan_tables(dataset)

        names = [col["name"] for col in tables[0]["columns"]]
        assert "fk_entity" in names
        assert "meal_qty" in names

    def test_hidden_flag_is_preserved(self):
        c = _mk_client()
        dataset = {
            "id": "ds1",
            "tables": [{
                "name": "fact_meals",
                "columns": [
                    {"name": "fk_entity", "dataType": "Int64", "isHidden": True},
                    {"name": "meal_qty", "dataType": "Double"},
                ],
            }],
            "relationships": [],
        }

        tables, _ = c._parse_admin_scan_tables(dataset)
        by_name = {col["name"]: col for col in tables[0]["columns"]}

        assert by_name["fk_entity"]["isHidden"] is True
        assert by_name["meal_qty"]["isHidden"] is False

    def test_internal_engine_columns_still_dropped(self):
        """Relaxing the hidden filter must not let RowNumber-<GUID> back in —
        it is not queryable and any DAX referencing it fails."""
        c = _mk_client()
        dataset = {
            "id": "ds1",
            "tables": [{
                "name": "fact_meals",
                "columns": [
                    {"name": "RowNumber-2662979B-1795-4F74-8F37-6A1BA8059B61",
                     "dataType": "Int64", "isHidden": True},
                    {"name": "meal_qty", "dataType": "Double"},
                ],
            }],
            "relationships": [],
        }

        tables, _ = c._parse_admin_scan_tables(dataset)

        assert [col["name"] for col in tables[0]["columns"]] == ["meal_qty"]


def _metadata_df(rows):
    return pd.DataFrame(rows, columns=["Kind", "Tbl", "Name", "Info1", "Info2", "Flag"])


# One UNIONed projection over INFO.VIEW.COLUMNS / .MEASURES / .RELATIONSHIPS.
MODEL_METADATA_ROWS = [
    ("C", "fact_meals", "RowNumber-2662979B-1795-4F74-8F37-6A1BA8059B61", "Integer", "RowNumber", True),
    ("C", "fact_meals", "fk_entity", "Integer", "Regular", True),
    ("C", "fact_meals", "meal_qty", "Number", "Regular", False),
    ("C", "dim_entity", "primary_key", "Integer", "Regular", True),
    ("C", "dim_entity", "entity_name", "Text", "Regular", False),
    ("M", "fact_meals", "Total Meal Qty", "Number", "", False),
    ("R", "fact_meals", "fk_entity", "dim_entity", "primary_key", True),
]


class TestModelMetadataViaDax:
    def test_single_call_returns_columns_types_measures_and_relationships(self):
        """One request per dataset — same budget as the COLUMNSTATISTICS-only
        discovery it replaces, which matters on tenants with thousands of
        semantic models against a ~120 req/min limit."""
        c = _mk_client()
        c._execute_dax_internal = MagicMock(return_value=_metadata_df(MODEL_METADATA_ROWS))

        tables, rels, reason = c._get_model_metadata_via_dax("ws", "ds")

        assert reason is None
        assert c._execute_dax_internal.call_count == 1
        by_name = {t["name"]: t for t in tables}

        fact = by_name["fact_meals"]
        cols = {col["name"]: col for col in fact["columns"]}
        assert cols["meal_qty"]["dataType"] == "Number"      # real type, not "unknown"
        assert cols["fk_entity"]["dataType"] == "Integer"
        assert cols["fk_entity"]["isHidden"] is True
        assert cols["meal_qty"]["isHidden"] is False
        assert [m["name"] for m in fact["measures"]] == ["Total Meal Qty"]
        assert fact["measures"][0]["dataType"] == "Number"
        assert rels == [{
            "fromTable": "fact_meals", "fromColumn": "fk_entity",
            "toTable": "dim_entity", "toColumn": "primary_key",
            "crossFilteringBehavior": None,
        }]

    def test_internal_columns_dropped_by_data_category(self):
        c = _mk_client()
        c._execute_dax_internal = MagicMock(return_value=_metadata_df(MODEL_METADATA_ROWS))

        tables, _, _ = c._get_model_metadata_via_dax("ws", "ds")

        fact = next(t for t in tables if t["name"] == "fact_meals")
        assert not any("RowNumber-" in col["name"] for col in fact["columns"])

    def test_inactive_relationships_excluded(self):
        c = _mk_client()
        rows = [r for r in MODEL_METADATA_ROWS if r[0] != "R"]
        rows.append(("R", "fact_meals", "fk_entity", "dim_entity", "primary_key", False))
        c._execute_dax_internal = MagicMock(return_value=_metadata_df(rows))

        _, rels, _ = c._get_model_metadata_via_dax("ws", "ds")

        assert rels == []

    def test_auto_date_tables_excluded(self):
        c = _mk_client()
        rows = list(MODEL_METADATA_ROWS)
        rows.append(("C", "LocalDateTable_abc", "Date", "DateTime", "Regular", True))
        c._execute_dax_internal = MagicMock(return_value=_metadata_df(rows))

        tables, _, _ = c._get_model_metadata_via_dax("ws", "ds")

        assert not any(t["name"].startswith("LocalDateTable") for t in tables)

    def test_unsupported_endpoint_falls_back_to_columnstatistics(self):
        """Deployments that reject INFO functions must still index columns."""
        c = _mk_client()
        c._execute_dax_internal = MagicMock(
            side_effect=RuntimeError("DAX query failed: HTTP 400 unsupported function")
        )
        c._get_tables_via_column_stats_with_reason = MagicMock(return_value=(
            [{"name": "fact_meals", "columns": [{"name": "meal_qty", "dataType": "unknown"}]}],
            [], None,
        ))

        tables, rels, reason = c.get_dataset_tables_with_reason("ws", "ds")

        assert reason is None
        assert [t["name"] for t in tables] == ["fact_meals"]
        assert c._info_functions_supported is False

    def test_rls_denial_is_reported_distinctly(self):
        """'join an RLS role' and 'get Build permission' are requests to
        different people — collapsing them sends users down the wrong path."""
        c = _mk_client()
        err = RuntimeError("DAX query failed: HTTP 401 {'code':'RLSNotAuthorizedForModel'}")
        assert "row-level-security role" in c._short_error(err)

        missing = RuntimeError("DAX query failed: HTTP 404 {'code':'PowerBIEntityNotFound'}")
        assert "no access to this semantic model" in c._short_error(missing)


class TestRelationshipKeyColumns:
    def test_missing_join_key_is_added(self):
        """COLUMNSTATISTICS may omit hidden columns. A foreign key pointing at a
        column absent from the schema is worse than no foreign key."""
        tables = [
            {"name": "fact_meals", "columns": [{"name": "meal_qty"}]},
            {"name": "dim_entity", "columns": [{"name": "entity_name"}]},
        ]
        rels = [{
            "fromTable": "fact_meals", "fromColumn": "fk_entity",
            "toTable": "dim_entity", "toColumn": "primary_key",
        }]

        PowerBIClient._add_relationship_key_columns(tables, rels)

        assert [c["name"] for c in tables[0]["columns"]] == ["meal_qty", "fk_entity"]
        assert [c["name"] for c in tables[1]["columns"]] == ["entity_name", "primary_key"]
        assert tables[0]["columns"][1]["isRelationshipKey"] is True

    def test_existing_columns_are_not_duplicated(self):
        tables = [{"name": "fact_meals", "columns": [{"name": "fk_entity", "dataType": "Int64"}]}]
        rels = [{
            "fromTable": "fact_meals", "fromColumn": "fk_entity",
            "toTable": "dim_entity", "toColumn": "primary_key",
        }]

        PowerBIClient._add_relationship_key_columns(tables, rels)

        assert len(tables[0]["columns"]) == 1
        assert tables[0]["columns"][0]["dataType"] == "Int64"  # not clobbered

    def test_unknown_table_is_ignored(self):
        tables = [{"name": "fact_meals", "columns": []}]
        rels = [{
            "fromTable": "not_indexed", "fromColumn": "a",
            "toTable": "also_missing", "toColumn": "b",
        }]

        PowerBIClient._add_relationship_key_columns(tables, rels)

        assert tables[0]["columns"] == []


class TestNonAdminPathEndToEnd:
    def test_columnstatistics_path_now_yields_relationships(self):
        """The whole point: a deployment with no admin scan must still index
        relationships. This is the path every OBO identity takes."""
        c = _mk_client()
        c._get_tables_via_column_stats_with_reason = MagicMock(return_value=(
            [
                {"name": "fact_meals", "columns": [{"name": "meal_qty", "dataType": "unknown"}]},
                {"name": "dim_entity", "columns": [{"name": "entity_name", "dataType": "unknown"}]},
            ],
            [],
            None,
        ))
        c._execute_dax_internal = MagicMock(return_value=RELATIONSHIPS_DF)

        tables, rels, reason = c.get_dataset_tables_with_reason("ws", "ds")

        assert reason is None
        assert rels == [{
            "fromTable": "fact_meals", "fromColumn": "fk_entity",
            "toTable": "dim_entity", "toColumn": "primary_key",
            "crossFilteringBehavior": "OneDirection",
        }]
        # and the hidden key the relationship joins on is now present
        fact = next(t for t in tables if t["name"] == "fact_meals")
        assert "fk_entity" in [col["name"] for col in fact["columns"]]

    def test_unreadable_dataset_does_not_fan_out_extra_calls(self):
        """A model this identity cannot read (RLS, no Build permission) must
        cost the metadata probe and nothing more — no speculative relationship
        or measure lookups on top. On a large tenant those multiply across every
        unreadable dataset and are all guaranteed to fail the same way."""
        c = _mk_client()
        c._execute_dax_internal = MagicMock(
            side_effect=RuntimeError("DAX query failed: HTTP 401 not authorized")
        )
        c._get_tables_via_column_stats_with_reason = MagicMock(
            return_value=([], [], "COLUMNSTATISTICS failed: not authorized")
        )
        c._http.get.return_value = MagicMock(status_code=401, json=lambda: {}, text="")

        tables, rels, reason = c.get_dataset_tables_with_reason("ws", "ds")

        assert tables == [] and rels == []
        assert reason
        # Exactly one DAX attempt: the metadata probe. A 401 is per-dataset, so
        # it must not disable the feature for other models either.
        assert c._execute_dax_internal.call_count == 1
        assert c._info_functions_supported is None


class TestIncrementalReloadPreservesColumnMetadata:
    """An incremental reload must not downgrade what a full index captured.

    `refresh_data_source_schema` runs the interactive Reload with
    introspection="incremental", which rebuilds every already-known dataset from
    the stored catalog via `_tables_from_prior` instead of re-introspecting it.
    That rebuild used to reduce each column to {name, dtype}, discarding the
    `metadata` that `normalize_indexed_columns` had deliberately persisted — so
    one click of Reload stripped `role=measure`/`returns` off every measure and
    `hidden` off every join key, and the stripped form was written straight back
    to the catalog. Verified live against a tenant: 118 column-metadata entries
    (9 measures, 20 hidden keys) went to 0, while `fks` survived intact.

    That matters because the connector's own system prompt tells the agent to
    invoke a measure by name rather than re-deriving it with SUM — a hand-rolled
    equivalent does not reproduce the measure's filter context and disagrees
    with the customer's own reports. An untyped column carries no such signal.
    """

    # One measure and one hidden join key — exactly what the reload dropped.
    PRIOR = {
        "FleetOps/fact_service_event": {
            "columns": [
                {"name": "svc_vehicle_key", "dtype": "Integer",
                 "metadata": {"role": "column", "hidden": True}},
                {"name": "parts_cost", "dtype": "Number",
                 "metadata": {"role": "column"}},
                {"name": "Total Parts Cost", "dtype": "measure",
                 "description": "SUM(fact_service_event[parts_cost])",
                 "metadata": {"role": "measure", "returns": "Number",
                              "expression": "SUM(fact_service_event[parts_cost])"}},
            ],
            "pks": [],
            "fks": [{
                "column": {"name": "svc_vehicle_key", "dtype": "unknown"},
                "references_name": "FleetOps/dim_vehicle",
                "references_column": {"name": "vehicle_key", "dtype": "unknown"},
            }],
            "metadata_json": {"powerbi": {
                "datasetId": "ds1", "tableName": "fact_service_event",
            }},
        },
    }

    def _rebuild(self):
        c = _mk_client()
        prior_entries = list(self.PRIOR.items())
        tables = c._tables_from_prior(
            prior_entries, {"id": "ds1", "name": "FleetOps"}, "ws1", "BOW", [],
        )
        assert len(tables) == 1
        return {col.name: col for col in tables[0].columns}

    def test_measure_keeps_role_and_return_type(self):
        cols = self._rebuild()
        measure = cols["Total Parts Cost"]
        assert measure.dtype == "measure"
        assert measure.metadata == {
            "role": "measure", "returns": "Number",
            "expression": "SUM(fact_service_event[parts_cost])",
        }
        assert measure.description == "SUM(fact_service_event[parts_cost])"

    def test_hidden_join_key_stays_flagged_hidden(self):
        cols = self._rebuild()
        assert cols["svc_vehicle_key"].metadata == {"role": "column", "hidden": True}

    def test_plain_column_keeps_its_role(self):
        cols = self._rebuild()
        assert cols["parts_cost"].metadata == {"role": "column"}

    def test_relationships_still_survive_the_rebuild(self):
        """FKs were never the casualty here — pin that the fix did not disturb
        the one thing the reload already got right."""
        c = _mk_client()
        tables = c._tables_from_prior(
            list(self.PRIOR.items()), {"id": "ds1", "name": "FleetOps"}, "ws1", "BOW", [],
        )
        fks = tables[0].fks
        assert len(fks) == 1
        assert fks[0].column.name == "svc_vehicle_key"
        assert fks[0].references_name == "FleetOps/dim_vehicle"

    def test_column_without_metadata_stays_clean(self):
        """A source supplying no metadata must not gain an empty dict — the
        renderer branches on presence, and {} would read as 'has metadata'."""
        c = _mk_client()
        prior = {"FleetOps/t": {
            "columns": [{"name": "plain", "dtype": "Text"}],
            "pks": [], "fks": [],
            "metadata_json": {"powerbi": {"datasetId": "ds1", "tableName": "t"}},
        }}
        tables = c._tables_from_prior(
            list(prior.items()), {"id": "ds1", "name": "FleetOps"}, "ws1", "BOW", [],
        )
        col = tables[0].columns[0]
        assert col.metadata is None
        assert col.description is None

    def test_orm_style_metadata_attribute_is_ignored(self):
        """On a SQLAlchemy instance `.metadata` is the declarative MetaData
        registry, not column metadata. Passing it through would fail
        TableColumn validation and abort the entire rebuild."""
        class _Ormish:
            name = "c1"
            dtype = "Text"
            description = None
            metadata = object()  # not a dict — the MetaData registry stand-in

        c = _mk_client()
        prior = {"FleetOps/t": {
            "columns": [_Ormish()], "pks": [], "fks": [],
            "metadata_json": {"powerbi": {"datasetId": "ds1", "tableName": "t"}},
        }}
        tables = c._tables_from_prior(
            list(prior.items()), {"id": "ds1", "name": "FleetOps"}, "ws1", "BOW", [],
        )
        assert tables[0].columns[0].metadata is None
