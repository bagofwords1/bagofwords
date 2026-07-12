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
