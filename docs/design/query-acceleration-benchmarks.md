# Custom queries on consumption-priced warehouses — measured

The original case for caching was on-prem: a legacy Oracle or SQL Server box
cannot take an agent's exploratory bursts, and the fix is to stop generating
them. On BigQuery and Snowflake that argument is weak — they scale fine. The
argument that replaces it is **cost**, because both bill for every scan and an
agent asks the same question many ways.

These are real numbers from live accounts, not projections.

## What was run

Six questions an agent would plausibly ask of one relation, run two ways:
against the source every time, and against a cached DuckDB artifact refreshed
once.

| | BigQuery | Snowflake |
|---|---|---|
| source | `chicago_taxi_trips.taxi_trips` | `TPCH_SF10.LINEITEM` |
| size | 213,111,447 rows / 77.3 GiB | 59,986,052 rows |
| cached relation | 89 rows, 524 KiB | 13 rows, 524 KiB |
| refresh | 1.1 s | 1.3 s |

## Results

| | BigQuery | Snowflake |
|---|---|---|
| scanned, 6 questions live | 21.56 GiB | 9.59 GiB |
| scanned, cached (one refresh) | 6.53 GiB | 1.60 GiB |
| **scan reduction** | **3.3×** | **6.0×** |
| wall clock, live | 9.0 s | 13.0 s |
| wall clock, cached | 0.010 s | 0.006 s |
| **latency reduction** | **886×** | **2126×** |
| on-demand cost (US, $6.25/TiB) | $0.1316 live → $0.0399 cached | — |

## The number that actually governs this

The ratios above are an artifact of choosing six questions. The honest framing
is a break-even: caching pays for itself once the agent asks more than *N*
questions between refreshes, where *N* is the refresh scan divided by the
per-question scan.

| source | per question | per refresh | break-even |
|---|---|---|---|
| BigQuery | 3.59 GiB | 6.53 GiB | **1.8 questions** |
| Snowflake | 1.60 GiB | 1.60 GiB | **1.0 questions** |

Both break even almost immediately, because the refresh runs the same scan a
single question would. But the corollary matters and should not be buried:

> **A cache refreshed more often than it is queried costs more than it saves.**

An hourly refresh nobody asks questions against is 24 full scans a day bought
for nothing. The schedule is a cost decision, not a freshness preference, and
the UI should eventually say so — showing projected scan-bytes per day next to
the interval picker would make the trade visible at the point it is made.

Latency is the opposite: 886× and 2126× hold regardless of question count,
because a local artifact does not care how often it is read.

## Why scan cost is modelled separately from result size

Both warehouses report what a query will **read**, and neither reports how big
the **result** will be:

- Snowflake `EXPLAIN USING JSON` → `GlobalStats.bytesAssigned`
- BigQuery dry run → `total_bytes_processed`, exact and free

Mapping either onto the result-size caps would refuse exactly the queries worth
caching. Every relation above scans gigabytes and produces a 524 KiB artifact;
under a conflated model each would have been rejected. `Estimate.scan_bytes`
is therefore a separate field with its own ceiling (100 GiB, far above the
2 GiB artifact cap) guarding a different failure: not "the artifact won't fit"
but "this refresh is expensive, every time it repeats".

BigQuery's dry run is the best estimator of any source supported. Postgres
reports planner guesses; Snowflake reports partitions it expects to touch;
BigQuery reports the number it will bill.

## Caveats

- Single-run timings, one region, one warehouse size. Snowflake's first query
  paid 7.3 s of warehouse resume; the rest averaged 1.1 s.
- BigQuery's per-question scan varies (1.59–4.95 GiB) because it is columnar
  and each question touches different columns. The per-question average is
  used for break-even.
- Query-result caching was disabled on the live side (`use_query_cache=False`).
  With it on, a *repeated identical* question is free — but agents rarely repeat
  a question verbatim, which is the whole reason the scans differ above.
- Snowflake cost is not quantified: it bills warehouse-seconds, not bytes, so a
  credit figure needs the account's rate. Scanned bytes are reported instead as
  the comparable measure.
