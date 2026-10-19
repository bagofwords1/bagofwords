# Feedback Loop — MCP tools past the cap: native, searched, or guessed

The agent offers each MCP / custom-API tool on the run's agents as its own tool
(`mcp__<connection>__<tool>`). Past `tools_max` (org setting, default 50) the
rest must stay reachable without a gateway the model has to fill by hand.
This loop validates the cap, the `search_tools` + `call_tool` path, the stable
tool order, the strictest per-agent setting, and how the calls render in the UI.

## What was wrong (validated)

- The `execute_mcp` gateway and `search_mcps` were offered on every MCP run,
  even with every tool native; the model sometimes searched for tools it
  already had.
- The capped-off tools had no path except the gateway.
- A tool on two of the run's agents took "the first per-agent setting found"
  (`tool_policy_service.py` `resolve_for_run`), and the tool list used a
  different "last one loaded" rule, so the list and the call could disagree.
- Tools were loaded with no `ORDER BY`, so the tool list could change between
  runs (prompt cache) and so could which tools fell past the cap.
- Finding from the live run: past the cap, Haiku 5.5 called the hidden tools
  by a guessed `mcp__` name with guessed arguments (`artist_id: "90"`). The
  `<mcp_tools>` index listed them like the native ones, and it was rendered
  before registration knew which tools were deferred
  (`agent_v2.py` renders the schema excerpt before `_register_native_mcp_tools`).

## Loop A — deterministic (stubbed LLM and MCP provider)

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest -q tests/e2e/rbac/test_mcp_tools_cap.py \
  tests/unit/test_mcp_tool_registry.py tests/unit/test_mcp_native_schema_placement.py
```

`test_mcp_tools_cap.py` covers: under the cap no gateway tools are offered;
past the cap the most used are native and the rest are found and called;
identical tool list across runs; `search_tools` only returns tools the user
can reach (admin vs member, Auto); the strictest setting wins for a shared
tool; nothing is offered when the org switches MCP tools off; the context
index marks exactly the deferred tools.

Checked against the old behavior: restoring "first setting found" fails the
two strictest-setting tests; dropping the schema re-render after registration
fails the deferred-marker assertion.

## Loop B — live (Haiku 5.5, real UI)

Full stack (`tools/agent/boot_stack.sh`), the Music Store agent (Chinook +
the mock music-store MCP server, 5 tools), Playwright driving the UI:

1. Default cap — "find Iron Maiden and the album with the most tracks":
   `find_artist` and `get_artist_albums` are called natively and render in
   the MCP tool view (input + output). Answer: Live After Death, 18 tracks.
2. Settings → AI Settings → Tools & MCP → **Tools max** = 2.
3. Same question: 2 tools native, 3 behind search. Before the marker fix the
   model guessed hidden tool names and argument types and gave up; after it:
   native `find_artist` → `search_tools("get_artist_albums")` →
   `call_tool(name="mcp__music-store-mcp__get_artist_albums", arguments={"artist_id": 90})`
   → correct answer.

Screenshots: `media/pr/perf-agent-latency/`.

## The fix

- `app/ai/tools/mcp_tool_registry.py` — `build_mcp_tool_catalog`: strictest
  setting, usage ranking (30 days, cached a day), cap, sorted output,
  deferred list; `tools_max` (legacy `mcp_native_tools_max` honored when saved).
- `app/services/tool_policy_service.py` — `strictest_admin_setting`, used by
  the list and by `resolve_for_run`.
- `app/ai/tools/implementations/search_tools.py`, `call_tool.py` — new; the
  agent rewrites `call_tool` into `execute_mcp` (short names accepted when
  unambiguous).
- `app/ai/agent_v2.py` — never offers `execute_mcp`; offers search only when
  tools are deferred; marks them in the context and re-renders the schema
  excerpt once.
- Frontend — native `mcp__…`, `call_tool` and `search_tools` render with
  `MCPTool.vue`; a finished native call is labelled `tool · connection`.
