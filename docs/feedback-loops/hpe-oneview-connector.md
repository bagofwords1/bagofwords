# Feedback loop: HPE OneView connector — simulator, tests and full sandbox pass

Date: 2026-11-07 · Branch: `ai/loving-brahmagupta-d7snv9`
Evidence: `media/pr/hpe-oneview-connector/`

## What was built

- `backend/app/data_sources/clients/hpe_oneview_client.py` — `HpeOneViewClient`
  (OneView / Synergy Composer REST; catalog reference OneView 9.00, API 6800).
  Session per operation: `GET /rest/version` (no auth) negotiates
  `X-API-Version` (clamped to the catalog range 1200–7200), `POST
  /rest/login-sessions {"userName","password","authLoginDomain","loginMsgAck"}`
  → `sessionID` sent as the literal `Auth` header, `DELETE /rest/login-sessions`
  in `finally`. Paging `start/count` (never `count=-1`) following `nextPageUri`
  with origin/path/scope checks; `filter="field='v'"` terms, `sort`, `fields`,
  utilization windows and index queries; a single `_request` primitive that
  allows only catalogued GETs plus the login POST/DELETE.
- `backend/app/data_sources/clients/hpe_oneview/catalog.json` — 32 tables:
  `appliance_version`, `appliance_health`, `server_hardware`,
  `server_hardware_firmware`, `server_hardware_utilization`,
  `server_hardware_types`, `server_profiles`, `server_profile_templates`,
  `enclosures`, `enclosure_groups`, `logical_enclosures`, `interconnects`,
  `interconnect_ports`, `logical_interconnects`, `sas_interconnects`,
  `uplink_sets`, `ethernet_networks`, `fc_networks`, `fcoe_networks`,
  `network_sets`, `racks`, `datacenters`, `power_devices`, `alerts`, `events`,
  `tasks`, `firmware_drivers`, `storage_systems`, `storage_pools`,
  `storage_volumes`, `scopes`, `index_resources`. Fields not backed by an SDK
  fixture carry `"inferred": true`.
- `HpeOneViewConfig` (`auth_login_domain`, optional `api_version`) /
  `HpeOneViewCredentials` in `configs.py`; `REGISTRY["hpe_oneview"]`
  (`infra`, `beta`, explicit `client_path`); icon `hpe_oneview.png`.
- `tools/hpe_oneview/simulated_api.py` — loopback simulator for a Synergy
  estate (3 frames, 24 SY480 servers with mixed status, 20 profiles — 3
  NonCompliant —, 6 interconnects with ports — one Critical —, 2 SPP
  baselines, 80 alerts, 40 tasks — 3 Error —, networks, storage, hourly
  utilization samples). Enforces `X-API-Version` (412
  `UNSUPPORTED_API_VERSION`), `Auth` (401 `AUTHN_AUTH_TOKEN_INVALID`), 400
  `AUTHN_AUTH_FAIL` on bad credentials, 404 `RESOURCE_NOT_FOUND`, 405 with the
  real appliance's body on any mutating verb, and the documented
  `members/start/count/total/nextPageUri/prevPageUri/uri` envelope.
  `tools/hpe_oneview/SOURCES.md` lists every reference (HPE Python/Go SDK
  sources and fixtures, Ansible fixtures, the Zabbix Synergy template, HPE
  Developer articles) and the inferred shapes.
- Tests: `backend/tests/unit/test_hpe_oneview_client.py` (121 collected incl.
  one real-HTTP run against the in-process simulator),
  `backend/tests/e2e/test_hpe_oneview.py`, `hpe_oneview` remote-mode entry in
  `tests/integrations/ds_clients.py`.

## Loop A — deterministic

