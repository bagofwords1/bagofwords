# Feedback loop: Dell OpenManage Enterprise connector — simulator, tests and full sandbox pass

Date: 2026-11-07 · Branch: `ai/loving-brahmagupta-d7snv9`
Evidence: `media/pr/dell-ome-connector/`

## What was built

- `backend/app/data_sources/clients/dell_ome_client.py` — `DellOmeClient`
  (OME 3.x/4.x REST, OData-flavoured; not Redfish). Session per operation:
  `POST /api/SessionService/Sessions {"UserName","Password","SessionType":"API"}`
  → `X-Auth-Token` response header, `DELETE /api/SessionService/Sessions('<Id>')`
  in `finally` so the appliance's session cap (default 6) is never exhausted.
  `$top/$skip/$filter/$orderby`, `@odata.count`, bounded `@odata.nextLink`
  following with scope re-applied on every continuation.
- `backend/app/data_sources/clients/dell_ome/catalog.json` — frozen allow-list
  of 27 tables: `appliance_info`, `appliance_network`, `devices`,
  `device_summary`, `device_types`, `device_inventory*`, `device_hardware_logs*`,
  `device_power*`, `device_temperature*`, `device_system_uptime*`, `alerts`,
  `alert_categories`, `warranties`, `firmware_baselines`,
  `firmware_compliance_reports*`, `firmware_catalogs`, `jobs`,
  `job_execution_histories*`, `job_execution_details*`, `groups`,
  `group_devices*`, `group_all_leaf_devices*`, `audit_logs`, `templates`,
  `configuration_baselines`, `configuration_compliance_reports*`,
  `management_domains` (`*` = parent-scoped).
- `DellOmeConfig` / `DellOmeCredentials` in `configs.py`; `REGISTRY["dell_ome"]`
  (`infra`, `beta`, explicit `client_path`); icon `dell_ome.png`.
- `tools/dell_ome/simulated_api.py` — loopback OME 4.1.0 simulator: 201 +
  `X-Auth-Token` + `Location` on login, `Base.1.0.GeneralError` /
  `@Message.ExtendedInfo` error bodies (`CGEN1001`, `CUSR1001` session cap),
  `$filter` `eq`/`contains`/`and`, `$orderby`, `@odata.nextLink`, 405 on any
  non-session POST/PUT/PATCH/DELETE, 403 on `/Actions/`. Estate: ~40 devices
  (R650/R750/MX750c, MX7000 chassis, MX9116n switches) with mixed
  Status 1000/3000/4000, ~150 alerts over 7 days, warranties (expired and
  <30 days), 2 firmware baselines with CRITICAL compliance, 30 jobs incl.
  failed firmware updates, audit log. `tools/dell_ome/SOURCES.md` grades every
  table verified / partial / inferred (Dell's Ansible `module_utils/ome.py`,
  OpenManage-Enterprise sample scripts, PowerShell module, community posts).
- Tests: `backend/tests/unit/test_dell_ome_client.py` (84 tests incl. one
  against the in-process simulator), `backend/tests/e2e/test_dell_ome.py`,
  `dell_ome` remote-mode entry in `tests/integrations/ds_clients.py`.

Corrections to the brief discovered during research: configuration baselines
live under `/api/TemplateService/Baselines` (not `ConfigurationService`);
alerts carry `Message`, not `AlertMessage`; jobs do not support `$orderby`;
warranties do not support date comparisons in `$filter` — all enforced by the
client before I/O.

## Loop A — deterministic

```bash
cd backend && export TESTING=true BOW_DATABASE_URL=sqlite:///db/ome-tests.db
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('dell_ome'))"
uv run pytest tests/unit/test_dell_ome_client.py -q --db=sqlite
uv run pytest tests/e2e/test_dell_ome.py tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

Observed: **84 unit tests passed**, connector e2e + 13 generic tests passed.
Covered: session handshake and guaranteed logout, `CUSR1001` → `SessionLimitReached`,
paging continuation safety, `@odata.count` above `limit` fails before fetching,
401/403/404 codes without body leakage, invalid/unsupported `$filter` and
`$orderby` rejected before I/O, mutation impossible, secret and `@odata.*`
keys sanitised, per-component explosion of compliance reports.

A latent test defect was found when all connector suites ran in one pytest
session: two suites imported their simulators as the bare module
`simulated_api`, so the second one received the first one's module. Fixed by
loading each simulator under a unique module name.

## Loop B — real HTTP against the simulator

```bash
export OME_USERNAME=svc-bow OME_PASSWORD=synthetic-only
python3 tools/dell_ome/simulated_api.py --port 18093
cd backend && OME_TEST_URL=http://127.0.0.1:18093 OME_TEST_USERNAME=svc-bow OME_TEST_PASSWORD=synthetic-only \
  OME_TEST_ALLOW_HTTP=true uv run pytest tests/integrations/ds_clients.py -k dell_ome -q --db=sqlite
```

Observed: `test_connection` → "Connected to OpenManage Enterprise 4.1.0; 27
read-only tables available."; integration harness **passed**. curl shows the
documented handshake (201 + `X-Auth-Token`, 401 bodies, 7th session → 400
`CUSR1001`, `DELETE Sessions('<Id>')` → 204 and the token is dead afterwards).

## Loop C — sandbox UI with Claude Haiku 5.5

Same stack and Playwright driver as the F5 loop.

| Step | Observed |
|---|---|
| Catalog tile | Dell OpenManage Enterprise with the Dell logo (`01-catalog-tile.png`) |
| Connect | "Connection saved · Schema discovery · Discovered 27 tables · 0s" (`03-connection-tested.png`) |
| Tables | 27/27 selected (`04-tables-selected.png`) |
| Agent | generated overview, 27 tables (`05-agent.png`) |

Prompt: *"Several PowerEdge servers in DC-East went critical overnight.
Investigate with OpenManage Enterprise: which devices are in critical or
warning status, what critical alerts fired for them in the last 7 days, whether
any failed firmware/update jobs are related, and whether any of those servers
have expired warranties."*

Haiku 5.5 queried `devices` (filter `Status ∈ {4000, 3000}` → 13 rows, shown as
a live table in the report), delegated three sub-agents over `alerts`
(`SeverityType = 16`, 7-day window → 44 alerts; shared `PSU0003`, `FAN0001`
and CPU `IERR` messages), `jobs` (`LastRunStatus` 2070/2090 → 10 failed or
warning jobs incl. "Firmware update: Prod DB BIOS"), `firmware_baselines`
(both CRITICAL) and `warranties` (srv-02, srv-08, mx-34 expired; five more
expiring within 30 days), and concluded probable hardware faults rather than
firmware jobs — which is the planted story (`06-rca-report.png`,
`07-rca-report-full.png`). It also correctly rejected the "overnight" premise
from the alert timestamps.

The same planner-usability fixes as the F5 loop apply here (provenance columns
accepted in `fields`; explanatory `ResultLimitExceeded`), each with a
regression test.

## Faithfulness limits (see `tools/dell_ome/SOURCES.md`)

Dell's PDF API guides were unreachable from the build environment; shapes come
from Dell's open-source clients and community posts. `device_summary`,
`device_hardware_logs`, `device_power`, `device_temperature` and
`device_system_uptime` field names are **inferred**; `warranties`, `groups`,
`configuration_baselines` and `appliance_network` are partial. The client keys
error handling off HTTP status plus `CUSR1001` only. A read-only Viewer account
on a real OME is still required before GA.
