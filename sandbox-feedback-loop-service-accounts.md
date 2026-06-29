# Sandbox Feedback Loop — Service Accounts for API Use

Validates the new **service account** feature end-to-end in a fresh cloud
sandbox: a non-human, org-managed API principal with its own RBAC role and API
keys, not tied to any person.

This is the runnable feedback loop used to confirm the implementation works.

---

## What was built

A service account is a first-class principal backed by a hidden `users` row
(`is_service_account=True`, `is_active=False`) so every existing `user_id`
foreign key, ownership check, and the RBAC resolver work unchanged. Org binding
+ metadata live on a dedicated `service_accounts` table, so the account consumes
**no license seat** and never leaks into member lists.

Backend pieces:

- `app/models/service_account.py` — `ServiceAccount` (org_id, backing user_id,
  name, description, created_by, disabled_at).
- `app/models/user.py` — `is_service_account` flag.
- `app/models/api_key.py` — nullable `service_account_id`.
- `alembic/versions/c2d3e4f5a6b7_add_service_accounts.py` — migration.
- `app/core/permissions_registry.py` — new **non-EE** `manage_service_accounts`
  permission under "Members" (covered by `full_admin_access`).
- `app/core/permission_resolver.py` — `principal_belongs_to_org()` (Membership
  for humans **or** ServiceAccount for SAs).
- `app/core/permissions_decorator.py` + `routes/rbac.py` — use that helper so an
  SA with no Membership still passes org-binding checks.
- `app/core/auth.py` — `forbid_service_account_principal` dependency.
- `app/services/service_account_service.py`, `app/routes/service_account.py`,
  `app/schemas/service_account_schema.py` — CRUD, key issuance, role cap.
- `app/services/api_key_service.py` — keys carry `service_account_id`; auth
  rejects keys of disabled/deleted accounts.

Frontend pieces:

- `frontend/pages/settings/members.vue` — new **Service Accounts** sub-tab
  (gated by `manage_service_accounts`, no EE feature flag).
- `frontend/components/ServiceAccountsManager.vue` — list, create (with role),
  key issuance (copy-once), enable/disable, delete.
- `frontend/components/RolesManager.vue` + `locales/en.json` — the new
  permission renders in the role editor.

---

## Environment setup (fresh sandbox)

The app targets **Python 3.12**.

```bash
cd backend
pip install uv
uv sync --frozen --extra dev

export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
uv run alembic upgrade head      # includes c2d3e4f5a6b7_add_service_accounts
uv run python main.py            # http://localhost:8000
```

Frontend (for the UI screenshot):

```bash
cd frontend
yarn install
yarn dev                         # http://localhost:3000
```

---

## Backend end-to-end check (no UI needed)

`backend/scripts/verify_service_accounts.py` drives the whole flow against a
running server:

```bash
cd backend
BOW_DATABASE_URL="sqlite:///db/app.db" uv run python main.py &   # if not already up
uv run python scripts/verify_service_accounts.py
```

It asserts (18/18 passing):

1. Bootstrap admin registers + logs in; org resolved.
2. List roles; create a service account **CI Pipeline** with the `member` role.
3. SA reports its role and 0 keys.
4. **SA is absent from the org member list** (no seat, no leak).
5. Issue an API key (returns the `bow_…` secret once).
6. The SA key authenticates to the API and resolves its org from the key.
7. **SA can create/author a report (run queries), attributed to itself.**
8. Escalation guard: the SA key is **403** on `POST /api_keys`,
   `POST /service_accounts`, and `POST /…/roles`.
9. Disabling the SA **immediately rejects its key** (401/403); re-enabling
   restores access.

Expected tail:

```
==== SUMMARY ====
18/18 passed
ALL PASSED
```

---

## UI verification (screenshot)

1. Log in as the bootstrap admin at http://localhost:3000.
2. Settings → Members → **Service Accounts** tab.
3. **New service account** → name "CI Pipeline", role "member" → Create.
4. The detail modal opens; **Create key** → the `bow_…` token is shown once.
5. The list shows the account as **Active** with its role and key count.
6. Settings → Members → **Roles** → edit a role: the **Manage service accounts**
   permission checkbox appears under "Members & Access".

---

## Design decisions confirmed in code

- **B2 / A-backed** (backed users row + dedicated binding table): chosen so all
  `user_id` FKs work with zero migration and SAs consume no seat / never appear
  in member lists. See `docs/design/service-accounts.md`.
- **`is_active=False` is the login kill-switch**: interactive/JWT/SSO login is
  blocked, but `get_user_by_api_key` ignores `is_active`, so keys keep working.
- **Role cap**: a creator can only assign a role whose permissions they already
  hold (`_assert_creator_can_grant`); only a full admin can mint a full-admin SA.
- **Non-EE**: `manage_service_accounts` is a core permission; any full admin can
  CRUD service accounts without an enterprise license.

---

## Status

- [x] Migration applies on fresh SQLite (and is Postgres-safe via batch ops).
- [x] App imports; `/api/service_accounts*` routes registered.
- [x] Backend end-to-end script: 18/18 passing.
- [x] Frontend tab + manager component + RolesManager permission wired.
- [x] UI verified live: Service Accounts tab renders; created "Reporting Bot"
      via the UI and minted an API key (`bow_…`); service accounts correctly do
      NOT appear in the Members list. Screenshots captured.

Note: the frontend dev install needed the yarn.lock registry rewritten to
`registry.npmjs.org` (the sandbox proxy aborts `registry.yarnpkg.com` tarball
fetches); `bun install` completed it reliably. Roles/Groups tabs are EE-gated
(`custom_roles`), so the role-editor checkbox for `manage_service_accounts` is
verified in code (RolesManager `KNOWN_PERMISSION_KEYS` + registry + en.json),
not screenshotted in this OSS sandbox.
