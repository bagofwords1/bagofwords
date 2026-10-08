# ClickHouse custom tables — feedback loop

**Goal:** offer custom tables (scheduled, locally cached materializations) on
ClickHouse connections, so agents answer from a DuckDB copy instead of waking
the ClickHouse service on every question.

## Change

- `backend/app/data_sources/fast/clickhouse_source.py` — native extraction
  source: `EXPLAIN ESTIMATE` + `system.parts` for scan cost, server-bounded
  preview (`max_result_rows` / `result_overflow_mode=break`), Arrow streaming
  via `query_arrow_stream`, `KILL QUERY` by `query_id` on early stop,
  `max_execution_time` backstop.
- `clickhouse_client.py` — declares `EXTRACTION_SOURCE`; the clickhouse-connect
  client is now built lazily.
- `custom_query_service.py` — `clickhouse` added to `VERIFIED_TYPES`.

## Environment

- ClickHouse 24.8.4.13 (official static binary), `shop.orders` = 20,000,000 rows
  (184 MiB compressed) with `LowCardinality`, `Enum8`, `Decimal`, `Nullable`,
  `DateTime64`, `Array` columns. Password user `bow` (empty passwords are
  dropped by the connection form).
- Full BOW stack (backend :8000, Nuxt dev :3000, sqlite), Claude Haiku 5.5 as
  the only enabled model, `enable_custom_queries` on.

## What the live run found (a fake would not have)

1. **Session lock.** The shared clickhouse-connect client carries a session id;
   a stream abandoned mid-read left it `SESSION_IS_LOCKED`, refusing the kill
   and the agent's next query. Extraction now uses its own session-less client.
2. **Enums become integers.** Arrow has no enum type; `status` materialized as
   `1/2/3`. Enum columns are detected with `DESCRIBE` and cast to String.
3. **Agent turns woke the source with no data query.** Before the lazy client,
   a chat answered entirely from the cache still sent **9** queries to
   ClickHouse (3× `get_client` probes: `version()`, `system.settings`,
   `SELECT 1 AS check`). On ClickHouse Cloud that wakes an idle service and
   bills compute — the opposite of the point. After: **0**.

## Verification

| Check | Result |
|---|---|
| Estimate on `GROUP BY` over 20M rows | `scan_bytes` 184 MB, `~20,000,000 rows read`, no result size |
| Preview (UI) | 12 rows, enum labels, "Scans 184 MB at the source per refresh" |
| Refresh | `last_refresh_status=ok`, 12 rows, 524 KiB artifact, ~1.25 s |
| Row-cap abort on `SELECT *` (cap 150k) | `ExtractionAborted`; `KILL QUERY` logged; 0 `bow-extract-*` left in `system.processes` |
| Agent query after abort | succeeds (no session lock) |
| Zero-row / `Decimal256` / `UInt64` max | shape kept / widened to double / stored as uint64 |
| Haiku 5.5 chat (UI) | answered from `revenue_by_region_status` |
| ClickHouse queries during that chat | 9 before lazy client → **0** after |
| Unit tests (`test_extraction_sources`, `test_fast_sql_dialect`, `test_custom_queries`) | 126 passed |

Screenshots in `clickhouse-custom-tables/`: `01-preview-modal.png`,
`02-custom-table-active.png`, `03-haiku-answer.png`,
`04-haiku-answer-no-source-traffic.png`.

## Not covered

- ClickHouse Cloud itself (idle/wake billing was not measured against a real
  service); TLS (`secure=True`) path. Note `clickhouse_client.py` passes
  `verify=not secure`, disabling cert verification on TLS — pre-existing, out
  of scope here.
- `Map`/`Tuple`/`IPv4`/`UUID` columns into DuckDB were not exercised live.
