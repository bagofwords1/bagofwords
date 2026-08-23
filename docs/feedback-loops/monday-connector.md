# monday.com connector — sandbox feedback loop

Adds `monday` as a first-class queryable data source type (boards → tables →
`execute_query` → DataFrame), alongside the existing `monday` MCP preset
(which stays, for tool-style access). Verified end-to-end against a live
monday.com trial account (EU region) with Claude 4.5 Haiku as the only model.

## What was built

- `backend/app/data_sources/clients/monday_client.py` — `MondayClient(DataSourceClient)`
  over the GraphQL API (`api.monday.com/v2`, API-Version 2026-04). Boards →
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
- Tests: `tests/unit/test_monday_client.py` (14), two monday cases in
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

## Regression loop — multi-level boards missing from discovery

### Root cause (validated)

The connector pinned monday API version `2024-10` and its paginated `boards`
query did not specify `hierarchy_types`. monday excludes multi-level boards
from that legacy query for backwards compatibility, so an API token could see
the board in monday's UI while Bag of Words silently indexed only classic
boards. The omission was at
`backend/app/data_sources/clients/monday_client.py:29` and
`backend/app/data_sources/clients/monday_client.py:202` before the fix.

### Loop A — deterministic reproduction

The HTTP boundary stub returns a classic board for a legacy/unqualified query
and returns both classic and multi-level boards only when the request uses a
supporting API version and explicitly selects both hierarchy types:

```bash
cd backend
UV_CACHE_DIR=/tmp/bow-monday-uv-cache uv run pytest \
  tests/unit/test_monday_client.py::test_get_schemas_discovers_classic_and_multi_level_boards -q
```

Before the fix, the observed result was:

```text
FAILED test_get_schemas_discovers_classic_and_multi_level_boards
Extra items in the right set: 'Portfolio Projects'
1 failed
```

### The fix and verification

`monday_client.py` now pins stable API version `2026-04`, explicitly sends
`hierarchy_types: [classic, multi_level]`, requests `hierarchy_type`, and
persists it in each table's metadata. Re-running the full connector unit suite:

```bash
cd backend
UV_CACHE_DIR=/tmp/bow-monday-uv-cache uv run pytest \
  tests/unit/test_monday_client.py -q --disable-warnings
```

Observed:

```text
..............                                                           [100%]
14 passed
```

This proves every active board visible to the API-token identity is discovered
regardless of classic versus multi-level storage. It does not change the
existing workspace/board configuration filters, token permission boundaries,
or the connector's top-level-item query semantics.

## Regression loop — boards missing from a fresh sync (customer: 200 boards, 190 synced)

### Root causes (validated live against api.monday.com, EU trial seeded to 211 boards)

Four defects in `_fetch_boards`, any of which silently loses boards:

1. **Break-on-short-page pagination.** Discovery stopped as soon as one page
   returned fewer than `BOARDS_PAGE` boards. monday does not guarantee
   non-final pages are full, so a short page mid-crawl truncated every board
   created after it. Fixed: paginate until an EMPTY page.
2. **Global listing omissions.** The account-wide `boards (limit, page)` query
   is known to omit boards that workspace-scoped or by-id queries return
   (observed in the wild with shareable boards and cross-product workspaces;
   monday's own MCP server never global-crawls — it enumerates per workspace).
   Fixed: after the global crawl, a cheap per-workspace id sweep
   (`boards (workspace_ids: [...])` over `workspaces (membership_kind: all)`)
   recovers anything missing via `boards (ids: [...])`, with a warning log.
3. **Name-prefix subitem filter.** Real boards named "Subitems of …" (monday's
   "expand subitems into a new board" creates these) were dropped, and
   renamed/localized shadow boards leaked in. Fixed: filter on the API `type`
   field (`sub_items_board`); the name prefix remains only as a fallback when
   `type` is absent.
4. **Main-workspace scoping.** Boards in monday's legacy Main workspace return
   `workspace: null` (documented), so any `workspaces` config filter silently
   excluded them. Fixed: the aliases "Main workspace" / "main" select them.

Also: the `MAX_BOARDS` cap now counts real (non-subitem) boards and logs a
warning when it truncates instead of stopping silently, and discovery logs a
per-run summary (`monday discovery: N board(s) … global crawl pages […]`) so a
customer's missing-board report can be diagnosed from backend logs alone.

### Loop (reproduced)

- Unit: `uv run --extra dev python -m pytest tests/unit/test_monday_client.py`
  — 19 pass, including new regressions for short pages, global-listing
  omissions (recovered via workspace sweep), type-based subitem filtering,
  Main-workspace scoping, and workspace-listing permission failure.
- Live: against the seeded trial account (211 boards, 2 workspaces, private +
  "Subitems of …"-named real boards, shadow subitem boards), `get_schemas()`
  returns all 211; shadow boards excluded by type; scoping by workspace name
  still exact.
- Sandbox e2e: backend + frontend booted per `sandbox-feedback-loop`;
  monday data source created via API with the trial token; `test_connection`
  OK end-to-end; `refresh_schema` persisted 211 `datasource_tables` rows;
  Tables UI showed "0/211 active", Select all + Save → "211/211 active"
  (verified in sqlite). The LLM chat leg was blocked by the sandbox Anthropic
  key's credit balance (billing 400 from api.anthropic.com — unrelated to this
  change; the request pipeline itself was exercised).

### Known remaining sharp edge (out of scope here)

`save_or_update_tables` / `refresh_schema` key tables by NAME. When a board is
renamed — or a second board takes the same name, which renames both tables to
"name [id]" — the old row is deactivated and the replacements arrive inactive,
so an in-use board drops out of the agent's selection. The client already
persists `metadata_json.board_id`; sync should be keyed on it for monday.
