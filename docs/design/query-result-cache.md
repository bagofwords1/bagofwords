# Query Result Cache + Out-of-Core Results

Two independent, additive features that reduce how often we re-query data sources
and how much result data we hold in memory. Both default to the existing behavior;
neither changes `execute_query`.

- **Result Lake** (`app/ai/code_execution/result_lake.py`) — a process-local TTL
  cache of query results on disk (Parquet), so identical generated queries aren't
  re-run against the source.
- **LazyFrame** (`app/data_sources/clients/lazy_frame.py`) — an opt-in out-of-core
  query path (`execute_query_lazy`) that streams results to disk and returns a
  DuckDB-backed handle instead of a full `pandas.DataFrame`.

Guiding principle: **a cache miss / a fallback is always correct.** Every new path
degrades to the existing behavior on any failure, and never returns wrong data.

---

## Result Lake (caching)

### How it works
At the single seam `QueryCapturingClientWrapper.execute_query`
(`app/ai/code_execution/code_execution.py`), every connector's query is keyed by
`sha256(data_source_id : connection_id : client_key : normalized_sql)`. On a hit
within TTL the result is read back from Parquet; on a miss the source is queried,
the result spilled to Parquet, and the entry indexed. Eviction is **cost-aware**:
`value = (cost_ms × (hits+1)) / size_bytes` — expensive, reused results are kept,
cheap ones dropped first.

### Configuration (env)
| Var | Default | Meaning |
|---|---|---|
| `BOW_RESULT_CACHE_ENABLED` | `0` (off) | Master switch |
| `BOW_RESULT_CACHE_DIR` | `<tmp>/bow_result_cache` | Parquet cache directory |
| `BOW_RESULT_CACHE_MAX_BYTES` | 2 GiB | On-disk budget (triggers eviction) |
| `BOW_RESULT_CACHE_TTL_SECONDS` | 300 | Freshness bound |
| `BOW_RESULT_CACHE_MIN_COST_MS` | 250 | Don't cache queries cheaper than this |
| `BOW_RESULT_CACHE_SUBSUMPTION` | `0` (off) | Serve a narrower query from a cached full table scan |

Streaming sources (Druid/Pinot) get a tighter TTL automatically (`min(ttl, 30s)`).

### Subsumption (off by default)
When `BOW_RESULT_CACHE_SUBSUMPTION=1`, a query can be served from a cached **full
table scan** of the same single table without re-querying the source. Example: with
`SELECT * FROM orders` cached, `SELECT region, SUM(amount) FROM orders WHERE country='US'
GROUP BY region` is computed by rewriting the table reference onto the cached Parquet and
running it in DuckDB. It applies on both the DataFrame and lazy paths (after an exact miss).

Safety (see `subsumption.py`): only when *provably* sound — the cached query is a full
scan (`SELECT <cols|*> FROM t` with no WHERE/GROUP/HAVING/DISTINCT/LIMIT/aggregates), the
new query reads the same single table (no joins), and every referenced column exists in the
cached result. Anything unanalyzable, or any execution error, falls back to a normal miss.
Requires `sqlglot`; if absent, subsumption is silently disabled.

### Known limitations
- TTL-only freshness — no version-token / change-feed invalidation yet. Stale-within-TTL
  is the only guarantee; no force-refresh, no surfaced cache age.
- Cache spills plaintext Parquet to local disk — review for multi-tenant / sensitive data.

### Unified with streaming
The cache and the out-of-core path share one key and one Parquet store. The streamed
result of `execute_query_lazy` IS the cache artifact: on a miss the streamed Parquet is
adopted into the cache (`register_path`); on a hit the cached Parquet is hardlinked to a
private file and returned as a `LazyFrame` (`get_owned_copy`) — no re-query, no full
materialization. A DataFrame cached by `execute_query` and a LazyFrame from
`execute_query_lazy` for the same query interoperate (same key/path). Returned handles
own a private hardlink, so cache eviction can't pull the file out from under an in-flight
LazyFrame.

---

## LazyFrame (out-of-core, opt-in)

### How it works
`client.execute_query_lazy(sql)` streams the result to a temp Parquet file and
returns a `LazyFrame` — a thin handle over a DuckDB relation. Filtering/aggregation
run out-of-core in DuckDB; only `.to_df()` / `.to_arrow()` materialize (intended for
the *reduced* result). `.sql("... FROM data ...")` runs SQL against the handle.

The wrapper exposes `QueryCapturingClientWrapper.execute_query_lazy`, which routes the
lazy path through the **same** query capture, per-query timeout, and usage-quota
controls as `execute_query` (sizing uses the on-disk Parquet size, not materialization).

