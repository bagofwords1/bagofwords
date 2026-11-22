# Feedback Loop — "Group admin: add/remove members, but not invite or see prompts"

An enterprise customer runs many groups, each with its own agents, and has no
way to let a team lead manage their own team: any change to a group's members
needed org-wide `manage_members`. This adds a **group manager**: a delegated
admin scoped to one group.

Decided with the product owner:

| Action | Org admin | Group manager | Member |
|---|---|---|---|
| Add existing org members to the group | ✓ | ✓ (escalation guard below) | ✗ |
| Remove ordinary members | ✓ | ✓ | ✗ |
| Promote a member to manager | ✓ | ✓ | ✗ |
| Demote / remove a manager | ✓ | ✗ | ✗ |
| Leave the group | ✓ | ✓ (ends the role) | ✓ |
| Edit/delete the group, change its roles or access, a member's org role | ✓ | ✗ | ✗ |
| Invite new users, see members' prompts | as today | ✗ | ✗ |

- **Default:** nobody is a manager after upgrade, so behaviour is unchanged
  until an org admin promotes someone.
- **Imported groups** (SCIM, LDAP, OIDC) never have managers. Their membership
  belongs to the identity provider.
- **Org admins:** unchanged. Only SCIM groups are locked for them, as before.

## Root cause / gap (validated)

- **No scope on group membership.** Every group-member route required org-wide
  `manage_members` (`backend/app/routes/rbac.py`, list/add/remove members).
  There was no per-group grant, and a group was never a resource you could
  grant on.
- **The Groups tab was hidden from delegates.** It was gated on
  `manage_groups`, a permission the backend does not have
  (`frontend/composables/useAdminNav.ts:49`, `GroupsManager.vue`), so only full
  admins ever saw it. `manage_role_assignments` (GroupsManager roles column)
  and `manage_groups` (`MembersComponent.vue` invite group picker) had the
  same defect.
- **The generic grant routes accepted any resource type.** A full admin could
  `POST /resource-grants` with `resource_type: "group"` and any principal,
  including a group or a role. That would have been a back door around any
  rule placed on promotion.

## Design

The manager role is a row in the existing `resource_grants` table
(`resource_type="group"`, `permissions=["manage_members"]`), so no migration is
needed. The rules live in one place:

- **`managed_group_ids`** (`backend/app/core/permission_resolver.py:824`) is
  the only source of truth. A grant counts only when all of these hold:
  - its principal is the *user* themselves
  - that user is a live member of the group and has a live org membership
  - the group is live and hand-managed (`external_provider IS NULL`)

  `apply_group_manager_grants` (`:862`) replaces whatever group grants the
  resolver loaded with exactly this set, in both the single-org and bulk
  resolvers. A stale or smuggled row therefore confers nothing, and whoami
  exposes `group:<id>: ["manage_members"]` only to real managers.
- **`group_admin_mode`** (`backend/app/services/rbac_service.py:724`)
  authorizes each group-membership route:
  - org-wide `manage_members` → acts as org admin
  - a manager of *that* group → acts as manager
  - anyone else → audited 403

  The route door is `view_members`, which binds the caller to the org.
- **Promote** `PUT /organizations/{org}/groups/{gid}/managers/{uid}` (org
  admin or a manager of the group):
  - the target must be a registered member of the group, else 422
    `group.manager_not_member`
  - the group must be hand-managed, else 409 `group.managed_externally`
  - non-full-admins can't promote themselves
- **Demote** `DELETE …/managers/{uid}`: org admin only.
- **Remove a member** as a manager: they can't remove another manager (403
  `group.manager_protected`), but can remove themselves. Removing anyone from a
  group drops their manager grant, so rejoining starts as a member.
- **Escalation guard:** unchanged for org delegates. For managers it checks
  each folder grant at the level the group carries it
  (`folder_requires_manage=False`). Without that, a manager of a group with
  "Can query" on a folder could never add anyone.
