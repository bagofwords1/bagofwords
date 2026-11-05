# Feedback loop — audit logging plan verified at a real collector

Scope: the "Starting evidence and implementation map" of the audit logging
improvements plan. Every row of that table was reproduced (or refuted) against
a running stack whose audit streams deliver to an **independent collector**
that behaves like a SIEM intake: it authenticates every delivery, verifies the
HMAC signature, validates each envelope, deduplicates by envelope id, measures
ingest latency, and injects faults on command. Nothing in this record relies
on the application's own mock consumer or on unit tests.

Status: verification only. No application code was changed.

## Verdict per plan row

| Plan row | Verdict | Evidence (section) |
|---|---|---|
| Event contract — outcome and correlation not required | **Confirmed.** 0 of 23,101 envelopes carry an outcome; 0 carry a request or session correlation. A failed agent run exports `completion.created` and nothing that says it failed. | C1 |
| Authentication — events without organization are never exported | **Confirmed.** `auth.login` / `auth.login_failed` rows are stored with `organization_id = NULL`, never stamped, absent from the org's audit list, absent from both intakes, and not counted as pending. | C2 |
| Session lifecycle — login/logout/password hooks emit nothing | **Confirmed.** Password login (success and failure), logout, password change, forgot-password and reset-password produced **zero** audit rows. | C2 |
| Authorization — denial helper omits request context | **Confirmed.** All four `access.denied` envelopes: `ip_address: null`, `user_agent: null`, target id `null`; metadata is the permission name and the handler's function name only. A cross-organization attempt (404) emitted nothing. | C3 |
| Tool persistence — queue, retries, spill, replay exist | **Confirmed working.** A write blocked for 135 s (longer than busy_timeout × retries) was spilled to disk by the producer process and replayed by the *backend's* 60 s job; the event reached both intakes with its original id. | C5 |
| Query evidence — count and length limits | **Confirmed.** 12 executed queries of 503+ characters arrive as **10** queries of exactly 503 characters (500 + `...`), with no marker that two were dropped. | C4 |
| Event timing — queued events export write time | **Confirmed.** A write delayed 8 s exports `occurred_at` = write time; the real time survives only in `metadata.occurred_at`. The replayed spill event exports a time 2 min 19 s after the event. Splunk HEC `time` copies the same wrong value. | C5 |
| Historical identity — joins and deletion change evidence | **Confirmed, both ways.** An email changed after the event but before export is what the collector receives. After the user row is deleted, 7 of their events export as `actor.type = "system"`, `id = null`, `email = null`. | C6 |
| Access coverage — read paths need inventory | **Confirmed gap.** Report read, data-source schema read, query create/run/preview via the API, public (anonymous) report and query reads, and public export produce **no** events. File download does. `report.published` is emitted **twice** per publish, once without request context. | C7 |
| Health — silent producers and persistent lag | **Confirmed.** An idle stream and a dead producer look identical (`pending 0, lag 0`). Two minutes of HTTP 503 kept the stream `active` with growing backoff, no state change and no alert; the only signal is `lag_seconds` on the status API. A 401 moved the stream to `invalid` in 35 s with a `state_changed` event, but the admin e-mail outcome was never logged or checked. | C8 |

