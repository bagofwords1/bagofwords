# Audit-trail coverage — gaps & remediation

Status: **in progress** (this PR closes the high-priority gaps; lower-priority
items are tracked below). Branch: `claude/audit-trail-coverage-a5d2j2`.

The audit subsystem (`app/ee/audit/`) is solid — append-only `audit_logs`,
org-scoped, SIEM-pollable via API key, license-gated *viewing* but
*always-on writing*. The problem is **coverage drift**: recent feature work
(prompts, scheduled prompts, RBAC, webhooks, OAuth clients, usage policies,
external user mapping) shipped without wiring in audit calls. This doc
inventories the gaps and records what this change fixes.

---

## How auditing works (the contract)

Two entry points, same `audit_logs` table:

- **HTTP routes / services** — `audit_service.log(db, organization_id, action,
  user_id, resource_type, resource_id, details, request, commit=True)`
  (`app/ee/audit/service.py`). Pass `request` to capture IP + user-agent.
- **AI tools** — `log_tool_audit(runtime_ctx, action, resource_type,
  resource_id, details)` (`app/ee/audit/tool_audit.py`), non-blocking queue.

Conventions, established by the existing call sites:

- `action` is `"<resource>.<verb>"`, dot-separated, e.g. `prompt.created`,
  `role.assigned`. The verb's second segment drives the colour chip in the
  settings UI (`created`/`deleted`/`removed`/`published`/`invited` are special).
- The audit call is **best-effort**: wrap it in `try/except` so an audit
  failure never breaks the request (see `api_key.py`).
- Action types shown in the UI filter are **discovered dynamically** from
  distinct `audit_logs.action` values (`get_action_types`) — there is **no
  static registry to update** when adding a new action.
- Where a route is decorated with `@requires_permission`, adding a
  `request: Request` parameter and a route-level audit call is supported and
  is the established pattern (`report.py`).

Reference implementation: `app/routes/api_key.py`.

---

## Coverage before this change

Well-covered: reports, members/org settings, data-source CRUD, connections
CRUD, API keys, builds, completions, files, instructions (create/update/revert),
LLM providers + model access/toggle/restriction, entities, AI tool calls.

**Not covered** (state-changing, no audit emitted):

| Area | Endpoints | Severity |
|---|---|---|
| Prompts (create/update/delete/run) | `prompt.py` | High — new feature, you flagged it |
| Scheduled prompts (create/update/delete/trigger) | `scheduled_prompt.py` | High — recurring jobs + subscribers |
| RBAC (roles, groups, group members, role-assignments, resource-grants) | `rbac.py` | **Critical** — permission changes |
| Webhooks (create/update/delete/rotate-secret) | `webhook.py` | High — inbound routing + secrets |
| OAuth clients (create/update/delete/rotate-secret) | `oauth_server.py` | High — client secrets |
| Usage policies (create/update/delete/assign) | `usage_limits.py` | High — enterprise controls |
| External user mapping (map/update/delete/verify) | `external_user_mapping.py` | High — account linking |
| Auth signup/registration | `auth_providers.py` | Medium — login is audited, signup isn't |
| Git repos (CRUD + sync/push/publish) | `git.py` | Medium |
| Connection ops (identity switch, tool policy, refresh) | `connection.py` | Medium |
| Data-source members + table activation | `data_source.py` | Medium |
| Instruction delete/bulk/hunks/labels | `instruction.py` | Medium |
| LLM model create/update/delete | `llm.py` | Medium |
| Agent/Eval YAML apply | `agent_yaml.py`, `eval_yaml.py` | Medium |
| Per-user inbox/review state (read/dismiss) | `notification.py`, `review.py` | Low / out of scope |
| User profile (instructions, avatar), onboarding | `user_profile.py`, `onboarding.py` | Low |

Note: entities were initially miscounted as a gap — `entity_service.py` already
audits create/update/delete.

---

