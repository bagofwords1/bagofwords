# Sandbox Feedback Loop — License Seat Cap on Auto-Provisioning

Validates the fix for the reported bug: an **EE org with a 25-seat cap kept
gaining uninvited members** (Entra provisioning), while the **admin's own invite
was blocked** at the overflowed count.

This is the runnable feedback loop used to confirm the fix works end-to-end
against a live server.

---

## Root cause

The license seat cap (`max_users`) was enforced on exactly two paths — admin
invite and CSV import (`app/services/organization_service.py`). Every
**auto-provisioning** path minted memberships with no seat check:

- domain-signup invites — `app/core/auth.py::_create_domain_invites`
- chat auto-provision — `app/core/auth.py::auto_provision_user_for_org`
- LDAP group sync — `app/ee/ldap/sync_service.py::_ensure_org_memberships`
- **SCIM provisioning** — `app/ee/scim/service.py::create_user`
- OIDC group sync — `app/ee/oidc/group_sync_service.py::_ensure_org_membership`

For an **Entra ID** customer the live leak is SCIM provisioning and/or
domain-signup JIT (both enterprise-tier features that ship on the same license
carrying the cap). Those paths pushed the org past the cap; the admin's invite —
the one guarded path — then 402'd on the now-overflowed count. Exactly the
reported symptom.

---

## What was built

`app/core/seats.py` — single source of truth for counting memberships and
deciding whether new ones fit:

- `count_org_memberships` — live members + pending invites (`deleted_at IS NULL`)
- `seats_remaining` — `None` if unlimited, else `max(0, cap − current)`
- `has_seat_for(adding=1)` — graceful boolean for auto-provisioning paths
- `enforce_seat_limit(adding=1)` — raises HTTP 402 for paths that surface errors

Wired into all five bypass sites, each degrading per the chosen policy
(**block only truly new members** — existing members are never removed or blocked;
only creation beyond the cap is refused):

| Path | Behavior when full |
|------|--------------------|
| Domain-signup invites | Skip that org's invite + log; other admitting orgs still processed |
| Chat auto-provision | Return `None` before creating anything (no orphan user) |
| LDAP sync | Fill up to remaining seats (sorted, deterministic), skip + log the rest |
| SCIM provisioning | Reject with 402, after the existing 409 duplicate guard |
| OIDC group sync | Skip new membership + log; existing members untouched |

`organization_service` now delegates to the shared helper — the already-guarded
admin invite / CSV import paths are unchanged.

---

## Environment setup (fresh sandbox)

The app targets **Python 3.12**.

```bash
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e .            # or: uv sync --frozen --extra dev

export BOW_DATABASE_URL="sqlite:///db/app_sandbox.db"
mkdir -p db
python -m alembic upgrade head
```

The seat cap is license-driven, so the sandbox needs an **active enterprise
license with a small `max_users`**. `backend/scripts/gen_sandbox_license.py`
generates a throwaway RSA keypair, installs the test public key at
`app/ee/license_public_key.pem` (backing up the original to `.orig`), and prints
a signed `bow_lic_…` license with `tier=enterprise, max_users=3`. It only uses a
local throwaway key — it never touches the real signing key:

```bash
LIC=$(python scripts/gen_sandbox_license.py 3)   # backs up + swaps the public-key pem
BOW_LICENSE_KEY="$LIC" BOW_DATABASE_URL="sqlite:///db/app_sandbox.db" python main.py &
# ... run the check below ...
# afterwards, restore the tracked key:  mv app/ee/license_public_key.pem.orig app/ee/license_public_key.pem
```

Server log confirms load: `Enterprise license active: Seat Cap Sandbox, tier: enterprise`.

---

## Backend end-to-end check (no UI needed)

`backend/scripts/verify_seat_cap.py` drives the whole flow over real HTTP against
a running server. It reads the cap from `GET /api/license/usage` and drives to
exactly fill + overflow:

```bash
python scripts/verify_seat_cap.py
```

It asserts (11/11 passing):

1. License is active with a seat cap (`GET /license/usage`).
2. Org starts at 1 seat (the admin).
3. Mint a SCIM token (Entra's provisioning credential).
4. **SCIM provisions up to the cap** (each fresh user → 201).
5. **SCIM create over the cap → 402** — the bug: this used to be 201.
6. SCIM re-provision of an existing member → **409** (not masked by the 402;
   ordering preserved).
7. Enable the org's domain-signup policy.
8. **Over-cap domain-signup registration still creates the user account…**
9. **…but grants NO org membership** (auto-provision refused, not a login error).
10. The admin's **manual invite over the cap → 402** (the reported symptom).
11. **Member count held at the cap (3)** — never exceeded.

Expected tail:

```
==== SUMMARY ====
11/11 passed
ALL PASSED
```

### Negative control (harness has teeth)

With the two `enforce_seat_limit` calls in `app/ee/scim/service.py` temporarily
neutered (simulating the pre-fix path) and the DB reset, the same run reports:

```
FAIL - SCIM create over the cap -> 402   >> 201 {... userName: u_…@scimsandbox.com ...}
FAIL - member count held at the cap (3)   >> current_users=4
9/11 passed
```

i.e. the leak reproduces (a 4th member slips past a cap of 3), confirming the
check — not a false green — is what makes the fixed run pass.

---

## Automated tests

- `backend/tests/e2e/test_seat_cap_autoprovision.py` — new: the shared helper
  plus one case per path (auto-provision blocked & allowed, OIDC skip, LDAP
  fill-to-cap, SCIM 402). **6/6 passing.**
- `backend/tests/e2e/test_license_limits.py` — unchanged, still **21/21** (admin
  invite + CSV import behavior preserved by the refactor).
- Touched suites regression-checked green: `test_oidc_group_sync.py` (8),
  `test_scim.py` + `test_ldap.py` (27).

---

## Status

- [x] Migration/setup runs on fresh SQLite; enterprise license loads with a cap.
- [x] Live e2e over HTTP: **11/11 passing** with the fix.
- [x] Negative control: with the SCIM check disabled, over-cap SCIM leaks
      (201 / count=4) — the harness catches the regression.
- [x] Unit/e2e suites: 6 new + 21 license + 8 OIDC + 27 SCIM/LDAP green.
- [x] Test public key restored; no license material committed.

Note: LDAP and OIDC *login-time* provisioning are covered by the pytest e2e
suite (they need a live directory / IdP to drive over HTTP); SCIM and
domain-signup — the two Entra-relevant paths — are driven live here.
