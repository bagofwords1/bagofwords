# Feedback Loop — durable eval run-finished wake

The eval run-finished wake (the hook that resumes the origin chat when an
agent-started run completes) was dispatched as `asyncio.create_task(...)` —
fire-and-forget, in-process. If the worker restarted in the window between the
run finalizing and the wake completion being written, **the notification was
silently lost and nothing retried it**. Ironically the general `wait` tool was
already durable (APScheduler jobstore + misfire grace); the newer eval wake
traded that away for immediacy.

## Change

Dispatch the wake as a one-shot APScheduler `date` job on the shared jobstore —
the exact pattern `WaitService.run_wait_wake` uses — instead of an in-process
task. **No recurring sweep** (that would poll for a rare event and be empty
99.9% of the time); the jobstore *is* the durable queue, and a job exists only
when there's an actual wake to deliver.

- Module-level `run_eval_wake(job_id, run_id)` — the scheduler callable
  (serializable by import path); claims the fire cross-worker
  (`claim_scheduled_run`) then delegates to `_fire_eval_wake`.
- `_schedule_eval_wake(run_id)` — `scheduler.add_job(trigger="date",
  run_date=now, id="eval_wake:<run_id>", replace_existing=True,
  misfire_grace_time=3600)`. Deterministic id collapses duplicate schedules
  (finalizer + a live `stream_run` client) to one job; misfire grace recovers a
  job whose worker died before it ran.
- The four dispatch sites (finalizer, `stop_run`, two `stream_run` paths) now
  call `_schedule_eval_wake` when `wake_on_finish` is set, instead of
  `asyncio.create_task(_maybe_fire_wake)`.
- **Ordering fix:** `_fire_eval_wake` creates the wake completion FIRST and
  clears `wake_on_finish` only AFTER it succeeds. A crash between the two
  re-fires (flag still set) rather than dropping — at-least-once delivery, with
  duplicates absorbed by the existing "already handled, acknowledge and stop"
  wake-prompt guidance. (Previously the flag was cleared *before* the
  completion, so a crash mid-fire lost the wake permanently.)

UI is unchanged: still `run_machine_turn` → the same `role='external'` event
strip. Durability only changes *how* the wake is dispatched, not what it renders.

## Verification

- `backend/tests/unit/test_eval_wake_dispatch.py` — deterministic job id;
  `_schedule_eval_wake` registers a durable one-shot job with the right kwargs
  and never raises on a scheduler error; `run_eval_wake` claims then delivers,
  and skips when the cross-worker claim is lost. (5 tests; eval+wait unit set:
  52 passed.)
- Integration smoke (no live agent — `run_machine_turn` stubbed so the
  credit-limited sandbox agent isn't needed):
  1. the **real** scheduler accepts the job (`misfire_grace_time=3600`);
  2. `_fire_eval_wake` delivers exactly once with the right payload
     (`trigger_source=eval_run`, meta counts, `mode=training`);
  3. `wake_on_finish` flips to False only after delivery, so a second call
     no-ops (idempotent).

## Notes

- Guaranteed-delivery now matches the `wait` tool. A worker crash between
  "run finished" and "chat notified" recovers on restart instead of dropping.
- Deliberately out of scope: baking per-case results into the event strip so
  the outcome survives a *dead agent turn* (today detail comes from the agent's
  `get_eval_run` follow-up). Separate enhancement.
