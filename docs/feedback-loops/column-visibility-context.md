# Feedback Loop — per-agent column visibility (context curation)

Ask: in the tables selector, let an agent manager choose not only which tables
but which **columns** reach the agent's context. Not RLS/CLS — only what the
model is told about. Must also work for per-user auth (`user_required`)
connections.

## What shipped

| Layer | Change |
|---|---|
| Model + migration | `DataSourceTable.excluded_columns` (JSON deny-list, `dstexclcols01`). NULL/empty = all visible. Kept off `columns` because schema re-sync rewrites that list; a deny-list survives re-sync and lets newly discovered columns show up by default. |
| Filter helpers | `prompt_formatters.filter_columns / filter_fks / apply_column_exclusions` — case-insensitive. |
| Agent context | `SchemaContextBuilder` applies the filter on **both** branches: shared-credential (canonical) and per-user overlay. Overlay result = user's own grants ∩ agent's selection. PKs and FKs on a hidden column are dropped, including inbound FKs from other tables (`_prune_unresolvable_fks`). |
| Other agent-facing reads | `DataSource.get_schemas` (active-only reads), `read_user_data_source_schema(active_only=True)` (mentions/list_files), `DataSourceTable.to_prompt_table`. `describe_tables` no longer live-samples a table whose columns are empty *because* they were hidden. |
| API | `PUT /data_sources/{id}/update_tables_status` accepts `excluded_columns: {table_id: [names]}` (full replacement; `[]` = show all). Unknown names dropped, case/dupes normalised. Same `manage` permission as table activation. Listings return `excluded_columns`. |
| Cache | `ContextHub` caches the built schema per org for 5 min and **nothing ever called `invalidate_schema_cache`** — found live (see below). Both table-update endpoints now invalidate it. This also fixes the same staleness for plain table (de)activation. |
| UI | `TablesSelector.vue`: per-column checkbox in the expanded panel, Show all / Hide all, "N/M cols" badge on the row, struck-through + "hidden" pill, rides the existing draft/Save flow. Strings in en/es/he. |

## Sandbox

Booted per `.claude/skills/sandbox-feedback-loop`. Seeded via
`tools/agent/seed_org.py`; Anthropic provider + **`claude-haiku-5-5`** as the
only default model (`tools/agent/setup_haiku_llm.py` with
`HAIKU_MODEL_ID=claude-haiku-5-5`). Agent "Chinook Store" = sqlite
`backend/tests/config/chinook.sqlite`, all 11 tables active.

Wire capture: backend started with `ANTHROPIC_LOG=debug`, which logs every
`/v1/messages` request body; a small parser extracts the `<table name="Customer">`
block from each request.

## Loop

1. **UI (Playwright)** — Tables → expand Customer → uncheck Email, Phone, Fax,
   Address → Save. HTTP log: one `PUT update_tables_status` with
   `excluded_columns: {<Customer id>: ["Address","Email","Fax","Phone"]}` →
   `columns_updated_count: 1`. Reload: badge `9/13 cols`, checkboxes persisted.
2. **Real completion, hidden** — "What columns does the Customer table have?
   Then list the email and phone of the top 5 customers…". Haiku 5.5 answers
   *"The Customer table has 9 columns … no email or phone column"*.
3. **First control run exposed a bug** — after *Show all*, the planner requests
   still carried **9** columns while `describe_tables` (fresh build) carried 13.
   Root cause: `context_hub._SCHEMA_CACHE` (TTL 300 s) never invalidated.
   Fixed + unit test.
4. **Final cycle (after fix), wire evidence, all requests `claude-haiku-5-5`:**

   | Run | Customer columns in every request to the model |
   |---|---|
   | hidden | `CustomerId, FirstName, LastName, Company, City, State, Country, PostalCode, SupportRepId` (9) |
   | Show all → Save → next prompt | all 13 incl. `Address, Phone, Fax, Email` |

   With columns visible, the agent returns the emails/phones table — the
   setting is the only variable.
5. Employee keeps its own Email/Phone throughout — exclusions are per table.
6. RTL (`he`) panel checked.

## Tests

- `backend/tests/unit/test_column_visibility_context.py` — shared path
  (parametrised hidden sets incl. a PK that is an inbound FK target), per-user
  overlay intersection, `get_schemas` agent vs management reads, cache
  invalidation. Verified to fail with the builder/model changes stashed (5/6).
- `backend/tests/e2e/test_column_visibility.py` — API round-trip,
  normalisation, agent-facing `/schema` drops columns, survives schema refresh.
- `backend/tests/e2e/rbac/test_rbac_column_visibility.py` — member gets 403,
  nothing changes; admin succeeds.

## Not covered / limits

- **Not access control.** The agent can still `SELECT *` or guess a name; query
  results can contain hidden columns. The UI says so.
- Per-user auth was verified by unit test against the real builder (overlay
  branch, classifier patched to "delegated"); a live delegated connection
  (Power BI / Fabric OBO) needs real tenant credentials not available here.
- Semantic-model metadata blobs (`metadata_json` hierarchies for Analysis
  Services etc.) are not rewritten; they are table-level and rarely name
  columns, but a hidden column could still appear there.
- Column toggles live in the List view only, not the Visual (ERD) canvas.
- The agent-tree sidebar and `Name`/`Type` panel headers are unchanged
  (headers were already hard-coded English).
