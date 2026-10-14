"""Unit tests for OpenSearchClient.

Covers:
- base_url construction (host/port/secure, full-URL host)
- index_pattern parsing (dedup, order)
- get_tables(): mapping flattening (object recursion, nested `[]` marker,
  multi-field `.keyword` subfields), system-index exclusion, alias union
- execute_query(): envelope validation, hits -> DataFrame, size defaults,
  result-window cap, aggregation flattening, SQL escape hatch
- auth / TLS parameters at the transport boundary
- test_connection() success / failure
- registry wiring (resolve_client_class, config + credentials schemas)

`requests` is mocked at the module boundary, so these run without a live
cluster.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from app.data_sources.clients.opensearch_client import OpenSearchClient


# ---------- fake transport ---------- #


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text or json.dumps(self._json)

    def json(self):
        return self._json


class _CapturingRequest:
    """Stand-in for ``requests.request`` that records calls and answers from
    a {(method, path): response_json} routing table."""

    def __init__(self, routes=None, error=None):
        self.routes = routes or {}
        self.error = error
        self.calls = []

    def __call__(self, method, url, **kwargs):
        path = "/" + url.split("://", 1)[1].split("/", 1)[1] if "/" in url.split("://", 1)[1] else "/"
        self.calls.append({"method": method, "url": url, "path": path, **kwargs})
        if self.error is not None:
            raise self.error
        if (method, path) in self.routes:
            body = self.routes[(method, path)]
            if isinstance(body, _FakeResponse):
                return body
            return _FakeResponse(200, body)
        return _FakeResponse(404, {"error": "no route"}, text="no route")


@pytest.fixture
def transport(monkeypatch):
    def _install(routes=None, error=None):
        fake = _CapturingRequest(routes, error)
        monkeypatch.setattr(
            "app.data_sources.clients.opensearch_client.requests.request", fake
        )
        return fake
    return _install


ORDERS_MAPPING = {
    "orders": {
        "mappings": {
            "properties": {
                "order_id": {"type": "keyword"},
                "total": {"type": "double"},
                "created_at": {"type": "date"},
                "title": {"type": "text",
                          "fields": {"keyword": {"type": "keyword", "ignore_above": 256}}},
                "customer": {"properties": {
                    "name": {"type": "text"},
                    "tier": {"type": "keyword"},
                }},
                "items": {"type": "nested", "properties": {
                    "sku": {"type": "keyword"},
                    "qty": {"type": "integer"},
                }},
            }
        }
    }
}


# ---------- construction ---------- #


class TestConstruction:
    def test_defaults(self):
        c = OpenSearchClient(host="localhost")
        assert c.base_url == "http://localhost:9200"

    def test_secure_and_port(self):
        c = OpenSearchClient(host="search.example", port=9243, secure=True)
        assert c.base_url == "https://search.example:9243"

    def test_full_url_host_wins(self):
        c = OpenSearchClient(host="https://search.example.com:9200/", port=1234, secure=False)
        assert c.base_url == "https://search.example.com:9200"

    def test_index_pattern_dedup_and_order(self):
        c = OpenSearchClient(host="h", index_pattern=" logs-* , orders ,logs-*, metrics ")
        assert c._patterns == ["logs-*", "orders", "metrics"]

    def test_index_pattern_empty(self):
        assert OpenSearchClient(host="h")._patterns == []
        assert OpenSearchClient(host="h", index_pattern="  ")._patterns == []


# ---------- transport params (auth / TLS) ---------- #


class TestTransportParams:
    def test_basic_auth_and_verify_forwarded(self, transport):
        fake = transport({("GET", "/"): {"version": {"number": "2.19.1"}}})
        c = OpenSearchClient(host="h", user="admin", password="secret",
                             secure=True, verify_certs=False)
        c.test_connection()
        call = fake.calls[0]
        assert call["auth"] == ("admin", "secret")
        assert call["verify"] is False
        assert call["url"].startswith("https://")

    def test_no_auth_when_no_user(self, transport):
        fake = transport({("GET", "/"): {"version": {}}})
        OpenSearchClient(host="h").test_connection()
        assert fake.calls[0]["auth"] is None


# ---------- schema discovery ---------- #


class TestGetTables:
    def _routes(self, aliases=None):
        return {
            ("GET", "/_mapping"): {**ORDERS_MAPPING,
                                   ".system-idx": {"mappings": {"properties": {"x": {"type": "keyword"}}}}},
            ("GET", "/_alias"): aliases or {"orders": {"aliases": {}}},
        }

    def test_flattens_mapping_and_excludes_system_indices(self, transport):
        transport(self._routes())
        tables = OpenSearchClient(host="h").get_tables()
        assert [t.name for t in tables] == ["orders"]
        cols = {c.name: c.dtype for c in tables[0].columns}
        # scalars
        assert cols["order_id"] == "string"
        assert cols["total"] == "number"
        assert cols["created_at"] == "datetime"
        # object recursion -> dot paths; analyzed text with no keyword
        # subfield is flagged non-aggregatable
        assert cols["customer.name"] == "string (full-text; NOT aggregatable/sortable)"
        assert cols["customer.tier"] == "string"
        # nested -> array column + [] children
        assert cols["items"] == "array"
        assert cols["items[].sku"] == "string"
        assert cols["items[].qty"] == "integer"
        # multi-field subcolumn
        assert cols["title.keyword"] == "string"
        # pk convention
        assert [p.name for p in tables[0].pks] == ["_id"]

    def test_analyzed_text_dtype_points_at_keyword_subfield(self, transport):
        # A text field WITH a keyword subfield routes aggs/sort to the
        # subfield; one WITHOUT is flagged. Mirrors the Elasticsearch client.
        transport(self._routes())
        cols = {c.name: c.dtype for c in OpenSearchClient(host="h").get_tables()[0].columns}
        assert cols["title"] == "string (full-text; aggregate/sort on title.keyword)"
        assert cols["title.keyword"] == "string"
        assert cols["customer.name"] == "string (full-text; NOT aggregatable/sortable)"

    def test_raw_types_metadata_kept(self, transport):
        transport(self._routes())
        t = OpenSearchClient(host="h").get_tables()[0]
        raw = t.metadata_json["raw_types"]
        assert raw["title"] == "text"
        assert raw["title.keyword"] == "keyword"
        assert raw["items"] == "nested"
        assert t.metadata_json["type"] == "index"

    def test_alias_surfaces_as_union_table(self, transport):
        transport(self._routes(aliases={"orders": {"aliases": {"recent_orders": {}}}}))
        tables = OpenSearchClient(host="h").get_tables()
        by_name = {t.name: t for t in tables}
        assert set(by_name) == {"orders", "recent_orders"}
        alias = by_name["recent_orders"]
        assert alias.metadata_json["type"] == "alias"
        assert alias.metadata_json["indices"] == ["orders"]
        assert {c.name for c in alias.columns} == {c.name for c in by_name["orders"].columns}

    def test_explicit_pattern_narrows_mapping_call(self, transport):
        fake = transport({
            ("GET", "/logs-*,orders/_mapping"): ORDERS_MAPPING,
            ("GET", "/_alias"): {},
        })
        tables = OpenSearchClient(host="h", index_pattern="logs-*,orders").get_tables()
        assert fake.calls[0]["path"] == "/logs-*,orders/_mapping"
        assert [t.name for t in tables] == ["orders"]

    def test_returns_empty_on_error(self, transport):
        transport(error=RuntimeError("connection refused"))
        assert OpenSearchClient(host="h").get_tables() == []

    def test_data_stream_surfaces_with_union_columns(self, transport):
        ds_mapping_1 = {"mappings": {"properties": {
            "@timestamp": {"type": "date"}, "level": {"type": "keyword"}}}}
        ds_mapping_2 = {"mappings": {"properties": {
            "@timestamp": {"type": "date"}, "trace_id": {"type": "keyword"}}}}
        transport({
            ("GET", "/_mapping"): {
                **ORDERS_MAPPING,
                # Backing indices are hidden but can leak into wide responses.
                ".ds-logs-app-000001": ds_mapping_1,
            },
            ("GET", "/_alias"): {},
            ("GET", "/_data_stream"): {"data_streams": [{
                "name": "logs-app",
                "indices": [{"index_name": ".ds-logs-app-000001"},
                            {"index_name": ".ds-logs-app-000002"}],
            }]},
            ("GET", "/logs-app/_mapping"): {
                ".ds-logs-app-000001": ds_mapping_1,
                ".ds-logs-app-000002": ds_mapping_2,
            },
        })
        tables = OpenSearchClient(host="h").get_tables()
        by_name = {t.name: t for t in tables}
        assert set(by_name) == {"orders", "logs-app"}
        stream = by_name["logs-app"]
        assert stream.metadata_json["type"] == "data_stream"
        assert stream.metadata_json["indices"] == [
            ".ds-logs-app-000001", ".ds-logs-app-000002"]
        # Union across generations of backing indices.
        assert {c.name for c in stream.columns} == {"@timestamp", "level", "trace_id"}

    def test_backing_indices_excluded_even_with_pattern(self, transport):
        ds_mapping = {"mappings": {"properties": {"@timestamp": {"type": "date"}}}}
        transport({
            # An explicit pattern can match backing indices directly...
            ("GET", "/logs-*/_mapping"): {".ds-logs-app-000001": ds_mapping},
            ("GET", "/_alias"): {},
            ("GET", "/_data_stream/logs-*"): {"data_streams": [{
                "name": "logs-app",
                "indices": [{"index_name": ".ds-logs-app-000001"}],
            }]},
            ("GET", "/logs-app/_mapping"): {".ds-logs-app-000001": ds_mapping},
        })
        tables = OpenSearchClient(host="h", index_pattern="logs-*").get_tables()
        # ...but they surface only through their stream, never as raw indices.
        assert [t.name for t in tables] == ["logs-app"]
        assert tables[0].metadata_json["type"] == "data_stream"

    def test_data_stream_discovery_failure_keeps_indices(self, transport):
        # No /_data_stream route -> 404 -> graceful "no streams".
        transport(self._routes())
        tables = OpenSearchClient(host="h").get_tables()
        assert [t.name for t in tables] == ["orders"]

    def test_bad_pattern_does_not_hide_other_streams(self, transport):
        ds_mapping = {"mappings": {"properties": {"@timestamp": {"type": "date"}}}}
        transport({
            ("GET", "/orders,logs-*/_mapping"): ORDERS_MAPPING,
            ("GET", "/_alias"): {},
            # "orders" is a plain index: GET /_data_stream/orders 404s (no route).
            ("GET", "/_data_stream/logs-*"): {"data_streams": [{
                "name": "logs-app",
                "indices": [{"index_name": ".ds-logs-app-000001"}],
            }]},
            ("GET", "/logs-app/_mapping"): {".ds-logs-app-000001": ds_mapping},
        })
        tables = OpenSearchClient(host="h", index_pattern="orders,logs-*").get_tables()
        assert {t.name for t in tables} == {"orders", "logs-app"}

    def test_get_schema_single_index(self, transport):
        transport({("GET", "/orders/_mapping"): ORDERS_MAPPING})
        t = OpenSearchClient(host="h").get_schema("orders")
        assert t.name == "orders"
        assert any(c.name == "customer.tier" for c in t.columns)

    def test_prompt_schema_renders(self, transport):
        transport(self._routes())
        out = OpenSearchClient(host="h").prompt_schema()
        assert "orders" in out and "customer.tier" in out


# ---------- query execution ---------- #


class TestExecuteQuery:
    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            OpenSearchClient(host="h").execute_query("not json")

    def test_missing_index_raises(self):
        with pytest.raises(ValueError, match="index"):
            OpenSearchClient(host="h").execute_query('{"query": {"match_all": {}}}')

    def test_hits_to_dataframe(self, transport):
        hits = {"hits": {"hits": [
            {"_id": "1", "_source": {"order_id": "o1", "total": 10.5,
                                     "customer": {"tier": "gold"}}},
            {"_id": "2", "_source": {"order_id": "o2", "total": 3.0,
                                     "customer": {"tier": "silver"}}},
        ]}}
        fake = transport({("POST", "/orders/_search"): hits})
        df = OpenSearchClient(host="h").execute_query('{"index": "orders", "query": {"match_all": {}}}')
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns)[0] == "_id"
        assert list(df["customer.tier"]) == ["gold", "silver"]
        # default size injected, timeout set
        body = fake.calls[0]["json"]
        assert body["size"] == 100
        assert body["timeout"] == "60s"

    def test_empty_hits(self, transport):
        transport({("POST", "/orders/_search"): {"hits": {"hits": []}}})
        df = OpenSearchClient(host="h").execute_query('{"index": "orders"}')
        assert df.empty

    def test_aggs_default_size_zero(self, transport):
        fake = transport({("POST", "/orders/_search"): {"aggregations": {}}})
        OpenSearchClient(host="h").execute_query(
            '{"index": "orders", "aggs": {"n": {"value_count": {"field": "_id"}}}}')
        assert fake.calls[0]["json"]["size"] == 0

    def test_result_window_cap(self):
        with pytest.raises(ValueError, match="10000"):
            OpenSearchClient(host="h").execute_query(
                '{"index": "orders", "size": 9000, "from": 2000}')

    def test_non_search_keys_not_forwarded(self, transport):
        fake = transport({("POST", "/orders/_search"): {"hits": {"hits": []}}})
        OpenSearchClient(host="h").execute_query(
            '{"index": "orders", "collection": "x", "explain_me": true}')
        assert "collection" not in fake.calls[0]["json"]
        assert "explain_me" not in fake.calls[0]["json"]

    def test_http_error_raises(self, transport):
        transport({("POST", "/orders/_search"): _FakeResponse(400, {"error": "parse error"},
                                                              text="parse error")})
        with pytest.raises(RuntimeError, match=r"\[400\]"):
            OpenSearchClient(host="h").execute_query('{"index": "orders"}')

    def test_sql_escape_hatch(self, transport):
        fake = transport({("POST", "/_plugins/_sql"): {
            "schema": [{"name": "status", "type": "keyword"},
                       {"name": "COUNT(*)", "alias": "n", "type": "integer"}],
            "datarows": [["active", 2], ["cancelled", 1]],
        }})
        df = OpenSearchClient(host="h").execute_query(
            '{"sql": "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"}')
        assert list(df.columns) == ["status", "n"]
        assert list(df["n"]) == [2, 1]
        assert fake.calls[0]["json"] == {
            "query": "SELECT status, COUNT(*) AS n FROM orders GROUP BY status"}


# ---------- aggregation flattening ---------- #


class TestAggFlattening:
    def _run(self, aggregations, envelope=None, transport=None):
        transport({("POST", "/orders/_search"): {"aggregations": aggregations}})
        q = envelope or '{"index": "orders", "aggs": {"placeholder": {}}}'
        return OpenSearchClient(host="h").execute_query(q)

    def test_terms_with_metric(self, transport):
        df = self._run({
            "by_status": {"buckets": [
                {"key": "active", "doc_count": 2, "revenue": {"value": 200.5}},
                {"key": "cancelled", "doc_count": 1, "revenue": {"value": 15.0}},
            ]}
        }, transport=transport)
        assert list(df["by_status"]) == ["active", "cancelled"]
        assert list(df["doc_count"]) == [2, 1]
        assert list(df["revenue"]) == [200.5, 15.0]

    def test_nested_buckets(self, transport):
        df = self._run({
            "by_status": {"buckets": [
                {"key": "active", "doc_count": 2,
                 "by_tier": {"buckets": [
                     {"key": "gold", "doc_count": 1, "revenue": {"value": 120.5}},
                     {"key": "silver", "doc_count": 1, "revenue": {"value": 80.0}},
                 ]}},
            ]}
        }, transport=transport)
        assert list(df["by_status"]) == ["active", "active"]
        assert list(df["by_tier"]) == ["gold", "silver"]
        # doc_count comes from the innermost bucket level
        assert list(df["doc_count"]) == [1, 1]
        assert list(df["revenue"]) == [120.5, 80.0]

    def test_date_histogram_prefers_key_as_string(self, transport):
        df = self._run({
            "per_day": {"buckets": [
                {"key": 1751328000000, "key_as_string": "2026-07-01", "doc_count": 4},
            ]}
        }, transport=transport)
        assert list(df["per_day"]) == ["2026-07-01"]

    def test_filters_agg_dict_buckets(self, transport):
        df = self._run({
            "segments": {"buckets": {
                "big": {"doc_count": 3},
                "small": {"doc_count": 7},
            }}
        }, transport=transport)
        assert sorted(zip(df["segments"], df["doc_count"])) == [("big", 3), ("small", 7)]

    def test_top_level_metric_only(self, transport):
        df = self._run({"avg_total": {"value": 71.83}}, transport=transport)
        assert df.shape == (1, 1)
        assert df["avg_total"][0] == 71.83

    def test_stats_metric_expands(self, transport):
        df = self._run({
            "t": {"count": 3, "min": 15.0, "max": 120.5, "avg": 71.83, "sum": 215.5}
        }, transport=transport)
        assert df["t.min"][0] == 15.0 and df["t.sum"][0] == 215.5

    def test_percentiles_values(self, transport):
        df = self._run({"p": {"values": {"50.0": 80.0, "99.0": 120.5}}}, transport=transport)
        assert df["p.50.0"][0] == 80.0

    def test_unknown_shape_falls_back_to_json(self, transport):
        df = self._run({"weird": {"some": {"nested": ["thing"]}}}, transport=transport)
        assert json.loads(df["weird"][0]) == {"some": {"nested": ["thing"]}}


# ---------- connection / registry ---------- #


class TestConnectionAndRegistry:
    def test_test_connection_success(self, transport):
        transport({("GET", "/"): {"version": {"distribution": "opensearch",
                                              "number": "2.19.1"}}})
        res = OpenSearchClient(host="h").test_connection()
        assert res["success"] is True
        assert "opensearch 2.19.1" in res["message"]

    def test_test_connection_failure(self, transport):
        transport(error=RuntimeError("connection refused"))
        res = OpenSearchClient(host="h").test_connection()
        assert res["success"] is False and "refused" in res["message"]

    def test_registry_resolves_client(self):
        from app.schemas.data_source_registry import resolve_client_class
        assert resolve_client_class("opensearch") is OpenSearchClient

    def test_registry_entry_shape(self):
        from app.schemas.data_source_registry import get_entry
        e = get_entry("opensearch")
        assert e.data_shape == "objects"
        assert e.is_document_based is True
        assert e.credentials_auth.default == "userpass"
        assert set(e.credentials_auth.by_auth) == {"userpass", "none"}
        assert e.credentials_auth.by_auth["none"].scopes == ["system"]

    def test_credentials_schemas(self):
        from app.schemas.data_source_registry import credentials_schema_for
        from app.schemas.data_sources.configs import (
            OpenSearchCredentials,
            OpenSearchNoAuthCredentials,
        )
        assert credentials_schema_for("opensearch", "userpass") is OpenSearchCredentials
        assert credentials_schema_for("opensearch", "none") is OpenSearchNoAuthCredentials
        creds = OpenSearchCredentials(user="admin", password="pw")
        assert creds.user == "admin"
        with pytest.raises(Exception):
            OpenSearchCredentials()  # user/password required for userpass
        OpenSearchNoAuthCredentials()  # no fields

    def test_config_defaults(self):
        from app.schemas.data_sources.configs import OpenSearchConfig
        cfg = OpenSearchConfig(host="localhost")
        assert cfg.port == 9200
        assert cfg.secure is False
        assert cfg.verify_certs is True
        assert cfg.index_pattern is None
