# Feedback Loop — blank plan decisions can exhaust the planner loop

Reported: a report turn can render repeated empty **Planning (action)** cards,
make no tool progress, and remain busy until it is stopped or reaches the
planner step limit. The screenshot attached to BOW-17 shows this shape: several
empty planning cards followed by a `sigkill` after roughly 40 seconds.

This investigation joins every `plan_decision` from the 30-day `bow`, `bow-eu`,
and `fattal` exports to its completion, agent execution, completion blocks, tool
executions, and neighboring decisions. The restricted row-level evidence lives
under `backend/db/ai-harness-exports/2026-08-05/` and is intentionally ignored
by git.

## What "blank" means

There are two materially different shapes; combining them produces a misleading
21.4% rate:

| Shape | Definition | 30-day result | Interpretation |
|---|---|---:|---|
| Fully blank | no reasoning, assistant text, final answer, action name/args; `analysis_complete=false` | 194 / 6,923 (2.80%) | Invalid/no-progress decision |
| Action without narration | no text, but a valid action is present | 1,286 / 6,923 (18.58%) | Usually expected with native tool calling |

Of the 1,286 action-without-narration rows, 1,281 have a related tool execution.
Those are not failures: PlannerV3 permits a provider-native tool call without
assistant narration. Only five have no related tool; all five are stopped,
sigkilled, or still `in_progress`, so they form the interrupted-placeholder
failure described below.

Every one of the 194 fully blank rows has one related empty completion block,
no related tool execution, `plan_type=NULL`, and an agent execution recorded as
`success`:

| Server | Fully blank decisions |
|---|---:|
| `bow` | 14 |
| `bow-eu` | 173 |
| `fattal` | 7 |

They affect 33 executions, but split sharply into two populations:

1. **Main-loop runaway: 163 rows in two `bow-eu` executions.** One execution
   persisted 100 blank decisions (loop indexes 0–99), ran 501,316 ms, invoked
   zero tools, then recorded `success`. The other persisted 63 blank decisions
   (0–62), ran 302,639 ms, invoked zero tools, and also recorded `success`.
   Both used `claude-fable-5` on version `0.0.439`.
2. **Knowledge-harness no-op: 31 rows in 31 executions.** Each is a single
   `phase=knowledge_harness` decision (loop index 1000+). These span all three
   servers and versions through `0.0.524`. They do not create the 100-step main
   loop, but they persist an empty `Planning (unknown)` block and make successful
   turns look unfinished.

The exact loan number visible in the screenshot was not present in any of the
three 30-day archives, so this investigation does not claim an exact row match.
The persisted shapes and the screenshot's repeated empty cards are structurally
consistent with the two paths below.

The resource impact was much larger than the row count suggests:

| Execution | Calls | Prompt tokens | Cache-creation tokens | Result |
|---|---:|---:|---:|---|
| 100-step run | 100 | 447,800 | 2,637,700 | incorrectly `success` |
| 63-step run | 63 | 331,191 | 1,661,751 | completion `error` |

The second completion's persisted error is **“anthropic call failed: Monthly
LLM spend quota exceeded.”** Thus the runaway is not only a stuck UI turn: it
can consume the shared organization/provider budget and make unrelated LLM
requests fail. Both runs had zero cache reads despite recreating the same large
prefix on every call, which amplified the cost further.

## Root cause A — an empty provider stop is a valid PlannerDecision

`backend/app/ai/agents/planner/planner_v3.py:441-462` derives the decision from
provider events:

- `analysis_complete` is true only for `stop_reason == "end_turn"` with no
  actions;
- `plan_type` is derived only when an action exists;
- empty text buffers become `None`.

`backend/app/schemas/ai/planner.py:50-63` permits exactly that combination:
`analysis_complete=false`, nullable `plan_type`, no text, no action, empty
`actions`, and no error. `_build_decision` therefore returns a schema-valid
object instead of a validation error when a stream ends without a recognized
stop reason, text, or tool call.

The exported metrics make this more specific than “the model returned nothing.”
The runaway calls reported 3–4 completion tokens and usage, but produced no
recognized text, reasoning, or tool event. The provider adapter can silently
ignore content-block/event types it does not recognize, and maps new stop
reasons to `other`; PlannerV3 does not persist the raw stop reason or observed
event types. We therefore cannot distinguish a genuinely empty model response
from an adapter compatibility gap using the stored row. That observability gap
is part of the defect and must be fixed at the stream boundary.

