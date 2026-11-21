# Feedback Loop — save a cited result as a cached table

Feature: a connection admin looking at a result the agent produced can turn the
query behind it into a BOW custom table ("cached table"), from a button beside
**Save Query**. The existing custom-table dialog opens pre-filled, and the
first load shows a progress bar.

Driven end to end through the UI as a real user, against a live Power BI
semantic model, with Claude Haiku 5.5 as the agent's model.

## What was built

| Layer | Change |
|---|---|
| Result card (`frontend/components/tools/ToolWidgetPreview.vue`) | "Save as cached table" button in the bottom action bar, beside Save Query |
| Rules (`frontend/utils/cacheCandidate.ts`, `frontend/composables/useCacheCandidate.ts`) | When the button is offered; the suggested table name |
| Dialog (`frontend/components/datasources/CustomQueryModal.vue`) | First-load progress bar; a clear outcome when the first load fails |
| API (`backend/app/routes/connection.py`) | `GET /connections/{id}/custom-queries/from-result/{tool_execution_id}` returns the query behind a result; the list route adds `refresh_progress` |
| Service (`backend/app/services/custom_query_service.py`, `custom_table_runs.py`) | `single_query_of`, `definition_from_result`, `_reporting_progress`, `live_progress` |
| Loader (`backend/app/data_sources/fast/loader.py`, `extractor.py`) | `Limits.on_rows`: the running row total after every batch |
| Query capture (`backend/app/ai/code_execution/code_execution.py`) | Each query timing records `connection_id`, `data_source_id`, `client_key` |

## When the button appears

All of these must hold; otherwise it is hidden, not disabled:

- custom tables are switched on for the organization (AI settings);
- the result came from exactly one query, with no error;
- that query ran on the connection itself, not through a cached table
  (`…::fast` client);
- the connection can host custom tables (shared credentials, queryable type);
- the viewer has `manage_connection` on that connection;
- the card is not read-only (a shared or public view) and the viewer may see code.

The server checks again when the dialog asks for the query: connection admin
(route guard), able to open the report, able to view code, and the result is
one query against that connection.

## Sandbox setup

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
ANTHROPIC_API_KEY=... HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py
```

Then, through the API (setup only): create a `powerbi` agent on a workspace
whose models the service principal can read, with `auth_policy: system_only`,
and enable the model's tables. Everything after that was done in the browser.

The Power BI model is synthetic: a retail star schema with 3,000,000 generated
sales rows, 2,000 stores, a date table and about twenty measures. No real data.

Three things about the sandbox that cost time and are worth knowing:

- **Pin `BOW_ENCRYPTION_KEY` before the first boot.** `boot_stack.sh` and
  `restart_backend.sh` do not. A restart then invents a new key, and stored
  LLM and connection credentials stop decrypting (`InvalidToken` on connection
  test, `Failed to decrypt` in the log). Updating the connection in place also
  fails, so the database had to be reset and re-seeded.
- **Editing backend files while a load is running kills the load.** Uvicorn
  reloads on any `.py` change under `backend/`, including test files, and it
  can hang at "Waiting for background tasks to complete". Kill the process tree
  and start again.
- **A service principal cannot index a workspace that holds an RLS model.** One
  unreadable model makes discovery "incomplete" and the agent is refused. The
  fixture model lives in a workspace with no RLS models for that reason.

## The run, as a user

1. **Settings → AI Settings → Agent capabilities:** switched "Custom tables
   (beta)" on. `enable_custom_queries` read back `true`.
2. **Chat:** asked *"Show Total Sales and Units Sold by year-month and store
   type."* The agent answered from Power BI with a 336-row table.
3. **Result panel:** opened the result from its citation. "Save as cached
   table" is beside "Save Query".
4. **Dialog:** opened with the agent's DAX, the name
   `total_sales_and_units_sold_by_year_month_and_store_type`, the connection,
   and the model's semantic models listed.
5. **Widened the query** in the dialog to a reusable grain, ran the preview,
   and clicked "Create & cache".
6. **Progress bar** from zero to done (below).
7. **Follow-up question** naming the new table: answered in 8 seconds from the
   cached copy with SQL, through the `…::fast` client.

## Progress bar, observed

The bar's label and percentage were sampled from the DOM every 400 ms.

**Multi-page load, to completion** (quarter × store × channel, 131,308 rows, 172 s):

| Seconds | Label | Bar |
|---|---|---|
| 0.1 | Starting… | 0% |
| 1.5 | Counting rows at the source… | 0% |
| 4.4 | Caching 0 of 131,308 rows | 0% |
| 132.4 | Caching 35,522 of 131,308 rows | 27% |
| 144.3 | Caching 67,605 of 131,308 rows | 51% |
| 159.5 | Caching 104,877 of 131,308 rows | 79% |
| 169.3 | Caching 127,271 of 131,308 rows | 96% |
| 171.4 | Caching 131,136 of 131,308 rows | 99% |

The saved table holds 131,308 rows, the source's own count.

**Larger load** (day × store, 870,017 rows): tracked from 0% through fourteen
steps to 51% (449,048 rows) over 446 s, then failed on a connection reset
between this sandbox's proxy and Power BI. That is an environment fault, not a
product one, but it is how the failed-first-load gap below was found.

**Single-page load** (month × store, 76,584 rows, 34 s): the bar stays at
"Caching 0 of 76,584 rows" and then finishes. Progress is reported per batch,
and a result that fits one Power BI response is one batch.

**Small load** (336 rows, 5 s): Starting… → Counting rows at the source… →
Finishing.

The bar never shows 100%: it holds at 99% until the request itself returns,
because the file is still being sealed and published after the last row.

## Checks

| Check | Expected | Observed |
|---|---|---|
| Admin, live Power BI result | button shown | 1 button |
| Dialog prefill | agent's DAX and a valid name | both present |
| Result answered from the cached table | button hidden | 0 buttons; endpoint 422 |
| Failed attempts inside one answer | not offered | endpoint 422 |
| Member with access to the agent, same question | button hidden, Save Query still shown | 0 buttons; endpoint 403 |
| Hebrew (RTL) | translated, laid out right to left | "שמור כטבלה במטמון" |
| First load fails (row limit lowered between preview and save) | dialog explains and closes; the table is left with its error | toast "Table created, but its first load failed", dialog closed, row in `error` |
| Follow-up after saving | answered from the cached table | `…::fast` client, 35 rows, 1.1 s query |

## Found and fixed during the run

- **Dialog opened empty.** It fills its form when it goes from closed to open;
  loaded on demand and mounted already open, it never saw that transition. It
  is now mounted closed and opened once mounted.
- **The query text is not in a listed result.** The completions list omits
  `executed_queries`, so the button cannot read it in the browser. The dialog
  asks the server for it, which is also where access is checked.
- **A failed first load left the dialog inviting a second "Create".** The table
  row already exists at that point, so the retry could only fail on the name.
  The dialog now says the table was created, shows the error, and closes.

## Not covered

- Other connectors: only Power BI was driven end to end. The rules are
  connector-neutral and the unit tests use SQLite and a fake source.
- Results that ran more than one query: deliberately not offered.
- Progress inside a single batch: a one-response Power BI result jumps from 0
  to done.
- Postgres as the application database: the run and the tests used SQLite.

## Tests

```bash
cd backend && TESTING=true ENVIRONMENT=production \
  uv run pytest tests/unit/test_custom_table_from_result.py --db=sqlite
cd frontend && node --test tests/unit/cacheCandidate.mjs
```
