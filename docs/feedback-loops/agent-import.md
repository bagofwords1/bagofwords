# Feedback Loop — "import an agent through a similar zip file"

"Export agent" downloads a zip (`agent.yaml` + `instructions/**.md` +
`evals/*.yaml` + `queries/*.yaml`). There was no way back: a user moving to a
new environment, with different (or the same) connections, could not recreate
the agent from that zip. This loop covers the new import path, end to end:
**Agents › New › Import agent** → upload → map connections → review → import →
review tables on the new agent.

## What was missing (validated)

- Nothing read the bundle. `agent.yaml` could be applied by hand through
  `POST /agents/apply` (`backend/app/services/agent_yaml_service.py:106`), but it
  names connections **by name only** (`ManifestResolver._resolve_connections`,
  `agent_yaml_service.py:1059`), so in another environment it failed with
  `connection_not_found`. Its active tables are FQNs on those names
  (`{connection}.{schema}.{table}`, `_table_fqn`), so they had to be rewritten too.
- `apply` **updates** an agent with the same name instead of creating a new one,
  and `TestSuiteService.import_yaml` **upserts by suite name**
  (`test_suite_service.py:332`, deleted suites included). Both would overwrite
  the target environment's data if reused naively.
- Instructions, folders, saved queries had no reader at all (the export comment
  said "export-only").
- Suites are found per agent by their **home agent** (`TestSuite.data_source_id`),
  which YAML import never sets, so an imported suite would not show under the
  agent (or in its next export).

## The change

- `backend/app/services/agent_bundle_import_service.py` (new) — parses the zip
  safely (size/file caps, path-escape filter, accepts a bundle re-zipped under one
  folder), then:
  - **preview** (read-only): bundle counts, each bundle connection with a
    suggested mapping (same name → case-insensitive → the only connection) and,
    for the mapped one, `tables_found` / `missing_tables` / `indexing_pending`,
    plus members that do not exist here.
  - **import** (always a NEW agent; 409 `agent_import.name_taken` otherwise):
    rewrites connections/tables/tools onto the mapped names, dry-runs
    `AgentYamlService.apply` and prunes unknown members/tools into warnings, applies
    it, then creates instructions in **one fresh build** (drafts by default,
    `publish_instructions` keeps exported status), recreates folders, re-resolves
    table references by name, imports suites under a unique name homed on the new
    agent, and saved queries.
- `backend/app/routes/agent_bundle_import.py` (new) — `POST /api/agents/import/preview`
  and `POST /api/agents/import` (multipart `file` + `options` JSON), gated like
  `POST /data_sources`: org `create_data_source` or per-connection
  `create_data_sources`, and the service requires `create_data_sources` on every
  mapped connection.
- `AgentYamlService.apply(..., create_overrides=)` — passes `kind`/`folder_id`
  into the create path only.
- Frontend: `ImportAgentWizardModal.vue` + `agents/AgentBundleSummary.vue`, wired
  into the `/agents` **New** menu (`KnowledgeExplorer.vue`). Strings in all 10
  catalogs (`agentImport.*`, `agentsPage.importAgent*`, `errors.agent_import.*`).

## Loop A — backend contract (self-contained)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_agent_bundle_import.py -m e2e --db=sqlite -q
# 5 passed
```

A second org stands in for the new environment (none of the source's
connections, agents or users exist there). The source bundle is produced by the
real export route over the bundled chinook SQLite file.

| Test | Invariant |
|---|---|
| `test_exported_agent_imports_into_another_environment` | differently-named connection is suggested; exactly the chosen tables are active; instructions land as drafts with their folder and table reference; suite is homed on the new agent; ids never become titles |
| `test_import_never_overwrites_existing_agents_or_suites` | same name → 409; preview offers a free name; second import gets its own suite, first suite untouched |
| `test_import_rejects_bad_bundles_and_unmapped_connections` | not-a-zip / no `agent.yaml` → `invalid_bundle`; all connections skipped → `no_connection` |
| `test_import_leaves_out_what_the_target_does_not_have` | hand-written bundle nested in a folder: missing table + unknown member reported, not fatal; `publish_instructions` keeps status |
| `test_import_requires_agent_building_permission` | plain member → 403 on preview and import, nothing created |

Mutation check (rule 6): skipping the table-name rewrite fails 3 tests; dropping
the suite-name dedupe fails the overwrite test; restoring the filename-as-title
fallback fails the round-trip test. Existing `test_agent_yaml_apply.py` and
`test_instruction_export.py` still pass (17 passed together).

## Loop B — the UI, as a user (running stack)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --sqlite-sources 1
# DevBox: skip both; the stack and admin already exist.
cd ../frontend && npx playwright test tests/data_sources/agent-import.spec.ts --project=features
```

The spec creates and exports an agent through the API, then drives
**New › Import agent**: upload the zip, see the bundle summary and the
auto-matched connection, accept the suggested free name (then rename),
import, open the agent, and checks the new agent and its draft instruction
through the API. Breaking the Import button makes it fail.

Manual run on the seeded stack (richer bundle: 4 instructions, 2 in a folder, one
table-scoped, an eval suite with 2 cases, a saved query), re-mapping the bundle's
`sqlite-1` onto a second connection `Warehouse`:

```
mapping rows: 1 | status: All 1 tables found · matched by name
after remap status: All 1 tables found
suggested name: sqlite_source_1 (imported)
result banner: “Sales (imported)” was imported | 1 active tables · 4 instructions · 1 eval suites · 1 saved queries
== Sales (imported): connections=['Warehouse'] active tables: ['orders_1']
   instructions: 4 × draft   folders: ['metrics'] placements: 2
   suites: ['Sales smoke (Sales (imported))']  queries: [('Revenue by region', 'draft')]
== sqlite_source_1 (source): unchanged — 4 × published, suite 'Sales smoke', query published
```

Hebrew (`he`) renders RTL with no leaked keys. Screenshots and a GIF of the flow
are in `media/pr/agent-import/`. `nuxt build` passes.

## Notes / known limits

- Folder names come back slugified (`Metrics` → `metrics`): the export writes
  slugs as directory names.
- A user who has only per-connection `create_data_sources` passes the route gate,
  but `AgentYamlService` checks org-level `create_data_source` for creates, so
  the import is refused for them (same as `POST /agents/apply` today).
- If a mapped connection is still indexing, the agent is created and
  `connection_indexing_pending` is reported; tables are picked in the wizard's
  Tables step once indexing finishes.
- The one 403 seen in the browser during the flow is
  `GET /connections/{id}/custom-queries` from the shared `TablesSelector`
  (custom tables are a disabled beta for the org); it appears on every agent's
  Tables tab and is unrelated to import.
- Sandbox gotcha: the backend's `--reload` hung on "Waiting for background
  tasks" twice after file edits; restart it (`pkill -f main.py`, boot again).
