# Feedback loop — agent step/visualization saves reloaded the report graph

**PR-A of the agent_v2 performance stack** (items 1 + 2 of the plan).

Every `create_data` call saved its step through five `project_manager`
helpers. Each helper committed and then called `refresh()` on the step. A
refresh reloads the step's eager `selectin` graph: step → query → report →
all widgets / steps / queries, decrypting each step's `EncryptedJSON` data.
The two visualization writes did a graph-loading `get` plus a refresh each.
Creating the query, step and visualization took four more commits and
refreshes, all awaited before code generation could start. The completion
event bus also carried step, widget and text-widget broadcasts that no
subscriber reads. And every step update started a Slack sender, which loaded
the graph and only then discovered that the report has no Slack routing.

## Change

| where | before | after |
|---|---|---|
| `ProjectManager.finalize_tool_step` (new) | 5 helpers × (commit + refresh) | data model, code, data, parameters (in a savepoint) and status in **one commit**, no refresh; error-payload fallback kept |
| `ProjectManager.finalize_visualization` (new) | get + commit + refresh, twice | view + status in one commit on the loaded row |
| `ProjectManager.create_query_step_visualization` (new) | query / step / default-step / viz: 4 commits + refreshes | one transaction (`create_query(commit=False)`, flush, Core UPDATE for `default_step_id`) |
| `agent_v2._handle_streaming_event` | `get()` with eager graph | `select(...).options(lazyload("*"))`; per-column / series updates pass `refresh=False` |
| `models/step.py`, `widget.py`, `text_widget.py` | broadcast on every insert/update | removed (the bus has one subscriber, `AgentV2._handle_completion_update`, which reads only completions) |
| `models/completion.py` | full completion JSON on the bus | slim `_bus_event()`: ids, status, role, message type, parent, sigkill, plus prompt for steering only |
| Slack senders (`step.py`, `completion_block.py`, `slack_notification_service.py`) | load the graph, then check routing | one-column routing query first; web runs exit early |

API routes keep their existing helpers and behaviour.

## Loop — real UI scenarios, before vs after

Setup:
- Claude Haiku 4.5, Music Store demo data.
- The chat UI was driven with Playwright. Each run:
  1. typed the prompt;
  2. watched the live SSE stream;
  3. on every `tool.finished` / `visualization.updated`, fetched the object
     the way the UI does;
  4. reloaded the page.
- A local-only probe (never committed) counted SQL statements and commits
  per agent run, timed the hot methods, and sampled event-loop lag.

Environments:
- **P0**: Postgres 16 on the same machine.
- **P1**: the same, behind a 15 ms one-way latency proxy.
- **SQLite**: single-writer mode.

Scenarios:
- **S1**: three tables in parallel.
- **S2**: one line chart.
- **S3**: S1 with the data source attached.
- **S4**: a two-chart follow-up on an S3 report.
- **S5**: a 200k-row CSV upload.
- **S6**: steering plus Stop.

Every run was checked against these gates:
- agent execution `success`;
- every `create_data`, step and visualization `success`, with row counts
  checked through `GET /api/steps/{id}`;
- every query has its default step;
- table-usage events ≥ recordable;
- exactly one `completion.finished`;
- **0 event→fetch mismatches**;
- UI without "Agent failed" or greenlet errors;
- no new backend-log errors.

### P0 — local Postgres (mean, min–max)

| scenario | metric | before | after | Δ |
|---|---|---|---|---|
| S1 ×3 | SQL statements / run | 2791 (2724–2834) | **1212** (1141–1304) | **−57%** |
| | commits / run | 127 | 105 | −17% |
| | `_handle_tool_output` SQL · ms | 1105 · 1568 | **50 · 115** | −95% · −93% |
| | `_handle_streaming_event` SQL · ms | 785 · 1085 | **121 · 248** | −85% · −77% |
| | whole run | 45.0s | 41.1s | −9% |
| S3 ×3 | SQL statements / run | 3248 | **1387** | **−57%** |
| | `_handle_tool_output` ms | 2304 | **71** | −97% |
| | max event-loop lag | 850 ms | **161 ms** | −81% |
| S2 ×2 | SQL / run · tool time | 1450 · 3.9s | 764 · 2.9s | −47% · −26% |
| S4 follow-up | SQL / run · tool batch | 2825 · 10.9s | 1148 · 5.0s | −59% · −54% |
| S5 200k CSV | SQL / run · run | 1443 · 28.8s | 767 · 26.1s | −47% · −9% |
| S6 | steer applied · Stop acknowledged | 1033 · 1251 ms | 1033 · 1268 ms | = |
| gates | runs passing all gates | 17/18 ¹ | **12/12** | |

¹ The one baseline failure is a pre-existing shared-session race in parallel
tools (`create_data._resolve_active_tables raised … concurrent operations
are not permitted`). PR-B fixes it.

### P1 — Postgres with 15 ms latency

| scenario | metric | before | after | Δ |
|---|---|---|---|---|
| S1 ×2 | whole run | 74.5s | **63.5s** | −15% |
| | parallel tool batch | 39.7s | **28.2s** | −29% |
| | SQL / run | 2799 | 1202 | −57% |
| S3 ×2 | whole run | 80.4s | **68.3s** | −15% |
| | parallel tool batch | 47.8s | **28.4s** | −41% |
| | max event-loop lag | 930 ms | 328 ms | −65% |
| gates | | 4/4 | **4/4** | |

### SQLite (single-writer)

| scenario | metric | before | after | Δ |
|---|---|---|---|---|
| S3 | whole run · tool batch | 44.3 · 20.3s | **34.6 · 12.9s** | −22% · −36% |
| | SQL / run · commits | 1872 · 145 | 868 · 115 | −54% · −21% |
| S6 | steer applied · Stop acknowledged | 1147 · 2167 ms | 1143 · 1396 ms | = · −36% |
| gates | | 2/2 | **2/2** | |

Correctness totals across all 18 runs after the change:
- 82 event→fetch checks, **0 mismatches**;
- table usage recorded 16/16;
- 0 new log errors.

A UI reload after every run showed the same rows and charts as the live
stream.

## Unit tests

`backend/tests/unit/test_agent_step_persistence.py` (7) covers:
- creation is one commit (table and chart);
- finalize is one commit for 1 and 250 rows, and the data, parameters,
  applied params and visualization are all persisted;
- data survives a failing parameter write;
- the completion bus event still carries what Stop and steering read.

## Not changed / not verified

- API routes (`/api/steps`, widgets, queries) still use the old helpers.
- Slack delivery was not exercised live (no Slack workspace in the sandbox).
  The routing check runs before the old code path, which is otherwise
  unchanged.