```bash
cd backend && export TESTING=true BOW_DATABASE_URL=sqlite:///db/ov-tests.db
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('hpe_oneview'))"
uv run pytest tests/unit/test_hpe_oneview_client.py -q --db=sqlite
uv run pytest tests/e2e/test_hpe_oneview.py tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

Observed: all unit tests passed; connector e2e + 13 generic tests passed.
Covered: version negotiation and clamping, pinned version outside
`[minimumVersion, currentVersion]` fails before login, `Auth` header and
guaranteed logout, continuation safety (`nextPageUri` must stay on the
resource and advance), `total > limit` fails before fetching, 401/403/404/412
codes without body echo, invalid queries make zero requests, mutation
impossible, secret-like keys stripped, utilization sample flattening.

## Loop B — real HTTP against the simulator

```bash
export ONEVIEW_USERNAME=reader ONEVIEW_PASSWORD=local-test-pass
python3 tools/hpe_oneview/simulated_api.py --port 18095
cd backend && ONEVIEW_TEST_URL=http://127.0.0.1:18095 ONEVIEW_TEST_USERNAME=reader ONEVIEW_TEST_PASSWORD=local-test-pass \
  ONEVIEW_TEST_ALLOW_HTTP=true uv run pytest tests/integrations/ds_clients.py -k hpe_oneview -q --db=sqlite
```

Observed: `test_connection` → "Connected to HPE Synergy Composer2
9.00.00-0417420 (X-API-Version 6800, catalog reference 6800). 32 read-only
tables available."; integration harness **passed**.

## Loop C — sandbox UI with Claude Haiku 5.5

| Step | Observed |
|---|---|
| Catalog tile | HPE OneView with the HPE logo (`01-catalog-tile.png`) |
| Connect | "Connection saved · Schema discovery · Discovered 32 tables" (`03-connection-tested.png`) |
| Tables | 32/32 selected (`04-tables-selected.png`) |
| Agent | generated overview with key tables (`05-agent.png`) |

Prompt: *"We have a Synergy frame with degraded compute. Investigate in HPE
OneView: which server hardware is Critical or Warning, what active alerts are
attached to them, whether their server profiles are non-compliant, whether any
interconnect is unhealthy, and which recent tasks ended in Error."*

Haiku 5.5 queried `server_hardware` (status Critical/Warning → 5 servers,
shown as a live table), then four sub-agents read `alerts` (Active alerts on
those URIs: two `ProcessorStatus … thermal fault, throttled` Critical alerts
on Frame-2 bay 3 and Frame-3 bay 3, a `PowerSupplyRedundancyLost` and a DIMM
correctable-error warning), `server_profiles` + `server_profile_templates`
(esx-03, esx-11, rhel-14 NonCompliant against SPT-ESXi-Prod / SPT-RHEL-Prod),
and `interconnects` (one Critical module in the affected frames). Its summary
named the shared thermal fault as the strongest signal and listed what it
could not finish (`06-rca-report.png`, `07-rca-report-full.png`).

### What the live run exposed (fixed in this branch)

1. **Construction failure from the stored config.** The connect form stores
   an empty optional field as `""`; the client rejected `api_version: ""`
   while the wizard's Test connection had passed. The periodic health probe
   then deactivated the connection and pruned its tables, so the first chat
   saw "no OneView source" and the generated overview said the schema had no
   tables. Empty/numeric-string `api_version` now means "negotiate"; the
   agent was re-probed through the real API, its schema refreshed and the
   overview regenerated before the run above.
2. **Filters on non-indexed columns.** Sub-agents filtered
   `server_profile_templates` by `uri` and `interconnect_ports` by
   `interconnectUri`; OneView only accepts `filter=` on indexed attributes, so
   those were rejected. Any other catalogued column is now filtered
   client-side after the complete fetch with the same `op/value` vocabulary.
3. Open-ended time windows (`start_time` without `end_time`, or a future
   `end_time`) now mean "up to now", and provenance columns may be listed in
   `fields` (shared with the other connectors).

Each has a regression test. The remaining transcript failures
(`SyntaxError` in generated code, `create_data` outside the sub-agent profile,
a `KeyError` on the client key) are platform-side, not connector logic.

## Faithfulness limits (see `tools/hpe_oneview/SOURCES.md`)

HPE's REST API Reference is a JavaScript application whose content could not
be fetched, so field names come from HPE's SDK sources/fixtures; health-status
member fields, the failed-login status, `sort` direction tokens, utilization
`metricSamples` layout and several rack/datacenter/power-device fields are
inferred and flagged. Synergy Composer and plain OneView appliances share the
API but differ in data; a read-only account on a real appliance is still
required before GA.
