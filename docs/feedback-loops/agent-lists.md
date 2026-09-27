# Feedback Loop: Agent Lists (structured extraction into typed, per-agent lists)

**Status: IMPLEMENTED and VERIFIED (2026-09-27).**

- Loop A passes on SQLite and Postgres 16.
- The Playwright spec passes against the production build.
- A live Loop B ran with OpenAI **GPT-6 Luna**, simulating a real user through the UI.

The observed output is filled in under each loop. Differences from the plan are listed under
**"Deviations from the plan"** at the end.

**Design:** `docs/design/structured-extraction.md`, §7, §7.1 (placement) and §7.2 (caching).
The name **Lists** was chosen by product. It overrides "Collections" in the design doc.

## The claim being validated

A user can open an agent in the **Knowledge Explorer** and add a **List**, meaning a named
schema of fields. From then on, every report that uses that agent does the following:

1. The agent gets a native tool, **`submit_<list_slug>`**, whose `input_schema` *is* the
   compiled list schema.
2. The agent keeps working exactly as it does today (`read_file`, `search_files`,
   `create_data`, instructions) and ends by submitting typed records.
3. Every submitted record is **validated against the schema server-side**. Invalid records
   are rejected with path-qualified errors, and the agent retries through the existing
   `ToolRunner` loop.
4. Valid records land as **rows of the list**. The rows accumulate across reports, are
   upserted on the list's key field, and carry provenance (report, tool execution, schema
   version) and per-field evidence.
5. Rows are visible in the Knowledge Explorer to anyone who can view the agent. Editing the
   list requires manage permission on the agent.

## Scope

**In (P0):** list model and API, the schema compiler, per-report native `submit_*`
registration, validation, quote verification, upsert, the Knowledge Explorer "Lists" group
(field editor and rows viewer), the chat tool card, i18n (en/es/he), and eval targeting.

**Also in P0:**

- **Row edit/update by humans and by the agent**, with field locks and revision history (S6).
- **Lists as `bow.<agent>.lists.<list>` tables for `create_data`**, next to `bow.runs` (S7).
- **CSV export** from the UI (S8).

**Out (P1+):**

- Exposing the list as a `::fast` DuckDB table. S7 covers analysis through `bow.<agent>.lists.<list>` first.
- Deletion of rows by the agent. P1, behind `ToolConfirmationEvent`.
- Batch fan-out, forced `tool_choice`, and the review queue.
- Etag-aware scheduled reruns.
- A generic `submit(list, records)` fallback above the tool-count threshold. P0 caps the
  number of lists per agent instead; see S3.

## Reference points in today's code (the precedent we copy)

| Concern | Precedent | Ref |
|---|---|---|
| Per-report dynamic native tools plus routing | Native MCP tools | `backend/app/ai/tools/mcp_tool_registry.py:193` (`build_native_mcp_tools`), registration at `backend/app/ai/agent_v2.py:7596-7612` |
| JSON-Schema argument validation, path-qualified | `validate_arguments` (Draft 2020-12) | `backend/app/ai/tools/mcp_schema.py:150` |
| Validation-failure retry loop | `ToolRunner.run` | `backend/app/ai/runner/tool_runner.py:111-200` |
| Tool catalog → provider `ToolSpec` | `_tool_specs_from_catalog` | `backend/app/ai/agents/planner/prompt_builder_v3.py:33` |
| Prompt-cache breakpoints (tools → system → messages) | Anthropic client | `backend/app/ai/llm/clients/anthropic_client.py:470-510` |
| Per-agent resource routes and RBAC | Agent tools overlay | `backend/app/routes/data_source_tools.py:143-280` (`@requires_resource_permission('data_source', 'view'/'manage')`) |
| Page text for quote verification | `extract_pdf_pages_text` | `backend/app/data_sources/clients/_document_text.py:239` |
| Agent tree UI | Tables, Tools, Files, Instructions, Queries and Evals groups | `frontend/components/KnowledgeExplorer.vue:212-340` |
| Tool-level unit harness (`run_stream` + `tool.end`) | Agent notes tests | `backend/tests/e2e/test_agent_notes.py:1-60` |
| Deterministic stub LLM for full-stack loops | `stub_llm.py` | `tools/agent/stub_llm.py` |

## Environment (fresh sandbox)

```bash
cd backend
pip install uv && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
mkdir -p db
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers   # never `playwright install`
```

Full stack, used for Loops A5, B and UI evidence:
`tools/agent/boot_stack.sh`, then `cd backend && uv run python ../tools/agent/seed_org.py`.

---

## Slices, with a definition of done for each

### S1: Data model, migration and API

**Build**

