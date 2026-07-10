# Feedback Loop — Zabbix connector (new)

Validates the new Zabbix data source connector end-to-end: schema-driven admin
form → connection → live schema discovery (8 virtual tables) → agent table
selection → LLM prompts building charts/analysis from real Zabbix (JSON-RPC
API) data. Design doc: `docs/design/zabbix-connector.md`.

Zabbix is modeled on the **ServiceNow** connector, not a SQL/BI one: it's an
HTTP JSON-RPC API (`/api_jsonrpc.php`) with no query language, so
`execute_query` takes a JSON spec that maps 1:1 to a `*.get` method, and
`get_schemas` exposes a fixed catalog of virtual tables. It is an **enterprise**
data source (`requires_license="enterprise"`, gated via
`ENTERPRISE_DATASOURCES`).

## What was added

- `backend/app/data_sources/clients/zabbix_client.py` — JSON-RPC client.
  8 virtual tables (hosts, host_groups, items, triggers, problems, events,
  history, trends), each backed by a `*.get` method; JSON query spec →
  method+params; DataFrame results; two auth variants (API token Bearer /
  `user.login` session token). Key detail: the Bearer header is attached
  **per-request on authed calls only** — Zabbix 7.0 rejects
  `apiinfo.version`/`user.login` when an `Authorization` header is present.
- `ZabbixConfig` / `ZabbixTokenCredentials` / `ZabbixUserPassCredentials` in
  `backend/app/schemas/data_sources/configs.py`; registry entry (type
  `zabbix`, explicit `client_path`, `requires_license="enterprise"`) in
  `backend/app/schemas/data_source_registry.py`.
- `zabbix` added to `ENTERPRISE_DATASOURCES` in `backend/app/ee/license.py`.
- `frontend/public/data_sources_icons/zabbix.png`.
- `backend/tests/unit/test_zabbix_client.py` (22 tests), `zabbix` added to the
  integration suite `backend/tests/integrations/ds_clients.py`.
- `tools/zabbix/` — reproducible local environment: `docker-compose.yaml`
  (Zabbix 7.0 server + web + Postgres), `seed_zabbix.py` + `seed_problems.py`
  (sizable fleet + history/trends + problems), `build_dashboard.py`.

## Loop A — deterministic reproduction (no external services)

Mock the HTTP boundary; runs in a clean sandbox with no Zabbix server.

```bash
cd backend
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('zabbix'))"
# -> <class 'app.data_sources.clients.zabbix_client.ZabbixClient'>
uv run pytest tests/unit/test_zabbix_client.py -q
# -> 22 passed
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
# -> 11 passed
```

The unit tests assert the invariants, not one scenario: Bearer-only-on-authed
calls, `user.login` session-token round-trip, the 8-table catalog shape
(pks/fks), method dispatch + output/limit defaults, spec validation, and
JSON-RPC error surfacing.

## Loop B — live confirmation (real Zabbix 7.0.28)

```bash
cd tools/zabbix
docker compose up -d                    # Zabbix 7.0: web :8080, api /api_jsonrpc.php
python3 seed_zabbix.py                   # 20 hosts, 158 items, ~210k+109k history rows,
python3 seed_problems.py                 #   ~27k trends, 122 events, 70 problems (18 active)
# seed_zabbix.py prints API_TOKEN=<token> on its last line
```

Then, driving the real `ZabbixClient` (secrets via env only — the token below
is a throwaway from the local sandbox instance):

```
test_connection (token)    -> {'success': True, 'message': 'Connected to Zabbix API v7.0.28'}
test_connection (userpass) -> {'success': True, ...}
get_schemas()              -> 8 tables (hosts, host_groups, items, triggers,
                              problems, events, history, trends)
problems (active)          -> 19 rows; by severity {5:14, 4:4, 3:1}
history web-03 CPU (7d,5m) -> 2016 rows, value range 15.5–119.8  (incident spike)
trends  web-03 CPU (hourly)-> 168 rows, max hourly avg 107.8
```

### Full-app agentic pass (the running product)

With the stack booted (`tools/agent/boot_stack.sh`), an Anthropic LLM provider
configured, and the connection created through the catalog:

- Admin catalog shows **Zabbix** (icon) → connect form (schema-generated from
  `ZabbixConfig`) → **Test connection: "Found 8 tables"** → tables selector
  lists all 8 virtual tables (8/8 active).
- Backend log confirms the agent constructs `ZabbixClient` and queries it:
  `construct_client: Resolved ClientClass=ZabbixClient`, then
  `create_data … viz_infer got_raw=True` / `tool.data_queried`.
- Prompt *"show a bar chart of active problems per host + a risk summary"* →
  the agent runs 3 queries against Zabbix, renders a bar chart (cache-02 top at
  3 disaster-level problems; web-04/db-02/web-02 next), writes a risk summary,
  and proposes Zabbix-specific follow-ups. Screenshots under
  `media/pr/zabbix-integration-design/`.

Note: the `history` table requires `itemids` **and** the matching `history`
value-type (0 float default, 3 unsigned) — a bare `history.get` returns
nothing. This is documented in `system_prompt()` and surfaced in the schema
description.

## What this proves / regression notes

- The connector resolves via the registry, is enterprise-gated, discovers its
  fixed catalog, and executes real JSON-RPC queries against Zabbix 7.0.
- The unit suite survives as a regression test (mocked boundary, no network).
- Pre-existing unrelated issue observed while driving the app: the LLM model
  PATCH route (`PUT/PATCH /api/llm/models/{id}`) raises
  `AttributeError: 'LLMService' object has no attribute 'update_model'` —
  independent of this change (set defaults via the DB or provider-create
  instead).
