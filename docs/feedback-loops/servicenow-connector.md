# Feedback Loop — ServiceNow connector (new)

Validates the new ServiceNow data source connector end-to-end: schema-driven
admin form → connection → live schema discovery → agent table selection →
LLM prompts building charts/dashboards from real ServiceNow (Table API) data.
Design doc: `documents/servicenow_connector_plan.md`.

## What was added

- `backend/app/data_sources/clients/servicenow_client.py` — REST Table API
  client. JSON query spec → encoded query; bulk metadata discovery via
  `sys_db_object` + `sys_dictionary` (inheritance resolved in memory,
  reference fields → fks); pagination; `{link, value}` normalization;
  explicit detection of the silent metadata-ACL failure (HTTP 200 + empty
  `sys_dictionary` result).
- `ServiceNowCredentials` / `ServiceNowConfig` in
  `backend/app/schemas/data_sources/configs.py`; registry entry (type
  `servicenow`, explicit `client_path`) in
  `backend/app/schemas/data_source_registry.py`.
- `frontend/public/data_sources_icons/servicenow.png`.
- Fix: `ConnectionService._resolve_client_by_type` now resolves through
  `resolve_client_class()` so a registry `client_path` wins over the naming
  convention (`servicenow` would otherwise derive `ServicenowClient` and
  fail). See "Root cause" below.

## Root cause (validated)

Creating a servicenow connection through the API failed with
`Unable to load client for servicenow: module ... has no attribute
'ServicenowClient'` while `Connection.get_client()` worked. The service layer
had a *second*, duplicated resolver
(`connection_service.py::_resolve_client_by_type`) that re-derived the class
name from the type string and ignored the registry's explicit `client_path`
contract (`data_source_registry.py:1220` documents that explicit path is the
contract). Any connector whose class name isn't a naive title-casing of its
type would hit this. Fixed by delegating to `resolve_client_class()`.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/unit/test_servicenow_client.py -q
```

Observed: `19 passed`. HTTP is faked at the `requests.Session` boundary and
served from fixtures captured from a real developer instance
(`tests/unit/fixtures/servicenow/`, hostname scrubbed), so the ServiceNow
payload quirks are real: reference values as `{link, value}` objects,
dot-walked keys (`super_class.name`), dictionary rows split across the table
hierarchy. Covers: connection test (success/401/silent-ACL), discovery
(inheritance, fks, `tables` override, `discover_all` filtering), query
(params, pagination, row cap, dict specs, malformed specs, HTTP errors).

Before the connection-service fix, this bug reproduced with:

```bash
uv run python -c "
from app.services.connection_service import ConnectionService
ConnectionService()._resolve_client_by_type('servicenow', {'instance_url': 'https://x'}, {'username': 'u', 'password': 'p'})"
# AttributeError: ... no attribute 'ServicenowClient'   (now: constructs ServiceNowClient)
```

## Loop B — live confirmation (real credentials)

Requires a ServiceNow instance (free PDI from developer.servicenow.com).
Secrets via env vars only: `SERVICENOW_INSTANCE_URL`, `SERVICENOW_USERNAME`,
`SERVICENOW_PASSWORD`.

```bash
cd backend
uv run python - <<'EOF'
import os, json
from app.schemas.data_source_registry import resolve_client_class
client = resolve_client_class("servicenow")(
    instance_url=os.environ["SERVICENOW_INSTANCE_URL"],
    username=os.environ["SERVICENOW_USERNAME"],
    password=os.environ["SERVICENOW_PASSWORD"])
print(client.test_connection())
print([(t.name, len(t.columns), len(t.fks)) for t in client.get_schemas()])
print(client.execute_query(json.dumps({
    "table": "incident", "query": "active=true^ORDERBYDESCopened_at",
    "fields": ["number", "short_description", "priority", "state"], "limit": 5})))
EOF
```

Observed against a Yokohama PDI (2026-07): `{'success': True, ...}`; 11
curated tables with inherited fields (incident 91 cols / 20 fks, including
task's fields) ; a 5×4 DataFrame of real incidents with display values
("1 - Critical", "In Progress").

Full-stack UI loop (what the screenshots show): `tools/agent/boot_stack.sh`,
seed org + Anthropic provider, then in the browser — ServiceNow tile in the
Add Connection catalog → schema-driven form → "Connected successfully. Found
11 tables." → live indexing progress → agent wizard with 5 tables selected →
report prompts against live PDI data. Observed end-state: "Show open
incidents by priority as a bar chart" produced a populated bar chart
(Critical 17 / Planning 9 / Moderate 7 / High 4 / Low 3, total 40) with a
correct narrative summary; the dashboard prompt produced an incidents master
table (67 rows, display values: "1 - Critical", "In Progress", assignee
names) and the agent self-captured a knowledge instruction ("ServiceNow
Incident State and Priority Mappings") from what it learned probing the
schema.

The first live run exposed a prompt-quality gap, fixed in the client: with
display values on, the coder generated `pd.to_numeric(df["priority"])` on
"1 - Critical" strings and coerced every row to NaN (widget showed 0 rows;
the agent noticed and pulled a raw sample to investigate). The client's
`system_prompt` now spells out the display-value shapes and that
encoded-query *filters* still use raw codes — the rerun produced correct
charts on the first attempt.

## What this proves / regression notes

- The connector round-trips the full product path (form → registry →
  connection → indexing → agent tools → charts) with zero bespoke frontend
  code, confirming the registry-driven design.
- `POST /api/llm/models` is broken independently of this change
  (`LLMService` has no `create_model`; route `routes/llm.py:130` calls it) —
  hit while seeding the LLM provider, worked around by creating models
  inline with the provider. Reproduces on main with this branch stashed.
- When `BOW_ENCRYPTION_KEY` is unset, `bow_config.py` generates a random
  Fernet key **per process** — every backend restart orphans all stored
  credentials (`InvalidToken` on decrypt, surfacing as "An error occurred"
  in chat with no user-visible cause). Bit us twice in this loop; dev/agent
  environments should pin the key (docs/boot script candidate).
- The generated dashboard artifact failed to render ("React is not
  defined") and `edit_artifact` failed with a missing
  `frontend/.output/public/libs/tailwindcss-3.4.16.js`. Root cause: the
  sandbox stack was booted without running
  `scripts/download-vendor-libs.sh`, and artifacts embed those libs
  server-side at creation time — so artifacts created while the libs are
  missing are permanently broken and must be recreated after provisioning.
  Unrelated to the connector (its widgets/queries had data). Note the
  script's default output path is CWD-relative (`frontend/public/libs`),
  so run it from the repo root; boot_stack.sh is a candidate home for it.
