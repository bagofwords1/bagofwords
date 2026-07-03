# Sandbox Feedback Loop — Role demotion / RBAC ↔ legacy-role divergence

Reproduces (and will later validate the fix for) the customer-reported bug:

> "We invited a user as an Admin… Later we changed their role from Admin to
> Member. After that they were no longer able to log in and were getting a
> permission-related error."

The root cause is **two parallel role stores that drift apart**:

| Store | What it is | Written by |
|-------|-----------|-----------|
| `Membership.role` | legacy string `'admin'`/`'member'` (`app/models/membership.py`) | invite (`add_member`), legacy `update_member` (`PUT /members/{id}`), SSO/SCIM provisioning |
| RBAC `role_assignments` → `roles` | **the source of truth for permission checks** (`app/core/permission_resolver.py`) | the Members UI (`/role-assignments`), `_attach_open_memberships` at registration |

Different role-change paths touch different stores, so the two disagree; and the
`sso_only` break-glass login gate (`_user_can_use_local_login`, `app/core/auth.py`)
reads the **legacy** string, so the drift turns into a login lockout.

This is the runnable feedback loop that confirms the behavior in a fresh sandbox.
It currently runs **RED** (the asserted states ARE the bug); after the fix the
same script must run **GREEN** with the fix-mode assertions (see "After the fix").

---

## What the bug looks like (the contract being violated)

1. A role change must update the user's **effective permissions** consistently —
   there must be exactly one source of truth. Today there are two.
2. Changing a role in the Members UI (`/role-assignments`) leaves
   `Membership.role` stale.
3. The legacy `update_member` (`PUT /members/{id}`) changes `Membership.role`
   but never touches RBAC, so a "demoted" admin **keeps** `full_admin_access`.
4. In `auth.mode = sso_only`, break-glass password login is decided by
   `Membership.role == 'admin'` — the wrong store — so a real RBAC admin can be
   locked out, and a demoted member can keep break-glass access.

---

## Environment setup (fresh sandbox)

Targets **Python 3.12** (uv provisions it even on a 3.11 host).

```bash
cd backend
pip install uv
uv sync --frozen --extra dev

export BOW_ENCRYPTION_KEY="dGVzdC1lbmNyeXB0aW9uLWtleS0zMmJ5dGVzLWxvbmchIQ=="   # any base64 32-byte key
export ANTHROPIC_API_KEY="sk-test-not-used"                                     # unused by this loop
export BOW_DATABASE_URL="sqlite:///db/app.db"
export ENVIRONMENT=development
mkdir -p db
uv run alembic upgrade head
```

The default dev config (`configs/bow-config.dev.yaml`) is **community / no
license** (`is_enterprise=False`) with `auth.mode: local_only`. That is enough:
the two `admin`/`member` **system roles** exist and are assignable via
`/role-assignments` in the community build (only *custom* roles/groups are
EE-gated), so the whole bug reproduces without a license.

An `sso_only` config used only for the login-gate phase:

```yaml
# /tmp/bow-config.sso.yaml  — clone of the dev config with:
auth:
  mode: "sso_only"
```

---

## Reproduction — Phase 1: store divergence (server in local_only)

```bash
# terminal 1 — default (local_only) server
uv run python main.py            # :8000

# terminal 2
BOW_PHASE=divergence uv run python scripts/verify_role_demotion.py
```

The script:
1. Registers the bootstrap admin; invites **alice**/**bob** as *admin*, **carol**
   as *member*; each accepts their invite and registers.
2. Demotes/promotes them three different ways:
   - **alice** admin→member via the **Members UI path** (`POST`+`DELETE /role-assignments`).
   - **carol** member→admin via the **Members UI path**.
   - **bob**  admin→member via the **legacy** `PUT /members/{id}` (`update_member`).
3. Reads both stores back and asserts they disagree.

Expected tail (RED = bug reproduced):

```
PASS - BUG alice: legacy role vs RBAC DRIFT after UI demotion
PASS - alice effective perms are member (no full_admin_access)
PASS - BUG carol: legacy role vs RBAC DRIFT after UI promotion
PASS - BUG bob: legacy update_member demoted to 'member' but RBAC still admin
11/11 checks matched expectation
```

Resulting states (confirmed via the admin members list):

```
alice   legacy_role=admin    rbac_roles=['member']   # UI demotion left legacy stale
carol   legacy_role=member   rbac_roles=['admin']    # UI promotion left legacy stale
bob     legacy_role=member   rbac_roles=['admin']    # legacy PUT left RBAC = still admin
```

## Reproduction — Phase 2: the sso_only login lockout (same DB)

```bash
# stop the local_only server, restart the SAME db in sso_only mode
BOW_CONFIG_PATH=/tmp/bow-config.sso.yaml uv run python main.py   # :8000

BOW_PHASE=sso uv run python scripts/verify_role_demotion.py
```

Expected tail (RED = bug reproduced):

```
PASS - sso_only: real bootstrap admin can break-glass login
PASS - BUG sso_only: real RBAC admin (carol) is LOCKED OUT of password login
PASS - BUG sso_only: demoted member (alice) still gets break-glass admin login
3/3 checks matched expectation
```

Raw login codes (`POST /api/auth/jwt/login`) in `sso_only`:

```
admin@example.com  (legacy=admin,  rbac=admin)   -> 200   correct
alice@example.com  (legacy=admin,  rbac=member)  -> 200   WRONG (demoted user keeps break-glass)
carol@example.com  (legacy=member, rbac=admin)   -> 400   WRONG (real admin locked out)
bob@example.com    (legacy=member, rbac=admin)   -> 400   WRONG (real admin locked out)
```

---

## EE vs community (why this is a core, not EE, bug)

- `admin`/`member` **system roles** exist and are **assignable** in the
  community build; `POST/DELETE /role-assignments` is gated only by
  `manage_members`, **not** by a license. Verified: `POST /roles` (custom role)
  returns **402** without a license, but role *assignment* works.
- The buggy code — the resolver, the login gate, `update_member`, whoami's
  `role` field — is all **core**. The fix must stay **license-independent**
  (never gated behind `custom_roles`), so a lapsed/community license can never
  strip already-resolved permissions.
- SCIM/LDAP/OIDC provisioning (which also create `Membership.role='member'` with
  no RBAC assignment) live under `app/ee/` and are verified at the code/DB level,
  not live in this OSS sandbox.

---

## After the fix (target GREEN behavior)

The reproduction script's assertions flip to the fixed contract:
- Any role change keeps the two stores consistent (either `Membership.role` is
  derived from RBAC, or `update_member` and `/role-assignments` both write both).
- `bob` demoted via any path loses `full_admin_access`.
- In `sso_only`, break-glass login is decided by resolved RBAC
  (`full_admin_access`), so **carol** (real admin) can log in and **alice**
  (demoted) cannot.
- Every membership resolves to at least its baseline role (no zero-permission
  users after provisioning/backfill).

---

## Status

- [x] Repro script `backend/scripts/verify_role_demotion.py` added.
- [x] Phase 1 (divergence) reproduces RED: 11/11 asserted bug-states.
- [x] Phase 2 (sso_only lockout) reproduces RED: 3/3 asserted bug-states.
- [x] Confirmed community/no-license: system-role assignment works; custom role
      creation is 402.
- [ ] Fix implemented (Tier 1+2: RBAC-authoritative, derive `role`, login-gate
      union, backfill migration + resolver safety net) — pending plan sign-off.
- [ ] Script re-run GREEN against the fixed build.
