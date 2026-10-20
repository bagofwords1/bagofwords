# Feedback Loop — RBAC audit side doors (#2, #11, #16, #18)

Four findings from the access-control audit, each an authorization check that a
sibling route already performs but one route skips. #20 (entity scope clearing)
was reviewed and deliberately left out of this change.

| # | Report | Who could do it |
|---|--------|-----------------|
| 2 | Viewer of a shared conversation can steer the owner's active run via `/steer` | Any org member with `create_reports` and the completion id (viewers see it) |
| 11 | Data-source manager can edit Global instruction directories | Anyone holding `manage_instructions` on a single agent |
| 16 | Service-account principals can manage SCIM tokens | A service-account key whose role has `manage_identity_providers` |
| 18 | Non-admin can create resource grants that apply to a whole role | Anyone holding `manage` on a single agent |

## Root cause (validated)

- **#2** — `steer_completion` and `delete_queued_completion`
  (`backend/app/services/completion_service.py`) loaded the completion with an
  org-only check. The route's `@requires_permission('create_reports')` has no
  `owner_only`. Sending a prompt (`routes/completion.py`, `owner_only=True`)
  and `sigkill` (`_get_completion_for_run_owner`) both restrict run control to
  the run's owner. `/steer` and `DELETE …/queued` did not.
- **#11** — `_assert_manage_scope` (`backend/app/routes/instruction.py`) skipped
  every check when `data_source_id` was null, because its docstring assumed the
  route decorator gated Global org-wide. All four directory routes are
  `resource_scoped=True`, and that flag admits anyone holding the permission on
  *any* agent (`core/permissions_decorator.py`, resource_scoped branch).
- **#16** — `scim_admin_router` (`backend/app/ee/scim/routes.py`) lacked
  `forbid_service_account_principal`. The RBAC, API-key and service-account
  routers all use that dependency so that a leaked key cannot self-escalate. A
  SCIM token provisions users and groups. `ldap_admin_router` had the same gap
  and gets the same fix.
- **#18** — `ResourceGrantCreate.principal_type` was a free `str`
  (`backend/app/schemas/rbac_schema.py`). `_require_resource_manage` only checks
  `manage` on the resource, and the resolver honours `principal_type="role"`.
  So an agent manager could grant to the whole `member` role (effectively the
  org), or store arbitrary principal types. Role-scoped grants are otherwise
  written only by the role editor (`_sync_role_grants`), which also hard-deletes
  them on every role save.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/rbac/test_rbac_audit_side_doors.py`. The LLM boundary is
stubbed at `AgentV2.main_execution` to keep a run `in_progress`. Everything
else runs for real: routes, services and the DB.

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_rbac_audit_side_doors.py -m e2e --db=sqlite -n 4
```

**Before the fix** (`3c74e121`, PR #1304 head): `14 failed, 4 passed`. Each
failure is the unauthorized call *succeeding*:

```
assert 201 == 403   # SA key minted a SCIM token -> {"name":"from-sa","token":"bow_scim_…"}
assert 200 == 403   # SA key listed SCIM tokens / read LDAP sync status
assert 204 == 403   # SA key deleted the admin's SCIM token
assert 200 in (403, 422)  # agent manager created grants with principal_type role / organization / bogus
assert 200 == 403   # agent manager created, renamed, deleted Global folders and placed a Global instruction
assert 200 in (403, 404)  # other member: {"status":"steered"} x2, {"status":"deleted"} (owner's queued prompt)
```

The 4 that pass are the "still allowed" controls: a human holding the
identity-provider role, an agent manager granting to users and groups, an
agent manager editing their own agent's folders, and the owner steering their
own run.

**After the fix:** `18 passed`.

## Loop B — live confirmation (#2, real backend + real LLM provider)

`tools/agent/repro_steer_viewer_live.py` needs a running backend seeded with
`seed_org.py --invite viewer@example.com --sqlite-sources 1`. The owner shares
the conversation with the viewer and starts a real `gpt-6-luna` run. The
viewer then POSTs `/steer`. The key comes from `OPENAI_API_KEY_LIVE` and is
never written to disk in the repo.

```
=== BEFORE (fix stashed) ===
share conversation with viewer -> 200
VIEWER steer -> 200 {"status":"queued","reason":"completion_not_running"}
viewer-injected rows in owner's timeline: 1
=== AFTER (fix applied) ===
share conversation with viewer -> 200
VIEWER steer -> 404 {"detail":"Completion not found"}
viewer-injected rows in owner's timeline: 0
```

Caveat: the supplied OpenAI key was expired (`401 expired_secret_key`), so the
run errored. In the "before" pass it had already ended by the time the steer
landed, which is why steer fell back to *queueing*. That is still the bug: the
viewer's message was written into the owner's conversation. Loop A covers the
in-flight `"steered"` case deterministically.

## The fix

- `completion_service.py`: `steer_completion` and `delete_queued_completion`
  load through `_get_completion_for_run_owner`, the same rule `sigkill` uses
  (report owner or run starter, 404 otherwise).
- `instruction.py`: `_assert_manage_scope` requires org-level
  `manage_instructions` for the Global scope.
- `ee/scim/routes.py`, `ee/ldap/routes.py`: admin routers depend on
  `forbid_service_account_principal`.
- `rbac_schema.py`: `ResourceGrantCreate.principal_type: Literal["user", "group"]`.
  The frontend only sends these two values.

## What this proves / regression notes

- Each invariant has a negative test and a positive one, so the fixes cannot
  pass by blocking everyone.
- Neighbouring suites pass with the change applied:
  `tests/e2e/rbac`, `test_rbac.py`, `test_completion_queue_steer.py`,
  `test_instruction_directories.py` and `test_scim.py`.
- `tests/unit` has 76 failures in this sandbox (sharepoint on-prem, OBO,
  sybase, ODBC `libodbc.so.2` and others). The failing set is identical with
  the fix stashed, so they are environmental and pre-existing.
