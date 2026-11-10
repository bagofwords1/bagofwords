# Audit event contract, content policy and operator controls

Status: implemented (PR #1595). Builds on `audit-log-streams.md` (delivery)
and `audit-trail-coverage.md` (which actions exist). This document is the
contract every producer, the table, the envelope and a receiving collector
share, plus the controls that are the deployment operator's rather than the
application's. The verification that motivated it is recorded in
`docs/feedback-loops/2026-11-04-audit-collector-verification.md`.

## 1. Event contract

One row in `audit_logs` is one event. Columns in **bold** were added by the
`auditcontract01` migration; every one is nullable, so rows written before it
stay valid and are exported as *legacy* records.

| Field | Column | Meaning |
|---|---|---|
| Event id | `id` | Stable UUID assigned when the event is built (before any queue, spill or retry). Collectors deduplicate on it. |
| Schema version | **`schema_version`** | `2` for rows written under this contract; `NULL` (exported as `1`) for legacy rows. |
| Occurrence time | **`occurred_at`** | When the action happened, UTC. Fixed when the event is built; survives queueing, spill and replay. |
| Recording time | `created_at` | When the row was written, UTC. Equal to `occurred_at` for a direct write; later for a queued, spilled or replayed one. |
| Action, category | `action` (+ derived) | `resource.verb`; the category (`authentication`, `authorization`, `administration`, `data_access`, `agent`, `background`, `platform`) is derived from the prefix in `app/ee/audit/contract.py` and exported. |
| Outcome | **`outcome`** | `success`, `failure` or `denied`. Legacy rows get the inferred outcome (`access.denied` → denied, `*_failed` → failure, else success); new producers state it. |
| Failure reason | **`failure_reason`** | Short safe code (`invalid_credentials`, `missing_permission`, `execution_failure`, …). Never free text from the client. |
| Actor | `user_id`, **`actor_type`**, **`actor_snapshot`** | Live reference plus identity at event time (`{"email","name"}`). `actor_type` is `user`, `agent` (an AI run acting for a user), `service`, `system` (jobs) or `unknown` (pre-authentication: nobody is fabricated). |
| Target | `resource_type`, `resource_id`, `details.title` | The resource acted on, when there is one. |
| Source context | `ip_address`, `user_agent` | Allowlisted request context, taken from the ambient audit context (section 3). |
| Correlation | **`request_id`**, **`session_id`**, `details.agent_execution_id`, `details.parent_request_id` | Opaque ids (section 3). |
| Evidence | `details` | Structured JSON that went through the content policy (section 2). |
| Export bookkeeping | `export_seq` | Visibility-ordered sequence stamped by the exporter. Mutable bookkeeping on an otherwise immutable row; see section 6. |

### Required fields by category

Checked by the health endpoint (`records_missing_outcome`), not enforced at
write time: a producer with partial facts records them honestly and leaves
the rest null.

| Category | Required |
|---|---|
| authentication | outcome, ip_address, auth_method (`details.auth_method`) |
| authorization | outcome, actor, permission (`details.permission`) |
| administration, data_access | outcome, actor, target |
| agent | outcome, actor, execution id |
| background, platform | outcome |

### Envelope (streams and the export endpoint)

`app/ee/audit/streams/envelope.py`. The envelope stays `version: 1` and is
**additive**: every v1 key keeps its place and meaning, so existing parsers
(the destination tests, the mock SIEM, the Splunk `time` mapping) continue to
work. Added keys:

```
schema_version  1 | 2            legacy    true | false
recorded_at     when written     occurred_at  now means when it happened
outcome, failure_reason, category
actor.name                       (actor.email/name come from the snapshot first)
correlation {request_id, session_id, parent_id, execution_id}
```

Consumer upgrade path: a v1 consumer keeps working unchanged; to use the new
facts, read the new keys and treat `legacy: true` records as "outcome
inferred, times equal, no correlation". Nothing is backfilled: the original
time, outcome and identity of a legacy row are not known and are not
invented.

## 2. Content policy

`app/ee/audit/content_policy.py`, applied once, before the row, the spill
file, the export and any diagnostic log of an audit failure.

- **Secrets never pass.** Keys that name reusable authentication material
  (`password`, `token`, `api_key`, `authorization`, `cookie`, `secret`,
  `hmac`, `private_key`, `credentials`, …) are replaced by `[REDACTED]` at any
  depth; values shaped like a JWT, a bearer string, a Fernet token, an AWS
  key id, a PEM block or a provider key are redacted whatever their key.
- **Request context is an allowlist**: client address (section 3), user
  agent, request id, session id. Headers and bodies are never copied.
- **Payloads are bounded, never silently cut.** `details` beyond
  `audit.max_details_bytes` (default 64 KiB) becomes a marker with the
  dropped keys and original size. Executed queries are kept in full count;
  past `audit.max_query_payload_bytes` (default 32 KiB) the longest are
  shortened from the tail and `executed_queries_truncation` says which, with
  original lengths.
- **No result sets.** Query evidence is the statement and the row count;
  rows are never logged.
- **Personal data is referenced, not duplicated**: the actor snapshot holds
  email and display name only; everything else is a resource reference.

Query literals are therefore stored as executed. Deployments that must not
retain literals narrow `max_query_payload_bytes` or filter the action at the
stream; an encrypted-payload mode is a documented follow-up, not shipped.

## 3. Correlation and source context

`app/ee/audit/context.py`. An HTTP middleware binds an `AuditContext` per
request in a contextvar; jobs and agent runs bind one with
`audit_context.scope(audit_context.for_job(...))`. Every producer reads it, so
services, tools and the permission decorator record context without being
handed the request.

- **Request id**: `X-Request-ID` from the client when it matches
  `[A-Za-z0-9._:-]{8,128}`, else minted. Echoed back as `X-Request-ID`. Safe
  to return to clients and to propagate.
- **Session id**: the random `sid` claim minted into the JWT at login
  (`SessionEpochJWTStrategy.write_token`). Independent of the credential:
  it is not derived from the token, cannot be used to authenticate, and is
  safe to hand to a collector. API keys and service accounts have none.
- **Client address**: `X-Forwarded-For` is honoured only when the TCP peer is
  in `security.trusted_proxies` (`BOW_TRUSTED_PROXIES`, CIDR list). Unset,
  the trusted set is loopback plus RFC 1918 / ULA ranges, which covers a
  reverse proxy on the same host or Docker network; on a shared network set
  it to the proxy addresses; `[]` records the TCP peer only. The hops are
  walked from the right and the first untrusted one is the client. Uvicorn's
  own `--proxy-headers` / `FORWARDED_ALLOW_IPS` rewrites the peer first and
  composes with this.
- **Parent and execution ids**: a job carries the originating request id as
  `parent_id`; agent events carry `agent_execution_id`.

## 4. Security events and pre-authentication routing

`app/ee/audit/security_events.py` is the single entry point for every login
provider and session hook.

| Scenario | Action | Outcome / reason | Where recorded |
|---|---|---|---|
| Password or LDAP sign-in | `auth.login` | success | every organization the account belongs to |
| Wrong password, known account | `auth.login_failed` | failure / `invalid_credentials` | the account's organizations |
| Disabled account, local login not allowed | `auth.login_failed` | failure / `account_disabled`, `local_login_not_allowed` | the account's organizations |
| Unknown account | `auth.login_failed` | failure / `invalid_credentials`, `actor_type: unknown` | **no organization** |
| OAuth / OIDC / SAML | `auth.login`, `auth.login_failed` | provider reason | as above (known account → its organizations) |
| Sign-out | `auth.logout` | success | the account's organizations |
| Password change / reset / reset requested | `auth.password_changed`, `auth.password_reset`, `auth.password_reset_requested` | success | the account's organizations |
| Permission denied | `access.denied` | denied / `not_a_member`, `missing_permission`, `not_owner`, `source_access` | the organization, with ip, user agent, request id, attempted resource |

Routing rule: membership is a justified association, so an event about a
known account is recorded once per organization it belongs to. An event
about an unknown account has none and is recorded with
`organization_id = NULL`. Such rows are **never** stamped for an
organization stream, never listed by the org API, and reach a collector only
through the **platform stream** configured in bow-config:

```yaml
audit:
  platform_stream:
    enabled: true
    destination: splunk          # any stream destination type
    config: { hec_url: https://collector.example.com/splunk }
    secrets: { token: ${AUDIT_PLATFORM_HEC_TOKEN} }
```

The platform stream is one `audit_log_streams` row with a NULL organization,
synced from config at startup (`app/ee/audit/maintenance.sync_platform_stream`),
delivered by the same exporter with the same lease, cursor, backoff and
retry semantics, and invisible to the organization-scoped API. Nothing is
routed from untrusted login input.

## 5. Data-access evidence

Every event records `details.observation`, the strongest fact the server has:

| Path | Action | Observation |
|---|---|---|
| `GET /api/reports/{id}` | `report.viewed` | `served` (the response was produced; client display is not confirmed by a fetch) |
| `GET /api/r/{id}` (published) | `report.public_viewed` | `served`, `access_scope: anonymous | member` |
| `POST /api/queries/{id}/run`, `/preview` | `query.executed` | `executed`, mode `builder | viewer | preview`, the code that ran, `row_count`, outcome and reason on failure |
| `GET /api/data_sources/{id}/schema` | `data_source.schema_viewed` | `served`, table count |
| `GET /api/files/{id}/content` | `file.downloaded` | existing; a cached read is not distinguishable server-side |
| `GET /api/enterprise/audit/export` | `audit_log.exported` | existing: format, row count, filters |
| Agent tools | `tool.data_queried`, `tool.data_query_failed` (outcome failure) | every executed query under the content policy, `row_count` |
| `POST /api/mcp` `tools/call` | `tool.mcp_server_call` | one per call: `details.tool` (`mcp.<name>`), `transport` (`json` / `sse`), the OAuth app (`origin_app_type`, `origin_app_id`) when it came through one, the report when the result names one; outcome `success`, `failure` (`error_type` when it raised or the client disconnected) or `denied` (`permission_denied`). Never the arguments or the result. Queued in the background (`queue_tool_audit`), so the call never waits on it |
| MCP `execute_mcp`, `describe_entity` (saved-query run) | `tool.mcp_executed`; `tool.data_queried` / `tool.data_query_failed` | the same events as the in-app tools, with `details.tool` `mcp.execute_mcp` / `mcp.describe_entity` |

`report.published` / `report.unpublished` is recorded once, by the service,
with request context from the ambient audit context.

## 6. Historical identity, immutability and lifecycle

- **Identity at event time**: `actor_snapshot` is captured on write; the
  envelope uses it first and the live join only for legacy rows. A renamed
  user's old events keep the old email; a deleted user's events keep
  `actor_type: user` and their snapshot while `user_id` becomes NULL.
- **Organization deletion** cascades to its audit rows (database FK). An
  organization that must keep evidence past teardown exports it first (log
  stream or `/export`) or is retained by the operator's backup policy. The
  platform stream's rows are not affected.
- **Immutability**: application code only inserts rows; `export_seq` is the
  single mutable column (exporter bookkeeping). Moving it to a separate table
  is a documented follow-up; until then the operator's database permissions
  (section 8) are what prevent updates to event content.
- **Retention**: `audit.retention_days` (`BOW_AUDIT_RETENTION_DAYS`) purges
  rows older than the window once daily, **only** when every non-deleted
  stream of the organization has delivered them (`export_seq` ≤ the slowest
  cursor; unstamped rows are never purged). `audit.legal_hold`
  (`BOW_AUDIT_LEGAL_HOLD=true`) suspends purging. Each purge records
  `audit.retention_purged` with the count.
- **Spill buffer**: `BOW_AUDIT_SPILL_DIR` is an *outage* buffer (JSONL,
  replayed after the next successful write, by a 60 s job and at startup),
  not a guaranteed local history. Size it for the longest database outage
  the deployment must survive at peak event rate; its content is
  policy-clean but unencrypted, so it belongs on protected local storage.

## 7. Health and alerting

- `GET /api/enterprise/audit/health` (view_audit_logs): queue depth,
  failed writes, recording failures (primary writes that fell back to the
  queue), spilled / replayed / replay errors / dropped, spill backlog on disk,
  unstamped rows, records missing a required field, legacy record count,
  per-stream pending, lag, oldest pending age, consecutive failures,
  degraded-since and next attempt, last heartbeat time.
- **Heartbeat**: every `audit.heartbeat_interval_seconds` (default 300, 0
  disables) an `audit.heartbeat` event is written for each organization with
  an active stream. A collector that stops receiving heartbeats while the
  health endpoint shows them written has a delivery problem; one that shows
  none written has a stopped producer. Silence alone is no longer ambiguous.
- **Degraded**: after `audit.degraded_after_failures` consecutive retryable
  failures (default 5) a stream records `audit_stream.degraded` and e-mails
  the organization's admins once; it keeps retrying. The next success
  records `audit_stream.recovered`. Invalid credentials still stop the stream
  (`audit_stream.state_changed`, state `invalid`). The e-mail result is
  checked and logged; an undelivered alert is an ERROR log line.
- **Repeated-denial detection** is the collector's job: every
  `access.denied` carries actor, ip, permission, reason and request id, which
  is what a SIEM rule needs (for example: more than N `outcome=denied` from
  one `actor.id` or `context.ip_address` in M minutes). The application does
  not rate or score denials.

## 8. Operator responsibility matrix

| Control | Application | Operator |
|---|---|---|
| Event capture, content policy, durability (queue, retry, spill, replay) | ✅ | — |
| Delivery (at-least-once, no gaps, dedupe id), backoff, degraded/invalid alerts | ✅ | collector-side dedupe on `id` |
| Protected storage for the database and `BOW_AUDIT_SPILL_DIR` (permissions, encryption at rest) | — | ✅ |
| Database roles: the app writes; readers/exporters read; nobody updates event content | — | ✅ |
| Backup and restore of `audit_logs`, `audit_log_streams`, `audit_export_state`; restore drills | — | ✅ |
| Clock synchronization (event times are host UTC) | — | ✅ |
| TLS to collectors (`verify_tls`), certificate rotation, collector credentials | per-stream config | ✅ |
| Encryption key (`BOW_ENCRYPTION_KEY`) custody and recovery (stream secrets are Fernet-encrypted with it) | — | ✅ |
| Trusted proxy list (`BOW_TRUSTED_PROXIES`) and Uvicorn `FORWARDED_ALLOW_IPS` | defaults | ✅ |
| Retention window, legal hold, purge review | job | ✅ decides and reviews |
| Periodic review of `audit.heartbeat`, `audit_stream.degraded`, health endpoint | signals | ✅ |

The application does not, by itself, establish regulatory compliance; it
produces the evidence and the signals the operator's process relies on.

## 9. Coverage matrix

Trigger → actor → target → outcome → context → persistence → collector route.
"ctx" means ip, user agent, request id and session id from the ambient
context.

| Trigger | Action | Actor | Target | Outcome | Context | Persistence | Route |
|---|---|---|---|---|---|---|---|
| Local sign-in | `auth.login` | user (snapshot) | user | success | ctx + auth_method | direct, fallback queue | org streams |
| Failed sign-in, known / unknown | `auth.login_failed` | user / unknown | user / — | failure + reason | ctx | direct, fallback queue | org streams / platform |
| Provider sign-in (OAuth, OIDC, SAML) | `auth.login`, `auth.login_failed` | user / unknown | user | success / failure | ctx + provider | direct | org streams / platform |
| Sign-out, password change, reset, reset requested | `auth.*` | user | user | success | ctx | direct | org streams |
| Permission denied (any decorator path) | `access.denied` | user / service | permission + attempted resource | denied + reason | ctx | direct | org streams |
| Report, public report, query, schema reads | section 5 | user / anonymous | resource | success / failure | ctx | direct | org streams |
| File upload / download, exports, API keys, members, roles, streams, LLM providers, instructions, builds | existing actions | user | resource | success | ctx | direct | org streams |
| Agent tools (queries, files, artifacts) | `tool.*` | agent (user snapshot + execution id) | report / data source | success / failure | ctx of the originating request when bound | queue, spill, replay | org streams |
| Scheduled and background jobs | `scheduled*`, `audit.heartbeat`, `audit.retention_purged`, `audit_stream.*` | system | job / stream | success / failure | job context | direct | org streams |
| Stream lifecycle | `audit_stream.created/updated/paused/activated/deleted/test/state_changed/degraded/recovered` | user / system | stream | — | ctx | direct | org streams |

Verification per row is in the feedback-loop record and in
`backend/tests/e2e/audit/test_audit_event_contract.py`.

## 10. Configuration summary

```yaml
security:
  trusted_proxies: ["10.0.0.0/8"]        # BOW_TRUSTED_PROXIES; null = loopback + private ranges; [] = peer only
audit:
  max_details_bytes: 65536               # BOW_AUDIT_MAX_DETAILS_BYTES
  max_query_payload_bytes: 32768         # BOW_AUDIT_MAX_QUERY_PAYLOAD_BYTES
  heartbeat_interval_seconds: 300        # BOW_AUDIT_HEARTBEAT_INTERVAL_SECONDS (0 = off)
  degraded_after_failures: 5             # BOW_AUDIT_DEGRADED_AFTER_FAILURES
  retention_days: null                   # BOW_AUDIT_RETENTION_DAYS (null = keep)
  legal_hold: false                      # BOW_AUDIT_LEGAL_HOLD
  platform_stream: { enabled: false, destination: https, config: {}, secrets: {} }
```

## 11. Rollout and backout

Rollout: run migrations (additive), set `BOW_TRUSTED_PROXIES` when the proxy
is not on a private network, optionally enable the platform stream, point
collector dashboards at `outcome`, `occurred_at` and `correlation`.
Backout: the migration's downgrade drops the new columns and the platform
stream row; envelopes revert to legacy shape. Events recorded in between keep
their ids, so a collector that deduplicates on `id` sees no duplicates.

## 12. Known limits and follow-ups

- Export bookkeeping (`export_seq`) still lives on the event row.
- Query literals are stored as executed; no encrypted-payload mode yet.
- Organization deletion cascades to evidence; retention past teardown is an
  export or backup concern.
- A cached file read is not distinguishable from a fresh one server-side.
- LDAP bind failures surface as `invalid_credentials` with method `ldap`.
