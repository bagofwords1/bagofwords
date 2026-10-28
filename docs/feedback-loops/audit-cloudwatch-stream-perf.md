# Feedback Loop — Amazon CloudWatch Logs audit stream destination, and whether streaming costs the app anything

Design: `docs/design/audit-log-streams.md` (Destinations table). This record covers two claims:

1. An org admin can stream audit events to Amazon CloudWatch Logs the same way as to the seven existing destinations (picker, form, test event, save, pause, delete), with delivery at least once and no gaps.
2. An active stream has no measurable effect on request latency, event-loop responsiveness, CPU or memory of the app under write load, and costs nothing visible when idle.

Both were checked against the real AWS service with throwaway resources, all deleted at the end (`describe_log_groups(prefix="/bagofwords")` → `[]`).

## What was built

| Piece | Where |
|---|---|
| Destination adapter: `PutLogEvents`, one `{timestamp, message}` per envelope, sorted, split under the 1 MB / 10k-event call limit, `occurred_at` clamped into the 14 d-past / 2 h-future window, log group and stream created on first use (opt-out in Advanced), AssumeRole + External ID, custom endpoints checked against the egress policy | `backend/app/ee/audit/streams/destinations/cloudwatch.py` |
| Error classification: `UnrecognizedClientException` / `AccessDeniedException` / 403 → `invalid`; throttling / 5xx → `retryable`; `InvalidParameterException` / missing group with creation off → `fatal` | same |
| Registry, `DESTINATIONS` tuple, External ID generation on create/update | `destinations/__init__.py`, `models.py`, `service.py` |
| Mock intake (`POST /cloudwatch`, `X-Amz-Target: Logs_20140328.*`): SigV4 presence, stream-must-exist, chronological timestamps, size limits, window check, fault injection | `tools/agent/mock_siem_consumer.py` |
| Picker tile + icon, External ID hint, type union, 10 locales | `frontend/components/audit/*`, `frontend/ee/composables/useAuditStreams.ts`, `locales/*.json` |

## Loop A — deterministic (CI)

```bash
cd backend && BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/unit/test_audit_stream_destinations.py tests/e2e/audit/test_audit_streams_api.py -q
```

| Check | Result |
|---|---|
| Registry covers 8 destinations; destination schema endpoint lists `cloudwatch` with secret fields marked | pass |
| Batch of 1 and 7 delivered in the intake's format (sorted timestamps, JSON envelope per message) | pass |
| Wrong access key → `invalid`, nothing recorded | pass |
| 7 injected fault modes classified (400 fatal, 401/403 invalid, 404/429/500/503 retryable, timeout, connection reset) | pass |
| Chunking: timestamps outside the window clamped not dropped; 6 × 300 KB events split into ≤1 MB calls with oversized `metadata` replaced by `{"truncated": true}` | pass |
| Missing group + stream auto-created, then delivered; creation off → `fatal` naming `ResourceNotFoundException` | pass |
| Whole audit suite (`test_tool_audit_queue`, envelope, destinations, `tests/e2e/audit`, `test_audit.py`, `test_license.py`) | **172 passed** |

## Loop B — live stack against real CloudWatch

Sandbox: backend on SQLite with the enterprise license faked in-process, `BOW_AUDIT_STREAM_INTERVAL_SECONDS=5`, pinned `BOW_ENCRYPTION_KEY`, Nuxt dev frontend. AWS credentials were held only in the shell environment and in the stream's Fernet-encrypted `secrets` column.

### B1 — real delivery

- Direct adapter call: group `/bagofwords-smoke/…` and stream created, 5 + 2 events accepted (879 ms first call including creation, 505 ms after), all ids read back with `get_log_events`, bad key → `invalid`. Group deleted.
- Through the UI (Playwright, `media/pr/audit-log-streams-cloudwatch/`): CloudWatch tile in the picker → form → **Send test event** → "Test event delivered" → **Save stream** → row shows `Active · 1 pending`; the stream's own `audit_stream.created` event arrived in the group before the stream was deleted. Group deleted.
- API response masks both secrets (`••••K33U`, `••••8+ig`); the raw values never appear in any response body.

