# Service Accounts for API Use — Design Document

**Status:** Draft / Design Phase (analysis complete — not implemented)
**Date:** 2026-06-26
**Author:** (design only — not implemented)

---

## Problem Statement

Today the only programmatic credential is a **per-user API key** (`ApiKey`,
`backend/app/models/api_key.py`). Every key carries a `NOT NULL user_id` and
resolves straight back to a human `User` (`api_key_service.get_user_by_api_key`,
`backend/app/services/api_key_service.py:118`). A key is therefore pure
**impersonation**: a request authenticated by it runs as that person, inherits
*their* roles and data-source grants, and any object it creates is owned by
them.

We want a **service account**: a first-class, non-human principal that

- authenticates via API key (CI pipelines, MCP integrations, automation),
- has its **own RBAC role** (not a copy of some employee's permissions),
- is **owned/managed at the org level** and survives employee offboarding,
- is attributable in audit logs as itself ("CI Pipeline"), not as a person,
- **cannot** log in interactively (no password / SSO / JWT), and
- **cannot** escalate its own privileges.

This document analyses the existing backend and specifies how to add service
accounts with the smallest, lowest-risk surface.

## Goals

- A managed `ServiceAccount` principal per organization, with a display name,
  description, owner, and enable/disable switch.
- One or more API keys bound to a service account (reuse the existing `ApiKey`
  machinery — same `bow_` format, hashing, expiry, revocation).
- Its own RBAC role(s) via the existing `RoleAssignment` mechanism.
- Zero migration churn on the ~40 `users.id` foreign keys.
- Hard guardrails against interactive login and self-escalation.

## Non-Goals (this phase)

- OAuth2 client-credentials grant (the existing OAuth server is a worse fit —
  see [Alternatives](#alternatives-considered)).
- Per-key scopes/permissions narrower than the account's role.
- Fine-grained IP allowlists / mTLS for keys.
- Frontend build-out beyond the management surface (covered separately).

---

## How it works today (grounded findings)

### Authentication

- `current_user` (`backend/app/core/auth.py:905`) tries a JWT first, then an
  API key from the `X-API-Key` header (`:923`) or `Authorization: Bearer bow_…`
  (`:932`). Both delegate to `api_key_service.get_user_by_api_key`.
- The JWT path calls `_update_last_seen` (`:919`); **the API-key path does
  not** — programmatic activity is tracked via `ApiKey.last_used_at`
  (`api_key_service.py:150`), not `User.last_seen`.
- `get_user_by_api_key` validates prefix → hash → soft-delete → expiry, then
  returns the `User`. **It never checks `User.is_active`.** This asymmetry is
  the key to the design: `is_active=False` blocks interactive login while
  leaving API-key auth working.
- Org is resolved by `get_current_organization`
  (`backend/app/dependencies.py:66`) from the `X-Organization-Id` header or,
  failing that, from the API key.

### Authorization (RBAC)

- The single chokepoint is `resolve_permissions(user_id, org_id)`
  (`backend/app/core/permission_resolver.py:82`). It reads `RoleAssignment`
  (`principal_type ∈ {user, group}`), `GroupMembership`, and `ResourceGrant`
  (`principal_type ∈ {user, group, role}`). **It does not require a
  `Membership` row.**
- However, the **decorators do**: `@requires_permission`
  (`backend/app/core/permissions_decorator.py:77`),
  `@requires_resource_permission` (`:223`), and `_require_resource_manage`
  (`backend/app/routes/rbac.py:245`) each `SELECT Membership WHERE user_id, org_id`
  and 403 if it's missing — **before** RBAC runs. This is the one structural
  obstacle to a principal that has no `Membership`.
- `assert_full_admin_exists` (`permission_resolver.py:361`) counts only direct
  `principal_type="user"` assignments for lockout prevention — a service account
  with `full_admin_access` would count toward it (usually undesirable; see
  [Open questions](#open-questions)).

### The `user_id` blast radius

There are **~40 FKs to `users.id`**. They split into:

- **Action attribution** (a service account *writes* these): most are already
  `nullable=True` — `completion`, `agent_execution`, `query`, `prompt`, `plan`,
  `llm_usage_record`, `*_usage_event`, `agent_automation_run.requested_by_user_id`.
  Three are **`NOT NULL`**: `report.user_id` (`models/report.py:42`),
  `artifact.user_id` (`models/artifact.py:25`), `file.user_id`
  (`models/file.py:20`).
- **Human config** (rarely touched): `membership`, `*_credentials`,
  `oauth_server`, `report_share`, `report_star`, `git_repository`, `webhook`,
  `entity.owner_id`, `scheduled_prompt`.

Serialization is already defensive — almost every read guards
`x.user.email if x.user else None` / `"Unknown"` (`report_service.py:235`,
`rbac_service.py:271`, `ee/audit/service.py:150`) — so a missing actor renders
"Unknown" rather than crashing.

**Implication:** any design where the service account is *not* a real `users`
row must alter the three `NOT NULL` tables, add polymorphic actor columns to
~10 attribution tables, and update every serializer. A design where it *is*
backed by a `users` row makes all 40 FKs work unchanged.

### Seats / billing

`_enforce_user_limit` (`backend/app/services/organization_service.py:260`)
counts **`Membership` rows** (`_count_org_memberships`, `:246`) against the
license cap. **Any service account that holds a `Membership` burns a paid
seat.** This is decisive for the org-binding choice below.

### Visibility surfaces

If a service account had a `Membership`, it would surface in ~16 places that
query `Membership`/`User`: member lists (`organization_service.py:128, 628`),
group members (`rbac_service.py:252`), data-source members
(`data_source_service.py:3595`), report-share & people pickers
(`report_service.py:210`), seat counts, usage-policy resolution
(`usage_policy_service.py:442`), external-user-mapping lookup
(`external_user_mapping_service.py:213`), and invite emails. Each would need an
`is_service_account` filter.

### Identity creation & sync (login isolation)

`users` rows are created by: local register (`auth.py:659`), OAuth/OIDC callback
(`:460`), LDAP auto-provision (`:119`), SCIM (`ee/scim/service.py:228`), chat
auto-provision (`:761`), invite acceptance (`:205`). LDAP/OIDC group sync and
SCIM **match existing users by email / external-id only** and never create the
service-account row themselves. So a service account with a **non-routable,
unique email outside any allowed signup domain** will never be adopted by SSO,
and `is_active=False` + an unusable password hash blocks the local/JWT/SSO login
paths (`current_user(active=True)` at `auth.py:886`; password verify in
`UserManager.authenticate`, `:53`). `users.email` is DB-unique
(`alembic/versions/21952d7521e4_users_type.py:37`) and `EmailStr`-validated, so
the synthetic address must be RFC-valid and unique.

### Existing precedents

- Chat auto-provision already mints synthetic `users` rows with random
  passwords (`auth.py:816`) — precedent for non-interactive users (though those
  are `is_active=True`).
- An OAuth2 server exists (`models/oauth_server.py`, `bow_oauth_` tokens), but
  its access tokens still pin to a human `user_id` and there is no
  client-credentials grant — see [Alternatives](#alternatives-considered).

---

## Design

### Chosen shape: backed identity + dedicated binding table ("B2")

A `ServiceAccount` is a **first-class managed principal** backed by a hidden
`users` row, bound to its org by a **dedicated table** rather than a
`Membership`.

```
ServiceAccount (org-scoped metadata + management)
   id, organization_id, name, description,
   created_by_user_id, disabled_at, timestamps
        │ 1:1
        ▼
users row (FK plumbing only)
   is_service_account = True
   is_active = False            # blocks JWT/SSO login; API-key path ignores it
   is_verified = True
   is_superuser = False
   email = "svc.<sa_id>@service.invalid"   # unique, non-routable
   name  = "<display name>"     # so attribution reads the name, not "Unknown"
   hashed_password = <random, unusable>
        │ principal_id (principal_type="user")
        ▼
RoleAssignment(org, role_id, principal_type="user", principal_id=<users.id>)
        │
        ▼
ApiKey(user_id=<users.id>, organization_id=org, …)   # existing table, unchanged
```

**Why backed by a `users` row.** It makes all ~40 `user_id` FKs, ownership
checks (`obj.user_id == user.id`), and `resolve_permissions` work with **zero**
migration to those tables. Attribution renders the account's display name.

**Why a dedicated binding table instead of a `Membership`.** Org-binding has to
live somewhere. If it lives on a `Membership`, the account burns a license seat
(`_enforce_user_limit`) and leaks into ~16 member/people surfaces. If it lives
on `ServiceAccount.organization_id`, it is **invisible to those queries by
construction** and consumes no seat. The cost is teaching the three permission
decorators that a service account's org-membership is proven by the
`ServiceAccount` row, not a `Membership` (one centralized change — see below).

This shape is exactly the "A-backed" model: first-class identity/UX on top,
`users`-row plumbing underneath.

### Schema changes

1. **New table `service_accounts`** — `id`, `organization_id` (FK, indexed),
   `name`, `description`, `user_id` (FK → the backing `users` row, unique),
   `created_by_user_id` (FK → users, the human creator), `disabled_at`
   (nullable), standard `BaseSchema` timestamps + soft delete.
2. **`users`** — add `is_service_account Boolean NOT NULL DEFAULT false`
   (indexed). Used purely to (a) exclude from human-facing surfaces and (b)
   branch the decorators.
3. **`api_keys`** — *no change required for auth* (a key with
   `user_id = backing users.id` already resolves correctly). **Optional:** add
   nullable `service_account_id` for cleaner management/listing and to scope
   "keys for this service account" without joining through `users`.

Alembic migration: create `service_accounts`, add `users.is_service_account`
(default false, backfill trivially), optional `api_keys.service_account_id`.
No data migration for the 40 attribution tables.

### Authentication

`get_user_by_api_key` continues to return the (backing) `User` — no change. The
backing row's `is_active=False` is intentionally ignored on the API-key path, so
keys keep working while interactive login is blocked. Optionally, reject keys
whose service account is `disabled_at IS NOT NULL` (a cheap join, or denormalize
a flag) so disabling an account instantly kills all its keys.

### Authorization & the decorator change

`resolve_permissions` already works for a `principal_type="user"` assignment with
no `Membership`. The only change is the **mandatory `Membership` check** in three
places — `permissions_decorator.py:77`, `:223`, and `rbac.py:245`. Replace the
bare "`Membership` must exist" with:

> the principal belongs to the org if **either** a `Membership` row exists
> (humans) **or** the user is a service account whose `ServiceAccount.organization_id`
> matches and is not disabled.

A small helper — `principal_belongs_to_org(db, user, org)` — centralizes this so
the three call sites stay in lockstep. The `is_verified` gate
(`permissions_decorator.py:58`) is satisfied because service accounts are created
`is_verified=True`.

### Who can create one, and the escalation guard

**Creation** is gated by a new org-level permission `manage_service_accounts`,
added to `backend/app/core/permissions_registry.py` (its own category). It is
covered automatically by `full_admin_access` and is **not** added to
`DEFAULT_MEMBER_PERMISSIONS` — so only admins by default, delegable via a custom
role.

**Role cap on creation.** The role assigned to a new service account must be a
subset of the creator's own effective permissions (intersect with
`resolve_permissions(creator)`), and minting a `full_admin_access` service
account warrants an explicit confirmation / separate guard.

**Self-escalation guard (critical).** A request *authenticated as* a service
account must be blocked from the privilege surface regardless of its role. The
agent analysis enumerated the exact endpoints to deny:

- Role CRUD & assignment — `rbac.py` `POST/PUT/DELETE /roles`,
  `POST/DELETE /role-assignments`, group CRUD & membership (`:57–192`).
- Member management — `organization.py` `POST/PUT/DELETE /members`, import
  (`:25–180`).
- API-key issuance — `api_key.py` `POST/DELETE /api_keys` (a leaked key must not
  mint more keys).
- Service-account management — the new endpoints themselves (no self-replication).

Cleanest enforcement: a `forbid_service_account_principal` dependency applied to
those routers (detect `current_user.is_service_account`), so the deny is
declarative and independent of the account's RBAC role. Resource-grant creation
(`_require_resource_manage`) may stay *allowed* if desired, since it is already
gated to resources the principal manages.

### Ownership semantics

Objects a service account creates are owned by its backing `users.id`;
`owner_only` checks (`permissions_decorator.py:101`) work unchanged. Reports /
artifacts / files (`NOT NULL user_id`) are satisfied by the backing row. On
deletion of a service account we should **soft-disable** (set `disabled_at`,
`is_active` already false) rather than hard-delete, to preserve attribution and
avoid FK orphans; hard delete would cascade per `User.*` relationships.

### Human-surface filters (small, since B2 makes most moot)

With B2, member lists and seat counts are already clean. Remaining belt-and-
suspenders filters (`WHERE NOT users.is_service_account`) to apply for polish:

- People pickers that query `User` directly (report-share candidates, group-add
  validation, mentions) so a service account can't be picked as a human.
- `external_user_mapping_service.find_user_by_email` (`:213`) — exclude.
- Notification/email senders — never email a service account; the synthetic
  non-routable address is a backstop.

### Audit & attribution

`audit_log.user_id` (`ee/audit/models.py:22`) already accepts the backing user
id and renders `log.user.email` (`ee/audit/service.py:150`). Two polish items:
attribute service-account actions with the display name (not email), and
optionally tag `details.principal_type="service_account"` so the audit UI can
badge machine actions.

---

## Alternatives considered

- **A-pure (no backing `users` row).** Honors "not a user" literally but forces
  altering the 3 `NOT NULL` attribution tables, adding polymorphic
  `actor_type`/`service_account_id` columns to ~10 attribution tables, teaching
  `resolve_permissions` a new principal type, and updating every serializer.
  Large, invasive, touches hot paths. Rejected for cost/risk.
- **B1 (backed row + `Membership` for org binding).** Decorators work
  unchanged, but every service account burns a license seat and leaks into ~16
  member/people surfaces, each needing an `is_service_account` filter — a
  rot-prone tax (miss one → leak or seat burn). Rejected in favor of B2's
  fewer, centralized changes.
- **OAuth2 client-credentials via the existing OAuth server.** `OAuthClient` is
  already org-scoped with a hashed secret, but `OAuthAccessToken.user_id` still
  pins to a human and there is no client-credentials grant
  (`services/oauth_server_service.py`). Building one is more surface than
  extending API keys, and MCP/CI consumers already speak the `bow_` key format.
  Rejected for now; revisit if standards-compliant client-credentials is needed.

## Change inventory (for implementation)

| Area | File(s) | Change |
|---|---|---|
| Model | new `models/service_account.py`; `models/user.py` | new table; `is_service_account` flag |
| Model (opt.) | `models/api_key.py` | nullable `service_account_id` |
| Migration | `alembic/versions/…` | create table, add column(s), backfill default |
| Auth | `core/auth.py`, `api_key_service.py` | optional: reject keys of disabled accounts |
| Decorators | `core/permissions_decorator.py:77,223`; `routes/rbac.py:245` | org-binding via `principal_belongs_to_org` |
| Permissions | `core/permissions_registry.py` | add `manage_service_accounts` |
| Escalation guard | `routes/rbac.py`, `routes/organization.py`, `routes/api_key.py`, new SA routes | `forbid_service_account_principal` dependency |
| Service + routes | new `services/service_account_service.py`, `routes/service_account.py` | CRUD, key issuance, role assignment (capped) |
| Filters (polish) | report-share / mentions / `external_user_mapping_service.py:213` | exclude `is_service_account` |
| Audit (polish) | `ee/audit/service.py` | attribute by name; tag principal_type |

## Open questions

1. **Full-admin service accounts** — allow at all? If so, should they count
   toward `assert_full_admin_exists` lockout prevention (currently they would)?
   Recommendation: exclude service accounts from the lockout count and gate
   `full_admin_access` SAs behind an extra confirmation.
2. **Disable vs. delete** — confirm soft-disable (`disabled_at`) as the default,
   with hard delete reserved and blocked while the account owns objects.
3. **`api_keys.service_account_id`** — add for clarity, or derive via the
   backing `users.is_service_account`? (Leaning: add it; cheap and explicit.)
4. **Key issuance UX** — keys minted only through the service-account management
   API by a `manage_service_accounts` holder (not self-service).
