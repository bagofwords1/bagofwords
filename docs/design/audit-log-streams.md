# Audit log streams — implementation plan

**Status:** plan (not implemented). Branch: `ai/hopeful-pasteur-ubk7d7`.

An org admin configures one or more **log streams** in *Settings → Audit*,
and every audit event the org produces is delivered to their SIEM or bucket:
Datadog, Splunk, Microsoft Sentinel, AWS S3, Google Cloud Storage, a generic
HTTPS endpoint, or syslog over TLS. Delivery is **at-least-once and resumable**:
a stream that is paused, broken, or rejected picks up from the last event it
delivered, with no gaps.

Precondition: the tool-audit queue must stop dropping events. A stream can only
deliver what reached `audit_logs`, so that fix is work package 0.

Verification follows the **sandbox-feedback-loop** skill. A single **mock SIEM
consumer** (`tools/agent/mock_siem_consumer.py`) emulates every destination,
records what it receives, and injects faults on command. Every claim in this
plan has a loop that can fail before the change and pass after it. The results
land in `docs/feedback-loops/audit-log-streams.md`.

---

## Outcome (acceptance criteria)

| # | Promise | Proven by |
|---|---|---|
| A1 | Every event written to `audit_logs` for an org reaches each of its `active` streams | Loop B1: mock `/_stats` shows `missing == 0` against a DB count |
| A2 | No event is skipped: no gaps, even across retries, restarts, and late-committing transactions | Loops A3, B3, B5 |
| A3 | Duplicates are possible but carry the same `id` | Loop B5: duplicates appear only after a kill-mid-batch, every one with the same `id` |
| A4 | A 429 or 5xx response is retried and the stream stays `active` | Loop B2 |
| A5 | A 401 or 403 moves the stream to `invalid`, a non-retryable 4xx moves it to `error`, and fixing the config resumes from the cursor | Loop B3 |
| A6 | The tool-audit queue never drops events: `dropped == 0` under a burst 10× the queue size with a slow DB | Loop A0 |
| A7 | Each destination's wire format and auth match what that vendor's intake expects | Loop A2 (per-destination contract tests against the mock) |
| A8 | Only `manage_settings` can create, edit, or delete streams; `view_audit_logs` can view their status; members can do neither | Loop A4 (RBAC e2e) |
| A9 | Stream secrets are never returned by the API and are encrypted at rest | Loop A4 |
| A10 | The UI works in en, es, and he (RTL) | Loop B6 (ui-evidence) |

---

## Validated current boundaries

- `backend/app/ee/audit/service.py:23`: `AuditService.log` is the single
  writer behind all ~57 call sites (`db.add` at :73, commit at :75). The stream
  exporter reads rows this function wrote and never touches the request path.
- `backend/app/ee/audit/models.py`: `AuditLog` has `id` (uuid),
  `organization_id`, `user_id`, `action`, `resource_type`, `resource_id`,
  `details` (JSON), `ip_address`, `user_agent`, and `created_at` (app clock,
  `datetime.utcnow`). There is an index on `(organization_id, created_at)` and
  no retention purge.
- `backend/app/ee/audit/tool_audit.py`: the queue drops or loses events in
  five places:
  - :227: `put_nowait` → `QueueFull` → the event is dropped (`_QUEUE_MAXSIZE = 1000`, :20).
  - :130: a failed write is counted, never retried.
  - :88: one session and one commit per event, serially. This low throughput is why the queue fills.
  - :186: shutdown drains for only 5 s (:21), then cancels; the rest is lost.
  - :146: `_ensure_worker` swaps in a fresh queue when the task has died, orphaning pending events.
- `backend/main.py:458`: `try_acquire_scheduler_leader()` takes a per-host
  `flock` (`app/core/scheduler.py:67`). That is **not** cluster-wide, so with
  multiple replicas each host has its own leader. The exporter therefore also
  needs a row lock per stream.
- `backend/app/ee/audit/routes.py:23`: the router prefix is
  `/enterprise/audit`. Routes there are gated with
  `@require_enterprise(feature="audit_logs")` and
  `@requires_permission("view_audit_logs")`.
- `backend/app/ee/license.py:19`: `TIER_FEATURES`. A new feature key needs no
  license regeneration.
