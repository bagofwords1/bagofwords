# Plan + Sandbox Feedback Loop — MCP agent-tool gateway

Adds the ability for an **external MCP client** (Claude, Cursor, …) to discover
and trigger an **agent's** (fka data source's) connected **MCP servers** and
**custom APIs** *through* BOW — i.e. BOW's MCP server becomes a gateway in front
of each agent's tools/connectors/custom-API, in addition to the existing
`create_data` / `inspect_data` surface.

This doc is both the implementation plan and the runnable manual-verification
loop. **Status: implemented and verified** — Loop A below was run in the sandbox
and passes; observed JSON-RPC transcript is inline.

---

## Background — two MCP planes

BOW has two separate tool planes (this was the source of the original confusion,
because `execute_mcp` already existed on plane #1):

1. **Internal planner tools** (`app/ai/tools/`) — what BOW's own LLM agent calls.
   Already had the full gateway pattern: `search_mcps` (discover) + `execute_mcp`
   (invoke) over `Connection`s of type `mcp` / `custom_api`, resolving to
   `ConnectionTool` rows, honoring `is_enabled` / `policy`.
2. **External MCP server** (`app/ai/tools/mcp/`, registered in `MCP_TOOLS`, served
   by `routes/mcp.py` as JSON-RPC) — what Claude/Cursor connect to. Exposed
   `create_data`, `inspect_data`, `get_context`, … but **nothing** to reach an
   agent's MCP/custom-API tools.

The gap was on plane #2. The work was to **port the plane-#1 pattern to plane #2**
and share the resolution/policy/execution logic via a service.

---

## Design (as built)

### 1. Shared service — `app/services/connection_tool_gateway.py`

`ConnectionToolGateway` centralizes the three things that previously lived inline
in the internal `execute_mcp`:

| Method | Responsibility |
|---|---|
| `list_tools(db, org, *, data_source_ids, include_disabled=False)` | Resolve effective tools across agents. Effective `is_enabled`/`policy` come from the **per-agent `DataSourceConnectionTool` overlay** when present, else the `ConnectionTool` default. |
| `execute(db, org, *, data_source_id, tool_name, arguments, connection_id=None, current_user=None, allow_confirm=False)` | Enforce enablement + policy, construct the provider client via `ConnectionService.construct_client`, call `acall_tool`, normalize the result. |

Policy over the gateway: only `allow` tools run; `confirm`/`deny` are blocked
(there is no interactive confirm step for an external client). `confirm` can be
opted into via `allow_confirm=True`.

### 2. External MCP tools (plane #2), registered in `MCP_TOOLS`

* **`list_agent_tools`** (`app/ai/tools/mcp/list_agent_tools.py`) — external twin
  of `search_mcps`. Scope by `report_id` (all agents on a report) or
  `data_source_ids`. Returns each tool's **full `input_schema`**. Honors per-user
  agent visibility.
* **`execute_mcp`** (`app/ai/tools/mcp/execute_mcp.py`) — external twin of the
  internal `execute_mcp`. Args: `data_source_id`, `tool_name`, `arguments`,
  optional `connection_id`. Checks the `enable_mcp_tools` org kill-switch +
  per-user agent access, delegates to the gateway, returns the result inline
  (tabular rows / text capped to a preview; full payload otherwise).

### 3. `get_context` now advertises tools

`get_context` previously returned only tables + resources. It now attaches each
agent's MCP/custom-API tools (**name + description only**, mirroring the internal
`<mcp_tools>` block — full schemas come from `list_agent_tools`). Implemented by
reusing `ConnectionToolGateway.list_tools`, so external and internal context stay
consistent and both respect `is_enabled` + the overlay.

Schema additions in `app/schemas/mcp.py`: `ToolInfo` (+ `DataSourceInfo.tools`),
`MCPListAgentToolsInput/Output` + `AgentToolDetail`, `MCPExecuteToolInput/Output`.

### Decisions taken

* **Scope by `data_source_id` (agent), not report.** Matches "an agent now has
  tools/connectors/custom api" and the per-agent overlay. `report_id` is still
  accepted by `list_agent_tools` as a convenience scope.
* **Allow-only policy over MCP.** `confirm`/`deny`/disabled are blocked with a
  clear error.
* **No internal refactor.** Plane-#1 `execute_mcp` left untouched to keep blast
  radius small; the new gateway service is the shared path for plane #2 (internal
  can adopt it later).

---

## Environment setup (fresh sandbox)