Not in the plan table but observed: `X-Forwarded-For` is trusted from any
client (a spoofed `203.0.113.77` was recorded as the actor's IP), the
documentation header of `tool_audit.py` still describes a `created_at` cursor
that the exporter no longer uses, and the secret scan over every delivered
byte was clean (no API key, JWT, HMAC secret, bearer token, HEC token or
password literal).

What held up well: across 23,099 organization rows and two intakes, **every row
arrived at every intake exactly once after dedupe by id**, including through a
`SIGKILL` of the backend mid-batch (the in-flight batch of 500 was redelivered
once per intake, nothing was lost, nothing was left unstamped), a 2-minute
collector outage, an invalid-credential stop and a resume.

## Environment

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL='sqlite:////abs/path/backend/db/audit_loop.db'
export BOW_ENCRYPTION_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
export BOW_AUDIT_STREAM_INTERVAL_SECONDS=5          # export tick, default 15
export BOW_AUDIT_SPILL_DIR=/abs/path/spill          # shared by producer processes and the backend
export BOW_LICENSE_KEY=...                           # enterprise tier (audit_log_streams)
uv run alembic upgrade head && uv run python main.py
uv run python ../tools/agent/seed_org.py --demo --invite member@example.com --db-path db/audit_loop.db
uv run python -I docs/feedback-loops/2026-11-04-audit-collector-verification/collector.py /abs/path/collector-state 8790
```

The collector listens on `127.0.0.1:8790` with two intakes: `/webhook`
(generic HTTPS destination: `Authorization: Bearer collector-bearer-token`,
HMAC secret `collector-hmac-secret`) and `/splunk/services/collector/event`
(Splunk HEC, token `collector-hec-token`). Its state directory holds
`deliveries.jsonl` (one line per HTTP delivery, secrets redacted) and
`events.jsonl` (one line per event with receipt time, intake, dedupe flag,
schema problems and latency). `GET /_control/stats` summarises; `POST
/_control/fault {"mode": "503|429|401|timeout|reset", "count": N}` injects
faults.

Scripts (run from `backend/` with the same environment, in order):
`01_setup.py` (LLM provider + two streams, start_from=beginning) →
`02_agent_chat.py` → `03_security_events.py` → `04_authz.py` →
`05_noorg.py` → `06_tool_evidence.py` → `06c_spill.py` →
`07_access_coverage.py` → `08_identity.py` → `09_health.py` →
`10_restart.py`. `common.py` reads `seed.json` written by `seed_org.py`
(copy it next to the scripts). Set `AUDIT_LOOP_DB` to the sqlite file.

## C1 — Event contract as the collector sees it

Across every unique envelope received (23,101, envelope version 1):

| Field | Observation |
|---|---|
| `outcome` | absent on 100 % of events |
| request / session / operation correlation | absent on 100 % |
| `context.ip_address`, `context.user_agent` | null on 64 % of non-burst events (null on 100 % of events written from services, tools, the exporter and the denial helper) |
| `occurred_at` | always present, UTC `Z`; equals the row's write time, not the event time (see C5) |
| `actor.type` | `user` / `agent` / `system`; `system` is also what a deleted user becomes (C6) |
| target id | null on `access.denied` and `audit_log.exported` |

The failed live agent run (the model API rejected the key for lack of credits)
exported `report.created` and `completion.created` and nothing else: a
consumer cannot tell that the completion failed.

## C2 — Authentication and session lifecycle

`03_security_events.py` drove the real routes with a distinct user agent and
then read the audit table and both intakes:

```
login ok 200 · login wrong password 400 · unknown user 400 · logout 204
password change 200 · forgot-password 202 · reset-password (bad token) 400
DB rows written during the scenario: (none)
Collector received: (none)
```

`05_noorg.py` then recorded an `auth.login_failed` and an `auth.login` the way
`app/services/auth_providers.py` does for OAuth/OIDC, through the same
function, against the live database:

```
organization_id=None  user_id=None  ip=198.51.100.9  export_seq=None   (both rows)
org audit list total for auth.*: 0 · collector auth.* received: {} · stream status pending: 0
```

They exist in the table and nowhere else. The OAuth callback routes could not
be exercised over HTTP because no provider is enabled in the sandbox (404).

## C3 — Authorization denials

A member hit four admin-only routes (403) with `X-Forwarded-For` set. Every
envelope at the collector:

```json
{"actor": {"type": "user", "id": "…", "email": "member@example.com"},
 "context": {"ip_address": null, "user_agent": null},
 "targets": [{"type": "permission", "id": null}],
 "metadata": {"permission": "manage_settings", "endpoint": "create_stream"}}
```

The same member's request against another organization id returned 404 and
emitted no event at all.

## C4 — Query evidence

`06_tool_evidence.py` calls `log_tool_audit` with real User/Organization rows
and `_truncate_queries`, exactly as `create_data.py` does, with 12 queries of
more than 500 characters each. At both intakes:

```
queries delivered=10 of 12; lengths=[503 ×10]; last query ends with '...col_9_28 AS c28, col_9_29 A...'
```

The dropped two and the truncation are not marked anywhere in the envelope
(the `query_count` used above was added by the harness; the tool does not send
it).

## C5 — Timing, spill and replay

| Case | Real time | Envelope `occurred_at` | `metadata.occurred_at` | Collector receipt |
|---|---|---|---|---|
| B: write blocked 8 s by a DB lock | 15:48:34.407 | 15:48:42.411 | 15:48:34.407 | 15:48:56.6 |
| C: write blocked 135 s → spilled → replayed by the backend job | 15:52:07.193 | 15:54:26.658 | 15:52:07.193 | 15:54:26.7 |

Spill evidence from the backend log:

```
WARNING app.ee.audit.tool_audit:_spill:252 - Spilled 1 tool audit events to …/spill/5381-…jsonl
INFO    app.ee.audit.tool_audit:replay_spilled_tool_audit_events:318 - Replayed 1 spilled tool audit events
```

The event id was preserved through the spill (`87b4fc68-…` in the spill file,
in the table and at both intakes). Splunk HEC `time` equalled the envelope
`occurred_at` on all 100 checked HEC events, so a SIEM timeline places both
events at write/replay time. Note: a write that waits inside SQLite's 30 s
`busy_timeout` stamps `created_at` before the commit blocks, so the delay that
`_LATE_WRITE_SECONDS` is meant to annotate is only recorded when the wait
happens before the row is built.

## C6 — Historical identity

`08_identity.py`, with both streams paused between the action and the export:

```
1) email changed after the event, before export -> actor.email = "renamed-member@example.com"
2) user row deleted after the event, before export -> actor = {"type": "system", "id": null, "email": null}
   audit rows for the org: 83 before, 83 after; rows with user_id NULL: 24 → 31