- `backend/app/ee/encryption/types.py:216`: `encrypt_value` / `decrypt_value`
  for secrets at rest.
- `frontend/pages/settings/audit.vue`: the existing audit settings page; the
  streams UI goes here as a tab.
- `httpx` and `boto3` are already dependencies (`backend/pyproject.toml`).

---

## Design

### Event envelope (v1)

The envelope is a stable public contract, separate from the table schema and
built from one `AuditLog` row:

```json
{
  "id": "6f1c…",                          // audit_logs.id: the dedupe key
  "version": 1,
  "action": "api_key.created",
  "occurred_at": "2026-10-03T12:00:00.123Z",
  "organization": {"id": "…", "name": "Acme"},
  "actor":   {"type": "user", "id": "…", "email": "a@acme.com"},
  "targets": [{"type": "api_key", "id": "…"}],
  "context": {"ip_address": "203.0.113.4", "user_agent": "…"},
  "metadata": { /* audit_logs.details, verbatim */ }
}
```

- `actor.type` is `user` when `user_id` is set and `agent` when `details`
  carries `agent_execution_id` (tool events, see `tool_audit._build_event`).
  Otherwise it is `system`.
- Lives in `app/ee/audit/streams/envelope.py` as a pure function, covered by a
  golden unit test whose expected output is reviewed by hand (tests AGENTS.md
  rule 9).

### Data model (migration `auditstrm01`)

`audit_log_streams`:

| column | type | notes |
|---|---|---|
| `id` | str(36) | |
| `organization_id` | fk | indexed |
| `name` | str | display name |
| `destination` | enum str | `datadog`, `splunk`, `sentinel`, `s3`, `gcs`, `https`, `syslog` |
| `config` | JSON | non-secret fields (site, URL, bucket, region, DCR id…) |
| `secrets` | JSON | encrypted envelope (`encrypt_value`); never serialized out |
| `action_filter` | JSON null | list of prefixes, e.g. `["tool.", "user."]`; null means all |
| `state` | enum str | `active`, `inactive`, `error`, `invalid` |
| `cursor_created_at`, `cursor_id` | datetime, str | last delivered event; null means start position |
| `start_from` | enum str | `now` or `beginning` (backfill), applied on first activation |
| `last_delivered_at`, `last_attempt_at` | datetime | |
| `last_error` | text | truncated; no secrets |
| `consecutive_failures`, `next_attempt_at` | int, datetime | drives backoff |
| `created_at`, `updated_at`, `created_by` | | |

This also adds an index `audit_logs (organization_id, created_at, id)` so the
cursor scan is index-only. It must work on both sqlite and postgres; CI runs
both.

### Destinations

All destinations share one interface in `app/ee/audit/streams/destinations/`:

```python
class Destination(Protocol):
    max_batch: int
    async def send(self, events: list[dict]) -> SendResult  # ok | retryable(err) | invalid(err) | fatal(err)
    async def test(self) -> SendResult                       # sends one audit_stream.test event
```

