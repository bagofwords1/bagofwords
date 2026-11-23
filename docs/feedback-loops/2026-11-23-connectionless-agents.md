# Feedback Loop — "Allow creating agents with no connections; deleting a connection must not delete agents"

Two asks, one invariant. Until now every agent (`DataSource`) had at least one
connection, enforced in three places, and deleting a connection deleted every
agent that had no other one — with its instructions, saved queries, lists and
evals. The asks: (1) an agent can be created with no connection (instructions,
files and a description only); (2) deleting a connection keeps its agents.

Prior attempt: [bagofwords1/bagofwords#327](https://github.com/bagofwords1/bagofwords/pull/327)
lifted only the create-time check; 1,166 commits behind `main` and it missed the
runtime crash below.

## Root cause (validated)

The ≥1-connection rule was enforced at:

| Where | What it did |
|---|---|
| `backend/app/schemas/data_source_schema.py` `validate_connection_ids_or_type` | 422 unless `connection_id(s)` or `type`+`config` |
| `backend/app/services/data_source_service.py` `remove_connection_from_domain` | 400 "Cannot remove the last connection from an agent" |
| `backend/app/services/connection_service.py` `delete_connection` | ran the full agent delete on every agent whose only connection it was |

…and the runtime assumed it. **Validated by Loop A** (create gate lifted, runtime
untouched — every chat that includes such an agent fails):

```
test_connectionless_agent.py:107: {"detail":"Data source has no associated connections"}   (pinned alone)
test_connectionless_agent.py:107: {"detail":"Data source has no associated connections"}   (pinned with a data agent)
test_connectionless_agent.py:107: {"detail":"Data source has no associated connections"}   (Auto, admin)
test_connectionless_agent.py:107: {"detail":"Data source has no associated connections"}   (Auto, member)
test_connectionless_agent.py:168: GET .../refresh_schema -> 500
```

- `DataSourceService.construct_clients` raised 400 for an agent with no
  connections; `prepare_run_agents` (`ai/tools/implementations/agent_focus_common.py`)
  re-raises any non-403, so the completion request failed. Auto includes every
  accessible agent, so one public connectionless agent would break Auto chats
  for the whole org.
- With that fixed, `AgentV2.__init__`'s client filter (`ai/agent_v2.py`, `_has_client`)
  dropped the agent whenever another agent had clients, and
  `TablesSchemaContext.render_combined` (`ai/context/sections/tables_schema_section.py`)
  dropped any section with no tables/tools/files — the planner never saw the
  agent's description.
- `refresh_data_source_schema` fell through to a legacy fetch that needs a
  connection (500); `test_data_source_connection` reported failure;
  `DataSourceMention` required `data_source_type`/`auth_policy` (500 on
  `/api/mentions/available`); `llm_sync` would invent a summary and overview
  instruction from an empty schema.

Found while verifying (not in the original audit):
- **Delete-connection with no impact warning.** Opened from an agent's
  "Manage connections", `ConnectionDeleteAction` receives the agent-embedded
  connection, which carries no `agent_count`, so the "This will impact N
  agents" block never rendered — the before shot shows only "Are you sure?"
  while the action deleted the agent.
- **Demo-dataset cleanup relied on the cascade.** `create_demo_dataset` deletes
  its connection when an install fails; that used to take the half-built demo
  agents with it.
- **Unlinking in a session that already holds the agent's tables** (the training
  tools' shared session): flushing the agent row after the bulk table delete
  re-cascaded into deleted rows (`InvalidRequestError: … has been deleted`).

## Loop A — deterministic reproduction (no external services)

LLM boundary stubbed (the shared `env`/`llm` planner fixtures); routes, services,
context building and the agent loop run real.

```bash
cd backend
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true \
  uv run pytest tests/e2e/test_connectionless_agent.py -v
```

On `main`: 12 of 14 fail (422 on create, "Cannot remove the last connection",
agent deleted with its connection). With only the create gate lifted: the 4 run
tests fail with the 400 above. With the fix: **15 passed** (one connector test
added later).

Also covered:
`tests/training/test_connection_agent_tools.py::test_create_agent_with_no_connection`
(chat `create_agent` tool), `tests/training/test_create_demo_dataset_tool.py::test_failed_install_leaves_no_connection_and_no_agents`
(fails without the cleanup fix: `['Footprint …', 'Sales …']` left behind).

## Loop B — live UI (sandbox stack, stub LLM)

```bash
tools/agent/boot_stack.sh --dev
(cd backend && uv run python ../tools/agent/seed_org.py --demo --sqlite-sources 1)
STUB_LOG=/tmp/stub.jsonl python3 tools/agent/connectionless_agent/stub_llm_agents_echo.py &   # :9099
(cd backend && uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1)
cd frontend
OUT=/tmp/connectionless-agent node ../tools/agent/connectionless_agent/login.cjs
PHASE=after OUT=/tmp/connectionless-agent node ../tools/agent/connectionless_agent/capture_controls.cjs
OUT=/tmp/connectionless-agent node ../tools/agent/connectionless_agent/flow.cjs
OUT=/tmp/connectionless-agent node ../tools/agent/connectionless_agent/legacy_page.cjs
```

Pin `BOW_ENCRYPTION_KEY` before booting if you will restart the backend: without
it each process invents a key, and a restart can no longer decrypt the stub
provider's credentials ("Failed to decrypt credentials for provider 'custom'").
DevBox: skip boot/seed (`bow-stack`), log in as the DevBox admin.

The stub's planner reply lists the `<agent>`/`<data_source>` entries it was
given, so the transcript shows what reached the model. Observed:

| Step | Result |
|---|---|
| New agent wizard, nothing selected | Save & Continue shown, steps are Connection → Set Context |
| Save | straight to Set Context; no "Generating overview instruction" |
| Agent page | "No data connections … Add connection"; tree has no Tables/Tools; "No connections" badge |
| Chat | `HR Policies: description: "Answers HR policy questions…"; no data connection` |
| Manage connections, one connection | unlink button shown |
| Delete that connection | warning names the agent; after delete the agent is kept, "No connections" |
| Legacy `/agents/new` | no forced Add Connection modal; routes to `/context` |
| `he` | badge, hint and link render RTL |

Evidence: `media/pr/ai-happy-knuth-casmkv/` (before/after PNGs, flow GIF).

## The fix

Backend
- `schemas/data_source_schema.py`: no connection is a valid mode; only a
  half-configured new connection (type xor config) is rejected.
- `services/data_source_service.py`
  - `create_data_source`: Mode 3 — no connection, no validation/indexing, no LLM onboarding.
  - `construct_clients`: `{}` for an agent with no connection (after the access checks).
  - `refresh_data_source_schema` → `[]`; `test_data_source_connection` → success with `no_connections`;
    `llm_sync` → skipped; `update_data_source` → 400 for connection settings instead of dropping them.
  - `remove_connection_from_domain`: the last connection may go (404 for a connection
    not linked); resets `is_active` with a plain UPDATE.
- `services/connection_service.py` `delete_connection`: unlinks every agent through
  `remove_connection_from_domain` (which also clears instruction references to the
  removed tables — the ORM cascade misses those), keeps agents, deletes only a
  **connector** (someone's mailbox / MCP preset, `data_source_kind`) whose only
  connection it was. Response: `agents_without_connections`, `deleted_connectors`.
- `ai/agent_v2.py`, `ai/tools/implementations/agent_focus_common.py`: keep
  connectionless agents in the run (and mid-run attach); drop one the user may not
  use (403) explicitly, since it has no client to be filtered by.
- `ai/context/…`: a connectionless agent renders with its description and a
  `<no_data>` note (standing context only).
- `schemas/mention_schema.py`: `data_source_type`/`auth_policy` optional.
- `ai/tools/schemas/create_agent.py`: `connection_ids` may be empty.
- `ai/tools/implementations/create_demo_dataset.py`: failed-install cleanup deletes its agents explicitly.

Frontend
- `NewAgentWizardModal.vue`, `pages/agents/new/index.vue`: connections optional,
  table step skipped, no LLM draft.
- `KnowledgeExplorer.vue`: "No connections" badge; no Tables/Tools groups; landing
  hint with "Add connection".
- `AgentConnectionsModal.vue`: last connection can be unlinked (own confirm text).
- `ConnectionDeleteAction.vue`: fetches the impact when the passed connection has
  no counts; says agents are kept. Wizard's delete modal says the same.
- `locales/{en,es,he}.json`: `connectionlessAgent.*`.

## What this proves / regression notes

- Regression sweep (sqlite): 1,661 passed in the data-source/connection/RBAC/agent
  suites. Pre-existing, reproduce with the change stashed: `rbac/test_connector_actions.py::test_an_action_past_the_native_budget_is_still_listed_for_search`
  and 6 errors in `rbac/test_list_files_persist_per_user.py` (sandbox proxy CA
  bundle: `session.verify` is the bundle path, not `True`); 5 failures in
  `tests/training/test_connection_agent_tools.py` (greenlet errors in `create_agent`
  selection paths; not run in CI).
- Updated to the new contract: `test_connection.py::test_connection_delete_keeps_linked_agents`,
  `rbac/test_delete_agent_linked_to_project.py::test_deleting_a_connection_keeps_its_only_agent_and_everything_on_it`,
  `test_agent_teardown_dependents.py::test_deleting_the_only_connection_of_a_project_default_agent`,
  `training/test_connection_agent_tools.py::test_connection_delete_keeps_owned_agent_and_its_drafts_suite`.
- Not run here: `--db=postgres` (no Docker). The delete path now reuses the unlink
  cleanup that `test_agent_teardown_dependents.py` already exercises on Postgres in CI.
- Out of scope, worth an issue: a conversation or published dashboard release
  whose only agent is *deleted* resolves to Auto (`resolve_run_agents`,
  `dashboard_runs.py` release fallback). Keeping agents on connection delete
  removes the most common trigger, not the behaviour.

## Review round (fd34032f → next commit)

Five review findings on the first push, each reproduced before the fix:

| # | Finding | Loop (fails on fd34032f → passes) |
|---|---|---|
| 1 | The last unlink matched tables only through `connection_table_id`; legacy rows and a delegated user's own discoveries (NULL link) and their overlays survived, and the schema then served them to everyone | `test_losing_the_last_connection_leaves_no_table_behind[unlink\|delete-connection]`: `assert (2, 1) == (0, 0)` → pass |
| 2 | The failed-install demo rollback deleted every agent linked to the new connection, including one linked there by someone else meanwhile | `test_failed_install_leaves_no_connection_and_no_agents`: `an agent the install did not create is never deleted` → pass. Rollback now uses the ids `installer.create_agents(created_ids=…)` records as each agent commits |
| 3 | A direct unlink could strand a `kind=connector` with no connection | `test_a_connector_cannot_lose_its_last_connection_by_unlinking`: 409; the UI hides the button |
| 4 | The confirmation listed a connector that the delete removes under "agents are kept" | `GET /connections/{id}` now returns `connectors_deleted_with_it` (the same `connectors_deleted_with()` the delete uses); the dialog lists kept agents and deleted connectors apart |
| 5 | Delete was enabled before the impact lookup returned, and stayed enabled if it failed | Live UI loop with a delayed and a failing `GET`: Delete disabled while loading and after a failure, Retry recovers (`media/pr/…/review_delete_{impact,loading,error}.png`) |

Regression after the round: 101 passed across the connection / data-source /
RBAC / teardown suites; training suites unchanged (the same 5 pre-existing
failures as `main`).

## Re-review round (d8258d44 → next commit)

| # | Finding | Loop (fails on d8258d44 → passes) |
|---|---|---|
| 1 | On an agent that keeps another connection, a user's own discoveries through the departing connection (NULL link, only `metadata_json.discovered_connection_id`) survived the unlink and were then served as canonical rows | `test_removing_one_connection_takes_the_rows_discovered_through_it[unlink\|delete-connection]`: `a user's discovery through the departed connection must not survive` → pass; the remaining connection's `Album` stays visible |
| 2 | The schema wizard's Tools tab had its own delete dialog (no impact, Delete enabled at once) | It now renders the shared `ConnectionDeleteAction` (`auto-confirm`); live: names the agent kept and the connector deleted (`media/pr/…/review_wizard_delete.png`) |
| 3 | A connector could be created with no connection, or a connectionless agent switched to `kind=connector` | `test_a_connector_cannot_exist_without_a_connection`: POST 422, PUT 400; an agent with a connection can still become one |
| 4 | Demo agent ids were recorded after `create_data_source` returned, but it commits the row earlier | `create_data_source(on_committed=…)` reports the id at the first commit; `test_rollback_covers_an_agent_whose_create_failed_after_commit`: `half-created agent left behind: ['Sales …']` → pass |

Regression after the round: 112 passed (connection / data-source / RBAC /
teardown / delegated-scope / sign-in suites); training suites unchanged apart
from the new test (the same 5 pre-existing failures as `main`).
