"""Unit tests for ElasticsearchClient.

Covers:
- auth header selection: ApiKey (raw id:key vs pre-encoded) / basic / none
- get_tables(): mapping -> columns; date-suffixed indices collapse into one
  `<base>-*` pattern table (union of fields); system `.`-indices excluded
- execute_query(): DSL search body defaults, size+from window guard,
  aggregation flattening, DataFrame shape
- SQL (`/_sql`) and ES|QL (`/_query`) escape-hatch dispatch + response shapes
- test_connection() success / failure

The HTTP boundary (`_request`) is mocked, so these run with no live cluster.
"""
from __future__ import annotations

import base64

import pandas as pd
import pytest

from app.data_sources.clients.elasticsearch_client import ElasticsearchClient


# ---------- auth ---------- #

def test_apikey_raw_pair_is_base64_encoded():
    c = ElasticsearchClient(host="h", api_key="theid:thekey")
    auth, headers = c._auth()
    assert auth is None
    expected = base64.b64encode(b"theid:thekey").decode()
    assert headers["Authorization"] == f"ApiKey {expected}"


def test_apikey_preencoded_passed_through():
    token = base64.b64encode(b"id:key").decode()  # contains '='
    c = ElasticsearchClient(host="h", api_key=token)
    _, headers = c._auth()
    assert headers["Authorization"] == f"ApiKey {token}"


def test_basic_auth_when_user_set():
    c = ElasticsearchClient(host="h", user="elastic", password="pw")
    auth, headers = c._auth()
    assert auth == ("elastic", "pw")
    assert "Authorization" not in headers


def test_no_auth():
    c = ElasticsearchClient(host="h")
    auth, headers = c._auth()
    assert auth is None and headers == {}


# ---------- schema discovery ---------- #

def _mapping(props):
    return {"mappings": {"properties": props}}


def test_date_suffixed_indices_collapse_to_pattern(monkeypatch):
    c = ElasticsearchClient(host="h")
    responses = {
        "/_mapping": {
            "logs-app-2026.07.09": _mapping({"level": {"type": "keyword"},
                                             "message": {"type": "text"}}),
            "logs-app-2026.07.10": _mapping({"level": {"type": "keyword"},
                                             "status": {"type": "integer"}}),
            "orders": _mapping({"total": {"type": "double"}}),
            ".security-7": _mapping({"x": {"type": "keyword"}}),
        },
        "/_alias": {},
        "/_data_stream": {"data_streams": []},
    }

    def fake_request(method, path, json_body=None, params=None):
        return responses.get(path, {})

    monkeypatch.setattr(c, "_request", fake_request)
    tables = {t.name: t for t in c.get_tables()}

    # The two daily indices collapse into one pattern table (union of fields).
    assert "logs-app-*" in tables
    assert "logs-app-2026.07.09" not in tables
    field_names = {col.name for col in tables["logs-app-*"].columns}
    assert {"level", "message", "status"} <= field_names
    assert tables["logs-app-*"].metadata_json["type"] == "pattern"
    # A lone index is left concrete; the system `.`-index is excluded.
    assert "orders" in tables
    assert ".security-7" not in tables


def test_single_day_index_not_collapsed(monkeypatch):
    c = ElasticsearchClient(host="h")
    responses = {
        "/_mapping": {"logs-2026.07.10": _mapping({"a": {"type": "keyword"}})},
        "/_alias": {},
        "/_data_stream": {"data_streams": []},
    }
    monkeypatch.setattr(c, "_request", lambda m, p, json_body=None, params=None: responses.get(p, {}))
    names = {t.name for t in c.get_tables()}
    # One member -> no point collapsing; stays concrete.
    assert names == {"logs-2026.07.10"}


def test_multifield_keyword_surfaces_as_column(monkeypatch):
    c = ElasticsearchClient(host="h")
    props = {"message": {"type": "text", "fields": {"keyword": {"type": "keyword"}}}}
    responses = {"/_mapping": {"idx": _mapping(props)}, "/_alias": {},
                 "/_data_stream": {"data_streams": []}}
    monkeypatch.setattr(c, "_request", lambda m, p, json_body=None, params=None: responses.get(p, {}))
    cols = {col.name for col in c.get_tables()[0].columns}
    assert "message" in cols and "message.keyword" in cols


