# Feedback Loop — "wait N minutes, then try again"

Adds a `wait` tool to the Agent V2 harness (prompt builder v3). The agent calls
it when the only sensible next step is to let real-world time pass and then
retry — a data refresh / ETL still running, a rate limit, "try again in 30
minutes". The tool **ends the current turn** (like `clarify`) and arms a
**one-shot** resume; when the delay elapses the agent auto-resumes on the same
report with full history and continues where it left off.

This is deliberately **not** a scheduled task: one-shot (not recurring), no
user-visible `ScheduledPrompt` row (the only record is the `wait` tool
execution, which the UI already renders), and it self-deletes after firing once.

## What was built

Backend:
- `app/ai/tools/schemas/wait.py` — `WaitInput` (`delay_minutes` 1..1440 in
  MINUTES; `reason` — a self-contained instruction for the resume) + `WaitOutput`.
- `app/ai/tools/implementations/wait.py` — `WaitTool` (`category="action"`,
  auto-registered by `app/ai/registry.py`). Arms the wait, then ends the turn by
  emitting an observation with `analysis_complete=True` + a `final_answer`
  (the exact terminal path `clarify` uses — `agent_v2.py:3122`).
- `app/services/wait_service.py` — arms a one-shot APScheduler `date` job on the
  shared scheduler (`app/core/scheduler.py`) and cancels it. On fire,
  `run_wait_wake` claims the run (cross-worker dedup) and creates a fresh
  completion on the report with the pause `reason` as the prompt — mirroring
  `ScheduledPromptService.scheduled_run_prompt`.
  - The job callable is a **module-level function**, not a bound method:
    APScheduler's `SQLAlchemyJobStore` serializes the callable by import path, and
    a bound method deserializes without `self` and would crash on fire after a
    restart. Verified by `obj_to_ref`/`ref_to_obj` round-trip identity.
- `app/services/completion_service.py::cancel_wait` +
  `POST /api/completions/{completion_id}/tool_executions/{tool_execution_id}/cancel_wait`
  — remove the pending job and flip the tool execution's `result_json.status` to
  `cancelled`. Same initiator/owner guard as `clarify_response`. Idempotent.
- `app/ai/agents/planner/prompt_builder_v3.py` — one PLAN TYPE GUIDANCE line
  routing pause-and-retry to `wait` and steering recurring asks to
  `create_scheduled_task`.

Frontend:
- `frontend/components/tools/WaitTool.vue` — a small, minimal pill: clock +
  "Waiting" + a **live MM:SS / H:MM:SS countdown** to `result_json.wake_at` +a
  right-aligned **✕ to cancel**. Cancel POSTs to `cancel_wait` (optimistically
  stops the countdown) and the pill flips to a muted "Wait cancelled". When the
  countdown hits zero it shows a spinning "Resuming…".
- Wired into `getToolComponent` in `pages/reports/[id]/index.vue`; `tools.wait.*`
  i18n keys added to all `locales/*.json`.

Tests:
- `backend/tests/unit/test_wait_tool.py` — **16 passing**: input bounds,
  context guard, happy path emits `scheduled` output + a terminal
  (`analysis_complete`) observation, the delay-phrasing helper, `WaitService`
  arms a one-shot `date` job and cancels it, and the wake callable is a
  module-level serializable function.

## Loop A — deterministic (unit)

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/test_wait.db" TEST_DATABASE_URL="sqlite:///db/test_wait.db"
uv run pytest tests/unit/test_wait_tool.py -q
# -> 16 passed
```

## Loop B — live agent end-to-end (real scheduler fire)

Boot the stack + seed the **Music Store** (chinook) demo, configure an Anthropic
model (`ANTHROPIC_API_KEY` via env only — never commit it), then drive the
test case over HTTP:

> "Create a data widget with the total number of tracks in the music store.
> Then wait 1 minute and create that same widget again."

Observed (inspecting `db/agent.db`):

```
tool_executions for the report:
  create_data  success
  wait         success   status=scheduled  wake_at=+1min  job=wait:<report>:<token>
apscheduler_jobs: … wait:<report>:<token>        # one-shot job persisted

# ~40s later the scheduler fires the job on its own:
completions 2 -> 4        # a new resume turn appears (no user action)
steps       1 -> 3        # the widget is created AGAIN

resumed turn tools:
  turn 2 user   = "[Automatic resume after a scheduled wait] … Resume the task now: …"
  turn 3 system = create_data (success)
```

The resumed agent's own message: *"After the 1-minute wait elapsed, I
successfully created the total tracks widget again … Both widgets have now been
created … The task is complete."*

UI evidence (Playwright, `media/pr/agent-v2-wait-tool-sjmzvu/`):
- `wait-countdown.png` / `wait-pill.png` — the live pill: `🕐 Waiting 27:27 ✕`.
- `wait-cancelled.png` — after clicking ✕: muted `✕ Wait cancelled`; the backend
  job is removed (`apscheduler_jobs` no longer lists it) and
  `result_json.status == "cancelled"`.
- `wait-resume.png` — the report where the wait fired: the second widget +
  "created … again after the 1-minute wait", two queries in the panel.

## What this proves

- The agent picks `wait` for pause-and-retry and the turn ends cleanly.
- A real, persisted, one-shot APScheduler job fires without any user action and
  re-enters the agent loop on the same report with full context.
- Cancel removes the job and updates the UI; no orphaned schedule remains.
- No recurring `ScheduledPrompt` is created — `wait` stays distinct from
  `create_scheduled_task`.

## Notes / follow-ups

- No cap on wait→wake→fail→wait chains yet (per product decision). `attempt` is
  already threaded through the job args, so a cap can be added later without a
  migration.
- A pending wait still fires even if the user sends a new message before it
  elapses (jobstore-only state, no cancel-on-new-message). Harmless today (the
  wake just adds a turn with full context); revisit if it proves noisy.
- Disambiguation vs `create_scheduled_task`: early on, Haiku sometimes *also*
  spun a `create_scheduled_task` for "wait 30 min and do X again" (a standing job
  the user never asked for). Fixed by adding cross-reference clauses to BOTH tool
  descriptions — `create_scheduled_task` now says "for a one-time pause-and-retry
  use `wait`, NOT this tool", and `wait` says "put the 'do it again' action in
  `reason` — you do NOT also need a scheduled task". After the fix, the same
  prompt produced `create_data` + `wait` with **0/4** stray scheduled tasks on
  Haiku.
