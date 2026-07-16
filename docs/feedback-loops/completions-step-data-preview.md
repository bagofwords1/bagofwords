# Feedback Loop — "investigate why this report is so slow"

A user reported that opening a report (`/reports/{id}`) was very slow. The report
page's own code even flagged it: `frontend/pages/reports/[id]/index.vue` had the
comment *"loadCompletions is slow (~30s)"*. This validates the root cause and the
fix: the authenticated completions endpoint embedded the **full result set of
every widget/step created in the last N completions**, so the payload scaled with
stored data and was re-shipped on every open, poll, and post-stream refresh.

## Root cause (validated)

`GET /api/reports/{id}/completions` → `completion_service.get_completions_v2` →
`serializers/completion_v2.py:serialize_block_v2_sync`. The serializer stripped
`widget_data` from `result_json` but then embedded the entire `Step.data` (the
result rows, a JSON column — `app/models/step.py:31`) **twice**: once as
`created_step.data` and once as the created widget's `last_step.data`.

- With 6 recent turns each holding a 1.8 MB result set, the response was
  **~19.5 MB** (90,000 rows, doubled across `created_step` + `last_step`).
- The list is fetched on every report open, on the 15 s scheduled-completions
  poll (`index.vue`), and after every stream `[DONE]` — so the megabytes were
  serialized in Python and shipped to the browser repeatedly.

Secondary contributors found while investigating (not fixed here): no index on
`completions.report_id` (the hot `WHERE report_id=? ORDER BY created_at DESC`
query) or `steps.widget_id`; `get_completions_v2` re-runs the full `get_report`
(≈8 COUNT/selectin queries) just for an access check.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/test_completions_v2_step_data_perf.py` seeds the block graph
directly (report → completion → agent_execution → completion_block →
tool_execution → step with `BOW_REPRO_ROWS` rows) and calls `get_completions_v2`
in-process, measuring the serialized payload.

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
BOW_REPRO_ROWS=15000 .venv/bin/python -m pytest \
  tests/e2e/test_completions_v2_step_data_perf.py -s
```

**FAIL on the pre-fix serializer** (`git stash push -- app/serializers/completion_v2.py`):

```
[completions-v2] actual payload: small=34.2kB large=19581.1kB  (rows leaked: small=60 large=90000)
>       assert ... "step rows are embedded in the completions payload again"
```

## The fix

1. **Serializer preview** — `serializers/completion_v2.py`: `_preview_step_data`
   embeds only the first `PREVIEW_ROWS` (20) rows plus a `truncated` /
   `total_rows` marker; `data_model` + `view` (small chart/column config) stay
   inline so the card lays out immediately. Small results (≤ 20 rows) ship whole.
2. **Client lazy hydration** — `frontend/pages/reports/[id]/index.vue`: an
   IntersectionObserver on the tool cards (`[data-step-id]`) fetches the complete
   set via `GET /api/steps/{id}` when a result is `truncated`, caches by
   `step_id`, and patches `created_step.data` reactively so every tool component
   (ToolWidgetPreview, DescribeEntityTool, WriteCsvTool, …) upgrades preview →
   full unchanged. Off-screen turns and small/live-streamed results never fetch.

**PASS after the fix** — payload is now bounded regardless of stored data:

```
[completions-v2] per-step dataset: small=1kB large=1.8MB  (6 recent turns)
[completions-v2] if full rows were embedded, payload would be ~10.7MB
[completions-v2] actual payload: small=34.2kB large=47.2kB  (max rows/step: small=10 large=20)
[completions-v2] /api/steps/{id} still serves 15000 rows on demand — lazy hydration path intact
1 passed
```

## Loop B — live UI confirmation (running stack + Playwright)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
# dismiss first-run onboarding for the seeded org, then seed a large-data report:
TESTING=true TEST_DATABASE_URL=sqlite:///db/agent.db BOW_DATABASE_URL=sqlite:///db/agent.db \
  uv run python scripts/seed_perf_report.py <org_id> <user_id> 12000   # prints report id
# copy the .mjs into frontend/ (ESM needs frontend/node_modules) and run:
cd ../frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node ../tools/agent/verify_step_preview.mjs <report_id>
```

Observed (`VERIFY PASS`):
- `GET /api/reports/{id}/completions` → **20,537 bytes** (was ~19.5 MB for the
  same data).
- **2 lazy `GET /api/steps/{id}`** requests fired as cards entered the viewport,
  each `200`.
- The card rendered the full result: table shows **"1 to 50 of 12,000"**
  (screenshot: `/tmp/bow-agent/report_after.png`).

## What this proves / regression notes

The completions list no longer scales with stored step data — a constant-size
preview payload with the full set lazy-loaded per visible widget. The regression
test asserts the general invariant (payload bounded, preview capped + flagged
truncated, small results untruncated, `/api/steps/{id}` still serves the full
set) rather than one magic dataset size. Unrelated pre-existing console errors
seen during Loop B (`/completions/estimate` 400, favicon/asset 404s) reproduce
without this change.
