# Feedback loop — app chatbot tools match regular agent chat

An app conversation must use the regular agent tool catalog for the same user,
selected agents, capabilities and disabled-tool settings. Only dashboard viewer
chat has a special read-only allowlist.

## Validated cause

App conversations and dashboard viewer chat both use the `artifact_chat` report
type. `backend/app/ai/agent_v2.py:1032` applied the dashboard tool allowlist to both.
This removed MCP gateways before native tool registration, so a support chatbot
could not see its selected agent's repository tools.

Adding MCP alone was insufficient: a real app-versus-regular-chat comparison
still failed with missing tools including `add_parameter`, `create_artifact`,
`create_doc`, and delegation tools. App prompt guidance and list/browser guards
also assumed every artifact-chat thread was read-only.

## Reproduce and verify

Use Python 3.13 and the backend development dependencies:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/e2e/rbac/test_mcp_tools_run_scope.py \
  tests/unit/test_app_surface_prompt_slim.py \
  tests/unit/test_artifact_chat_surface_prompt.py \
  tests/e2e/rbac/test_agent_lists.py \
  tests/e2e/rbac/test_sandbox_list_writes.py \
  tests/e2e/rbac/test_delete_list_rows_tool.py \
  tests/e2e/test_artifact_browser_policy.py \
  --db=sqlite -q --disable-warnings
```

For PostgreSQL, use `--db=postgres` (Docker required). For delegation:

```bash
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/e2e/test_sub_agents.py tests/e2e/test_sub_agent_messages.py \
  -k 'wave_fans_out_runs_children_hidden_and_fans_in or child_question_creates_one_queued_parent_wake' \
  --db=sqlite -q --disable-warnings
```

The fixtures create isolated users, agents, connections, apps and conversations.
Only external boundaries such as the LLM and MCP provider are stubbed; the app
route, agent loop, registry, permission gateways and database run real.

Before the fix, the new app-route regression failed because the planner's
native MCP tool set was empty. The stricter parity regression then failed:

```text
assert offered == regular_catalog
AssertionError: {'extra': set(), 'missing': {'add_parameter', 'cancel_scheduled_task',
'cancel_wait', 'create_artifact', 'create_doc', 'create_memory', ...}}
```

Removing the special app filter exposed another report-type gate: delegation
tools were still missing. That gate now distinguishes apps from dashboards, and
the delegated lifecycle test verifies the app surface survives child runs and
the final parent response.

## Fix

- Apps bypass the dashboard-only allowlist and use normal agent tool filtering.
- App prompt guidance no longer says tools are read-only and includes normal
  artifact-authoring guidance. App delivery instructions still describe what
  this UI actually displays.
- `backend/app/services/conversation_surface.py` distinguishes the surfaces for
  list registration, submission/deletion, sandbox list writes and browser policy.
  Missing/unknown surface context retains dashboard restrictions.
- Delegated children, question wakes and final parent turns inherit the exact
  originating turn's app context rather than whichever message arrived last.
- The built-in chatbot skill documents regular-agent tool availability.

## What this proves / limits

The app catalog equals regular chat for both admin and member using the same
agent selection. Selected MCP tools execute and their results reach the model;
unselected connections remain inaccessible. List permissions, explicit viewer
submission opt-in, deletion confirmation and browser policy still apply.
Dashboard catalog and write restrictions remain intact. Delegation is tested
through real child runs and the final parent response.

No schema migration, production data modification or frontend change is needed.
The deterministic loop does not verify live customer GitHub parsing, OAuth/OBO,
provider credits, browser presentation of every tool card, or host-app handling
of asynchronous replies. Tool availability does not imply every host UI renders
every specialized tool interaction.

During development, verification caught and corrected test-helper argument
mistakes and an assertion that confused a documentation marker with actual host
activity. These were introduced by this work, not attributed to the baseline.


## Observed final verification

| Suite | Result |
|---|---|
| SQLite prompt, sandbox list, delete-list and browser policy suites | 87 passed |
| PostgreSQL app parity, native list permissions, sandbox lists, deletion and browser regressions | 14 passed |
| SQLite real delegated lifecycle (regular and app) plus queued-question regression | 3 passed |

The PostgreSQL regression run also rechecked the deleted-list test whose helper
was corrected during development. All listed final runs have zero failures.
