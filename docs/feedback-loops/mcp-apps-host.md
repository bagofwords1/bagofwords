# Feedback Loop — MCP Apps in report chat + MCP protocol 2026-07-28

Validates two changes end to end:

1. **MCP Apps host.** BOW renders the interactive UI an MCP server attaches
   to a tool (extension `io.modelcontextprotocol/ui`, ext-apps spec
   2026-01-26) inside `/reports/[id]`. That covers the sandbox, the JSON-RPC
   bridge, tool calls from the app, model context, display modes, theming
   and RTL.
2. **MCP 2026-07-28 client.** The Python SDK moved from `mcp` 1.30 to 2.3.
   The client negotiates the protocol era: 2026-07-28 is stateless, older
   servers still get the `initialize` handshake. It advertises the Apps
   extension on both. OAuth follows the new rules: `iss` validated,
   credentials bound to their issuer, DCR `application_type`.

## What changed

**Backend**

- `app/data_sources/clients/mcp_client.py`
  - Connects through `mcp.Client` (`mode="auto"` for streamable HTTP;
    `legacy` for HTTP+SSE).
  - Advertises `io.modelcontextprotocol/ui` with `text/html;profile=mcp-app`.
  - Keeps `_meta.ui` from discovery in `metadata.ui`.
  - Returns each tool result verbatim as `raw_result`, capped at 512 KB.
  - Reads SDK 1.x/2.x attribute names alike.
  - Recognizes `structuredContent: {"result": …}` as a duplicate of the
    text, not a second copy.
- `app/core/egress.py`: the pinned, SSRF-safe transport is re-based on
  `httpx2`, the HTTP library of the 2.x SDK (`mcp_http_client`). The httpx
  path is unchanged.
- `app/data_sources/clients/mcp_session_pool.py`: no `ping` on 2026-07-28+
  connections. The protocol removed it, and those connections have no
  session to go stale.
- `app/services/mcp_apps.py`
  - Tool UI binding and visibility rules.
  - Resource `_meta.ui` parsing, where the content item wins over the
    listing entry.
  - Domain validation and the CSP builder.
  - `ui://` document read and cache.
- `app/routes/mcp_apps.py`
  - `GET /api/mcp_apps/{te}/resource`: the app document plus its sandbox
    policy. Any viewer of the report may read it.
  - `POST /api/mcp_apps/{te}/rpc`: `tools/call` and `resources/read` from
    the app, to its own server only. Owner only.
  - `POST /api/mcp_apps/{te}/model_context`: `ui/update-model-context`,
    stored on the tool call's history entry. Owner only.
  - `GET /api/mcp_apps/sandbox`: the sandbox proxy page. Its CSP header is
    built from the app's declared domains, and it may only be framed by BOW.
- `execute_mcp.py`
  - Refuses app-only tools.
  - Attaches `result_json.mcp_app` (resource URI, tool input, raw result),
    which is UI only and never goes to the observation.
- Catalog filters keep app-only tools out of every model-facing list:
  `mcp_tool_registry.py`, `schema_context_builder.py`, `get_connection.py`.
- `persisted_summary.py`: the fallback history projection drops `mcp_app`.
- Org switch `enable_mcp_apps`, on by default.
- Config `mcp_apps.sandbox_origin` (env `BOW_MCP_APPS_SANDBOX_ORIGIN`).
- `mcp_dcr_service.py` and `connection_oauth.py`:
  - DCR sends `application_type: "web"`.
  - The issuer is recorded at registration.
  - An issuer change triggers re-registration and drops the old tokens.
  - The callback rejects an `iss` that doesn't match the recorded issuer.

**Frontend**

- `components/tools/McpAppFrame.vue`: the host.
  - Sandbox proxy iframe, plus the JSON-RPC bridge: `ui/initialize`,
    `tool-input` / `tool-result` / `tool-cancelled`, `size-changed`,
    `host-context-changed`, `resource-teardown`.
  - Proxied `tools/call` and `resources/read`, with BOW's own confirmation
    card for write-capable tools.
  - `ui/open-link` with confirmation, `ui/message`,
    `ui/update-model-context`, and `ui/request-display-mode`.
  - Full screen uses a `<dialog>` with `showModal()`, so it sits in the top
    layer without moving the node, and the app keeps its state.
- `utils/mcpAppHost.ts`: host context (BOW tokens as the spec's CSS
  variables, theme, locale, time zone), capabilities, URL and sandbox
  helpers.
- `components/tools/MCPTool.vue`: renders the app under the tool's status
  line. It only does so where the report page provides `mcpAppHost`; the
  public `/c/` page keeps the text result.
- `pages/reports/[id]/index.vue`: provides `mcpAppHost.sendMessage` for
  `ui/message`, the same path as a follow-up chip.
