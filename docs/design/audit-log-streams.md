# Audit log streams, export and the Audit Logs page

**Status:** shipped in 0.0.575 ([bagofwords1/bagofwords#1232](https://github.com/bagofwords1/bagofwords/pull/1232)).
How it was verified, and the bugs the verification found:
`docs/feedback-loops/audit-log-streams.md`.

Three changes:

1. **Log streams.** An org admin sends every audit event the org produces to
   Datadog, Splunk, Microsoft Sentinel, Amazon S3, Google Cloud Storage, an
   HTTPS webhook or syslog. Delivery is at least once, with no gaps, and
   resumes from the last delivered event after any outage, pause, credential
   failure or restart.
2. **Tool-audit durability.** Agent tool events (`log_tool_audit`) are never
   dropped: not under load, not on database errors, not on shutdown.
3. **Audit Logs page.** Readable rows, a detail drawer, user, resource and
   time filters, wider search, and JSON/CSV export.

Gated by the `audit_log_streams` license feature (enterprise tier) for streams,
and by `audit_logs` for the page and export. Viewing needs `view_audit_logs`;
creating, editing or deleting a stream needs `manage_settings`.

---

## Event envelope (v1)

`app/ee/audit/streams/envelope.py` maps an `audit_logs` row to the public shape
that every stream and the JSON export emit. It is versioned and independent of
the table schema.

```json
{
  "id": "…",                      // audit_logs.id — the receiver's dedupe key
  "version": 1,
  "action": "api_key.created",
  "occurred_at": "2026-10-03T12:00:00.123Z",
  "organization": {"id": "…", "name": "Acme"},
  "actor":   {"type": "user|agent|system", "id": "…", "email": "…"},
  "targets": [{"type": "api_key", "id": "…", "name": "<details.title>"}],
  "context": {"ip_address": "…", "user_agent": "…"},
  "metadata": { /* audit_logs.details, verbatim */ }
}
```

`actor.type` is `agent` when `details.agent_execution_id` is set, `user` when
`user_id` is set, and `system` otherwise. The UI marker uses the same rule
(`frontend/utils/auditActionFormat.ts`).

## Data model (migration `auditstrm01`)

- **`audit_log_streams`**: one row per stream.
  - `destination`, `config` (JSON) and `secrets`. Secrets are a Fernet-encrypted
    JSON object, using the same scheme as `Connection.credentials`; they are
    never returned by the API.
  - `action_filter` (list of prefixes), `state`
    (`active | inactive | error | invalid`), and `start_from` with `start_after`.
  - `cursor_seq`, the last delivered `export_seq`.
  - Status fields: `delivered_count`, `last_delivered_at`, `last_attempt_at`,
    `last_error`, `consecutive_failures`, `next_attempt_at` (backoff and lease).
- **`audit_logs.export_seq`**: a visibility-ordered sequence, stamped by the
  exporter (below). Indexed on `(organization_id, export_seq)`.
- **`audit_export_state`**: a single row holding `last_seq` and the stamper
  lease.
- An `(organization_id, created_at, id)` index on `audit_logs` serves the
  export endpoint's keyset scan.

## Exporter (`app/ee/audit/streams/exporter.py`)

Runs as an APScheduler interval job, every `BOW_AUDIT_STREAM_INTERVAL_SECONDS`
(default 15). APScheduler fires it in every uvicorn worker, because the job
store is shared; the leases below make that safe.

**1. Stamp.** One process at a time wins the stamper lease, a compare-and-set
`UPDATE` on `audit_export_state` lasting 30 s. It gives newly visible rows of
organizations that have a stream the next `export_seq` values, in batches of
2,000, within a 10 s budget per tick.

The ordering is the guarantee. A row whose transaction commits late becomes
visible later and is stamped later, with a higher number, so no stream cursor
can already be past it. Ordering by `created_at` with a lag window (the first
design) lost rows under lock contention and on spill replay.

**2. Deliver.** Up to 4 streams run concurrently. Each due stream is claimed
with a compare-and-set lease on `next_attempt_at` (120 s). `SELECT … FOR UPDATE`
is not used, because SQLite ignores it. Then, within a 20 s budget per stream:

- read the next batch with `export_seq > cursor_seq`, plus
  `created_at >= start_after` for "new events only";
- apply `action_filter`;
- send the batch, and move `cursor_seq` only if the destination accepted it.

Rows excluded by the filter still advance the cursor.

| Send result | Effect |
|---|---|
| `ok` | cursor saved, failures reset |
| `retryable` (408/425/429/5xx, network, timeout) | stays `active`, backoff `min(5·2^(n-1), 900)` s ± 20 % |
| `invalid` (401/403, rejected token, TLS verification, undecryptable secrets) | state `invalid` |
| `fatal` (other 4xx, missing bucket) | state `error` |

A move to `invalid` or `error` is audited (`audit_stream.state_changed`) and
emailed once to the org's admins. Bookkeeping writes (lease release, progress,
finish) retry while the database is locked, so a busy database delays
delivery but never strands a stream behind its lease.

Duplicates can only follow a crash mid-send. They carry the same `id`, and an
S3 or GCS object key is a deterministic function of its batch, so a retried
batch overwrites the same object.

## Destinations (`app/ee/audit/streams/destinations/`)

Every destination implements `send(batch) -> SendResult`, and its fields are
declared once as `FieldSpec`s. That list drives API validation and the UI form
(`GET /enterprise/audit/streams/destinations`).

| Destination | Wire | Auth | Batch |
|---|---|---|---|
| `datadog` | `POST /api/v2/logs` on the chosen site; envelope JSON as `message` | `DD-API-KEY` | 500 |
| `splunk` | HEC `/services/collector/event`, NDJSON `{time, sourcetype, event}` | `Authorization: Splunk …` | 500 |
| `sentinel` | Logs Ingestion API `…/dataCollectionRules/{dcr}/streams/{stream}?api-version=2023-01-01` | Entra client credentials (`https://monitor.azure.com/.default`) | 500 |
| `s3` | `PutObject` NDJSON (optional gzip) at `{prefix}/{YYYY-MM-DD}/{ts}_{first_id}.json`, with `Content-MD5` | access keys, or AssumeRole with a generated External ID | 1000 |
| `gcs` | same writer through GCS's S3-compatible API | HMAC keys | 1000 |
| `https` | JSON array POST | optional auth header; `X-BOW-Signature: v1=hmac_sha256(secret, ts + "." + body)` with `X-BOW-Timestamp` | 500 |
| `syslog` | RFC 5424 over TCP/TLS, octet-counted framing; JSON or CEF message | optional mTLS, custom CA | 500 |

Each SDK's own retries are turned off (`total_max_attempts=1` for botocore), so
the exporter alone owns retry and backoff.

## Tool-audit queue (`app/ee/audit/tool_audit.py`)

- **Batching:** the worker drains up to 200 events into one session and one
  commit.
- **Retry:** a failed batch is retried after 0.5, 1 and 2 s, then written event
  by event so one bad row cannot sink the rest.
- **Full queue:** the caller waits up to 2 s for space, then writes its event
  inline. Nothing is discarded.
- **Disk spill:** what still cannot reach the database (an outage, or a shutdown
  that times out) is appended to `BOW_AUDIT_SPILL_DIR` (default
  `backend/data/audit-spill`). The scheduler leader replays it on the next
  start; each file is claimed by atomic rename.
- **No duplicates:** event ids are assigned at enqueue, so retries and replays
  skip rows that are already written.
- **`created_at`** is stamped when the row is written. A write delayed more than
  5 s keeps the original time in `details.occurred_at`.

## Scheduler resilience (`app/core/scheduler.py`)

`ResilientAsyncIOScheduler` wraps APScheduler 3.x's `_process_jobs`.
APScheduler does not guard `jobstore.update_job` there, so one failing
job-store write (for example SQLite "database is locked") escaped `wakeup()` and
silently stopped every scheduled job in the worker until restart. A failed pass
is now logged and retried after `jobstore_retry_interval`.

## API

| Route | Gate |
|---|---|
| `GET /enterprise/audit` — list, now with `resource_type` (comma list), `user_id`, `start_date`, `end_date`; `search` also matches actor email and `details.title` | `view_audit_logs` |
| `GET /enterprise/audit/resource-types` | `view_audit_logs` |
| `GET /enterprise/audit/export?format=json\|csv&<filters>` — streamed, oldest first, capped at 100,000 rows (`audit_log.export_too_large`); emits `audit_log.exported`; CSV cells are guarded against formula injection | `view_audit_logs` |
| `GET /enterprise/audit/streams`, `/{id}`, `/destinations` | `view_audit_logs` |
| `POST /enterprise/audit/streams`, `PATCH /{id}`, `DELETE /{id}`, `POST /test` | `manage_settings` |

The streams router is mounted before the audit router, because `/{log_id}`
would otherwise capture `/streams`. Responses mask secrets (`••••last4`). On
update, a masked value keeps the stored secret and an empty string clears it.
Stream create, update, pause, activate and delete are audited
(`audit_stream.*`). Errors are typed (`audit_stream.not_found`,
`.invalid_destination`, `.missing_field`, `.invalid_field`).

## UI (`frontend/pages/settings/audit.vue`, `frontend/components/audit/`)

- **Activity tab:**
  - each row shows the exact time, the actor with a "via agent" marker, the
    action as a muted resource path plus a verb chip coloured by its last
    segment (it truncates and never overlaps), and the target;
  - the drawer shows who, when (local, UTC, relative), where, what and the
    parsed details (queries, changes), with the raw JSON and copy buttons;
  - user, resource, time and action filters are kept in the URL;
  - JSON or CSV export of the filtered view.
- **Streams tab:**
  - a destination picker, then a form generated from the field schema;
  - **Send test event** before saving;
  - state chips, delivered and pending counts, the last error, and the retry
    countdown;
  - pause, resume, edit and delete.
- Strings are in all 10 locales, and the page works RTL (`he`).

## Operational notes

- **Encryption key:** set a stable `BOW_ENCRYPTION_KEY`, as for connection
  credentials. If the key changes, streams with secrets move to `invalid` and
  resume once their secrets are re-entered; no events are lost.
- **Network policy:** on air-gapped installs, the S3, Datadog and Sentinel
  endpoints may be blocked, and the failure shows up in the stream's status.
  Syslog or HTTPS to an internal collector still work.
- **Future retention purge:** it must not delete rows a stream has not yet
  delivered, i.e. rows past the lowest `cursor_seq` among active streams.

## Verification

Replay steps and observed results are in
`docs/feedback-loops/audit-log-streams.md`. In brief:

- **Mock SIEM:** `tools/agent/mock_siem_consumer.py` emulates every intake and
  injects faults.
- **Tests:** pytest contract, exporter, API and RBAC suites on SQLite and
  Postgres.
- **Live loop:** `tools/agent/audit_streams_loop.py` runs the happy path,
  transient faults, invalid-then-fixed credentials, a 10,000-event tool burst
  and a restart mid-send, finishing 27/27.
- **UI flow:** `tools/agent/audit_ui_flow.mjs`, 12/12 checks in `en` and `he`.
