# Plan: Zabbix connector — `ZabbixClient`

## Mission
Add a new data source type `zabbix` to bagofwords that lets the agent query a
Zabbix monitoring instance (hosts, items, triggers, problems, events, metric
history) through Zabbix's JSON-RPC 2.0 API. Model it on the existing
**ServiceNow** connector, not on the SQL (MSSQL) or BI (PowerBI) connectors —
Zabbix, like ServiceNow, is an HTTP API with no SQL endpoint, where
`execute_query` takes a **JSON query spec** and `get_schemas` exposes a curated
set of virtual tables.

This follows the repo's own `.agents/skills/add-connection-type/SKILL.md`. A
connector is registry-driven: the frontend form, auth variants, and client
resolution all derive from one entry in
`backend/app/schemas/data_source_registry.py`.

## Why ServiceNow is the template (not MSSQL / PowerBI)
- **Not SQL** — Zabbix speaks JSON-RPC 2.0 at `<url>/api_jsonrpc.php`, method
  calls like `host.get`, `item.get`, `problem.get`, `history.get`. There is no
  query language to pass through, so `execute_query` takes a JSON spec (exactly
  like `servicenow_client.py`), not a SQL string.
- **No native driver** — it's plain HTTP via `requests` (already a dependency).
  So, unlike MSSQL (ODBC) or the OAuth BI connectors, there is **no Dockerfile
  change, no system library, no new Python driver**.
- **Virtual tables** — the catalog is a fixed set of "tables" backed by `*.get`
  methods, defined in code like PostHog's `POSTHOG_SCHEMAS` predefined list.

## Data model: Zabbix API methods → virtual tables

| Virtual table | Zabbix method | Notes |
|---|---|---|
| `hosts`        | `host.get`      | host id/name/status + host groups |
| `host_groups`  | `hostgroup.get` | groups |
| `items`        | `item.get`      | monitored metrics (key, value type, units) per host |
| `triggers`     | `trigger.get`   | alert definitions + severity/status |
| `problems`     | `problem.get`   | currently-active problems |
| `events`       | `event.get`     | historical alert events |
| `history`      | `history.get`   | numeric time-series values; **requires `itemids` + the correct `history` value-type (0 float, 3 uint, …)** |
| `trends`       | `trend.get`     | hourly aggregated min/avg/max |

`get_schemas()` returns these as `Table`/`TableColumn` objects with known
columns (predefined, PostHog-style). The `items` table may be enriched from a
live `item.get` so the agent sees real metric keys.

## Query spec (what `execute_query` accepts)
A JSON string that maps 1:1 to a JSON-RPC call, mirroring ServiceNow's spec:

```json
{"table": "problems",
 "filter": {"severity": [4, 5]},
 "output": ["eventid", "name", "severity", "clock"],
 "limit": 100}
```

`system_prompt()` documents the spec plus Zabbix-specific gotchas: timestamps
are Unix epoch `clock` fields (not ISO), `history` requires `itemids` and the
matching value-type, severities are integers 0–5, and results are capped at
`MAX_ROWS` with pagination via `limit`/`offset` where the method supports it.

## Authentication (the one real decision)
Zabbix auth changed across versions, so `credentials_auth` offers two variants
(same shape as ServiceNow/PostHog), each `scopes=["system", "user"]`:

- **`token` (default)** — API token sent as `Authorization: Bearer <token>`.
  Zabbix 5.4+/6.4+. Cleanest; no login round-trip.
- **`userpass`** — `user.login` with username/password returns a session token
  passed in each request's `auth` field. Works on older on-prem installs.

`test_connection()` calls `apiinfo.version` (unauthenticated) to confirm the
endpoint, then one authed `host.get` (limit 1) to confirm credentials.

## Files to create / modify (in order, per the skill)