- **Generic routes refuse group grants:**
  - `ResourceGrantCreate` and `RoleResourceGrantInput` validators reject
    `resource_type="group"` (422)
  - update/delete by id refuse it (`_refuse_group_grant`)
  - the unfiltered grant listing omits it
- **Cleanup:**
  - removing a member from the org already deletes all their user grants
    (`revoke_member_org_access`)
  - deleting a group now deletes its manager grants
- **Audit:** `group.manager_promoted`, `group.manager_demoted`; add/remove
  entries record `as_group_manager`; every refusal logs a `.denied` entry.
- **UI** (`frontend/components/GroupsManager.vue`):
  - the Groups tab shows for `manage_members` holders or anyone managing at
    least one group (`useAdminNav`, `resourceType: 'group'`)
  - managers see only their groups, tagged "You manage", with no
    create/edit/delete/roles controls
  - the members dialog shows a Manager badge, "Make manager" (admin or
    manager) and "Remove manager" (admin only)
  - a manager gets no remove button on a fellow manager
  - add/remove now surface server errors through `useErrorMessage` instead of
    always toasting success
  - strings are in all 10 locale catalogs

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/rbac/test_group_manager.py` covers 17 cases, with the
enterprise license faked by the `enterprise_license` fixture:

```sh
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_group_manager.py -m e2e --db=sqlite -q
```

Before the change, all 17 fail: there is no managers endpoint, no
`manager_user_ids`, and the generic grant route accepts group grants:

```
17 failed, 4509 warnings in 41.11s
```

After:

```
17 passed, 4745 warnings in 43.85s
```

The cases cover:

- **Default:** a plain member is refused everywhere, and groups start with no
  managers.
- **Allowed for a manager:**
  - add and remove members, who then gain and lose the group's folder agent
  - promote a member
  - leave the group; being re-added doesn't restore the role
- **Refused for a manager:**
  - acting on another group
  - demoting or removing a fellow manager (an org admin can)
  - promoting a non-member, a member of another group, or an unknown id
  - editing or deleting the group, or giving it a role
  - cross-org ids
- **Back doors:**
  - generic grant route with a user or group principal
  - role editor
- **Imported groups:** scim, ldap and oidc. An existing grant stops
  conferring, and promotion is refused with 409.
- **Cleanup:** leaving the org, or deleting the group, ends the role.
- **Audit:** promotion, demotion and denials are logged.

## Loop B — live app, real HTTP and real browser (fake EE license, no LLM)

`tools/agent/group_manager_live.py` boots the real app on its own SQLite DB and
seeds through the API. `tools/agent/group_manager_ui.cjs` drives Chromium.

```sh
cd backend
uv run python ../tools/agent/group_manager_live.py serve &   # :8000
uv run python ../tools/agent/group_manager_live.py seed
(cd ../frontend && yarn dev) &                                # :3000
uv run python ../tools/agent/group_manager_live.py attack     # 37 HTTP attacks
cd .. && node tools/agent/group_manager_ui.cjs flow media/pr/ai-kind-mayer-wcdtw8
cd backend && uv run python ../tools/agent/group_manager_live.py reset
```

**Attack table**, run with real sessions. Every row behaved as specified:

```
PASS  manager lists own group / adds an org member / removes a member      200/201/204
PASS  member lists group / adds to own group / promotes self               403
PASS  manager lists, adds self to, removes from, promotes in other group   403
PASS  manager promotes non-member / random id                              422
PASS  manager promotes a member                                            204
PASS  manager demotes peer / removes peer manager / demotes self           403
PASS  admin demotes manager                                                204
PASS  manager renames / deletes / creates group                            403
PASS  manager gives group admin role / gives self admin role               403
PASS  manager invites new user                                             403
PASS  admin mints group grant (user principal / group principal)           422
PASS  manager mints group grant                                            422
PASS  role carries group grant                                             422
PASS  admin promotes in imported (oidc) group                              409
PASS  extra fields on add ({"is_manager": true, "permissions": [...]})     201, not promoted
PASS  foreign group id                                                     403
PASS  manager leaves → lists 403 → re-added → still not manager            204/403/201/403
37/37 as expected
```

**Browser flow** as `lena@` (Sales manager):

```
PASS  manager sees Sales on the Groups tab
PASS  manager does not see Finance
PASS  no "New group" button
PASS  olga added to Sales
PASS  pete shown as manager
PASS  no remove button on a fellow manager
PASS  sam removed from Sales
```

Then `check`, against the real permission resolver: Sam lost the Sales agent,
Olga gained it, and Lena manages exactly Sales.

```
{"email":"sam@example.com","groups":[],"manages":[],"agents":[]}
{"email":"olga@example.com","groups":["Entra Analysts","Sales"],"manages":[],"agents":["Sales pipeline"]}
{"email":"lena@example.com","groups":["Sales"],"manages":["Sales"],"agents":["Sales pipeline"]}
```

Evidence is in `media/pr/ai-kind-mayer-wcdtw8/`:

- before and after shots for admin and manager (`before-*`, `after-*`)
- Hebrew RTL shots (`after-he-*`)
- the flow recording (`flow.gif`)

Before the change, a manager sees no Groups tab at all.

## Review follow-ups (round 1)

Each point was confirmed against the code, fixed, and given a test that
fails on the previous commit:

1. **Promotion skipped the escalation guard.**
   - **Problem:** an org-wide `manage_members` delegate could promote a member
     of a group whose access they don't hold, and that manager could then admit
     others.
   - **Fix:** `promote_group_manager` now runs the same `_assert_can_grant` as
     `add_group_member`. Folder grants are checked at `manage` for org
     delegates and at the group's level for a manager of the group.
   - **Test:** `test_promotion_is_held_to_the_same_rule_as_adding_a_member`
     covers a group with folder access and a group carrying a role. The
     positive control is `test_org_delegate_can_promote_in_a_group_they_could_add_to`.
2. **Promotion could race a removal of the same member.**
   - **Fix:** promotion locks the member row `FOR UPDATE` and removal locks the
     same row, so they serialize. As a second line of defence, joining a group
     always deletes any leftover manager grant for that person.
   - **Test:** `test_promotion_and_removal_of_the_same_member_serialize`
     (PostgreSQL only) drives the real service methods on two connections and
     holds the promotion before its commit. It asserts that the removal waits
     on a lock (`pg_stat_activity`) and that no grant outlives the membership.
     Without the locks it fails with "removal finished while the promotion held
     the member row".
   - **Test:** `test_rejoining_never_restores_a_leftover_manager_grant`.
3. **The Groups roles picker always reported success.**
   - **Fix:** `updateGroupRoles` checks each request, reloads the real
     assignments, and shows the server's error.
   - **Verified** in the browser as a `manage_members` delegate picking Admin:
     the toast reads "You cannot grant permissions you do not hold" and the
     roles are unchanged (`media/pr/ai-kind-mayer-wcdtw8/after-delegate-role-refused.png`).
4. **The bulk resolver queried managers once per org.**
   - **Fix:** `managed_group_ids_by_org` fetches every org in one query, and
     the single-org resolver uses the same function.
   - **Test:** `test_session_reports_managed_groups_per_organization`.

**Results:**
- New test file: 23/23 on PostgreSQL 16 (`--db=external`); 22 pass + 1
  PostgreSQL-only skip on SQLite.
- Neighbouring suites (`test_member_management_escalation`,
  `test_invite_roles_and_groups`, `test_scim_groups`, `test_rbac_registry`,
  `test_rbac.py`, `test_agent_folders`, `test_membership`): 137 passed.
- Live attack table: 37/37.

## Follow-up: the Admin entry for group managers

**Problem.** A group manager who isn't an org admin had no way to reach the
Groups page except typing the URL. The app sidebar's Admin entry
(`frontend/layouts/default.vue`) appears only for org-wide admin permissions,
and a manager holds `manage_members` on their group only. The first browser
flow navigated to `/settings/members?tab=groups` directly, which hid this.

**Fix.** A user whose only admin power is managing groups now gets the Admin
entry (`useCanAny('manage_members', 'group')`), and it opens on
`/settings/members?tab=groups`. Plain members still get no Admin entry, and
org admins' entry is unchanged.

**Checked in the browser:**

```
before:  FAIL  manager: admin entry=false
after:   PASS  manager: admin entry=true landing=/settings/members?tab=groups
         PASS  member:  admin entry=false
         PASS  admin:   admin entry=true landing=/settings/members
