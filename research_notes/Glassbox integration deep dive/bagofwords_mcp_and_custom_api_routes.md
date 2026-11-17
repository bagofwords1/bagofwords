# Bag of Words: MCP-preset and Custom-API routes for a Glassbox integration

All sources are repo-relative paths in `/home/user/bagofwords` with line numbers (`path:line`), read from the working tree on 2026-11-17. Nothing was run (no app, no tests) and no repo file other than this one was changed. This extends `research_notes/Glassbox analytics integration research/bagofwords_connector_architecture.md` (native-connector contract) and `reports/Glassbox analytics integration research.md`; facts already established there are referenced, not repeated.

Terms used below: a **preset** is a named catalog tile that pre-fills the generic `mcp` (or `custom_api`) connection form; a **tool provider** is a connection type with `is_connection=False` whose catalog is a list of callable tools, not tables.

## 1. MCP route: where presets live, every existing preset, and whether a preset can take a per-tenant URL

### Takeaway
A preset is one `McpPreset(...)` literal in `MCP_PRESETS`; it carries branding and form defaults only and always resolves to `type="mcp"`. There are 11 presets, all with a fixed vendor URL and streamable HTTP, and **none is an analytics vendor** (no PostHog, Amplitude or Mixpanel preset exists). The model has no per-tenant URL template: the user can overwrite the URL under "Advanced", but preset recognition is by exact `server_url` match, so a tenant-specific Glassbox URL would lose the brand icon and auth gating after save unless the form is changed to persist a `catalog_key`.

### Cited Findings
**Schema classes and registry**
- `McpPreset` fields: `key`, `title`, `server_url` (a single required string), `transport` (default `streamable_http`; comment lists `streamable_http | sse`), `auth` (default `oauth`; comment lists `oauth(DCR) | oauth_app | bearer | basic`), `description`, `category` (default `services`), `allowed_auth` (form vocabulary `none | bearer | api_key | basic | dcr | oauth_app`; `None` offers all), `oauth_defaults`, `sample_tools` — `backend/app/schemas/data_source_registry.py:489-521`
- Docstring: "All presets resolve to `type="mcp"` — they are named *instances* of the mcp type, not types of their own (the MCP runtime, DCR, and OAuth all gate on `connection.type == "mcp"`)" — `backend/app/schemas/data_source_registry.py:490-498`
- `McpAuthDefaults` (pre-fill for `oauth_app`): `authorize_url`, `token_url`, `scopes`, `audience`, `token_endpoint_auth_method` (`client_secret_post | client_secret_basic | none`) — `backend/app/schemas/data_source_registry.py:464-486`
- The generic `mcp` registry entry: `category="custom"`, title "MCP Server", six auth variants, `client_path=...mcp_client.McpClient`, `version="beta"`, `is_connection=False`, `data_shape="tools"`, `catalog_ownership="none"`, `ui_form="mcp"` — `backend/app/schemas/data_source_registry.py:2777-2824`
- Auth variants on `mcp` and their credential scopes: `none` (system), `bearer` (system, user), `api_key` (system, user; reuses `CustomAPIKeyCredentials`), `basic` "Email + API token (Basic)" (system, user), `oauth_app` "OAuth Client (admin-configured)" (system, user), `oauth` "Sign in (per-user OAuth)" (user only) — `backend/app/schemas/data_source_registry.py:2786-2815`
- `MCPConfig`: `server_url` (free string), `transport` (default `sse`, options `sse`, `streamable_http`), `headers` (static key/values on every request), `header_injection` (per-user identity as headers), `metadata_injection` (per-user identity into a metadata argument) — `backend/app/schemas/data_sources/configs.py:4606-4649`
- Credential classes: `MCPBearerCredentials.token` (`:4689-4695`), `MCPBasicCredentials.username/password` (`:4698-4711`), `MCPOAuthAppCredentials` with `authorize_url`, `token_url`, `client_id`, `client_secret`, `scopes`, `audience`, `token_endpoint_auth_method` (`:4714-4773`), `CustomAPIKeyCredentials.api_key/api_key_header` default `X-API-Key` (`:4847-4859`), `OAuthDelegatedCredentials` empty (`:2761-2763`) — `backend/app/schemas/data_sources/configs.py`
- `MCP_PRESETS` list — `backend/app/schemas/data_source_registry.py:3017-3131`; helpers `mcp_presets()` / `mcp_preset(key)` — `:3234-3241`
- Catalog endpoint `GET /connectors/catalog` returns `mcp_presets()` unchanged — `backend/app/routes/data_source.py:43-52`

**Transports**
- `McpClient` supports only `sse` and `streamable_http`; any other value raises "Unsupported MCP transport"; class docstring says "stdio planned for later" — `backend/app/data_sources/clients/mcp_client.py:254-259`, `:812-831`
- The constructor accepts `command`, `args`, `env` but `_connect` never uses them — `backend/app/data_sources/clients/mcp_client.py:265-267`, `:285-287`, `:790-833`
- Streamable HTTP uses `mode="auto"` (stateless for 2026-07-28+ servers, `initialize` handshake for older); SSE uses `mode="legacy"` — `backend/app/data_sources/clients/mcp_client.py:795-825`
- The form offers exactly two transport options — `frontend/components/MCPConnectionForm.vue:48-51`, `:168-171`

**Auth header construction**
- Bearer wins over Basic; API key is added as its own header; static `headers` are merged last (so they can override) — `backend/app/data_sources/clients/mcp_client.py:295-307`

**Every existing preset** (all leave `transport` unset, so all are `streamable_http` per the default at `:502`)

| key | vendor | server_url (fixed) | default `auth` | `allowed_auth` | category | lines |
|---|---|---|---|---|---|---|
| `monday` | monday.com | `https://mcp.monday.com/mcp` | oauth (DCR) | dcr | services | `:3018-3020` |
| `notion` | Notion | `https://mcp.notion.com/mcp` | oauth (DCR) | dcr | services | `:3021-3023` |
| `atlassian` | Jira / Atlassian | `https://mcp.atlassian.com/v1/mcp/authv2` | oauth (DCR) | dcr | services | `:2955`, `:3029-3031` |
| `bitbucket` | Bitbucket (Atlassian) | `https://mcp.atlassian.com/v2/mcp` | basic | basic, bearer | dev | `:2956`, `:3038-3044` |
| `gitlab` | GitLab | `https://gitlab.com/api/v4/mcp` | oauth (DCR) | dcr, bearer | dev | `:3050-3055` |
| `linear` | Linear | `https://mcp.linear.app/mcp` | oauth (DCR) | dcr | services | `:3056-3058` |
| `sentry` | Sentry | `https://mcp.sentry.dev/mcp` | oauth (DCR) | dcr | infra | `:3059-3061` |
| `github` | GitHub | `https://api.githubcopilot.com/mcp/` | oauth_app | oauth_app | services | `:3062-3069` |
| `hubspot` | HubSpot | `https://mcp.hubspot.com` | oauth_app | oauth_app | services | `:3086-3095` |
| `google_drive` | Google Drive (MCP Preview) | `https://drivemcp.googleapis.com/mcp/v1` | oauth_app (with RFC 8707 `audience`) | oauth_app | files | `:3099-3106` |
| `x` | X | `https://api.x.com/mcp` | bearer | bearer, oauth_app | services | `:3115-3130` |

(All line numbers in the table are in `backend/app/schemas/data_source_registry.py`.)