| destination | wire | auth | classification |
|---|---|---|---|
| `datadog` | `POST https://http-intake.logs.{site}/api/v2/logs`, array of `{ddsource:"bagofwords", service, hostname, message: <envelope json>}` | `DD-API-KEY` header | 202 ok · 400/413 fatal · 403 invalid · 429/5xx retryable |
| `splunk` | `POST {hec_url}/services/collector/event`, NDJSON lines of `{time, sourcetype:"bagofwords:audit", source, event: <envelope>}` | `Authorization: Splunk <token>` | 200 ok · 401/403 invalid · 400 fatal · 503/5xx retryable |
| `sentinel` | `POST {dce}/dataCollectionRules/{dcr_id}/streams/{stream}?api-version=2023-01-01`, JSON array | Entra client-credentials token (`login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, scope `https://monitor.azure.com/.default`), fetched with `httpx`, no new dependency | 204 ok · 401/403 invalid · 413 fatal · 429/5xx retryable |
| `s3` | `PutObject` key `{prefix}/{YYYY-MM-DD}/{first_occurred_at}_{first_id}.json`, NDJSON (optional gzip), `ContentMD5` set so Object-Lock buckets accept it | Cross-account role via STS `AssumeRole` + external ID (generated per stream and shown in the UI), or access keys for self-hosted | AccessDenied invalid · NoSuchBucket fatal · throttling/5xx retryable |
| `gcs` | Same writer as S3 against `https://storage.googleapis.com` | HMAC keys | Same as S3 |
| `https` | `POST {url}`, JSON array | Custom headers plus `X-BOW-Timestamp` and `X-BOW-Signature: v1=hex(hmac_sha256(secret, ts + "." + body))` | 2xx ok · 401/403 invalid · other 4xx fatal · 408/429/5xx retryable |
| `syslog` | RFC 5424 over TCP+TLS with RFC 5425 octet-counting framing (`asyncio.open_connection(ssl=…)`). `MSG` is the envelope JSON; optional CEF | Optional mTLS client cert | Connect/TLS failure retryable · cert rejected invalid |

The S3 key is a deterministic function of the batch's first event, and batch
boundaries come from the cursor. A retried batch therefore overwrites the same
object instead of creating a duplicate.

### Exporter

The exporter lives in `app/ee/audit/streams/exporter.py` and is registered in
`main.py` next to the other leader-only jobs. It runs as an interval job every
15 s with `max_instances=1` and `coalesce=True`.

For each stream with `state='active'` and `next_attempt_at <= now`:

1. **Claim the stream.** `SELECT … FOR UPDATE SKIP LOCKED` on postgres; on
   sqlite, a single process already serializes this. That keeps multiple hosts
   from double-sending.
2. **Read a batch.** Up to `max_batch` rows where
   `organization_id = :org AND (created_at, id) > (:cursor_created_at, :cursor_id) AND created_at < now() - :lag`,
   ordered by `created_at, id`. `:lag` defaults to 30 s (`BOW_AUDIT_STREAM_LAG_SECONDS`).
   The lag covers transactions that commit late with an earlier `created_at`;
   without it the cursor would step over them.
3. **Filter.** Apply `action_filter` in Python. Filtered-out rows still advance the cursor.
4. **Send** with `destination.send(batch)`.
5. **Advance or back off.**
   - `ok`: advance the cursor to the last row, reset failures, and loop while
     full batches keep coming (bounded per tick).
   - `retryable`: `next_attempt_at = now + min(2^n · 5 s, 15 min)` with jitter;
     the state stays `active`.
   - `invalid` or `fatal`: set the state, store a redacted `last_error`, and
     email the org admins once per transition.
6. **Commit** the cursor and state together.

Re-activating a stream (PATCH `state=active`, or saving new credentials) clears
`next_attempt_at`. Delivery resumes from the cursor, so nothing written while
the stream was down is lost.

### API

New routes in `app/ee/audit/streams/routes.py`, under prefix
`/enterprise/audit/streams`:

| method | path | gate |
|---|---|---|
| GET | `` | `view_audit_logs` |
| POST | `` | `manage_settings` |
| GET | `/{id}` | `view_audit_logs` |
| PATCH | `/{id}` (config, secrets, filter, `state: active\|inactive`) | `manage_settings` |
| DELETE | `/{id}` | `manage_settings` |
| POST | `/{id}/test` (synchronous, returns the `SendResult`) | `manage_settings` |
| GET | `/{id}/status`: state, lag (`now - cursor_created_at`), pending count, last error | `view_audit_logs` |

- All routes carry `@require_enterprise(feature="audit_log_streams")`; the key
  is added to the `enterprise` tier in `app/ee/license.py`.
- Responses expose `secrets` only as `{"<field>": "••••<last4>"}`.
- Create, update, and delete each emit an audit event:
  `audit_stream.created|updated|deleted|activated|paused`.

### UI

A **Streams** tab in `frontend/pages/settings/audit.vue`:

- The list shows a state chip, destination icon, lag, and last error.
- *Add stream* opens a dialog: choose a destination, fill in its form, choose
  "start from now / include history", then **Send test event**, then save.
- Pause, resume, edit, and delete per stream.
- The S3 form shows the generated external ID and a copyable trust-policy
  snippet.
- Strings go in `locales/{en,es,he}.json` (same shape); RTL is checked in `he`.

---

## Work packages

### WP0: tool-audit queue stops dropping events (`tool_audit.py`)

1. **Batched writes.** The worker awaits one event, then drains up to 200 with
   `get_nowait()`. One session, `add_all`, one commit.
2. **Retry.** Up to 3 attempts per batch (0.5 s, 1 s, 2 s backoff), then fall
   back to per-event writes so one poison row can't sink the batch.
3. **No drop on full.** If the queue is full, the calling tool writes the event
   inline via `_write_event` and pays the latency itself. `_dropped_count`
   stays as a guard metric that should always read 0.
4. **Disk spill.** If the DB is still failing after retries, or the shutdown
   drain times out, append to `$BOW_DATA_DIR/audit-spill/<pid>-<ts>.jsonl`. The
   scheduler leader replays and deletes spill files on startup.
5. **Worker restart.** `_ensure_worker` reuses the existing queue and only
   restarts the task.
6. **Stats.** `get_tool_audit_queue_stats()` adds `spilled` and `replayed`.

### WP1: envelope, model, migration, exporter core, `https` + `s3` destinations
### WP2: `datadog`, `splunk` destinations (thin wrappers over the shared HTTP sender)
### WP3: `sentinel`, `gcs`, `syslog` destinations
### WP4: API routes, license key, RBAC, secret redaction, admin emails on state transitions
### WP5: UI tab + i18n + ui-evidence
### WP6: feedback-loop doc + docs.bagofwords.com page (docs-update skill) + CHANGELOG (release-notes skill)

Rough size: WP0 1d · WP1 3d · WP2 1d · WP3 2d · WP4 1d · WP5 2d · WP6 0.5d.

---

## The mock SIEM consumer

`tools/agent/mock_siem_consumer.py` is a single-file mock in the same style as
`tools/agent/mock_infor_epm_server.py`: stdlib only, run with
`python tools/agent/mock_siem_consumer.py --port 8790 --syslog-port 6514`.
It is **not** a vendor product. It emulates each intake's documented surface
closely enough to validate wire format and auth, and nothing more.

It is also importable as a pytest fixture (`tests/mocks/siem_consumer.py`
re-exports it) and starts on an ephemeral port in a thread. Loop A and Loop B
use the same mock.

### Emulated intakes

| path / port | emulates | validates | 2xx response |
|---|---|---|---|
| `POST /dd/api/v2/logs` | Datadog logs intake | `DD-API-KEY == demo-dd-key`; body is an array; each `message` parses as envelope v1 | 202 `{}` |
| `POST /splunk/services/collector/event` | Splunk HEC | `Authorization: Splunk demo-hec-token`; NDJSON; each line has an `event` | 200 `{"text":"Success","code":0}` |
| `POST /entra/{tenant}/oauth2/v2.0/token` | Entra token endpoint | client id/secret `demo-client`/`demo-secret`, scope | 200 `{access_token, expires_in}` |
| `POST /sentinel/dataCollectionRules/{dcr}/streams/{stream}` | Logs Ingestion API | bearer token minted above; `api-version` present | 204 |
| `PUT /s3/{bucket}/{key}` (path-style) | S3 / GCS PutObject | `Content-MD5` matches the body; key matches `{prefix}/YYYY-MM-DD/{ts}_{id}.json`; SigV4 header present (signature not verified) | 200 + `ETag` |
| `POST /sts/` `Action=AssumeRole` | STS | `ExternalId == stream's` (configured via control API) | 200 XML credentials |
| `POST /https/{anything}` | Generic HTTPS | `X-BOW-Signature` recomputed with `demo-hmac-secret`; timestamp within 5 min | 200 |
| TCP+TLS `:6514` | syslog RFC 5425 | octet-count framing; RFC 5424 header; `MSG` parses as envelope | — (no ack, by protocol) |

The syslog listener uses a self-signed cert the mock writes to
`--state-dir/tls/` at startup. The stream config points `ca_cert` at it.

### Control and inspection API

| method | path | purpose |
|---|---|---|
| POST | `/_control/fault` `{"dest":"splunk","mode":"503\|429\|401\|403\|400\|timeout\|reset","count":3}` | The next *count* requests to *dest* fail that way (`count: -1` means until cleared) |
| DELETE | `/_control/fault` | clear all faults |
| POST | `/_control/sts` `{"external_id":"…"}` | set the expected external ID |
| GET | `/_received?dest=…` | received envelopes, in arrival order |
| GET | `/_stats?dest=…&expect_ids=<file>` | `{received, unique, duplicates, missing:[…], out_of_order, auth_failures, format_errors}` |
| DELETE | `/_received` | reset |

Every request is also appended to `--state-dir/<dest>.jsonl` with headers
redacted, so a Loop B run leaves an inspectable artifact.

`missing` is computed against an id list the loop exports from the DB
(`SELECT id FROM audit_logs WHERE organization_id = …`). That cross-check is
what proves A1 and A2: the DB is the source of truth, and the mock's view of
it must match.

---

## Loop A: deterministic (no external services, runs in CI)

Setup comes from the sandbox-feedback-loop skill:

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true && mkdir -p db
```

### A0. Queue drops: reproduce first (`tests/unit/test_tool_audit_queue.py`)

The test stubs only the DB boundary's latency: it wraps `async_session_maker`
so each commit takes 20 ms. It then fires `10 × _QUEUE_MAXSIZE`
`log_tool_audit` calls concurrently and drains.

- **Invariant:** `rows in audit_logs == calls made` and
  `stats["dropped"] == 0`. The test is parametrized over burst size and commit
  latency, so it covers the general case, not one magic number.
- **Before WP0: FAIL** (`dropped > 0`, rows < calls).
- **After WP0: PASS.**
- Also: a DB that fails N times then recovers leads to every event written
  (retry). A DB that never recovers during shutdown leads to the spill file
  holding exactly the unwritten events, and replay on the next start leads to
  every row present once.

### A1. Envelope (`tests/unit/test_audit_stream_envelope.py`)

- User, agent, and system actor mapping.
- `details` passes through unchanged.
- Timestamps are ISO-8601 UTC with a `Z` suffix.
- A golden file for one row of each actor type, reviewed by hand.

### A2. Destination contracts (`tests/unit/test_audit_stream_destinations.py`)

For each destination, send a batch to the mock fixture and assert:

- The mock reports `format_errors == 0` and `auth_failures == 0`.
- Received ids equal sent ids.
- With wrong credentials, the result is `invalid`.
- For each fault mode, `SendResult` matches the classification table above
  (parametrized over destination × mode).
- S3: `Content-MD5` is present, and the key is deterministic (sending the same
  batch twice produces one key).
- HTTPS: the HMAC verifies.
- Syslog: framing round-trips.

### A3. Exporter cursor semantics (`tests/e2e/audit/test_audit_streams_exporter.py`)

These tests seed events through real API actions: create and revoke API keys
via fixtures. They point a stream at the mock fixture and call the exporter
tick function directly (no scheduler, no sleep).

- **All delivered:** N events, tick until idle, then `missing == []`.
- **Cursor only moves on success:** inject 503×3, tick ×4, then every event
  is delivered once and the state is still `active`.
- **Late commit:** insert a row with `created_at` older than the cursor but
  inside the lag window, tick, and it is delivered. This is the one direct DB
  write, with a comment explaining why: the API can't backdate.
- **Invalid then fixed:** a 401 moves the stream to `invalid`. PATCH new
  credentials and `active`, tick, and events written while it was invalid
  arrive with no gaps.
- **Action filter:** filtered actions are absent and the cursor still advances
  past them.
- **Org isolation:** org B's events never reach org A's stream.
- **Backfill:** `start_from=beginning` delivers pre-existing rows;
  `start_from=now` does not.

All of these run with `--db=sqlite` and `--db=postgres`.

### A4. API + RBAC (`tests/e2e/audit/test_audit_streams_api.py`)

- An admin can do full CRUD.
- A member gets 403 on every route.
- A role with only `view_audit_logs` can GET but not create.
- Without a license, every route returns the enterprise-required error.
- Response bodies never contain the plaintext secret.
- The DB column holds an encrypted envelope, not the plaintext.
- CRUD emits `audit_stream.*` events.
- `/test` against the mock returns ok; against a mock 401 it returns invalid.

---

## Loop B: live stack against the mock consumer

This loop runs the real app, uvicorn workers, scheduler, and browser. It needs
no third-party credentials.

```bash
# 1. stack + org
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py --demo --invite member@example.com
export BOW_AUDIT_STREAM_LAG_SECONDS=2     # shorten for the loop; default 30

# 2. mock consumer
python tools/agent/mock_siem_consumer.py --port 8790 --syslog-port 6514 \
  --state-dir /tmp/siem-mock &

# 3. driver
uv run python ../tools/agent/audit_streams_loop.py --base-url http://localhost:8000 \
  --mock http://localhost:8790 --scenario all
```

`tools/agent/audit_streams_loop.py` creates one stream per destination via the
API, all pointed at the mock. It generates events through real actions: login,
API key create/revoke, member invite, and an agent chat via
`tools/agent/stub_llm.py` so tool audit events flow too. It then polls
`/_stats` until convergence or a timeout and prints a PASS/FAIL table per
scenario.

| scenario | steps | pass condition |
|---|---|---|
| **B1 happy path** | 200 mixed events | every destination: `missing == 0`, `format_errors == 0` |
| **B2 transient** | `fault splunk 503 count=5`, `fault dd 429 count=3`, generate 50 events | `missing == 0`; stream status shows `active`; backend log shows backoff |
| **B3 invalid → fixed** | `fault sentinel 401 count=-1`, generate 30 events, check state is `invalid` and the admin email captured (SMTP stub), clear fault, PATCH `active` | the 30 events arrive; `missing == 0` |
| **B4 tool burst** | Run the stub-LLM agent in a loop producing about 10× the queue size of tool events, with `tools/agent/pg_latency_proxy.py` adding DB latency (postgres leg) | `GET` queue stats: `dropped == 0`; `missing == 0` at the mock |
| **B5 crash mid-batch** | `fault https timeout count=1`, run `tools/agent/restart_backend.sh` during the in-flight request | `missing == 0`; every duplicate has the same `id` (A3) |
| **B6 UI** | Playwright via `tools/agent/capture.mjs`: add a stream, send a test event (shows success), pause/resume, the state chip turns `invalid` under a 401 fault; repeat in `he` | screenshots + a GIF of the add-stream flow (ui-evidence skill), RTL layout correct |
| **B7 multi-worker** | `start.sh` with `WORKERS=4`; also two hosts against one postgres (two `boot_stack` backends on different ports, both with `BOW_SCHEDULER_LEADER=1`) | duplicates bounded by one batch; `missing == 0` |

The loop exits non-zero on any FAIL, so it can run as a manual or nightly CI
job.

### Loop C (optional): real vendors

This loop sends to real trial accounts (a Datadog trial, Splunk Cloud trial,
an Azure DCR, and a bucket). Credentials come from env vars only:
`BOW_IT_DD_API_KEY`, `BOW_IT_SPLUNK_HEC`, and so on. The tests go in
`tests/integrations/audit_streams/` and are CI-gated. Each one sends a test
event, then reads it back through the vendor's search API. Its job is to catch
drift between the mock and the real intake; it is not required to merge.

---

## Feedback-loop record

After implementation, write `docs/feedback-loops/audit-log-streams.md` using
the skill's template:

- A0's observed FAIL (dropped count) before WP0, and the PASS after.
- A2 and A3 pytest output.
- Loop B's PASS table plus the `/tmp/siem-mock/*.jsonl` excerpts (redacted).
- The ui-evidence screenshots.
- Any pre-existing unrelated failures, verified with the change stashed.

## Risks / open questions

- **Clock skew across hosts.** `created_at` comes from each host's app clock.
  The lag window absorbs small skew. If multi-host skew exceeds it, switch
  `created_at` to a DB-side `server_default=func.now()` for new rows.
- **Very high-volume orgs.** Tool events dominate the volume. Add
  `action_filter` presets (e.g. "exclude `tool.*`") in the UI, and cap
  per-tick work per stream so one stream can't starve the others.
- **Outbound network policy.** On air-gapped installs, the S3, Datadog, and
  Sentinel endpoints may be blocked; the error shows up in the stream's status.
  Syslog and generic HTTPS to an internal collector stay available.
- **Future retention purge.** It must not delete rows newer than the minimum
  `cursor_created_at` of active streams. Leave a guard and a test hook in WP1.
