# Feedback Loop — "we see some refreshes of the ECharts when it first appears"

On `/reports/[id]`, a `create_data` chart card (`CreateDataTool` →
`ToolWidgetPreview` → `EChartsVisual`) blanked and replayed its entry animation
several times right after it appeared, and again after a report reload. The
claim being validated: the chart is being *destroyed and re-created* (not
updated) each time one of the card's inputs settles.

## Root cause (validated)

1. **Every rebuild remounted the chart.** `EChartsVisual.vue` bound
   `<VChart :key="chartKey">` and ended every `buildOptions()` with
   `chartKey.value++`, so any rebuild threw away the echarts instance and
   created a new one (new `_echarts_instance_`, entry animation replays). The
   options watcher is `deep: true`, which fires on any touched dependency even
   when nothing changed — a re-wrapped `view` (`ToolWidgetPreview`'s
   `normalizedView` returns a fresh `{ view, version: 'v2' }`), a fresh
   `filteredData` object, a reloaded transcript, a re-applied report theme.
2. **The card's inputs churn on first paint.**
   - The inline step is a 20-row preview marked `truncated`
     (`backend/app/serializers/completion_v2.py` `PREVIEW_ROWS`; the streamed
     `tool.finished` `result_json` goes through the same cap). The chart drew
     the first 20 rows, then redrew when the full set landed.
   - `loadCompletions` (`frontend/pages/reports/[id]/index.vue`) rebuilt each
     `tool_execution` without `created_visualizations`, so every reloaded card
     started from a synthetic `viz-<id>` and `hydrateVisualizationIfNeeded`
     refetched the query to swap in the real visualization. The live
     `tool.finished` handler sets `[{ id }]` (no `status`), which triggers the
     same refetch.
   - After `[DONE]` the transcript reload replaces every `tool_execution`
     object in place.

## Loop A — deterministic reproduction (stub LLM, no external services)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --sqlite-sources 1   # note sqlite_source_1's id
echo '[{"id":"<ds id>","name":"sqlite_source_1","table":"orders_1"}]' > /tmp/bow-agent/sources.json
STUB_SOURCES_FILE=/tmp/bow-agent/sources.json STUB_PHASES=create \
  STUB_VIZ_TYPE=bar_chart STUB_CODEGEN=rows \
  uv run python ../tools/agent/stub_llm.py &          # 50-row bar chart per create_data
# point the org's default model at the stub (provider "stub" → http://127.0.0.1:9099/v1),
# e.g. ensure_llm_provider(..., 'stub') from tools/agent/run_concurrency_probe.py
cd .. && ln -sfn frontend/node_modules node_modules   # lets tools/agent/*.mjs resolve playwright
DS_ID=<ds id> node tools/agent/verify_chart_remounts.mjs before /tmp/bow-agent/chartprobe
```

`verify_chart_remounts.mjs` submits a prompt, waits for the run and the
post-`[DONE]` reload, then reloads the report, counting distinct
`_echarts_instance_` ids per card (each = one mount). The stub issues three
`create_data` calls, so there are three cards.

Observed on the base code (FAIL):

```
[before-shared] LIVE  mounts: 14 per card: [4,5,5] | rows label: 50 rows | spinner: false
[before-shared] RELOAD mounts: 4 per card: [1,1,2] | rows label: 50 rows | spinner: false
[before-shared] FAIL: a chart card mounted 5 times
```

## The fix

- `frontend/components/dashboard/charts/EChartsVisual.vue` — `chartKey` is the
  chart **type**, so VChart remounts only when the type changes; every other
  rebuild hands the new option to the live instance (`setOption`). The
  watcher compares a serialized signature of everything `buildOptions()`
  reads and skips identical re-triggers.
- `frontend/components/tools/ToolWidgetPreview.vue` — while a `truncated`
  preview is being upgraded, the chart slot shows a same-height spinner
  instead of charting the first 20 rows (on fetch failure it falls back to the
  preview, once per step). If the host swaps a full step back to its preview
  after mount, the card upgrades it again.
- `frontend/pages/reports/[id]/index.vue` — `loadCompletions` keeps
  `created_visualizations`, so reloaded cards bind the real visualization
  without a refetch.

Re-run of Loop A with the fix (PASS):

```
[after-shared] LIVE  mounts: 3 per card: [1,1,1] | rows label: 50 rows | spinner: true
[after-shared] RELOAD mounts: 3 per card: [1,1,1] | rows label: 50 rows | spinner: true
[after-shared] PASS: every chart card mounted once
```

Updates still reach the chart: dispatching `visualization:updated` with
`horizontal: true` re-renders horizontally on the **same** instance (0
remounts); switching `bar_chart` → `line_chart` remounts once (type key).

## What this proves / regression notes

- Each chart card now mounts exactly once, both when it first appears during a
  run and on reload, and draws the full result set in one pass.
- `EChartsVisual` also backs dashboard/artifact charts and metric-card
  sparklines; those get the same in-place updates.
- Not covered: the page-level `/api/steps/{id}` hydration (IntersectionObserver)
  and the card's own `default_step` fetch still both run for a truncated card —
  a duplicate request, but no longer a visible redraw.
