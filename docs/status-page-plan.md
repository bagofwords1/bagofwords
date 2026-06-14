# Status Page — Implementation Plan

## Goal

A status/health page that surfaces the connectivity and health of the
platform's moving parts:

- **LLM integrations** — per provider
- **Data connections** — per connection, **scoped to user permissions**, and
  correctly handling `system_only` vs `user_required` auth
- **Internal systems** — database, scheduler, background workers, websockets,
  storage, encryption key, migrations
- **Auth & SSO** — SMTP, Google OAuth / OIDC
- **Provisioning (EE)** — LDAP, SCIM
- **Messaging / outbound integrations** — Slack / Teams / WhatsApp / inbound
  email, webhooks

### Audience

- **Backend:** per-user, permission-aware status resolution.
- **Frontend:** a full status page for admins (`manage_settings` or
  `full_admin`); a small per-user status **badge** for everyone.

## Core principle: observe, don't probe

Active health probing is expensive and dangerous here:

- LLM `test_connection` streams a real completion → **burns tokens**.
- Connecting to serverless warehouses (Snowflake, BigQuery, Redshift
  Serverless, Databricks) can **auto-resume compute and bill the customer**
  just for a health check.

So health is **derived from real traffic the system already does** plus
**already-cached status fields**, never from synthetic checks. The only thing
that ever costs tokens or wakes customer compute is an explicit, admin-clicked
**"Test"** button.

| Component | How health is determined | Cost |
|---|---|---|
| LLM providers | Passive: outcome of real completions (last success/fail/latency/error per provider) | Free |
| Data connections (`system_only`) | Read cached `Connection.last_connection_status` / `last_connection_checked_at` (updated by real queries + reindex) | Free |
| Data connections (`user_required`) | Per-user: derived from `UserConnectionCredentials` (`is_active`, `last_used_at`, `expires_at`, `metadata_json` error) | Free |
| App database | `SELECT 1` on our own DB + pool stats | Free |
| Scheduler / jobs / workers | Read scheduler state, `scheduled_job_runs`, leader lock | Free |
| SMTP | TCP connect + auth/NOOP (no test email) | Cheap |
| SSO (OIDC/Google) | `GET issuer/.well-known/openid-configuration` discovery probe + login success/fail ratio from audit log | Cheap |
| LDAP (EE) | Existing `test_connection()` + last sync result | Cheap |
| SCIM (EE) | Config-derived: token active / expiring-soon / stale (`last_used_at`) | Free |
| External platforms | Cached `is_active` + last delivery; manual Test to re-verify | Free |
| Webhooks | Rollup of `last_delivery_at` across active webhooks | Free |
| Fernet key / migrations | Key load check; Alembic head vs current | Free |

Staleness rule: when there's no recent signal (older than ~2× the expected
cadence), status is **`unknown`** ("last seen X ago") — never a false `down`,
and never a trigger to probe.

## Data model

New table `system_health_check`, upserted by the background job and by passive
signal hooks. One row per component, keyed across three optional dimensions:

| Column | Notes |
|---|---|
| `id` | PK |
| `component_type` | `database`, `scheduler`, `worker`, `websocket`, `storage`, `encryption`, `migrations`, `llm_provider`, `connection`, `smtp`, `sso`, `ldap`, `scim`, `external_platform`, `webhook` |
| `component_id` | nullable FK-ish id (provider id, connection id, platform id…) |
| `organization_id` | **nullable** — `NULL` = instance-wide (DB, scheduler, SSO, LDAP, global SMTP) |
| `user_id` | **nullable** — set only for per-user signals (`user_required` connections); `NULL` = shared/system |
| `status` | `ok` / `degraded` / `down` / `unknown` (+ `action_required` for "user must connect") |
| `latency_ms` | nullable |
| `message` | short human string |
| `details` | JSON (counts, error, sub-checks) |
| `checked_at` | timestamp of the signal/check |

Uniqueness: `(component_type, component_id, organization_id, user_id)` upsert
key. Current-state only (no uptime history in v1).

### Why the three nullable dimensions

- `organization_id = NULL` → instance/global component (shared infra, global
  config like SSO/LDAP/global SMTP).
- `user_id = NULL` → shared status (system_only connections, LLM providers).
- `user_id` set → that user's own credential health for a `user_required`
  connection.

## `user_required` connections (per-user health)

This is the subtle case. A `Connection` with `auth_policy = "user_required"`
has **no shared credential** — each user supplies their own via
`UserConnectionCredentials`. So a single org-wide status cannot represent it.

Per-user status is derived **passively** from the user's
`UserConnectionCredentials` row (no probing):

| Situation | Status shown to that user |
|---|---|
| No credentials row | `action_required` — "Connect your account" |
| Row exists, `expires_at` passed | `down` — "Credentials expired" |
| Row exists, `metadata_json` has recent error | `degraded` / `down` |
| Active, recent successful `last_used_at` | `ok` |
| Active but no recent activity | `unknown` ("not used recently") |

