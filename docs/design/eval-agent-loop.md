# Eval agent loop — design (phases 1, 3, 4)

Target workflow: a user creates an agent, adds instructions, sets up evals,
runs them, checks results, updates instructions, reruns, and iterates to
green — driven either from chat (training mode) or from the UI. This is CI
for agents: instruction builds are the commits, eval cases are the tests,
`TestRun` is the CI run.

This document designs the missing interfaces. It deliberately **excludes**
the agent↔case schema refactor (association table replacing
`TestCase.data_source_ids_json`) — that is "phase 2" and everything below is
designed to work without it.

## What already exists (do not rebuild)

| Capability | Where |
|---|---|
| Background run execution | `TestRunService.create_and_execute_background` |
| Build pinning (evals test the *staged* instructions) | `run_eval` resolves `data.build_id or runtime_ctx['training_build_id']`; `TestRun.build_id` |
| Stop a run — service, API, UI button | `TestRunService.stop_run`, `POST /api/tests/runs/{id}/stop`, `evals/runs/[id].vue` |
| Run status / results / transcript APIs | `GET /runs/{id}/status`, `/runs/{id}/results`, `/runs/batch`, `/runs/{id}/stream` |
| Live run streaming to the UI | `TestRunService.stream_run` (SSE over POST) |
| One-shot pause/resume for the agent | `wait` tool + `WaitService` (APScheduler `date` job, cross-worker claim) |
| Cancel a wait — service, API, UI button | `WaitService.cancel_wait`, `POST /api/completions/{cid}/tool_executions/{tid}/cancel_wait`, `WaitTool.vue` |
| Instruction-change → auto-run affected evals | `AgentReliabilityService.schedule_for_build` → `run_automation` (trigger `instruction_change`), incl. gating, self-heal iterations, promote/pend |
| Agent-scoped eval selection (JSON-based) | `AgentReliabilityService.list_agent_eval_case_ids` |
| Eval tool digests in conversation history | `_digest_eval_tool` in `message_context_builder.py` |

The recurring theme: engines exist; the gaps are thin interfaces —
mostly agent tools — plus one genuinely new mechanism (run-completion
wake-up) and one comparison view.

---

## Phase 1 — chat-driven loop, end to end

### 1.1 `get_eval_runs` + `get_eval_run` tools (new)

Two research tools — a cheap list and a detailed read. Splitting them
keeps each catalog description crisp and matches how the planner thinks
("what's running?" vs. "how did run X do?").

**`get_eval_runs`** — list mode, handles the many-runs case.

- **Input**: `{ status?: "in_progress"|"success"|"error"|"stopped"|"all"
  = "all", limit: int = 10 }`.
- **Output**: most recent runs first — `{run_id, title, status,
  trigger_reason, build_number, started_at, finished_at, counts}` (counts
  from `summary_json`, or live `TestResult` tallies for in-progress
  runs). This is "list running evals" for the agent, and it surfaces
  reliability-automation runs too (`trigger_reason="automation:*"`).
- Deliberately summary-only: no per-case results, so a long history stays
  cheap in context.

**`get_eval_run`** — detail mode.

- **Input**: `{ run_id: str, compare_to_previous: bool = false }`.
- **Output**: run status, `build_id`/`build_number`, `trigger_reason`,
  started/finished timestamps, counts (`total/finished/passed/failed`),
  and per-case results `{case_id, case_name, status, failure_reason}`.
  With `compare_to_previous` → additionally returns flips vs. the
  comparison base (see §3.1); this is how the agent answers "did my
  instruction change fix it?" in one call.
- **Live-run semantics**: returns a snapshot with `finished/total`; the
  agent compares counts across calls. No streaming needed.

Shared metadata for both: `category="research"`, `idempotent=True`,
`allowed_modes=["training"]` (matching `run_eval`; knowledge mode has no
business reading runs), `required_permissions=["manage_evals"]`.
Implementation reuse: `TestRunService.get_run`, `list_runs`,
`list_results` — no new service code beyond the compare helper (§3.1).
Context: extend `_digest_eval_tool` to digest both (mirrors the existing
`run_eval` digest: counts + up to 3 failure reasons).

### 1.2 Background-by-default `run_eval` (modify)

`run_eval` becomes **fire-and-forget by default**: it creates the run,
emits a start event with the case list, and ends immediately with
`{status: "in_progress", run_id, total}`. The run execution engine is
untouched. Three parts:

**(a) Default = background.** New input field `wait_s: int = 0`
(clamped to `[0, 600]`). Default `0` = kick off and return immediately.
The agent (or the planner, for a one-case smoke check) can opt into a
short inline wait — with `wait_s > 0`, today's behavior applies within
the budget: live per-case progress streamed to chat, inline results if
the run finishes in time.

