"""Search results retain query semantics across the two DataFrame connectors."""
import json

import pytest

from app.data_sources.clients.elasticsearch_client import ElasticsearchClient
from app.data_sources.clients.opensearch_client import OpenSearchClient


@pytest.fixture(params=[ElasticsearchClient, OpenSearchClient])
def search(request, monkeypatch):
    client = request.param(host="localhost")

    def run(body, response):
        captured = {}

        def http(method, path, json_body, **kwargs):
            captured.update(json_body)
            return response

        monkeypatch.setattr(client, "_request", http)
        frame = client.execute_query(json.dumps({"index": "events", "body": body}))
        return frame, captured

    return run


@pytest.mark.parametrize("value,relation", [(0, "eq"), (13, "eq"), (10003, "eq"), (10, "gte")])
def test_count_retains_total_and_precision(search, value, relation):
    frame, wire = search({"size": 0, "track_total_hits": 10 if relation == "gte" else True},
                         {"hits": {"total": {"value": value, "relation": relation}, "hits": []}})
    assert wire["track_total_hits"] == (10 if relation == "gte" else True)
    assert frame.to_dict("records") == [{"doc_count": value, "relation": relation}]


def test_filters_field_projection_collapse_and_pagination(search):
    body = {"post_filter": {"term": {"category": "blue"}}, "collapse": {"field": "region"},
            "fields": ["metric"], "_source": False, "sort": ["region"], "search_after": ["east"]}
    hit = {"_id": "a", "_index": "events", "fields": {"metric": [37]}, "sort": ["west"]}
    frame, wire = search(body, {"hits": {"hits": [hit]}})
    assert all(wire[k] == v for k, v in body.items())
    assert frame.iloc[0]["fields.metric"] == [37]
    assert frame.iloc[0]["_sort"] == ["west"]


def test_nested_metrics_and_mixed_results(search):
    agg = {"group": {"doc_count": 13, "peak": {"value": 83}}}
    hit = {"_id": "a", "_index": "events", "_source": {"metric": 37}}
    result = {"hits": {"hits": [hit]}, "aggregations": agg}
    query = {"aggs": {"group": {"filter": {"match_all": {}}, "aggs": {"peak": {"max": {"field": "metric"}}}}}}
    frame, _ = search(query, result)
    assert frame.to_dict("records") == [{"group.doc_count": 13, "group.peak": 83}]
    mixed, _ = search({**query, "size": 1}, result)
    assert mixed.attrs["aggregations"] == agg
    assert mixed.attrs["hits"] == [hit]


def test_unknown_option_is_rejected(search):
    with pytest.raises(ValueError, match="misspelled_option"):
        search({"misspelled_option": True}, {"hits": {"hits": []}})


@pytest.mark.parametrize("partial", [{"timed_out": True}, {"_shards": {"failed": 1}},
                                     {"terminated_early": True}])
def test_partial_results_are_rejected(search, partial):
    with pytest.raises(ValueError, match="partial|timed out"):
        search({"size": 0}, {**partial, "hits": {"total": {"value": 7, "relation": "eq"}, "hits": []}})


def test_exact_total_alongside_scalar_metric(search):
    body = {"size": 0, "track_total_hits": True, "aggs": {"peak": {"max": {"field": "metric"}}}}
    frame, _ = search(body, {"hits": {"total": {"value": 13, "relation": "eq"}},
                             "aggregations": {"peak": {"value": 83}}})
    assert frame.to_dict("records") == [{"peak": 83, "doc_count": 13, "relation": "eq"}]


@pytest.mark.parametrize("index,body", [
    ("other", {"size": 0}),
    ("events", {"from": 2, "size": 2}),
    ("events", {"aggs": {"a": {"max": {"field": "m"}}, "b": {"min": {"field": "m"}}}}),
    ("events", {"post_filter": {"terms": {"service": {"index": "lookup", "id": "1", "path": "names"}}}}),
])
def test_native_body_obeys_connection_limits(monkeypatch, index, body):
    client = ElasticsearchClient(host="localhost", allowed_index_patterns="events",
                                 max_result_size=3, max_aggregations=1)
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: pytest.fail("unexpected HTTP request"))
    with pytest.raises(ValueError):
        client.execute_query(json.dumps({"index": index, "body": body}))


def test_filter_with_child_buckets_retains_parent_and_child_counts(search):
    result = {"outer": {"doc_count": 7, "by_region": {"buckets": [
        {"key": "west", "doc_count": 5}, {"key": "east", "doc_count": 2}]}}}
    frame, _ = search({"aggs": {"outer": {"filter": {"match_all": {}}, "aggs": {
        "by_region": {"terms": {"field": "region"}}}}}}, {"aggregations": result})
    assert frame["outer.doc_count"].tolist() == [7, 7]
    assert frame["outer.by_region"].tolist() == ["west", "east"]
    assert frame["doc_count"].tolist() == [5, 2]


