# Feedback Loop — "When adding a member invite, can we set role(s) and group(s)?"

The Invite Member modal let an admin pick **one** role and several groups. Two
defects sat behind it, and multi-role was missing entirely:

1. **A custom role chosen in the modal was silently dropped.** The dropdown
   listed every role (custom included), but the choice was sent as the legacy
   `role` string, and only built-in names are ever turned into a role
   assignment. The pending row even *displayed* "Analyst"; after registering,
   the person held no role at all. A misspelt role name was also accepted.
2. **Inviting an email that already has an account, with groups, failed but
   reported success.** The modal added groups in follow-up calls using the
   new membership id. For a registered user, that endpoint refuses a
   membership principal ("Membership is already registered; use a user
   principal instead"), so each group raised a red error toast, then a green
   "Invitation sent" toast. The user ended up in no groups.

## Root cause (validated)

- `frontend/components/MembersComponent.vue` (`inviteRoleOptions`) offered
  every role from `/roles` by **name**; `inviteMember` sent it as `role`.
- `backend/app/services/organization_service.py` `add_member` stores `role` on
  `Membership.role`. The only places that turn that string into RBAC
  (`_assign_system_role`, and `ensure_system_role_assignment` on invite
  acceptance in `backend/app/core/auth.py`) resolve it with
  `resolve_assignable_system_role` (`backend/app/core/permission_resolver.py`),
  which matches built-in roles only. A custom name → `None` → nothing assigned.
  The escalation guard used `_system_role_permissions`, which also returns
  `[]` for a custom name, so it passed.
- The modal's group loop always used `{membership_id}`;
  `rbac_service._get_pending_membership` rejects a membership that has a
  `user_id` (correct for that endpoint; wrong principal chosen by the caller).
  The quota pre-assignment had the same `principal_type: 'membership'` bug.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/rbac/test_invite_roles_and_groups.py` (enterprise license
faked by the existing `enterprise_license` fixture):

```sh
cd backend
TESTING=true uv run pytest tests/e2e/rbac/test_invite_roles_and_groups.py -m e2e --db=sqlite -q
```

Before the fix (base `538b4942`):

```
FAILED ...::test_pending_invite_applies_all_roles_and_groups
FAILED ...::test_pending_invite_with_only_a_custom_role_holds_that_role
FAILED ...::test_legacy_role_string_naming_a_custom_role_is_granted
        assert {'manage_members','view_members'} <= {...}   # custom role never granted
FAILED ...::test_unknown_role_name_is_rejected
        assert 200 in (400, 404)                            # typo accepted
FAILED ...::test_inviting_existing_account_applies_roles_and_groups
FAILED ...::test_unknown_role_or_group_rejects_invite_without_side_effects[role|group]
FAILED ...::test_delegate_cannot_invite_with_admin_in_role_ids
FAILED ...::test_delegate_cannot_invite_into_group_carrying_admin
FAILED ...::test_delegate_can_invite_with_roles_and_groups_they_hold
10 failed
```

## Loop B — live app (real UI, fake EE license, no LLM calls)

```sh
cd backend
uv run python ../tools/agent/invite_access_live.py serve &     # :8000
uv run python ../tools/agent/invite_access_live.py seed
cd ../frontend && yarn dev &                                    # :3000
# "before": BOW_BACKEND_DIR=<checkout of the base commit>/backend ... serve
uv run python ../tools/agent/invite_access_live.py check priya@example.com
```

Drive Members → Add Member:

| Scenario | Before | After |
|---|---|---|
| `priya@example.com`, role Analyst (+ Data steward after), group Finance, then registers | row shows "Analyst"; after registering: `roles: []`, shown as Member | `effective_roles: ["analyst","data steward"]`, Finance |
| `dana@other.example` (account in another org), groups Finance + Marketing | 2× "Membership is already registered…" + "Success"; `groups: []` | `roles: ["analyst","member"]`, `groups: ["Finance","Marketing"]`, one Success toast |

Screenshots and GIFs: `media/pr/invite-roles-groups/`.

## The fix

- **API**: `MembershipCreate` takes `role_ids` and `group_ids`
  (`backend/app/schemas/organization_schema.py`). `add_member`:
  - validates every role (system or this org's) and group (enterprise
    `custom_roles`, not SCIM-managed) **before writing anything**. An unknown
    id → 404 and no membership is created.
  - runs the escalation guard over **everything** the invite confers: the
    union of the roles' permissions and resource grants, plus each group's
    role permissions and grants (`member.invite` audit on denial).
  - stages the role assignments and group memberships **in the same commit**
    as the membership. A registered user gets `user` principals; a pending
    invite gets `membership` principals, which the existing
    `_materialize_pending_rbac` moves onto the user at registration. Rows a
    re-added user still holds are skipped, not duplicated.
  - legacy `role` string: a built-in name works as before; a custom role
    name is now granted; any other name → 404 `role.not_found`.
- **UI** (`MembersComponent.vue`): "Roles" is a multi-select over all roles
  (by id), prefilled with the org default role. Roles and groups are sent in
  the single invite request, so the group follow-up loop is gone. The quota
  pre-assignment uses a `user` principal for an existing account. When the
  roles list can't be loaded, the modal falls back to the legacy single-role
  select. New catalog key `settings.members.rolesLabel` (en/es/he).

After: `10 passed`. Wider run of `tests/e2e/rbac` plus the
membership/RBAC/OIDC suites: 1283 passed. The 6 errors in
`test_list_files_persist_per_user.py` come from the sandbox proxy CA bundle
in the `create_data_source` fixture (`requests.verify` is a path); they are
unrelated to this change.

## What this proves / regression notes

- The invariant tested is "an invite holds exactly the roles and groups
  chosen, for both pending and existing accounts, atomically, under the
  grant guard". It is not tied to the reported role or group names.
- Not run here: the Postgres leg (no Docker in the sandbox) and the frontend
  Playwright suite. `tests/members/member-flow.spec.ts` relies on the
  prefilled default role, which still holds (Member is preselected and Send
  is enabled).