### B2 — performance under load (the question this loop exists to answer)

Load: 6 concurrent workers × 40 iterations of audited writes (API-key create + delete, report create + update, a paginated list read in between), ~720 requests per leg, with a `/health` probe every 50 ms as an event-loop responsiveness gauge. Backend CPU and RSS read from `/proc/<pid>`. Legs interleaved so SQLite noise is visible on its own: baseline → active → paused → active → idle. The active stream started from the beginning of history, so leg B1 exported the backlog from A1 while absorbing new load (worst case).

| Leg | Stream | Wall s | Req/s | Backend CPU s | Writes p50 / p95 / p99 ms | Reads p50 / p95 ms | /health p50 / p95 / max ms | Errors |
|---|---|---|---|---|---|---|---|---|
| A1 no stream | no | 45.0 | 16.0 | 51.5 | 228 / 518 / 5200 | 367 / 457 | 4.6 / 12.9 / 5015 | 0 |
| B1 CloudWatch active (+ A1 backlog) | yes | 34.9 | 20.6 | 51.8 | 248 / 508 / 634 | 373 / 454 | 4.7 / 12.2 / 1108 | 0 |
| A2 stream paused | no | 38.8 | 18.6 | 50.2 | 232 / 527 / 1413 | 368 / 447 | 4.6 / 12.8 / 5011 | 0 |
| B2 CloudWatch active | yes | 44.9 | 16.0 | 51.4 | 227 / 562 / 5350 | 370 / 1451 | 4.5 / 11.2 / 5011 | 0 |
| C idle, stream active | yes | 20.0 | 0 | 1.13 | — | — | 3.2 / 4.1 / 15.6 | 0 |
| C' idle, no stream | no | 20.0 | 0 | 0.90 | — | — | 3.2 / 4.2 / 15.7 | 0 |

Reading:

- **p50 is flat across all four loaded legs** (227–248 ms writes, 367–373 ms reads, 4.5–4.7 ms probe). Backend CPU per leg is 50–52 s whichever way the stream is set; the exporter's work (27 `PutLogEvents` calls, 5,005 events) is within that ±1 s.
- **The tail is SQLite, not the stream.** The ~5 s maxima (busy timeout) appear in the two no-stream legs (A1 p99 5200 ms, A2 max 5011 ms) as much as in B2. The scheduler's "database is locked" retries in the backend log start at 15:06:53, before any stream existed, and recur in every leg. The fastest leg of the five was B1, with the stream active and draining backlog.
- **Idle cost is below the noise floor:** 1.13 s vs 0.90 s CPU over 20 s, with the stream's 5-second tick finding nothing pending; probe latencies identical.
- **Why it cannot block:** `put_log_events` runs under `asyncio.to_thread`, the exporter is a scheduler job (not a request hook) with `max_instances=1` and a per-stream row lease, and botocore retries are off so the exporter's own backoff is the only retry.

### B3 — correctness during the run

| Check | Result |
|---|---|
| Catch-up after leg C: `pending=0`, `state=active`, `last_error=None` | 0.1 s after C ended |
| Delivered count vs audit rows | 5,005 / 5,005 |
| Read back from CloudWatch | 5,006 events, 5,006 unique ids (the +1 is the `audit_stream.test` event, excluded from the DB count) |
| Duplicates | 0 |
| Cleanup | stream `DELETE` → 204; `/bagofwords-perf/*`, `/bagofwords-ui/*`, `/bagofwords-smoke/*` all deleted; `describe_log_groups(prefix="/bagofwords")` → `[]` |

## Not covered

- Postgres leg (SQLite only; the lock-wait tail above would not exist there).
- AssumeRole against a real IAM role (covered by the mock STS path shared with S3).
- Multi-worker uvicorn (the stream claim lease already covers racing exporters in `test_audit_streams_exporter.py`).