- `locales/{en,es,he}.json`: `tools.mcp.app.*` strings and the
  `enable_mcp_apps` setting label.

## Sandbox model

| Mode | When | App origin | Notes |
|---|---|---|---|
| Opaque (default) | `sandbox_origin` unset | `null` | Outer frame `sandbox="allow-scripts allow-forms"`. The app cannot touch BOW's storage, DOM or API, and cannot use `localStorage`. |
| Separate origin (spec's preferred setup) | `BOW_MCP_APPS_SANDBOX_ORIGIN=https://bow-sandbox.example.com` (same backend, another hostname) | that origin | `allow-same-origin` is safe here; the app gets working storage. |

The proxy page refuses to run when it is top-level, or when it is framed
without isolation at BOW's own origin.

## Loop A: deterministic tests (no LLM)

```bash
cd backend
export BOW_DATABASE_URL='sqlite:///db/test_scratch.db' TESTING=true
uv run pytest tests/unit/test_mcp_apps.py tests/unit/test_mcp_client_apps.py \
  tests/unit/test_mcp_client_protocol.py tests/unit/test_mcp_session_pool.py tests/unit/test_mcp_dcr.py
uv run pytest tests/e2e/rbac/test_mcp_apps_access.py -m e2e --db=sqlite
```

- **`test_mcp_client_apps.py`** runs the real mock server twice. Once as
  itself, a 2026-07-28 server; once behind a shim that answers
  `server/discover` like a pre-2026 server, which forces the `initialize`
  fallback. In both eras it checks:
  - the negotiated version;
  - the advertised extension;
  - the kept UI metadata;
  - the raw result;
  - the app document.
- **`test_mcp_apps_access.py`** covers the HTTP contract: viewer vs. owner,
  outsiders, visibility, unknown tools, confirmation, policy deny,
  model-context replacement and bounds, the org switch, the sandbox CSP and
  `frame-ancestors`, and app-only tools absent from the agent's catalog
  (native and deferred).
- **Mutation-checked.** Removing the extension advertisement fails 4 client
  tests; removing the catalog visibility filter fails 2 e2e tests.

## Loop B: live stack, real LLM (Haiku), real browser

Mock server: `backend/tests/mocks/mcp_apps_server.py`. It uses the SDK's
own `Apps` extension and offers four tools:
- `show_sales_dashboard`: UI, visible to model and app.
- `refresh_sales`: app-only, read-only.
- `create_followup_task`: app-only, write.
- `client_handshake`: reports what the client negotiated.

Its UI is `mcp_apps_sales_app.html`.

```bash
cd backend && uv run python tests/mocks/mcp_apps_server.py --port 3340 &
# stack: see .agents/skills/sandbox-feedback-loop (backend :8000, frontend :3000)
cd tools/agent/mcp_apps
node 01_signup.js
BOW_TEST_LLM_KEY=… node 02_llm.js    # Anthropic provider, Haiku only
node 03_mcp.js                       # connection + tool discovery + agent
DSF=2 node 04_chat.js                # "Show me the regional sales dashboard for Q3."
node 06_interact.js                  # every host feature, screenshots per step
node 09_security.js                  # isolation probes
node 10_fullscreen.js
node 12_hebrew.js
```

Observed:

| Check | Result |
|---|---|
| Client ↔ mock server | `protocol_version: 2026-07-28`, `client_supports_apps: true` |
| App renders from the tool result after a live Haiku run | yes; also after a reload |
| App-only `refresh_sales` (Q1) through `/rpc` | rendered; audit row `mcp_app.tool_called confirmed:false` |
| Write tool `create_followup_task` | BOW confirmation card first; audit row `confirmed:true` |
| `ui/update-model-context` (bar click) | stored on the tool call; the next Haiku turn quoted it ("APAC selection in the sales app ($251,400 revenue, 822 orders)") |
| `ui/message` | posted as a user turn; Haiku answered |
| `ui/open-link` | confirmation with the URL; cancel returns a denial to the app |
| Full screen | above the whole shell; Escape and the button exit; app state kept |
| Dark mode / Hebrew | BOW tokens applied live; `locale: he` gives the app `dir=rtl`; host card in Hebrew |
| Opaque sandbox | app origin `null`; BOW localStorage, DOM and API blocked; internet blocked (no declared domains) |
| Separate-origin sandbox | app origin `http://127.0.0.1:8000`, `localStorage` works, no BOW cookies |
| Proxy framed unsandboxed / opened top-level | refuses to run |
| MCP Apps switched off | no frame, no error box; text result as before |

Evidence: `media/pr/mcp-apps/`.

## Known, not caused here

`tests/e2e/test_mcp.py::test_mcp_tools_list` and
`::test_mcp_rest_tools_endpoint` count BOW's *own* MCP server tools (27 vs.
the expected 20). They fail identically on an untouched `HEAD`.
