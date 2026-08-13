# monday.com connector — sandbox feedback loop

Adds `monday` as a first-class queryable data source type (boards → tables →
`execute_query` → DataFrame), alongside the existing `monday` MCP preset
(which stays, for tool-style access). Verified end-to-end against a live
monday.com trial account (EU region) with Claude 4.5 Haiku as the only model.

## What was built

- `backend/app/data_sources/clients/monday_client.py` — `MondayClient(DataSourceClient)`
  over the GraphQL API (`api.monday.com/v2`, API-Version 2024-10). Boards →
  `Table` (name-disambiguated with `[board_id]` on duplicates), columns →
  `TableColumn` named by column TITLE (id in the description), status/dropdown
  labels surfaced in column descriptions, `board_relation` columns → FKs.
  `execute_query` takes a JSON spec (`board`, `columns`, `rules`, `operator`,
  `order_by`, `limit`) → `items_page` + `next_items_page` cursor pagination
  (page 500, cap 10k). 429s (HTML body + Retry-After) and complexity
  throttles are absorbed with bounded retries.
- `configs.py`: `MondayConfig` (workspaces/boards scoping), `MondayApiTokenCredentials`
  (api_token + optional oauth_client_id/secret, ServiceNow pattern).
- Registry: `monday` entry — `api_token` (system+user) + `oauth` (user,
  `OAuthDelegatedCredentials`), explicit `client_path`, category `services`.
- `connection_oauth_service.get_oauth_params`: `monday` branch —
  `https://auth.monday.com/oauth2/{authorize,token}`, read-only scopes
  (`boards:read workspaces:read users:read account:read`). monday tokens do
  not expire; no refresh token exists, so there is no refresh path.
- `connection_service.default_user_auth_modes`: monday added to the
  oauth_client_id → `["oauth"]` list.
- Tests: `tests/unit/test_monday_client.py` (11), two monday cases in
  `tests/unit/test_connection_oauth.py`, `monday` in
  `tests/integrations/ds_clients.py` (remote mode, needs
  `{"monday": {"enabled": true, "api_token": "..."}}`).

## Live findings (worth keeping)

- **Filter rules match label INDICES, not text.** `{"compare_value": ["Done"],
  "operator": "any_of"}` on a status column returns 0 rows; the index (e.g. 2)
  matches. The client translates label text → index from `settings_str`, so
  generated queries can use human labels. Verified live both ways.
- **`greater_than` on timeline columns** → `no_operator_config` error; date
  comparisons in rules are unreliable. The system prompt steers the coder to
  fetch + filter in pandas.
- **429s come back as HTML** (a font-embedded page), not JSON — parse nothing,
  read `Retry-After`.
- **Trial accounts have a small daily API budget.** Bulk-seeding ~300 boards
  exhausted it (~800 mutations in): after that even `query { me { id } }`
  429s for hours. Plan seeding/indexing accordingly; the client's bounded
  retries surface a clean error instead of hanging forever.
- Community license: switching a monday connection to `user_required`
  (per-user OAuth) is enterprise-gated like every tables-shaped connector.

## Sandbox loop (reproduced)

1. Boot backend + frontend per `sandbox-feedback-loop`; Anthropic provider with
   ONLY Claude 4.5 Haiku enabled (becomes default).
2. `/agents/new` → Services → monday.com tile → schema-generated form
   (Workspaces/Boards config, API Token credential) → Test connection
   ("Connected to monday.com as … on account …") → Save and Continue →
   "Discovered 4 tables · 1s".
3. Tables step defaults to INACTIVE — "Select all" + Save (agent page →
   "N tables" → Select all → Save; verify `datasource_tables.is_active=1`).
4. Chat: "How many items does each board have? And show the status breakdown of
   the Product Roadmap Q4 [000] board" → Haiku generated
   `ds_clients["Monday:monday.com"].execute_query('{"board": …, "limit": 10000}')`
   and a second step with `"columns": ["Status"]` — both steps `success`,
   38 requests to api.monday.com in the backend log, 751-item board proved
   cursor pagination past the 500-row page.
5. OAuth: token endpoint validated the app's client id/secret live
   (`invalid_grant` for a fake code vs `Invalid client_secret param` /
   `Invalid client_id param` for wrong credentials); authorize 302s into
   monday login carrying the request payload. Browser consent not automatable
   here (Chromium egress blocked in the remote sandbox + account is Google
   SSO) — the last mile needs a human click on the authorize URL produced by
   `GET /connections/{id}/oauth/authorize`.
