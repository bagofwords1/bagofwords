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

Elasticsearch 8.17.4 returned both the nested count and maximum. OpenSearch 3.10.0 flagged the zero-size nested maximum query as terminated early on the fixture. OpenSearch can set this flag on a complete aggregation-only response. The live test requires the known count and maximum on both engines. Timeouts and failed shards still raise errors. The mixed query returned the expected hit and nested maximum on both engines.

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

`backend/app/data_sources/clients/search_frames.py` validates envelope keys and constructs DataFrames. Both clients apply their engine-specific limits to the normalized request before sending it. An exact count is a row containing `doc_count` and `relation`. Every available raw hit total is also accessible through `df.attrs["total_hits"]`. Scalar aggregation columns use their aggregation names. A positive size keeps the flat aggregation table. To retrieve the engine response, set `"result_format": "raw"` alongside `"index"` and read `df.iloc[0]["response"]`. This uses the same request limits and completion checks.

The tests establish connector behavior independently of the language model. Live chat also exercises schema discovery, code generation and result interpretation. The documented retries matter when evaluating first-answer reliability.

## Generated-code verification

The inspection and chart tools both call StreamingCodeExecutor. Its search-summary checks report the missing name and the actual columns for direct scalar reads, pandas get calls with zero/null defaults, and column-membership checks with a zero/null fallback. The regression suite runs the observed false-zero expression in both the subprocess and in-process executors. It also checks genuine zero and nonzero nested aggregation results. These checks cover common access patterns in generated Python. Arbitrary downstream Python calculations can still introduce errors.

In a fresh Haiku 5.5 report, the saved queue telemetry table contained 10 events and a maximum duration of 284 ms. A second prompt saved a genuine zero for HTTP 451. Each turn made one successful create_data call. Both agreed with direct engine queries. The saved count used the supported total_hits value metadata accessor even though the count-only guidance recommends the row's doc_count and relation columns.

## Empty buckets and row-loop performance

On the local 100,000-event fixture, the subprocess executor returned an exact document count of zero for HTTP 429. Grouping all documents by region with a terms include-list containing only south produced zero rows with columns `regions`, `doc_count` and `peak`. Its exact hit total remained 100,000. Trying to read an undeclared metric with a zero fallback raised an error listing those three columns. Empty buckets keep names determined by the request; some aggregation outputs have data-dependent keys that cannot be inferred before a bucket exists.

Pandas 2.3.3 copies DataFrame attrs during row selection. The default table used to carry the entire raw aggregation tree, so each iteration copied all buckets. A synthetic terms aggregation with one average per bucket was timed on the same machine before and after the change. Construction was outside the timed loop. Each entry below is the median of three runs of `sum(df.iloc[i]["avg"] for i in range(len(df)))`. The checksum was verified on every run.

| Buckets | Before | After |
| ---: | ---: | ---: |
| 500 | 388 ms | 3.82 ms |
| 1,000 | 1,655 ms | 7.88 ms |
| 2,000 | 7,963 ms | 15.72 ms |

At 5,000 buckets, a separate single-run comparison took 49.02 s on the parent implementation and 38.5 ms on the updated implementation. The synthetic response contained one bucket per integer i, with key i, doc_count 1 and avg.value i.

Two further localhost report questions used Claude Haiku 5.5. Each turn saved a table on its first create_data call. The retry-event breakdown returned 5,000 events per service, with checkout highest at 306 ms. The blank southern dashboard question returned 100,000 events overall and no southern services. Direct terms and maximum aggregations independently confirmed the answers. Haiku questioned the uniformity of the retry counts; the fixture has precisely 20,000 events whose event_kind equals retry.

## Parent counts and empty percentiles

Repeat the opt-in contract test above against both engines. A region with one document and no matching child keeps its count of one. The missing child key and child count are null. A match-all filter with three documents keeps its count even when its child has no buckets. An empty histogram exposes the requested p95 and pipeline columns with zero rows. Reading an undeclared percentile raises a missing-column error.

The same cases ran through the subprocess executor against the 100,000-event Elasticsearch fixture. All four service parents retained 25,000 documents when a child terms include-list selected only billing. A filter on east retained 33,334 documents with an empty child. The southern hourly p95 table had zero rows. Reading the unrequested 59th percentile with a zero fallback raised a schema error.

Two Claude Haiku 5.5 questions exercised the report UI. Asked for event totals and counts over 307 ms per service, it kept all four services and returned 333 slow events each for billing and search and zero each for checkout and gateway. Asked why the southern hourly p95 chart was blank, it reported that the index contained no southern observations and left p95 uncalculated.