- A Gmail MCP preset was removed when the native `gmail_mail` connector shipped; existing MCP-based Gmail connections keep working — `backend/app/schemas/data_source_registry.py:3107-3110`
- No preset uses SSE; Atlassian's old `/v1/sse` endpoint was retired and connections migrated — `backend/app/schemas/data_source_registry.py:3024-3028`, `:2957-2958`
- No preset uses `api_key` or static custom headers as its default; those are reachable only through the generic "MCP Server" tile or by offering `api_key` in `allowed_auth` — `backend/app/schemas/data_source_registry.py:510-513`, `:2796-2800`

**Per-tenant / self-hosted URL handling**
- The only self-hosted precedent is a code comment on the GitLab preset: "Self-managed: https://<host>/api/v4/mcp (18.6+)"; the preset itself still hard-codes `gitlab.com` — `backend/app/schemas/data_source_registry.py:3045-3050`
- For a preset tile the Server URL and Transport inputs are hidden from the main form and shown, still editable, under "Advanced" — `frontend/components/MCPConnectionForm.vue:40-51`, `:160-171`; the hint string reads "Known for this connector — change only for a proxy or self-hosted endpoint." — `locales/en.json:4803`
- The prefill copies `server_url`, `transport`, `auth_type` and OAuth defaults into editable form fields — `frontend/components/MCPConnectionForm.vue:515-530`; `frontend/components/AddConnectionModal.vue:573-600`
- In edit mode the form finds its preset by **exact** `server_url` equality against the catalog — `frontend/components/MCPConnectionForm.vue:532-546`
- The saved config is `{server_url, transport, auth_type, ...forwarding}`; no `catalog_key` is written — `frontend/components/MCPConnectionForm.vue:649`, `:690`
- The backend resolves a connection's brand key from `config.catalog_key` if present, else by exact `server_url` match against the presets — `backend/app/services/data_source_service.py:36-58`; `backend/app/schemas/agent_icon.py:42-59`
- The frontend also reads `c.config?.catalog_key` as a fallback for the icon — `frontend/composables/useToolConnectionIcon.ts:97`
- The HubSpot loop documents the fragility: "Since `MCPConnectionForm` matches a preset by *exact* `server_url`, adding `/mcp` breaks preset recognition in edit mode as well as the connection itself" — `docs/feedback-loops/hubspot-mcp-preset.md` ("Three findings", finding 2)

