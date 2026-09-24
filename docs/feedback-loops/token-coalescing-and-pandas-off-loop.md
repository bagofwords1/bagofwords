# Feedback loop — coalesced token deltas, pandas formatting off the event loop

**PR-D of the agent_v2 performance stack** (items 6 and 10 of the plan).

- **Planner and coder text streaming.** `PlanningTextStreamer` and its
  subclass `ReasoningTextStreamer` emitted one `block.delta.token` SSE event
  per LLM chunk, each with its own seq and queue put. `throttle_ms=16` and
  `char_threshold=30` were set on the streamer but never used. `_delta` walked
  both strings character by character on every chunk.
- **`format_df_for_widget`** turned result DataFrames into widget JSON **on the
  event loop**. For a 200k-row CSV that took about 240 ms, during which every
  other stream on the server stalled. `write_csv` also ran `pd.read_csv` on the
  loop.

## Change

- `_delta`: a `startswith` fast path; the character walk only runs when the
  text isn't an extension.
- Token deltas are buffered per field and emitted once `char_threshold` chars
  are pending or `throttle_ms` has passed. A timer flushes a quiet tail.
- Pending text is flushed before any replace snapshot, periodic snapshot or
  final snapshot, so snapshots never overtake tokens. An `asyncio.Lock`
  serializes take → seq → emit between `update()` and the timer.
- `StreamingCodeExecutor.format_df_for_widget_async` runs the formatting on
  the code-exec thread pool. It is used by `create_data`, `create_widget`,
  `write_csv` and `execute_and_update_step`. `write_csv`'s CSV read runs in an
  executor.

## Verification

### Text correctness (the risk of this change)

- **Replay of real recorded streams.** 16 planner blocks from baseline S1/S3
  runs were fed through the new streamer with their original inter-token
  timing. The client-reconstructed text was **identical in 16/16** blocks, and
  token events went from 4487 to 3706 (−17%).
- **Live check on every run.** A new verifier gate rebuilds the text from
  tokens and asserts that it equals every following non-replace snapshot:
  **655 checks, 0 mismatches**, across all PR-D runs on P0, P1 and SQLite.
- **Unit tests** in `backend/tests/unit/test_planning_text_streamer_coalescing.py`:
  - a fast stream is coalesced and reconstructs exactly;
  - a quiet tail is flushed by the timer;
  - a source switch flushes pending text before the replace;
  - `_delta` cases.
- 73 related streaming tests still pass.

### Events and latency

| env · scenario | metric | before (PR-C) | after (PR-D) |
|---|---|---|---|
| P1 · S1 ×2 | token delta events per run | 999 | **597 (−40%)** |
| P1 · S3 ×2 | token delta events per run | 954 | **723 (−24%)** |
| SQLite · S3 | token delta events per run | 944 | **649 (−31%)** |
| P0 · S1 ×4, interleaved C/D/C/D | token delta events per run | 897 | **732 (−18%)** |
| P0 · S2 ×2 | token delta events per run | 68 | **49 (−28%)** |
| Coder reasoning, microbench (641 chunks) | events · wall time · worst append | 647 · 2776 ms · 2.4 ms | **168** · 2716 ms · 2.3 ms |
| P0 · S5 (200k-row CSV) ×2 interleaved | `format_df_for_widget` on the event loop | ~242 ms per call | **0** (runs on the pool; `formatting_widget` stage 10–30 ms per 237-row result) |
| | mean event-loop lag | 4.6 ms | 3.2 ms |
| | whole run | 22.6s | 20.9s |

### No regressions: interleaved P0 A/B

Runs alternated C, D, C, D, two S1 runs plus one S6 run per block:

| metric | PR-C | PR-D |
|---|---|---|
| S1 whole run | 34.1s | 34.7s |
| S1 gates | 4/4 | 4/4 |
| S6 steer applied | 1033 ms | 1037 ms |
| S6 Stop acknowledged | 862 ms | 810 ms |
| S6 gates | 2/2 | 2/2 |

The first chain run of PR-D on P0 had slower S1 tool batches, but
`sub_timings_json` puts all of the extra time in `generating_code`, the LLM
codegen: 14–18.6s on one tool versus 8–12s. Formatting (10–30 ms) and
execution (20–50 ms) were unchanged, and the streamer micro-benchmark shows
no added latency.

### Correctness totals for PR-D

| env | runs passing all gates |
|---|---|
| P0 | 19/20 |
| P1 | 4/4 |
| SQLite | 3/4 |

Across these runs:
- 112 event→fetch checks, 0 mismatches;
- table usage 22/22;
- 0 new log errors.

There are two failures, both in S6 and neither caused by this change:

- **P0: a harness race.** With the old 25s wait, the turn finished before
  the harness clicked Stop.
- **SQLite: a pre-existing early-Stop bug.** It reproduces on the **base
  commit** with the same harness (1 of 3 runs). When Stop is clicked
before the page knows the server's system-completion id, `abortStream()` only
aborts locally, and the agent runs to completion. Separately,
`POST /api/completions/{id}/sigkill` returns 500 on every call on the base
branch (a `RecursionError` while serializing the response; the sigkill write
itself commits). Both are filed as a follow-up and are not touched by this
stack.
