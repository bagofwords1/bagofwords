# Design + Sandbox Feedback Loop — Result Store (spill / slice / cite)

Designs and validates the **Result Store**: any large tool
result — `create_data`, `execute_mcp`, `custom_api` — becomes a **durable,
encrypted, sliceable artifact** on a shared NFS mount, with a **handle row in
the backend DB**, so the agent can page / grep / aggregate over log-scale
output instead of losing everything beyond a 48 KB preview.

This is the enabler for **NOC / RCA workloads** (Splunk-, Elastic-,
Prometheus-scale results) and the storage substrate for the future
**evidence/audit chain** (a cited conclusion must resolve to the exact,
immutable data the agent saw).

| shell analogue | agent surface | mechanism |
| --- | --- | --- |
| `less +N` / paging | `read_query(page=…)` | DuckDB `LIMIT/OFFSET` over the artifact |
| `grep -E` | `read_query(match=…)` | DuckDB `regexp_matches()` pushdown |
| `awk '{sum…}'` / `wc -l` | `read_query(sql=…)` (SELECT-only) | DuckDB aggregation in-file |
| `cut -f` | `read_query(columns=[…])` | column projection |
| time slicing | `read_query(time_range=…)` | zone-map row-group skipping (time-sorted write) |

Status: **design — nothing below is built yet.** Every loop is written to be
runnable in a fresh cloud sandbox the moment implementation starts, in the same
shape as `sandbox-feedback-loop-network-dir-connector.md`.

---

## Problem (verified in code, with references)

1. **The full result of a large query is never persisted.**
   `format_df_for_widget` caps stored rows at org setting `limit_row_count`
   (default **1000**) — `backend/app/ai/code_execution/code_execution.py:931-961`.
   Everything past row 1000 is discarded at the source.
2. **The LLM sees at most a 48 KB head+tail preview** of the latest
   observation (`DEFAULT_PREVIEW_BUDGET_BYTES = 48_000`,
   `backend/app/ai/context/data_preview.py:17`), and **older observations are
   compacted to 3 sample rows** (`SAMPLE_ROWS = 3`; stripping logic in
   `backend/app/ai/context/builders/observation_context_builder.py:45-102`).
3. **Recall exists only for DB-persisted results.** `read_query`
   (`backend/app/ai/tools/implementations/read_query.py`) re-reads a prior
   `create_data` result by id — but only the ≤1000-row stored slice, with no
   filter/page/grep.
4. **Opaque tool output has no recall at all.** `execute_mcp` materializes
   tabular results to a CSV that feeds the observation
   (`backend/app/ai/tools/implementations/execute_mcp.py:214-224`);
   `custom_api` returns raw JSON. Once the preview window moves on, the middle
   of a 100k-line Splunk/Elastic result is **gone with no re-fetch handle**.