```

`tools/agent/group_manager_ui.cjs flow` now starts from home and clicks Admin
(9/9 steps). Evidence: `before-manager-home.png`, `after-manager-home.png`,
`after-manager-admin-click.png`.

## Follow-up: the Admin entry opens each person's own page

**Problem.** The Admin entry always opened Members, a page every member can
view. Someone whose only admin permission is `manage_llm`, `view_audit_logs`,
`manage_settings` or `manage_service_accounts` therefore landed on a read-only
member list and had to find their own page.

**Fix.** `useAdminNav().adminLanding` (`frontend/composables/useAdminNav.ts`)
picks the landing page:

- org-wide `manage_members` → Members
- otherwise, the first top-level admin page the person can open (LLM, AI
  settings, Audit logs, Identity provider, …)
- otherwise, the first Members sub-page behind a real permission (Groups,
  Service accounts)

`layouts/default.vue` uses it for the entry, replacing the group-manager
special case.

**Checked** with `tools/agent/group_manager_ui.cjs landing`, which signs in as
each seeded role (`group_manager_live.py seed` adds one single-permission user
per role):

```
                         before                     after
org admin                /settings/members          /settings/members
group manager            /settings/members?tab=groups  (same)
LLM manager              /settings/members  FAIL    /settings/models
auditor                  /settings/members  FAIL    /settings/audit
settings manager         /settings/members  FAIL    /settings/ai_settings
service-account manager  /settings/members  FAIL    /settings/members?tab=service_accounts
plain member             no Admin entry             no Admin entry
```

Evidence: `before-landing-llm-manager.png`, `after-landing-llm-manager.png`,
`after-landing-auditor.png`.

## What this proves / regression notes

- **What a manager can reach.** They can only change who is in the groups
  they manage. They can't widen what any group can access, reach another
  group, or mint the role through any other route. Whatever they add carries
  exactly the group's existing access.
- **The guarantee holds at read time.** Even a hand-inserted grant row
  confers nothing unless the holder is a live member of a live, hand-managed
  group.
- **Regression suites.** `tests/e2e/rbac`, `test_rbac.py`, `test_membership*`,
  `test_oidc_group_sync.py` and `org_isolation` (1,796 tests): 1,732 passed,
  58 failed, 6 errors with the change.
  - **Registry ratchet:** one failure was
    `test_rbac_registry::test_frontend_use_can_permissions_exist_in_registry`,
    which correctly flagged that `manage_groups` and `manage_role_assignments`
    are no longer stale. They were removed from its known-stale list, and it
    now passes.
  - **Pre-existing:** the other 57 failures and 6 errors are in
    `org_isolation/{test_sign_in_email_linking, test_oidc_org_mapping,
    test_reserved_instance_admin_email, test_expired_invite_is_never_claimed,
    test_invite_claims_only_its_organization}.py` and
    `rbac/test_list_files_persist_per_user.py`. Those files give the identical
    57 failed / 6 errors on the unmodified base commit.
- **Frontend unit tests.** 14 of 111 fail identically with the change stashed
  (artifact runtime browser tests).

**Follow-ups, not done here:**

- The Members and Roles tabs still gate on `manage_roles`,
  `add_/update_/remove_organization_members`, which the backend doesn't have.
  Org-wide `manage_members` delegates therefore don't see those controls,
  though the API allows them within the escalation guard. The guard's
  "hide what 403s" follow-up in `rbac-manage-members-privilege-escalation.md`
  still applies.
- Scoping monitoring by group, and invite requests that group managers raise
  for an admin to approve, were deliberately left out.
