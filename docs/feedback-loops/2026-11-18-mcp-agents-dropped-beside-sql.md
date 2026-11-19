# Feedback Loop — "MCP tools not available" when an MCP agent shares a report with a SQL/file agent

Reported: a report with several agents attached (MCP / custom API agents plus a
file agent) could only use the file agent. Every MCP tool call failed as a tool
the run did not have (on older versions worded "not available for plan_type
'action'"). Each MCP agent worked when it was the only agent selected. The
claim validated here is that the MCP agent is dropped from the run *only when
another agent built a query client*, and that the trigger is the cached
connection health flag.

## Root cause (validated)

1. `DataSourceService.construct_clients` skips connections with
   `Connection.is_active = False`, the cached health flag a run of failed
   background probes sets (`app/services/data_source_service.py`, the
   `active_connections` filter). An agent whose only connection carries the
   flag gets **no** query client.
2. `AgentV2.__init__` keeps only agents that have a client
   (`app/ai/agent_v2.py`, `if clients:` / `_has_client`), and that filter runs
   **only when `clients` is non-empty**:
   - MCP agent alone: `clients` is empty, the filter is skipped, and the agent stays.
   - MCP agent + SQL/file agent: `clients` holds the other agent's keys, so the
     MCP agent is dropped.
3. `_register_native_mcp_tools` and `execute_mcp`'s scope both read the
   post-filter working set (`_run_agent_ids`), so the dropped agent contributes
   no `mcp__…` tools and its connection is out of scope.

The flag is irrelevant to tool providers everywhere else: `execute_mcp`
builds its own client per call and never reads it, and tool-provider clients
never reach generated code (`tool_provider_base.codegen_clients`). Only this
one filter honoured it, so an MCP agent's availability depended on which
*other* agents shared the report.

The agent's own `DataSource.is_active` follows its connections only when all
of them are `system_only` (`ConnectionService._sync_agents_with_connection`).
For per-user connections, or agents with mixed connections, the agent stays
live and selectable while its MCP connection is flagged. That is the state
reproduced here.

Not validated: whether the reporting instance had the flag set. Its backend
logs would show it (no `run agents: client construction failed for …` line,
and the connection's `is_active` false). If the client build had *raised*
instead, the agent is excluded by design; see below.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_custom_api_tools_beside_file_agent.py \
  -m e2e --db=sqlite -q -k whatever_other
```

`test_api_tools_are_offered_whatever_other_agents_build_query_clients` uses a
custom API agent served by a real local HTTP server, with its connection
flagged unhealthy. The LLM is stubbed. It runs alone and beside a SQLite agent.

Before the fix:

```
FAILED ...[beside-a-sql-agent] - AssertionError: assert 'mcp__costs…__get_cost_report' in ['add_parameter', 'cancel_scheduled_task', ...]
1 failed, 1 passed
```

## Loop B — live stack, real MCP server

Everything real except the LLM: the backend over HTTP, a real streamable-HTTP
MCP server (`tools/agent/mcp_cost_server.py`, synthetic `cost_report` tool),
and a scripted OpenAI-compatible stub LLM (`tools/agent/stub_llm_mcp_call.py`)
that calls `mcp__<conn>__cost_report` if the run offers it.

```bash
cd backend
mkdir -p /tmp/bow-agent db
export TESTING=true ENVIRONMENT=production \
  BOW_DATABASE_URL=sqlite:///db/agent.db TEST_DATABASE_URL=sqlite:///db/agent.db
# Pin the key: the backend reloads on file changes, and a fresh random key
# makes the stored LLM credentials undecryptable.
export BOW_ENCRYPTION_KEY=$(uv run python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
uv run alembic upgrade head
setsid uv run python main.py > /tmp/bow-agent/backend.log 2>&1 &
MCP_COST_PORT=9310 setsid uv run python ../tools/agent/mcp_cost_server.py > /tmp/bow-agent/mcp.log 2>&1 &
STUB_PORT=9311 setsid uv run python ../tools/agent/stub_llm_mcp_call.py > /tmp/bow-agent/stub.log 2>&1 &
uv run python ../tools/agent/seed_org.py --db-path db/agent.db
uv run python ../tools/agent/setup_stub_llm.py --stub-url http://127.0.0.1:9311/v1
uv run python ../tools/agent/mcp_beside_sql_loop.py --db db/agent.db
```

DevBox: skip the boot, migrate, seed and key lines, and `source
tools/agent/devbox_env.sh` first. Then pass `--db "$BOW_SQLITE_DB"`.

The driver creates a SQLite agent and an MCP agent through the API, sets the
MCP tool's policy to `allow` so the run is unattended, flags the connection
unhealthy, then asks the same question on a report with the MCP agent alone
and on one with both agents.

Before the fix:

```
PASS  MCP agent alone          tool offered=True  MCP server calls=1  planner saw result=True
FAIL  MCP agent + SQL agent    tool offered=False  MCP server calls=0  planner saw result=False
```

## The fix

`construct_clients` builds tool-provider connections (`mcp`, `custom_api`,
`remote_ssh`, `remote_smb`; `connection_tool_gateway.TOOL_CONNECTION_TYPES`)
whatever their health flag says. SQL and file connections keep the skip, so
generated code still never sees a known-dead query client. An MCP agent now
always has a client key, so the `_has_client` filter keeps it beside any other
agent, and its tools are registered. The same holds for an agent attached
mid-run (`_ensure_clients_for_attached` builds through the same function).

After the fix:

```
Loop A: 2 passed
Loop B:
PASS  MCP agent alone          tool offered=True  MCP server calls=1  planner saw result=True
PASS  MCP agent + SQL agent    tool offered=True  MCP server calls=1  planner saw result=True
```

## What this proves / regression notes

- The outcome no longer depends on which other agents share the report: the
  test covers alone and beside a SQL agent, and the existing file-agent cases
  (OneDrive / SharePoint / Google Drive) still pass.
- Unchanged by design (`tests/e2e/rbac/test_agent_mcp_tools_reach.py`): an
  agent whose MCP client **fails to build** (unreachable server, missing
  sign-in) stays out of the run, and an agent the user cannot access is still
  refused (403 in `construct_clients`, before the connection filter).
- Unchanged: an inactive SQL connection is still skipped
  (`tests/unit/test_report_context_datasource_dropped.py`).
- Sweep: 73 e2e files touching MCP, run agents and connection health:
  732 passed, 2 failed, 114 errors. The 2 failures
  (`test_sharepoint_2010_index_search.py`, proxy CA bundle) and the 114 errors
  (dashboard corpus fixture, `no such column: starting_folder_ids`) reproduce
  identically with the fix stashed.
- Unit tests need `libodbc.so.2` (`apt-get install unixodbc`) in a fresh
  sandbox, or 28 fail on import, with or without the fix.
