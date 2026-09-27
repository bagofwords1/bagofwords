# Feedback Loop: Agent Lists (structured extraction into typed, per-agent lists)

**Status: PLAN.** Nothing is implemented yet. Each loop below is written *before* the code.
It says exactly what to run and what the observed output must be when the slice is done.
Each slice is filled in with the real observed output as it lands.

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

- [ ] The migration upgrades and downgrades cleanly on `--db=sqlite` and `--db=postgres`.
- [ ] CRUD works through `test_client`, and invalid field definitions return 422 with field
      paths.
- [ ] RBAC is covered for both roles (tests/AGENTS.md rule 7):
  - A member without manage gets 403 on writes and 200 on reads.
  - A user without agent access gets 403 or 404 on both.
- [ ] Every row in the classification table is covered by a unit test on
      `classify_schema_change`.
- [ ] Deleting a list soft-deletes it: its rows stay readable to admins through the API, and
      its tool disappears from new runs.

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

- [ ] Property-style unit tests (`tests/unit/test_agent_list_compiler.py`) over a generated
      set of field definitions (every type × required/optional × enum/array). For every
      compiled schema:
  - [ ] It is a valid Draft 2020-12 schema (`Draft202012Validator.check_schema`).
  - [ ] Every object has `additionalProperties:false`, and every object's `required` equals
        its property keys.
  - [ ] It contains no unsupported keywords.
  - [ ] Compiling twice gives byte-identical `json.dumps(sort_keys=True)`.
- [ ] Round trip: a hand-built valid record passes `validate_arguments`, and the same record
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

- [ ] Tool-level tests (`tests/e2e/test_agent_list_submit.py`, using the `run_stream`
      harness like the agent-notes tests) cover:
  - [ ] A valid record creates one row carrying `schema_version`, `report_id` and
        `tool_execution_id`.
  - [ ] An invalid record returns `success:false` with path-qualified errors, and no row is
        written.
  - [ ] Submitting the same key twice gives one row with the updated values. Without a key,
        it gives two rows.
  - [ ] A quote present in the fixture PDF gives `verified:true`. A fabricated quote gives
        `verified:false` and the row is still saved.
  - [ ] With `require_evidence`, a found `extract` field with no quote is rejected.
- [ ] Catalog tests (`tests/unit/test_list_tool_registry.py`):
  - [ ] A report with the agent includes `submit_<slug>`, and its `input_schema` equals the
        compiled schema.
  - [ ] A report without the agent does not include it.
  - [ ] A user without view access on the agent does not get it.
  - [ ] Two builds for the same report give **byte-identical** tool arrays. This is the
        cache invariant.
  - [ ] A slug collision across two agents gives distinct names of 64 characters or fewer.
- [ ] Mutation check (rule 6): make the executor skip validation, and the invalid-record test
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

- [ ] `cd frontend && yarn build` passes, and the lint and i18n catalog sync check passes.
- [ ] Before and after screenshots through the **ui-evidence** skill, saved to
      `media/pr/agent-lists/`:
  - The tree group, the editor (en and he), the rows grid with the evidence popover, and
    the chat card.
- [ ] A read-only member sees Lists and rows, but no add, edit or delete controls.

### S5: Evals hook

**DoD**

- [ ] An eval `FieldRule` with `TargetRef(category="tool:submit_<slug>", field="records.0.fields.<name>.value")`
      combined with `NumberCmp` or `TextEquals` evaluates against a stubbed run. This is an
      e2e test on the existing test-run service.
- [ ] If the dynamic tool name is not reachable by `TargetRef`, fix that in this slice. Do not
      work around it.

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

- [ ] `PATCH` tests:
  - [ ] Stale `row_version` → 409, and the row is unchanged.
  - [ ] A type-invalid value → 422 with the field path.
  - [ ] A viewer → 403, and a manager → 200.
  - [ ] Each successful edit writes exactly one revision, and the field is locked.
- [ ] Agent-update tests:
  - [ ] Updating by `row_id` changes only the non-null fields.
  - [ ] A locked field survives an agent update, and the observation lists it as skipped.
  - [ ] A `row_id` from another list → `success:false`.
  - [ ] Two concurrent upserts of the same new key → 1 row and 2 revisions (on sqlite and
        postgres).
- [ ] Revert restores the previous values and writes its own revision.
- [ ] Mutation check: remove the lock check, and the locked-field test must fail.

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

- [ ] Unit tests: `catalog()` for a chat-mode user who can view an agent with 2 lists
      advertises exactly those 2 `bow.<agent>.lists.*` tables and **no** `bow.runs`. In training
      mode with console scope, it advertises runs, tool_calls and lists.
- [ ] e2e tests: a `BowQuery` over a list returns one row per list row with the typed
      columns. A user without agent view gets a permission error from the service, and
      that holds even when the code is replayed through a saved query.
