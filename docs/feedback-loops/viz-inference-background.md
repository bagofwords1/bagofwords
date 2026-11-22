# Feedback Loop — "Now that show viz is MD based… can we put infer viz in bg?"

Since figures moved into the answer (`{{show:<id>}}`, #1844), `create_data`'s
chart is drawn by the answer, not by the tool card. The claim validated here:
the visualization inference (a small LLM pass after the query runs) no longer
needs to block the agent loop. `create_data` can end with the deterministic
chart and refine it in the background, without a figure ever drawing the
wrong chart, a later tool reading a chart that is about to change, or the
database ending in a state different from what the user saw.

## Root cause (validated)
- `create_data` awaited `_infer_visualization_model` between running the
  query and ending the tool
  (`backend/app/ai/tools/implementations/create_data.py`, the
  "inferring_visualization" stage). The planner's next call could not start
  until it returned: ~0.5–0.6 s per chart on Haiku 5.5 (up to 1.6 s
  observed), more on slower models.
- Nothing downstream of the planner needed it inline: the chart type is
  pinned to the requested type, and `chart_spec.build_final_data_model`
  already derives a complete, renderable spec from the rows alone; inference
  only refines series / group_by / filters / display.

## The change
- **Tool** (`create_data.py`): with `runtime_ctx["defer_viz_inference"]`
  (agent v2 only), the tool ends with the deterministic spec, marks its output
  `viz_pending: true`, and hands the inference over as an `asyncio.Task` in
  `runtime_ctx["viz_refinement"]` (`_refine_visualization`). The inline path
  (MCP, harness, `BOW_VIZ_INFERENCE_INLINE=1`) is unchanged; both share
  `_finalize_visualization`, so the same inference yields the same spec.
- **Tracker** (`backend/app/ai/viz_refinement.py`): owns each task until it is
  written. *Announce* — as soon as it lands, the agent emits SSE
  `visualization.refined` (in-memory, safe from a background task). *Apply* —
  the DB writes (step `data_model`, visualization `view`, tool execution
  `result_json` **and** `context_summary_json`) run only in `settle()`, which
  the agent calls on its own coroutine: before a tool batch that may read
  visualizations (`wait=True`; batches of only `create_data` / `inspect_data`
  don't wait) and before `completion.finished`. In single-writer mode (always
  on SQLite) the write session *is* the loop's session, so a background write
  would be a concurrent use of it; settling from the loop keeps every write on
  one coroutine in both modes. A pass still running when a waiting settle
  times out (20 s) is cancelled and applied as "no refinement"; a stopped run
  settles with a zero timeout; an errored run cancels.
- **Frontend**: the report page handles `visualization.refined` (patches the
  block's `result_json`, `created_step`, `created_visualizations`, and tells
  result cards via `visualization:refined`); `ChatFigure` keeps its loading
  outline while `viz_pending` is set, capped at 12 s, so a figure draws once —
  the refined chart.

## Loop A — deterministic
```bash
cd backend
uv run pytest tests/unit/test_viz_refinement.py tests/unit/test_create_data_viz_deferred.py --db=sqlite
cd ../frontend && node tests/unit/figureModel.mjs && node tests/unit/useCitations.mjs
```
`test_viz_refinement.py` pins the race rules: announced once / applied once
whatever the timing; `settle(wait=True)` returns only when everything is
applied (timeouts cancel and clear pending); `settle(wait=False)` never
blocks; concurrent settles, refinements tracked mid-settle, failing applies,
failing announces, per-pass timeouts and teardown. `test_create_data_viz_deferred.py`
pins that the background pass equals the inline finalization for the same
inference reply, that failures return "keep the deterministic chart", and
that the deterministic chart is renderable on its own.

## Loop B — live, real model (Claude Haiku 5.5)
```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --demo
ANTHROPIC_API_KEY=… HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py
uv run python ../tools/agent/viz_refine_probe.py --db db/agent.db --prompt "Chart revenue by billing country as a bar chart."
```
`tools/agent/viz_refine_probe.py` sends one turn over SSE and exits 1 on any
violation: a pending result without exactly one refined event before
`completion.finished`; another tool starting before the refinement landed;
or the database (tool execution `result_json`, `context_summary_json`, step,
visualization) not holding the refined spec.

| Scenario | Runs | Violations |
|---|---|---|
| bar / line / two charts in parallel / metric card / genre | 17 | 0 |
| chart → `create_artifact` (settle before the batch, then the artifact reads the refined chart) | 2 | 0 |
| stop right after `create_data` (settled at run end) | 1 | 0 |
| every inference forced to time out (`BOW_VIZ_REFINE_TIMEOUT_S=0.01`) — announced unrefined, pending cleared, deterministic chart persisted, dashboard still built | 2 | 0 |

Same 4 prompts × 2, background vs inline (`BOW_VIZ_INFERENCE_INLINE=1`):

| | background | inline |
|---|---|---|
| `create_data` duration, median | 2.80 s | 4.16 s (3.27 s on the less noisy second pass) |
| whole turn, median | 8.2 s | 10.0 s |
| inference (now off the critical path) | 0.47–0.55 s | same, on the critical path |

The inline numbers are noisy (codegen variance, 3.1–8.4 s); the saving the
change can claim is the inference itself, ~0.5 s per chart on Haiku 5.5. The
refinement landed 0.33–0.56 s after `tool.finished`, before the planner's
next event, in every run; final settles waited 4–26 ms.

UI (`tools/agent/figures_ui_flow.mjs`): figures drawn with the refined spec
(currency units come only from inference), no raw markers, no console errors,
every ••• action working, same after reload. A live run recorded the refined
event at 5.8 s and the figure going loading → ready at 6.4 → 6.8 s (row
hydration only; no hold). The hold itself, checked by marking a persisted
result pending: outline for 12 s, then drawn.

## Found by the loop
- **Reload served the deterministic chart.** Report loads read the tool's
  `context_summary_json` projection (`report_payload_projection.py`), not
  `result_json`; the first version updated only `result_json`. The probe's
  database check caught it (`display` refined in one, absent in the other);
  the apply now refreshes the projection's `data_model` / `view` too. The
  projection never carries `viz_pending`, so a reload can't hang on a flag a
  dead run left behind.
- Inline mode emitted `"viz_pending": null`; the key is now present only while
  pending (`live-inline.jsonl` predates that fix, hence its probe errors).

## Regression notes
- Full unit suite: 22161 passed, 207 failed, 1 error. All 207 fail identically
  on the base commit in this sandbox (SharePoint/Kerberos, OBO, Sybase,
  artifact precompile — missing system libraries / CA bundle); the error is
  `libodbc.so.2` missing. None touch the agent loop or `create_data`.
- The dev backend runs uvicorn with `--reload`; editing backend files (or
  `git stash`) during a live loop froze the reload at "Waiting for background
  tasks to complete" twice. Restart instead of relying on reload.

## Round 2 — review of `39a257c` (2026-11-22)

Seven findings, each verified against the code before changing it, each now
pinned by a test that fails on `39a257c`:

| # | Finding | Fix | Test |
|---|---|---|---|
| 1 | The settle ran once before a tool batch, before `create_data` had handed over its task: in `[create_data, read_query]` the reader saw the provisional chart. | The barrier runs per action inside `_dispatch_action_batch` (`_viz_barrier`), for every tool outside `create_data` / `inspect_data`. | `test_viz_refinement_dispatch.py` drives the real dispatcher: 3 of 4 fail with the barrier removed. |
| 2 | A chart the user saved while the refinement was pending was overwritten. | The write applies only while the stored view and the step's tool-written keys still equal the provisional ones (`stored_chart_still_provisional`); otherwise the refinement is dropped and the projection takes the user's view. | `test_viz_refinement_apply.py::test_a_chart_the_user_changed_while_pending_is_kept` |
| 3 | Chat channels send the chart image when the tool block succeeds, before a refinement lands. | Deferral only for web report runs (`_defer_viz_inference_for_run`: no external platform, the same test the planner uses for figures). Channel runs keep the inline pass. | `test_only_web_report_runs_defer_the_inference` |
| 4 | PostgreSQL: a tool row not inserted within 10 s left a refined step next to a provisional tool result. | All reads and checks happen first; a missing row raises before anything is modified, and the write is retried. The wait is now 30 s, and the persist task bounds itself. | `test_a_missing_tool_row_changes_nothing` |
| 5 | A late provisional `block.upsert` (PostgreSQL background persist) replaced the refined tool execution on the client. | The server re-announces `visualization.refined` after the commit, which lands after the persist's snapshot. The client also remembers refinements by tool execution and re-applies them on every `block.upsert`. | Live: 2 events per result, last one after the snapshot. Probe accepts 1–2 and checks the last one against the DB. |
| 6 | `applied` was set before the write. | Applied means committed: a failed write is retried with backoff (`MAX_APPLY_ATTEMPTS = 3`). If it never commits, the browser is re-told the provisional chart the database kept. | `test_a_write_that_never_commits_…`, `test_a_transient_write_failure_is_retried_until_it_commits` |
| 7 | The settle loaded `Step.data` (full encrypted result) to update metadata. | `load_only(Step.id, Step.data_model, Step.status)` (`status` is read by the Step `after_update` hook). | `test_the_step_query_does_not_read_the_result_rows` (captured SQL) |

Also found while fixing: in single-writer mode a rollback inside the apply
would hit the loop's shared session. The apply now raises before modifying
anything, so a "not ready yet" never rolls back.

Re-run after the fixes (Haiku 5.5): 6 probe runs (bar, two charts, chart →
`create_artifact` with the barrier settling before it, metric cards, line, stop)
with 0 violations, DB refined everywhere including `context_summary_json`.
Figures UI flow passing; live: refined event at 5.9 s, post-commit re-announce
at 6.9 s, figure loading → ready at 6.7 → 7.3 s.