```bash
cd backend
python3.12 -m venv /tmp/venv312
VIRTUAL_ENV=/tmp/venv312 uv sync --frozen --extra dev   # populates backend/.venv
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

Tests run on SQLite; the autouse `run_migrations` fixture builds the schema per
test (`tests/conftest.py`).

---

## Loop A — external MCP client drives the gateway end-to-end

`tests/e2e/test_mcp_agent_tools.py` acts as an external MCP client over the real
`/api/mcp` JSON-RPC endpoint: it stands up an agent backed by a mocked MCP
provider (`MockToolProviderClient`, no network), discovers its tools, and invokes
them — plus policy/enablement negative cases.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest tests/e2e/test_mcp_agent_tools.py -q -s -p no:warnings
```

**Observed (PASS, 4 tests):** JSON-RPC transcript, verbatim —

```
# create_report → report + the mock agent attached
← 200 {"result":{"content":[{"text":"{\"report_id\": \"b0fb3f89…\", \"data_sources\": [{\"id\": \"22606035…\", \"name\": \"Mock Agent\", \"type\": \"mcp\"}]}"}],"isError":false}}

# get_context now advertises the agent's tools (names only)
[get_context] advertised tools: ['echo', 'failing_tool', 'get_records', 'search_docs']

# list_agent_tools returns full input schemas
[list_agent_tools] get_records schema:
  {"type":"object","properties":{"count":{"type":"integer","description":"Number of records to return","default":5}}}

# execute_mcp — tabular tool returns rows through BOW
[execute_mcp get_records] -> {"success": true, "content_type": "tabular", "connection_name": "Mock MCP",
  "row_count": 3, "result": [{"id":1,"name":"Record 1","status":"inactive"},
  {"id":2,"name":"Record 2",…}, {"id":3,…}], "truncated": false}

# execute_mcp — echo (json)
[execute_mcp echo] -> {"success": true, "content_type": "json", "connection_name": "Mock MCP",
  "result": {"echoed": "hello gateway"}}

4 passed
```

Negative cases asserted in the same file:
* `test_gateway_unknown_tool_returns_schema_help` — unknown tool → `success:false`, "not found".
* `test_gateway_respects_disabled_tool` — disabling a `ConnectionTool` hides it from
  `list_agent_tools` and blocks `execute_mcp` ("disabled").
* `test_gateway_blocks_non_allow_policy` — `policy="deny"` → blocked ("policy").

---

## Loop B — no regressions in the existing MCP suite

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest \
  tests/e2e/test_mcp.py tests/e2e/test_mcp_tools.py tests/e2e/test_mcp_agent_tools.py \
  tests/e2e/test_oauth_mcp.py tests/e2e/rbac/test_mcp_analysis.py \
  tests/unit/test_mcp_resource_helpers.py tests/unit/test_search_mcps_query.py \
  -q -p no:warnings
```

Only change needed in existing tests: the two `tools/list` count assertions in
`test_mcp.py` (`11 → 13` JSON-RPC, `12 → 14` REST) since two tools were added;
the new names are now asserted explicitly.

---

## Files

| File | Change |
|---|---|
| `app/services/connection_tool_gateway.py` | **new** — shared resolve/policy/execute service |
| `app/ai/tools/mcp/list_agent_tools.py` | **new** — discovery MCP tool |
| `app/ai/tools/mcp/execute_mcp.py` | **new** — invocation MCP tool |
| `app/ai/tools/mcp/__init__.py` | register `list_agent_tools`, `execute_mcp` |
| `app/ai/tools/mcp/get_context.py` | advertise each agent's tools |
| `app/schemas/mcp.py` | `ToolInfo`, `DataSourceInfo.tools`, list/execute IO schemas |
| `tests/e2e/test_mcp_agent_tools.py` | **new** — end-to-end gateway proof |
| `tests/e2e/test_mcp.py` | bump tool-count assertions + assert new names |

---

## Deferred (intentionally out of scope)

* **Interactive `confirm` over MCP** — would need a structured "needs
  confirmation" round-trip the host re-calls with a token. Currently `confirm` is
  treated like `deny` at the gateway (`allow_confirm=False`).
* **Internal `execute_mcp` adopting `ConnectionToolGateway`** — left as a DRY
  follow-up to avoid touching the working planner path in this change.
* **Tracking/audit parity** — the internal tool materializes tabular results to
  CSV File records linked to the report and writes tool-audit rows; the external
  gateway returns inline and relies on the route's existing handling. Wiring the
  external path into `_finish_tracking` is a possible enhancement.