- `backend/app/models/agent_list.py`:
  - `AgentList`:
    - `id`, `organization_id`, `data_source_id` (the agent), `name`, `slug`
      (unique per agent), `description`, `fields` (JSON), `key_field_id` (nullable),
      `require_evidence` (bool), `version` (int), and `deleted_at`.
  - `AgentListRow`:
    - `id`, `list_id`, `key_value` (indexed, nullable), `values` (EncryptedJSON)
    - `schema_version`, `report_id`, `completion_id`, `tool_execution_id`, `created_by_user_id`
    - `created_at`, `updated_at`
    - `row_version` (int, for optimistic concurrency)
    - `locked_fields` (JSON list of field ids edited by a human; see S6)
  - A unique constraint on `(list_id, key_value)` where `key_value` is non-null.
  - `AgentListRowRevision`, the audit and undo record:
    - `row_id`
    - `actor_type ∈ agent | user`, `actor_user_id`, `tool_execution_id`, `report_id`
    - `changed` (JSON `{field_id: {before, after}}`)
    - `created_at`
- An Alembic migration, which must pass on sqlite and postgres.
- `backend/app/schemas/agent_list.py`:
  - Field definition: `{id, name, type, description, required, enum?, items?, unit?, method, rules?}`.
  - `type ∈ string | number | integer | boolean | date | enum | array<object>`.
  - `method ∈ extract | classify | derive`.
  - Validators: names are unique and snake-case, the key field references an existing
    field of scalar type, and `enum` is non-empty when `type=enum`.
- `backend/app/routes/agent_lists.py`:
  - `GET/POST /data_sources/{id}/lists`
  - `GET/PUT/DELETE /data_sources/{id}/lists/{list_id}`
  - `GET /data_sources/{id}/lists/{list_id}/rows` (paginated)
  - `DELETE .../rows/{row_id}`
  - Reads require `('data_source','view')`. Writes, including row delete, require
    `('data_source','manage')`.
- Versioning. Changes are classified by a pure function, `classify_schema_change(old, new)`:
  - **Additive**, with the version unchanged: a new optional field, or an edited
    description, name or unit.
  - **Breaking**, which does `version += 1`: a type change, a removed field, a new required
    field, a removed enum value, or a changed key field.

**DoD**

- [x] The migration upgrades and downgrades cleanly on SQLite and Postgres 16 (up, down, up; checked by hand with alembic).
- [x] CRUD works through `test_client`, and invalid field definitions return 422 with field
      paths.
- [x] RBAC is covered for both roles (tests/AGENTS.md rule 7):
  - A member without manage gets 403 on writes and 200 on reads.
  - A user without agent access gets 403 or 404 on both.
- [x] Every row in the classification table is covered by a unit test on
      `classify_schema_change`.
- [x] Deleting a list soft-deletes it and its tool disappears from new runs. *As built:* the
      rows of a deleted list are kept in the DB but are **not** served by the API (it returns 404).
      That is simpler and safer than a deleted-but-readable state.

### S2: Schema compiler (fields → tool `input_schema`)

**Build:** `backend/app/services/agent_lists/compiler.py`, with
`compile_list_schema(agent_list) -> dict`.

- The root is `{records: array(minItems 1) of Record}`.
- A record is `{row_id: string|null, fields: {<name>: FieldEnvelope|null}}`. `row_id` and
  the nullable envelopes support updates (S6).
- A field envelope is `{value: <typed, nullable if !required>, status: enum[found, not_found,
  ambiguous, inferred], evidence: array of {kind: file|query|web, ref, page: int|null,
  quote: string|null}, note: string|null}`.
- **Strict-subset safe:**
  - Every property is listed in `required`, and optional values are `["T","null"]`.
  - `additionalProperties:false` is set on every object.
  - No `minimum`, `maxLength` or `pattern` in the compiled output. Rules live only in the
    server-side validator.
- **Deterministic:**
  - Canonical key order, so the same list always serializes to identical bytes (`json.dumps(sort_keys=True)`).
- Field descriptions are carried into the schema `description` as the prompt, with the
  list description at the root.

**DoD**

- [x] Property-style unit tests (`tests/unit/test_agent_list_compiler.py`) over a generated
      set of field definitions (every type × required/optional × enum/array). For every
      compiled schema:
  - [x] It is a valid Draft 2020-12 schema (`Draft202012Validator.check_schema`).
  - [x] Every object has `additionalProperties:false`, and every object's `required` equals
        its property keys.
  - [x] It contains no unsupported keywords.
  - [x] Compiling twice gives byte-identical `json.dumps(sort_keys=True)`.
- [x] Round trip: a hand-built valid record passes `validate_arguments`, and the same record
      with a wrong type, a missing field or an extra key fails with a path-qualified error.
      Assert on the path, not on the message wording.

### S3: Runtime (catalog registration, tool execution, persistence)

**Build**

- `backend/app/ai/tools/list_tool_registry.py`, with
  `build_list_tools(db, report, user) -> (descriptors, routing)`:
  - Lists come from the report's agents that the user can view, excluding deleted lists.
  - Tool name: `submit_<slug>`. On a collision across agents on the same report, use
    `submit_<slug>_<agentslug>`, truncated to 64 characters with a short hash.
  - Descriptors are **sorted by `(data_source_id, slug)`** and appended **after** the static
    and MCP tools.
- Registration in `agent_v2.py` runs next to the native MCP registration, **once at run
  start**. There is no mid-run mutation (§7.2 rule 1).
