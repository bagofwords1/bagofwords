# Feedback Loop — Power BI connector, live end-to-end through the UI

Validates the full Power BI path against a real Azure tenant: seed an empty
tenant with data, connect via the product UI (service principal), index the
schema, run real LLM completions, and confirm `inspect_data` / `create_data` /
`create_artifact` execute real DAX and return correct numbers.

This is a **Loop B** (live credentials) validation by design — the premise
under test is the third-party integration itself. Secrets come from env vars
only: `PBI_TENANT_ID`, `PBI_CLIENT_ID`, `PBI_CLIENT_SECRET`, `ANTHROPIC_API_KEY`.

## Filling an empty tenant with data

The connector discovers workspaces/datasets via the REST API and queries via
`executeQueries` (DAX), so the tenant needs ≥1 workspace (SP as member/admin)
containing ≥1 semantic model with rows. Both are scriptable with the same
service principal — no portal clicks needed:

1. `POST /v1.0/myorg/groups?workspaceV2=true` — create a workspace (the SP
   becomes admin automatically), unless one already exists.
2. Create a **push dataset** (`POST /groups/{ws}/datasets` with
   `defaultMode: "Push"`, then `POST .../tables/{t}/rows`). Push datasets are
   fully queryable through `executeQueries` — verified: `EVALUATE ROW(...)`,
   `EVALUATE TOPN(...)`, and `SUMMARIZECOLUMNS(...)` all return data.
   (Importing a sample PBIX via the Imports API also works but needs a PBIX
   binary; the GitHub sample downloads are blocked in some sandboxes.)

The validation run seeded dataset `SalesPush` with `Sales` (300 rows, seeded
RNG so aggregates are reproducible) and `Customers` (40 rows).

## The loop

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py
# LLM: POST /api/llm/providers with the model INLINE in `models` (see finding 1)
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

UI flow (Playwright, `chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' })`):

1. Sign in → skip onboarding → `/agents/new`. With zero connections the
   **Add Connection** modal auto-opens (don't click "Create new connection").
2. Search "Power BI" → tile → fill `#tenant_id`, `#client_id`,
   `#client_secret` → **Test Connection** → expect green
   `Connected successfully. Found N tables.` → **Save and Continue**.
3. Indexing step runs schema discovery (6 tables across 4 datasets in ~5s in
   the validation run) → **Connect** → name the agent → **Save & Continue** →
   **Select all** tables → context step → agent created. The LLM sync
   generates the agent description/relationships/starters — first real
   completions.
4. New report → prompt for analysis (`.mention-input-field`, send button
   `button.w-7.h-7.rounded-full:not([disabled])`) → poll
   `/api/reports/{id}/completions` until the system completion is terminal.

## Observed result (2026-07-14)

- `test_connection`: success (probe DAX reached the engine).
- Indexing: `connection_indexings.status = completed`, 6 tables catalogued
  (`SalesPush/{Sales,Customers}`, `deals2/…Opportunities 1`,
  `leads/…Leads 2`, 2 SharePoint-sourced `mySM/…` tables).
- Run 1 (SalesPush revenue analysis): `create_note`, `inspect_data`,
  `create_data`×3 → **all three widget outputs matched the seeded ground
  truth to the dollar** (region / category / month aggregates). One
  `create_data` errored on malformed generated Python (missing `except`) and
  the agent retried the same widget successfully. It also self-created an
  instruction about bracket-notation column names.
- Run 2 (deals2+leads pipeline + dashboard): 4 `inspect_data` errors — all
  LLM table-name mistakes (invented `SalesModel/…` prefix from the system
  prompt's *example*, bare table name without dataset prefix, defensive
  empty-DataFrame returns). Every error message correctly listed the known
  schema tables and the agent converged: `create_data`×2 + `create_artifact`
  (dashboard) all success.
- Executed DAX (from `steps.code`): `SUMMARIZECOLUMNS` group-bys with
  `ORDER BY`, full-table `EVALUATE Sales`, `COUNTA`/`COUNTROWS`/`SUM`
  measures — all via `executeQueries` against both push and imported models.
- 44 LLM calls, ~359K input / ~19K output tokens (Claude Haiku 4.5).
- Backend log: no product errors (only sandbox thumbnail-browser noise).

## Findings (pre-existing, not fixed here)

1. **`POST /api/llm/models` is broken**: `app/routes/llm.py:130` calls
   `llm_service.create_model(...)`, which doesn't exist on `LLMService`
   (only `_create_models`). Standalone model creation 500s; the UI path works
   because it passes `models` inline to `POST /api/llm/providers`
   (`tools/agent/setup_haiku_llm.py` hits this bug).
2. **Column-name mangling on whole-table DAX**: `powerbi_client.py`
   `_execute_dax_internal` cleans headers with `col.strip("[]")`, which turns
   `Sales[Region]` into `Sales[Region` (leading table prefix keeps the inner
   bracket). Widgets built from `EVALUATE <Table>` show raw
   `Table[Column`-style headers; the LLM copes but the UI looks wrong. A
   regex like `re.sub(r"^.*\[(.+)\]$", r"\1", col)` would restore clean names.
3. **LLM provider delete is soft**: recreating a provider with the same name
   409s on the `llm_providers.organization_id+name` unique constraint even
   after `DELETE` returns 200.
