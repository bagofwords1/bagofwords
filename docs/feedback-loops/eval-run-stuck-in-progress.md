# Feedback Loop — eval run "stuck on in_progress" until you open the run page

A background-executed eval run (via **/api/tests/runs/batch** and the
`run_eval` agent tool) would sit at `status = in_progress` on the evals list
forever. Only when someone **opened the run detail page** did the status flip
(to `fail`/`error`) and the run finish — because the deferred evaluation ran
at that moment. This loop reproduces the stuck state deterministically with a
stub LLM (no external services) and shows the run finalizing on its own after
the fix.

## Root cause (validated)

`create_and_execute_background` dispatches the agent in the background but
never evaluates the assertions or finalizes the run
(`backend/app/services/test_run_service.py:566`). All evaluation +
finalization lived **only inside `stream_run`** — the SSE endpoint the run
page opens (`backend/app/services/test_run_service.py:861`). So after the
background agent's system completion reached a terminal state, the
`TestResult` stayed `in_progress` and the `TestRun` stayed `in_progress`
until a client hit `POST /tests/runs/{id}/stream`.

Secondary defect: even when `stream_run` did finalize, it aggregated run
status off the **request session's** identity map, which still held the
pre-evaluation `in_progress` copies of the results (they were persisted in
separate sessions). A run could therefore persist `status=success` while a
result was `fail` (`test_run_service.py:1518`, pre-fix).

## Loop A — deterministic reproduction (no external services)

Boundaries stubbed: the LLM is `tools/agent/stub_llm.py` (OpenAI-compatible),
wired as the org default model. No data sources. The case asserts the
completion text contains a phrase the stub's canned answer never produces, so
the case deterministically resolves to `fail` once evaluated.

### Environment

```bash
cd backend
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"   # config load
# stub LLM (empty sources -> agent produces a one-shot answer and finishes):
echo "[]" > /tmp/stub_sources.json
STUB_SOURCES_FILE=/tmp/stub_sources.json STUB_PHASES="" STUB_PORT=9099 \
  uv run python ../tools/agent/stub_llm.py &
```

Boot the backend (any DB) and seed an org:

```bash
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL="sqlite:///db/repro.db" \
  uv run alembic upgrade head
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL="sqlite:///db/repro.db" \
  uv run python main.py &
uv run python ../tools/agent/seed_org.py
```

### Reproduce

`tools/agent/repro_eval_stuck.py` wires the stub as default, creates a
one-case suite, POSTs `/api/tests/runs/batch`, then polls the **status**
endpoint (never `/stream`) until the agent's system completion is terminal,
and prints the run/result status the backend left behind.

```bash
cd backend && uv run python ../tools/agent/repro_eval_stuck.py
```

**Observed on buggy code (pre-fix):**

```json
{"run_status": "in_progress", "result_statuses": ["in_progress"], ...}
```

The agent finished, yet the run never left `in_progress`. In the UI, the run
sits at **In progress** on the evals list — 6m 5s elapsed and counting,
0/1 results — with nothing ever finalizing it:

![before](https://raw.githubusercontent.com/bagofwords1/bagofwords/claude/eval-status-stuck-progress-xyrnrt/media/pr/eval-status-stuck/before-runs.png)

### Regression test

`backend/tests/e2e/test_eval_run_finalize.py` covers the finalization
aggregate directly (no LLM), including the stale-session case
(`fail` → run `error`, not `success`), stopped-preservation, idempotency, and
the no-op-while-in-progress guard:

```bash
BOW_DATABASE_URL="sqlite:///db/app.db" \
  python -m pytest tests/e2e/test_eval_run_finalize.py -v   # 7 passed
```

## The fix

`backend/app/services/test_run_service.py`:

- **`_finalize_result_background`** — a per-result task spawned (after the run
  commits) by `create_and_execute_background`. It waits for the agent, runs
  any additional turns, evaluates assertions, persists the `TestResult`, and
  finalizes the run. Replaces `_run_additional_turns_background` and now
  covers single-turn cases too, so the run is self-finalizing on the server.
- **`_evaluate_and_persist_result`** — mirrors `stream_run`'s evaluation,
  best-effort with an error fallback so a result always leaves `in_progress`.
- **`_maybe_finalize_run`** — session-safe run aggregate. Runs in its own
  session, so it reads freshly-committed results instead of a stale identity
  map (fixes the `success`-while-`fail` bug). Idempotent and concurrency-safe.
- `stream_run` now finalizes via `_maybe_finalize_run` and re-reads the
  authoritative status for the `run.finished` event.

**Observed on fixed code (same harness, only `/status` polled):**

```json
{"run_status": "error", "result_statuses": ["fail"], ...}
```

The run finalizes itself — no page open required. The evals list shows the run
**Failed**, 0/1, 3s:

![after](https://raw.githubusercontent.com/bagofwords1/bagofwords/claude/eval-status-stuck-progress-xyrnrt/media/pr/eval-status-stuck/after-runs.png)

| Before (buggy) | After (fixed) |
|----------------|---------------|
| Status **In progress**, 6m 5s, 0/1 — stuck until the run page is opened | Status **Failed**, 3s, 0/1 — finalized server-side, page never opened |

## What this proves / regression notes

- The run reaches a terminal state **without any client opening the run
  page** — the harness only ever calls `GET /status`, never
  `POST /stream`. Before the fix that same sequence left the run
  `in_progress` indefinitely.
- The persisted run status is correct (`error`, matching the `fail` result),
  confirming the stale-session aggregate fix.
- The `POST /api/tests/runs` (`create_run`) path is intentionally left as-is:
  it creates `init` results and is stream-driven by design (nothing executes
  until viewed) — a separate flow, out of scope here.

### Harness scripts (added under `tools/agent/`)

- `repro_eval_stuck.py` — API-level reproduction (wire stub, run batch, poll
  status, print result).
- `login_and_capture.mjs` — Playwright UI login + screenshot (skips
  onboarding; `--click` a tab; points at the pre-provisioned
  `/opt/pw-browsers` chromium).
- `restart_backend.sh` — restart only the backend on a chosen sqlite DB (the
  built frontend proxies `/api` → :8000, so before/after legs swap the
  backend while the UI stays up).
