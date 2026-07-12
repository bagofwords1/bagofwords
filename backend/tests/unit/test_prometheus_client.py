"""Unit tests for PrometheusClient.

Covers the connector's contract without a live Prometheus:
- HTTP method selection (GET for discovery endpoints, POST for query/series)
- auth wiring (bearer header, basic auth tuple, X-Scope-OrgID tenant header)
- get_schemas() metric->Table shaping (labels + timestamp + value columns,
  metadata type/help, metric_prefix filtering)
- execute_query() instant (vector), range (matrix) and scalar flattening
- Prometheus API error surfacing
- test_connection() success / failure
- registry wiring (resolve_client_class)

The `requests.Session` boundary is faked via a routing table keyed by path, so
these run with no network and no `requests` calls leaving the process.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.data_sources.clients.prometheus_client import PrometheusClient


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
    """Routing table keyed by path. Records every call so tests can assert the
    HTTP method a given endpoint was reached with (a real contract: Prometheus
    rejects POST on the label/metadata endpoints with 405)."""

    def __init__(self, routes):
        self._routes = routes
        self.headers = {}
        self.auth = None
        self.verify = None
        self.calls = []  # list of (method, path, params/data)

    def _lookup(self, url):
        path = url.split(":9090", 1)[-1] if ":9090" in url else url
        for route_path, resp in self._routes.items():
            if path.startswith(route_path):
                return resp
        raise AssertionError(f"no fake route for {path}")

    def get(self, url, params=None, timeout=None):
        self.calls.append(("GET", url, params))
        return self._lookup(url)

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        return self._lookup(url)

    def close(self):
        pass


def _install(monkeypatch, routes):
    session = _FakeSession(routes)
    monkeypatch.setattr(
        "app.data_sources.clients.prometheus_client.requests.Session",
        lambda: session,
    )
    return session


def _ok(data):
    return _FakeResponse({"status": "success", "data": data})


# ---------- discovery / schema ---------- #


def _schema_routes():
    return {
        "/api/v1/label/__name__/values": _ok(
            ["up", "node_cpu_seconds_total", "http_requests_total", "job:up:count"]
        ),
        "/api/v1/metadata": _ok(
            {
                "node_cpu_seconds_total": [
                    {"type": "counter", "help": "CPU seconds", "unit": ""}
                ],
                "up": [{"type": "gauge", "help": "target up", "unit": ""}],
            }
        ),
        "/api/v1/series": _ok(
            [
                {"__name__": "up", "job": "prometheus", "instance": "a"},
                {"__name__": "node_cpu_seconds_total", "cpu": "0", "mode": "idle"},
                {"__name__": "http_requests_total", "handler": "/", "code": "200"},
                {"__name__": "job:up:count", "job": "prometheus"},
            ]
        ),
    }


def test_get_schemas_models_each_metric_as_a_table(monkeypatch):
    _install(monkeypatch, _schema_routes())
    tables = PrometheusClient(base_url="http://h:9090").get_schemas()

    by_name = {t.name: t for t in tables}
    # Every discovered metric name becomes a table.
    assert set(by_name) == {"up", "node_cpu_seconds_total", "http_requests_total", "job:up:count"}

    node = by_name["node_cpu_seconds_total"]
    col_names = [c.name for c in node.columns]
    # Labels become columns; every metric gets synthetic timestamp + value.
    assert {"cpu", "mode"}.issubset(set(col_names))
    assert col_names[-2:] == ["timestamp", "value"]
    # __name__ is the table name, never a column.
    assert "__name__" not in col_names
    # Metadata type/help propagate.
    assert node.description == "CPU seconds"
    assert node.metadata_json["metric_type"] == "counter"


def test_metric_prefix_filters_discovery(monkeypatch):
    _install(monkeypatch, _schema_routes())
    tables = PrometheusClient(base_url="http://h:9090", metric_prefix="node_").get_schemas()
    names = {t.name for t in tables}
    assert names == {"node_cpu_seconds_total"}


def test_discovery_uses_get_but_series_uses_post(monkeypatch):
    session = _install(monkeypatch, _schema_routes())
    PrometheusClient(base_url="http://h:9090").get_schemas()
    methods = {path.split(":9090")[-1].split("?")[0]: method
               for method, path, _ in session.calls}
    # Label/metadata endpoints are GET-only in Prometheus (POST => 405);
    # /series form-encodes potentially large match[] payloads via POST.
    assert methods["/api/v1/label/__name__/values"] == "GET"
    assert methods["/api/v1/metadata"] == "GET"
    assert methods["/api/v1/series"] == "POST"


# ---------- querying ---------- #


def test_instant_vector_flattens_to_one_row_per_series(monkeypatch):
    routes = {
        "/api/v1/query": _ok(
            {
                "resultType": "vector",
                "result": [
                    {"metric": {"__name__": "up", "job": "a"}, "value": [1000, "1"]},
                    {"metric": {"__name__": "up", "job": "b"}, "value": [1000, "0"]},
                ],
            }
        )
    }
    _install(monkeypatch, routes)
    df = PrometheusClient(base_url="http://h:9090").execute_query("up")
    assert len(df) == 2
    assert {"job", "timestamp", "value"}.issubset(df.columns)
    # value is numeric, timestamp is datetime
    assert pd.api.types.is_numeric_dtype(df["value"])
    assert pd.api.types.is_datetime64_any_dtype(df["timestamp"])
    assert set(df["value"]) == {0.0, 1.0}


def test_range_matrix_explodes_samples(monkeypatch):
    routes = {
        "/api/v1/query_range": _ok(
            {
                "resultType": "matrix",
                "result": [
                    {
                        "metric": {"job": "a"},
                        "values": [[1000, "1"], [1060, "2"], [1120, "3"]],
                    }
                ],
            }
        )
    }
    session = _install(monkeypatch, routes)
    df = PrometheusClient(base_url="http://h:9090").execute_query(
        "rate(x[5m])", start="1000", end="1120", step="60s"
    )
    # One row per (series, sample).
    assert len(df) == 3
    assert list(df["value"]) == [1.0, 2.0, 3.0]
    # Range query goes to query_range via POST.
    assert any("/api/v1/query_range" in p and m == "POST" for m, p, _ in session.calls)


def test_scalar_result_is_single_row(monkeypatch):
    routes = {"/api/v1/query": _ok({"resultType": "scalar", "result": [1000, "42"]})}
    _install(monkeypatch, routes)
    df = PrometheusClient(base_url="http://h:9090").execute_query("scalar(count(up))")
    assert len(df) == 1
    assert df["value"].iloc[0] == 42.0


def test_api_error_is_surfaced(monkeypatch):
    routes = {
        "/api/v1/query": _FakeResponse(
            {"status": "error", "errorType": "bad_data", "error": "parse error: unexpected }"},
            status_code=400,
        )
    }
    _install(monkeypatch, routes)
    with pytest.raises(RuntimeError, match="parse error"):
        PrometheusClient(base_url="http://h:9090").execute_query("up}")


# ---------- auth wiring ---------- #


def test_bearer_token_sets_authorization_header(monkeypatch):
    session = _install(monkeypatch, {"/api/v1/query": _ok({"resultType": "scalar", "result": [0, "1"]})})
    PrometheusClient(base_url="http://h:9090", token="secret", org_id="tenant-7").test_connection()
    assert session.headers["Authorization"] == "Bearer secret"
    assert session.headers["X-Scope-OrgID"] == "tenant-7"
    assert session.auth is None  # token and basic are mutually exclusive


def test_basic_auth_sets_auth_tuple_not_header(monkeypatch):
    session = _install(monkeypatch, {"/api/v1/query": _ok({"resultType": "scalar", "result": [0, "1"]})})
    PrometheusClient(base_url="http://h:9090", username="u", password="p").test_connection()
    assert session.auth == ("u", "p")
    assert "Authorization" not in session.headers


# ---------- test_connection ---------- #


def test_test_connection_success_reports_version(monkeypatch):
    routes = {
        "/api/v1/query": _ok({"resultType": "scalar", "result": [0, "1"]}),
        "/api/v1/status/buildinfo": _ok({"version": "3.13.0"}),
    }
    _install(monkeypatch, routes)
    result = PrometheusClient(base_url="http://h:9090").test_connection()
    assert result["success"] is True
    assert "3.13.0" in result["message"]


def test_test_connection_failure_is_reported_not_raised(monkeypatch):
    routes = {
        "/api/v1/query": _FakeResponse(
            {"status": "error", "errorType": "unavailable", "error": "server down"},
            status_code=503,
        )
    }
    _install(monkeypatch, routes)
    result = PrometheusClient(base_url="http://h:9090").test_connection()
    assert result["success"] is False
    assert result["message"]


# ---------- registry wiring ---------- #


def test_registry_resolves_prometheus_client():
    from app.schemas.data_source_registry import resolve_client_class

    assert resolve_client_class("prometheus") is PrometheusClient