1. **Create** `backend/app/data_sources/clients/zabbix_client.py`
   - `class ZabbixClient(DataSourceClient)`, `capabilities = {Capability.QUERY}`.
   - `__init__(url, auth_token=None, username=None, password=None, verify_ssl=True, history_window_days=7)`.
   - `connect()` — `requests.Session`; helper `_rpc(method, params)` that POSTs
     JSON-RPC to `/api_jsonrpc.php`, injects auth (Bearer header or `auth`
     field), and raises readable errors on the JSON-RPC `error` object and on
     401/403/429 (mirror ServiceNow's `_get` status handling).
   - `test_connection()`, `get_schemas()`, `get_schema()`, `execute_query()`,
     `prompt_schema()` (via `ServiceFormatter`), `system_prompt()`,
     `description`.
   - Keep calls sync — the base provides async wrappers.
2. **Edit** `backend/app/schemas/data_sources/configs.py`
   - `ZabbixConfig` — `url` (required, e.g. `https://zabbix.example.com`),
     `verify_ssl: bool = True`, `history_window_days: int = 7` (default lookback
     for `history`/`trends`). Field titles/descriptions are user-facing form
     copy.
   - `ZabbixTokenCredentials` — `api_token` (`ui:type: password`).
   - `ZabbixUserPassCredentials` — `username`, `password` (`ui:type: password`).
3. **Edit** `backend/app/schemas/data_source_registry.py`
   - Import the three schemas at the top.
   - Add a `"zabbix"` entry to `REGISTRY`:
     `config_schema=ZabbixConfig`,
     `credentials_auth=AuthOptions(default="token", by_auth={"token": ..., "userpass": ...})`,
     **explicit** `client_path="app.data_sources.clients.zabbix_client.ZabbixClient"`,
     `data_shape="tables"`, `catalog_ownership="shared"`, `ui_form="data_source"`,
     `version="beta"`. Optionally `dev_only=True` while incubating.
4. **Icon** — add `frontend/public/data_sources_icons/zabbix.png`.
   `DataSourceIcon.vue` auto-resolves `/data_sources_icons/<type>.png` from the
   type token, so **no frontend code change** is expected.
5. **Driver** — none. `requests` is already present; no `pyproject.toml` or
   `Dockerfile` change.

## Verification

Two layers — a reliable always-green mock unit test, plus a container-based
integration test so CI can exercise a real instance without stored secrets.

### 1. Registry + import resolves
```bash
cd backend
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('zabbix'))"
```

### 2. Unit test (mock the HTTP boundary — always green, no network)
`backend/tests/unit/test_zabbix_client.py`, in the style of
`tests/unit/test_druid_client.py`: mock `requests` / `_rpc` and assert
- `get_schemas()` returns the expected virtual-table shape;
- `execute_query()` dispatches the right `*.get` method with the right params
  and normalizes the JSON-RPC `result` into a DataFrame;
- `test_connection()` reports success/failure and surfaces JSON-RPC errors.

### 3. Generic data-source e2e still green
```bash
cd backend
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

### 4. Integration test against a real Zabbix (testcontainers)
Add `"zabbix"` to `DATA_SOURCES` in `backend/tests/integrations/ds_clients.py`.
Because the all-in-one `zabbix/zabbix-appliance` image is decommissioned, a real
instance is **multi-container** (zabbix-server + zabbix-web + a DB). Wire it as a
`DockerCompose`-based `CONTAINER_REGISTRY` entry (heavier than the single-image
postgres/mysql entries, but keeps the connector CI-verifiable without live
creds). Seed an API token (or use the default `Admin`/`zabbix` login) in the
compose bootstrap.

Reference: the official compose stack at
[`zabbix/zabbix-docker`](https://github.com/zabbix/zabbix-docker) — API at
`<host>/api_jsonrpc.php`, default UI login `Admin` / `zabbix`.

### 5. Live UI pass
`tools/agent/boot_stack.sh` + `seed_org.py`, then in the app: create a Zabbix
connection → **Test connection** succeeds → the tables selector lists the
virtual tables → run a prompt that queries `problems` and `history`. Capture the
connect form + tables list (**ui-evidence** skill) — the form is
schema-generated, so this doubles as the review of the config schemas.

### 6. Record the loop
Add `docs/feedback-loops/zabbix-connector.md` (**sandbox-feedback-loop** skill),
mirroring `docs/feedback-loops/servicenow-connector.md`, so the next agent can
re-verify.

## Pitfalls (from the skill + connector history)
- **Always set `client_path`.** `zabbix` → naive title-casing gives
  `ZabbixClient`, which happens to match — but the explicit path is the
  contract, and the service layer's resolver has bitten connectors before (see
  the ServiceNow root-cause in `docs/feedback-loops/servicenow-connector.md`).
- **`history.get` needs `itemids` + the matching value-type** — a bare
  `history.get` returns nothing. Document this in `system_prompt()` and default
  the lookback window from `history_window_days`.
- **Auth token vs session token** — the Bearer header (tokens) and the `auth`
  request field (`user.login`) are different mechanisms; pick per auth variant.
- **`is_connection`** — leave it unset (True). It's a real data source, not an
  MCP-style tool provider; setting it False would make schema indexing skip the
  type.
- Registry `description` and config field descriptions are product copy — write
  them for end users, not as code comments.

## Scope summary
~2 files new (client, unit test) + config/registry/icon edits and a compose
testcontainer entry. No SQL, no native driver, no Dockerfile change, no
frontend-form work.
