"""Opt-in contract check against disposable indexes on local real engines."""
import json
import os
from uuid import uuid4

import pytest
import requests

from app.data_sources.clients.elasticsearch_client import ElasticsearchClient
from app.data_sources.clients.opensearch_client import OpenSearchClient


@pytest.mark.parametrize("client_type,variable", [
    (ElasticsearchClient, "TEST_ELASTICSEARCH_URL"),
    (OpenSearchClient, "TEST_OPENSEARCH_URL"),
])
def test_live_search_contract(client_type, variable):
    url = os.environ.get(variable)
    if not url:
        pytest.skip(f"Set {variable} to a disposable local engine")
    index = f"bow-contract-{uuid4().hex}"
    endpoint = f"{url.rstrip('/')}/{index}"
    client = client_type(host=url)

    def search(**body):
        return client.execute_query(json.dumps({"index": index, "body": body}))

    mapping = {"region": {"type": "keyword"}, "event_id": {"type": "long"}}
    requests.put(endpoint, json={"mappings": {"properties": mapping},
                 "settings": {"number_of_replicas": 0}}, timeout=15).raise_for_status()
    try:
        lines = []
        for event_id, region in enumerate(["east", "west", "east"], 1):
            lines.extend([json.dumps({"index": {"_id": str(event_id)}}),
                          json.dumps({"event_id": event_id, "region": region})])
        bulk = requests.post(f"{endpoint}/_bulk", params={"refresh": "true"},
                             data="\n".join(lines) + "\n",
                             headers={"Content-Type": "application/x-ndjson"}, timeout=30)
        bulk.raise_for_status()
        assert not bulk.json()["errors"]
        east = {"term": {"region": "east"}}
        for query, count in [(east, 2), ({"term": {"region": "south"}}, 0)]:
            assert search(query=query, size=0).to_dict("records") == [{"doc_count": count, "relation": "eq"}]
        page = search(post_filter=east, size=2, fields=["event_id"], _source=False,
                      sort=["event_id"], search_after=[1])
        assert list(page["_id"]) == ["3"]
        assert page.iloc[0]["fields.event_id"] == page.iloc[0]["_sort"] == [3]
        assert len(search(collapse={"field": "region"})) == 2
        aggs = {"east": {"filter": east, "aggs": {"last": {"max": {"field": "event_id"}}}}}
        expected = [{"east.doc_count": 2, "east.last": 3}]
        assert search(aggs=aggs).to_dict("records") == expected
        mixed = search(size=1, post_filter={"term": {"region": "west"}}, aggs=aggs)
        assert mixed.to_dict("records") == expected
        assert mixed.attrs.keys().isdisjoint({"hits", "aggregations"})
        raw = client.execute_query({"index": index, "result_format": "raw", "size": 1,
                                    "post_filter": {"term": {"region": "west"}}, "aggs": aggs})
        response = raw.iloc[0]["response"]
        assert [hit["_id"] for hit in response["hits"]["hits"]] == ["2"]
        assert response["aggregations"]["east"]["last"]["value"] == 3
        empty = search(size=0, track_total_hits=True, aggs={"regions": {
            "terms": {"field": "region", "include": ["south"]},
            "aggs": {"peak": {"max": {"field": "event_id"}}}}})
        assert empty.empty and list(empty.columns) == ["regions", "doc_count", "peak"]
        assert empty.attrs["total_hits"] == {"value": 3, "relation": "eq"}
        with pytest.raises(KeyError):
            empty["unrequested_metric"]
    finally:
        requests.delete(endpoint, timeout=15).raise_for_status()
