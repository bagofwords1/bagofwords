# Feedback Loop — "Create connections" role: creators manage only their own connections

Request: a role permission like "Create agent", but for connections. A member
who has it can create connections, manage the ones they created, and create
and manage agents on them. They must not see other people's connections. Full
admins and members with "Manage connections" see everything.

## Root cause (validated)

- Creating a connection required the org-wide admin permission:
  `POST /api/connections` was gated on `manage_connections`
  (`backend/app/routes/connection.py`, `create_connection`). There was no tier
  between "no connections" and "every connection".
- Ownership already existed. Creating a connection, either through
  `POST /api/connections` or inline from the agent form, calls
  `grant_connection_owner` (`backend/app/services/connection_service.py`).
  That writes a per-connection grant of `manage_connection` +
  `manage_data_sources` for the creator. Nothing new was needed to make a
  creator an owner. What was missing was the permission to create.
- `KnowledgeExplorer.vue` showed "New connection" to "Create agent" users,
  but `POST /connections` then returned 403. That was a dead button.
- Found during Loop B (missed by the API tests at first): the connection form
  pre-tests through `POST /api/data_sources/test_connection`, which required
  `create_data_source`. A connection creator got "Permission denied" on
  Connect.
- Found by the regression run: my first draft let per-connection managers see
  `credentials_meta`. That broke an existing contract
  (`test_rbac_connections.py::test_credential_metadata_is_admin_only`). Fixed:
  managers get the config, and credential identifiers stay with org admins.

## Loop A — deterministic API tests (no external services)

```bash
cd backend
export TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db
uv run pytest tests/e2e/rbac/test_connection_creator_role.py -m e2e --db=sqlite -q
```

The world: a full admin who owns a connection and a private agent on it, two
builders who hold only `create_connections`, one `manage_connections` admin,
and one plain member.

| Invariant | Test |
|---|---|
| Without the permission: 403 on create and both pre-save test routes. With it, or with `manage_connections`: 200 | `test_create_connections_permission_gates_connection_creation` |
| The creator's whoami carries the owner grant on their connection and nothing on others' | `test_creator_owns_connection_in_resolved_permissions` |
| The owner can GET (edit view), PUT, test and DELETE their own connection; others' return 403 | `test_owner_manages_own_connection_but_not_others` |
| Builders list only their own connections; admin and connection admin list all | `test_connection_list_visibility_by_role` |
| Connection admin and full admin still edit every connection | `test_connection_admin_still_manages_every_connection` |
| The owner can build agents on their own connection without "Create agent", but not on others' | `test_owner_builds_agents_only_on_own_connection` |
| The owner manages agents other people built on their connection; a peer builder can't | `test_owner_manages_agents_others_built_on_their_connection` |
| Tools: the owner refreshes and toggles their own; foreign tool ids through their own URL return 404 | `test_owner_manages_tools_on_own_connection_only` |
| A connection created inline from the agent form is owned by its creator | `test_inline_connection_from_agent_form_is_owned_by_its_creator` |
| The per-org and multi-org permission resolvers agree | `test_single_and_bulk_resolvers_agree_on_connection_ownership` |

**Before** (base `d76852d`): every test fails at setup with
`422 Unknown permission(s): create_connections`, because the role can't be
created.
**After:** `10 passed`.
**Mutation check:** I removed the tool-ownership filter in
`batch_update_tools`; `test_owner_manages_tools_on_own_connection_only` failed.

Regression sweep (sqlite, `-n 4`): `tests/e2e/rbac` plus the connection, MCP,
OAuth and custom-API suites gave 923 passed. The failures that remained
reproduce identically on the base commit (OAuth recovery, overlay sync,
`list_files_persist_per_user`), except `test_mcp_instruction_parity`, which
passes when run alone (5/5) and only timed out under load. The credential
metadata failure was mine and is fixed.

## Loop B — live stack, real UI (Playwright)

```bash
tools/agent/boot_stack.sh                       # production build, like CI
cd backend && uv run python ../tools/agent/connection_creator/seed.py
ln -s ../../../frontend/node_modules tools/agent/connection_creator/node_modules   # once
cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node ../tools/agent/connection_creator/flow.mjs ../media/pr/ai-zealous-bell-3adpz0
```

`seed.py` creates, through the real API:
- the full admin, who owns "Finance Warehouse" and a private agent "Finance Analyst" on it;
- a "Connection Builder" role holding `[create_connections]`, assigned to two builders;
- builder2, who owns "Marketing DB";
- a "Connection Admin" role holding `[manage_connections]`;
- a plain member.

`flow.mjs` logs in as each persona, asserts, and screenshots:

| # | Persona | What it proves |
|---|---|---|
| 01 | Admin | The role editor shows "Create connections" (checked on Connection Builder) |
| 02 | Builder | The agents page shows none of the admin's or builder2's connections |
| 03 | Builder | Add connection, then SQLite, then Connect: "Connection saved", schema discovered |
| 04 | Builder | "View all" lists only the builder's own connections |
| 05–06 | Builder | New agent on their own connection, without the "Create agent" permission |
| 07 | Connection admin | Sees Finance Warehouse, Marketing DB and the builder's connection |
| 08 | Full admin | Sees all of them |
| 09 | Plain member | No "Add connection" button |

Loop B first ran against an intermediate build and showed the
`/data_sources/test_connection` 403 ("Permission denied: this action needs
'create_data_source'"). The fix is a test in Loop A; the rerun is green.

Expected 403s in the browser log, unrelated to this change:
`GET /api/data_sources/demos` (demo agents need "Create agent") and
`GET /api/connections/{id}/custom-queries` (custom tables are a beta that is
off for this org; admins get the same 403).

## The fix

- `backend/app/core/permissions_registry.py`: new org permission `create_connections` ("Data & Connections").
- `backend/app/core/permission_resolver.py`: `manage_connections ⇒ create_connections` (mirrored in `frontend/composables/usePermissions.ts`).
- `backend/app/routes/connection.py`:
  - Create and test-params accept `create_connections`.
  - test-tool on a saved connection, refresh-tools, the tool updates and kerberos-access are gated on per-connection `manage_connection` instead of org-admin.
  - Tool updates are scoped to the connection in the URL.
  - Detail, read and OAuth checks honour connection managers; credential metadata stays admin-only.
- `backend/app/routes/data_source.py`: the pre-save `test_connection` accepts `create_connections`.
- Frontend:
  - The role-editor label in all 10 locales.
  - "New connection" buttons gated on `create_connections`.
  - The connection page's Edit button gated per connection.
  - The session refreshes after a create, so owner grants apply without a reload.

## What this proves / regression notes

- A connection creator can do everything on their own connections and nothing on others'.
- The org connection admin and full admin are unchanged.
- Not verified here: Postgres for the new tests (left to CI), and RTL layout of the new label (a single checkbox label, same component as its neighbours).
- Out of scope: a dedicated "created by" column. Ownership is the revocable grant `grant_connection_owner` already writes.
