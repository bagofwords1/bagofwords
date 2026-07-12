"""Unit tests for JaegerClient.

Covers the connector's contract without a live Jaeger:
- get_schemas() fixed virtual-table catalog (services/operations/spans/dependencies)
- execute_query() routing on the `table` key + JSON/dict/kwargs query shapes
- span flattening (process→service resolution, micros→datetime/ms, tag spread,
  parent-span resolution, promoted status_code/error columns)
- services / operations / dependencies table queries
- auth wiring (bearer header vs basic auth tuple, mutual exclusivity)
- Jaeger API error surfacing and test_connection() success / failure
- registry wiring (resolve_client_class)

The `requests.Session` boundary is faked via a routing table keyed by path, so
these run with no network and no `requests` calls leaving the process.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.data_sources.clients.jaeger_client import JaegerClient


# ---------- fake requests plumbing ---------- #


class _FakeResponse:
    def __init__(self, payload, status_code=200, text="", json_raises=False):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("no json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            from requests import HTTPError

            raise HTTPError(f"{self.status_code} error")


class _FakeSession:
    """Routing table keyed by path prefix. Records each call so tests can assert
    which endpoint was reached and with what params."""

    _HOST = "http://h:16686"

    def __init__(self, routes):
        self._routes = routes
        self.headers = {}
        self.auth = None
        self.verify = None
        self.calls = []  # list of (method, path, params)

    def _lookup(self, url):
        path = url.split(self._HOST, 1)[-1] if self._HOST in url else url
        # Longest-prefix match so /api/traces/{id} beats /api/traces.
        for route_path in sorted(self._routes, key=len, reverse=True):
            if path.startswith(route_path):
                return self._routes[route_path]
        raise AssertionError(f"no fake route for {path}")

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        return self._lookup(url)

    def close(self):
        pass


def _install(monkeypatch, routes):
    session = _FakeSession(routes)
    monkeypatch.setattr(
        "app.data_sources.clients.jaeger_client.requests.Session",
        lambda: session,
    )
    return session


def _ok(data):
    return _FakeResponse({"data": data, "total": len(data) if hasattr(data, "__len__") else 0, "errors": None})


def _client(**kw):
    return JaegerClient(base_url="http://h:16686", **kw)


# A two-span trace: a root HTTP span and a child DB span, across two services.
def _sample_trace():
    return {
        "traceID": "t1",
        "spans": [
            {
                "traceID": "t1", "spanID": "s1", "operationName": "HTTP GET /cart",
                "references": [], "startTime": 1_000_000, "duration": 5000,
                "processID": "p1",
                "tags": [
                    {"key": "http.method", "type": "string", "value": "GET"},
                    {"key": "http.status_code", "type": "int64", "value": 500},
                    {"key": "error", "type": "bool", "value": True},
                ],
            },
            {
                "traceID": "t1", "spanID": "s2", "operationName": "SELECT cart",
                "references": [{"refType": "CHILD_OF", "traceID": "t1", "spanID": "s1"}],
                "startTime": 1_002_000, "duration": 1500, "processID": "p2",
                "tags": [{"key": "db.system", "type": "string", "value": "postgresql"}],
            },
        ],
        "processes": {
            "p1": {"serviceName": "frontend", "tags": []},
            "p2": {"serviceName": "cart-db", "tags": []},
        },
    }


# ---------- schema ---------- #


def test_get_schemas_exposes_four_fixed_tables(monkeypatch):
    _install(monkeypatch, {"/api/services": _ok(["frontend", "cart-db"])})
    tables = _client().get_schemas()
    by_name = {t.name: t for t in tables}
    assert set(by_name) == {"services", "operations", "spans", "dependencies"}

    spans = by_name["spans"]
    col_names = [c.name for c in spans.columns]
    assert col_names[:5] == ["trace_id", "span_id", "parent_span_id", "service", "operation"]
    assert "duration_ms" in col_names and "error" in col_names
    # Discovered services enrich the description (cheap connectivity signal).
    assert "frontend" in by_name["services"].description


def test_get_schemas_survives_discovery_failure(monkeypatch):
    # A transient API error must not blank the fixed catalog.
    _install(monkeypatch, {"/api/services": _FakeResponse({"data": None, "errors": [{"msg": "boom"}]}, 503)})
    tables = _client().get_schemas()
    assert {t.name for t in tables} == {"services", "operations", "spans", "dependencies"}


# ---------- span search / flattening ---------- #


def test_span_search_flattens_one_row_per_span(monkeypatch):
    session = _install(monkeypatch, {"/api/traces": _ok([_sample_trace()])})
    df = _client().execute_query({"service": "frontend", "lookback": "2h"})

    assert len(df) == 2
    root = df[df["span_id"] == "s1"].iloc[0]
    child = df[df["span_id"] == "s2"].iloc[0]

    # process -> service resolution
    assert root["service"] == "frontend"
    assert child["service"] == "cart-db"
    # parent-span resolution via CHILD_OF reference
    assert root["parent_span_id"] is None
    assert child["parent_span_id"] == "s1"
    # micros -> ms / datetime
    assert root["duration_ms"] == 5.0
    assert pd.api.types.is_datetime64_any_dtype(df["start_time"])
    # promoted status/error columns
    assert str(root["status_code"]) == "500"
    assert bool(root["error"]) is True
    assert bool(child["error"]) is False
    # tags spread into their own columns
    assert root["http.method"] == "GET"
    assert child["db.system"] == "postgresql"

    # search hit /api/traces with the service + lookback params
    _, _, params = session.calls[-1]
    assert ("service", "frontend") in params
    assert ("lookback", "2h") in params


def test_span_search_requires_service(monkeypatch):
    _install(monkeypatch, {"/api/traces": _ok([])})
    with pytest.raises(RuntimeError, match="service"):
        _client().execute_query({"table": "spans"})


def test_query_accepts_json_string_and_kwargs(monkeypatch):
    _install(monkeypatch, {"/api/traces": _ok([_sample_trace()])})
    # JSON string form
    df1 = _client().execute_query('{"service": "frontend"}')
    assert len(df1) == 2
    # kwargs-only form
    df2 = _client().execute_query(service="frontend")
    assert len(df2) == 2


def test_tags_dict_is_json_encoded_for_api(monkeypatch):
    session = _install(monkeypatch, {"/api/traces": _ok([])})
    _client().execute_query({"service": "frontend", "tags": {"http.status_code": "500"}})
    _, _, params = session.calls[-1]
    tag_param = dict(params)["tags"] if isinstance(params, list) else None
    assert tag_param == '{"http.status_code": "500"}'


def test_trace_id_fetches_single_trace(monkeypatch):
    session = _install(monkeypatch, {"/api/traces/t1": _ok([_sample_trace()])})
    df = _client().execute_query({"trace_id": "t1"})
    assert set(df["span_id"]) == {"s1", "s2"}
    assert any("/api/traces/t1" in url for _, url, _ in session.calls)


# ---------- services / operations / dependencies ---------- #


def test_services_table(monkeypatch):
    _install(monkeypatch, {"/api/services": _ok(["b", "a"])})
    df = _client().execute_query({"table": "services"})
    assert list(df["service"]) == ["a", "b"]  # sorted


def test_operations_table_handles_object_and_string_forms(monkeypatch):
    _install(monkeypatch, {
        "/api/services/frontend/operations": _ok([{"name": "op2", "spanKind": "server"}, "op1"]),
    })
    df = _client().execute_query({"table": "operations", "service": "frontend"})
    assert list(df["operation"]) == ["op1", "op2"]
    assert set(df["service"]) == {"frontend"}


def test_operations_table_requires_service(monkeypatch):
    _install(monkeypatch, {"/api/services": _ok([])})
    with pytest.raises(RuntimeError, match="service"):
        _client().execute_query({"table": "operations"})


def test_dependencies_table(monkeypatch):
    _install(monkeypatch, {
        "/api/dependencies": _ok([{"parent": "frontend", "child": "cart-db", "callCount": 42}]),
    })
    df = _client().execute_query({"table": "dependencies", "endTs": 1719792000000, "lookback": 3600000})
    assert list(df.columns) == ["parent", "child", "call_count"]
    assert df.iloc[0]["call_count"] == 42


# ---------- auth wiring ---------- #


def test_bearer_token_sets_authorization_header(monkeypatch):
    session = _install(monkeypatch, {"/api/services": _ok([])})
    _client(token="secret").test_connection()
    assert session.headers["Authorization"] == "Bearer secret"
    assert session.auth is None  # token and basic are mutually exclusive


def test_basic_auth_sets_auth_tuple_not_header(monkeypatch):
    session = _install(monkeypatch, {"/api/services": _ok([])})
    _client(username="u", password="p").test_connection()
    assert session.auth == ("u", "p")
    assert "Authorization" not in session.headers


# ---------- errors / test_connection ---------- #


def test_api_error_is_surfaced(monkeypatch):
    _install(monkeypatch, {
        "/api/traces": _FakeResponse(
            {"data": None, "errors": [{"code": 400, "msg": "parameter 'service' is required"}]},
            status_code=400,
        )
    })
    with pytest.raises(RuntimeError, match="required"):
        _client().execute_query({"service": "x"})


def test_test_connection_success_reports_service_count(monkeypatch):
    _install(monkeypatch, {"/api/services": _ok(["frontend", "cart-db"])})
    result = _client().test_connection()
    assert result["success"] is True
    assert "2 services" in result["message"]


def test_test_connection_failure_is_reported_not_raised(monkeypatch):
    _install(monkeypatch, {
        "/api/services": _FakeResponse({"data": None, "errors": [{"msg": "server down"}]}, status_code=503)
    })
    result = _client().test_connection()
    assert result["success"] is False
    assert result["message"]


# ---------- registry wiring ---------- #


def test_registry_resolves_jaeger_client():
    from app.schemas.data_source_registry import resolve_client_class

    assert resolve_client_class("jaeger") is JaegerClient
