# Feedback Loop — "As a manager I only see prompts whose report explicitly selects my agent"

On `/monitoring` (Explore, Diagnosis, Cost and the trace modal), an agent manager
(`manage` on some agents, no org-wide `manage_settings`/`manage_connections`)
saw only runs on reports that **pinned** their agents. A report on **Auto** (no
agent picked) was invisible to every manager, even when its user could reach
only agents that manager manages. The claim validated here: the console can show
those Auto runs without exposing anything from an agent the manager does not
manage.

## Root cause (validated)

- Scope check: a scoped caller sees a report only when every agent *linked* to it
  is managed (`ConsoleService._reports_of_data_sources`,
  `backend/app/services/console_service.py`).
- "Linked" was `agent_links()` = pinned agents ∪ agents a folder run used
  (`backend/app/services/folder_monitoring.py`). Auto = `report.data_sources`
  empty (`agent_focus_common.report_selection_is_auto`), and the agents Auto
  resolves to at run time (`resolve_run_agents` → `accessible_agents`) were never
  stored. So an Auto report had no links and was dropped on purpose ("reports
  with no agent never match").
- Observed use (`record_used_agents`) only ran for folder runs
  (`agent_v2.py`, guarded by `report.agent_folder_id`).

Why "used" is not enough: a run's context snapshots hold the schema of **every**
agent it was given, and each run's context replays the conversation so far. So
attribution must be *everything the run was given*, and a turn that reached an
unmanaged agent taints every later turn of its report.

## The fix

1. **Record exposure per run** — new table `execution_exposed_agents
   (execution_id, agent_id)` (migration `exposed01`). `AgentV2._record_exposure`
   writes the run's working set ∪ the report's pins at run start, after every
   tool call (covers `set_report_agents` / `create_agent` pinning mid-run), when
   a mid-run client is built, and in the run's `finally`. Observed use is now
   recorded for every run, not only folder runs.
2. **Visibility rules** (`folder_monitoring.py`):
   - `out_of_scope_runs(S)`: exposure or use outside `S`; runs recorded before
     this change fall back to pins / observed use, and an old Auto run with
     neither stays hidden (unchanged).
   - `visible_runs(S)`: in scope **and** no out-of-scope run at or before it in
     the same report — used by the runs list, summaries, diagnosis metrics, tool
     calls and per-run drill-downs.
   - `reports_in_scope(S)`: every run visible (and no pin outside `S`) — used by
     report-level totals (messages, usage, cost, feedback), the legacy whole-report
     trace, the world view and the BOW monitoring source, which cannot show part
     of a report.
   All three use aliased tables and `.correlate(None)`: they are embedded in
   queries that already select from `agent_executions`, and auto-correlation
   silently bound them to the outer row (caught by this loop).
3. **Trace modal** — `GET /console/reports/{id}/conversation` returns only the
   visible turns plus `hidden_turns`; the usage roll-up and check-ins are limited
   to those turns. The modal shows "N later turns hidden…" (`traceModal.hiddenTurns`
   in all locales).
4. **Agent filter** — a scoped manager's default filter (all their agents) no
   longer drops Auto runs that queried nothing; a narrower filter also matches the
   agents a run was given. Org-wide, an Auto run is filed under agents it used.

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_rbac_console_auto_scope.py tests/e2e/rbac/test_rbac_console_scope.py -q
```

Cast: manager manages A and B, not C. Auto reports: *inside* (runs given {A,B}),
*outside* (given {A,C}, used C), *glimpsed* (given {A,C}, used only A),
*mixed* (turn 1 {A}, turn 2 {A,C}, turn 3 {A}), *legacy* (nothing recorded).
Asserts, for the manager, on every surface (summaries, diagnosis runs with many
queries, tool calls, conversation, per-run trace, by-completion, context
snapshot, legacy trace, llm-usage, metrics): inside visible; mixed shows turn 1
only with `hidden_turns == 2` and turn-1-only usage; outside / glimpsed / legacy
refused; totals exclude mixed entirely; admin unchanged; plain member still 403.

Observed: **34 passed** (16 new + 18 existing scope tests).

Mutation checks (each run, then reverted):

| Mutation | Result |
|---|---|
| prefix rule off (`earlier.created_at <= run.created_at` → `earlier.id == run.id`) | 10 failed |
| exposure ignored (only "used" counts) | 11 failed (the *glimpsed* case — given C, never used it — is what catches this; without it the mutation passed) |

## Loop B — live stack, real agent loop (LLM stubbed)

The Anthropic key in the sandbox had no credit, so the LLM boundary is
`tools/agent/stub_llm.py`; everything else (API, AgentV2, tool execution,
recording, console) is real.

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py --sqlite-sources 3
STUB_SOURCES_FILE=<[sqlite_source_1]> uv run python ../tools/agent/stub_llm.py &   # restart with source_3 before turn2
uv run python ../tools/agent/setup_stub_llm.py
uv run python ../tools/agent/console_auto_scope_live.py setup    # Max manages 1,2; Mia uses 1,2
uv run python ../tools/agent/console_auto_scope_live.py turn1    # Mia, Auto
uv run python ../tools/agent/console_auto_scope_live.py turn2    # admin grants Mia source 3; Mia asks again
uv run python ../tools/agent/console_auto_scope_live.py inspect
node ../tools/agent/console_auto_scope_shots.mjs max.manager@example.com ../media/pr/console-auto-scope/after-turn3-max
```

Observed (recorded by the real agent loop):

```
run 4244d191 exposed=['sqlite_source_1', 'sqlite_source_2']                    used=['sqlite_source_1']
run 558092bf exposed=['sqlite_source_1', 'sqlite_source_2', 'sqlite_source_3'] used=[]
run 8bb7c647 exposed=['sqlite_source_1', 'sqlite_source_2', 'sqlite_source_3'] used=[]
[max] summaries ['How many orders do we have, by region?']
[max] conversation ['How many orders do we have, by region?'] hidden_turns 2
[mia] summaries 403
turn 2/3: trace, by-completion, context snapshot -> 403; legacy whole-report trace -> 403
tool_calls per turn: [2, 0, 0]; later prompts absent from the conversation payload
```

Before (same data, change stashed): Max's Diagnosis shows **0 runs**. After turn 1:
Max sees Mia's Auto run and its full trace. After turn 3: the trace shows turn 1
and "2 later turns hidden". Screenshots: `media/pr/console-auto-scope/`.

## What this proves / regression notes

- Auto runs are attributable to the agents they were given; a manager sees them
  exactly when all of those (and all earlier turns') are theirs.
- Behaviour change: a **pinned** run whose user also had a personal connector
  (mail, Teams, Drive) or any agent outside the manager's set now counts that
  agent in its exposure, so such runs are hidden from that manager. Previously
  they were shown on the strength of the pins alone even though the trace could
  carry that connector's tools/results.
- Public agents count like any other: if a user's Auto set includes a public
  agent the manager does not manage, the run stays hidden.
- Report-level totals count a report only when all its runs are visible, so a
  partially visible report's visible turns appear in the runs list but not in the
  totals (no hidden turn's numbers can leak through a sum).
- The report title is shown with the visible turns; titles are generated from
  the first prompt.
- Pre-existing, unrelated: `GET /console/agent_executions/{id}/context_snapshot/{bad-id}`
  returns 500 for an unknown snapshot id after the access check passes.
- Sandbox note: without a pinned `BOW_ENCRYPTION_KEY`, a backend restart cannot
  decrypt provider credentials; re-save them (the live script's run did).
