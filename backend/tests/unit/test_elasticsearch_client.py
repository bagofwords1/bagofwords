"""Unit tests for ElasticsearchClient.

Covers:
- auth header selection: ApiKey (raw id:key vs pre-encoded) / basic / none
- get_tables(): mapping -> columns; date-suffixed indices collapse into one
  `<base>-*` pattern table (union of fields); system `.`-indices excluded
- execute_query(): DSL search body defaults, size+from window guard,
  aggregation flattening, DataFrame shape
- SQL (`/_sql`) and ES|QL (`/_query`) escape-hatch dispatch + response shapes
- test_connection() success / failure
- least-privilege operation: no cluster privileges, one API key per index
  pattern — the probe ladder, per-pattern discovery, and privilege reporting

The HTTP boundary (`_request`) is mocked, so these run with no live cluster.
The privilege behaviours they encode were measured against a real ES 8.15.3
with index-scoped API keys (see the `elasticsearch-least-privilege` feedback
loop doc).
"""
from __future__ import annotations

import base64

import pandas as pd
import pytest

from app.data_sources.clients.elasticsearch_client import (
    ElasticsearchClient,
    ElasticsearchHttpError,
)


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


def test_stream_discovery_fallback_batches_respect_url_budget(monkeypatch):
    # Stream names may run to 255 bytes; joined batches must stay under the
    # engine's ~4kB request-line limit, so long names shrink the batch size.
    c = ElasticsearchClient(host="h")
    n = 40
    long = "logs-" + "x" * 200  # ~205 chars each
    streams = [{"name": f"{long}-{i:02d}",
                "indices": [{"index_name": f".ds-{long}-{i:02d}-000001"}]}
               for i in range(n)]
    calls = []

    def fake_request(method, path, json_body=None, params=None):
        calls.append(path)
        if path in ("/_mapping", "/_alias"):
            return {}
        if path == "/_data_stream":
            return {"data_streams": streams}
        names = path.strip("/").split("/")[0].split(",")
        return {f".ds-{nm}-000001": _mapping({"@timestamp": {"type": "date"}})
                for nm in names}

    monkeypatch.setattr(c, "_request", fake_request)
    tables = c.get_tables()
    assert len(tables) == n
    fallback = [p for p in calls if p not in ("/_mapping", "/_alias", "/_data_stream")]
    # 40 names x ~208 chars -> multiple batches, every request line under budget.
    assert len(fallback) > 1
    assert all(len(p) < 3200 for p in fallback)


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


# ---------- least privilege (index privileges only, no cluster ones) ------- #

def _forbidden(path: str) -> ElasticsearchHttpError:
    return ElasticsearchHttpError(
        f"Elasticsearch request GET {path} failed [403]: security_exception",
        status_code=403, body="security_exception")


def test_test_connection_falls_back_when_cluster_monitor_is_denied(monkeypatch):
    # `GET /` needs cluster monitor, which index-scoped keys never carry. A 403
    # there must not fail the connection: it blocks the save path and flips
    # is_active off on every status sweep, for a probe the connector does not
    # actually depend on.
    c = ElasticsearchClient(host="h", index_pattern="eksa*")

    def fake_request(method, path, json_body=None, params=None):
        if path == "/":
            raise _forbidden("/")
        if path == "/_security/_authenticate":
            return {"username": "svc", "authentication_type": "api_key",
                    "api_key": {"id": "x", "name": "eksa-key"}, "roles": []}
        if path == "/_security/user/_has_privileges":
            return {"has_all_requested": True,
                    "index": {"eksa*": {"read": True, "view_index_metadata": True}}}
        raise AssertionError(f"unexpected probe: {path}")

    monkeypatch.setattr(c, "_request", fake_request)
    res = c.test_connection()
    assert res["success"] is True
    assert res["reachable"] is True
    assert "eksa-key" in res["message"]          # names the key, not just its owner
    assert res["details"]["cluster_monitor"] is False


def test_test_connection_falls_back_to_metadata_probe(monkeypatch):
    # Security-disabled or proxied clusters have no `_security` endpoint: the
    # last rung is the metadata read the connector actually needs.
    c = ElasticsearchClient(host="h", index_pattern="eksa*")

    def fake_request(method, path, json_body=None, params=None):
        if path in ("/", "/_security/_authenticate"):
            raise _forbidden(path)
        if path == "/eksa*/_mapping":
            return {"eksa-app-2026.08.10": {"mappings": {}}}
        if path == "/_security/user/_has_privileges":
            return {"has_all_requested": True, "index": {}}
        raise AssertionError(f"unexpected probe: {path}")

    monkeypatch.setattr(c, "_request", fake_request)
    res = c.test_connection()
    assert res["success"] is True and res["reachable"] is True
    assert "eksa*" in res["message"]


def test_test_connection_http_error_is_reachable(monkeypatch):
    # An HTTP answer proves the host is there. `reachable` is what the save path
    # hard-blocks on, so a privilege problem must never read as a bad endpoint.
    c = ElasticsearchClient(host="h", index_pattern="eksa*")

    def fake_request(method, path, json_body=None, params=None):
        if path == "/_security/user/_has_privileges":
            return {"has_all_requested": False,
                    "index": {"eksa*": {"read": False, "view_index_metadata": False}}}
        raise _forbidden(path)

    monkeypatch.setattr(c, "_request", fake_request)
    res = c.test_connection()
    assert res["success"] is False
    assert res["reachable"] is True
    assert "`read` on `eksa*`" in res["message"]
    assert len(res["details"]["missing_privileges"]) == 2