def test_single_child_bucket_stays_separate_from_sibling_series(search):
    aggregations = {
        "subset": {"doc_count": 5, "regions": {"buckets": [{"key": "west", "doc_count": 5}]}},
        "services": {"buckets": [{"key": "billing", "doc_count": 7}]},
    }
    frame, _ = search({"aggs": {"subset": {"filter": {"match_all": {}},
                                           "aggs": {"regions": {"terms": {"field": "region"}}}},
                                "services": {"terms": {"field": "service"}}}}, {"aggregations": aggregations})
    assert len(frame) == 2
    assert frame["subset.regions"].dropna().tolist() == ["west"]
    assert frame["services"].dropna().tolist() == ["billing"]
    assert frame["doc_count"].tolist() == [5, 7]


def test_legacy_total_and_empty_aggregations(search):
    frame, _ = search({"size": 0}, {"hits": {"hits": [], "total": 5}})
    assert frame.to_dict("records") == [{"doc_count": 5, "relation": "eq"}]
    empty, _ = search({"aggs": {}}, {"hits": {"hits": [], "total": 0}})
    assert empty.empty
    assert empty.attrs["aggregations"] == {}


def test_source_collisions_and_metric_metadata(search):
    hit = {"_id": "1", "_source": {"fields": {"tag": "old"}, "_sort": "original"},
           "fields": {"tag": ["projected"]}, "sort": [8]}
    frame, _ = search({"size": 2, "fields": ["tag"]}, {"hits": {"hits": [hit]}})
    row = frame.iloc[0]
    assert row["fields.tag"] == ["projected"] and row["_source.fields.tag"] == "old"
    assert row["_sort"] == [8] and row["_source._sort"] == "original"
    frame, _ = search({"aggs": {"peak": {"max": {"field": "metric"}}}},
                      {"aggregations": {"peak": {"value": 3, "meta": {"unit": "ms"}}}})
    assert frame.iloc[0]["peak"] == 3


@pytest.mark.parametrize("access", [
    'row["wrong"]',
    'row.get("wrong", 0)',
    'row["wrong"] if "wrong" in frame.columns else 0',
])
@pytest.mark.parametrize("mode", ["subprocess", "inprocess"])
def test_generated_missing_summary_column_fails_with_actual_schema(monkeypatch, access, mode):
    monkeypatch.setenv("BOW_CODE_SANDBOX", mode)
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    client = ElasticsearchClient(host="localhost")
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {
        "hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}})
    code = f'''def generate_df(ds_clients, excel_files):
    frame = ds_clients["search"].execute_query('{{"index":"events","size":0}}')
    row = frame.iloc[0]
    return pd.DataFrame([{{"count": {access}}}])
'''
    with pytest.raises(Exception, match="Missing search result column 'wrong'.*doc_count.*relation"):
        StreamingCodeExecutor().execute_code(code=code, ds_clients={"search": client}, excel_files=[])
    good = code.replace(access, 'row["doc_count"]')
    result, _, _ = StreamingCodeExecutor().execute_code(code=good, ds_clients={"search": client}, excel_files=[])
    assert result.to_dict("records") == [{"count": 0}]


def test_lookup_target_uses_connection_policy(monkeypatch):
    client = ElasticsearchClient(host="localhost", allowed_index_patterns="events")
    checked = []
    monkeypatch.setattr(client, "_check_target", checked.append)
    client._check_dsl_lookups({"like": [{"_index": "events", "_id": "sample"}]})
    assert checked == ["events"]


def test_generated_nested_summary_returns_count_and_peak(monkeypatch):
    from app.ai.code_execution.code_execution import StreamingCodeExecutor

    client = ElasticsearchClient(host="localhost")
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: {
        "aggregations": {"active": {"doc_count": 4, "peak": {"value": 23}}}})
    code = '''def generate_df(ds_clients, excel_files):
    frame = ds_clients["search"].execute_query({"index": "events", "size": 0,
        "aggs": {"active": {"filter": {"exists": {"field": "sensor"}},
                            "aggs": {"peak": {"max": {"field": "elapsed"}}}}}})
    return frame[["active.doc_count", "active.peak"]]
'''
    result, _, _ = StreamingCodeExecutor().execute_code(code=code, ds_clients={"search": client}, excel_files=[])
    assert result.to_dict("records") == [{"active.doc_count": 4, "active.peak": 23}]
