# Feedback Loop — "Power BI loads the semantic models, but the dataset ID/GUID is never available to the agent when querying — it always asks the user to provide that GUID"

The Power BI (cloud) connector indexes semantic models correctly, but at query
time the agent cannot resolve which dataset GUID to execute DAX against, fails
with `dataset_id is required (pass table_name or dataset_id)`, and — since no
GUID exists anywhere in its context — asks the user to supply one. This loop
validates the root cause and proves the fix.

## Root cause (validated)

Power BI's `executeQueries` REST endpoint is addressed by dataset GUID
(`POST .../datasets/{dataset_id}/executeQueries`) — unlike SQL connectors,
where the query string itself names the table. Four compounding defects:

1. **GUIDs stripped from the agent's context.** Indexing stores
   `metadata_json.powerbi.{datasetId, workspaceId}` on every table
   (`powerbi_client.py`, `get_schemas`), and the schema builder loads it into
   `PromptTable.metadata_json` — but every renderer only had branches for
   `tableau` and `powerbi_report_server`, never `powerbi`
   (`tables_schema_section.py` `_render_table_xml` / `_render_topk_tables_full`,
   `prompt_formatters.py`). The GUIDs never reached the prompt.
2. **Query-time `table_name` resolution re-crawled the tenant.** Clients are
   constructed from connection config + credentials only
   (`data_source_service.py` `construct_clients`) with no DB access, so
   `execute_query(dax, table_name)` resolved the GUID via
   `get_schema()` → `get_schemas()` — a full live discovery (list workspaces,
   list datasets/reports per workspace, admin scan with polling sleeps,
   per-dataset `COLUMNSTATISTICS`) on **every query**, uncached, inside the
   60-second sandbox query timeout (`code_execution.py`,
   `DEFAULT_QUERY_TIMEOUT_SECONDS`).
3. **Failures swallowed into a GUID-demanding error.** The lookup was wrapped
   in `except Exception: pass`; any failure degraded to
   `ValueError("dataset_id is required (pass table_name or dataset_id)")` —
   the message that trains the model to ask the user for a GUID it cannot know.
4. **Generic coder guidance contradicted the convention.** `coder.py` showed
   single-arg `execute_query("SOME QUERY")` examples; the Power BI two-argument
   rule lived only in the client description blob.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_powerbi_context_rendering.py \
              tests/unit/test_powerbi_client.py -q
```

Observed on pre-fix code (`git stash push -- backend/app` to reproduce):

```
FAILED tests/unit/test_powerbi_context_rendering.py::test_full_render_includes_dataset_and_workspace_guids
FAILED tests/unit/test_powerbi_context_rendering.py::test_combined_render_includes_dataset_and_workspace_guids
FAILED tests/unit/test_powerbi_context_rendering.py::test_pbirs_render_includes_report_and_dataset_ids
FAILED tests/unit/test_powerbi_context_rendering.py::test_service_formatter_includes_guids
FAILED tests/unit/test_powerbi_context_rendering.py::test_table_formatter_includes_guids
FAILED tests/unit/test_powerbi_client.py::TestExecuteQueryResolution::* (8 tests,
       incl. test_unresolvable_table_error_names_table_and_known_tables — the
       pre-fix error is exactly "dataset_id is required")
FAILED tests/unit/test_powerbi_client.py::TestGetSchemasCache::* (2 tests)
```

## Loop B — live confirmation (real tenant, optional)

Credentials via env vars only (`PBI_TENANT_ID`, `PBI_CLIENT_ID`,
`PBI_CLIENT_SECRET` — a service principal with workspace access). Run the
harness from a scratch directory (it is intentionally not committed):
authenticate → `get_schemas()` → render context → `attach_table_metadata` →
`execute_query(dax, table_name)` with an HTTP-call counter.

Observed against a live tenant (2 workspaces, 3 datasets, 4 model tables):

- **Old path** (no attached metadata, per-query fallback):
  `execute_query` made **9 REST calls** (plus admin-scan traffic not routed
  through `_request`) and took **4.0s** — for ONE query, repeated per query
  since nothing was cached. On real tenants this scales with workspace/dataset
  count and routinely exceeded the 60s sandbox budget.
- **New path** (metadata attached, as `construct_clients` now does):
  **exactly 1 REST call** (`executeQueries`), 3 rows returned.
- Warm-cache fallback (2nd query, same instance): 1 call, 0.3s.
- Rendered context (`render("full")`, `render_combined()`, `ServiceFormatter`)
  now contains `<powerbi datasetId="..." workspaceId="..."/>` for every table.
- Unresolvable table → `Could not resolve Power BI dataset for table
  'Bogus/Nothing'. ... Pass the schema table name EXACTLY as shown in the
  schema context ... Known schema tables include: ...` (the old
  `dataset_id is required` message is gone).

## The fix

1. `app/ai/context/sections/tables_schema_section.py` — new
   `_render_powerbi_cloud_metadata_xml()` emits
   `<powerbi datasetId= workspaceId= datasetName= tableName=/>` in both
   `_render_table_xml` (full render) and `_render_topk_tables_full`
   (combined render); `powerbi_report_server` blocks now also emit
   `report_id`/`dataset_id`.
2. `app/ai/prompt_formatters.py` — `ServiceFormatter.format_table` and
   `TableFormatter.format_table` emit the same GUID metadata.
3. `app/data_sources/clients/powerbi_client.py` —
   `attach_table_metadata()` accepts the persisted catalog
   (name → `powerbi` metadata); `execute_query` resolves `table_name` from it
   first (zero API calls), falls back to live discovery **cached per instance**
   (`get_schemas(force_refresh=False)`), and raises an actionable error naming
   the table and known schema names instead of `dataset_id is required`.
   The client `system_prompt` documents the explicit-ID form and forbids
   asking the user for IDs.
4. `app/services/data_source_service.py` — `construct_clients` injects the
   persisted `DataSourceTable.metadata_json` into any client exposing
   `attach_table_metadata` (`_attach_stored_table_metadata`).
5. `app/ai/agents/coder/coder.py` — the three generic `execute_query`
   instruction blocks now state the Power BI two-argument / explicit-ID
   convention and forbid asking the user for IDs.
6. `app/data_sources/clients/powerbi_report_server_client.py` — same-family
   hardening: `get_schemas()` cached per instance (indexing calls with a
   progress callback still re-discover), `query_note` strings recommend
   `table_name=` first.

Re-run Loop A after the fix:

```
78 passed (test_powerbi_client.py, test_powerbi_context_rendering.py,
           test_powerbi_report_server_client.py)
```

## What this proves / regression notes

- The agent's context now carries every Power BI table's dataset/workspace
  GUID, so DAX execution needs no user-supplied identifiers — validated
  end-to-end against a live tenant (auth → discovery → render → 1-call query).
- Query-time `table_name` resolution is a dict lookup; the per-query tenant
  crawl (the timeout/throttling source) is gone, with a cached live fallback
  for tables indexed after the catalog snapshot.
- Adjacent suites (`test_schema_section_agent_status.py`,
  `test_attach_and_index.py`, `test_powerbi_report_server_client.py`,
  `test_file_tools.py`) pass unchanged — except
  `test_file_tools.py::TestResolveFileClientIdResolution::test_rejects_unrelated_id_with_helpful_error`,
  which fails identically with this change stashed (pre-existing, unrelated).