- Deadline passes and the run is not terminal → emit
  `tool.progress {kind: "eval.run_detached", ...}` then `tool.end` with
  output `{success: true, status: "in_progress", run_id, total, finished,
  passed, failed, message: "Run executing in background; check with
  get_eval_run(run_id=...) — use the wait tool for longer gaps."}`.
  **Do not** call `stop_run` (unlike today's hard-timeout path — detach
  is not a failure).
- The existing `_PER_CASE_TIMEOUT_S`/`_MAX_TIMEOUT_S` deadline logic and
  its `stop_run` cascade are removed from the *tool*; runaway-run
  protection belongs to the run engine / org concurrency caps (§3.4), not
  to whichever tool happens to be watching.
- Sigkill cascade applies only while attached (`wait_s > 0`). A
  background run keeps executing server-side; stopping it is an explicit
  action (UI button or `stop_eval_run`, §1.5). Document this in the tool
  description.
- Output schema: `RunEvalOutput` gains `detached: bool`;
  `success` keeps meaning "the tool did its job" (kicked off and reported
  truthfully), while `status` carries the run state.

**(b) Heartbeat (for attached waits).** The poll loop only emits events
on case-status transitions, so a quiet case >180s trips `ToolRunner`'s
idle timeout (`TimeoutPolicy(idle_timeout_s=180)` in `agent_v2.py`),
which kills the generator, *retries the tool* (launching a duplicate
TestRun), and orphans the first run. Fix: every ~30s of no transitions,
emit `tool.progress {kind: "eval.heartbeat", run_id, finished_so_far,
total}`. The frontend ignores unknown kinds; ToolRunner's idle timer
resets. Only relevant when `wait_s > 180`, but harmless always.