def test_test_connection_warns_when_only_metadata_is_granted(monkeypatch):
    # The trap: metadata-only keys test green AND index a full catalog, then
    # 403 on every query. The test must say so while still passing.
    c = ElasticsearchClient(host="h", index_pattern="eksa*")

    def fake_request(method, path, json_body=None, params=None):
        if path == "/":
            raise _forbidden("/")
        if path == "/_security/_authenticate":
            return {"username": "svc", "roles": []}
        if path == "/_security/user/_has_privileges":
            return {"has_all_requested": False,
                    "index": {"eksa*": {"read": False, "view_index_metadata": True}}}
        raise AssertionError(f"unexpected probe: {path}")

    monkeypatch.setattr(c, "_request", fake_request)
    res = c.test_connection()
    assert res["success"] is True
    assert "queries will fail" in res["message"]
    assert "schema discovery" not in res["message"]


def test_test_connection_transport_failure_is_unreachable(monkeypatch):
    c = ElasticsearchClient(host="h")

    def boom(*a, **k):
        raise ConnectionError("name resolution failed")

    monkeypatch.setattr(c, "_request", boom)
    res = c.test_connection()
    assert res["success"] is False and res["reachable"] is False


def test_discovery_is_per_pattern_so_one_bad_target_is_not_fatal(monkeypatch):
    # Measured on 8.15.3: a *named* index the key may not see rejects the whole
    # request, taking the readable patterns with it. One call per pattern keeps
    # the readable ones.
    c = ElasticsearchClient(host="h", index_pattern="eksa*,finance-secret")
    calls = []

    def fake_request(method, path, json_body=None, params=None):
        calls.append(path)
        if path == "/eksa*/_mapping":
            return {"eksa-app-2026.08.10": _mapping({"level": {"type": "keyword"}})}
        if path.startswith("/finance-secret"):
            raise _forbidden(path)
        return {}

    monkeypatch.setattr(c, "_request", fake_request)
    names = {t.name for t in c.get_tables()}
    assert names == {"eksa-app-2026.08.10"}
    assert "/eksa*,finance-secret/_mapping" not in calls  # never joined into one


def test_discovery_raises_when_every_pattern_fails(monkeypatch):
    # Returning [] here shows the admin "0 tables" and no reason; the raised
    # message lands on the indexing row and in the connection test instead.
    c = ElasticsearchClient(host="h", index_pattern="eksa*")

    def fake_request(method, path, json_body=None, params=None):
        raise _forbidden(path)

    monkeypatch.setattr(c, "_request", fake_request)
    with pytest.raises(RuntimeError, match="403"):
        c.get_tables()


def test_alias_failure_does_not_discard_the_mappings(monkeypatch):
    # Aliases are an enrichment. They used to share a try block with the
    # mappings, so an alias 403 returned an empty catalog.
    c = ElasticsearchClient(host="h")

    def fake_request(method, path, json_body=None, params=None):
        if path == "/_mapping":
            return {"orders": _mapping({"total": {"type": "double"}})}
        if path == "/_alias":
            raise _forbidden("/_alias")
        return {}

    monkeypatch.setattr(c, "_request", fake_request)
    assert {t.name for t in c.get_tables()} == {"orders"}


def test_metadata_calls_are_lenient_and_expand_hidden(monkeypatch):
    # ignore_unavailable/allow_no_indices keep an empty-or-unreadable target
    # from failing the call; hidden expansion stops a hidden index that matches
    # an explicitly configured pattern from vanishing.
    c = ElasticsearchClient(host="h", index_pattern="eksa*")
    seen = {}

    def fake_request(method, path, json_body=None, params=None):
        seen[path] = params or {}
        return {}

    monkeypatch.setattr(c, "_request", fake_request)
    c.get_tables()
    assert seen["/eksa*/_mapping"]["ignore_unavailable"] == "true"
    assert seen["/eksa*/_mapping"]["allow_no_indices"] == "true"
    assert seen["/eksa*/_mapping"]["expand_wildcards"] == "open,hidden"


def test_bare_star_pattern_does_not_drag_in_system_indices(monkeypatch):
    # `index_pattern = "*"` used to disable the `.`-index filter wholesale.
    c = ElasticsearchClient(host="h", index_pattern="*")

    def fake_request(method, path, json_body=None, params=None):
        if path == "/*/_mapping":
            return {"orders": _mapping({"a": {"type": "keyword"}}),
                    ".security-7": _mapping({"b": {"type": "keyword"}})}
        return {}

    monkeypatch.setattr(c, "_request", fake_request)
    assert {t.name for t in c.get_tables()} == {"orders"}
    # ...but a pattern that explicitly names them still exposes them.
    c2 = ElasticsearchClient(host="h", index_pattern=".ds-*")
    monkeypatch.setattr(c2, "_request", lambda m, p, json_body=None, params=None: (
        {".ds-logs-000001": _mapping({"a": {"type": "keyword"}})} if p.endswith("/_mapping") else {}))
    assert {t.name for t in c2.get_tables()} == {".ds-logs-000001"}


def test_forbidden_error_carries_an_actionable_hint():
    c = ElasticsearchClient(host="h")

    class Resp:
        status_code = 403
        text = '{"error":{"type":"security_exception"}}'

    hint = c._privilege_hint(Resp.status_code)
    assert "view_index_metadata" in hint and "wildcard" in hint
    assert "invalid or expired" in c._privilege_hint(401)
    assert c._privilege_hint(404) == ""


def test_configured_scope_is_advertised_to_the_agent():
    c = ElasticsearchClient(host="h", index_pattern="eksa*,ekpb*")
    assert "restricted to eksa*, ekpb*" in c.description
    assert "SCOPE" not in ElasticsearchClient(host="h").description
