# Feedback Loop: "create/edit_scheduled_task skip update_reports" and "my-credentials PATCH and /test skip allowed_user_auth_modes"

Aikido pentest findings, work packages WP4 and WP6:

- **828754957 / 828808036 (medium).** The agent tools `create_scheduled_task` and `edit_scheduled_task` call `ScheduledPromptService` directly. They have no `update_reports` check and no ownership check. The REST routes for the same actions require `@requires_permission('update_reports', model=Report, owner_only=True)`.
- **828857171 / 829374392 (medium).** `PATCH /api/data_sources/{id}/my-credentials` and `POST .../my-credentials/test` check only the registry's `user` scope. They never check the connection's `allowed_user_auth_modes`, which `POST /my-credentials` does enforce.

## Root cause (validated)
- `backend/app/ai/tools/implementations/create_scheduled_task.py` and `edit_scheduled_task.py` declare `required_permissions=[]`, and `ScheduledPromptService.create_scheduled_prompt` / `update_scheduled_prompt` (`backend/app/services/scheduled_prompt_service.py`) do no authorization. Only the REST decorator guarded these actions.
- `backend/app/services/user_data_source_credentials_service.py`: `upsert_my_credentials` checks `allowed_user_auth_modes`, but `patch_my_credentials` (auth_mode-change branch) and `test_my_credentials` do not. In `test_my_credentials` the client is built and `atest_connection()` runs before any policy check.

## Loop A: deterministic reproduction (pytest, no network)
- `backend/tests/e2e/rbac/test_scheduled_task_tool_authority.py`. Tools are invoked exactly as the planner calls them, with a real DB session, real User and Report rows, and a real org. Principals:
  - a service account whose role had `update_reports` revoked after it created a task (`update_reports` is a baseline permission for humans, so a human member always holds it);
  - a human member acting on someone else's report;
  - owners as positive controls.
- `backend/tests/e2e/rbac/test_user_credential_auth_mode_policy.py`. A Snowflake connection set to `user_required` that allows only `userpass` (the registry also offers `keypair` and `oauth` to users). The outbound client is replaced by a recorder, so the test proves that no connection attempt happens for a disallowed mode.

```bash
cd backend && ENVIRONMENT=production TESTING=true .venv/bin/python -m pytest -q --db=sqlite --disable-warnings \
  tests/e2e/rbac/test_scheduled_task_tool_authority.py tests/e2e/rbac/test_user_credential_auth_mode_policy.py
```
- With the fix reverted: `16 failed, 7 passed`. The 9 WP4 denial cases and the 7 WP6 cases fail. The keep-working tests pass both with and without the fix.
- With the fix: `23 passed`.

## Loop B: live backend (HTTP plus direct tool invocation)
`docs/feedback-loops/scripts/scheduled_task_and_credential_policy.py`. The backend runs on its own port with a fresh SQLite DB and is seeded with `tools/agent/seed_org.py --invite member@example.com`. No LLM key was available, so the tools are called in-process against the server's DB.

```bash
cd backend && TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/live.db .venv/bin/alembic upgrade head
.venv/bin/uvicorn main:app --port 8747 &
.venv/bin/python ../tools/agent/seed_org.py --base-url http://localhost:8747 --invite member@example.com
PYTHONPATH=$PWD BOW_BASE=http://localhost:8747 TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/live.db \
  .venv/bin/python ../docs/feedback-loops/scripts/scheduled_task_and_credential_policy.py
```
(Set `PYTHONPATH` to the checkout under test. Otherwise an editable install can import a different tree.)

| Action | Before the fix | After the fix |
|---|---|---|
| tool `required_permissions` | `[]` | `['update_reports']` |
| member: tool create on **admin's** report | `tool.end success=True` (task planted) | `tool.end success=False`, "Not allowed to create this scheduled task: Only the report owner…" |
| member: tool edit of **admin's** task | `success=True` (task rewritten) | `success=False`, clean denial |
| member: REST POST on admin's report | 403 | 403 |
| member: tool create + edit on own report | success | success |
| member: REST POST + PUT on own report | 200 / 200 | 200 / 200 |
| admin: tool create on own report | success | success |
| member POST `/my-credentials/test` keypair (not allowed) | **200**, outbound Snowflake connect attempted | **400** "Authentication mode not permitted…" |
| member POST `/my-credentials/test` oauth (not allowed) | **200**, outbound attempt | **400** |
| member POST `/my-credentials` keypair (control) | 400 | 400 |
| member PATCH userpass → keypair | **200**, stored mode is now `keypair` | **400**, stored mode stays `userpass` |
| member PATCH rotates the userpass secret | 422 (the row had already been flipped to keypair) | 200 |
| member POST `/test` userpass (allowed) | 200, reaches outbound | 200, reaches outbound |

## The fix
- `ScheduledPromptService.ensure_can_manage()`: the report must be in the org, the caller must own it, and the caller must hold org `update_reports`. This mirrors the REST decorator. It is called from `create_scheduled_prompt` and `update_scheduled_prompt`, so the REST and tool paths share one control.
- Both tools declare `required_permissions=["update_reports"]`, so the planner filter (`agent_v2._apply_tool_permission_filter`) hides them from principals who hold the permission nowhere. A 403/404 from the service becomes a `tool.end` with `success=False` and a readable message, so the agent can explain the denial instead of crashing.
- `UserDataSourceCredentialsService._user_auth_variant()` performs the registry user-scope check plus the `allowed_user_auth_modes` check. POST, PATCH (when `auth_mode` changes) and `/test` all use it, and `/test` calls it before building any client.

## What this proves / regression notes
- Owners still schedule and edit tasks from chat and from the REST/UI path. Allowed modes, including an `oauth`-only policy, still save and test. Rotating a secret in the existing mode still works.
- Out of scope, noted for follow-up:
  - `cancel_scheduled_task` still declares `[]`. The DELETE route requires `update_reports`.
  - A PATCH that changes only `credentials` on a row whose mode a later policy change disallowed is not re-checked here. Runtime credential resolution is the place to enforce a policy narrowing.
  - `POST /api/connections/{id}/my-credentials` (Windows credential store) is a separate code path that this change does not touch.
- Pre-existing failures that also fail on the base branch: `test_connection_oauth_flow.py::TestUserOverlaySync` (2) and `test_sharepoint_onprem.py` (3). Causes: `libodbc.so.2` is missing in the sandbox, and the proxy CA bundle makes `requests.Session.verify` a path.
