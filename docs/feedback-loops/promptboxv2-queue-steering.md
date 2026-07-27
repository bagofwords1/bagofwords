# Feedback Loop — "add prompt to queue if a completion is wip" + "steer, like Codex"

Feature request: while a completion is running, PromptBoxV2 should let the user
(1) **queue** a follow-up prompt that runs automatically when the current turn
finishes, and (2) **steer** — inject a message into the *running* completion so
the agent incorporates it mid-run, Codex-style. This loop validates both,
before (gap) and after (implemented).

## Root cause (validated — the pre-change gap)

- Single-in-flight by UI gating only: `canSubmit` returned `false` while
  `latestInProgressCompletion` was set (`frontend/components/prompt/PromptBoxV2.vue`),
  and a second submit while streaming *aborted* the current run
  (`frontend/pages/reports/[id]/index.vue`, `onSubmitCompletion`).
- No backend queue: `create_completion` / `create_completion_stream`
  (`backend/app/services/completion_service.py`) always started an agent run;
  nothing prevented concurrent runs per report and nothing deferred prompts.
- No mid-run input path: the only interrupt was the cooperative sigkill flag
  (`update_completion_sigkill` → WS broadcast → `AgentV2.sigkill_event`,
  `backend/app/ai/agent_v2.py`).

## The implementation (what changed)

Backend (no DB migration — `queued` is a new value in the existing `status`
string column; steering rows are `role='user'`, `message_type='steering'`,
`parent_id=<running system completion>`):

- `POST /api/reports/{id}/completions` with `queue: true` persists a
  `status='queued'` user row instead of starting a run
  (`CompletionService.create_queued_completion`).
- `DELETE /api/completions/{id}/queued` removes a still-queued row.
- `POST /api/completions/{id}/steer` (`{content}` or
  `{queued_completion_id}` to promote a queued row) persists the steering row
  and signals the running agent; degrades to enqueueing when the run finished.
- Dispatcher `CompletionService.start_next_queued_if_idle` runs from every
  agent-run `finally` (stream, background, foreground, dispatched): drains the
  queue only after a `success` run (error/stopped pauses it), claims the oldest
  queued row with a conditional UPDATE (no double-starts), and runs the agent
  via `_run_dispatched_agent` with a registered event queue so any client can
  attach through the existing watch endpoint.
- `AgentV2` picks up steering at the top of every loop iteration (WS fast path
  + per-iteration DB poll for cross-worker delivery), renders it into the
  planner's `user_message` via `_effective_user_message()`, emits a
  `completion.steering.applied` SSE event, and refuses to finalize over a
  late-arriving steer (re-plans one more iteration instead).
- Queued rows are excluded from the planner's conversation history
  (`message_context_builder.py`) until dispatched.

Frontend: the input stays live during a run; Enter/☰ queues, ⚡ steers; queued
prompts render as chips (remove / steer-now); steered messages get an amber
"Steered" badge; the WS handler attaches to dispatcher-started runs.

## Loop A — deterministic reproduction (no external services)

Agent stubbed at the `AgentV2.main_execution` boundary; routes/services/DB/
dispatcher run real:

```bash
cd backend
TESTING=true BOW_DATABASE_URL="sqlite:///db/test.db" \
  uv run pytest tests/e2e/test_completion_queue_steer.py -q
```

Observed on pre-change code (feature stashed): **4 failed** — queue flag runs
a second concurrent completion, `DELETE .../queued` and `POST .../steer` are
404 (route missing). After the change: **4 passed** — queue-while-running,
pause-on-stop + drain-after-success, steer + queued-row promotion, finished-run
fallback, and target/auth validation.

## Loop B — live confirmation (real Anthropic credentials + real UI)

Full stack + real model (key via env only):

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --sqlite-sources 1
# create an Anthropic provider via POST /api/llm/providers with $ANTHROPIC_KEY
cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node ../tools/agent/verify_queue_steer.mjs
```

Observed: `E2E PASS` — while turn 1 streamed, a second prompt queued (chip),
a steer message landed mid-run; the final turn-1 answer contained the steered
instruction's content ("region with the fewest orders", short form) proving
planner injection; on success the dispatcher auto-ran the queued prompt and the
queue emptied. Stage screenshots: `media/pr/promptboxv2-queue-steering/1..5*.png`.

## What this proves / regression notes

- Queueing never starts a concurrent run; the dispatcher is the only starter
  and claims atomically. Stop pauses the queue by design.
- Steering is delivered in-run (same-worker WS fast path, cross-worker DB poll)
  and demonstrably alters the in-flight answer.
- Pre-existing, unrelated: locale key drift between `en` and `es`/`he`
  (reproduces on `main` with this change stashed); the broken
  `POST /api/llm/models` route (`LLMService.create_model` missing).
- Harness note: the sync `TestClient` kills request-spawned asyncio tasks when
  each request's loop closes — dispatcher chains can't be observed through
  HTTP in tests, hence `_drive_dispatcher` in the test file calls the public
  service method directly.