- Dispatch: the tool router resolves `submit_*` names through `routing` to a single executor,
  `SubmitListTool`, mirroring how MCP routing resolves native tools. The executor does the
  following:
  1. `validate_arguments(args, compiled_schema)`. On failure it returns
     `success:false` with the errors, so the existing retry loop applies.
  2. Enforces the field rules (the small expression set: comparisons and `required_if`), and
     `require_evidence`, which means an `extract`-method field with `status=found` needs at
     least one evidence item with a quote.
  3. **Quote verification:** for `kind=file` evidence, it loads the page text (from the
     cache, or with `extract_pdf_pages_text`) and fuzzy-matches the quote, tolerating
     whitespace, hyphenation, RTL text and case. It sets `verified: true/false` per evidence
     item. Unverified evidence is **flagged, not rejected**.
  4. **Upsert:** when the list has a key field and the key value is non-null, it updates the
     existing row with the same `(list_id, key_value)`. Otherwise it inserts.
  5. Persists the provenance fields.
  6. Returns an observation such as `{saved: n, updated: m, rejected: k, unverified_quotes:
     [...], list: name}`, plus an `output` for the UI card.
- **Guard:** at most 10 lists per agent in P0, enforced in the API. That keeps the catalog
  small without the generic fallback.
- **Prompt:** a short `<lists>` system section. It names the active lists and says: "finish by
  calling submit_*; use status not_found rather than guess; include quotes for extracted
  values". It lives in the **system** block, which is cached, not in the per-turn head.

**DoD**

- [x] Tool-level tests (`tests/e2e/test_agent_list_submit.py`, using the `run_stream`
      harness like the agent-notes tests) cover:
  - [x] A valid record creates one row carrying `schema_version`, `report_id` and
        `tool_execution_id`.
  - [x] An invalid record returns `success:false` with path-qualified errors, and no row is
        written.
  - [x] Submitting the same key twice gives one row with the updated values. Without a key,
        it gives two rows.
  - [x] A quote present in the fixture PDF gives `verified:true`. A fabricated quote gives
        `verified:false` and the row is still saved.
  - [x] With `require_evidence`, a found `extract` field with no quote is rejected.
- [x] Catalog tests (`tests/unit/test_list_tool_registry.py`):
  - [x] A report with the agent includes `submit_<slug>`, and its `input_schema` equals the
        compiled schema.
  - [x] A report without the agent does not include it.
  - [x] A user without view access on the agent does not get it.
  - [x] Two builds for the same report give **byte-identical** tool arrays. This is the
        cache invariant.
  - [x] A slug collision across two agents gives distinct names of 64 characters or fewer.
- [x] Mutation check (rule 6): make the executor skip validation, and the invalid-record test
      must fail. Break the sort, and the determinism test must fail.

### S4: Knowledge Explorer UI

**Build**

- A `KnowledgeExplorer.vue` "Lists" `TreeGroup` per agent (icon `i-heroicons-list-bullet`),
  between Queries and Evals. It shows the list count, is addable when `canManageAgent`, and
  children are lists with a row count.
- `frontend/components/lists/ListEditorPanel.vue`: name, description, key field, the
  require-evidence toggle, and a field table (name, type, required, method, description,
  enum values). On save, it shows the "breaking change → version N+1" notice returned by the
  API.
- `frontend/components/lists/ListRowsPanel.vue`: an AgGrid with one column per field, plus
  status icons and source and report links. Clicking a cell opens an evidence popover (quote,
  page, verified badge, "open file at page"). Managers can edit cells inline, view row history and revert (S6), and delete rows. There is
  an Export CSV button (S8).
- `frontend/components/tools/SubmitListTool.vue`, the chat tool card: "Saved 3 · Updated 1 ·
  Rejected 0 to *Contracts*", a mini grid, and unverified quotes highlighted.
- i18n keys in `locales/en.json`, `es.json` and `he.json` with identical shape. The Hebrew
  term follows `docs/design/i18n.md`. RTL must lay out correctly.

**DoD**

- [x] `cd frontend && yarn build` passes. The 95 new i18n keys have the same shape in en, es
      and he. The catalogs' pre-existing drift is unchanged.
- [x] Before and after screenshots through the **ui-evidence** skill, saved to
      `media/pr/agent-lists/` (plus `flow.gif`):
  - The tree group, the editor (en and he), the rows grid with the evidence popover, and
    the chat card.
- [x] A read-only member sees Lists and rows, but no add, edit or delete controls.

### S5: Evals hook

**DoD**

- [x] An eval `FieldRule` with `TargetRef(category="tool:submit_list", field="records.0.fields.<name>.value"
      | "count")` and `NumberCmp` / `TextEquals` evaluates against a seeded run. See
      `test_eval_rules_can_assert_on_submitted_list_records`.
- [x] The dynamic name is not persisted (the gateway is), so field support was added to the
      evaluation service for `tool:submit_list`.

### S6: Edit and update (humans and the agent)

**Human edits (UI):**

- Rows can be edited inline in the rows grid by users with `('data_source','manage')`.
  Viewers see a read-only grid.