**Analytics-vendor presets and what adding a preset involved**
- Registry search for `posthog|amplitude|mixpanel|heap|fullstory|glassbox` near `preset|mcp` returned nothing; PostHog exists only as a native connector — `backend/app/schemas/data_source_registry.py:1701` (earlier notes, section 3)
- GitLab + Bitbucket presets (the only preset addition whose commits are individually visible in this clone's history): commit `1cf313e2` (2026-10-31) changed `backend/app/data_sources/clients/mcp_client.py` (+9), `backend/app/schemas/data_source_registry.py` (+58/-?), `backend/app/schemas/data_sources/configs.py` (+17), `frontend/components/AddConnectionModal.vue` (4), `frontend/components/DataSourceIcon.vue` (+2), `frontend/components/MCPConnectionForm.vue` (+19); commit `b4e92be1` (2026-10-31) added `backend/alembic/versions/atlv201_atlassian_mcp_v2_endpoint.py` (+63), `backend/app/services/mcp_dcr_service.py` (+10), `backend/tests/unit/test_mcp_code_host_presets.py` (+162), `frontend/public/data_sources_icons/{bitbucket,gitlab}.svg`, and `locales/{en,es,he}.json` (8 lines each) — `git show --stat 1cf313e2`, `git show --stat b4e92be1`
- Most of that diff was for a **new auth mode** (HTTP Basic) and the Atlassian endpoint move, not for the presets themselves; the locale keys added were generic Basic-auth form labels (`authBasic`, `basicUsernameLabel`, `basicHint`, ...), not per-preset strings — `git show b4e92be1 -- locales/en.json`
- HubSpot preset: the feedback loop lists exactly three touched pieces: the `MCP_PRESETS` entry, the icon file plus one line in `CONNECTOR_ICON_FILE`, and a `test_hubspot_*` block in `backend/tests/unit/test_mcp_presets.py`; "No new backend plumbing" — `docs/feedback-loops/hubspot-mcp-preset.md` ("What was added (validated)")
- HubSpot changelog line — `CHANGELOG.md:577`; GitLab/Bitbucket changelog line — `CHANGELOG.md:228`
- The skill's own statement of scope: a preset "carries only branding + a form spec — no client, no config/credentials schema, no per-type tests, no new dispatch sites" — `.agents/skills/add-connection-type/SKILL.md:101-108`

### Inferences
- A Glassbox preset fits the model cleanly only if Glassbox serves MCP from **one vendor-wide URL**. The earlier report established that Glassbox consoles are per tenant (`console.<tenant>.glassboxdigital.io`) and sometimes customer-hosted; if the MCP endpoint follows that pattern, `McpPreset.server_url` cannot express it.
- Three workable options for a per-tenant URL, in rising cost: (a) ship no preset and document "use the generic MCP Server tile with your Glassbox URL" (zero code); (b) ship a preset with a placeholder URL the admin must overwrite under Advanced, accepting that the saved connection will render as a generic MCP connection because neither exact-URL match nor `catalog_key` will identify it; (c) make the form write `config.catalog_key` on create, which the backend already honours (`data_source_service.py:54-55`) and which would also fix edit-mode recognition if `matchedPreset` were taught to use it. Option (c) is a small frontend change, not present today.
- Because the local commit history does not isolate the HubSpot commit (pickaxe on `McpPreset(key="hubspot"` returns only large merge commits `1d7e5686`, `c0b0d921`), the HubSpot file list above rests on the feedback-loop document, not on a diff.

### Gaps
- Whether Glassbox's MCP server is single-URL or per-tenant, and its transport, are unknown (not in this repo).
- I did not read `frontend/components/AddMCPModal.vue` or `McpModal.vue`; whether another entry point writes `catalog_key` is unverified beyond the grep that found no writer in `frontend/components`, `frontend/pages`, `frontend/composables`, `backend/app/routes` or `connection_service.py`.
- The `category` values `dev` used by GitLab/Bitbucket were not cross-checked against the category list in `DataSourceRegistryEntry` (earlier notes list `databases|bi|infra|services|files|custom`).

## 2. How MCP tools are discovered, stored, enabled, permissioned, exposed and called; how results reach reports; limits

### Takeaway
Tools are discovered with `tools/list`, stored as `connection_tools` rows with an enable flag and a policy, and offered to the model as native `mcp__<connection>__<tool>` tools (first 50 by default, the rest via `search_tools`/`call_tool`). Every successful call writes the full result to a session file and returns a `file_id`; `create_data(source_file_ids=[...])` turns it into a dataframe that can be charted and, by setting `tables_by_source` in the same call, mixed with warehouse tables. Generated Python can never call the MCP connection itself.

### Cited Findings
**Discovery and storage**
- `McpClient._alist_tools` follows `tools/list` cursor pagination up to 50 pages and records name, description, input/output schema, annotation hints and a starting policy per tool — `backend/app/data_sources/clients/mcp_client.py:67-68`, `:317-358`
- Starting policy: a tool the server marks `readOnlyHint=true` (and not destructive) starts on "allow"; anything else, including a tool with no hints, starts on "ask" — `backend/app/data_sources/clients/mcp_client.py:91-101`
- `ConnectionService.refresh_tools` upserts `ConnectionTool` rows (new rows `is_enabled=True`, policy = explicit, else default, else `allow`) and deletes rows the server no longer lists — `backend/app/services/connection_service.py:3279-3330`, `:3388-3393`, `:3413-3418`
- For per-user OAuth connections, discovery is skipped until a user signs in (no system token exists) — `backend/app/services/connection_service.py:3295-3309`; `backend/app/services/connection_identity.py:70-84`; it then runs in the OAuth callback — `backend/app/routes/connection_oauth.py:404-415`
- Tables: `connection_tools` (name, connection_id, description, input_schema, output_schema, `is_enabled`, `policy`, `metadata_json`) — `backend/app/models/connection_tool.py:12-30`; per-agent overlay `data_source_connection_tool` (`is_enabled`, `policy`) — `backend/app/models/data_source_connection_tool.py:16-30`; per-user accessibility `user_connection_tools` — `backend/app/models/user_connection_tool.py:13-28`; per-user policy preference `user_connection_tool_preferences` (`allow | ask | deny | auto`) — `backend/app/models/user_connection_tool_preference.py:20-33`
- Admin/agent endpoints for enabling, disabling and setting policy, single and batch (max 500 per batch), plus per-user `my_policy` — `backend/app/routes/data_source_tools.py:111`, `:145`, `:272`, `:348`, `:389`, `:449`, `:530`, `:610`, `:652`

**Permissioning**
- Policies are `allow | ask | deny | auto`; the strictest admin setting across the run's agents applies — `backend/app/services/tool_policy_service.py:31-36`, `:62-65`
- Organization-level gate `enable_mcp_tools` ("MCP & Custom API Tools", default on) blocks all calls when off — `backend/app/schemas/organization_settings_schema.py:381`; `backend/app/ai/tools/implementations/execute_mcp.py:568-586`
- A call is only allowed against a connection attached to one of the run's agents, within the caller's organization, of type `mcp | custom_api | remote_ssh | remote_smb` — `backend/app/ai/tools/implementations/execute_mcp.py:619-667`
- Disabled tools are refused; then the effective policy is enforced ("deny blocks, ask pauses the run for user approval, auto delegates the decision to a small-model judge") — `backend/app/ai/tools/implementations/execute_mcp.py:766`, `:776-781`; approval wait is 240 s — `:1517`
- Per-user identity can be forwarded to the server as headers or injected metadata; locked fields are hidden from the model — `backend/app/ai/tools/implementations/execute_mcp.py:688-720`, `:738-747`
- Unicode tag characters are stripped from tool descriptions and results as a prompt-injection defence — `backend/app/data_sources/clients/mcp_client.py:39-44`, `:343`, `:539`

**Exposure to the agent**
- Each allowed tool becomes one planner tool named `mcp__<connection>__<tool>` carrying the server's JSON Schema; names are capped at 64 characters — `backend/app/ai/tools/mcp_tool_registry.py:1-20`, `:35-36`
- `tools_max` (org setting, default 50; `BOW_TOOLS_MAX` env overrides) caps native registration; beyond it the most-used tools in the last 30 days are native and the rest are reached with `search_tools` + `call_tool` — `backend/app/ai/tools/mcp_tool_registry.py:11-16`, `:38`, `:64-81`, `:381`, `:412`; `backend/app/schemas/organization_settings_schema.py:383`; `backend/app/ai/tools/implementations/search_tools.py:28-45`; `backend/app/ai/tools/implementations/call_tool.py:19-34`
- Disabled, denied and app-only tools are left out of the catalog entirely — `backend/app/ai/tools/mcp_tool_registry.py:354-362`
- All paths dispatch through `execute_mcp`: "a native or `call_tool` call is rewritten into `execute_mcp` arguments, so tool policy, per-user identity forwarding, CSV/JSON materialization and audit are unchanged" — `backend/app/ai/tools/mcp_tool_registry.py:18-20`
- MCP resources are also bridged (`resources/list`, `resources/templates/list`, `resources/read`), capped at 500 — `backend/app/data_sources/clients/mcp_client.py:368-380`

**Call path and result handling**
- Sequence inside `ExecuteMCPTool.run_stream`: feature gate → resolve connection in run scope → identity forwarding → tool enabled → policy → local argument validation against the stored schema → cooldown check → `construct_client` (with the run's user so per-user tokens resolve) → `acall_tool` — `backend/app/ai/tools/implementations/execute_mcp.py:562-880`
- Result extraction: one content block comes back as itself (parsed JSON or string), several text blocks are joined, mixed blocks become a list; `structuredContent` is merged; file blobs (embedded resources, images, audio) are returned separately as `binaries` — `backend/app/data_sources/clients/mcp_client.py:533-573`, `:689-735`
- Content type (`tabular | text | json`) is detected, including tables wrapped in an envelope such as `{"data": [...], "pages": {...}}` — `backend/app/data_sources/clients/mcp_client.py:782-788`; `backend/app/ai/tools/implementations/execute_mcp.py:928-943`
- Invariant: "a successful call leaves exactly ONE durable artifact plus a pointer to it, and that artifact is the payload AS RECEIVED"; `tabular_path` and `candidate_paths` tell the consumer where rows are — `backend/app/ai/tools/implementations/execute_mcp.py:945-956`
- Output contract: `file_id`, `file_name`, `media` (`csv | json | text | none`), `row_count`, `tabular_path`, `candidate_paths`, `record_shape`, `parse_skipped`, `materialization_error` — `backend/app/ai/tools/schemas/execute_mcp.py:55-88`
- Tool description tells the model: chart or aggregate with `create_data(source_file_ids=[file_id])`; CSV with `write_csv(source_file_ids=[file_id])`; read more with `read_file(file_id, offset)`; "Never try to call this connection from generated Python — generated code has no access to it" — `backend/app/ai/tools/implementations/execute_mcp.py:499-522`
- `create_data.source_file_ids`: "File IDs this code should read, e.g. the file_id returned by execute_mcp ... A clean tabular MCP result needs only this" — `backend/app/ai/tools/schemas/create_data.py:64-75`
- Mixing with warehouse data: `tables_by_source` description says set it "for any database query, or set source_file_ids instead when the data comes from files or tool results — **set both when mixing**" — `backend/app/ai/tools/schemas/create_data.py:51-62`
- `create_data` scopes the generated code's `excel_files` to exactly the named files and gives it a reader per extension (`pd.read_csv`, `pd.read_json`, ...) — `backend/app/ai/tools/implementations/create_data.py:1716-1758`; `backend/app/ai/tools/implementations/_source_files.py:1-30`, `:93-110`

**Limits**
- Parse cap: a result string over 5,000,000 characters is not parsed into objects; it is saved raw and flagged `parse_skipped` — `backend/app/data_sources/clients/mcp_client.py:12-22`, `:554-557`
- Inline budget: no fixed cap; one result may take at most half the transcript budget at an assumed 2 characters per token; fallback 50,000 characters when the window is unknown (`_MAX_INLINE_CHARS // 10`); at least 3 preview rows survive — `backend/app/ai/tools/implementations/execute_mcp.py:32-58`, `:61-77`, `:80-99`
- Truncation is explicit: `preview_row_count` of `row_count`, and "Never answer from a partial list" — `backend/app/ai/tools/implementations/execute_mcp.py:506-513`, `:971-973`
- Attached files: at most 10 per result; the rest are reported, not saved — `backend/app/ai/tools/implementations/execute_mcp.py:54-55`, `:1080`
- Result pagination is **not** automatic: envelope cursors/totals are kept as `result_metadata` and the model is told to "call this tool again with it" — `backend/app/ai/tools/implementations/execute_mcp.py:517-518`, `:979-983`
- HTTP timeouts for MCP: 30 s connect, 300 s read, redirects not followed ("a redirect could carry the auth headers to another host") — `backend/app/core/egress.py:435-452`, `:455-460`
- Tool-run timeouts: `execute_mcp` declares `timeout_seconds=60`, but the runner's hard limit is `max(hard_timeout_s=300, start+idle=190, declared)`; idle timeout 180 s — `backend/app/ai/tools/implementations/execute_mcp.py:551`; `backend/app/ai/runner/tool_runner.py:295-308`; `backend/app/ai/runner/policies.py:15-17`
- Retry: one retry on a stale pooled session for any tool; one retry on transport errors for read-only tools only; writes are never auto-retried — `backend/app/data_sources/clients/mcp_client.py:112-145`, `:499-531`
- Rate limits: an error text matching rate-limit patterns starts a per (org, user, connection) cooldown (parsed `retry after`, default 60 s, max 86,400 s); in-process only, "other workers still rely on the provider's limits" — `backend/app/services/mcp_cooldown.py:1-22`; `backend/app/ai/tools/implementations/execute_mcp.py:818-825`, `:883`, `:894`
- Session pool: default 32 sessions, 300 s idle, `BOW_MCP_POOL_SIZE=0` disables pooling — `backend/app/data_sources/clients/mcp_session_pool.py:34-37`; `backend/app/data_sources/clients/mcp_client.py:835-847`

### Inferences
- A Glassbox MCP tool that returns a JSON list of sessions, errors or journey metrics would be tabulated and chartable with no Glassbox-specific code, and could be merged in pandas with a warehouse query in one `create_data` call. The join happens in generated Python after both sides are fetched; nothing is pushed down to Glassbox.
- If Glassbox's server returns prose answers from Pulse (the earlier report's inference), the result would be saved as text and could be quoted or summarised but not charted.
- Because unannotated tools start on "ask", a Glassbox server that omits `readOnlyHint` would prompt the user to approve every call until an admin sets the tools to "allow".
- The 50-tool cap is per run across all MCP/custom-API connections on the run's agents, so a Glassbox server with a large tool list competes with other connected servers for native slots.

### Gaps
- Whether `execute_mcp` emits progress events during a long remote call (which decides whether the 180 s idle timeout can fire before the 300 s hard limit) was not traced in `tool_runner._stream_with_idle`.
- How `write_csv`, `read_file` and `inspect_data` treat MCP-materialized files was not read.
- Whether a result saved from an MCP call can back a **scheduled** report refresh (re-invoking the tool on each run) was not investigated; `docs/feedback-loops/2026-11-08-scheduled-report-tool-output-files.md` exists but was not opened.
- `backend/app/services/connection_tool_gateway.py` (external MCP clients calling through BOW) was outlined only.

## 3. OAuth for MCP and egress controls

### Takeaway
MCP OAuth is always per user: the connection stores only the OAuth client (admin-entered, or obtained once by Dynamic Client Registration) and each user's tokens live encrypted in `user_connection_credentials`, refreshed when within five minutes of expiry. The redirect URI is derived from the deployment's `bow_config.base_url`, so self-hosted Bag of Words works if that URL is reachable by the user's browser and registered with the provider. Egress is IP-policy based, not a host allow-list: private and loopback MCP hosts are allowed by default and refused only when `security.block_private_outbound` is on; cloud-metadata and link-local addresses are always refused.

### Cited Findings
**Credential model**
- Bearer, Basic and API-key MCP connections are saved `auth_policy="system_only"` (one shared credential); `oauth_app` and `dcr` are saved `auth_policy="user_required"` with `allowed_user_auth_modes=["oauth"]` — `frontend/components/MCPConnectionForm.vue:618-625`, `:695-706`
- The registry also allows `user` scope for `bearer`, `api_key` and `basic` — `backend/app/schemas/data_source_registry.py:2791-2805`
- Per-user credentials table: `connection_id`, `user_id`, `organization_id`, `auth_mode`, `encrypted_credentials`, `expires_at`, `is_active` — `backend/app/models/user_connection_credentials.py:15-34`
- Per-user auth on tool-shaped connections is not licence-gated (`_user_auth_needs_enterprise("mcp") is False`), unlike table-shaped warehouses — `backend/tests/unit/test_mcp_presets.py:34-41`

**Dynamic Client Registration (preset `auth="oauth"`)**
- Discovery chain: 401 `WWW-Authenticate` challenge → RFC 9728 protected-resource metadata → RFC 8414 authorization-server metadata → RFC 7591 registration as a public client with PKCE — `backend/app/services/mcp_dcr_service.py:1-26`
- The registered client and endpoints are persisted encrypted on the connection so registration runs once; scopes are re-derived from the server on every sign-in; an admin `scopes` override wins — `backend/app/services/mcp_dcr_service.py:20-26`, `:307-330`
- `ensure_mcp_oauth_config` only acts on `type == "mcp"` and is called from the authorize route — `backend/app/services/mcp_dcr_service.py:318-319`; `backend/app/routes/connection_oauth.py:199-200`
- Discovery "is no longer gated on a host allowlist"; a server without a `registration_endpoint` fails closed with an explicit message — `docs/feedback-loops/hubspot-mcp-preset.md` (finding 1)

**Pre-registered app (preset `auth="oauth_app"`)**
- `get_oauth_params` for `mcp` and `custom_api` requires `authorize_url`, `token_url`, `client_id`; `client_secret` is optional (public client); default client auth is `client_secret_post` when a secret exists, else `none` — `backend/app/services/connection_oauth_service.py:190-234`
- Authorization-code + PKCE; optional RFC 8707 `resource` (audience) sent on token and refresh requests when configured — `backend/app/services/connection_oauth_service.py:191-196`, `:621-622`; `backend/app/schemas/data_sources/configs.py:4757-4762`

**Flow, redirect URL, storage, refresh**
- Routes: `GET /api/connections/{connection_id}/oauth/authorize` and `GET /api/connections/oauth/callback` — `backend/app/routes/connection_oauth.py:171`, `:276`
- Redirect URI is `{settings.bow_config.base_url}/api/connections/oauth/callback` — `backend/app/routes/connection_oauth.py:224`, `:358`; DCR registers the same value, defaulting the base to `http://localhost:8000` when unset — `backend/app/services/mcp_dcr_service.py:290-292`
- State is signed with a 600-second lifetime; the PKCE verifier is held in a cookie — `backend/app/routes/connection_oauth.py:48-49`, `:103-115`
- Sign-in requires access to the connection (org admin, connection manager, or user with access) and a `user_required` policy with `oauth` allowed — `backend/app/routes/connection_oauth.py:127-159`
- After token exchange the tokens are saved, the connection is tested with them, and the save is rolled back if the test fails — `backend/app/routes/connection_oauth.py:369-400`
- Refresh: if the access token expires within 5 minutes and a refresh token exists, it is refreshed and re-encrypted; with no refresh token the stale credentials are returned and a warning logged — `backend/app/services/connection_oauth_service.py:1182-1233`
- Token-endpoint calls use the pinned egress client with a 30 s timeout — `backend/app/services/connection_oauth_service.py:625-637`

**Egress controls**
- Two policies: `PUBLIC` (every address must be globally routable; agent-chosen URLs) and `ADMIN` ("cloud metadata and link-local are always refused; private, LAN and loopback are refused only when `security.block_private_outbound: true`"), with ADMIN used for "MCP / custom API servers, OAuth discovery" — `backend/app/core/egress.py:15-20`, `:72-74`, `:167-177`
- `block_private_outbound` defaults to `False` — `backend/app/settings/bow_config.py:403`
- The check runs inside the transport on every request including redirect hops, with DNS pinning ("DNS rebinding closed") — `backend/app/core/egress.py:10-11`; `backend/app/data_sources/clients/mcp_client.py:806-808`
- `McpClient` builds both transports from `mcp_client_factory(Policy.ADMIN)` / `mcp_http_client(Policy.ADMIN)` — `backend/app/data_sources/clients/mcp_client.py:812-825`
- Save-time check: `construct_client` runs `acheck_connection_params`, which inspects `server_url`, `base_url`, `token_url` and other URL keys; unresolvable hosts pass — `backend/app/core/egress.py:718-727`, `:759-788`; `backend/app/services/connection_service.py:2814-2874` (earlier notes, section 2)
- The MCP config's own example URL is `http://localhost:3000/mcp` — `backend/app/schemas/data_sources/configs.py:4610`

### Inferences
- An on-premises or single-tenant Glassbox MCP server on a private address is reachable from a self-hosted Bag of Words by default; it is unreachable from any deployment that sets `block_private_outbound: true`, and there is no per-host exception list to carve it out. I found no allow-list mechanism in `egress.py`; the control is a single boolean.
- A hosted Bag of Words instance can reach only what its network can route to, so a bank-hosted Glassbox behind a firewall needs customer network work regardless of this code.
- If Glassbox's MCP authorization server supports DCR, the preset is `auth="oauth"` with zero admin setup; if it issues tokens through the customer's SSO (the earlier report notes SAML SSO) without DCR, it is `oauth_app` and each customer admin must register a client and supply endpoints, which for per-tenant identity providers cannot be pre-filled in `oauth_defaults`.
- OAuth 2.0 **client-credentials** (machine-to-machine) is not an offered MCP auth mode; the nearest fit is a static bearer token, which means a manually rotated service token.

### Gaps
- `_apply_client_auth`, `exchange_code_for_tokens` and `save_oauth_credentials` were not read line by line.
- Multi-organization behaviour of DCR clients (`backend/tests/e2e/test_oauth_mcp_multiorg.py`) and redirect-URI scheme rules (`docs/feedback-loops/oauth-redirect-uri-schemes.md`) were not opened.
- TLS options for MCP servers with private certificate authorities (custom CA bundle, `verify` toggle) were not found in `MCPConfig` and were not searched for elsewhere.
- Outbound proxy handling (`env_proxy_for`, `backend/app/core/egress.py:260`) was not read.

## 4. Custom API route

### Takeaway
`custom_api` is a declarative REST tool provider: an admin types a base URL, static headers and a list of endpoint definitions into a form (or JSON), each endpoint becomes one tool, and calls go through the same `execute_mcp` path as MCP tools. There is no OpenAPI import, no pagination, no response shaping and no retry or rate-limit back-off in the client. Auth covers none, bearer, Basic, API-key header and per-user OAuth authorization-code; OAuth client-credentials is not supported.

### Cited Findings
**Definition**
- Registry entry: "Connect to any REST API by defining endpoint schemas. Endpoints are exposed as callable tools."; `is_connection=False`, `data_shape="tools"`, `catalog_ownership="none"`, `ui_form="custom_api"`, `version="beta"` — `backend/app/schemas/data_source_registry.py:2884-2929`
- `CustomAPIConfig`: `base_url`, `headers` (static, every request), `endpoints` (list, `ui:type: json`), `csrf_token_flow`, `csrf_fetch_path` — `backend/app/schemas/data_sources/configs.py:4777-4815`
- Endpoint shape: `{name, description, method, path, parameters:[{name, in, type, required, description}], confirm}` — `backend/app/schemas/data_source_registry.py:3262-3264`
- Parameter features: `in` = `path | query | header | body` (default `query`); types `string, number, integer, boolean, object, array`; optional `enum`, `default`, `example`, array `items`, object `properties` / `required_properties`, or a full JSON-Schema `schema` override — `backend/app/data_sources/clients/custom_api_client.py:28-33`, `:151-188`, `:262-281`
- Pinned parameters: a parameter with a `value` is injected server-side on every call, hidden from the model, and wins over a model-supplied argument — `backend/app/data_sources/clients/custom_api_client.py:142-149`, `:200-203`, `:263-268`
- Undeclared arguments are not dropped: they go into the JSON body for body-capable methods and into the query string for GET/DELETE/HEAD — `backend/app/data_sources/clients/custom_api_client.py:283-292`
- Per-endpoint `timeout` in seconds, default 30, clamped to 1-120 — `backend/app/data_sources/clients/custom_api_client.py:65-67`, `:298-303`
- Form editor: per-endpoint rows with parameter rows, pin toggle, tri-state approval (`auto | ask | allow`), and a per-tool live "Test" with arguments — `frontend/components/CustomAPIConnectionForm.vue:242-256`, `:270-296`, `:411-430`
- Unknown keys in an endpoint definition (`enum`, `schema`, `items`, `timeout`, ...) are preserved by the form — `frontend/components/CustomAPIConnectionForm.vue:486-490`
- **No OpenAPI/Swagger import**: a case-insensitive search for `openapi|swagger` in `frontend/components/CustomAPIConnectionForm.vue`, `backend/app/data_sources/clients/custom_api_client.py`, `backend/app/services/connection_service.py`, `backend/app/routes/data_source.py` and `backend/app/routes/connection.py` returned no matches
- Presets: `CustomApiPreset` (`key`, `title`, `description`, `category`, `base_url`, `auth`, `headers`, `endpoints`, `oauth_defaults`); one exists, `x_write` — `backend/app/schemas/data_source_registry.py:3251-3266`, `:3275-3314`; served by `GET /connectors/custom-api-presets` — `backend/app/routes/data_source.py:54-63`; opened from the catalog by `selectCustomApiPreset` — `frontend/components/AddConnectionModal.vue:602-621`

**Auth**
- Variants: `none` (system), `bearer` (system, user), `basic` (system, user), `api_key` (system, user), `oauth_app` "OAuth Client (admin-configured)" (system, user) — `backend/app/schemas/data_source_registry.py:2893-2922`
- Header construction: `Authorization: Bearer` for `bearer | oauth_app | oauth`; a named header for `api_key`; `Authorization: Basic` for `basic`; static `headers` merged last — `backend/app/data_sources/clients/custom_api_client.py:55-58`, `:107-119`
- `oauth_app` is per-user authorization-code OAuth reusing the MCP OAuth-app credential class and the same `get_oauth_params` branch — `backend/app/schemas/data_sources/configs.py:4862-4870`; `backend/app/services/connection_oauth_service.py:190-196`
- The form saves OAuth connections as `user_required` with `allowed_user_auth_modes: ['oauth']`, everything else as `system_only` — `frontend/components/CustomAPIConnectionForm.vue:857-858`
- DCR is MCP-only (`ensure_mcp_oauth_config` returns early for other types) — `backend/app/services/mcp_dcr_service.py:318-319`
- CSRF token handshake for session-bound APIs (SAP Gateway style), with one retry on a stale token — `backend/app/data_sources/clients/custom_api_client.py:95-100`, `:305-334`, `:463-481`

**Call and result handling**
- One HTTP request per call on a pinned shared transport, `follow_redirects=True` — `backend/app/data_sources/clients/custom_api_client.py:431-481`
- Status >= 400 returns `success: False` with a message extracted from the body; timeouts return "Request timed out after Ns" — `backend/app/data_sources/clients/custom_api_client.py:483-503`
- Result shaping mirrors MCP: JSON parsed below the 5,000,000-character cap, oversized JSON passed raw with `parse_skipped`, JSON under a `text/plain` label still parsed, non-text bodies returned as `binaries` — `backend/app/data_sources/clients/custom_api_client.py:12-17`, `:513-599`
- Tabular detection uses the same `detect_content_type` helper as MCP — `backend/app/data_sources/clients/custom_api_client.py:601-607`
- Calls go through `execute_mcp` ("Execute a tool on a connected MCP server or custom API endpoint"), so policy, materialization to a file, `file_id` and the `create_data(source_file_ids=...)` path are identical to MCP — `backend/app/ai/tools/implementations/execute_mcp.py:499-520`, `:651`; `backend/app/ai/tools/mcp_tool_registry.py:1-4`
- Default policy: write methods (POST/PUT/PATCH/DELETE) start on "ask", reads on "allow"; an explicit `confirm: true|false` overrides and is re-applied on every refresh — `backend/app/data_sources/clients/custom_api_client.py:59-61`, `:121-133`, `:226-235`
- `test_connection` sends `HEAD {base_url}`; success is status < 400 or 405; a 404 at the root is reported as a warning because "probing the root is a heuristic"; for per-user OAuth any status below 500 counts as reachable — `backend/app/data_sources/clients/custom_api_client.py:615-692`

**Limits compared with a native connector**
- No pagination: `call_tool` issues one request and returns one response; there is no cursor, offset or `Link`-header loop in the client — `backend/app/data_sources/clients/custom_api_client.py:431-511`
- No retry and no 429 back-off in the client; only the generic in-process cooldown in `execute_mcp` applies — `backend/app/data_sources/clients/custom_api_client.py:483-491`; `backend/app/services/mcp_cooldown.py:13-22`
- No response transformation or field selection: the body is returned as received — `backend/app/data_sources/clients/custom_api_client.py:568-599`
- `output_schema` is always empty — `backend/app/data_sources/clients/custom_api_client.py:220`
- No table catalog: tool providers have `data_shape="tools"`, `catalog_ownership="none"` and are skipped by schema indexing — `backend/app/schemas/data_source_registry.py:2924-2928`, `:3399-3406`
- Not callable from generated code: "generated code has no access to it" — `backend/app/ai/tools/implementations/execute_mcp.py:521-522`
- The HubSpot design doc states the trade: a native connector's value is "the indexed catalog ... tracked queries and reports, and `data_shape="tables"` integration" — `docs/design/hubspot-connector.md` (cited in earlier notes, section 3)
- A POST used for querying (common for analytics search APIs) defaults to "ask" unless the admin sets `confirm: false` — `backend/app/data_sources/clients/custom_api_client.py:128-133`

### Inferences
- A customer holding Glassbox's gated API reference could define a handful of GET/POST endpoints against their console host today, with no Bag of Words release, **provided** the API authenticates with a static bearer token, Basic credentials, an API-key header, or authorization-code OAuth. If it needs OAuth client-credentials, a login call that returns a session cookie, or request signing, `custom_api` cannot do it without code changes (the CSRF flow is the only session-style handshake present).
- Paginated Glassbox endpoints would work only by exposing the page/cursor parameter to the model and letting it call repeatedly; `execute_mcp` surfaces envelope cursors as `result_metadata` to support that, at the cost of one model turn and one file per page.
- Because there is no query pushdown or catalog, the agent learns what an endpoint returns only from the endpoint description the admin writes plus the `record_shape` of each result. Good descriptions are the whole "semantic layer" on this route.
- A Glassbox `CustomApiPreset` (pre-filled endpoints) is technically possible and would be the middle option between "recipe in docs" and a native connector, but it has the same fixed-`base_url` problem as an MCP preset, and it requires knowing the endpoints.

### Gaps
- How `custom_api` credentials reach `auth_type` in the constructor (it is not a credentials field; `test_resolve_client_preserves_auth_type` at `backend/tests/unit/test_custom_api_oauth.py:156` suggests special handling) was not traced.
- `docs/feedback-loops/custom-api-basic-auth-hardening.md`, `2026-11-08-custom-api-beside-onedrive.md` and `x-oauth-and-posting.md` were not opened.
- Whether per-user `bearer`/`api_key` (scope `user`) is actually selectable in `CustomAPIConnectionForm.vue` (the form saves non-OAuth as `system_only`) was not confirmed beyond line 857.

## 5. Tests, docs, icons, locale strings and release steps when adding a preset

### Takeaway
An MCP preset needs four edits at minimum (registry literal, icon file, one icon-map line, unit-test assertions) plus README, CHANGELOG/VERSION, a feedback-loop doc and UI screenshots by repo convention. No locale keys are needed for a preset as such: title and description come from the registry in English, and the form's strings are shared.

### Cited Findings
- Skill instructions: "Extend `backend/tests/unit/test_mcp_presets.py` — pin the contract (auth, server_url, transport, `oauth_defaults`, `allowed_auth`, `sample_tools`) and catalog serialization"; live pass "catalog tile → prefilled form → Test/Verify → create → tools discovered on first sign-in"; screenshots via ui-evidence; reference loops `docs/feedback-loops/mcp-preset-form-defaults.md` and `docs/feedback-loops/x-mcp-preset.md` — `.agents/skills/add-connection-type/SKILL.md:169-177`
- `allowed_auth` uses form vocabulary (`dcr`), not the catalog value (`oauth`) — `.agents/skills/add-connection-type/SKILL.md:165-167`
- Tests that enumerate presets by key and would take a Glassbox assertion:
  - `test_presets_include_default_dcr_set` (subset check on five keys) — `backend/tests/unit/test_mcp_presets.py:12-14`
  - `test_dcr_presets_are_oauth` — `backend/tests/unit/test_mcp_presets.py:17-22`
  - `test_oauth_app_presets_prefill_endpoints` (iterates `x, github, google_drive, hubspot`) — `backend/tests/unit/test_mcp_presets.py:49-54`
  - `test_dcr_presets_have_no_oauth_defaults` (`:73`), `test_preset_allowed_auth_gating` (`:79`), `test_catalog_exposes_preset_form_spec` (`:181-188`), `test_sample_tools_present` (`:191-199`) — `backend/tests/unit/test_mcp_presets.py`
  - Per-preset block precedent: eight `test_hubspot_*` tests — `backend/tests/unit/test_mcp_presets.py:94-169`
  - `test_new_presets_are_in_the_catalog` and per-preset contract tests for GitLab/Bitbucket/Atlassian — `backend/tests/unit/test_mcp_code_host_presets.py:30-58`
- None of these asserts an exact total count of presets (the enumerating tests use subset checks or named keys), so adding a preset breaks nothing; a new assertion is convention, not enforcement — `backend/tests/unit/test_mcp_presets.py:12-14`; `backend/tests/unit/test_mcp_code_host_presets.py:56-58`
- Custom API preset tests live in `backend/tests/unit/test_custom_api_oauth.py` (imports `custom_api_preset`, `custom_api_presets` at `:17`; `test_custom_api_oauth_app_resolves` `:184`); client contract tests in `backend/tests/unit/test_custom_api_client.py` (`:43-504`); end-to-end `backend/tests/e2e/test_custom_api_tools.py`
- The only test files referencing `connectors/catalog`, `custom-api-presets`, `mcp_presets` or `custom_api_presets` are those three unit files; no e2e or `frontend/tests` file does — grep over `backend/tests` and `frontend/tests`
- Icon: add the asset under `frontend/public/data_sources_icons/` and one entry in `CONNECTOR_ICON_FILE` (keyed by preset key, explicit filename) — `frontend/components/DataSourceIcon.vue:86-103`, `:106-112`; without a mapping the connection falls back to the generic `/icons/mcp.png` — `frontend/components/DataSourceIcon.vue:150-154`
- Locale strings: the GitLab/Bitbucket commit touched `locales/{en,es,he}.json` only, for generic Basic-auth form labels — `git show --stat b4e92be1`; ten locale files exist (`ar, de, en, es, fr, he, it, pt, ru, sv`) — listing of `locales/`
- README table of ready-to-connect MCP integrations lists Monday, Notion, Jira/Atlassian, Linear, Sentry, GitHub, HubSpot, Google Drive, X, X (Write), Custom MCP server, Custom API — `README.md:242-259`; GitLab and Bitbucket are in `MCP_PRESETS` but not in this table
- CHANGELOG precedent lines — `CHANGELOG.md:228` (GitLab/Bitbucket), `CHANGELOG.md:577` (HubSpot); release-notes skill bumps `VERSION` and `CHANGELOG.md` — `AGENTS.md` ("Agent skills")
- Feedback-loop docs per preset exist: `docs/feedback-loops/hubspot-mcp-preset.md`, `docs/feedback-loops/x-mcp-preset.md`, `docs/feedback-loops/mcp-preset-form-defaults.md` — listing of `docs/feedback-loops/`
- `sample_tools` precedent: HubSpot shipped `None` first, then was filled from a live `tools/list`; "They are discovered names, not guesses" — `docs/feedback-loops/hubspot-mcp-preset.md` (finding 3); `backend/app/schemas/data_source_registry.py:3003-3015`
- UI changes under `frontend/{pages,components,layouts,assets}/**` need before/after screenshots (ui-evidence); docs.bagofwords.com is refreshed via the docs-update skill — `AGENTS.md` ("Agent skills")

### Inferences
- The locale commit for GitLab/Bitbucket added keys to three of ten catalogs. Whether the other seven were updated in a separate commit, or the "identical shape" rule is enforced only for some locales, was not checked; anyone adding locale keys should verify the sync check in `docs/design/i18n.md` first. A plain preset avoids the question because it adds no keys.
- The egress-matrix and literal-rule enforcement tests described in the earlier notes (section 6) apply to `requests`-based native clients and do not apply to a preset.

### Gaps
- `docs/feedback-loops/mcp-preset-form-defaults.md` and `x-mcp-preset.md` were not opened; their loop steps are taken from the skill's pointer only.
- The release-notes, ui-evidence and docs-update skill files were not read.
- Whether the catalog-search alias mechanism (earlier notes, section 5 gap) covers preset tiles was not determined.

## 6. Session-ID stitching: which registered connectors could carry Glassbox session IDs, and how an admin could teach the agent to build replay links

### Takeaway
Snowflake, BigQuery, Databricks SQL, Redshift, Splunk, Athena, ClickHouse, Elasticsearch, Azure Data Explorer and PostgreSQL are registered and can query a customer table that holds a Glassbox session-ID column today. New Relic, Adobe Analytics, Datadog and Dynatrace have no connector; Google Analytics has a client file but no registry entry. The instructions system (organization or agent scoped, always-loaded or keyword-retrieved, attachable to a specific table and column) is an existing mechanism for teaching the replay-link pattern without code.

### Cited Findings
**Registry status** (key searched as `    "<key>": DataSourceRegistryEntry` in `backend/app/schemas/data_source_registry.py`)

| Source named in the brief | Registry key | Status | Line |
|---|---|---|---|
| Snowflake | `snowflake` | registered | `:783` |
| BigQuery | `bigquery` | registered | `:810` |
| Databricks | `databricks_sql` | registered (`databricks` is not a key) | `:1778` |
| Redshift | `aws_redshift` | registered (`redshift` is not a key) | `:1455` |
| Splunk | `splunk` | registered | `:1237` |
| Athena | `aws_athena` | registered | `:844` |
| ClickHouse | `clickhouse` | registered | `:1272` |
| Elasticsearch | `elasticsearch` | registered | `:1215` |
| Azure Data Explorer | `azure_data_explorer` | registered | `:1330` |
| PostgreSQL | `postgresql` | registered | `:646` |
| PostHog | `posthog` | registered | `:1701` |
| Google Analytics 4 | none (`google_analytics`, `ga4` absent) | **client file only**: `backend/app/data_sources/clients/google_analytics_client.py` | n/a |
| New Relic | none (`newrelic`, `new_relic` absent) | no client file, no entry | n/a |
| Adobe Analytics | none (`adobe_analytics`, `adobe` absent) | no client file, no entry | n/a |
| Datadog, Dynatrace, Mixpanel, Amplitude | none | no client file, no entry | n/a |

- Client-file check: `ls backend/app/data_sources/clients | grep -i "relic|adobe|analytics|splunk|databricks|redshift|datadog|dynatrace|amplitude|mixpanel"` returned only `aws_redshift_client.py`, `databricks_sql_client.py`, `google_analytics_client.py`, `splunk_client.py`

**Instruction / semantic-hint mechanism**
- `Instruction` model: free `text`, `status`, `category`, `kind`, `scope_mode` (default `global`), many-to-many link to data sources (agents), `load_mode` (`always | intelligent | disabled`), `applicable_modes`, `applicable_channels`, `title`, `description`, `structured_data`, git-sync fields — `backend/app/models/instruction.py:11-12`, `:19-34`, `:66-98`
- `InstructionReference` attaches an instruction to an object with `object_type` ("metadata_resource | datasource_table | memory"), `object_id`, optional `column_name`, and `relation_type` ("scope | mention") — `backend/app/models/instruction_reference.py:7-15`
- Context builder loads all `always` instructions in full and retrieves `intelligent` ones by keyword search — `backend/app/ai/context/builders/instruction_context_builder.py:53-66`, `:226-258`, `:291-316`
- README: "Each agent gets its own data, tools, credentials, instructions, permissions" — `README.md:8`
- Instructions can also be scoped to MCP tools/tables (files exist: `backend/app/ai/tools/mcp/instruction_scope.py`, `instruction_placement.py`; test `backend/tests/unit/test_mcp_table_selection_instructions.py`) — file listing only

### Inferences
- A no-code stitching recipe is: connect the warehouse or Splunk as usual; add an instruction scoped to the agent (and optionally referenced to the table and session-ID column) that states the column's meaning and the link template, for example "to link a Glassbox replay, build `https://<console host>/webinterface/webui/...` from `glassbox_session_id` and the session start time in epoch milliseconds". The agent's generated SQL/pandas can then emit a URL column. The exact URL template must come from the customer's console; the earlier report verified its shape from a single public example only.
- New Relic is the one third-party tool with a verified Glassbox session-ID attribute (`GlassboxSessionId`, per the earlier report), and it is not connectable natively. The available routes would be the generic MCP tile (if the customer runs a New Relic MCP server) or a `custom_api` endpoint for NerdGraph; neither was verified here.
- Splunk is registered, but the earlier report's snippet-level evidence was of a user unable to get Glassbox events into Splunk, so Splunk helps only where the customer already forwards session IDs in their own logs.
- Stitching and an MCP/custom-API connection compose: a warehouse query returns session IDs, and the agent can pass one to a Glassbox tool in a later step, because tool arguments are model-supplied. No code links the two; the instruction would have to describe the hand-off.

### Gaps
- Whether instruction text is included in the **coder** prompt (so generated code knows the URL template) or only in the planner's context was not traced.
- Whether report/dashboard table cells render a URL column as a clickable link was not checked in the frontend.
- The MCP-scoped instruction files were not read, so how an instruction is attached to a specific MCP tool is unverified.
- The Splunk, Databricks SQL and Redshift clients were not opened; registry status only.

## 7. Assessment: file checklists, open questions, and what decides between the routes

### Takeaway
Assessment (mine, derived from the findings above): if Glassbox's MCP server is a remote HTTP endpoint with one of the six supported auth modes, an integration works **today with zero code** through the generic "MCP Server" tile, and a branded preset is a four-file change. The single structural mismatch is the per-tenant URL. Custom API is viable as a no-code probe only if the read API uses a simple static credential. A native connector is justified only if Glassbox exposes row-level data that the MCP server withholds and that customers want as an indexed, schedulable table catalog.

### Cited Findings
(Each item restates a finding cited above; citations repeated for the load-bearing ones.)
- Presets resolve to `type="mcp"`, need no client or schema — `backend/app/schemas/data_source_registry.py:490-498`; `.agents/skills/add-connection-type/SKILL.md:101-108`
- Transports limited to SSE and streamable HTTP; no stdio — `backend/app/data_sources/clients/mcp_client.py:812-831`
- Preset recognition by exact URL; `catalog_key` honoured by backend but never written by the form — `frontend/components/MCPConnectionForm.vue:542-546`, `:690`; `backend/app/services/data_source_service.py:54-57`
- Custom API has no pagination, OpenAPI import or client-credentials OAuth — `backend/app/data_sources/clients/custom_api_client.py:431-511`; `backend/app/schemas/data_source_registry.py:2893-2922`
- Results from either route can be charted and mixed with warehouse tables via `create_data` — `backend/app/ai/tools/schemas/create_data.py:51-75`

### Inferences
**(1) Glassbox MCP preset: file checklist**

Add:
1. `frontend/public/data_sources_icons/glassbox.png` (or `.svg`).
2. `docs/feedback-loops/glassbox-mcp-preset.md` (convention; mirrors `hubspot-mcp-preset.md`).
3. Evidence screenshots for the PR (catalog tile, prefilled form, tools list), per ui-evidence.

Edit:
4. `backend/app/schemas/data_source_registry.py`: one `McpPreset(key="glassbox", title="Glassbox", server_url=..., auth=..., allowed_auth=[...], category="services", description=..., sample_tools=None or a live-discovered list, oauth_defaults=... only for oauth_app)` in `MCP_PRESETS` (`:3017`).
5. `frontend/components/DataSourceIcon.vue`: `glassbox: 'glassbox.png'` in `CONNECTOR_ICON_FILE` (`:89-103`).
6. `backend/tests/unit/test_mcp_presets.py`: a `test_glassbox_*` block pinning `auth`, `server_url`, `transport`, `allowed_auth`, `oauth_defaults`, `sample_tools`, plus membership in the relevant enumerating test (`:17-22` if DCR, `:49-54` if oauth_app).
7. `README.md:246-259` table row; `CHANGELOG.md` and `VERSION`.

Only if needed:
8. `frontend/components/MCPConnectionForm.vue` (+ possibly `AddConnectionModal.vue`): write and read `config.catalog_key` so a tenant-specific URL keeps its Glassbox branding and auth gating. Required if the URL is per tenant; this is a UI change needing before/after screenshots.
9. `backend/app/schemas/data_sources/configs.py` + `mcp_client.py` + form + locale keys in all catalogs: only if Glassbox needs an auth mode outside the six (the Basic-auth addition in `1cf313e2`/`b4e92be1` is the size reference: about 360 lines across 14 files, most of it tests and a migration unrelated to auth).

Not expected to change: Alembic migrations, routes, services, `backend/app/ai/**`, locale files (for a plain preset), egress tests.

**Open questions for the preset (answers must come from Glassbox)**
- Is the MCP endpoint one global URL or per tenant / per deployment? (Decides item 8 and whether a preset is worth shipping over a docs recipe.)
- Transport: streamable HTTP, SSE, or local stdio? (stdio is unsupported.)
- Auth: OAuth with DCR, OAuth with a pre-registered client (whose authorization server: Glassbox's or the customer's SSO?), static token, Basic, API-key header? Are refresh tokens issued?
- Does the server set `readOnlyHint` on its tools? (Otherwise every tool starts on "ask".)
- Tool list and count (against the 50-tool native cap), result formats (JSON records vs prose), result sizes, pagination cursors, rate limits.
- Is it available to on-premises and single-tenant customers, and at what network location?
- Licensing/GA status, and whether masking applies to MCP output.

**(2) "Use Custom API for Glassbox" recipe with no code change: checklist**

No repo files change except, optionally, a docs page (docs.bagofwords.com via docs-update, or a `docs/` recipe). The recipe an admin would follow, from the code as it stands:
1. Add Connection → Custom API; `base_url` = the tenant's Glassbox API root (must pass the ADMIN egress policy; private hosts fail only when `block_private_outbound` is on).
2. Choose auth: bearer, Basic, API key (with header name), or OAuth client (authorization-code, per user).
3. Add static headers if the API needs a tenant or application identifier on every request; or pin them as parameters.
4. Define endpoints: name, method, path with `{placeholders}`, parameters with `in`/`type`/`required`/`description`; set `confirm: false` on read-only POST search endpoints; raise `timeout` up to 120 s for slow reports.
5. Expose pagination parameters (cursor/offset/page) as ordinary parameters and say in the endpoint description how to page.
6. Write endpoint descriptions that state the response shape and field meanings, since there is no catalog.
7. Use each tool's "Test" in the form; attach the connection to an agent; set tool policies.
8. Add an instruction describing when to use each tool and how to build replay links.

Open questions for the recipe: the same API facts as the earlier report's table (endpoints, auth, pagination, limits), plus two that are specific to this route: does auth fit one of the five variants (client-credentials, cookie login and signed requests do not), and are single responses small enough to be useful without automatic paging?

**(3) Facts that decide between preset, custom API and native connector**

| Fact to obtain from Glassbox | Points to |
|---|---|
| MCP server is remote HTTP (streamable or SSE) with OAuth/DCR, OAuth app, bearer, Basic or API key | MCP: works now via generic tile; preset is cosmetic polish |
| MCP server is stdio/local only, or uses unsupported auth | MCP route blocked without platform work; fall back to custom API or native |
| MCP endpoint is per tenant | Docs recipe on the generic MCP tile first; preset only with the `catalog_key` form change |
| MCP tools return structured records (sessions, errors, funnels) | MCP is sufficient for charts and warehouse joins |
| MCP tools return only Pulse prose/evidence summaries | MCP gives narrative answers only; row-level analysis needs the REST API |
| REST read API exists with static-credential or auth-code auth and modest responses | Custom API recipe is viable immediately and is the cheapest way to learn the API |
| REST API needs client-credentials, session login or signing; or large paginated exports | Native connector (handles token minting, paging, back-off in client code) |
| Customers need an indexed table catalog, tracked/scheduled queries, or code-level access from generated Python | Native connector; neither tool route provides these |
| A SQL-like or report-execution endpoint exists | Native connector on the PostHog archetype (earlier notes, section 3) |
| No customer access to API or MCP at all | Stitching only (section 6) |

A reasonable sequence, as assessment: try the generic MCP tile against a real tenant first (zero code, yields the tool list for `sample_tools`), in parallel wire two or three REST endpoints through Custom API to see real payloads, and decide on a native connector only after comparing what the two return.

### Gaps
- Every Glassbox-side fact above is unknown in this repo; the checklists are conditional on them.
- None of the checklists was validated by running anything; in particular the claim that a preset breaks no existing test is from reading the assertions (subset checks), not from a test run.
- The effort estimate for the `catalog_key` form change is unscoped; I identified where the config is built (`frontend/components/MCPConnectionForm.vue:649`, `:690`) and where it is read (`:542-546`), not what else depends on exact-URL matching.
- Whether Bag of Words' own docs site already has a "Custom API" or "MCP Server" how-to page that a Glassbox recipe could extend was not checked.
