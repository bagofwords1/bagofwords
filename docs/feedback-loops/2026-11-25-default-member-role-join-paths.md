# Feedback Loop — "the default-role assignment is not working as expected"

An admin set the Members page **Default role** to one of the organization's own
custom roles (`role-default-access`). A new user who signed in through the
company's SSO arrived with the built-in **Member** role instead. Claim validated
here: the default role was read **only** by the Add-member path. Every other way
a person joins an organization hardcoded `"member"`, or read a separate
sign-up setting.

## Root cause (validated)

- `organization_service.add_member` was the only caller of
  `resolve_default_member_role` (`backend/app/services/organization_service.py:441-446`).
- Every other join path ignored it:

  | Join path | Before |
  |---|---|
  | SAML first sign-in, no invite (`services/saml_service.py` `admit_user`) | `"member"` |
  | SCIM create, both branches, plus its pending invite (`ee/scim/service.py`) | `"member"` |
  | OIDC group sync (`ee/oidc/group_sync_service.py` `_ensure_org_membership`) | `"member"` |
  | LDAP group sync (`ee/ldap/sync_service.py` `_ensure_org_memberships`) | `"member"` |
  | LDAP first sign-in (`core/auth.py` `admit_directory_identity`) | `"member"` |
  | Members CSV import (`organization_service.py` import loop) | `role="member"` passed explicitly |
  | Domain signup, chat auto-provision, Entra admission | `signup_policy.auto_invite_role`, default `"member"` |

- Even where a custom role name was stored, nothing granted it. Two causes:
  - `ensure_system_role_assignment` only resolved system roles. This was fixed
    in bagofwords1/bagofwords#1924, which this change builds on.
  - Chat auto-provision never created an RBAC assignment at all. The
    legacy-role fallback only understands `admin` and `member`.
- Latent bug found on the way: OIDC org mapping's `_rank` ranked any custom role
  at -1. The next sign-in under a `member` rule would therefore "raise" a
  custom-role member to built-in `member`, overwriting the admin's choice.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_default_member_role_join_paths.py -m e2e --db=sqlite -q
```

The test drives each join path at its real entry point: HTTP routes for Add
member, import and register, and the service function each identity callback
calls after verification for SAML, SCIM, LDAP and OIDC. The identity providers
are the only boundary stubbed. The test asserts the invariant: the person holds
the org's default role as a resolved RBAC role, with its permissions, and not
the built-in `member`.

**Before** (main + #1924, this fix stashed):

```
FAILED ...[chat_auto_provision]   FAILED ...[csv_import]
FAILED ...[domain_signup]         FAILED ...[ldap_first_login]
FAILED ...[ldap_group_sync]       FAILED ...[oidc_group_sync]
FAILED ...[oidc_org_mapping]      FAILED ...[saml_first_login]
FAILED ...[scim_create]
FAILED ...test_oidc_mapping_keeps_custom_role_on_later_sign_ins
10 failed, 4 passed
```

The 4 that passed are `add_member_api`, the only path that already worked, and
the three control tests.

**After:** `14 passed`.

## Loop B — live stack, real HTTP (no credentials needed)

```bash
cd backend
uv run python ../tools/agent/default_member_role_live.py serve &   # fake EE license, own SQLite DB
uv run python ../tools/agent/default_member_role_live.py seed      # custom role set as Default role
uv run python ../tools/agent/default_member_role_live.py join      # Add member / import / domain signup / SCIM x2 / control
uv run python ../tools/agent/default_member_role_live.py check
(cd ../frontend && yarn dev)                                       # for screenshots
```

Before:

```
OK  added@test.example       role-default-access   Add member (no role)
BAD imported@test.example    member                CSV import
BAD carol@acme-corp.example  member                domain signup
BAD dave@okta.example        member                SCIM (Okta)
BAD erin@okta.example        member                SCIM (Okta)
OK  control@test.example     member                Add member, role=member (control)
RESULT: 4 member(s) got the wrong role
```

After: every row is `OK`. The four people above get `role-default-access`; the
control keeps `member`.

Screenshots are in `media/pr/default-member-role/`, in `before-*` / `after-*` pairs.

## The fix

- **One source of truth:** `resolve_default_member_role(db, org_id)` in
  `organization_settings_service.py`.
  - The stored name may be a system role or a custom role.
  - A name that no longer resolves, such as a deleted role, falls back to
    `member` rather than leaving the person with no role.
  - Every join path in the table now calls it.
- **Sign-up override:** `resolve_signup_role(db, org_id, policy)`.
  - `signup_policy.auto_invite_role` is now an *optional override*; empty, the
    new default, means "use the org default".
  - A stored override is still honored. Orgs that saved `member` keep it until
    an admin switches the picker to "Organization default".
- **Chat auto-provision** now creates the RBAC assignment.
- **Explicit choices still win over the default** (follow-up from review):
  - an OIDC org-mapping rule's role is operator configuration and is used
    as written; `_rank` treats a custom role as member-tier, so a later
    `member` rule doesn't overwrite it, and an `admin` rule still raises it;
  - inside a host application's embedded sign-in window, both resolvers
    return the host's role (`app_user` or `member`), and `stamp_membership`
    stamps rows that start as that role, so the org default can't widen an
    embedded account.
- **The default is writable only by full admins:** `default_member_role` is
  in `PROTECTED_CONFIG_KEYS`, so the generic settings writer (which only needs
  `manage_settings`) refuses it.
- **Both setters validate through `resolve_assignable_role`.** Before, picking
  `member` after customizing the Member role matched two rows and returned a 500.
- **CSV import** passes `role=None`, so `add_member` resolves the default, as
  the modal does.
- **UI:**
  - an info tooltip on **Default role** lists the paths it covers;
  - the Signup page picker gains an "Organization default (‹role›)" option. It
    is backed by a sentinel value because `USelectMenu` can't select `''`.
  - New strings were added to all 10 locale catalogs.

## What this proves / regression notes

- The invariant holds for 9 join paths, with these controls:
  - an explicit Add-member role and a sign-up override both beat the default;
  - with no default set, every path still gives `member`;
  - a deleted default role falls back to `member`;
  - an OIDC mapping rule's role and an embedded host's role (SAML and LDAP)
    are honored;
  - the generic settings writer refuses the default, for an admin and for a
    `manage_settings` role;
  - a customized Member role can be picked in both settings.
- Related suites, run with `ENVIRONMENT=production` like CI: SCIM, SAML, LDAP,
  OIDC group sync, OIDC org mapping, `org_isolation/`, invites, membership
  import, seat caps, the signup-policy and default-role settings, and the RBAC
  invite/escalation/app-user suites. Result: **612 passed, 1 skipped**. Related
  unit tests: **386 passed**.
- Under the default `development` environment, 39 `org_isolation` OIDC tests
  fail with a 404 on `/api/auth/{provider}/callback`. They fail identically
  with this change stashed: the dev config uses `auth.mode: local_only`, so the
  SSO routes aren't mounted. Run them with `ENVIRONMENT=production`.
- Not covered by a dedicated test: Entra token-exchange admission. It calls
  the same `resolve_signup_role` as domain signup.