def test_stream_discovery_reuses_bulk_mapping_no_per_stream_calls(monkeypatch):
    # Backing indices present in the bulk /_mapping (the serverless case):
    # stream tables must be assembled from it — exactly 3 HTTP calls total,
    # regardless of stream count, with identical union metadata.
    c = ElasticsearchClient(host="h")
    n = 200
    bulk = {f".ds-logs-s{i}-default-000001": _mapping({"@timestamp": {"type": "date"},
                                                       "level": {"type": "keyword"}})
            for i in range(n)}
    streams = [{"name": f"logs-s{i}-default",
                "indices": [{"index_name": f".ds-logs-s{i}-default-000001"}]}
               for i in range(n)]
    responses = {"/_mapping": bulk, "/_alias": {},
                 "/_data_stream": {"data_streams": streams}}
    calls = []

    def fake_request(method, path, json_body=None, params=None):
        calls.append(path)
        return responses.get(path, {})

    monkeypatch.setattr(c, "_request", fake_request)
    tables = c.get_tables()
    assert len(tables) == n
    assert calls == ["/_mapping", "/_alias", "/_data_stream"]
    t = next(t for t in tables if t.name == "logs-s0-default")
    assert t.metadata_json["type"] == "data_stream"
    assert t.metadata_json["indices"] == [".ds-logs-s0-default-000001"]
    assert {c_.name for c_ in t.columns} == {"@timestamp", "level"}


def test_stream_discovery_batched_fallback_when_backing_hidden(monkeypatch):
    # Backing indices absent from the bulk /_mapping (stateful clusters hide
    # .ds-*): streams are resolved in comma-joined batches, not one call each.
    c = ElasticsearchClient(host="h")
    n = 120  # -> ceil(120/50) = 3 fallback calls
    streams = [{"name": f"logs-s{i}",
                "indices": [{"index_name": f".ds-logs-s{i}-000001"}]}
               for i in range(n)]
    calls = []

    def fake_request(method, path, json_body=None, params=None):
        calls.append(path)
        if path == "/_mapping" or path == "/_alias":
            return {}
        if path == "/_data_stream":
            return {"data_streams": streams}
        # batched fallback: /{a,b,c}/_mapping
        names = path.strip("/").split("/")[0].split(",")
        return {f".ds-{nm}-000001": _mapping({"@timestamp": {"type": "date"}})
                for nm in names}

    monkeypatch.setattr(c, "_request", fake_request)
    tables = c.get_tables()
    assert len(tables) == n
    fallback = [p for p in calls if p not in ("/_mapping", "/_alias", "/_data_stream")]
    assert len(fallback) == 3
    assert all(p.endswith("/_mapping") for p in fallback)
    # A failed batch degrades to "those streams have no table", never an error.


def test_stream_discovery_failed_fallback_batch_skips_streams(monkeypatch):
    c = ElasticsearchClient(host="h")
    streams = [{"name": "logs-a", "indices": [{"index_name": ".ds-logs-a-000001"}]},
               {"name": "logs-b", "indices": [{"index_name": ".ds-logs-b-000001"}]}]

    def fake_request(method, path, json_body=None, params=None):
        if path == "/_data_stream":
            return {"data_streams": streams}
        if path in ("/_mapping", "/_alias"):
            return {}
        raise RuntimeError("mapping fetch failed")

    monkeypatch.setattr(c, "_request", fake_request)
    assert c.get_tables() == []  # degraded, no exception


def test_analyzed_text_dtype_points_at_keyword_subfield(monkeypatch):
    # A text field WITH a keyword subfield: the schema should route aggs/sort
    # to the subfield rather than describing the base field as aggregatable.
    c = ElasticsearchClient(host="h")
    props = {"message": {"type": "text", "fields": {"keyword": {"type": "keyword"}}}}
    responses = {"/_mapping": {"idx": _mapping(props)}, "/_alias": {},
                 "/_data_stream": {"data_streams": []}}
    monkeypatch.setattr(c, "_request", lambda m, p, json_body=None, params=None: responses.get(p, {}))
    dtypes = {col.name: col.dtype for col in c.get_tables()[0].columns}
    assert dtypes["message"] == "string (full-text; aggregate/sort on message.keyword)"
    assert dtypes["message.keyword"] == "string"


def test_analyzed_text_without_keyword_marked_not_aggregatable(monkeypatch):
    # Serverless logsdb maps message fields as match_only_text with NO keyword
    # subfield — the schema must say the field cannot be aggregated/sorted,
    # or the coder writes terms aggs that 400.
    c = ElasticsearchClient(host="h")
    props = {
        "error": {"properties": {"message": {"type": "match_only_text"}}},
        "level": {"type": "keyword"},
    }
    responses = {"/_mapping": {"idx": _mapping(props)}, "/_alias": {},
                 "/_data_stream": {"data_streams": []}}
    monkeypatch.setattr(c, "_request", lambda m, p, json_body=None, params=None: responses.get(p, {}))
    dtypes = {col.name: col.dtype for col in c.get_tables()[0].columns}
    assert dtypes["error.message"] == "string (full-text; NOT aggregatable/sortable)"
    assert dtypes["level"] == "string"


