# Feedback Loop — `/monitoring/diagnosis` slow to load; "data should be paginated server side"

Reproduces the reported slowness of the **Monitoring → Diagnosis** page at
production-like data volumes. The report suspected client-side pagination; the
loop shows the table **is** paginated server-side (`LIMIT/OFFSET`), and that the
real cost is unbounded per-request aggregation work in the three backend calls
the page fires on every load — all of which grow linearly with the number of
agent executions in the date range.

---

## Root cause (validated)

The page (`frontend/pages/monitoring/diagnosis.vue`) fires three API calls on
load, and re-fires them on every filter/day/agent change:

1. `GET /api/console/agent_executions/summaries?page=N&page_size=10` — the table.
   Server-side paginated (`console_service.py:2363`), **but** each request also runs
   **two identical unfiltered `COUNT(*)` subquery scans** — the first
   (`console_service.py:2287-2289`) is dead code, its result is unconditionally
   recomputed after filters at `console_service.py:2359-2361`.
2. `GET /api/console/diagnosis/metrics` — the KPI cards + tab counts.
   `get_diagnosis_dashboard_metrics` (`console_service.py:2550`) runs **5
   sequential COUNT queries**; the two "low confidence / low instruction
   coverage" ones each do a double self-join through `completions`
   (`AgentExecution → system completion → parent user completion`), and they
   dominate the endpoint (measured below).
3. `GET /api/console/diagnosis/timeseries` — the activity chart.
   `get_diagnosis_timeseries` (`console_service.py:2690-2736`) **selects every
   `AgentExecution` row in the date range into Python** (no LIMIT, no GROUP BY)
   plus every failed-tool execution id, and buckets them by day in a Python
   loop — to return ~31 points. The same aggregation as a single SQL `GROUP BY`
   is ~5× faster (measured below).

Notes:

- The composite index `ix_ae_org_created (organization_id, created_at)` from
  `alembic/versions/perfidx01_hot_path_indexes.py` **exists and is used**
  (verified with `EXPLAIN QUERY PLAN`), so this slowness is *not* a missing
  index — it's per-row aggregation work repeated on every page interaction.
- The frontend's "All time" period sends only `end_date`;
  `_normalize_date_range` (`console_service.py:97-98`) silently turns that into
  *last 30 days*. That cap is the only thing bounding the timeseries row fetch
  today.
- Found in passing: `console_service.py:2256` uses Python `and` inside the
  outerjoin condition — `Report.id == AgentExecution.report_id and
  Report.report_type == 'regular'` evaluates to just the first comparison
  (SQLAlchemy `==` expressions are falsy unless comparing the same column), so
  the `report_type` restriction is silently dropped. Needs `and_(...)`.

## Loop A — seed + benchmark (fresh SQLite sandbox, no LLM)

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD=dummy ANTHROPIC_API_KEY=dummy
mkdir -p db && rm -f db/app.db
uv run alembic upgrade head
uv run python main.py &          # backend at :8000

# admin + org
BASE=http://localhost:8000
curl -s -X POST $BASE/api/auth/register -H "Content-Type: application/json" \
  -d '{"email":"sandbox@bow.dev","password":"Sandbox123!","name":"Sandbox Admin"}'