### Streaming strategies
- **SQLAlchemy** clients: server-side cursor + `pd.read_sql(chunksize)` → Parquet.
- **Raw DBAPI** (`pd.read_sql`): chunked read over the DBAPI connection.
- **DBAPI cursor**: `cursor.fetchmany` loop.
- **Native DuckDB**: `COPY (sql) TO parquet` — zero Python materialization.
- **Arrow** (BigQuery/ClickHouse): native Arrow batch/stream → Parquet.
- **Pagination** (Athena chunksize, NetSuite/Salesforce/Mongo/Spark): page/batch iterator → Parquet.

All enforce a row/byte cap (`BOW_LAZY_MAX_ROWS`, `BOW_LAZY_MAX_BYTES`,
`BOW_LAZY_CHUNKSIZE`, `BOW_LAZY_DIR`) and raise `ResultTooLargeError`
(`query.result_too_large`, HTTP 413) on oversized scans, deleting the partial file.

### Configuration (env)
| Var | Default | Meaning |
|---|---|---|
| `BOW_LAZY_CHUNKSIZE` | 50_000 | Rows/records per streamed chunk |
| `BOW_LAZY_MAX_ROWS` | 50_000_000 | Abort threshold (rows) |
| `BOW_LAZY_MAX_BYTES` | 8 GiB | Abort threshold (bytes) |
| `BOW_LAZY_DIR` | `<tmp>/bow_lazy` | Temp Parquet directory |

---

## Backend support matrix (out-of-core streaming)

**Streaming — bounded ingest peak or zero-load (25):**

| Strategy | Connectors |
|---|---|
| SQLAlchemy server-side cursor | Trino, Postgres, MySQL, MariaDB, MSSQL, Oracle, Presto, Snowflake |
| Raw DBAPI `read_sql` | SQLite, Teradata, Sybase |
| DBAPI `fetchmany` cursor | Redshift, Databricks, MS Fabric, Druid, Vertica |
| Native DuckDB `COPY` (zero-load) | DuckDB, QVD |
| Native Arrow stream | BigQuery, ClickHouse |
| Pagination / batch iterator | Athena, MongoDB, Spark Connect, NetSuite, Salesforce |

**Generic default only — full payload materialized once, then out-of-core downstream (17):**

| Connector | Why it can't stream |
|---|---|
| Timbr, Timbr A2A | `POST query/` returns one JSON payload — no cursor/pagination |
| Pinot | broker returns a single `resultTable` JSON |
| Azure Data Explorer (Kusto) | query results returned whole; streaming API is ingest-only |
| PostHog | single HogQL `POST` response |
| Tableau, Qlik, Sisense, PowerBI, PowerBI Report Server, Oracle BI | BI semantic-layer APIs return a full hypercube/result set; no row cursor |
| Google Analytics | small aggregate reports; non-SQL signature; near-zero memory benefit |
| google_drive, graph_drive | file/document connectors, not row queries |
| gcp, aws_cost, service_demo | misc/demo, monolithic responses |

These still get the `LazyFrame` API (and out-of-core downstream compute) via the
base-class default — they just materialize the full result once at fetch, because
their upstream API exposes no streaming primitive.

---

## Codegen integration (opt-in)
The coder agent prompt now documents `execute_query_lazy` as the option for large/expensive
scans, with the `.sql(... FROM data ...)` → `.to_df()` pattern. `execute_query` (DataFrame)
remains the default, so existing behavior is unchanged. As a safety net, the executor
auto-materializes a `LazyFrame` if generated code returns one instead of a DataFrame, so the
downstream DataFrame contract (widgets, `step.data`, serialization) holds regardless. Lazy
use is therefore the model's choice for big results, never forced.

## What's intentionally NOT done yet
- **Lazy is opt-in, not default** — `execute_query` (DataFrame) is still the default path;
  the model elects `execute_query_lazy` for large scans. A full default-to-lazy switch would
  require the persistence layer to store handles natively (see `step.data` below).
- **`step.data` persistence** still serializes full results to JSON (memory/DB bloat).
- **Live verification** — the Arrow/pagination overrides (BigQuery, ClickHouse, Spark,
  Mongo, Vertica, Druid, NetSuite, Salesforce, Athena) mirror each SDK's documented API
  but have not been run against a live service. Blast radius is contained because
  `execute_query_lazy` is separate and unused by the app today.
- **Cache invalidation** beyond TTL, and at-rest encryption of spilled Parquet.
