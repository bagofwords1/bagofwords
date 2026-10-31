# Feedback loop — chatbot says repository tools are unavailable

An app conversation should offer and execute tools on its selected agents while
preserving viewer access checks. Dashboard viewer chat remains read/query-only.

## Validated cause

Both app conversations and dashboard viewer chat use the `artifact_chat` report
type. `backend/app/ai/agent_v2.py:542` previously removed `read_artifact` for
the app surface but otherwise retained the dashboard-only allowlist. This
removed `execute_mcp` before native MCP tool registration, so the planner could
not see the selected agent's tools.

Lists had a second restriction:
`backend/app/ai/tools/list_tool_registry.py:31` and
`backend/app/ai/tools/implementations/submit_list.py:101` rejected every
artifact-chat report, including apps.

## Reproduce and verify

Use Python 3.13 and the backend development dependencies:

```bash
cd backend
uv sync --frozen --extra dev
mkdir -p db
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/e2e/rbac/test_mcp_tools_run_scope.py \
  tests/e2e/rbac/test_agent_lists.py \
  tests/unit/test_app_surface_prompt_slim.py \
  --db=sqlite -q --disable-warnings
```

For PostgreSQL, run the same command with `--db=postgres` (Docker required).
The fixtures create isolated organizations, users, agents, connections, apps,
and conversations. Only the LLM and external MCP provider are stubbed; the app
HTTP route, agent loop, tool registry, gateway and database run real.

Before the fix, the new app route regression failed at:

```text
test_app_conversations_offer_and_execute_only_selected_agent_tools[shared-admin]
assert _native(offered, org['agents']['shared']['slug'])
AssertionError: ['clarify', 'create_data', 'describe_entity', ...]
assert set()
```

The test records the planner's offered catalog, then forces a gateway call.
Admin and member can execute the selected agent's tool; neither can reach an
unselected agent's connection. The successful tool result reaches the planner.
The list tests cover admin submissions, rejected viewer submissions, and a
viewer allowed by the list's explicit opt-in. The existing dashboard test
continues to reject list registration and direct submission.

## Fix and limits

The existing app surface discriminator enables MCP tools/resources and list
submissions; no new report type or migration is introduced. List registration
receives the same per-message context as execution. Agent scope and submission
permission checks remain in their existing gateways. The app message route
constructs this context server-side and rechecks access on every message;
surface metadata is not an authorization grant.

The built-in chatbot skill now describes these capabilities and their permission
requirements. This fix does not enable artifact editing or other administrative
tools in the app catalog.

This loop proves runtime tool availability and scope with deterministic providers.
It does not prove live GitHub response parsing, customer OAuth/OBO, provider credit
availability, or browser rendering. Those are separate boundaries.