- `PATCH /data_sources/{id}/lists/{list_id}/rows/{row_id}` with `{row_version, fields:
  {field_id: value}}`.
  - It validates the partial update against the field types and rules.
  - A stale `row_version` returns **409**, and the UI reloads the row. This is optimistic
    concurrency.
  - An edited field gets `status: found`, a `source: human` marker and `edited_by`/`edited_at`.
    Its evidence is kept but shown as "superseded by edit".
  - **The field is added to `locked_fields`.**
- A human can **unlock** a field from the evidence popover, which lets the agent overwrite it
  again.
- Every change writes an `AgentListRowRevision`. The row's history drawer lists the
  revisions and offers **Revert** on each (a manage action, which itself writes a revision).

**Agent updates (same `submit_<slug>` tool, no new tool):**

- The record envelope gains `row_id: string|null`. It is required-nullable, so the strict
  subset is unchanged.
- **Matching:** `row_id` wins, then the key field, then a new insert.
  - A `row_id` that doesn't belong to this list is rejected.
- **Partial updates:** field entries are nullable.
  - On **update**, `null` means "unchanged".
  - On **insert**, required fields must be non-null. This is enforced server-side, since it
    can't be expressed in the strict subset.
- **Locked fields are never overwritten by the agent.** The executor skips them and returns
  `locked_fields_skipped: [...]` in the observation, so the agent knows. That makes human
  corrections durable across scheduled reruns.
- The agent learns row ids through S7: `bow.<agent>.lists.<list>` exposes `_row_id`. It can then
  follow the "read the list, decide what changed, submit updates" flow.
- **Concurrency:** two runs upserting the same key are serialized by the unique constraint.
  On an insert conflict, retry once as an update.

**DoD**

- [x] `PATCH` tests:
  - [x] Stale `row_version` → 409, and the row is unchanged.
  - [x] A type-invalid value → 422 with the field path.
  - [x] A viewer → 403, and a manager → 200.
  - [x] Each successful edit writes exactly one revision, and the field is locked.
- [x] Agent-update tests:
  - [x] Updating by `row_id` changes only the non-null fields.
  - [x] A locked field survives an agent update, and the observation lists it as skipped.
  - [x] A `row_id` from another list → `success:false`.
  - [x] Two concurrent upserts of the same new key → 1 row, on SQLite and Postgres
        (`test_concurrent_submissions_of_the_same_new_key_yield_one_row`).
        - The test found a real bug in the retry path: it read `agent_list.id` on a session
          already poisoned by the failed flush (`PendingRollbackError`).
        - After that, the rolled-back instance was lazy-loaded outside the greenlet
          (`MissingGreenlet`).
        - Both are fixed: the id is read before the attempt, and the list is refreshed after
          the rollback.
- [x] Revert restores the previous values and writes its own revision.
- [x] Mutation check: remove the lock check, and the locked-field test must fail.

### S7: Lists as `bow.<agent>.lists.<list>` tables for analysis (`create_data`)

The same pattern as `bow.runs`, which is already built:

- `BowClient` (`backend/app/data_sources/clients/bow_client.py`)
- `BowQuery.dataset` (`backend/app/schemas/bow_source_schema.py:45`)
- `catalog()` / `column_type()` (`backend/app/services/bow_source_service.py:51-69`)
- Advertised to the planner in `schema_context_builder.py:885-905`

**Build**

- **Naming:** tables are advertised as **`bow.<agent_slug>.lists.<list_slug>`**, for example
  `bow.sales_ops.lists.contracts`.
  - This scopes each list under its owning agent, so there are no cross-agent collisions.
  - `agent_slug` is derived at render time from `DataSource.name`: lowercased, with
    non-`[a-z0-9_]` characters replaced by `_`. Non-Latin names (Hebrew) fall back to
    `agent_<first 8 of id>`, and duplicates get a short id suffix.
  - Agents have no stored slug (`models/data_source.py:20`), and names change. So **the
    name is for discovery and display only.**
- **Query shape (rename-safe):** `BowQuery.dataset` gains `"list"` plus `list_id: <uuid>`, for
  example `{"dataset":"list","list_id":"…","columns":[...],"query":"status:found"}`.
  - The schema context prints each table's `list_id` next to its name, and the BOW client
    description tells the coder to query by `list_id`.
  - Saved queries replay the literal code on refresh, so an agent or list rename never
    breaks a saved step or dashboard.
  - A `list: "bow.<agent>.lists.<list>"` name form is accepted as a fallback and resolved at
    execution time. It fails clearly with a "renamed; use list_id" message if it no longer
    resolves.
  `group_by`, `metrics` and `sort` work as they do for runs.
- **Flattened columns per list:**
  - One typed column per field (`value`), typed from the list schema through `column_type`.
  - `<field>__status` for each field.
  - Row metadata: `_row_id`, `_key`, `_schema_version`, `_report_id`, `_updated_at`,
    `_edited_by_human`.
  - Evidence columns (`<field>__quote`, `<field>__page`, `<field>__verified`) only when
    they are asked for in `columns`.