```

Account deletion keeps the rows (`ON DELETE SET NULL`) but turns 7 human
actions into "system" actions. Organization deletion is `ON DELETE CASCADE`
on `audit_logs.organization_id` (source: `backend/app/ee/audit/models.py`),
which would also drop undelivered rows; this was not executed because the
sandbox refuses a second organization (`400 You cannot create more than one
organization`), which also blocked a live cross-organization isolation test.

## C7 — Data-access coverage

Admin requests (status) and what reached the collector:

| Path | Status | Event at collector |
|---|---|---|
| `GET /api/reports`, `GET /api/reports/{id}`, `GET …/completions` | 200 | none |
| `GET /api/data_sources/{id}`, `GET …/schema` | 200 | none |
| `POST /api/queries`, `GET /api/queries/{id}` | 200 | none |
| `POST /api/queries/{id}/run` (builder, executes SQL), `POST …/preview` | 200 | none |
| `POST /api/files` | 200 | `file.uploaded` (ip, ua present) |
| `GET /api/files/{id}/content` ×2 | 200 | `file.downloaded` ×2 (cached read indistinguishable) |
| `POST /api/reports/{id}/publish` | 200 | `report.published` **twice**: one with ip/ua, one with `ip_address: null, user_agent: null` |
| `GET /api/r/{id}`, `GET /api/r/{id}/queries` (anonymous, after publish) | 200 | none |
| `GET /api/r/{id}/export_html` (anonymous) | 409 | none |
| `GET /api/enterprise/audit/export?format=json` | 200 | `audit_log.exported` (`row_count: 72, filters: {}`) |
| `POST /api/api_keys` | 200 | `api_key.created` (name only; the key itself never appears) |

The spoofed `X-Forwarded-For: 203.0.113.77` was recorded as `ip_address` on
every event that has request context; there is no trusted-proxy check.

## C8 — Health, faults and recovery

Stream status while nothing is produced (indistinguishable from a dead
producer):

```
collector-webhook: state=active failures=0 pending=0 lag_seconds=0.0
```

Collector returns 503 to every delivery for 2 minutes while real events keep
being produced (`09_health.py`):

```
t=10s failures=1 next=+5s   · t=20s failures=2 next=+10s · t=30s failures=3 next=+20s
t=50s failures=4 next=+40s  · t=90s failures=5 next=+80s · state stays active · pending 12 · lag 110 s
audit_stream.state_changed rows: 0 · admin notification: none
```

Collector returns 401: within 35 s the HEC stream is `invalid`, a
`audit_stream.state_changed` row is written and exported, but the backend log
shows no "Custom email sent" line and no warning either; `_on_transition`
ignores the e-mail result. After clearing the fault and re-activating, all 12
events created during the outage arrived (12/12), each once per intake.

Restart mid-send (`10_restart.py`): 20,000 rows written in one transaction,
backend `SIGKILL`ed when the collector had 1,000 of them, restarted:

```
db_rows 20000 · webhook unique 20000 (500 ids redelivered, max 2 copies) · hec unique 20000 (500 redelivered)
unstamped rows left 0 · both streams active, cursor 23099 = delivered 23099 · drained 85 s after restart
```

## Limitations of this run

- The live LLM run failed (`400 credit balance is too low`), so agent-mediated
  evidence was produced through the exact helper the tools call
  (`log_tool_audit`, `_truncate_queries`) with real rows, not through a model
  choosing tools. A live run should be repeated once a funded key is available.
- SQLite only. The lock-based scenarios (C5) depend on SQLite's `busy_timeout`;
  PostgreSQL lock behaviour was not exercised.
- Single organization: multi-org isolation could not be exercised live; the
  organization-less path (C2) is the only isolation evidence here.
- No frontend; every path was driven over the same HTTP API the UI uses.
- No SMTP: the admin e-mail on stream failure could only be shown as "no log
  line, result unchecked", not as a delivered or bounced message.
- Both intakes ran on plain HTTP on loopback; TLS and syslog destinations were
  not part of this run (they are covered by the existing destination tests).

---

# After — the same scenarios against the implemented contract (PR #1595)

The fixes are described in `docs/design/audit-event-contract.md`. The same
harness ran against the implemented code in the same sandbox, with the backend
started without the reloader (`uvicorn main:app`), the platform stream and a
30 s heartbeat configured through a bow-config copy, and a fresh collector
state so dedupe counts start at zero. `11_after.py` adds the checks that had
no "before".

| Plan row | Before | After (observed at the collector) |
|---|---|---|
| Event contract | no outcome, no correlation | every new envelope: `outcome`, `failure_reason`, `category`, `schema_version: 2`, `legacy: false`, `recorded_at`, `correlation.request_id` (the client's `X-Request-ID` echoed back) and `correlation.session_id` (opaque, not in the token); 23,101 older rows export as `legacy: true` with the inferred outcome |
| Authentication, org-less events | stored, never exported, not pending | an unknown-account failure arrives at the **platform** intake only (`organization.id: null`, `actor.type: unknown`, `failure_reason: invalid_credentials`, client IP present) and at no organization intake; the two older org-less rows were delivered on the first tick |
| Session lifecycle | zero rows | `auth.login` (method password), `auth.login_failed` (known account routed to its organization, `account_known: true`), `auth.logout`, `auth.password_changed`, `auth.password_reset_requested`, all with IP, user agent, request id, session id and actor snapshot; the credential never appears |
| Authorization | null IP/UA/target | all four denials carry IP, user agent, `reason: missing_permission`; `delete_data_source` names the data source as target |
| Query evidence | 10 of 12, cut at 503 | 12 of 12, full length (1,047 to 1,109 chars); the marker appears only when the byte budget is exceeded (e2e test) |
| Event timing | envelope = write time | B: envelope `occurred_at 22:28:34.391` = the real time, `created_at 22:28:42.396` = the write, 8 s later; the exporter late-write test asserts `occurred_at < recorded_at` |
| Historical identity | rename rewrote, delete → "system" | rename: old email preserved in the envelope; delete: `actor.type: user`, email and name preserved, `id: null` |
| Access coverage | 9 read paths silent | `report.viewed`, `data_source.schema_viewed` (11 tables), `query.executed` ×3 (builder with the SQL, viewer, preview) with `observation`; `report.published`/`unpublished` once, with request context |
| Health | idle = broken, no alert on 503 | `audit.heartbeat` every 30 s per streamed organization and for the platform stream; after 2 consecutive 503s each stream wrote `audit_stream.degraded` and the backend logged `admin alert (degraded) was NOT delivered: SMTP is not configured` as an ERROR; recovery wrote `audit_stream.recovered`; `GET /api/enterprise/audit/health` reports queue, spill backlog, recording failures, unstamped rows, legacy count and per-stream lag |

Unchanged and still true: 12 of 12 events produced during a two-minute
collector outage arrived after recovery, once per intake; the secret scan
over every delivered byte stayed clean; the public report read returned 404
in this run because the publish call toggled the report back to draft (the
e2e test covers `report.public_viewed`).

Collector note: the harness's own schema check marks platform events
"missing organization.id"; that is the collector's rule from before the
platform stream existed, not a defect, and the count is reported as such.

Tests added or adapted: `backend/tests/unit/test_audit_contract_policy.py`
(43 cases: redaction, bounded queries, outcome inference, request id,
session id, trusted-proxy resolution), `test_audit_stream_envelope.py`
(legacy golden unchanged on the v1 keys, contract golden, snapshot wins),
`backend/tests/e2e/audit/test_audit_event_contract.py` (14 end-to-end
scenarios: session lifecycle, unknown-account routing, correlation and
spoofed `X-Forwarded-For`, denial context, read paths, tool query evidence,
rename, platform stream, heartbeat, retention with legal hold, health
endpoint, degraded/recovered). The dashboard corpus fixture replays
schema-only migrations unstamped, so `auditcontract01` is idempotent and
listed there.