Views:

- **Per-user `/status` & badge:** for a `user_required` connection, show
  **that user's own** status (the row keyed by their `user_id`), not a shared
  one.
- **Admin page:** show an **aggregate** for `user_required` connections —
  e.g. "12/15 users connected, 2 failing, 1 not configured" — computed from
  all `UserConnectionCredentials` rows for the connection. Plus the
  `system_only` connections with their single shared status.

Passive signal: the existing per-user query/credential-use path writes the
outcome (success/failure + error) so health reflects real usage.

## Backend

### Passive signal hooks (the only new write path on hot paths)

A lightweight, fire-and-forget `record_health_signal(...)` helper that upserts
a `system_health_check` row. Wired at the **existing real call sites** (exact
locations confirmed during implementation):

- **LLM completions** — record per-provider success/failure/latency/error.
- **Connection usage** — for `system_only`, the existing
  `last_connection_status` update already covers it; for `user_required`, record
  per-user outcome (also reflected in `UserConnectionCredentials.metadata_json`).

These must never block or fail the real request (wrap in try/except, fast).

### Background checker (APScheduler, leader-only)

Registered alongside existing jobs in `main.py`. Runs **only the free/cheap
checks** and upserts results:

- ~1 min: app DB ping + pool, scheduler/leader/overdue jobs, workers,
  websockets.
- ~10 min: SMTP connect+NOOP, OIDC discovery + audit login ratio, LDAP
  connectivity + sync result, SCIM token expiry/staleness, external platform
  `is_active`, webhooks rollup, Fernet key, Alembic head.
- LLM and data connections are **not** actively checked — purely passive +
  manual Test.

Reuses existing leader-election (`/tmp/bow-scheduler.lock`) so only one worker
runs it.

### API (per-user, permission-aware)

- `GET /status` — resolves the caller's permissions via
  `permission_resolver`, returns only the sections they may see:
  - **Data connections** — everyone, scoped to accessible connections; for
    `user_required` shows the caller's own status.
  - **LLM** — `manage_llm` / `full_admin`.
  - **Internal systems** — `manage_connections` / `full_admin`.
  - **Auth / SSO / LDAP / SCIM / SMTP** — `manage_settings` / `full_admin`.
- `GET /status/badge` — cheap worst-status rollup across (instance-wide
  components the user may see) + (org/user-scoped components per their perms).
- `POST /status/recheck/{component}` — admin-only on-demand re-test that
  reuses existing test endpoints (`/llm/test_connection`,
  `/connections/{id}/test`, `/organization/smtp/test`,
  `/enterprise/ldap/test-connection`, …). This is the **only** path that may
  cost tokens / wake compute; UI labels it accordingly.

Permission gating reuses `@requires_permission` and the resolver helpers
(`get_accessible_data_source_ids`, `can_view_all_data_sources`).

> If `manage_settings` is not already a permission in the role catalog, add it.

## Frontend (Nuxt 3)

- **Admin status page** — `pages/monitoring/status.vue`. Component grid grouped
  by section (Internal · LLM · Data connections · Auth & SSO · Provisioning ·
  Messaging). Each card: status dot, message, `checked_at` ("checked 2m ago"),
  details, and a **Test** button where applicable. Gated by
  `manage_settings` / `full_admin`. For `user_required` connections, shows the
  aggregate ("12/15 connected").
- **Per-user badge** — small header/layout indicator backed by `/status/badge`;
  green/amber/red with a tooltip listing degraded items the user can see
  (including their own `user_required` connections needing action). Refreshes on
  navigation / light interval.
- EE sections (LDAP, SCIM) are hidden gracefully when EE is disabled.

## Phasing

1. **Backend foundation** — `system_health_check` model + migration, passive
   `record_health_signal` hooks (LLM + user_required connections), background
   checker job, `GET /status` + `GET /status/badge` (permission-scoped).
2. **Admin page** — `pages/monitoring/status.vue` + components, including
   `user_required` aggregates and `POST /status/recheck` Test buttons.
3. **Per-user badge** — header indicator + `/status/badge` wiring.

## Decisions captured

- Audience: backend per-user; frontend admin page + per-user badge.
- Evaluation: background periodic checks for free/cheap components; passive for
  LLM and data connections.
- LLM health: **passive only** (no synthetic tokens).
- Data-connection checks: **all passive** (never auto-wake warehouses).
- Auth/SSO/LDAP/SCIM visibility: `manage_settings` / `full_admin`.
- SSO health: OIDC discovery probe + audit login success/fail ratio.

## Assumed defaults (override if needed)

- **Current-state only**, no uptime history in v1.
- **Staleness = older than ~2× expected cadence → `unknown`.**
- Add `manage_settings` permission if it does not already exist.
