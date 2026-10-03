# Feedback Loop — "Audit export to a SIEM (webhook, S3 or syslog). Also make the tool-audit queue stop dropping events"

Plan: `docs/design/audit-log-streams.md`. This record covers four claims:

1. The tool-audit queue (`app/ee/audit/tool_audit.py`) dropped events under load and lost them on DB errors and shutdown. After the fix it loses none.
2. Org admins can stream every audit event to Datadog, Splunk, Microsoft Sentinel, S3, GCS, an HTTPS webhook or syslog. Delivery is at least once, with no gaps across retries, rejected credentials, pauses, restarts and racing exporters.
3. The Audit Logs page overlapped multi-segment actions (the production screenshot) and hid most stored fields. After the fix, rows never overlap, every field is reachable in a drawer, and filters, search and export work.
4. The UI works in `en` and `he` (RTL).

Every loop below was run in a fresh sandbox. Each was seen failing on the old code wherever an old code path existed, then passing.

## Root causes (validated)

| # | Where | What broke |
|---|---|---|
| Q1 | `tool_audit.py` (old :227) | `put_nowait` on a full 1000-slot queue dropped the event. |
| Q2 | old :130 | A failed write was counted but never retried, so the event was lost. |
| Q3 | old :88 | One session and one commit per event, serially. Low throughput is what filled the queue. |
| Q4 | old :186 | Shutdown drained for 5 s, then cancelled; the rest was lost. Nothing survived a process crash. |
| Q5 | old :146 | `_ensure_worker` replaced the queue when the task had died, orphaning queued events. |
| U1 | `pages/settings/audit.vue` (old :111, :263, :271) | The action chip sat in a fixed `w-24` column with no truncation. Only two-segment actions were shortened, so `artifact.record.created` printed in full over the resource column. The colour came from `split('.')[1]` (`record`), not the verb. |
| U2 | `ee/audit/service.py` (old :119) | `search` matched only `action` and `resource_type`, so an email or a report title returned 0 rows. |
| E1 | new exporter, draft 1 | The stream claim was `SELECT … FOR UPDATE SKIP LOCKED`. SQLite ignores `FOR UPDATE`, so racing exporters all sent the same batch (4 of 4). Replaced with a compare-and-set lease. |
| E2 | new exporter, draft 2 | Streams cursored on `(created_at, id)` with a lag window. A row that commits after newer rows were sent lands behind the cursor and is never delivered. Loop B4 hit this twice: queue rows stamped at enqueue time, then SQLite lock waits longer than the window (≈4,940 events lost per stream). Replaced by a visibility-ordered `audit_logs.export_seq`, stamped once a row is committed. |
| S1 | `app/core/scheduler.py` (pre-existing) | APScheduler 3.11 does not guard `jobstore.update_job` in `_process_jobs`. One SQLite "database is locked" there escaped `wakeup()` and silently stopped every scheduled job in the worker until restart (12 occurrences during Loop B). Now `ResilientAsyncIOScheduler` logs the failed pass and retries. |