The agent then amplifies it:

1. `backend/app/ai/agent_v2.py:4590-4638` persists the decision and creates its
   completion block **before** checking whether it represents progress.
2. `backend/app/ai/agent_v2.py:4795-4823` retries a missing action only when
   `plan_type == "action"`. The fully blank object's plan type is `None`, so it
   reaches `if not action: continue` without incrementing the invalid-output
   retry counter or adding an observation. The next planner call can repeat the
   same no-op.
3. The outer loop is bounded only by the configured step limit
   (`backend/app/ai/agent_v2.py:3853-3916`, default 100).
4. Falling out of that loop is not treated as exhaustion. The common finalizer
   at `backend/app/ai/agent_v2.py:5820-5874` marks the agent execution and
   completion `success` unless the sigkill flag is set or an exception escaped.

The database/UI faithfully exposes the invalid state. `project_manager.py`
uses `Planning (unknown)`, `in_progress`, and null content/reasoning for the
row (`backend/app/project_manager.py:1450-1472`). The report page renders the
block shell but only renders reasoning/content when those fields exist
(`frontend/pages/reports/[id]/index.vue:333-381`).

### Loop A — deterministic reproduction (no network or database)

The surviving regression test stubs the LLM stream with exactly the production
shape: a stop packet plus usage (1,200 prompt tokens, 4 completion tokens, and
8,000 cache-creation tokens), but no recognized semantic event.

```bash
cd backend
TESTING=true PYTHONPATH=. .venv/bin/python -m pytest \
  tests/unit/test_agent_loop_rescue.py::test_planner_rejects_stream_without_semantic_output -q
```

Before the fix, the assertion failed because `final.error is None`; the stream
was folded into a normal `analysis_complete=false`, no-action decision. After
the fix:

```text
.                                                                        [100%]
1 passed
```

The test also proves that stop reason, observed stream event types, token usage,
and semantic-event count survive in the typed error.

## Root cause B — the knowledge harness persists its terminal no-op

The knowledge harness has its own partial guard, but it checks only whether a
`final_decision` object exists (`backend/app/ai/agent_v2.py:1764-1780`). A fully
blank PlannerDecision exists and is truthy, so the harness:

1. persists and renders it at `agent_v2.py:1782-1823`;
2. finds no actions at `agent_v2.py:1825-1833`;
3. treats that as normal completion and exits.

That explains the 31 singleton rows at loop index 1000+. This path is not the
catastrophic main loop, but it is still an invalid state projection and remains
present in newer exported versions.

## Root cause C — interrupted multi-action placeholders survive

Native tool decisions often contain no narration by design. The agent persists
the primary decision block and then pre-creates one empty block for every extra
action (`backend/app/ai/agent_v2.py:4833-4858`). `project_manager.py:1461-1472`
explicitly strips content and reasoning from secondary blocks to avoid duplicate
text. A later successful tool persistence attaches the tool and gives the card
meaning.

If execution is stopped between block fan-out and tool persistence, the
placeholders remain `in_progress`. The cancellation helper only owns the
single `current_block_id` created before planning (`agent_v2.py:4214-4260`), not
the complete `_action_block_ids` list created afterward.

The export contains a direct example on `bow` version `0.0.517`: one
`route_model` decision, zero related tools, and **eight** related empty
`Planning (action)` blocks at indexes 200–207. The completion is stopped and the
agent is `sigkill`. Four other action decisions have no tool (one stopped and
three stale `in_progress` rows), each leaving one empty block.

## The fix

The repair is layered so a provider regression cannot turn into a paid retry
storm or a false-success trace:

1. **Reject at the stream boundary.** `planner_v3.py:434-452` now records the
   stop reason, normalized stream event types, and semantic event count. A
   usage/stop-only stream becomes `PlannerError(code="empty_stream")` while
   preserving its token metrics. The fields live in `PlannerMetrics`
   (`schemas/ai/planner.py:31-41`).
2. **Enforce one progress invariant everywhere.**
   `planner/reliability.py:42-80` accepts only a terminal outcome or at least one
   tool action. `plan_type` is no longer trusted as the retry signal, while
   valid tool-only decisions remain accepted.
