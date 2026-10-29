# Feedback Loop — "a data-source manager can rewrite a shared connection via PUT /api/data_sources/{id}"

Aikido pentest 828732091 (high). `PUT /api/data_sources/{id}` is gated only by
`data_source:manage`, but `config`, `credentials` and `auth_policy` in the body
are forwarded to the agent's underlying Connection, which other agents may
share. The direct route `PUT /api/connections/{id}` requires
`connection:manage_connection`. Claim: a user with `manage` on an agent but no
connection rights can change a shared connection's auth policy or credentials.

## Root cause (validated)
- `backend/app/routes/data_source.py:187`: `@requires_resource_permission('data_source', 'manage')` only.
- `backend/app/services/data_source_service.py` `update_data_source`: pops
  `connection_fields = {'config','credentials','auth_policy'}` and calls
  `ConnectionService.update_connection(... connections[0] ...)` with no
  connection-level check.

## Loop A — deterministic reproduction (pytest, no network)
`backend/tests/e2e/rbac/test_data_source_update_connection_authority.py`
(agent linked to a SQLite connection and an MCP connection; `ds_manager` has
`manage` on the agent and `create_data_sources` on both connections, but no
`manage_connection`).

```bash
cd backend && TESTING=true uv run pytest tests/e2e/rbac/test_data_source_update_connection_authority.py -m e2e --db=sqlite -q
```
With the fix stashed: `4 failed, 5 passed`. All four `cannot_rewrite_connection` cases get 200 or 400 instead of 403.
With the fix: `9 passed`.

## Loop B — live stack (HTTP)
```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --invite dsmgr@example.com
uv run python tests/mocks/echo_mcp_http_server.py --port 3333 &   # real MCP server
uv run python ../docs/feedback-loops/scripts/ds_update_connection_authority.py
```
No LLM key is needed.

| dsmgr request | before fix | after fix |
|---|---|---|
| PUT `{config}` | 400 (reached connection validation, no authz) | **403** |
| PUT `{credentials}` | **200** (written) | **403** |
| PUT `{auth_policy: user_required}` | **200**, shared SQL connection flipped `system_only` to `user_required` | **403**, connection unchanged |
| PUT `{name, description}` | 200 | 200 |
| DELETE + POST link, SQLite connection | 200 / 200 | 200 / 200 |
| DELETE + POST link, MCP connection | 200 / 200 | 200 / 200 |
| admin PUT `{config}` | 200 | 200 |

## The fix
When any of `config`/`credentials`/`auth_policy` is present,
`update_data_source` now calls
`check_resource_permissions(..., "connection", [connections[0].id], "manage_connection")`.
That is the same resolver `@requires_resource_permission` uses, so full admins
and org-level `manage_connections` pass. Otherwise it raises a generic 403
before any write. Link/unlink routes and every other field are untouched.

## What this proves / regression notes
- The check lives in the service, so every caller of `update_data_source` is covered.
- Frontend: no current UI sends connection fields through `PUT /data_sources/{id}`.
  The `dataSourceId` edit branch in `ConnectForm.vue` has no caller: every usage
  passes `connection-id` and goes to `PUT /connections/{id}`. The other callers
  send only `is_public`, `icon`, `description`, `primary_instruction_id` or
  publish status.
- These existing suites all pass, with no pre-existing failures seen:
  `rbac/test_rbac_data_sources.py`, `rbac/test_rbac_connections.py`,
  `rbac/test_connection_management_identity.py`,
  `rbac/test_rbac_agent_manager_stories.py`, `test_data_source.py`,
  `test_connection.py`.