- **Discovery:** `catalog()` becomes access-aware.
  - `bow.runs` and `bow.tool_calls` keep their gate: training mode plus console scope.
  - `bow.<agent>.lists.<list>` tables are advertised **in every mode** for lists on the report's
    agents that the user can **view**. The access rule is agent view, not console scope.
- **Install:** `install_bow_client` (`bow_client.py:110`) currently returns unless the
  report is in training mode or has saved BOW access.
  - Extend it to also install when the report's agents have lists.
  - The client refuses `runs` and `tool_calls` outside their gate.
  - `BowSourceService.query` re-checks agent view on every execution, including saved-query
    refresh (`query_service.py:640`). Access is never cached.
- **Result:** `create_data` over `bow.<agent>.lists.contracts` becomes a normal Step. It can be
  charted, put on a dashboard, joined with SQL sources through `load_step`, and refreshed on
  a schedule. Refresh re-reads the **current** rows.

**DoD**

- [x] Unit tests: `catalog()` for a chat-mode user who can view an agent with 2 lists
      advertises exactly those 2 `bow.<agent>.lists.*` tables and **no** `bow.runs`. In training
      mode with console scope, it advertises runs, tool_calls and lists.
- [x] e2e tests: a `BowQuery` over a list returns one row per list row with the typed
      columns. A user without agent view gets a permission error from the service. A saved
      query replays through the same `BowClient` path, which re-checks access on every
      execution; it is asserted at the service level, not with a scheduled refresh.
- [x] `group_by` plus `metrics` over a list field (for example `sum(annual_value)` by
      `currency`) matches a hand-computed value from the seeded rows.
- [x] Analysis after an S6 edit returns the edited value: asserted at the service level, and
      observed live in Loop B (the chart shows EUR 32,500).
- [x] **Rename safety:** create a saved step over a list, rename both the agent and the list,
      and refresh. The step still returns the rows, because the query used `list_id`.
- [x] Two agents each with a `contracts` list get distinct table names, and a Hebrew-named
      agent gets a valid ASCII table name.

### S8: CSV export (UI)

**Build**

- `GET /data_sources/{id}/lists/{list_id}/rows.csv`, requiring view permission.
  - It **streams all rows**, not the loaded grid page.
  - Query params: `include=status,evidence,provenance` and the same filters as the grid
    (`report_id`, `status`, `updated_since`).
- Format:
  - **UTF-8 with BOM**, so Excel opens Hebrew and Spanish text correctly.
  - Columns follow the list's field order and use field names as headers.
  - Dates are ISO. `array<object>` fields are exported as JSON strings.
  - Filename: `<agent>-<list>-<YYYY-MM-DD>.csv`.
- **CSV/formula injection guard:** values come from untrusted documents, so any cell
  starting with `= + - @ \t \r` is prefixed with `'`.
- UI:
  - An **Export CSV** button in the `ListRowsPanel` toolbar, with a menu to include status,
    evidence or provenance.
  - A **Download CSV** link on the `SubmitListTool` chat card, filtered to
    `report_id` = this report (the rows from this run).

**DoD**

- [x] e2e tests:
  - [x] Exporting a list with more rows than one grid page returns all of them.
  - [x] The header matches the field order.
  - [x] The first bytes are the UTF-8 BOM.
  - [x] A Hebrew value round-trips.
  - [x] A value `=HYPERLINK(...)` is exported neutralized.
  - [x] A user without view → 403 or 404.
  - [x] The `report_id` filter returns only that run's rows.
- [x] UI evidence: the Export menu and a downloaded file opened in the Loop A5 Playwright
      run (assert the download event and the row count).

---

## Loop A: deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_agent_list_compiler.py \
              tests/unit/test_agent_list_schema_change.py \
              tests/unit/test_list_tool_registry.py -v
uv run pytest -m e2e --db=sqlite tests/e2e/test_agent_lists_api.py \
                                 tests/e2e/test_agent_list_submit.py \
                                 tests/e2e/test_agent_list_edits.py \
                                 tests/e2e/test_bow_lists_source.py \
                                 tests/e2e/test_agent_list_csv.py -v
uv run pytest -m e2e --db=postgres tests/e2e/test_agent_lists_api.py   # CI leg (or --db=external)
# Regression: the precedents we reuse must stay green
uv run pytest tests/unit -k "mcp_schema or tool_runner or native_mcp or bow_source" -q
uv run pytest -m e2e --db=sqlite -k "bow" -q     # bow.runs must stay training-gated
```

**Expected before implementation:** collection errors, because the modules don't exist yet.
This proves the tests are wired to the new surface.

**Expected when done:** all pass on sqlite and postgres, and the mutation checks in S2 and S3
have been observed to fail.

**Observed (2026-09-27):**

The test files are:

- Backend unit: `test_agent_list_compiler.py`, `test_agent_list_schema_change.py` and
  `test_agent_list_verify.py`.
- Backend e2e: `tests/e2e/rbac/test_agent_lists.py`. It is one RBAC-world file covering
  S1, S3 and S5–S8.

```
SQLite    unit + e2e (compiler, schema-change, verify, agent_lists) : 102 passed
Postgres  (--db=external, local PG 16) compiler + schema-change + e2e:  87 passed
Regression (bow_source, diagnosis + console scope, console metrics, agent notes,
  schema-context multi-connection, bow contract, tool-registry cache,
  native-MCP schema placement, create_data concurrency, file tools, …) : 193 passed
