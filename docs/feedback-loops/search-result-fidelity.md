# Filtered searches and exact counts

An analyst could request one event and receive the first page of unrelated events. A count of matching documents could produce an empty table even when the index contained matches.

## Root cause

The DSL branch of each client's `execute_query` copied selected envelope keys to the wire and discarded the rest. A `post_filter` could disappear before the HTTP request. After execution the document path read only `hits.hits`, which is empty for a count-only request with `size: 0`. The engine carries its count in `hits.total`. The aggregation path also discarded child metrics next to a single-bucket `doc_count`.

## Deterministic reproduction

From the backend directory, run the parameterized contract suite.

```sh
.venv/bin/pytest -q tests/unit/test_search_result_contract.py
```

The initial suite against the previous client implementation produced 19 failures and one pass. Assertions cover fields on the wire, exact and lower-bound totals, nested metrics, mixed hits and aggregations, rejection of unknown options, and incomplete shard responses. Additional cases exercise connection limits on the native body envelope.

## Real-engine reproduction

Point the following command at local engines where the test can create and remove its own randomly named index. It uses three synthetic events and checks totals of two and zero, cursor pagination, projected fields, collapse, a filter with a child maximum, and a request for both hits and aggregations.

```sh
cd backend
TEST_ELASTICSEARCH_URL=http://localhost:9200 TEST_OPENSEARCH_URL=http://localhost:19200 \
  .venv/bin/pytest -q tests/integrations/test_search_fidelity_live.py
```

Elasticsearch 8.17.4 returned both the nested count and maximum. OpenSearch 3.10.0 flagged the zero-size nested maximum query as terminated early on the fixture. The test checks the engine's flag independently and requires an explicit error in that case. The mixed query returned the expected hit and nested maximum on both engines.

## Local app verification

A separate 100,000-event synthetic index exercised column discovery and generated Python through the report composer with Claude Haiku 5.5. Direct Elasticsearch queries supplied the expected results.

| Question | Direct engine result | App observation |
| --- | --- | --- |
| Which region saw the most HTTP 503s? | East 331, north 330, west 330 | Correct breakdown |
| Which events recorded a sparse telemetry field? | 10 events, highest event ID 99920 | Correct final values. Generated code initially looked for a metric column ending in `.value` and needed a sorted-hit check |
| What happened to event 999? | Search service, north, HTTP 200, 108 ms | All four values matched |
| Did the export contain HTTP 418s? | 0 out of 100,000 | First answer qualified the count after generated code tried to cast the total object to an integer. A follow-up verified zero |
| Which events took at least 300 ms? | 3,330 total, billing and search 999 each, checkout and gateway 666 each | Grouped counts and an independent exact count agreed |
| Which service averaged the highest latency? | Billing 160.9 ms, search 159.9, gateway 158.9, checkout 157.9 | Correct ordering and means |

## Result contract

`backend/app/data_sources/clients/search_frames.py` validates envelope keys and constructs DataFrames. Both clients apply their engine-specific limits to the normalized request before sending it. An exact count is a row containing `doc_count` and `relation`. Every available raw hit total is also accessible through `df.attrs["total_hits"]`. Scalar aggregation columns use their aggregation names. The original aggregation tree is available in `df.attrs["aggregations"]` for aggregation-only requests. An explicit positive size with aggregations returns one row containing both result sections.

The tests establish connector behavior independently of the language model. Live chat also exercises schema discovery, code generation and result interpretation. The documented retries matter when evaluating first-answer reliability.
