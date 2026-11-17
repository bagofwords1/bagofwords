# Feedback Loop — an agent's MCP tools are listed to the model but not callable

An agent that combines a warehouse connection with an MCP tool connection can
lose every one of its MCP tools in a chat. Meanwhile the model is told those
tools are "in your tool list… call it directly". The model then tells the user
it can see the tools but cannot call them, and suggests workarounds that don't
help (start a new chat, re-select the agent). This loop reproduces the
three defects behind that and shows each one fixed.

## Root cause (validated)

Three independent defects, each reproduced by a failing test below.

**A. One failing connection takes the whole agent, and all its MCP tools, out of the run.**
`prepare_run_agents` (`backend/app/ai/tools/implementations/agent_focus_common.py:366`)
called `DataSourceService.construct_clients` in its default all-or-nothing
mode. A single connection that could not be set up for the running user (an
unreachable server, a missing per-user sign-in) raised, the error was only
logged, and the agent got no clients. `AgentV2.__init__` then dropped it from
the working set (`backend/app/ai/agent_v2.py:816`, `_has_client`). The MCP
catalog is scoped to that working set (`_register_native_mcp_tools`,
`agent_v2.py:9670`, via `_run_agent_ids()`), so the agent's healthy
connections and every MCP tool on it disappeared.

The filter at `agent_v2.py:816` only applies when *some* client exists
(`if clients:`). With a single agent in the run nothing is dropped, so the
bug only shows in workspaces with more than one agent. That trap is why
the regression test always includes a sibling agent.

The per-connection mode (`connection_errors=[]`) already existed and is used by
dashboard runs, publishing and report reruns. A failing connection gets an
`UnavailableConnectionClient` stand-in that raises its real error only when
queried.

**B. An agent attached mid-run never gets its MCP tools.**
The MCP catalog was built once, before the first planner call (`agent_v2.py:5882`).
The mid-run block (`agent_v2.py:6072`) built clients and re-rendered schemas
and instructions for newly attached agents, but never rebuilt the MCP
catalog. Worse, `create_agent` attaches to the report without changing the
focus, so the change-detection key (`_current_focus_key`, `agent_v2.py:1503`)
never fired: no clients were built and no schema was rendered for the new agent either.

Fixing that exposed a race. `_refresh_browser_tool_catalog`
(`agent_v2.py:9849`) runs as a background task during the loop head. It
snapshotted `planner.tool_catalog`, awaited a DB policy check, then assigned
a list derived from the stale snapshot, overwriting anything changed in
between. It passed on SQLite and failed on Postgres, where the await is slow
enough for the rebuild to land inside it.

**C. Focus / discovery observations claim tools are callable when they are not.**
`set_report_agents` and `search_agents` put the focused agents' schema into
their observation via `render_agents_full` (`agent_focus_common.py:445`).
That built a fresh `SchemaContextBuilder` and skipped
`ContextHub._apply_mcp_catalog`, the filter that keeps `<mcp_tools>` in sync
with the registered tools. It listed every MCP tool of the agent under the
default note *"Each tool above is in your tool list as mcp__…: call it
directly"* (`backend/app/ai/context/sections/tables_schema_section.py:712`),
even for an agent the run had dropped.

Observed (before fix C) in the `set_report_agents` observation for a dropped agent:

```
<mcp_tools>
<connection name="c94d951" type="mcp" id="…">
<tool name="echo">…</tool> … <tool name="failing_tool">…</tool>
</connection>
<note>Each tool above is in your tool list as mcp__&lt;connection&gt;__&lt;tool&gt;, … call it directly.</note>
</mcp_tools>
```

…while the next request's tool list held none of `mcp__c94d951__*`.

## Loop A — deterministic reproduction (no external services)

`backend/tests/e2e/rbac/test_agent_mcp_tools_reach.py`. The LLM is a
scripted planner that records every request's tool list and tool results. The
MCP provider is `MockToolProviderClient`. A connection "cannot be set up" by
making the MCP driver (`McpClient.__init__`) raise for that server, which is
the driver boundary. Routes, services, the agent loop and the tool gateway run
for real.

```bash
cd backend
uv sync --frozen --extra dev
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
uv run pytest tests/e2e/rbac/test_agent_mcp_tools_reach.py -m e2e --db=sqlite -p no:cacheprovider -q -rA
```

| Test | Invariant |
|---|---|
| `test_a_failing_connection_does_not_hide_the_agents_other_tools[auto\|pinned × admin\|member]` | the agent's healthy connections keep their MCP tools in the tool list |
| `test_healthy_agents_are_unaffected_by_another_agents_failing_connection` | bounded scope: an agent with no usable connection stays out; siblings unchanged |
| `test_an_observation_never_lists_a_tool_the_next_step_cannot_call[set_report_agents\|search_agents]` | every tool an observation lists without `deferred="true"` is in the next request's tool list |
| `test_an_agent_created_mid_run_brings_its_tools_into_the_next_step` | `create_agent` in training mode → its tools are offered from the next step |