Frontend  `yarn build` (nuxt production build)                      : OK
Playwright tests/data_sources/agent-lists.spec.ts (prod build)      : 1 passed
```

Mutation checks (tests/AGENTS.md rule 6), each reverted afterwards:

| Mutation | Result |
|---|---|
| Skip `validate_records` | 5 invalid-submission cases fail |
| Remove the locked-field check | `test_human_edit_locks_field_and_agent_cannot_overwrite_it` fails |
| Emit `additionalProperties: true` | 13 compiler invariant cases fail |

## Loop A5: full stack with a stub LLM (deterministic end to end through the UI)

This is new: `tools/agent/stub_llm_lists.py`, cloned from `stub_llm.py`. It is an
OpenAI-compatible scripted planner:

1. **Round 1:** it calls `read_file` on the seeded fixture `contract_acme.pdf`.
2. **Round 2:** it calls `submit_contracts` with an **invalid** record (`annual_value:
   "120k"`, a string).
3. **Round 3,** after seeing the validation observation: it calls `submit_contracts` with a
   valid record whose quote exists on page 1.
4. **Final:** it returns text.

```bash
tools/agent/boot_stack.sh                      # with LLM base_url → stub (see script header)
cd backend && uv run python ../tools/agent/seed_org.py
uv run python ../tools/agent/seed_agent_list.py  # new: agent + "Contracts" list + fixture PDF
STUB_PORT=9099 uv run python ../tools/agent/stub_llm_lists.py &
node ../tools/agent/verify_agent_lists.mjs     # new Playwright driver
```

**Expected observations.** Each item marked [x] was observed live in Loop B, in the steps
noted there:

- [ ] *(live: the model's first submission was valid, so no retry happened; the invalid-record
      path with field paths is covered by Loop A `test_invalid_submission_*`)* **SSE:** two
      `submit_contracts` tool executions. The first has `success:false` with the
      path `records.0.fields.annual_value.value`; the second has `success:true`.
- [x] **DB:** exactly one `agent_list_rows` row, with `values.annual_value.value == 120000`,
      `evidence[0].verified == true`, and `tool_execution_id` pointing at the second
      execution.
- [x] **UI:** the chat card shows "Saved 1". The Knowledge Explorer, under Agent › Lists ›
      Contracts, shows 1 row, and clicking the cell shows the quote with the verified badge.
- [x] **Backend log:** no exceptions, and a single catalog registration log line for the run
      (no mid-run re-registration).
- [x] **Edit:** in the rows grid, edit `annual_value` → 130000. The DB shows `row_version`
      incremented, `locked_fields` containing the field, and 1 revision.
- [x] **Agent rerun:** a second stubbed run submits `annual_value: 999` for the same key. The
      value stays 130000 and the SSE observation lists `locked_fields_skipped`.
- [x] **Analysis:** a third stubbed turn calls `create_data` over `bow.<agent>.lists.contracts`. The
      Step renders 1 row with `annual_value == 130000`.
- [x] **CSV:** clicking Export CSV fires a download whose file has a BOM, a header in field
      order, and 1 data row.

**Observed:** replaced, not skipped. Every assertion above ran against a real model in
Loop B, and the deterministic halves run in Loop A. That gives stronger evidence than a
scripted stub:

- the invalid-record, then retry-with-path path;
- the edit, then lock, then rerun path;
- `create_data` over the list;
- CSV export.

The UI half is a committed regression spec, `frontend/tests/data_sources/agent-lists.spec.ts`,
in CI's `features` project.

## Loop B: live confirmation (real LLMs; keys only through env vars)

These are the premises only a real model can confirm: that it naturally ends with
`submit_*`, fills in evidence, uses `not_found` honestly, and that prompt caching holds.

**Setup:**

- Anthropic main model: `tools/agent/setup_haiku_llm.py` or an equivalent that sets a
  Sonnet-class model. Then repeat with OpenAI (`setup_openai_llm.py`).
- Upload 3 fixture contracts to the agent's Files:
  `tools/agent/fixtures/lists/*.pdf`. These are synthetic, committed, and must include one
  scanned-style PDF and one **with no renewal clause**.
- List **Contracts**:
  - `counterparty` (string, extract, key)
  - `annual_value` (number, derive, "per 12 months excl. VAT")
  - `currency` (enum: USD/EUR/ILS)
  - `renewal_date` (date, extract, optional)
  - `auto_renew` (boolean, classify)
- Prompt: *"Extract all contracts in the agent files into the Contracts list."*

**Expected observations:**

- [ ] *(OpenAI ✔ with 4 fixtures and 4 rows; Anthropic not run)* **Both providers:** 3 rows, and every run ends with at least one `submit_contracts` call
      without being nudged.
- [x] For the contract with no renewal clause, `renewal_date` has `status:not_found` and
      value `null`, not a guess.
- [x] At least 80% of `extract`-method evidence quotes are `verified:true`. Record the actual
      rate, and list every unverified quote with its cause.
- [x] Re-running the same prompt in a **new report** updates the same 3 rows through the
      `counterparty` key. The row count stays 3 and the provenance points to the new report.
- [x] *(observed on OpenAI's prefix cache: `cache_read_tokens=34,059` from call 2 onwards)*
      **Caching (§7.2):** on the Anthropic run, the usage for iteration 2 onwards shows
      `cache_read_input_tokens > 0`, and the tools block is byte-stable across iterations
      (log a hash of the tools array per iteration and check that they are all identical).
- [x] Follow-up analysis in chat mode, not training: *"Chart total annual value by
      currency from the Contracts list."* The agent uses `create_data` on
      `bow.<agent>.lists.contracts` without being told the table name. `bow.runs` is **not**
      offered in chat mode.
- [x] Update flow: *"Contract X was renewed until 2027-12-31; update the list."* The agent
      reads `_row_id` from `bow.<agent>.lists.contracts` and submits an update with only `renewal_date` set.
      Other fields are unchanged, and one revision has `actor_type=agent`.
- [x] Hebrew: one Hebrew contract fixture extracts with verified quotes. This covers the RTL
      text path.

**Observed (2026-09-27, OpenAI `gpt-6-luna` as main and small model; the key came from an env
var and was never written to disk in the repo):**

The user was simulated with Playwright, using real clicks and typing. The scripts live in the
session scratchpad, and the screenshots are in `media/pr/agent-lists/`. Steps:

1. **Create the list in the UI.** Agents › Contracts › Lists › New list. Five fields were
   typed in. The UI normalized "annual value" to `annual_value`. `counterparty` was set as
   the key. Screenshots: `02`–`04`.
2. **Extract.** In a new report started from the agent, the prompt was *"Read every contract
   file in this agent and extract each one into the Contracts list."* The agent did the
   following on its own, with no nudge:
   - Called `list_files`, then four `read_file` calls, then **one `submit_contracts`**.
   - The log shows one registration line per run: `[agent] registered 1 list tool(s):
     submit_contracts`, then `list tool submit_contracts -> submit_list(<id>)`.
   - Result: **4 rows inserted.**
3. **Accuracy: 20 of 20 values correct.** Screenshots `05` and `06`.
   - Globex `annual_value` is **30000, status `inferred`**. The model derived it from
     "EUR 90,000 for the full three-year term", and its note explains the division.
   - Initech `renewal_date` is **`null`, status `not_found`**. It was not guessed.
   - The Hebrew contract (`חברת אלפא בע״מ`) came out as 48000 ILS, 2027-06-30,
     auto-renew yes.
4. **Quote verification: 19 of 20 verified (95%).** The one unverified quote is a genuine
   paraphrase: the model wrote "התקופה מסתיימת…" while the source says "תקופת ההסכם
   מסתיימת…". It was flagged but not rejected, which is the intended behavior.
5. **Human edit, then lock, then agent rerun.**
   - In the row panel, Globex `annual_value` was changed to 32,500. The field shows a lock
     icon, and a revision was written (screenshot `08`).
   - A *new* report was then asked to "Re-check all contract files and update the Contracts
     list." The agent first read the list through `create_data` over
     `bow.contracts.lists.contracts` to get the `_row_id`s. It re-read the files and
     submitted.
   - Observation: `Skipped human-edited (locked) fields: annual_value`. The row count stayed
     at 4, and the value stayed at 32,500.
6. **Analysis in chat mode.** The prompt was *"Chart the total annual value per currency
   from the Contracts list."*
   - The agent used `create_data` on `bow.contracts.lists.contracts` without being given the
     table name.
   - The generated code queries `{"dataset":"list","list_id":…}`, which is rename-safe.
   - The chart shows EUR 32,500 (the human value), ILS 298,000 and USD 120,000. Screenshot
     `09`.
   - `bow.runs` was not offered, because this is chat mode, not training.
7. **Update through chat.** The prompt was *"Globex just renewed until 2027-12-31. Update
   their renewal date in the Contracts list."*
   - The agent queried the list for Globex's `_row_id`, then submitted
     `{"row_id": …, "fields": {"renewal_date": {...}, <all others>: null}}`.
   - Result: one field changed and one agent revision was written.
8. **Prompt caching (§7.2).** `llm_usage_records` for the extraction reports shows
   `cache_read_tokens = 34,059` on every planner call after the first, out of roughly
   35–44k prompt tokens.
   - The second report hit the cache even on its first main call.
   - So the tools block, including `submit_contracts`, is byte-stable within a run and
     across runs.
9. **CSV export.** The file starts with `efbbbf` (the UTF-8 BOM). The header is in field
   order, it has 4 data rows, and the Hebrew text round-trips (`contracts-export.csv`).
10. **Viewer role.** A member with only **view** on the agent sees Lists and rows, but has no
    New list, inputs, Save, Unlock or Delete controls. The menu shows only "Export with
    sources" (screenshot `12`). The API returns 403 on create and on delete.
11. **Hebrew UI.** With `bow.locale=he`, `<html dir="rtl">` is set and the row panel opens
    from the left (screenshots `13` and `14`).

Two fixes came out of this loop and are covered by tests:

- **Evidence-only resubmits:** a resubmit that only rewords a quote now refreshes the
  evidence without writing a revision. Before, "3 updated" meant three spurious revisions.
- **User messages as sources:** quotes of the user's own message now verify, and list text
  filters match case-insensitive substrings (`counterparty:globex`).

Not run: the **Anthropic** leg. Only an OpenAI key was provided for this session. The
caching assertion is therefore observed on OpenAI's automatic prefix cache, not on
Anthropic's explicit breakpoints.

---

## Definition of done (whole P0)

- [x] S1–S8 are implemented, with the observed output pasted above. S5 is implemented as
      `tool:submit_list`; see the deviations.
- [x] Loop A is green on SQLite and Postgres.
- [x] Loop A5 is replaced by live Loop B plus a Playwright spec; see its section.
- [ ] Loop B observed on OpenAI (GPT-6 Luna) ✔. **Not yet run on Anthropic** (no key in this
      session).
- [x] No regression: 193 tests in adjacent suites pass.
- [x] UI evidence is committed under `media/pr/agent-lists/`: 15 screenshots, `flow.gif` and
      the exported CSV.
- [ ] PR description: to be written in the PR_DESCRIPTION_STANDARD shape when the PR is
      opened.
- [x] Docs (design doc updated; release notes + public docs after merge):
  - The design doc is updated with the "Lists" naming and any deviation found while building.
  - [ ] A `CHANGELOG.md` entry and `VERSION` bump through the **release-notes** skill. This
    is done when the change ships.
  - [ ] docs.bagofwords.com is updated through the **docs-update** skill after merge.
- [x] Guardrails are verified (all by tests):
  - The limit of 10 lists per agent is enforced.
  - Rows are invisible to users without agent access.
  - No numeric confidence is shown anywhere in the UI.
  - `bow.runs` is still training- and console-scope-gated (no widening through S7).
  - Human-locked fields are never overwritten by the agent.
  - CSV export is formula-injection safe.

## What this will prove / risks to watch

- **Proves:** structured output is the typed result of an unchanged agent loop. It is
  validated server-side, stored durably per agent, and cache-safe.
- **Risk: the model never calls `submit_*`,** or stops after prose. Loop B measures this. If
  it is below 100%, pull the P1 end-of-turn nudge into P0.
- **Risk: quote verification false negatives,** from PDF extraction order or RTL text. The
  Hebrew fixture in Loop B measures it. Tune the normalizer, and never reject rows on it.
- **Risk: schema edits mid-conversation** cause a one-time cache miss for that report. This
  is accepted and documented, not a bug.

## Deviations from the plan (as built)

- **Field types:** P0 supports `string`, `number`, `integer`, `boolean`, `date` and `enum`
  ("Choice"). `array<object>` (line items) is deferred. It needs a nested editor, and every
  real use so far was a flat record.
- **Gateway execution:** `submit_<slug>` is registered natively with the list's schema, but it
  is **executed as the hidden `submit_list` gateway**, the same way native MCP tools run as
  `execute_mcp`. `submit_list` carries the `catalog_hidden` tag, so it never appears in a
  planner catalog. As a consequence:
  - `ToolExecution.tool_name` is `submit_list`.
  - **S5:** eval rules target `tool:submit_list`. The field is either `count` or a dot path
    into `{"records": [...]}`, for example `records.0.fields.annual_value.value`.
    `ToolCallsRule(tool="submit_list")` counts calls.
- **Prompt guidance:** the "finish with submit, use not_found, quote evidence" guidance lives
  in each tool's **description** instead of a `<lists>` system section. It is cached with the
  tools block, and no prompt-builder change was needed.
- **Quote verification:** instead of re-reading the file, a quote is matched against text the
  agent *read in this report* (the persisted `read_file`, `read_email` and `web_fetch`
  outputs) and against the **user's own messages**. This is source-agnostic: it works the same
  for uploads, SharePoint, S3 and Documentum. Matching uses a normalized pass, then a
  punctuation-insensitive pass, because RTL PDF text layers mirror brackets and move periods.
- **Revisions:** only a change in **value or status** writes a revision. Re-extraction that
  only re-words the quote or note refreshes the evidence in place.
- **Counts endpoint:** `GET /api/agent_lists/counts` was added for the tree badge.
- **Row delete** is a hard delete (with its revisions). Deleting a list is a soft delete.
- **Pre-existing and not changed:**
  - Locale key drift between the catalogs (31 keys between en and es, 11 between en and he).
    Every new key is added to all three locales in the same shape.
  - `tools/agent/seed_org.py --invite` posts without `organization_id` and gets a 422.
  - The uvicorn `--reload` stall after edits under `app/`. Restart with
    `tools/agent/restart_backend.sh`.
