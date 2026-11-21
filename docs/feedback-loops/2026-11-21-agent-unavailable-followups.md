# Feedback Loop — "If a connector is offline or I'm not signed in, does chat say so?"

Reported: when a connector is offline, or the user has not signed in to it, the
agent disappears from the conversation or the request errors, instead of
telling the user what is wrong (#1825).

The sign-in half for ordinary agents is covered by
`2026-11-21-agent-signin-required.md`. This loop covers what that one left out:

| Case | Before | After |
|---|---|---|
| Agent whose connection is unreachable | "None of the agents available to me has sales data" | "That comes from the **Sales** agent, which is unreachable right now… an admin needs to check its connection" |
| The user's own connector (mail, Teams, per-user MCP), not signed in | "None of the agents available to me has mail data" | Connect card for the connector → sign in → **Ask again** answers from it |
| Folder-scoped conversation whose agents all need sign-in | `Agent failed: 409: No available agents in this folder` | Connect card → sign in → **Ask again** answers |
| MCP clients (`get_context`, `create_report`, `create_data`, `inspect_data`) | agent missing from the result, or "not found or not available to you" | `unavailable_agents` with `reason` and `action`; errors name the agent and say to sign in |

## Root cause (validated)

- **Unreachable.** `Connection.is_active` is a cached health flag: three failed
  probes in a row clear it (`ConnectionService._record_probe_outcome`).
  `DataSourceService.construct_clients` builds no client for such a connection
  and returns `{}` without an error, and `AgentV2.__init__` drops any agent
  with no client. The schema builder already flags a down connection when a
  sibling is up (`<unavailable_connections>`), but an agent with nothing else
  rendered nothing at all.
- **Connectors.** A person's own connectors join every run
  (`resolve_run_agents`), but one they never signed in to gets no client and is
  dropped. `<connectors>` only lists connected ones, and the sign-in block
  skipped connectors on purpose.
- **Folders.** `resolve_run_agents` filtered a folder's agents by sign-in
  before the run and raised `409 No available agents in this folder` when none
  was left, so the run never reached the point where the planner is told.
- **MCP.** `get_context` and `build_rich_context` filtered the same agents out
  and only logged their names; `create_report` answered "not found" for an
  agent the user can see in the selector with a Connect button.

## Loop A — deterministic (no external services)

The LLM boundary is stubbed; routes, services, context building, the tools and
the agent loop run for real.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/test_agent_unavailable.py -m e2e --db=sqlite -q
```

| Check | Fix stashed | With fix |
|---|---|---|
| an unreachable agent is listed in `<agents_unreachable>` and nowhere else; it returns once reachable (member and admin) | FAIL `planner must be told which agents are unreachable` | PASS |
| the same when it is the run's only agent (no client at all) | FAIL | PASS |
| an unconnected connector is listed for sign-in and `request_agent_signin` accepts it | FAIL `an unconnected connector is listed for sign-in` | PASS |
| with another agent pinned, the user's connector is still offered | FAIL | PASS |
| a folder whose agents need sign-in runs and asks for it | FAIL `…must not fail the request: {"detail":"No available agents in this folder"}` | PASS |
| MCP `get_context` reports left-out agents with a reason; `create_report` names the agent that needs sign-in and stays silent about unknown ids | FAIL `KeyError: 'unavailable_agents'` | PASS |

Nearby suites (the sign-in loop, agent folders, MCP reach, the MCP tools,
smart-instruction planner prompts, domains, the pinned
`test_report_context_datasource_dropped.py`) are green with the change.

## Loop B — live, through the UI as a real member

Stack: `tools/agent/boot_stack.sh --dev` with `BOW_ENCRYPTION_KEY` pinned, plus
a local Postgres 16 with database `workspace` (`messages`, `budget`), a service
login and a personal login.

```bash
cd backend
uv run python ../tools/agent/stub_llm_unavailable.py &                 # scripted model on :9099
uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9099/v1
uv run python ../tools/agent/seed_org.py --invite member@example.com
uv run python ../tools/agent/seed_agent_unavailable.py --pg-password …  # Sales, Inventory, Mailbox, Finance + folder
uv run python ../tools/agent/seed_agent_unavailable.py --sales-down     # right before the offline scenario
cd ../frontend
node ../tools/agent/agent_unavailable_flow.mjs offline   ../media/pr/agent-unavailable
DB_PASSWORD=… node ../tools/agent/agent_unavailable_flow.mjs connector ../media/pr/agent-unavailable
DB_PASSWORD=… node ../tools/agent/agent_unavailable_flow.mjs folder    ../media/pr/agent-unavailable
# before: stash backend/app, restart the backend, add --before
uv run python ../tools/agent/seed_agent_unavailable.py --forget-signins  # reset between runs
```

- **Sales** — `system_only` SQLite; its connection flagged unreachable.
- **Inventory** — `system_only` SQLite, reachable.
- **Mailbox** — Postgres, each person signs in, marked as a connector.
- **Finance** — Postgres, each person signs in, in folder "Finance team" that
  the member may view.

**LLM:** the sandbox's Anthropic key has no credit ("credit balance is too
low"), so the run uses `tools/agent/stub_llm_unavailable.py`, a scripted
OpenAI-compatible model that acts **only on what the planner prompt shows it**:
the agent the question is about is in `<agents_unreachable>` → it says so; in
`<agents_needing_signin>` → `request_agent_signin`; its table is in the schema
→ `create_data`; none of these → "no agent has that data". The before/after
difference therefore comes from the prompt this change builds. A run with a
funded real model is still to do.

Observed (two consecutive passes after the fix, one before):

| Scenario | Before | After |
|---|---|---|
| offline | "None of the agents available to me has sales data" | "That comes from the **Sales** agent, which is unreachable right now…"; no Connect card |
| connector | "None of the agents available to me has mail data" | Connect card for Mailbox → sign in → Ask again → the member's three unread messages (`billing@vendor.example`, `dana@acme.example`, `lee@acme.example`), queried with their own login |
| folder | `Agent failed: 409: No available agents in this folder` | Connect card for Finance → sign in → Ask again → budget by department |

Evidence: `media/pr/agent-unavailable/` (`*-before.png`, `*-after.png`,
`*-credentials.png`, `*-connected.png`, `*-answer.png`).

## The fix

- `backend/app/ai/tools/implementations/agent_focus_common.py`
  - `agent_is_unreachable(ds)`: every connection flagged unreachable, tool
    providers excepted (they are built whatever the flag says).
  - `folder_branch_agents`: a folder report's agents before the sign-in filter.
    `resolve_run_agents` uses it and raises the 409 only when the folder really
    has no agent for this user.
  - `signin_scope_agents`: the run's working set plus a folder's agents that
    were filtered out for sign-in.
- `backend/app/ai/context/agent_roster.py` — `render_unreachable_agents_xml`
  (`<agents_unreachable>`, name and one-liner, no tool to call), and
  `render_signin_required_xml` now marks connectors.
- `backend/app/ai/agent_v2.py` — the sign-in block covers connectors and
  folder agents; `_unreachable_block` adds the unreachable ones; both groups
  stay out of the schema and roster.
- `backend/app/ai/tools/implementations/request_agent_signin.py` — accepts ids
  from the same scope, so a connector or a folder agent gets its card.
- MCP: `get_context` returns `unavailable_agents`; `create_report` names an
  agent that needs sign-in; `create_data` / `inspect_data` say why no source
  could be connected.

## What this proves / notes

- The planner is told about every agent a run could not load for a reason the
  user or an admin can act on, without seeing its schema.
- **Scope left out:**
  - An agent whose client failed to build for another reason (misconfigured,
    no connection at all) is still dropped silently.
  - An unreachable agent gets no card or chip in the UI: the reply is the
    planner's text. A deterministic chip is a possible follow-up.
  - Creating a conversation with an agent that needs sign-in pinned still
    drops the pin (`report_service`); the conversation becomes Auto, where the
    block applies.
- **Sandbox pitfalls hit:**
  - The connection status sweep re-probes on its interval and after a restart,
    so a flag set by `--sales-down` on a healthy SQLite file is undone. Run it
    right before the offline scenario.
  - Postgres data kept under the session scratch directory lost its
    permissions mid-run and the server stopped; keep it under the `postgres`
    user's home.
  - Stashing files under `backend/app` hung uvicorn's reload; kill the process
    tree and start the backend again with the same `BOW_ENCRYPTION_KEY`.
