# Feedback Loop — "save the folder listing to a file so the agent can work through 1-2k case folders"

Customer shape: a file connection (SharePoint Online / SharePoint Server incl.
2010 / network directory) with ~20,000 files in ~1-2k case folders; the agent
reviews each folder and saves one row per folder to an Agent List. Before this
change the planner saw at most 50 rows of a file listing (300 in folder mode),
so enumerating took many paged calls against a 100-step turn cap, the names
stayed in the conversation, and "continue" after an interruption depended on
the model comparing a folder list in its context against list keys by eye.

Claim validated here: `list_files(persist=true)` saves the complete matching
listing as a CSV in the report, the model queries it with `create_data`
(alone, or joined with the Agent List table to get "folders with no row yet"),
the observation stays small, and a "continue" turn rebuilds the queue from the
file — for service-account connections from the shared catalog, and for
per-user connections from **the caller's own** files only.

## Design (validated)

- `ListFilesInput.persist` (`backend/app/ai/tools/schemas/file_tools.py`);
  implementation in `backend/app/ai/tools/implementations/list_files.py`
  (`_persist_refusal`, `_load_user_catalog`, `_persist`) and the pure CSV
  builders in `_file_catalog.py` (`persisted_file_rows`,
  `persisted_folder_rows`, `rows_to_csv`, `PERSIST_MAX_ROWS = 100_000`).
- The full matching set is computed by the same `page_catalog` /
  `group_catalog_folders` the paged listing uses (so the file equals the union
  of all pages, in sort order). `limit` only sizes the preview (default 20, hard
  cap 50). `cursor`, `source='live'`, and no catalog are refused before
  anything is written. Over 100k rows: cut, `truncated=true`, said in the
  summary.
- Columns. Files: `id, path, folder, name, ext, size, modified_at,
  created_at, web_url, indexed_at, folder_1..N` (folder name per level,
  `folder_1` = top level, N ≤ 10). Folders: `path, name, parent, depth,
  folder_1..N, file_count, latest_modified_at, earliest_created_at,
  sample_files, indexed_at`.
- Persistence reuses execute_mcp's artifact path, factored into
  `save_text_artifact` (`execute_mcp.py`): file under `uploads/files`,
  `File(source_kind="artifact", user_id=<caller>)`, linked to the report, and
  registered for the same turn (`_register_same_turn`) so `create_data`
  resolves `source_file_ids` immediately and later turns find it in
  `report.files`.
- Reuse: the file carries `source_ref = list_files:<fingerprint>` (inputs +
  a hash of the catalog rows + caller). The same persist again — this turn or a
  later one — returns that `file_id` (`reused=true`, no preview repeated); any
  catalog change after a reindex produces a fresh file.

### Per-user connections (auth_policy = user_required)

- Source: only the caller's own overlay rows — `UserDataSourceTable` for
  (report's agents, caller, this connection), written by
  `DataSourceService._sync_user_overlay_for_connection`
  (`data_source_service.py:5109`) under the caller's own credentials. Never the
  shared `ConnectionTable` catalog (`_load_catalog` refuses it for any
  non-system_only connection, `list_files.py:217`), never a live crawl. A
  caller with no own index is refused ("your own files … are not indexed yet"),
  matching how `group_by='folder'` already refuses rather than crawling.
  A bounded live listing was rejected as a source: it cannot be complete (≤500
  rows / the connection cap), so it would defeat the work-queue purpose and, on
  SharePoint 2010, cost minutes.
- Visibility: a report file is readable by anyone who can view the
  conversation (`file_access_service.py:199`, rule 4) and `create_data` loads
  every report file regardless of owner (`_source_files.py:77`,
  `agent_v2.py:733`). That is the same audience that already sees a per-user
  listing's names on the tool execution's `result_json`
  (`serializers/completion_v2.py:320`, only code is redacted), so the CSV adds
  volume, not readers. Someone who cannot open the report can neither download
  (`GET /files/{id}/content` → 404) nor load it by id (tested).
- The file is owned by the caller (`File.user_id`) and the output says whose
  view it is: `listed_as = 'user:<id>'` or `'service_account'`.

## Loop A — deterministic (no LLM)

```bash
cd backend
uv sync --frozen --extra dev
unset REQUESTS_CA_BUNDLE CURL_CA_BUNDLE SSL_CERT_FILE
TESTING=true ENVIRONMENT=production uv run --extra dev pytest -q --db=sqlite -m e2e \
  tests/e2e/rbac/test_list_files_persist.py tests/e2e/rbac/test_list_files_persist_per_user.py
```

`test_list_files_persist.py` (network_dir share, 23 case folders, Hebrew
names, nested subfolders; seeded through the API and indexed through the real
`refresh_schema` route): file rows equal the paged catalog set under six filter
combinations (rows and order); folder rows equal the folder listing at depth 1
and 2; preview follows `limit` and is capped at 50; refusals (cursor, live,
unindexed) write no file; row cap truncates and flags; work queue against a
real Agent List (bow list query) is exactly the folders without a row, also
from a later turn; same-run and later-turn persist reuse the file until the
catalog changes; a member persists in their own report, an outsider can
neither download nor load the file.

`test_list_files_persist_per_user.py` (simulated SharePoint 2010 SOAP server
with per-identity permissions; shared index as the service account, then
per-user NTLM credentials and per-user refresh through the API): admin
(`DOMAIN\alice`) and member (`DOMAIN\bob`) each get exactly their own files and
none of the service-account-only folders the shared catalog holds; two users
get disjoint private rows; a signed-in user without an own index is refused
for files, folders and `source='catalog'`; an outsider cannot read the file.

Observed: **24 passed**. Without the change: **16/16** of the first file's
original tests failed (`persist` unknown → normal listing, no file). With the
per-user read swapped for the shared catalog (simulated leak): **5 of 6**
per-user tests fail. Existing suites re-run green: list_files/folder grouping/
catalog paging/file tools/MCP materialization/SharePoint/network_dir units
(721 passed, 1 skipped), and e2e network_dir, SharePoint 2010/on-prem, MCP,
file, Agent Lists, connection browse plus the two new files (187 passed).

## Loop B — live, GPT-6 Luna, real stack on :8100

Sandbox: backend on port 8100 (own SQLite DB, `BOW_PLANNER_DUMP_FILE` on),
org seeded with `tools/agent/seed_org.py`, OpenAI provider with `gpt-6-luna`
(key loaded from a local secrets file at runtime, never written anywhere).
"Case Files" agent = network_dir share with **64 case folders + Templates, 311
files**, nested `Correspondence/` and `Evidence/Scans/`, 16 Hebrew folder names
(`תיק 2024-000` …), indexed through `refresh_schema`. Agent Lists keyed on
`case_id`. Turns are driven through `POST /api/reports/{id}/completions
?background=true`; an "interruption" is `POST /api/completions/{id}/sigkill`
after the second `submit_list`, followed by a "continue" turn. The model was
never told about persist.

### Observation size (same catalog, measured through the tool)

| listing | calls | observation chars | ~tokens |
|---|---:|---:|---:|
| files, no persist (catalog, default limit 50, paged to the end) | 7 | 42,511 | 17,780 |
| folders, no persist (group_by=folder) | 1 | 4,437 | 2,180 |
| files, persist=true | 1 | 3,409 | 1,384 |
| folders, persist=true | 1 | 2,195 | 944 |
| files, persist=true, limit=500 (preview capped at 50) | 1 | 7,355 | 3,153 |

The persisted observation does not grow with the catalog; the paged listing
grows ~57 tokens/file — at the customer's 20k files that is ~400 calls (beyond
the 100-step cap) and ~1.1M tokens, against one ~1.4k-token call.