**(c) Chat card follows the run, not the tool.** With background as the
default, the chat tool card can no longer rely on `tool.progress` events
for live pass/fail ticking. The card renders the start state ("Run
started — N cases") with a link to `/evals/runs/{id}`, and switches to
the existing run-level streaming/polling (`/runs/{id}/status` or
`/runs/{id}/stream`) for live counts — the same source the run detail
page already uses. This is a frontend change to the run_eval tool card
component only.

**Planner guidance (description text)**: "The run executes in the
background: continue with other work or call `get_eval_run` later; for
waits longer than a few minutes use the `wait` tool with a reason like
'check eval run <id> via get_eval_run and report the results'. A
completion wake-up will notify this conversation when the run finishes
(§4.1), so prefer not to arm a wait for the same run."

### 1.3 `cancel_wait` tool (new)

- **Input**: `{ job_id?: str }`. Omitted → cancel **all** pending waits on
  the current report. Job ids are already namespaced
  `wait:{report_id}:{token}`, so "all for this report" is a prefix scan.
- **New service helpers**: `WaitService.list_waits(report_id) ->
  [{job_id, wake_at, reason}]` (scheduler `get_jobs()` + prefix filter)
  and `WaitService.cancel_waits_for_report(report_id)`. `list_waits` also
  gives pending-wait visibility for future context injection.
- **Consistency with the UI card**: after removing the scheduler job(s),
  stamp `status: "cancelled"` into the matching `wait` ToolExecution's
  `result_json` (locate by `result_json.job_id`), same as
  `CompletionService.cancel_wait` does, so `WaitTool.vue` stops showing a
  live countdown.
- **Metadata**: `category="action"`, no mode restriction (same
  availability as `wait`), `required_permissions=[]`, `idempotent=True`
  (cancelling nothing is success, mirroring `WaitService.cancel_wait`).
- **Prereq fix**: include the `job_id` in the `wait` tool's observation
  summary (today only `result_json` has it, and the summary is what
  reaches the planner's history) — one line in `wait.py`.

### 1.4 Guidance (three one-liners, no new prompt surface)

1. **Wake prompt template** (`run_wait_wake`, server-side): append
   "If conversation history shows this was already handled or
   superseded, acknowledge briefly and stop — do not redo the work."
2. **`cancel_wait` description**: "Use when the user asks to stop
   waiting/checking, or when a newer task supersedes the pending
   wake-up."
3. **`wait` description**: mention that pending waits can be cancelled
   with `cancel_wait`.

### 1.5 `stop_eval_run` tool (new)

Thin wrapper over `TestRunService.stop_run` (API + UI button already
exist). Input `{run_id}`; `allowed_modes=["training"]`;
`required_permissions=["manage_evals"]`; output mirrors the run's final
counts. Used when the user says "stop that run" in chat, and by the agent
when a detached run is superseded by a new instruction edit.

**Phase 1 exit criterion**: in one chat session — create eval → `run_eval`
(returns immediately) → `wait 10m` → auto-resume → `get_eval_run` → edit
instructions → `run_eval` (pinned to new build) → `get_eval_run
compare_to_previous=true` → report fixed/regressed. No UI required.

---

## Phase 3 — quality of the loop

### 3.1 Build-over-build comparison

**Service**: `TestRunService.compare_runs(db, org_id, user, run_id,
against_run_id=None)`.

- Default base (`against_run_id` omitted): the most recent *terminal* run
  before this one that shares ≥1 case with it (found via `TestResult`
  case sets — no schema change needed).
- Response:
  ```
  { base_run: {id, build_number, finished_at, counts},
    against_run: {...},
    cases: [{case_id, case_name, base_status, against_status,
             flip: "fixed" | "regressed" | "same" | "added" | "removed"}],
    summary: {fixed, regressed, same, added, removed} }
  ```
  (`fixed` = fail/error → pass; `regressed` = pass → fail/error;
  `added`/`removed` = case present in only one run.)

**API**: `GET /api/tests/runs/{run_id}/compare?against_run_id=...`.

**UI** (`evals/runs/[id].vue`): a "Compare to" select listing prior
overlapping runs labeled by build number + date (default preselected);
summary chips ("3 fixed, 1 regressed"); per-row flip badge next to the
existing status pill. Read-only, no state changes.

**Agent**: exposed via `get_eval_run(compare_to_previous=true)` (§1.1) —
same service call, no separate tool.

### 3.2 `edit_eval` tool (new)

- **Input**: `{ case_id, name?, status? ("active"|"draft"|"archived"),
  expectations?, tags?, suite_id?, prompt? }` — partial update; only
  provided fields change. Promotion (`draft → active`) is the headline
  use-case since the knowledge harness only produces drafts.
- **Authorization**: same dual path as `create_eval` — org-level
  `manage_evals`, or per-data-source `manage_evals` on **every** agent the
  case is scoped to (from `data_source_ids_json`).
- **Guardrails**: `archived` is the delete-equivalent (results keep their
  FK target); editing `prompt.content` or `expectations` on a case with
  prior results is allowed but the description warns it invalidates
  history comparisons ("prefer creating a new case when the prompt's
  meaning changes").
- **Mode**: `allowed_modes=["training"]`. Knowledge mode stays
  append-only (drafts) by design — the harness must not mutate or promote
  existing cases.
- Digest added to `_digest_eval_tool`.

### 3.3 `search_evals` improvements (modify)

- Widen the substring match: `name` OR `prompt_json::text` OR
  `expectations_json::text` OR `tags_json::text` (same portable
  cast-to-text ILIKE pattern already used for `prompt_json`).
- Enrich each result item: `judge_excerpt` (first judge rule's prompt,
  ~200 chars), `rule_types` (e.g. `["tool.calls", "judge"]`), and
  `data_source_ids` — so the agent can make an informed dedupe decision
  without another lookup.
- Optional `data_source_id` filter param, implemented as a portable text
  match on `data_source_ids_json` (`LIKE '%"<id>"%'`). Interim until
  phase 2 makes this a join.

### 3.4 Run hygiene

- **Dedupe**: in `create_and_execute_background`, before creating a run —
  if an `in_progress` run exists with the same `build_id` (nullable-equal)
  and the same case set (compare via that run's `TestResult` case ids),
  return the existing run instead of creating a duplicate, flagged
  `deduped: true` so callers (tool output, API response) can say "already
  running, attaching you to run X". This also neutralizes the
  duplicate-run failure mode of tool retries.
- **Concurrency cap**: org-level `max_concurrent_eval_runs` (in
  `OrganizationSettings.config`, default 3). Creating a run beyond the cap
  is **rejected** with a clear message ("N runs in progress; wait or stop
  one") — the agent can `wait` and retry; the UI shows a toast. Queueing
  is deliberately out of scope (adds a scheduler state machine for little
  gain at current scale).
- Both checks live in the service so tool, API, and reliability-automation
  paths all get them. Note: `AgentReliabilityService` already has its own
  per-agent in-flight guard (`_already_running`); the org cap is additive.

---

## Phase 4 — automation

### 4.1 Run-completion wake-up (new mechanism)

Goal: a detached `run_eval` resumes the originating conversation when the
run finishes, replacing poll-or-wait in the common case.

With background as `run_eval`'s default (§1.2), this is the mechanism
that closes the loop without polling, so it moves up in priority (see
sequencing).

- **Schema** (one migration): `TestRun.origin_report_id` (nullable FK),
  `TestRun.origin_user_id` (nullable FK), `TestRun.wake_on_finish`
  (bool, default false). Set by `run_eval` at creation time;
  `wake_on_finish` is true whenever the tool returns before the run is
  terminal (the default background path), false when results were
  delivered inline within `wait_s`.
- **Firing point**: the tail of `create_and_execute_background`'s
  background task (after `_save_run_summary`) and `stop_run` — i.e. every
  transition to a terminal status. Fire = create a completion on the
  origin report (module-level function mirroring `run_wait_wake`,
  including the cross-worker claim) with prompt:
  `"[Eval run finished] Run <id> completed with status <status>
  (<passed>/<total> passed). Read the results with get_eval_run and
  report back. If this was already handled in the conversation,
  acknowledge briefly and stop."`
- **Double-wake avoidance**: `run_eval`'s detach message tells the agent a
  wake-up is armed, and its description says *not* to also arm a `wait`
  for the same run. If the user/agent cancels interest, `stop_eval_run`
  stops the run (which still fires one final wake reporting "stopped") —
  acceptable and simple. A `wake_on_finish=false` escape hatch on the
  `run_eval` input covers agents that explicitly want silence.
- **Failure isolation**: wake firing is best-effort (log on failure);
  a lost wake degrades to the phase-1 poll/wait path, never blocks run
  completion.

### 4.2 Suite creation: deliberately skipped

No `create_suite` tool in this scope. Agent-authored cases keep landing
in an explicit existing `suite_id` or the lazy-created "Drafts" suite;
suite management stays a UI/API concern. Grouping strategy (decision, no
code): suites stay *optional folders / run-sets*; fine-grained grouping
(smoke/complex/regression) uses `tags_json`, which already exists and
becomes searchable via §3.3. Selection-by-query (agent + tags + status)
is phase 2, alongside the association table. If LLM-driven suite
creation is ever added, it must be find-or-create-by-name backed by a
`(organization_id, lower(name))` unique index — noted here so the
constraint lands before the tool does.

### 4.3 Instruction-change → affected evals: already built

`AgentReliabilityService` already implements the end state we sketched:
`schedule_for_build` fans an instruction build out to affected agents,
`run_automation` gates on per-agent policy autonomy, selects that agent's
cases (`list_agent_eval_case_ids`), runs them against the candidate build,
and promotes/pends/self-heals on the outcome, recording
`trigger_reason="automation:<trigger>"`. **No new work in this phase.**
The only touchpoint: `get_eval_runs` surfaces automation runs
(they're ordinary `TestRun` rows), which closes the visibility loop for
chat users.

---

## Sequencing (PR-sized slices)

1. **PR 1 — core loop**: `run_eval` background-by-default (`wait_s`,
   detach, heartbeat) + chat-card switch to run-level streaming;
   `get_eval_runs` + `get_eval_run` tools; digests. (Unit tests mirror
   `test_run_eval_input.py` / `test_wait_tool.py` patterns.)
2. **PR 2 — wake-up**: `TestRun` origin columns migration +
   terminal-status wake firing + `run_eval` integration. Promoted to
   second because with background as the default, the wake-up *is* the
   loop's return path; poll/wait is the fallback.
3. **PR 3 — cancel & stop**: `cancel_wait` tool + `WaitService.list_waits`
   + job_id in wait summary; `stop_eval_run` tool; the three guidance
   lines.
4. **PR 4 — comparison**: `compare_runs` service + endpoint + run-detail
   UI + `compare_to_previous` in `get_eval_run`.
5. **PR 5 — authoring**: `edit_eval` tool; `search_evals` widening.
6. **PR 6 — hygiene**: dedupe + org concurrency cap in the run service.

PR 1 alone makes the chat loop usable (via poll or wait); each later PR
is independently shippable.

## Open questions

1. `wait_s` max (proposed 600s — ToolRunner's idle timer is defeated by
   the heartbeat either way). Default is settled: `0`, background.
2. Concurrency cap default (proposed 3) and whether rejection should
   instead auto-attach to the newest in-progress run when case sets
   overlap but aren't equal.
3. Should `get_eval_runs`/`get_eval_run` also be available in `chat` mode
   for read-only "how did last night's evals do?" questions, or does that
   stay a training-mode conversation? (Proposed: training-only until
   someone asks.)
4. Comparison default base: "most recent terminal run sharing ≥1 case"
   vs. "most recent run on the same build lineage". Proposed the former —
   simpler and matches "did my change help?" intuition.
5. Should PR 1 ship before PR 2 exists — i.e., is poll/wait acceptable as
   the interim return path? (Proposed: yes; the wait tool already covers
   it.)
