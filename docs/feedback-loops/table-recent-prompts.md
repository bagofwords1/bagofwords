# Feedback loop — recent prompts for a focused table

Agent admins can inspect actual prompts that led to tool executions using a focused table. The details panel loads five at a time, shows date and success/failure, and offers Show more. It never loads history for every node in the graph.

## Attribution and access

`backend/app/services/table_prompt_service.py:14` joins the exact `TableUsageEvent.datasource_table_id` and `data_source_id` to the tool execution through `created_step_id`, then to its agent execution and completion's parent user prompt. Organization and report identities must agree. Deleted records and historical events without a table-row or tool-step link are excluded. There is no table-name search, connection-wide history lookup, or inference from a report's current agent roster.

`backend/app/routes/data_source.py:768` gates the endpoint with the existing resource-level `data_source/manage` permission (agent manager/admin, including organization governance). The requested table must belong to that agent and organization. Managing another agent grants no access. UI visibility uses the same selector management permission; direct endpoint access is independently protected.

Repeated tool calls in one execution produce one example. If any qualifying table call failed, that example shows Failed. Identical text from distinct executions remains distinct history. Only prompt text, execution ID, date, and outcome are returned—no tool results or query data.

## Reproduce → fix → verify

Before implementation, an authenticated request to `/api/data_sources/{agent}/tables/{table}/recent-prompts` returned HTTP 404; the details panel had no prompt section.

The persisted regression also fails when the exact `datasource_table_id` filter is removed: unrelated examples appear ahead of the expected seven examples. Restoring the filter passes. Tests seed identities and agents through API fixtures; execution history is inserted directly because no public history-creation API exists without invoking an LLM.

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db python -m pytest tests/e2e/rbac/test_table_recent_prompts.py -q
```

Covers pagination, stable newest-first order, duplicate tool calls, failed calls, identical table names, missing table attribution, foreign table IDs, invalid page size, regular members, managers of another agent, managers of this agent, and outsiders.

## Live sandbox

Reuse the full-stack/seed procedure in `tables-selector-erd-canvas.md`, then add synthetic history. Keep seed JSON and the database outside the repository. The history seeder is idempotent.

```bash
python tools/agent/seed_table_prompts.py --seed /private/tmp/bow-tables-erd-run/prompts-seed.json --app-db /private/tmp/bow-tables-erd-run/app.db
cd frontend
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3110 \
BOW_ERD_SEED=/private/tmp/bow-tables-erd-run/prompts-seed.json \
PLAYWRIGHT_CHROMIUM_EXECUTABLE='/Users/yochze/Library/Caches/ms-playwright/chromium-1193/chrome-mac/Chromium.app/Contents/MacOS/Chromium' \
node node_modules/@playwright/test/cli.js test --config playwright.tables-canvas.config.ts --reporter=line
```

The local verification backend uses port 8108. On backend restart, refresh the synthetic admin/reader login tokens in the seed JSON. The new browser scenario verifies lazy loading, five then seven examples, empty history, delayed-response isolation after changing focus, retry after a 503, and Hebrew/dark mode. The reader scenario checks both hidden UI and HTTP 403. Screenshot inspection also exposed a pre-existing RTL details-panel overlap; the Vue Flow LTR surface now reserves the correct side for the RTL dock.

Evidence under `media/pr/tables-erd/`: `before-recent-prompts.png`, `after-recent-prompts.png`, `after-recent-prompts-he.png`, and `recent-prompts.gif`. All data is synthetic.

The frontend request/reset guard lives in `frontend/components/datasources/TableRecentPrompts.vue:30`; details-panel mounting and RTL reservation live in `frontend/components/datasources/TablesCanvas.vue:62` and `:111`.

## Verification results — 2026-09-05

- Production Nuxt build: passed. Existing duplicate `message_type` and bundle-size/import warnings remain.
- Backend API regression: passed; removing the exact-table filter makes it fail with unrelated prompts.
- Browser coverage: all 12 existing selector scenarios passed in the full run; the new prompt scenario passed after correcting the test's delayed-network mock (which had inadvertently released its request early). The final scenario checks loading, stale-response suppression, empty states, pagination, retry, and RTL layout against the production bundle.
- Locale suite: 30 passed across ten locales. All seven new keys exist in every catalog; no increased catalog drift.
- Graph invariants, including 1,000 tables: passed.
- English and Hebrew/dark screenshots inspected; final flow recorded in `recent-prompts.gif`.

Historical usage without exact persisted attribution intentionally yields no examples. There is no backfill or guessed matching, and no migration is needed.