### Runs

| run | code | what happened | tool calls |
|---|---|---|---:|
| A1 "save one row per case folder, batches of 10" (killed after 2 batches) | first cut | model found persist unprompted, but passed `limit=500` → 52k chars of "preview" per call, and persisted the same listing **3×** | 16 |
| A2 "continue" | first cut | rebuilt the queue from the file with create_data, no re-listing; 64/64 rows, no duplicates (churn came from the model's own "CASE-only" filter, unrelated) | 30 |
| B1 same task, new list (killed after 2 batches) | + preview cap 50 | persist(folders) → create_data → persist(files) → create_data → submit ×2; one `source='live'` persist refused | 10 |
| B2 "continue" | + preview cap | re-persisted both listings (new copies), then 5 × (create_data anti-join → submit) → 64/64, no duplicates | 14 |
| C1 same task, new list (killed after 2 batches) | final | identical persist **reused** (`reused=true`, no copy) | 14 |
| C2 "continue" | final | worked from the saved files; one persist → reused; 64/64 rows, 0 duplicates (2 idempotent re-submits "unchanged") | 19 |
| D1 "which case folders have no row in list D yet? count + first 10" (7 rows pre-saved, one of them not a real folder) | final | persist(folders) → create_data(file + list table) → **58**, first 10 correct; then 4 extra verification calls | 7 |
| P1 per-user SharePoint 2010: "which of my matter folders have no row yet?" | final | persisted **22 folders / 55 files from "your own files"** (alice's 18 + 4 joint); the shared catalog holds 15 more board-only files — **0** in the CSV | 9 |

Ground truth after the runs (rows API vs. the share): lists A, B, C each have
64 rows, 64 distinct keys, 0 missing, 0 extra; `file_count` correct in all
rows. Some `latest_document` values were mistyped by the model when it copied
values into `submit_list` (e.g. `מכתב ללקוח.txt` for `מכתב לקוח.txt`) —
transcription, not the listing.

Description changes driven by the runs (model confusion → fix → re-run):
- `limit=500` used as "give me everything" → preview hard-capped at 50 and
  "limit only sizes the preview (default 20, max 50)" in both descriptions.
- Same listing persisted repeatedly, including on "continue" → reuse by
  fingerprint, and "the same call again returns the same file; reuse its
  file_id on later turns".
- `source='live'` + persist ("refresh the inventory") → "leave source='auto'
  (source='live' is refused)". Still seen once in C1; the refusal message sent
  it back to `auto` in the next call.
- Queue built from the file listing by aggregating `folder_1` → description
  names the folder persist as the work-queue source.

## Not verified / notes

- Postgres leg not run locally (CI runs it); nothing here is SQLite-specific.
- SharePoint Online (Graph) and S3 were not driven live; they share the
  catalog path exercised here.
- No UI change; the CSV appears as an agent artifact file in the report.
- `_observation_hint` in `_source_files.py` does not find list_files files
  (it never matched execute_mcp's either — it looks for a top-level `file_id`
  on wrapped observations); the summary names the columns instead.