# ---------- query execution ---------- #

def test_execute_query_search_defaults_and_shape(monkeypatch):
    c = ElasticsearchClient(host="h")
    captured = {}

    def fake_request(method, path, json_body=None, params=None):
        captured["path"] = path
        captured["body"] = json_body
        return {"hits": {"hits": [
            {"_id": "1", "_index": "logs-app-2026.07.10", "_source": {"level": "error"}},
        ]}}

    monkeypatch.setattr(c, "_request", fake_request)
    df = c.execute_query('{"index": "logs-app-*", "query": {"match_all": {}}}')
    # size defaults to 100 for a document search, timeout is set.
    assert captured["body"]["size"] == 100
    assert captured["body"]["timeout"] == "60s"
    assert captured["path"] == "/logs-app-*/_search"
    assert list(df["_id"]) == ["1"]
    assert "_index" in df.columns and "level" in df.columns


def test_execute_query_window_guard():
    c = ElasticsearchClient(host="h")
    with pytest.raises(ValueError, match="size \\+ from"):
        c.execute_query('{"index": "x", "size": 9999, "from": 5000}')


def test_execute_query_requires_index():
    c = ElasticsearchClient(host="h")
    with pytest.raises(ValueError, match="index"):
        c.execute_query('{"query": {"match_all": {}}}')


def test_execute_query_aggregation_flattened(monkeypatch):
    c = ElasticsearchClient(host="h")

    def fake_request(method, path, json_body=None, params=None):
        return {"aggregations": {"by_level": {"buckets": [
            {"key": "error", "doc_count": 5},
            {"key": "info", "doc_count": 20},
        ]}}}

    monkeypatch.setattr(c, "_request", fake_request)
    df = c.execute_query('{"index": "logs-app-*", "aggs": {"by_level": {"terms": {"field": "level"}}}}')
    assert list(df["by_level"]) == ["error", "info"]
    assert list(df["doc_count"]) == [5, 20]


def test_aggs_default_size_zero(monkeypatch):
    c = ElasticsearchClient(host="h")
    captured = {}
    monkeypatch.setattr(c, "_request",
                        lambda m, p, json_body=None, params=None: captured.update(body=json_body) or {"aggregations": {}})
    c.execute_query('{"index": "x", "aggs": {"a": {"terms": {"field": "f"}}}}')
    assert captured["body"]["size"] == 0


def test_sql_escape_hatch(monkeypatch):
    c = ElasticsearchClient(host="h")

    def fake_request(method, path, json_body=None, params=None):
        assert path == "/_sql" and params == {"format": "json"}
        return {"columns": [{"name": "level"}, {"name": "n"}],
                "rows": [["error", 5], ["info", 20]]}

    monkeypatch.setattr(c, "_request", fake_request)
    df = c.execute_query('{"sql": "SELECT level, COUNT(*) n FROM x GROUP BY level"}')
    assert list(df.columns) == ["level", "n"]
    assert df.iloc[0]["n"] == 5


def test_esql_escape_hatch(monkeypatch):
    c = ElasticsearchClient(host="h")

    def fake_request(method, path, json_body=None, params=None):
        assert path == "/_query"
        return {"columns": [{"name": "level"}, {"name": "n"}],
                "values": [["error", 5]]}

    monkeypatch.setattr(c, "_request", fake_request)
    df = c.execute_query('{"esql": "FROM x | STATS n = COUNT(*) BY level"}')
    assert list(df.columns) == ["level", "n"]


def test_invalid_json_raises():
    c = ElasticsearchClient(host="h")
    with pytest.raises(ValueError, match="Invalid JSON"):
        c.execute_query("not json")


# ---------- connection ---------- #

def test_test_connection_success(monkeypatch):
    c = ElasticsearchClient(host="h")
    monkeypatch.setattr(c, "_request",
                        lambda m, p, json_body=None, params=None: {"version": {"number": "8.15.3"}})
    res = c.test_connection()
    assert res["success"] and "8.15.3" in res["message"]


def test_test_connection_failure(monkeypatch):
    c = ElasticsearchClient(host="h")

    def boom(*a, **k):
        raise RuntimeError("refused")

    monkeypatch.setattr(c, "_request", boom)
    res = c.test_connection()
    assert res["success"] is False and "refused" in res["message"]