## Environment

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
# Postgres leg without Docker: a local PG 16 on :5433
#   initdb -A trust; pg_ctl -o '-p 5433' start; createdb bow_test
#   TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/bow_test  … pytest --db=external
```

## Loop A — deterministic (no external services; runs in CI)

| Loop | File | Before | After |
|---|---|---|---|
| A0 queue durability | `tests/unit/test_tool_audit_queue.py` | **5 failed**: burst 200 into queue 20 dropped 180; 2 transient DB failures lost 2 of 7; no spill or replay existed | **5 passed** |
| A1 envelope v1 | `tests/unit/test_audit_stream_envelope.py` | — (new) | 3 passed; the golden value was reviewed by hand |
| A2 destination contracts | `tests/unit/test_audit_stream_destinations.py` | — (new) | **62 passed**: 7 destinations × format/auth, 6 × bad credentials → `invalid`, 5 × 7 fault modes classified, S3 deterministic key, AssumeRole + ExternalId, syslog CEF, untrusted TLS cert, unreachable collector |
| A3 exporter semantics | `tests/e2e/audit/test_audit_streams_exporter.py` | late-commit test (rows committed 5 s / 10 min / 2 days after newer rows were sent) **fails 3/3 on the created_at cursor**; spilled-then-replayed tool events **0 of 3 delivered**; concurrent-claim test **fails on the `FOR UPDATE` claim** (4 of 4 racers sent) | **16 passed**, including racing stampers (no duplicate sequence numbers) and stamping through a locked database |
| A3b scheduler | `tests/unit/test_scheduler_resilience.py` | plain APScheduler: ≤1 run after one failed `update_job` (kept as a test documenting upstream behaviour) | resilient scheduler keeps firing: **2 passed** |
| A4 API + RBAC | `tests/e2e/audit/test_audit_streams_api.py` | — (new) | **11 passed**: admin lifecycle, secrets masked and encrypted at rest, typed validation errors, member 403 on every route, `view_audit_logs`-only role reads but can't manage, license gate 402 |
| A5 row formatting | `frontend/tests/unit/auditActionFormat.mjs` (`node …`) | old `formatAction` / `getActionClass` produced the full action and the `record` colour for 3-segment actions (screenshot below) | passes for **224 action shapes**, including **all 218 actions the backend emits** (discovered by scanning `backend/app`) |
| A5 layout spec | `frontend/tests/settings/audit-log.spec.ts` | — | en + he; skips on unlicensed CI (the licensed run is the UI flow below) |
| A6 filters / search / export | `tests/e2e/audit/test_audit_log_query.py` | search-by-email **fails on the old search** (`assert 0 > 0`) | **12 passed** |

```bash
uv run pytest tests/unit/test_tool_audit_queue.py tests/unit/test_audit_stream_envelope.py \
  tests/unit/test_audit_stream_destinations.py tests/e2e/audit tests/e2e/test_audit.py -q
# sqlite: all pass · Postgres 16 (--db=external): 65 passed (audit e2e + queue + existing test_audit.py)
node frontend/tests/unit/auditActionFormat.mjs
```

Regression check on existing suites: `tests/e2e/test_audit.py`, `tests/e2e/test_license.py`,
`tests/unit/test_permissions_registry.py`, `tests/unit/test_changelog.py` → **65 passed**. All tests that
import the scheduler (`test_custom_queries`, `test_checkin_service`, `test_scheduled_prompt_cron`,
`e2e/test_agent_checkins`, …) → **183 passed**. Production frontend build (`nuxt build`) completes.

## Loop B — live stack against the mock SIEM consumer

The stack runs the production way: `uvicorn main:app --workers 2`, `BOW_AUDIT_STREAM_INTERVAL_SECONDS=5`, and a pinned `BOW_ENCRYPTION_KEY`. The mock (`tools/agent/mock_siem_consumer.py`) emulates every intake and injects faults. The driver (`tools/agent/audit_streams_loop.py`) creates one stream per destination through the API, generates events through real audited actions, and checks the mock against the database.

```bash
cd backend
uv run uvicorn main:app --port 8000 --workers 2 &          # TESTING=true, BOW_ENCRYPTION_KEY pinned
uv run python ../tools/agent/mock_siem_consumer.py --state-dir /tmp/siem-mock &
uv run python ../tools/agent/audit_streams_loop.py --scenario all \
  --ca-cert /tmp/siem-mock/tls/cert.pem --restart-cmd "<restart the backend>"
