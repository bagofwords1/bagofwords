# Feedback loop — accepted work stranded by a dead worker

A stored status or an APScheduler timer does not preserve the coroutine that
executes it. This change gives accepted machine deliveries a durable receipt,
and independently retires abandoned domain runs.

## Validated root causes

- `agent_reliability_service.py:_already_running` previously treated any
  running automation row as live indefinitely.
- `test_run_service.py:start_run_background` returned matching in-progress
  evaluations before stale-run handling.
- APScheduler removes date jobs at submission, before their coroutine completes.
  The preceding audit reproduced this with a disposable subprocess, SQLite
  jobstore, and SIGKILL after submission.
- Webhook acceptance and sub-agent fan-in previously relied on local tasks.

The original audit reproduced the first three mechanisms. The regression tests
here seed persisted crash states, because normal HTTP endpoints cannot produce
a dead owner's lease. They do not kill a deployed application worker.

## Recovery contract

| Work | Recovery |
|---|---|
| Accepted webhook, wait, scheduled prompt, evaluation wake, sub-agent fan-in | Claim a receipt using a conditional update; retry expired pre-agent delivery, at most three attempts |
| Delivery that committed an agent completion | Mark failed; do not replay possible tool side effects |
| Automation, evaluation, metadata indexing | Heartbeat every 30 seconds; after 15 minutes without progress, mark interrupted work terminal |
| Review workflow | Reopen the item and mark its running entries failed |
| Abandoned top-level completion | Use the existing completion stale threshold; recover independently of a new request |
| Queued chat | Dispatch after success or recovered worker loss; ordinary errors and explicit stops retain pause behavior |
| Sub-agent deadline | Resume child cleanup even if the previous worker already marked the wave timed out |

Receipts have five-minute renewable ownership leases. Two consumers run per
application worker; an independent recovery loop runs every 30 seconds. These
are detection intervals, not a delivery latency guarantee when workers are busy.

`background_jobs.enqueue` participates in the caller's transaction. New machine
turns call `checkpoint_event`, then `checkpoint_completion` in the transaction
that creates the agent completion. The latter checks ownership and expiry before
recording the no-replay boundary. Descendant agent work does not inherit this
single-start fence. A stale owner cannot create a second root completion.

One-shot schedules persist their receipt before registering their timer.
Recurring schedules enqueue when they fire; this does not backfill cron
occurrences missed during an outage. Result wakes and deadline/fan-in intents
can also be reconstructed from domain rows.

## Run the regression loop

From a fresh checkout, install the backend development dependencies with Python
3.13, then:

```bash
cd backend
export TESTING=true
export BOW_DATABASE_URL=sqlite:///db/test_bootstrap.db
uv run pytest   tests/unit/test_background_job_recovery.py   tests/unit/test_wait_tool.py   tests/unit/test_scheduled_prompt_cron.py   tests/e2e/test_worker_recovery.py   tests/e2e/test_agent_reliability_loop.py   tests/e2e/test_wait_wake_deprovisioned.py   tests/e2e/test_scheduled_prompt_one_time.py   tests/e2e/test_sub_agents.py   tests/e2e/test_completion_queue_steer.py   tests/e2e/test_webhook_error_disclosure.py --db=sqlite -q
```

Repeat with `--db=postgres` to use the suite's disposable PostgreSQL container.
A pre-set `TEST_DATABASE_URL` can point to an isolated test PostgreSQL server.
Never point this suite at application data: the fixtures reset its database.

The tests cover fresh versus expired rows, multiple stale ages, preserved
completed results, duplicate receipts, competing consumers, future scheduling,
rollback, cancellation after timer removal, ownership replacement, and partial
deadline cleanup. API fixtures create ordinary entities; only crash state is
seeded directly.

### Negative control

In a disposable checkout, temporarily return immediately from
`BackgroundWorker.recover`, `recover_rows`, and `checkpoint_completion`.
Run:

```bash
uv run pytest tests/unit/test_background_job_recovery.py   tests/e2e/test_worker_recovery.py   -k 'expired_delivery or recovery_preserves or creation_is_fenced'   --db=sqlite -q
```

Restore those three functions before running the positive suite. Expected
failures are orphan rows remaining active, expired receipts not transitioning,
and invalid delivery owners being allowed to create a completion.

## Rollout and limits

Apply Alembic revision `bgjob01` before new workers start; it follows `grpllm01`
and only adds the receipt table. Existing domain orphans are detected, but an
old in-memory webhook or already-dispatched timer with no durable row cannot be
reconstructed. Drain/restart workers together for this rollout: an old worker
does not supply the new automation/evaluation/indexing heartbeat, so a mixed
deployment lasting over the stale threshold can incorrectly retire its work.

This is bounded recovery, not exactly-once execution of external side effects,
nor continuation from an arbitrary tool instruction. Cancellation is cooperative;
a blocked synchronous operation or external request may outlive its task's
lease. Automation promotion and final statuses have additional database guards.
Interrupted agent turns require a deliberate new attempt.

Failed receipt payloads remain available for diagnosis; successful payloads are
cleared. A retention/administration policy and production throughput sizing are
follow-ups. A standalone webhook crash before its event checkpoint may leave an
empty report, although it has not started an agent yet.

No real external connectors, live email delivery, production load, rolling
upgrade, or full-process application chaos test was exercised. Tests use the
existing provider-boundary fakes. SQLite suites emit deprecation and event-loop
shutdown warnings; passing assertions do not imply warning-free execution.

## Context map

- `backend/app/services/background_jobs.py`: receipt lifecycle and owner fencing.
- `backend/app/services/worker_recovery.py`: heartbeats, terminal recovery, queue repair.
- `backend/main.py`: per-worker startup/shutdown.
- `backend/app/services/machine_turn.py`, `completion_service.py`: event and agent checkpoints.
- `backend/app/services/sub_agent_service.py`: transactional fan-in intent and resumable deadline cleanup.
- `backend/tests/e2e/test_worker_recovery.py`: domain and ownership contracts.
- `backend/tests/unit/test_background_job_recovery.py`: delivery and cancellation contracts.

Future background work should persist its intent before acknowledging acceptance,
and explicitly choose whether retry is safe before and after its side-effect
boundary. A scheduler registration alone is insufficient.

## Observed verification (2026-10-27)

- Negative control: **8 failed, 2 passed, 9 deselected**, matching the deliberately
  removed recovery/ownership behavior; all changes restored afterward.
- Initial combined SQLite run: **110 passed**.
- Additional partial-deadline regressions plus sub-agent suite: **15 passed**.
- Earlier PostgreSQL recovery/automation/wait/schedule selection: **50 passed**.

Review also found and fixed issues introduced during implementation: a heartbeat
could cancel normally completed automation, a SQLite savepoint could persist a
receipt despite outer rollback, scheduler engine pooling survived test database
replacement, and descendant agent turns could inherit the root receipt's fence.
Tests now cover transaction rollback and descendant creation; the normal
automation and scheduler suites were rerun after those corrections.

Final combined selection: **112 passed on SQLite** (47.91s) and **112 passed
on PostgreSQL 15** (58.49s), including the added partial-deadline cases. Both
runs applied the migration chain. No failing assertions remained in this
selection; the full repository suite was not run.