3. **Hard two-call fuse.** `NoProgressBudget` (`reliability.py:19-38`) permits
   only the original attempt plus one retry/fallback across the whole run. It is
   independent of the 100-step legitimate-work budget. The historical 100-call
   and 63-call failures are therefore structurally impossible on this path.
4. **Audit without blank UI.** The main loop persists rejected attempts with
   `phase=planner_error` (`agent_v2.py:4463-4484`), and
   `project_manager.py:1216-1228` embeds the typed error in `metrics_json`. It cancels the transient
   skeleton and never creates a visible completion block for the rejected row.
5. **Correct terminal state.** Exhausted no-progress attempts produce an error
   completion. Falling out of the outer workflow loop without a terminal result
   now becomes `planner_step_limit` (`agent_v2.py:5821-5836`), and the common
   finalizer derives both agent and completion status from `completion_errored`
   (`agent_v2.py:5953-6008`) rather than defaulting to success.
6. **Knowledge harness parity.** The harness applies the same guard, records
   `phase=knowledge_harness_error`, retries once, and never projects the rejected
   decision as a blank block (`agent_v2.py:1785-1827`). It is also skipped when
   the main completion failed.
7. **No auxiliary LLM waste on failure.** Early scoring is deferred until the
   first valid planner decision, and failed completions skip title generation,
   knowledge capture, follow-ups, and late scoring. An empty-stream incident is
   therefore limited to the two planner attempts guarded by the fuse.

The interrupted multi-action placeholder shape described under Root cause C is
a separate sigkill cleanup defect: it requires a valid action decision and does
not cause planner retries. It remains explicitly bounded out of this fix so the
no-progress repair does not risk deleting valid tool audit state.

## Loop B — live Luna confirmation with the local database

The existing localhost environment was restarted with Nuxt on port 3000 and
FastAPI on port 8000. In the signed-in app, model **GPT-5.6 Luna**
(`gpt-5.6-luna`) ran this prompt:

> Using the available data agents, inspect the available data and give me a
> concise overview. Use tools where needed, then finish with a clear answer.

Evidence:

| Object | ID / result |
|---|---|
| Report | `433c54a7-2756-43e2-b167-5488f2526b7b`, titled “Music Store and Procurement Overview” |
| Completion | `9e0d375b-2cc9-46db-85d8-84e59c3b635f`, `success`, model `gpt-5.6-luna` |
| Agent execution | `fc34cfc6-b1d7-4f95-89ce-f87020fb3cda`, `success` |
| Plan decisions | 4 total; 0 blank; 0 rejected |
| Tool executions | 13 total; 0 in progress; 0 errors |
| Completion blocks | 14 total; 0 in progress; 0 orphan decision blocks |
| Usage | 136,529 prompt + 2,757 completion tokens |

The UI reached a terminal answer and enabled follow-ups. The database was
queried during the run (two valid tool decisions already present) and after
completion (all rows terminal and linked).

## Verification

```bash
cd backend
TESTING=true PYTHONPATH=. .venv/bin/python -m pytest \
  tests/unit/test_agent_loop_rescue.py -q
```

Observed:

```text
18 passed
```

The focused test set covering planner prompt behavior, tool-call streaming, and
LLM error classification also completed with all tests passing. `py_compile`
and `git diff --check` passed.

This proves the reported no-progress stream can no longer enter the normal
action loop, cannot exceed two paid planner attempts, cannot render repeated
blank cards, and cannot be finalized as success after exhaustion. No software
can guarantee provider availability, but this failure mode now terminates
deterministically with preserved diagnostics instead of hanging or silently
burning the monthly quota.

## Data loop / evidence commands

The row-level analyzer can be rerun against a fresh combined trace:

```bash
backend/.venv/bin/python backend/db/analyze-blank-plan-decisions.py \
  backend/db/ai-harness-exports/2026-08-05/turn-trace.jsonl.gz \
  --summary backend/db/ai-harness-exports/2026-08-05/blank-plan-decisions-summary.json \
  --cases backend/db/ai-harness-exports/2026-08-05/blank-plan-decisions-cases.jsonl.gz \
  --csv backend/db/ai-harness-exports/2026-08-05/blank-plan-decisions.csv
```

The summary is aggregate-only. The compressed cases file contains every blank
decision together with its preceding/following decision, completion, agent
execution, blocks, tools, and context snapshots. Treat the export directory as
restricted production data.
