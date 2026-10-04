# agent_v2 performance stack — cumulative report

Four stacked PRs on top of [#1182](https://github.com/bagofwords1/bagofwords/pull/1182):

| PR | items | what it removes |
|---|---|---|
| [#1184](https://github.com/bagofwords1/bagofwords/pull/1184) **A** | 1, 2 | 5 commit+`refresh()` graph reloads per tool; per-step bus broadcasts nobody read; Slack graph loads on web runs |
| [#1185](https://github.com/bagofwords1/bagofwords/pull/1185) **B** | 8, 9, 5 | a schema build per tool (reuse the run's schema context); 25 hydrated result sets for step discovery; a parallel-tool shared-session race |
| [#1186](https://github.com/bagofwords1/bagofwords/pull/1186) **C** | 3, 4, 11 | N per-tool warm refreshes and snapshots per batch; full-section snapshot dumps; the serial browser-policy check |
| [#1187](https://github.com/bagofwords1/bagofwords/pull/1187) **D** | 6, 10 | one SSE event per LLM chunk; DataFrame formatting on the event loop |

Per-PR details and methodology are in `agent-persistence-single-commit.md`,
`tool-context-reads.md`, `warm-refresh-per-batch.md` and
`token-coalescing-and-pandas-off-loop.md`.

## How it was measured

Everything was measured through the real product, not unit benchmarks.
Playwright drove the chat UI with Claude Haiku 4.5 on the Music Store demo
data. Each run captured the live SSE stream and reloaded the page afterwards.
A local-only probe (never committed) counted SQL statements and commits per
agent run, timed hot methods, sampled event-loop lag, and timed
`_tool_db_lock`.

**Environments:**
- **P0**: Postgres 16 on the same machine as the app (the customer setup).
- **P1**: the same, behind a 15 ms one-way latency proxy (a managed/remote DB).
- **SQLite**: single-writer mode.

**Scenarios:**
- **S1**: three tables in parallel (a "think hard" prompt).
- **S2**: one line chart.
- **S3**: S1 with the data source attached.
- **S4**: a two-chart follow-up on an S3 report (S4t1 is its first turn).
- **S5**: a 200k-row CSV upload rendered as a table.
- **S6**: steering mid-run, then Stop.

**Gates on every run:**
- agent / tools / steps / visualizations `success`, with rows checked via
  `GET /api/steps/{id}`;
- each query has its default step;
- table usage ≥ recordable;
- exactly one `completion.finished`;
- **event→fetch consistency**: on every `tool.finished` /
  `visualization.updated`, the object is fetched the way the UI does and
  compared with its final state;
- **streamed-text consistency**: tokens rebuild every snapshot exactly;
- the UI has no "Agent failed" or greenlet text after a reload;
- no new backend-log errors.

## Results: baseline (#1182) → full stack (A+B+C+D)

Numbers are means. Wall times include the LLM, which the stack does not
change, so they carry model variance; SQL, commits, lag and events are
deterministic.

### P0 — Postgres on the same machine

| scenario | metric | baseline | full stack | Δ |
|---|---|---|---|---|
| S1 3 parallel tables | SQL statements / run | 2791 | **1127** | **−60%** |
| | commits / run | 127 | 97 | −24% |
| | token SSE events / run | 1164 | 823 | −29% |
| | whole run ¹ | 45.0s | 33.4–38.9s | −14…−26% |
| S3 3 tables, data source attached | SQL / run | 3248 | **1282** | **−61%** |
| | max event-loop lag | 850 ms | **169 ms** | **−80%** |
| | whole run · tool batch | 42.3 · 18.1s | 36.5 · 15.3s | −14% · −15% |
| S2 one chart | whole run | 12.5s | **9.8s** | −22% |
| | SQL / run | 1450 | 763 | −47% |
| S4t1 (3 tables, first turn; 1 run after) | whole run · tool batch | 49.3 · 23.3s | **29.8 · 8.9s** | −40% · −62% |
| S5 200k-row CSV | whole run | 28.8s | **22.4s** | −22% |
| | SQL / run | 1443 | 734 | −49% |
| | DataFrame formatting on the event loop | ~240 ms | **0** | |
| S6 | steer applied · Stop acknowledged | 1033 · 1251 ms | 1036 · 810 ms | = · −35% |

¹ 33.4s is PR-C and 38.9s is PR-D. Their difference is LLM codegen time
(`sub_timings_json` → `generating_code`). An interleaved C/D/C/D run measured
34.1 vs 34.7s.

### P1 — Postgres at 15 ms (where DB round-trips dominate)

| scenario | metric | baseline | full stack | Δ |
|---|---|---|---|---|
| S1 3 parallel tables | whole run | 74.5s | **52.2s** | **−30%** |
| | parallel tool batch | 39.7s | **20.8s** | **−48%** |
| | SQL / run | 2799 | 1139 | −59% |
| | max loop lag | 703 ms | 417 ms | −41% |
| | token events / run | 997 | 597 | −40% |
| S3 with data source | whole run | 80.4s | **57.5s** | **−28%** |
| | parallel tool batch | 47.8s | **22.1s** | **−54%** |
| | SQL / run | 3202 | 1238 | −61% |
| | max loop lag | 930 ms | 318 ms | −66% |

### SQLite (single-writer)

| scenario | metric | baseline | full stack | Δ |
|---|---|---|---|---|
| S3 with data source | whole run | 44.3s | **27.4s** | **−38%** |
| | tool batch | 20.3s | **9.1s** | **−55%** |
| | SQL · commits / run | 1872 · 145 | 798 · 104 | −57% · −28% |

### Where each PR's gain came from (P0 S1/S3 unless noted)

| step | biggest measured effect |
|---|---|
| **A** | `_handle_tool_output` 1105 → 50 SQL (1568 → 115 ms); `_handle_streaming_event` 785 → 121 SQL; SQL/run −57%; P1 tool batch −29…−41% |
| **B** | schema builds per run 7 → 4; S3 tool batch 20.0 → 12.3s (P0) and 28.4 → 20.2s (P1); fixed the `concurrent operations are not permitted` race seen in the baseline |
| **C** | warm refreshes per run 9 → 6; snapshots 6 → 4; S1 tool batch 17.9 → 11.6s (P0) and 31.3 → 21.2s (P1); tool executions now link to a snapshot that includes their observations |
| **D** | token events −18…−40% (coder reasoning −74%) with byte-identical text; 200k-row formatting moved off the event loop |

## Correctness across the stack

Across **100 real-UI runs** after the changes (25 phases):

| check | result |
|---|---|
| runs passing all gates | **95** |
| event→fetch checks | **412, 0 mismatches** |
| streamed-text checks (all phases) | **3,080, 0 mismatches** |
| table usage recorded | **80/80** |
| new log errors | **0** |
| offline replays | table resolution 16/16 identical · loadables text 12/12 · slim snapshot 6/6 · streamer 16/16 blocks |

**The 5 failures are all S6 (Stop), and none are caused by the stack:**
- **3 harness races.** The turn finished before the scripted Stop click.
- **2 cases of a pre-existing bug.** An early Stop only aborts in the
  browser when the page doesn't know the server's system-completion id yet;
  it reproduces on the baseline commit in 1 of 3 runs.

Also pre-existing, on the baseline: `POST /api/completions/{id}/sigkill`
returns 500 on every call (`RecursionError` while serializing the response,
after the sigkill write commits). Both are filed as follow-ups.

**UI regression pass** on the full stack, compared with the baseline:
- **Streaming and chat:** mid-stream screenshots show typing text and a chart
  rendering live, and the finished chat renders every tool card, chart, table,
  dashboard card and summary.
- **Reload:** the reloaded page matches the live stream.
- **Monitoring → trace / Context Browser:** renders schemas, instructions and
  observations.
- **Console errors:** the only errors come from the dashboard artifact
  iframe ("React is not defined", `/api/thumbnails` 404). They appear
  identically with the baseline backend, because this sandbox can't load the
  artifact's CDN script.

## Not verified

- Live Slack delivery (no Slack workspace in the sandbox).
- The stacked PRs don't get CI runs because their base branches aren't
  `main`. On #1182, CI is green except 7 unit tests that fail identically
  on `main`.
