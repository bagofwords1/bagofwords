# Feedback Loop — "All time" on the monitoring tabs shows the last 30 days

Report: on `/monitoring` (Explore), `/monitoring/cost` and `/monitoring/diagnosis`
the "All time" period behaves like "Last 30 days".

## Root cause (validated in the sandbox)

The pages and the API disagreed about what "no range" means.

- **Explore and Cost** send no `start_date` for "All Time"
  (`ConsoleOverview.vue`, `cost.vue`). Every console endpoint runs the range
  through `ConsoleService._normalize_date_range`, which filled a missing start
  with `end - 30 days`. `get_metrics_with_comparison` had its own copy of the
  same default, so the KPI cards also showed a "+100% vs previous period" trend
  computed against the 30 days before that.
- **Diagnosis** had no "All time" preset at all (`TimeRange.vue`: 24h / 7d /
  30d / 90d / custom, default 30d) and `GET /console/diagnosis/runs` rejected
  a request without `start`/`end` (`400 start and end are required`).

`docs/feedback-loops/diagnosis-filters.md` had already noted the 30-day
default as a "pre-existing quirk".

## Loop A — reproduction (sandbox stack, seeded runs)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
# scratch script: 5 agent runs at 100/75/45/20/2 days ago (+ usage rows),
# org created_at back-dated 120 days, diagnosis rollup run.
```

HTTP layer, no dates (what the UI sends for "All time"), before the fix:

| endpoint | expected | got |
|---|---|---|
| `GET /console/metrics` → `total_messages` | 10 (5 runs × user+system) | 4 |
| `GET /console/metrics/comparison` | totals, no trend | current 4, previous 2, `period_days` 30 |
| `GET /console/agent_executions/summaries` → `total_items` | 5 | 2 |
| `GET /console/metrics/cost` → `total_calls` | 5 | 2 (range Aug 18 – Sep 17) |
| `GET /console/diagnosis/runs` (no `start`/`end`) | 200, 5 runs | 400 |

Playwright over the real UI (`scratchpad/pw/monitoring.js`) confirmed the same
on screen: Explore on "All Time" shows 4 messages and 2 LLM calls; Diagnosis
offers no "All time" option and lists 2 of 5 runs; Cost on "All Time" keeps the
30-day totals.

## The fix

Backend
- `_normalize_date_range(start, end, organization)`: a missing start is now
  the organization's creation day (`ConsoleService.all_time_floor`). Nothing
  in an org predates the org, so this bounds every query with no extra
  round trip and keeps the day-by-day zero-fill in the timeseries/cost
  endpoints proportional to the org's age. All 11 callers pass the org.
- `get_metrics_with_comparison` uses the same normalization; with no start
  there is no previous period, so `changes` is `{}` and the cards show plain
  totals instead of a made-up trend.
- Diagnosis: `_diag_params` defaults `start` to the same floor and `end` to
  now, so the runs and facets endpoints accept "no range". The histogram
  gains a `month` granularity for spans longer than `MAX_BUCKETS` weeks, so
  an old org's all-time chart covers the whole range instead of stopping
  2.3 years in.

Frontend
- Diagnosis gets an "All time" preset (`range=all` in the URL). It sends no
  `start`/`end`; the chart labels month buckets and a click on one narrows
  to `created:YYYY-MM`. Switching from all time to a custom range seeds the
  inputs from the last 30 days rather than 1970.
- Explore and Cost keep sending no `start_date` for "All Time"; the backend
  change is what fixes them.

## Loop B — after the fix

Same probes, no dates: `total_messages` 10, summaries 5, llm-usage 5, cost
5 calls with `date_range.start` = org creation day, comparison
`changes == {}` with `period_days` 120, diagnosis runs 200 with 5 runs
(weekly buckets from the org's creation). An explicit 30-day range still
returns 2.

Playwright (`scratchpad/pw/monitoring_after.js`): Explore "All Time" shows
10 messages and 5 LLM calls with no trend badge, switching to "Last 30 days"
narrows it again; Diagnosis lists "All time" in the range menu, shows 5 runs,
survives a reload with `?range=all`, and seeds custom-range inputs from the
last 30 days; Cost "All Time" shows 5 calls.

## Tests

- `tests/e2e/test_console_metrics.py::test_no_date_range_means_the_organizations_whole_history`
  — runs at 1/45/200 days on a 400-day-old org: summaries, metrics and
  llm-usage count all three with no dates; comparison carries totals and an
  empty `changes`; an explicit 30-day window still narrows and still has a
  trend. Fails on the old code with `total_items 1 != 3`.
- `tests/e2e/test_diagnosis_explorer.py::test_no_range_means_all_time` — a
  2-year-old run on a 3-year-old org: no-range request returns it, buckets by
  month (≤120, `YYYY-MM`), facets work without a range. Fails on the old code
  with `400 start and end are required`.
- `backdate_organization` fixture (direct DB write, documented why: no API
  can move an org's creation time).
