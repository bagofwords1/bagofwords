"""Unit tests for PowerBIClient.

Covers:
- Workspace filter parsing and matching (names, IDs, case-insensitivity)
- list_workspaces filtering and early-stop behavior
- test_connection probe classification by HTTP layer:
    * 200            -> success
    * engine error   -> success (query access proven; e.g. empty model)
    * 401/403        -> permission failure
    * 404            -> skipped, next dataset probed
- Multi-dataset probing (one empty/system model can't fail the test)
- _request retry/backoff on 429
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.data_sources.clients.powerbi_client import PowerBIClient


def _mk_client(workspaces: str = None) -> PowerBIClient:
    """Client with a pre-set token so connect() never hits the network."""
    c = PowerBIClient(
        tenant_id="t", client_id="c", client_secret="s",
        access_token="tok", workspaces=workspaces,
    )
    c._http = MagicMock()
    return c


def _resp(status: int, payload=None, headers=None, text: str = ""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.headers = headers or {}
    r.text = text or (json.dumps(payload) if payload else "")
    return r


EMPTY_MODEL_ERROR = {
    "error": {
        "code": "DatasetExecuteQueriesError",
        "pbi.error": {
            "code": "DatasetExecuteQueriesError",
            "details": [
                {"code": "DetailsMessage",
                 "detail": {"type": 1, "value": "DAX Evaluate queries work only on databases which have at least one table."}},
            ],
        },
    }
}


# ---------- Workspace filter ---------- #


class TestWorkspaceFilter:
    def test_no_filter_allows_all(self):
        c = _mk_client()
        assert c._workspace_allowed({"id": "abc", "name": "Sales"})

    def test_filter_by_name_case_insensitive(self):
        c = _mk_client(workspaces="Sales, Marketing")
        assert c._workspace_allowed({"id": "1", "name": "sales"})
        assert c._workspace_allowed({"id": "2", "name": "MARKETING"})
        assert not c._workspace_allowed({"id": "3", "name": "Finance"})

    def test_filter_by_id(self):
        c = _mk_client(workspaces="11111111-2222-3333-4444-555555555555")
        assert c._workspace_allowed({"id": "11111111-2222-3333-4444-555555555555", "name": "X"})
        assert not c._workspace_allowed({"id": "other", "name": "X"})

    def test_blank_filter_string_means_no_filter(self):
        c = _mk_client(workspaces="  ,  ")
        assert c._workspace_filter == set()
        assert c._workspace_allowed({"id": "any", "name": "any"})

    def test_list_workspaces_applies_filter(self):
        c = _mk_client(workspaces="Sales")
        page = {
            "value": [
                {"id": "1", "name": "Sales", "type": "Workspace"},
                {"id": "2", "name": "Finance", "type": "Workspace"},
            ]
        }
        c._http.request.return_value = _resp(200, page)
        out = c.list_workspaces()
        assert [w["name"] for w in out] == ["Sales"]

    def test_list_workspaces_stops_early_when_filter_satisfied(self):
        c = _mk_client(workspaces="Sales")
        page1 = {
            "value": [{"id": "1", "name": "Sales"}],
            "@odata.nextLink": "https://api.powerbi.com/next",
        }
        c._http.request.return_value = _resp(200, page1)
        out = c.list_workspaces()
        assert len(out) == 1
        # nextLink must NOT be followed once every configured workspace is found
        assert c._http.request.call_count == 1


# ---------- test_connection classification ---------- #


def _wire_probe(c: PowerBIClient, workspaces, datasets_by_ws, probe_responses):
    """Wire mocks: list_workspaces/list_datasets return fixtures, executeQueries
    returns probe_responses in order."""
    c.list_workspaces = MagicMock(return_value=workspaces)
    c.list_datasets = MagicMock(side_effect=lambda ws_id: datasets_by_ws.get(ws_id, []))
    c._request = MagicMock(side_effect=probe_responses)


WS = [{"id": "ws1", "name": "Sales"}]
DS = {"ws1": [{"id": "d1", "name": "Model1"}, {"id": "d2", "name": "Model2"}]}


class TestTestConnection:
    def test_success_on_first_dataset(self):
        c = _mk_client()
        _wire_probe(c, WS, DS, [_resp(200, {"results": []})])
        out = c.test_connection()
        assert out["success"] is True
        assert "Model1" in out["message"]
        assert "Sales" in out["message"]

    def test_empty_model_passes_with_warning(self):
        """The exact production bug: an empty model must NOT fail the test —
        an engine-level error proves query access works."""
        c = _mk_client()
        _wire_probe(c, WS, DS, [
            _resp(400, EMPTY_MODEL_ERROR),
            _resp(400, EMPTY_MODEL_ERROR),
        ])
        out = c.test_connection()
        assert out["success"] is True
        assert "Query access verified" in out["message"]

    def test_empty_model_then_success_probes_next_dataset(self):
        """One empty/system model is skipped; the next dataset proves access."""
        c = _mk_client()
        _wire_probe(c, WS, DS, [
            _resp(400, EMPTY_MODEL_ERROR),
            _resp(200, {"results": []}),
        ])
        out = c.test_connection()
        assert out["success"] is True
        assert "Model2" in out["message"]

    def test_forbidden_fails_with_permission_message(self):
        c = _mk_client()
        _wire_probe(c, WS, DS, [_resp(403), _resp(403)])
        out = c.test_connection()
        assert out["success"] is False
        assert "Member or Contributor" in out["message"]

    def test_404_skipped_then_success(self):
        c = _mk_client()
        _wire_probe(c, WS, DS, [_resp(404), _resp(200, {"results": []})])
        out = c.test_connection()
        assert out["success"] is True

    def test_no_workspaces_with_filter_names_the_filter(self):
        c = _mk_client(workspaces="DoesNotExist")
        c.list_workspaces = MagicMock(return_value=[])
        out = c.test_connection()
        assert out["success"] is False
        assert "DoesNotExist" in out["message"]

    def test_no_datasets_fails(self):
        c = _mk_client()
        _wire_probe(c, WS, {"ws1": []}, [])
        out = c.test_connection()
        assert out["success"] is False
        assert "no datasets" in out["message"]

    def test_probe_budget_respected(self):
        c = _mk_client()
        many = {"ws1": [{"id": f"d{i}", "name": f"M{i}"} for i in range(20)]}
        _wire_probe(c, WS, many, [_resp(404)] * 20)
        out = c.test_connection()
        assert c._request.call_count == c.MAX_PROBE_DATASETS
        assert out["success"] is False


# ---------- _request retry ---------- #


class TestRequestRetry:
    def test_retries_on_429_then_succeeds(self):
        c = _mk_client()
        c._http.request.side_effect = [
            _resp(429, headers={"Retry-After": "0"}),
            _resp(200, {"value": []}),
        ]
        with patch("time.sleep") as sleep:
            out = c._request("GET", "https://api.powerbi.com/x")
        assert out.status_code == 200
        assert c._http.request.call_count == 2

    def test_gives_up_after_max_attempts(self):
        c = _mk_client()
        c._http.request.side_effect = [_resp(429, headers={})] * 3
        with patch("time.sleep"):
            out = c._request("GET", "https://api.powerbi.com/x", max_attempts=3)
        assert out.status_code == 429
        assert c._http.request.call_count == 3

    def test_no_retry_on_4xx(self):
        c = _mk_client()
        c._http.request.return_value = _resp(400, EMPTY_MODEL_ERROR)
        out = c._request("POST", "https://api.powerbi.com/x")
        assert out.status_code == 400
        assert c._http.request.call_count == 1


# ---------- execute_query dataset resolution ---------- #


PBI_META = {
    "powerbi": {
        "datasetId": "ds-guid-1",
        "workspaceId": "ws-guid-1",
        "workspaceName": "Sales",
        "datasetName": "SalesModel",
        "tableName": "Customers",
    }
}


class TestExecuteQueryResolution:
    """table_name -> (dataset_id, workspace_id) must resolve from the attached
    persisted metadata (a dict lookup) — NOT a live tenant re-crawl — and the
    failure message must steer the model to the schema table name, never to
    asking the user for a GUID."""

    def _client_with_map(self):
        c = _mk_client()
        c.attach_table_metadata([
            {"name": "SalesModel/Customers", "metadata_json": PBI_META},
            {"name": "SalesModel/Orders", "metadata_json": {
                "powerbi": {**PBI_META["powerbi"], "tableName": "Orders"}
            }},
            # Rows without powerbi metadata (e.g. other connectors) are ignored
            {"name": "other", "metadata_json": {"schema": "public"}},
        ])
        return c

    def test_resolves_from_attached_metadata_without_discovery(self):
        c = self._client_with_map()
        c.get_schemas = MagicMock(side_effect=AssertionError("live discovery must not run"))
        c._execute_dax_internal = MagicMock(return_value="df")
        out = c.execute_query("EVALUATE Customers", "SalesModel/Customers")
        assert out == "df"
        c._execute_dax_internal.assert_called_once_with(
            "ws-guid-1", "ds-guid-1", "EVALUATE Customers", max_rows=None
        )

    def test_resolution_is_case_insensitive(self):
        c = self._client_with_map()
        c.get_schemas = MagicMock(side_effect=AssertionError("live discovery must not run"))
        c._execute_dax_internal = MagicMock(return_value="df")
        c.execute_query("EVALUATE Customers", "salesmodel/customers")
        c._execute_dax_internal.assert_called_once()

    def test_resolves_by_internal_table_name(self):
        c = self._client_with_map()
        c.get_schemas = MagicMock(side_effect=AssertionError("live discovery must not run"))
        c._execute_dax_internal = MagicMock(return_value="df")
        c.execute_query("EVALUATE Orders", "Orders")
        args = c._execute_dax_internal.call_args[0]
        assert args[1] == "ds-guid-1"

    def test_explicit_ids_bypass_all_lookup(self):
        c = _mk_client()
        c.get_schemas = MagicMock(side_effect=AssertionError("live discovery must not run"))
        c._execute_dax_internal = MagicMock(return_value="df")
        c.execute_query("EVALUATE T", dataset_id="d-x", workspace_id="w-x")
        c._execute_dax_internal.assert_called_once_with("w-x", "d-x", "EVALUATE T", max_rows=None)

    def test_explicit_workspace_kept_when_table_name_resolves(self):
        c = self._client_with_map()
        c._execute_dax_internal = MagicMock(return_value="df")
        c.execute_query("EVALUATE Customers", "SalesModel/Customers", workspace_id="override-ws")
        args = c._execute_dax_internal.call_args[0]
        assert args[0] == "override-ws"

    def test_falls_back_to_live_discovery_when_not_in_map(self):
        c = self._client_with_map()
        from app.ai.prompt_formatters import Table
        live = Table(name="NewModel/Things", columns=[], pks=[], fks=[], is_active=True,
                     metadata_json={"powerbi": {"datasetId": "live-ds", "workspaceId": "live-ws"}})
        c.get_schemas = MagicMock(return_value=[live])
        c._execute_dax_internal = MagicMock(return_value="df")
        c.execute_query("EVALUATE Things", "NewModel/Things")
        args = c._execute_dax_internal.call_args[0]
        assert (args[0], args[1]) == ("live-ws", "live-ds")

    def test_unresolvable_table_error_names_table_and_known_tables(self):
        c = self._client_with_map()
        c.get_schemas = MagicMock(side_effect=RuntimeError("boom"))
        with pytest.raises(ValueError) as ei:
            c.execute_query("EVALUATE X", "Nope/Nothing")
        msg = str(ei.value)
        assert "Nope/Nothing" in msg
        assert "SalesModel/Customers" in msg          # known-tables hint
        assert "dataset_id is required" not in msg    # the old GUID-demanding message

    def test_missing_target_error_forbids_asking_user(self):
        c = self._client_with_map()
        with pytest.raises(ValueError) as ei:
            c.execute_query("EVALUATE X")
        msg = str(ei.value)
        assert "Do not ask the user" in msg
        assert "dataset_id is required" not in msg

    def test_empty_query_still_rejected(self):
        c = self._client_with_map()
        with pytest.raises(ValueError):
            c.execute_query("", "SalesModel/Customers")


class TestGetSchemasCache:
    def test_get_schemas_cached_per_instance(self):
        c = _mk_client()
        c.list_workspaces = MagicMock(return_value=[])
        assert c.get_schemas() == []
        assert c.get_schemas() == []
        c.list_workspaces.assert_called_once()

    def test_force_refresh_re_discovers(self):
        c = _mk_client()
        c.list_workspaces = MagicMock(return_value=[])
        c.get_schemas()
        c.get_schemas(force_refresh=True)
        assert c.list_workspaces.call_count == 2


class TestCleanDaxColumns:
    """executeQueries returns '[Measure]' and 'Table[Column]' headers; both
    must unwrap to bare column names (the old str.strip("[]") left
    'Sales[Region]' as 'Sales[Region')."""

    def test_measure_alias_unwrapped(self):
        assert PowerBIClient._clean_dax_columns(["[TotalRevenue]"]) == ["TotalRevenue"]

    def test_table_qualified_column_unwrapped(self):
        assert PowerBIClient._clean_dax_columns(
            ["Sales[Region]", "Sales[Revenue]"]
        ) == ["Region", "Revenue"]

    def test_mixed_forms(self):
        assert PowerBIClient._clean_dax_columns(
            ["Sales[Region]", "[TotalRevenue]", "Month"]
        ) == ["Region", "TotalRevenue", "Month"]

    def test_table_name_with_spaces(self):
        assert PowerBIClient._clean_dax_columns(
            ["Order Details[Unit Price]"]
        ) == ["Unit Price"]

    def test_collision_keeps_qualified_names(self):
        # Same bare column from two tables: unwrapping would create duplicate
        # DataFrame columns, so keep them qualified (dot notation).
        assert PowerBIClient._clean_dax_columns(
            ["Customers[Name]", "Products[Name]"]
        ) == ["Customers.Name", "Products.Name"]

    def test_measure_wins_bare_name_on_collision_with_column(self):
        assert PowerBIClient._clean_dax_columns(
            ["[Name]", "Customers[Name]"]
        ) == ["Name", "Customers.Name"]

    def test_unbracketed_and_malformed_names_pass_through(self):
        assert PowerBIClient._clean_dax_columns(
            ["plain", "Sales[Region", ""]
        ) == ["plain", "Sales[Region", ""]

    def test_execute_dax_uses_cleaned_columns(self):
        c = _mk_client()
        resp = MagicMock(status_code=200)
        resp.json.return_value = {
            "results": [{"tables": [{"rows": [
                {"Sales[Region]": "East", "[TotalRevenue]": 193327.0},
            ]}]}]
        }
        c._request = MagicMock(return_value=resp)
        df = c._execute_dax_internal("ws", "ds", "EVALUATE ...")
        assert list(df.columns) == ["Region", "TotalRevenue"]
