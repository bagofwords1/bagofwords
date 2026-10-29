# Feedback loop: member-management escalation (pentest WP1)

Date: 2026-10-11. Findings: 828552157 (high), 828922733, 829374573.

## Loop

1. Backend booted on a fresh SQLite DB (`alembic upgrade head`, `ENVIRONMENT=production TESTING=true`)
   with the enterprise license faked the same way the `enterprise_license` e2e fixture does
   (custom roles, groups and service accounts are enterprise features; no real key in the sandbox).
   Frontend was not booted; the fix is backend-only (see "Frontend callers" below).
2. A Python `requests` script drives the real HTTP API: admin signs up, invites a delegate (custom
   role `view_members` + `manage_members`) and a member, invites then removes an "outsider", creates a
   group, a service account (same role) with a key, and a pending invite.
3. Same script run against the code before the fix and after it.

## Results

| Step | Before | After |
|---|---|---|
| [828552157] delegate `PUT` own membership `role=admin` | 200, delegate gets `full_admin_access` | **403**, no admin |
| [828552157] delegate `PUT` other member `role=admin` | 200, member gets `full_admin_access` | **403**, no admin |
| [828922733] add removed (non-member) user to group | 201 | **404** |
| [829374573] service account `GET .../invite-link` | 200, invite token returned | **403** |
| [regression] admin `PUT` member `role=admin` | 200 | 200 |
| [regression] delegate `PUT` member note | 200 | 200 |
| [regression] delegate adds org member to group | 201 | 201 |
| [regression] admin adds pending invite to group | 201 | 201 |
| [regression] human admin `GET .../invite-link` | 200 | 200 |

## Tests

`backend/tests/e2e/rbac/test_member_management_escalation.py` (9 tests):
without the fix 5 fail (one per attack path) and 4 regressions pass; with the fix all 9 pass.

## Frontend callers

- `MembersComponent.vue` uses `PUT /members/{id}` for notes (unchanged, still 200), and
  `GET /members/{id}/invite-link` from a human session (unchanged).
- Bulk/single group adds (`MembersComponent.vue`, `GroupsManager.vue`) pick users from this org's
  member list or pass `membership_id` for pending invites, so they never hit the new 404.

## Not verified

- No browser run; frontend checked by reading the call sites only.
- A `manage_members` delegate can still *demote* another member (including an admin) to `member` via this
  route. #1305 also doesn't restrict revocation; the last-admin lockout guard still applies.