TOK=$(curl -s -X POST $BASE/api/auth/jwt/login \
  -d "username=sandbox@bow.dev&password=Sandbox123!" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
ORG=$(curl -s $BASE/api/organizations -H "Authorization: Bearer $TOK" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
echo "$TOK" > /tmp/token.txt; echo "$ORG" > /tmp/org.txt

# seed tiers (cumulative) and benchmark after each
uv run python scripts/seed_diagnosis_perf.py 5000
uv run python scripts/bench_diagnosis_endpoints.py /tmp/token.txt /tmp/org.txt
uv run python scripts/seed_diagnosis_perf.py 20000    # -> 25k
uv run python scripts/bench_diagnosis_endpoints.py /tmp/token.txt /tmp/org.txt
uv run python scripts/seed_diagnosis_perf.py 75000    # -> 100k
uv run python scripts/bench_diagnosis_endpoints.py /tmp/token.txt /tmp/org.txt
uv run python scripts/seed_diagnosis_perf.py 200000   # -> 300k
uv run python scripts/bench_diagnosis_endpoints.py /tmp/token.txt /tmp/org.txt

# service-level profile (SQL statement counts, GROUP BY comparison, index proof)
uv run python scripts/profile_diagnosis_perf.py
```

Each execution gets the rows the endpoints join against: user+system
completion pair, 3 tool executions (~5% failed, `create_data` failures =
"failed queries"), ~7% `status='error'`, ~10% feedback (⅓ negative), spread
uniformly over the last 30 days.

### Observed (bug reproduced) — endpoint medians, warm, local SQLite

| agent executions in range | summaries p1 (table) | summaries deep page | diagnosis/metrics (KPIs) | diagnosis/timeseries (chart) | page-load critical path |
|---|---|---|---|---|---|
| 5,000 | 35 ms | 41 ms | 91 ms | 63 ms | **91 ms** |
| 25,000 | 142 ms | 200 ms | 625 ms | 303 ms | **625 ms** |
| 100,000 | 656 ms | 949 ms | 3,141 ms | 1,804 ms | **3,141 ms** |
| 300,000 | 2,289 ms | 3,448 ms | 11,296 ms | 5,733 ms | **11,296 ms** |

Perfectly linear in row count (~4× rows → ~4-5× latency); the page paints when
the slowest of the three parallel calls returns. This is warm local SQLite —
production Postgres adds network round-trips and cold cache on top, and the
whole set re-fires on every tab click, chart-bar click, or agent change.

### Where the time goes (`profile_diagnosis_perf.py` @ 300k)

```
get_diagnosis_timeseries:         5,388 ms, 2 SQL stmts, returns 31 points
                                  (fetches all 300k AE rows into Python first)
same aggregation in SQL GROUP BY: 1,146 ms, 1 SQL stmt, returns 31 rows   ← ~5x

get_agent_execution_summaries:    2,329 ms, 10 SQL stmts for 10 rows
                                  (2 identical COUNT(*) scans — one dead)
get_diagnosis_dashboard_metrics: 11,146 ms, 5 sequential COUNT-with-join queries

EXPLAIN QUERY PLAN: SEARCH agent_executions USING COVERING INDEX ix_ae_org_created
                    (organization_id=? AND created_at>?)          ← index IS used
```

Per-query breakdown of the 11.1s KPI endpoint (sequential awaits, so they sum):

| KPI count | time @ 300k |
|---|---|
| low_confidence (completions double self-join) | 3,780 ms |
| low_instruction_coverage (same self-join again) | 3,748 ms |
| failed_queries (join tool_executions) | 1,638 ms |
| negative_feedback (join completion_feedbacks) | 1,127 ms |
| total (plain count, covering index) | 23 ms |

## Candidate fix (not yet applied)

1. **Timeseries → SQL aggregation**: replace the fetch-all-rows loop with one
   `GROUP BY date(created_at)` (counting errors via `status='error' OR EXISTS
   failed tool`), ~31 rows returned instead of N. Measured ~5× on SQLite alone;
   also removes N-row ORM materialization and transfer.
2. **Summaries**: delete the dead pre-filter COUNT (`console_service.py:2287-2289`).
3. **KPI metrics**: run the 5 counts concurrently (or fold into one query with
   conditional aggregation); precompute the scores join or index
   `completions(parent_id)` — the two self-join counts are the endpoint's long
   pole at every tier.
4. Optionally cache/share the KPI response between the cards and the filter-tab
   counts (the frontend already uses one call for both; the re-fetch per
   day-click could reuse the timeseries buckets it already has).

Iterate here: apply a fix, re-run the two scripts at the 300k tier, and the
corresponding row in the table should drop to near the SQL-GROUP-BY floor
(timeseries) / roughly halve (summaries) / drop several-fold (metrics).

## What this proves / regression notes

- The diagnosis table is already server-side paginated; "paginate the table"
  would not fix the reported slowness.
- All three endpoints scale linearly with executions-in-range even with the
  `perfidx01` composite index in place; the cost is aggregation shape, not
  index absence.
- The 30-day default hidden inside `_normalize_date_range` is currently the
  only bound on the timeseries row fetch — a true "all time" option would make
  it unbounded.

## Repro artifacts

- `backend/scripts/seed_diagnosis_perf.py` — bulk-seeds N agent executions +
  completions + tool executions + feedback for the existing org (cumulative).
- `backend/scripts/bench_diagnosis_endpoints.py` — times the exact API calls the
  page fires (median/min/max) + page-load critical path.
- `backend/scripts/profile_diagnosis_perf.py` — service-level wall time, SQL
  statement counts, SQL-GROUP-BY comparison, `EXPLAIN QUERY PLAN` index proof.