```

Final run: **27/27 checks passed** (`media/pr/audit-log-streams/loop-b-results.json`). The sandbox database had accumulated 73k events by then, so every stream also backfilled its full history.

| Scenario | What happens | Result |
|---|---|---|
| B1 happy path | 7 streams created with "include all history"; 200 mixed events through the API | every destination received all **73,440** DB events: 0 missing, 0 duplicates, 0 format errors |
| B2 transient | Splunk 503 × 5, Datadog 429 × 3, then 50 events | both converged (73,515), stayed `active`, failures back to 0 |
| B3 invalid → fixed | Sentinel intake 401 and the client secret rotated in Entra; 30 events | stream moved to `invalid` ("token endpoint HTTP 401"), `audit_stream.state_changed` audited; after PATCH with the new secret and `active`, all 73,562 events arrived |
| B4 burst | 10,000 concurrent `log_tool_audit` calls through the real queue in a second process on the same SQLite DB, with the exporter and 2 workers running | `dropped=0`, rows=10,000 (6,200 queued, 3,800 written inline under backpressure); every destination converged to **83,562**, 0 missing, 0 duplicates |
| B5 restart mid-batch | HTTPS intake made to hang, backend killed while the request was in flight | after restart and the 120 s stream lease, all 83,577 events arrived; 0 duplicates |

Earlier runs of the same loop are what exposed E2 and S1:

| Run | B4 outcome | Cause, then fix |
|---|---|---|
| 1 | Sentinel missing all; queue stats unreadable | the mock kept B3's rotated secret, and the driver parsed the wrong output line (harness fixes) |
| 2 | every stream exactly ~4,500 behind; cursors past the pending rows | E2: enqueue-time `created_at` → stamp at write time |
| 3 | ~4,940 behind at a 10 s lag | E2: lock waits longer than the window → `export_seq` |
| 4–5 | exactly 5,000 behind, then caught up later | S1: the scheduler died on "database is locked"; plus the stamper's lease release failing → resilient scheduler + retried bookkeeping |
| 6 | **all pass** | the log shows 5 recovered "Scheduler pass failed" events: the fix working live |

Things Loop B surfaced:

- **Stream secrets need a stable `BOW_ENCRYPTION_KEY`, exactly like connection credentials.** The first B5 run restarted a backend that had no pinned key. Every stream with stored secrets moved to `invalid` with "stored secrets could not be decrypted (encryption key changed?)", the transition was audited, and admins were emailed. Syslog (no secrets) kept delivering. This is the intended behaviour: nothing is lost, and delivery resumes from the cursor once the secret is re-entered. Deployments get the key from `start.sh`.
- **The backend must run the production way for a live loop.** `python main.py` auto-reloads on writes under `backend/app` (pytest's `__pycache__` included). After a reload, an orphaned multiprocessing child kept the scheduler leader lock, so no scheduled job (including the exporter) ran. This is a pre-existing issue, filed as its own task.
- **Throughput.** The first B1 run backfilled 20k historical events per stream at 5,000 per stream per tick, with streams run sequentially. The exporter now runs up to 4 streams concurrently, with a 20 s time budget per stream (10 s for stamping) instead of a fixed batch count. Profiling showed the raw pipeline (fetch + envelope + send) at about 6,000 events/s per stream.
- **APScheduler fires the tick in every worker** (shared job store, as `scheduler.py` already notes). The compare-and-set leases (one for stamping, one per stream) make that safe; Loop B ran on 2 workers with 0 duplicates.

## UI — Playwright flow as an org admin (`tools/agent/audit_ui_flow.mjs`)

`node ../tools/agent/audit_ui_flow.mjs ../media/pr/audit-log-streams --phase after --locale en|he` passed **12/12 checks in `en` and 12/12 in `he`**:

- rows render with no action cell overflowing without a tooltip;
- the drawer opens and shows the tool queries and the user-agent field;
- the resource and time filters land in the URL and survive a reload;
- search matches a user's email;
- export downloads envelope-v1 NDJSON;
- the Streams tab: add an HTTPS stream, **Send test event** reports "delivered", save, and the stream is listed.

The scheduler then delivered to that UI-created stream (mock stats: `received=1, duplicates=0`).

Screenshots and flow GIFs (`after-en-flow.gif`, `after-he-flow.gif`) are in `media/pr/audit-log-streams/`. `before-*` is the old page on the same seeded event shapes; `after-*` is the new one, on a fresh sandbox org with 7 live streams (one `invalid`, one retrying).

## What this proves / regression notes

- No event with an organization is dropped by the tool-audit path (A0, B4). The 10k burst through the real queue wrote every row.
- Every active stream receives every committed event in the database, however late it commits. Duplicates can only follow a crash mid-send and always carry the same `id` (B1–B5, A3).
- Fixes outside the feature, both found by these loops: the APScheduler wakeup crash (S1), and a separately filed task for the scheduler leader lock held by orphaned multiprocessing children.
- Pre-existing, unrelated: the locale catalogs were already out of sync before this change (`es` missing 31 keys, `he` 11, the other seven 176). This change adds its 165 keys to all 10 catalogs, so the drift is unchanged.
