# Feedback Loop — MCP historical questions and eval management

An authorized agent manager should discover `bow.runs` and `bow.tool_calls`
through MCP `get_context`, query them with `create_data`, and curate and run
agent evals from the same external session. Ordinary members must not gain
monitoring or eval-management access.

## Root cause (validated)

- `backend/app/ai/context/builders/schema_context_builder.py:956` previously
  admitted the history catalog only in training mode. MCP chat reports therefore
  omitted the built-in tables even for agent managers.
- `backend/app/ai/tools/mcp/context.py:128` previously built clients only for
  persisted report agents; it never installed the authenticated BOW client.
- `backend/app/ai/tools/mcp/__init__.py:33` previously had no eval tools in the
  external registry, although internal eval tools already implemented validation,
  authorization, persistence, background execution, and result inspection.

## Loop A — deterministic reproduction

Use Python 3.13 and the backend development dependencies. From `backend/`:

```sh
uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/rbac/test_mcp_training_capabilities.py -q --db=sqlite
```

The initial discovery/catalog reproduction observed **3 failed, 1 passed**:
admins and single-agent managers received no BOW tables, and the MCP eval tool
catalog was absent. The ordinary-member exclusion passed.

The extended regression suite seeds users, agents, management grants, historical
runs, tool calls, and eval cases through shared API fixtures. It stubs only the
LLM boundary with generated Python that queries the real authenticated BOW
client. Provider credentials are inert test values; no external service is used.

## The fix

- MCP schema discovery explicitly includes the history capability, still gated
  by `resolve_console_scope`. The report mode used for instruction resolution
  is preserved.
- MCP rich context installs the BOW client using the same scope check. Every
  query rechecks current permissions, limits results to authorized reports,
  and protects persisted monitoring snapshots using existing access lineage.
- Seven MCP adapters invoke the existing internal eval implementations:
  search, create, edit, run, stop, list runs, and inspect a run. Tool listing
  and invocation use the existing `manage_evals` gate. Individual cases and
  runs retain all-target agent authorization. No training report/wake consumer
  is fabricated; background runs are inspected with `get_eval_run`.

Re-running Loop A after the fix observed **12 passed** on SQLite (65.55s).

## What this proves / regression notes

The tests cover discovery and real `create_data` execution for both datasets as
an org admin, a single-agent manager, and a member; exact authorized run IDs;
persisted query/visualization results; eval catalog access; scoped eval search;
allowed and denied authoring; cross-agent/global edit and execution denial;
and run visibility, deduplication, detail, and stopping.

Additional regression command:

```sh
uv run pytest tests/e2e/rbac/test_rbac_eval_tools_scope.py tests/e2e/rbac/test_bow_source.py tests/unit/test_eval_loop_tools.py tests/unit/test_run_eval_input.py -q --db=sqlite
```

All **66 existing regression tests passed**. An early combined run also included
our new run-lifecycle test before its fixture configured an LLM provider; it
failed at REST pending-run creation with "No default LLM model configured".
The fixture now configures an inert test provider, and the final complete MCP
suite passes all 12 tests. No live LLM quality evaluation or PostgreSQL run is
claimed by this loop.
