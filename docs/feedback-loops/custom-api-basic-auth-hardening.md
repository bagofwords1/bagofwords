# Custom API: basic auth + execution hardening + tools UI

## What changed

**Basic auth (username/password)** is now a first-class auth option for
`custom_api` connections:

- `CustomApiClient` sends `Authorization: Basic base64(user:pass)` when
  `auth_type == "basic"`.
- New `CustomAPIBasicCredentials` schema + `"basic"` variant in the registry
  (`scopes: system, user`), so create/edit/test flows validate and store the
  secret like every other credentialed variant.
- `CustomAPIConnectionForm` gained the "Basic Auth (username & password)"
  option with username/password inputs (blank-on-edit keeps saved values).

**Execution parity with the MCP path.** The custom_api client used to be a
thin sketch next to `McpClient`; it now matches it capability-for-capability:

- Parse cap (`MAX_PARSE_CHARS`, 5M chars) with `parse_skipped`/`size_chars`
  so oversized JSON responses reach execute_mcp's materializer unparsed
  instead of exploding worker memory — and still land on disk as complete
  JSON.
- Non-text responses (PDF, images, …) come back as `binaries` (base64 +
  mime), which execute_mcp materializes into session files.
- HTTP error bodies are mined for the actual message
  (`{"error": {...}}`, `{"detail": ...}`, FastAPI shapes) instead of dumping
  500 chars of body at the agent.
- JSON mislabeled as `text/plain` is still parsed (prefix sniff); declared
  JSON that fails to parse degrades to text instead of erroring.
- Timeouts are surfaced distinctly, follow redirects enabled, per-endpoint
  `timeout` override (capped at 120s).
- `server_url` property so execute_mcp's loopback DB-release check applies.

**Richer endpoint parameters** for complex inputs:

- Param `type` may be `object` / `array` (plus the existing scalars);
  `enum`, `default`, `example`, array `items`, object `properties` /
  `required_properties` all flow into the tool's JSON Schema — so the
  planner's local schema validation (execute_mcp) and the `<mcp_tools>`
  rendering see real nested shapes, not `string` everywhere.
- A param-level `schema` field is a full JSON Schema override.
- `in: header` params are supported at call time.
- Undeclared arguments are routed (body for body-capable methods, query
  otherwise) instead of silently dropped.
- Path/query/body/header routing no longer double-sends consumed params in
  the fallback body.

**Tools UI (`/agents/{id}/tools`)** now shows what each HTTP tool actually
calls:

- `list_tools` emits `metadata: {method, path}`; `refresh_tools` persists it
  to `ConnectionTool.metadata_json`; the agent-tools API returns `metadata`,
  `input_schema`, and `connection_type`.
- `ToolsSelector` renders a color-coded method chip (GET/POST/PUT/PATCH/
  DELETE) + mono path next to each custom_api tool, and the expanded view
  finally renders the Parameters grid (input_schema previously never reached
  this page for any tool).
- The visual endpoint builder preserves unknown keys (enum, schema, items,
  confirm, timeout…) through raw-JSON ⇄ visual round-trips, adds a param
  description input, and offers object/array/header options.

## Sandbox verification (2026-08-04)

Booted the full sandbox (backend + frontend + real Claude 4.5 Haiku) and a
stdlib mock "Acme ERP" API on :9100 — 13 endpoints behind HTTP Basic auth
(apiuser/apipass123): customers (envelope pagination), nested-body order
search, order CRUD, products, warehouse stock, nested metrics, a text/csv
report, and a binary PDF invoice.

Verified through the UI (Playwright) and at the DB/log/HTTP layers:

1. **Connection form**: created the connection with Basic Auth via the modal;
   "Test connection" → "Connected successfully. Found 12 tool(s)."
2. **Discovery**: 12 `connection_tools` rows with `metadata_json`
   `{"method": ..., "path": ...}`; write endpoints defaulted to policy
   `ask`, `search_orders` (POST + `confirm: false`) stayed `allow`.
3. **Tools page**: method/path chips and expanded parameter grids rendered
   (incl. nested `filters` object with required badge).
4. **agent_v2 read path**: "find the 3 highest-value open orders over
   $10,000, then get the top order's customer" → `execute_mcp` called
   `search_orders` with nested args
   `{"filters": {"statuses": ["open"], "min_total": 10000}, "sort":
   {"field": "total", "direction": "desc"}, "limit": 3}` and
   `get_customer(customer_id="C-0042")`; both materialized to JSON files;
   the answer matched mock ground truth (O-00047, $17,369.93, Customer 42).
5. **Write path with approval**: "update O-00047 to shipped" paused on the
   `ask` policy card (Allow once / Always allow / Deny / Always deny);
   approving issued `PUT /orders/O-00047` → 200, thread shows "Allowed by
   Test Admin".
6. **Analysis path**: "fetch ALL customers, chart count by region" →
   `list_customers(limit=100)` → create_data → bar chart; counts matched the
   mock's seeded distribution exactly (NA 24, EMEA 22, APAC 19, LATAM 15).
7. **Negative auth**: wrong password → clean
   `HTTP 401: Missing or invalid Basic credentials` (message extracted from
   the JSON error body).

Unit coverage: `backend/tests/unit/test_custom_api_client.py` (28 tests —
auth headers, schema generation, request routing, parse cap, error
extraction, binaries). The parse-cap test caught a real bug during
development (`application/json` wasn't classified as text, sending every
JSON response down the binary path) — fixed in `_is_texty`.