- [ ] `group_by` plus `metrics` over a list field (for example `sum(annual_value)` by
      `currency`) matches a hand-computed value from the seeded rows.
- [ ] A saved-query refresh after an S6 edit returns the edited value.
- [ ] **Rename safety:** create a saved step over a list, rename both the agent and the list,
      and refresh. The step still returns the rows, because the query used `list_id`.
- [ ] Two agents each with a `contracts` list get distinct table names, and a Hebrew-named
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

- [ ] e2e tests:
  - [ ] Exporting a list with more rows than one grid page returns all of them.
  - [ ] The header matches the field order.
  - [ ] The first bytes are the UTF-8 BOM.
  - [ ] A Hebrew value round-trips.
  - [ ] A value `=HYPERLINK(...)` is exported neutralized.
  - [ ] A user without view → 403 or 404.
  - [ ] The `report_id` filter returns only that run's rows.
- [ ] UI evidence: the Export menu and a downloaded file opened in the Loop A5 Playwright
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

**Observed:** _fill in per slice._

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

**Expected observations:**

- [ ] **SSE:** two `submit_contracts` tool executions. The first has `success:false` with the
      path `records.0.fields.annual_value.value`; the second has `success:true`.
- [ ] **DB:** exactly one `agent_list_rows` row, with `values.annual_value.value == 120000`,
      `evidence[0].verified == true`, and `tool_execution_id` pointing at the second
      execution.
- [ ] **UI:** the chat card shows "Saved 1". The Knowledge Explorer, under Agent › Lists ›
      Contracts, shows 1 row, and clicking the cell shows the quote with the verified badge.
- [ ] **Backend log:** no exceptions, and a single catalog registration log line for the run
      (no mid-run re-registration).
- [ ] **Edit:** in the rows grid, edit `annual_value` → 130000. The DB shows `row_version`
      incremented, `locked_fields` containing the field, and 1 revision.
- [ ] **Agent rerun:** a second stubbed run submits `annual_value: 999` for the same key. The
      value stays 130000 and the SSE observation lists `locked_fields_skipped`.
- [ ] **Analysis:** a third stubbed turn calls `create_data` over `bow.<agent>.lists.contracts`. The
      Step renders 1 row with `annual_value == 130000`.
- [ ] **CSV:** clicking Export CSV fires a download whose file has a BOM, a header in field
      order, and 1 data row.

**Observed:** _fill in._

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

- [ ] **Both providers:** 3 rows, and every run ends with at least one `submit_contracts` call
      without being nudged.
- [ ] For the contract with no renewal clause, `renewal_date` has `status:not_found` and
      value `null`, not a guess.
- [ ] At least 80% of `extract`-method evidence quotes are `verified:true`. Record the actual
      rate, and list every unverified quote with its cause.
- [ ] Re-running the same prompt in a **new report** updates the same 3 rows through the
      `counterparty` key. The row count stays 3 and the provenance points to the new report.
- [ ] **Caching (§7.2):** on the Anthropic run, the usage for iteration 2 onwards shows
      `cache_read_input_tokens > 0`, and the tools block is byte-stable across iterations
      (log a hash of the tools array per iteration and check that they are all identical).
- [ ] Follow-up analysis in chat mode, not training: *"Chart total annual value by
      currency from the Contracts list."* The agent uses `create_data` on
      `bow.<agent>.lists.contracts` without being told the table name. `bow.runs` is **not**
      offered in chat mode.
- [ ] Update flow: *"Contract X was renewed until 2027-12-31; update the list."* The agent
      reads `_row_id` from `bow.<agent>.lists.contracts` and submits an update with only `renewal_date` set.
      Other fields are unchanged, and one revision has `actor_type=agent`.
- [ ] Hebrew: one Hebrew contract fixture extracts with verified quotes. This covers the RTL
      text path.

**Observed:** _fill in, with the date and models used._

---

## Definition of done (whole P0)

- [ ] S1–S8 DoD boxes are all checked, with the observed output pasted above.
- [ ] Loop A is green on sqlite and postgres. Loop A5 is green. Loop B is observed on
      Anthropic **and** OpenAI.
- [ ] No regression in the existing suites that touch the agent loop (tool runner, native MCP,
      planner v3). If a suite fails, stash the change and re-run it before calling the
      failure unrelated.
- [ ] UI evidence is committed under `media/pr/agent-lists/`, and the PR description follows
      `.claude/templates/PR_DESCRIPTION_STANDARD.md`.
- [ ] Docs:
  - The design doc is updated with the "Lists" naming and any deviation found while building.
  - A `CHANGELOG.md` entry and `VERSION` bump through the **release-notes** skill.
  - docs.bagofwords.com is updated through the **docs-update** skill after merge.
- [ ] Guardrails are verified:
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
