# Sandbox Feedback Loop — Private Connectors (tools-only, self-serve)

Runnable verification for **private connectors**: a tools-only, member-self-serve
data source (mcp / custom_api) that runs through the **existing agent loop** — no
custom loop (per design `docs/design/connectors-without-agents.md`).

Validated live against a running backend + real Claude + custom mock MCP servers,
with multiple users at different permission levels.

---

## What was built

**Backend**
- `app/core/permissions_registry.py` — new `create_private_connector` permission (in
  "Data & Connections" + default member role).
- `alembic/versions/cn1prv2conn3_member_create_private_connector.py` — grants
  `create_private_connector` to the system **member** role (idempotent).
- `app/routes/data_source.py` — `create_data_source` permission is now dynamic:
  full rights via `create_data_source`/admin, **or** a member self-serving a
  PRIVATE tools-only connector via `create_private_connector` (forced
  `is_public=False`). Non-connector (analytical) creation still needs
  `create_data_source`.
- `app/services/data_source_service.py` —
  (a) `_ds_is_connector()` + `is_connector` on list serializers (surfacing);
  (b) tool-provider connections **auto-discover tools on create** (members never
  touch the `/connections` routes, which need `manage_connections`);
  (c) Mode-1 connection validation **skips schema introspection** for
  tool-providers (they expose tools, not tables).
- `app/schemas/data_source_schema.py` — `is_connector` on `DataSourceListItemSchema`.

**Frontend**
- `components/KnowledgeExplorer.vue` (the `/agents` tree) — maps `is_connector`
  and shows a **"Connector"** badge. Private connectors already appear here (they're
  data sources the user owns); the badge labels them.

**Mocks** (`tools/sandbox/connector-mocks/`)
- `mock_mcp_regular.py` — Monday-like MCP (FastMCP streamable-http), tools
  `list_boards/get_items/create_item`; optional bearer via `MOCK_BEARER` (tier A/B).
- `mock_mcp_dcr.py` — Gmail-like MCP that advertises **RFC 9728/8414** discovery +
  **RFC 7591** `/register` + OAuth `authorize`/`token`; `/mcp` gated on the issued
  token (tier C, true DCR).
- `e2e_connectors.py` — full multi-user app e2e (real Claude).
- `verify_dcr.py` — DCR handshake protocol verification.

---

## Environment setup (fresh sandbox)

App targets **Python 3.12**; `uv` provisions it.

```bash
cd backend
uv sync --extra dev

# Secrets in a gitignored env file (.env.* is ignored). NEVER commit.
cat > .env.sandbox <<'EOF'
export ANTHROPIC_API_KEY="sk-ant-..."          # real key for live Claude
export BOW_DATABASE_URL="sqlite:///db/app.db"
export BOW_ENCRYPTION_KEY="$(python3 -c 'import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
EOF
source .env.sandbox

rm -f db/app.db db/app.db-wal db/app.db-shm     # fresh DB
uv run alembic upgrade head                      # includes cn1prv2conn3
```

## Run the env

```bash
# 1) Backend (no --reload: it watches logs/ and churns)
cd backend && source .env.sandbox
uv run uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1 --log-level warning &

# 2) Mock connectors
MOCK_PORT=9301 uv run python ../tools/sandbox/connector-mocks/mock_mcp_regular.py &   # no-auth regular
MOCK_PORT=9302 PUBLIC_URL=http://localhost:9302 \
  uv run python ../tools/sandbox/connector-mocks/mock_mcp_dcr.py &                     # DCR
MOCK_PORT=9303 MOCK_BEARER=secret-monday-token \
  uv run python ../tools/sandbox/connector-mocks/mock_mcp_regular.py &                 # bearer
```

## Verify

```bash
cd backend && source .env.sandbox
uv run python ../tools/sandbox/connector-mocks/e2e_connectors.py   # full multi-user e2e (real Claude)
uv run python ../tools/sandbox/connector-mocks/verify_dcr.py       # DCR handshake
```

---

## Results (validated)

**`e2e_connectors.py` — 13/13 ✅** (real Claude, `claude-haiku-4-5`)
- members joined same org
- Phase A: admin org-wide connector — agent invoked `execute_mcp`, answer reflected mock data
- Phase B: member self-serve PRIVATE connector — forced private, owned by member; agent invoked `execute_mcp`, real data
- Phase C: member denied analytical data source (403); member2 cannot see member1's private connector;
  connector surfaced in `/agents` with `is_connector=true`; admin governance view sees it

**`verify_dcr.py` — 11/11 ✅** — protected-resource + AS metadata (registration_endpoint) +
DCR 201 + authorize→code + token; unauth MCP rejected, DCR-authed connects, tools discovered, tool call returns data.

**Bearer (regular mock + McpClient) — 4/4 ✅** — no-token/wrong-token rejected, correct token connects + lists tools.

**Regression — 43 existing e2e passed** (`test_mcp_tools` 14, `test_custom_api_tools`, `test_data_source`, `test_rbac`, `rbac/test_rbac_data_sources` = 29).

---

## Manual test pointers

- Log in as a **member** → `/agents` → "Add" an MCP connector pointing at
  `http://localhost:9301/mcp` (transport `streamable_http`). It appears with a
  **Connector** badge and is **private** (lock).
- Open a report, select the connector, ask "list my Monday boards" → the agent
  calls the tool and answers from mock data.
- A **second member** cannot see the first member's private connector.
- Admin can create an **org-wide** connector (public) the same way.

## Outbound DCR (tier C) — implemented + verified vs REAL Notion

The backend now performs **outbound** OAuth discovery + Dynamic Client Registration
for `type=mcp` connections with no preconfigured client:
- `app/services/mcp_dcr_service.py` — `discover_mcp_oauth` (RFC 9728→8414) +
  `register_client` (RFC 7591) + `ensure_mcp_oauth_config` (persist on the connection).
- `app/routes/connection_oauth.py` — the authorize route runs `ensure_mcp_oauth_config`
  before building the consent URL; adds RFC 8707 `resource`; omits empty scope.
- `app/services/connection_oauth_service.py` — `client_secret` is now optional
  (public clients / `token_endpoint_auth_method=none`) in `get_oauth_params`,
  `exchange_code_for_tokens`, `refresh_access_token`.

Verify (hits live Notion):
```bash
cd backend && source .env.sandbox
uv run python ../tools/sandbox/connector-mocks/verify_dcr_notion.py    # direct funcs — 13/13
BOW_TEST_NOTION_DCR=1 uv run pytest tests/e2e/test_dcr_notion.py -q     # app route — 1 passed
uv run python ../tools/sandbox/connector-mocks/verify_dcr.py           # mock full token+tools — 11/11
```
Results: discovery → live DCR (real Notion `client_id`, public client) → config
persisted (idempotent) → authorize URL targets `mcp.notion.com` with client_id +
PKCE + `resource`. **Interactive consent + token exchange is the manual step**
(open the printed authorize URL, approve → callback stores the per-user token).

Notes:
- `user_required` connections (per-user OAuth) require an **enterprise license**
  (`BOW_LICENSE_KEY`); the e2e harness force-enables it. DCR itself is license-agnostic.
- DCR targets should be restricted to catalog entries / an admin host-allowlist (SSRF)
  — guardrail noted in the design doc; not yet enforced in this slice.
- `BOW_ENCRYPTION_KEY` is generated per sandbox; persist it if you want stored
  credentials to survive restarts.
