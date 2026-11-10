# Feedback Loop: "tool/plan_type mismatch" and "Input should be a valid dictionary" with OneDrive + custom API agents

**Reported:** when a OneDrive agent and a custom API agent are in the same
report scope, custom API calls fail with `resolve_error: tool/plan_type
mismatch`, and `execute_mcp` returns `arguments: Input should be a valid
dictionary`. Each agent works on its own.

## Root cause (validated vs. open)

**Validated: the "plan_type mismatch" message is wrong.**
- `_validate_tool_for_plan_type` (`backend/app/ai/agent_v2.py`) does not
  compare plan types. It returns False only when the tool name is not
  registered.
- So the error means *the run did not have that tool name*: an `mcp__…` name
  that `_rewrite_native_mcp_action` could not route. That happens when the
  name is stale (replayed from an earlier run's history), made up, or missing
  from this run's routing table.

**Validated: the "valid dictionary" error.**
- `ExecuteMCPInput.arguments` is `Dict[str, Any]` with no pre-validation.
- A model that calls `execute_mcp` directly with `arguments` as a JSON
  *string* is rejected by pydantic in `tool_runner`.

**Open: what OneDrive changes in the customer's setup.**
- A custom API agent beside a file-source agent (OneDrive, SharePoint, Google
  Drive) works in every setup tried here, on SQLite and on PostgreSQL:
  - with and without a persisted focus on the file agent;
  - with per-user (`user_required`) OneDrive connections.
- Two theories are ruled out by those runs:
  - an aborted Postgres transaction from `_stamp_index_times`;
  - focus-on-use narrowing the MCP catalog.
- The customer's backend log will name the trigger. Look for
  `mcp tool catalog failed; falling back to execute_mcp: <exc>`,
  `[agent] MCP tool registration failed, offering execute_mcp`, or the new
  `[agent] unknown tool mcp__… (native mcp tools: N, …)`.

## Loop A: deterministic reproduction

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_custom_api_tools_beside_file_agent.py -m e2e --db=sqlite
# PostgreSQL leg (no Docker on a devbox: point at a PG sandbox)
TEST_DATABASE_URL=postgresql://… uv run pytest tests/e2e/rbac/test_custom_api_tools_beside_file_agent.py -m e2e --db=external
```

**Before the fix:** `2 failed, 6 passed` on both databases.
- `execute_mcp` rejected `arguments='{"days": 30}'` with *Input should be a
  valid dictionary*.
- An unknown `mcp__…` name came back as `tool/plan_type mismatch`.
- The six beside-a-file-agent cases passed: the co-scope itself works here.

## The fix

- **`ExecuteMCPInput`** decodes `arguments` sent as a JSON-encoded object.
  `None` or empty becomes `{}`. Non-objects are still rejected.
- **Unknown tools** are refused before the plan-type check as `unknown_tool`.
  For an `mcp__` name the refusal lists the MCP tools the run *can* call (and
  points to `search_tools` when some are deferred), so the model fixes a stale
  name in one step. It also logs a warning with the native tool count, which
  is the diagnostic the open question above needs.

**After the fix:** `8 passed` on SQLite and PostgreSQL. Related suites:
- `tests/unit -k "mcp or execute or tool_runner or agent_v2"`: 562 passed.
- `tests/e2e/rbac/test_mcp_tools_cap.py`: 12 passed.
- Tests asserting unknown-tool behaviour: 118 passed.

## What this proves, and regression notes

- **Both reported errors** are fixed for any run, whatever the agents.
- **The regression test keeps the general invariant:** custom API tools stay
  native and callable beside any file-source agent, focused or not.
- **Not proven:** why the customer's run lost the tool name. Close the open
  question with their log before calling the root cause fixed.
