# Feedback Loop — "Privilege Escalation from manage_members to full_admin_access in RBAC role management"

A security scanner flagged `backend/app/routes/rbac.py` (High): a user holding
`manage_members` can reach `full_admin_access`. Claim validated: a delegate
who only holds `view_members` + `manage_members` could make themselves (or
anyone) a full org admin via three independent routes.

## Root cause (validated)

The RBAC write routes check only `@requires_permission("manage_members")`
(`backend/app/routes/rbac.py`: create/update role, add group member, create
role assignment). The service methods they call
(`backend/app/services/rbac_service.py` — `create_role`, `update_role`,
`add_group_member`, `create_role_assignment`) never compared what is being
granted with what the caller holds. The only existing guards were "system
roles are immutable" and the last-admin lockout check.

Escalation paths (all returned 2xx before the fix):

1. `POST /role-assignments` with the system `admin` role → self or anyone.
   This path does not even need an enterprise license.
2. `POST /roles` / `PUT /roles/{id}` with `full_admin_access` (or any
   permission the caller lacks), then assign it.
3. `POST /groups/{id}/members` to join a group that already holds `admin`.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
uv run pytest tests/e2e/rbac/test_rbac_privilege_escalation.py -m e2e --db=sqlite -q
```

Setup (via real API, enterprise license stubbed): admin creates a
`people-mgr` role = `[view_members, manage_members]`, assigns it to a
delegate; a plain member is also invited.

Observed on unfixed code:

```
9 failed, 2 passed
      7 E       assert 200 == 403
      2 E       assert 201 == 403
```

Every escalation attempt succeeded; the two passing tests are the positive
controls (admin can still grant admin; delegate can grant within own perms).

## The fix

`backend/app/services/rbac_service.py`:

- `_assert_can_grant` (line 35): resolves the actor's effective permissions;
  full admins pass, anyone else gets **403** unless every org permission
  *and* every role-scoped resource grant being conferred is one they already
  hold. Covers `full_admin_access` implicitly (only a full admin holds it).
- `_assert_not_self` (line 63): non-full-admins cannot assign roles to
  themselves or add themselves to a group.
- Applied in `create_role`, `update_role`, `create_role_assignment` (role's
  permissions + its resource grants) and `add_group_member` (union of the
  group's roles + the group's resource grants).
- Denied attempts are audit-logged as `<action>.denied` with the missing
  permissions.
- `actor_id` is an optional keyword; routes pass `current_user.id`.
  Internal callers that pass nothing keep today's behavior.

Re-run of Loop A after the fix:

```
11 passed
```

Regression run of the surrounding suites:

```
uv run pytest tests/e2e/rbac tests/e2e/test_rbac*.py tests/e2e/*member*.py tests/e2e/*scim*.py -m e2e --db=sqlite -n 4
667 passed, 8 skipped
```

## What this proves / regression notes

- `manage_members` is now delegated administration bounded by the caller's
  own permissions, not a path to `full_admin_access`.
- Full admins are unaffected.
- A delegate can still create roles and assign them to *other* people within
  their own permission set.
- Not changed: removal paths (delete assignment/role, remove member) are not
  escalations. They are still covered by the last-admin lockout. The UI may
  still offer actions that now return 403; hiding them is a follow-up.