Consequence: the agent cannot do narrative log analysis ("read the lines
around 14:32:07"), cannot re-examine a large result from a different angle, and
a future audit chain has nothing immutable to cite.

Design references (what we deliberately do *not* copy): HolmesGPT spills
oversized results to a **node-local tempfile** re-read via shell `cat` — which
breaks across replicas, dies with the container, and **silently drops data on
write failure**. Every choice below exists to avoid those failure modes.

---

## Decisions (locked)

| # | Decision | Choice | Why |
|---|----------|--------|-----|
| D1 | Handle vs payload | **Handle row in backend DB; payload file on shared storage** | RBAC, audit FKs, multi-pod discovery in DB; scale on disk |
| D2 | Storage medium (now) | **NFS mount** behind an `ArtifactStorage` interface | on-prem/air-gap friendly; object store (S3/MinIO) is a later impl of the same interface |
| D3 | Payload format | **One encrypted DuckDB database file per artifact** (table `data` + table `_meta`) | single engine for page/grep/aggregate/project; columnar pushdown works *inside* the encrypted file |
| D4 | Encryption | **DuckDB native encryption (AES-256-GCM)**, key passed at `ATTACH` | at-rest encryption without losing partial reads; GCM is authenticated → tamper-evidence |
| D5 | Key management | **Per-artifact data key, wrapped by the existing Fernet master** (pattern: `backend/app/models/llm_provider.py:88`), stored wrapped on the handle | rotation without re-encrypting files; per-tenant isolation; **crypto-shredding** for retention |
| D6 | Write policy | **Dual-write above a floor**: ≤ floor → DB only (today's path, unchanged); > floor → DB keeps preview + handle, NFS holds the full artifact | zero regression for the interactive BI path; full fidelity for large results |
| D7 | Producers | `create_data`, `execute_mcp`, `custom_api` all route through **one** artifact writer | this is what makes Splunk/Elastic (MCP/REST) output recallable — not just SQL results |
| D8 | Read surface | **Extend `read_query`** with slice parameters (page / match / columns / time_range / SELECT-only `sql`) | one recall tool; the agent already knows it; falls back to the DB preview when no artifact exists |
| D9 | Publish protocol | **Atomic**: tmp-write → fsync file → rename within mount → fsync dir → **only then** insert handle → only then replace context with pointer | no partial reads; no silent drops; handle row is the source of truth |
| D10 | Immutability | **Write-once.** Reruns create a new artifact (`superseded_by` link), never overwrite | cited evidence must stay frozen; reproduces past conclusions |
| D11 | Privacy | Every slice result passes `build_data_preview` / `gate_stats_for_privacy` under `allow_llm_see_data` | encryption protects at-rest; the gate protects what reaches the LLM — a spilled file must not become a privacy bypass |
| D12 | Bounded output | Every slice returns a **capped page** (≤ preview budget) + `total_matches` + cursor | a grep matching 50k rows returns a page, never the firehose |
| D13 | Retention | Governed on the **handle**: TTL-GC ∖ {`legal_hold`, `cited`}; crypto-shred on expiry; keep-last-N per scheduled task | finite NFS capacity; DORA-scale hold for cited evidence |
| D14 | Performance | Time-sort on write when a timestamp column is detected (`ts_column` on handle); right-sized row groups; warm read-only connection cache | telemetry RCA is time-window queries; zone maps make them cheap |

### Non-goals (this iteration)

- **Streamed/chunked writes for larger-than-RAM results.** The sandbox
  materializes a full pandas df from `generate_df`
  (`code_execution.py:630-689`) before the writer sees it. Up to ~GB-scale is
  fine; true multi-GB streaming is the flagged next frontier.
- Object-store backend (interface is ready for it; not built now).
- The audit-chain schema itself (separate design). The handle carries `cited`
  + `legal_hold` so that work lands on this substrate without migration churn.
- Content-addressed dedup of identical reruns (nice-to-have; the
  `content_sha256` field enables it later).
- UI browsing of artifacts.

---

## Data model — `result_file` (the handle)

| column | type | notes |
|---|---|---|
| `id` | uuid pk | the citable id |
| `organization_id` | fk | RBAC scope (checked on every slice) |
| `report_id` / `step_id` / `query_id` | fk, nullable | provenance into existing entities |
| `tool_execution_id` | fk | which call produced it |
| `producer` | enum | `create_data` \| `execute_mcp` \| `custom_api` |
| `content_type` | enum | `table` \| `events` \| `text` |
| `schema_json` | json | column names/types as written |
| `ts_column` | text, nullable | detected timestamp column (enables `time_range`) |
| `row_count` / `byte_size` | int | true totals |
| `storage_ref` | text | path relative to the mount root (never absolute) |
| `format` | text | `duckdb` (versioned for future formats) |
| `content_sha256` | text | integrity / tamper-evidence / future dedup |
| `wrapped_key` | text | per-artifact key, Fernet-wrapped by master |
| `status` | enum | `published` \| `tombstoned` |
| `expires_at` | ts, nullable | TTL (org retention_class default) |
| `legal_hold` | bool | never GC'd while true |
| `cited` | bool | set by the future audit chain; implies hold |
| `superseded_by` | uuid, nullable | rerun lineage |
| `created_at` | ts | |

Payload layout inside each `.duckdb` file: table **`data`** (the result,
time-sorted when `ts_column` exists) + table **`_meta`** (producer, source
query/params, created_at, content hash) so a file is self-describing even if
found on disk alone.

NFS layout: `<mount>/artifacts/{org_id}/{yyyy}/{mm}/{result_file_id}.duckdb`,
tmp writes under `<mount>/.tmp/`. Rename happens **within the same mount**
(atomic on a single NFS filesystem). No reliance on NFS locking anywhere —
write-once + read-only multi-attach never needs it.

---

## Write path (producer → artifact)

```
tool result (df / rows / json)
  └─ size ≤ floor?  ──yes──►  DB only (today's path, byte-for-byte unchanged)
      │no
      ├─ 1. mint data key; open DuckDB at <mount>/.tmp/{id}.duckdb.tmp with ENCRYPTION_KEY
      ├─ 2. normalize + write `data` (ORDER BY ts_column if detected) + `_meta`; compute sha256
      ├─ 3. fsync file → rename into artifacts/… → fsync directory
      ├─ 4. INSERT handle row (single transaction; wrapped_key, sha, counts)
      ├─ 5. DB also stores the existing ≤1000-row preview (widgets + first LLM look unchanged)
      └─ 6. observation carries preview + { result_file_id, row_count, "sliceable via read_query" }
```

**Failure contract (D9):** any failure in 1–4 → the observation says loudly
*"full result NOT retained — only the preview below is available"* (an
explicit `spill_failed` flag, surfaced in traces). Never Holmes' silent drop,
never a handle pointing at a missing/partial file.

Floor: org settings `artifact_floor_rows` (default = `limit_row_count`, 1000)
and `artifact_floor_bytes` (default 512 KB) — spill when either is exceeded.
Feature-gated by org setting `enable_result_store` (beta, default off).

Producer normalization (D7):
- `create_data` → df → `data` as-is.
- `execute_mcp` / `custom_api` → tabular JSON → columns; variable JSON docs →
  extracted common columns + `raw` JSON column; raw text → `(line_no, line)`
  (+ parsed `ts` when detectable).

## Read path (`read_query` extension)

New optional inputs (all combinable, all optional — bare call keeps today's
behavior exactly):

```
read_query(
  query_ids | result_file_id,
  page:       { offset, limit },
  match:      { regex, column? },          # regexp_matches pushdown
  columns:    [ … ],                        # projection
  time_range: { from, to },                 # requires ts_column
  sql:        "SELECT … FROM data …"        # SELECT-only, single fixed table
)
```

Guards, in order, on every slice:
1. **RBAC** — handle's org (and report scoping) exactly as `read_query` does
   today (`read_query.py:96-99`).
2. **SELECT-only validation** for `sql` — reuse the posture of
   `validate_sql_query` (`code_execution.py:270-344`): reject
   `ATTACH/COPY/INSTALL/LOAD/PRAGMA/EXPORT/read_*()` and any table other than
   `data`; allowlist of scalar/aggregate functions. The DuckDB connection is
   additionally opened read-only with external access disabled — validation is
   defense-in-depth, not the only wall.
3. **Privacy gate (D11)** — results flow through `build_data_preview` /
   `gate_stats_for_privacy`; with `allow_llm_see_data=false`, `match` returns
   **counts and line numbers only**, never raw rows, and `sql` is restricted
   to aggregates.
4. **Bounded page (D12)** — ≤ `limit_row_count` rows and ≤ preview byte
   budget, plus `total_matches` and a `next_offset` cursor.

Sandbox composition: `load_step(result_file_id_or_step)` in
`code_execution.py:733-763` gains artifact resolution with the same optional
filter args — cross-source joins (Prometheus × Splunk) load *slices*, not
everything.

## Retention & rerun (governed on the handle)

GC job (APScheduler, like `scheduled_reindex.py`):
- walk **handles**, never the filesystem first:
  `now > expires_at AND NOT legal_hold AND NOT cited` →
  **crypto-shred** (null `wrapped_key` — instantly unrecoverable) → delete
  file → `status=tombstoned` (row kept for audit).
- **orphan sweeps both directions**: file with no handle older than grace
  period (crashed publish) → delete; `published` handle with missing file →
  alarm (must be impossible if D9 holds — treat as a sev bug).
- **keep-last-N per scheduled prompt** so recurring investigations don't fill
  the mount.

Rerun semantics (D10): a rerun **re-queries the live source** and publishes a
**new** artifact (`superseded_by` on the old one). A past conclusion always
resolves to its frozen original. Overwriting a `published` artifact is not an
API that exists.

---

## Preflight — verify before writing a line of feature code

These are cheap scripted probes; any failure changes the design (fallback:
plain Parquet on an encrypted volume + DuckDB reads), so they run first.

- [ ] **P1 — DuckDB native encryption**: the pinned python `duckdb` version
      (must be ≥ the release that shipped `ATTACH … (ENCRYPTION_KEY …)`;
      verify exact minimum) can create, write, close, re-attach **read-only**
      an encrypted db; wrong key **fails closed** with a clean error.
- [ ] **P2 — Multi-process read over NFS**: two processes on the mount attach
      the same encrypted file read-only concurrently and both slice correctly.
- [ ] **P3 — Rename atomicity**: writer killed (`kill -9`) between tmp-write
      and rename leaves **nothing** at the final path; killed after rename
      leaves a complete, attachable file.
- [ ] **P4 — Throughput floor**: encrypted write + point-slice on a 10M-row
      synthetic table meets the Loop B budgets on sandbox hardware.

---

## Loop A — Store invariants (unit/integration, no network, no LLM)

Fixture: `scripts/gen_artifact_fixtures.py` — synthetic tables at 10 / 10k /
1M rows (mixed types, a timestamp column, unicode incl. Hebrew, a planted
needle row) + variable-schema JSON events + raw text log lines.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest \
  tests/unit/test_artifact_store.py \
  tests/unit/test_artifact_slice.py \
  tests/e2e/test_artifact_store_e2e.py -v
```

Must pin, at minimum:

1. **Floor behavior** — result ≤ floor → **no file, no handle**, DB path
   byte-identical to today (regression guard on the hot BI path).
2. **Publish atomicity** — writer killed mid-write → no handle, no file at
   final path, `.tmp` orphan swept; killed pre-INSERT → complete file, no
   handle, orphan sweep deletes it. **A `published` handle always attaches.**
3. **Encryption fails closed** — wrong/absent/shredded key → clean error, no
   plaintext anywhere on disk (scan the raw file bytes for known plaintext
   markers from the fixture).
4. **Integrity** — `content_sha256` mismatch on read → error, flagged.
5. **Slice correctness** — page windows exact and stable; `match` regex
   returns exactly the planted rows (+ correct `total_matches` with capped
   page); projection; `time_range` returns exactly the window; SELECT-only
   `sql` aggregates match pandas ground truth.
6. **SQL jail** — attack corpus (`ATTACH`, `COPY TO`, `INSTALL`, `LOAD`,
   `PRAGMA`, `read_csv('/etc/passwd')`, second table, multi-statement,
   comment-smuggled DDL) → every one rejected; connection is read-only +
   external access disabled even if validation were bypassed.
7. **Privacy gate** — `allow_llm_see_data=false` → no raw cell reaches any
   slice output (rows path, match path, sql path); aggregates still work.
8. **Bounded page** — a match with 50k hits returns ≤ budget bytes + cursor;
   walking the cursor reconstructs the full set exactly once.
9. **Retention state machine** — expired+uncited → shredded+tombstoned+file
   gone; `cited` / `legal_hold` survive GC even when expired **and** when
   their report is deleted; keep-last-N prunes correctly; tombstoned handle
   answers with a clear "shredded per retention" error, not a 500.
10. **Rerun immutability** — rerun produces a new artifact + `superseded_by`;
    the old artifact still slices identically (frozen).
11. **Producer parity** — the same logical data via `create_data`,
    `execute_mcp`, `custom_api` yields artifacts that slice identically.

### Negative controls (the harness has teeth)

- Temporarily disable the publish-order rule (insert handle **before**
  rename): the kill-mid-write test must now **fail** with a handle pointing
  at nothing — proving invariant 2 is what makes the fixed run pass.
- Temporarily skip the privacy gate on the `match` path: invariant 7 must
  fail with raw rows in the output.
- Run GC with the `cited` guard removed: invariant 9 must fail with cited
  evidence deleted.

Regression: full existing suites for `create_data` / `read_query` / widgets /
observation compaction run green with the feature flag **on** and floor at
default — nothing below the floor changes.

---

## Loop B — Scale & fast iterations (perf budgets as tests)

Fixture: 10M-row / multi-GB synthetic log table (time-ordered, planted
anomalies at known offsets), written once per sandbox.

Budgets asserted (tunable constants in the test, not vibes — numbers to be
calibrated on sandbox hardware in the first run, then **pinned**):

- Encrypted write of 10M rows: bounded wall-clock; **writer RSS delta stays
  under a fixed ceiling** (no full second copy of the df).
- Point `time_range` slice (5-min window) on cold attach: **< 2 s p95**; warm
  attach (connection cache): **< 300 ms p95**.
- `match` regex over all 10M rows: bounded seconds, returns capped page.
- 8 concurrent readers (multi-process) on one artifact: no errors, no lock
  contention, budgets hold within 2×.
- Tiny-file pressure probe: 1000 small-but-above-floor artifacts publish +
  GC cleanly (inode/latency sanity on NFS).

---

## Loop C — Multi-pod NFS (two containers, one mount)

docker-compose with two backend containers sharing the artifact volume:

- Pod 1 publishes during an agent run → Pod 2's `read_query` slices it by id
  (key unwrap from the shared DB) — **the HolmesGPT failure mode this design
  exists to kill**.
- Kill Pod 1 mid-publish → Pod 2 never sees a partial artifact; orphan sweep
  on either pod cleans the tmp.
- GC on Pod 2 shreds an artifact Pod 1 wrote; Pod 1's next read fails with
  the clean "shredded" error.

---

## Loop D — Live LLM confirmation (real model, needle-in-haystack)

Confirms the **LLM-facing contract**: with only the real tool metadata, a
model can *investigate* a result far larger than its context window.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
export ANTHROPIC_API_KEY=...   # never committed
.venv/bin/python scripts/artifact_store_live_agent.py
```

Scenario: a seeded connection returns **200k log lines** containing one
planted anomaly (a unique error signature at a known timestamp, absent from
the first/last 1000 lines so no preview can contain it). The task: *"find the
root-cause error in this log and quote the exact line."*

**PASS requires all of:**
1. The run spills (handle exists; preview + pointer in the observation).
2. The model reaches the needle **via slice calls** (`match` and/or
   `time_range` + `page`) — visible in the trace.
3. Final answer quotes the exact planted line + the correct timestamp, and
   references the artifact id.
4. **No single observation ever exceeded the preview byte budget** (asserted
   from the recorded trace — context safety held throughout).
5. Privacy variant: same run with `allow_llm_see_data=false` → model can
   still *locate* (counts + line numbers) but no raw line ever appears in any
   observation.

### Negative control

Same scenario with the feature flag **off** (today's behavior): the model
cannot find the needle (it's outside every preview) — proving Loop D passes
*because of* the artifact store, not because the needle leaked into the
preview or the model guessed.

---

## Rollout

1. Land behind `enable_result_store` (org setting, default off) with
   `artifact_floor_rows/bytes`; `ArtifactStorage` interface with `NfsStorage`
   as the only impl.
2. Loops A–C green in CI (A on SQLite + Postgres, C behind a compose profile).
3. Loop D run manually per release (records the trace as the acceptance
   artifact).
4. Beta on one internal workspace with a real Splunk/Elastic-shaped MCP
   source before customer exposure.

## What this will prove

- **App:** every large tool result — SQL or opaque — becomes durable,
  recallable, and sliceable by id, with zero change to the sub-floor BI path.
- **Safety:** encrypted at rest with per-artifact shreddable keys; SELECT-only
  read-only slicing; privacy gate holds on every read path; publish is atomic
  and never silently drops.
- **Scale:** log-scale results are investigated through bounded pages —
  context safety is enforced by construction, not by model discipline.
- **Live:** a real model, given only tool schemas, finds a needle in 200k
  lines it could never fit in context — the capability NOC RCA needs and the
  substrate the audit chain will cite.