## What this change implements (high-priority tier)

Route-level `audit_service.log(...)` (with `request` for IP/UA) added for:

### Prompts — `app/routes/prompt.py`
| Action | Resource | Trigger |
|---|---|---|
| `prompt.created` | `prompt` | `POST /prompts` |
| `prompt.updated` | `prompt` | `PUT /prompts/{id}` |
| `prompt.deleted` | `prompt` | `DELETE /prompts/{id}` |
| `prompt.run` | `prompt` | `POST /prompts/{id}/run` (details: `report_id`) |

(`prompt.run_for` already existed in `prompt_service.py`.)

### Scheduled prompts — `app/routes/scheduled_prompt.py`
`scheduled_prompt.created` / `.updated` / `.deleted` / `.triggered`
(resource `scheduled_prompt`, details include `report_id`, `cron` where known).

### RBAC — `app/routes/rbac.py`
`role.created` / `.updated` / `.deleted`,
`group.created` / `.updated` / `.deleted`,
`group.member_added` / `.member_removed`,
`role.assigned` / `.assignment_revoked`,
`resource_grant.created` / `.updated` / `.deleted`.

### Webhooks — `app/routes/webhook.py`
`webhook.created` / `.updated` / `.deleted` / `.secret_rotated`.

### OAuth clients — `app/routes/oauth_server.py`
`oauth_client.created` / `.updated` / `.deleted` / `.secret_rotated`.

### Usage policies — `app/routes/usage_limits.py`
`usage_policy.created` / `.updated` / `.deleted` / `.assigned`.

### External user mapping — `app/routes/external_user_mapping.py`
`external_user_mapping.created` / `.updated` / `.deleted` / `.verification_requested` / `.verified`.

---

## Deferred (follow-up, lower risk)

Git repos, connection identity/tool/refresh ops, data-source members + table
activation, instruction delete/bulk/hunk/label ops, LLM model CRUD, agent/eval
YAML apply, auth signup. These mostly reconfigure already-audited resources or
are low-sensitivity; they should be added in a follow-up pass following the same
pattern. Per-user inbox/review read-state and onboarding are intentionally out
of scope (per-user UI state, not org-audit material) — though `review.resolve`
(spawns eval/training) is a reasonable future addition.

---

## Verification

Manual, against a running sandbox (uvicorn + SQLite `db/app.db`), driving real
HTTP requests with curl and inspecting `audit_logs` directly — not automated
tests.

**Confirmed landing in `audit_logs`** (each with `user_id`, `ip_address`, and a
`details` payload):

- `prompt.created`, `prompt.updated`, `prompt.deleted`
- `scheduled_prompt.created`, `scheduled_prompt.updated`, `scheduled_prompt.deleted`
- `webhook.created`, `webhook.updated`, `webhook.secret_rotated`, `webhook.deleted`
- `oauth_client.created`, `oauth_client.updated`, `oauth_client.secret_rotated`, `oauth_client.deleted`
- `role.assigned`, `role.assignment_revoked`
- `resource_grant.created`, `resource_grant.updated`, `resource_grant.deleted`

**Not exercisable in this sandbox** (code wired identically to the above; could
not be driven E2E for environment reasons, not code reasons):

- `prompt.run`, `scheduled_prompt.triggered` — require a default LLM model
  configured (the underlying run returns 400; audit correctly fires only on
  success).
- `role.created/updated/deleted`, `group.*`, `usage_policy.*` — behind
  `@require_enterprise`; need a signed license to reach the handler. They live in
  the same files (`rbac.py`, `usage_limits.py`) as the verified ungated actions
  and use the identical pattern.
- `external_user_mapping.*` — needs a real Slack/Teams platform integration
  (token validated against the live provider API).

Note: the audit **viewing** UI (settings → audit) is itself `@require_enterprise`
gated, so in an unlicensed sandbox it renders the upsell rather than the rows —
DB inspection is the authoritative check here.