**Before** (app code at `HEAD`, test file present):

```
PASSED test_healthy_agents_are_unaffected_by_another_agents_failing_connection
FAILED test_a_failing_connection_does_not_hide_the_agents_other_tools[auto-admin]
FAILED test_a_failing_connection_does_not_hide_the_agents_other_tools[auto-member]
FAILED test_a_failing_connection_does_not_hide_the_agents_other_tools[pinned-admin]
FAILED test_a_failing_connection_does_not_hide_the_agents_other_tools[pinned-member]
FAILED test_an_observation_never_lists_a_tool_the_next_step_cannot_call[set_report_agents]
FAILED test_an_observation_never_lists_a_tool_the_next_step_cannot_call[search_agents]
FAILED test_an_agent_created_mid_run_brings_its_tools_into_the_next_step
7 failed, 1 passed
```

Each fix was also reverted on its own, to check that its tests fail for the
right reason. The observation tests fail with
`listed as callable but not offered: ['mcp__<conn>__echo', …]`. The mid-run
test fails with the new agent's `mcp__<conn>__*` names missing from request 1.

The bounded-scope test passes before and after, as designed: it guards that
fix A does not keep an agent with *no* usable connection in the run.

## The fix

- **A** `agent_focus_common.prepare_run_agents` and
  `AgentV2._ensure_clients_for_attached` build each connection on its own
  (`connection_errors=[]`) and log each unavailable connection. If every
  connection fails, `construct_clients` still raises, so an agent with
  nothing usable stays out exactly as before.
- **B** `AgentV2._refresh_native_mcp_tools` rebuilds the MCP catalog in the
  mid-run block, only when the working set differs from the one the catalog
  was built for. That keeps the tool list (the first prompt-cache breakpoint)
  stable otherwise. `_register_native_mcp_tools` is now re-entrant: it drops
  the previous build's native tools and reuses the gateway descriptors it took
  out on the first build, so filters that removed `execute_mcp` still apply.
  `_current_focus_key` now includes the report's attached agents, so
  `create_agent` triggers the mid-run block. `_refresh_browser_tool_catalog`
  merges only its browser decision into the catalog as it is after the await.
- **C** `ContextHub.apply_mcp_catalog_to(sections)` exposes the existing
  filter. `render_agents_full` takes the run's hub (passed by
  `set_report_agents` and `search_agents` from `runtime_ctx`), applies it, and
  for an agent whose tools were all filtered out, adds a note saying they are
  not in the tool list at this step and must not be called.

**After:**

```
8 passed in 22.56s   # --db=sqlite
8 passed in 32.56s   # --db=external (Postgres 16), run 3× — 3/3 green
```

## What this proves / regression notes

- A failing connection no longer hides an agent's other MCP tools, for admins
  and members, in Auto and with a pinned selection. The tools of the failing
  connection are still in the catalog, and calling one returns its real
  connection error. That gives the model something accurate to tell the user
  (for example "sign in to this connection") instead of a guess.
- The model is never told an MCP tool is callable unless it is in the next
  request's tool list (or marked deferred, behind `search_tools`).
- Regression suites run with the change:
  - SQLite (`-n 4`): `test_artifact_browser_policy`, `test_browser_connector_run_scope`,
    `test_artifact_automatic_check`, `test_mcp_tools_cap`, `test_mcp_apps_access`,
    `test_viewer_run_multi_connection`, `test_agent_folders`,
    `test_repro_auto_agent_rerun` and the new file: **119 passed**.
    The same set on Postgres: **119 passed**.
  - Broader sweep `tests/e2e -k "mcp or focus or search_agents or create_agent or set_report or training or custom_api or connection_tool"`:
    344 passed, 5 failed. All 5 fail identically with the app changes
    stashed, so they were failing before this change:
    `test_mcp_training_capabilities::test_mcp_eval_runs_preserve_read_stop_and_dedup_authority`
    and four in `test_training_summary_instruction_state.py`.
  - Unit: `test_mcp_native_schema_placement`, `test_mcp_tool_registry`,
    `test_decide_focus_mode`, `test_focus_keeps_builtin_bow_section`,
    `test_focus_on_use` pass. Also failing before this change, with the app
    changes stashed: `test_agent_focus_scope::test_manual_scope_is_selection_only`,
    and `test_obo_stored_login_recovery` (needs `libodbc.so.2`, missing in this
    sandbox).
- `tests/e2e/test_usage_limits.py`'s `construct_clients` test double now
  accepts `**kwargs` for the new keyword argument.
- Not covered: the reason a connection failed is only in the backend log and
  in the stand-in client's error at call time. Surfacing it in the model's
  context up front would be a follow-up.
