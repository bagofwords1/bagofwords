# Feedback loop: VMware vCenter connector — mock, tests and full sandbox pass

Date: 2026-11-05 · Branch: `claude/vcenter-connector`
Evidence: `media/pr/vcenter-connector/`

## What was built

- `backend/app/data_sources/clients/vcenter_client.py` — `VCenterClient`.
  - Talks to the vSphere Web Services API (SOAP `vim25`) through pyVmomi
    9.1.1, plus the Automation REST API for tags.
  - One login per operation, always followed by Logout.
  - Uses ContainerView with explicit property paths, paged through
    RetrievePropertiesEx and Continue.
  - Perf queries are batched under `vpxd.stats.maxQueryMetrics`, halving the
    batch on a `querySpec.size` fault.
  - Events and tasks are read through rewound history collectors that are
    destroyed afterwards.
  - 20 virtual tables with column descriptions: about, datacenters, clusters,
    hosts, vms, vm_disks, vm_nics, vm_partitions, vm_snapshots, datastores,
    networks, resource_pools, tags, alarms, events, tasks, licenses,
    perf_counters, metrics_available, metrics.
  - Queries are a JSON spec (see `system_prompt()`).
- `VCenterConfig` and `VCenterUserPassCredentials` in
  `backend/app/schemas/data_sources/configs.py`.
- `REGISTRY["vcenter"]` (`category="infra"`, `beta`, enterprise-gated,
  explicit `client_path`).
- Icon `frontend/public/data_sources_icons/vcenter.svg`, mapped in
  `DataSourceIcon.vue`.
- Dependency `pyvmomi>=9.1.1,<10` (pure Python, no Dockerfile change).
- `tools/vcenter/mock_vcenter.py` (+ `docker-compose.yaml`), the mock vCenter
  8.0 U3:
  - Serves the real vim25 wire over HTTPS with a private "VMCA" CA. Requests
    are decoded and responses encoded with **pyVmomi's own SOAP serializer and
    type system**, so every property is a real `vim.*` object and a wrong field
    name fails at seed time.
  - Also serves the REST tagging API and `GET /mock/state` (open sessions,
    call counts).
  - Modes: `VCENTER_MOCK_MODE=esxi`, `VCENTER_MOCK_API_VERSION=7.0.3.0`,
    `VCENTER_MOCK_EXTRA_VMS=N`.
- Tests: `backend/tests/unit/test_vcenter_client.py` (36 tests against the mock
  over real HTTPS) and a `vcenter` remote-mode entry in
  `backend/tests/integrations/ds_clients.py`.
- Loop drivers:
  - `tools/agent/vcenter_ui_flow.mjs` (Playwright connect + chat)
  - `tools/agent/vcenter_report_shots.mjs` (expanded step screenshots)
  - `tools/agent/stub_llm_vcenter.py` (scripted OpenAI-compatible LLM)

## The mock's faithfulness rules (each exercised by a test)

| vCenter behaviour | Mock | Pinned by |
|---|---|---|
| `vmware_soap_session` cookie. No session gives `NotAuthenticated`; a bad password gives `InvalidLogin` | yes | `test_bad_password_fails_cleanly` |
| Session cap per user (550) | yes | every test asserts zero leaked sessions, views and collectors |
| RetrievePropertiesEx pages at the server's limit (500, test lowers it to 4) and the client follows `token` | yes | `test_inventory_is_complete_across_property_collector_pages` |
| Unknown property path gives `InvalidProperty` | yes | implicitly by every inventory query |
| Realtime 20 s only for VMs/hosts and only for the last hour; historical intervals at stats level 1 | yes | `test_auto_interval_*`, `test_counters_above_the_statistics_level_*`, `test_per_instance_realtime_metrics` |
| >256 historical metrics in one QueryPerf gives `InvalidArgument querySpec.size` | yes | `test_historical_queries_are_batched_under_max_query_metrics` |
| ReadNext* is capped at 1000, oldest first | yes | `test_events_are_complete_beyond_one_collector_page_and_newest_first` (1,363 events) |
| `triggeredAlarmState` propagates to ancestors | yes | `test_every_triggered_alarm_is_listed_once_with_names_resolved` |
| Version negotiation via `vimServiceVersions.xml` | yes | `test_older_vim25_api_versions_negotiate[7.0.3.0, 8.0.3.0]` |
| Standalone ESXi: no `/api`, no clusters, realtime only | yes | `test_standalone_esxi_degrades_without_failing` |
| pyVmomi 8+ reads `mo.prop` through the generic `Fetch` method | yes | smoke script |

## Loop A — deterministic (no LLM, no appliance)

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('vcenter'))"
uv run pytest tests/unit/test_vcenter_client.py -q             # 36 passed (~45 s)
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q -n 4   # 13 passed
# integration harness against the mock as a separate process
(cd ../tools/vcenter && VCENTER_MOCK_PORT=8989 ../../backend/.venv/bin/python mock_vcenter.py &)
VCENTER_TEST_HOST=localhost VCENTER_TEST_PORT=8989 VCENTER_TEST_USERNAME=bow-ro@vsphere.local \
VCENTER_TEST_PASSWORD='VMware1!RO' VCENTER_TEST_CA_PEM="$(cat ../tools/vcenter/.certs/ca.pem)" \
  uv run pytest tests/integrations/ds_clients.py -k vcenter -v           # PASSED
```

**Mutation checks** (rule 6 of `backend/tests/AGENTS.md`). Each mutant was
applied to the client and its test went red:

- Logout removed
- token paging removed
- percent scaling removed
- alarm names dropped
- alarm states dropped
- collector stopped after one page
- task error rendered as a fault repr
- maxQueryMetrics halving removed
- `_plain` cell conversion removed (pickle regression)

**Scale:** with `VCENTER_MOCK_EXTRA_VMS=5000` (5,019 VMs), `vms` returns all
5,019 rows in about 8 s through 50 paged `ContinueRetrievePropertiesEx`
calls. A 500-VM × 2-counter × 12 h metrics query returns 144,000 rows in about
5 s. Afterwards there are 0 sessions, views or collectors open.

**Pre-existing, unrelated failure:**
`test_discovery_progress_contract.py::test_every_registered_catalog_can_cancel_before_external_io`
fails in this sandbox because of `libodbc.so.2` (another connector's driver).
It fails identically with this change stashed, and `vcenter` itself cancels
before any I/O.

## Loop B — the running product

Stack:
- `tools/agent/boot_stack.sh` with `BOW_LICENSE_KEY` (the connector is
  enterprise-gated) and a fixed `BOW_ENCRYPTION_KEY`
- `seed_org.py`
- the mock on :8989, started from `tools/vcenter`
- `stub_llm_vcenter.py` on :9099, registered with `setup_stub_llm.py`

1. **Connect wizard** (`vcenter_ui_flow.mjs connect`, screenshots `00`–`09`):
   - `/agents/new`, then the catalog, search "vcenter", *Infrastructure*, and
     the **VMware vCenter** tile.
   - The schema-generated form appears.
   - The flow fills host `localhost`, port `8989`, **Verify SSL left ON**, the
     mock CA pasted into *CA certificate*, and the user and password.
   - **Connect** shows "Connection saved · Schema discovery ✓ · Discovered 20
     tables · 1s".
   - **Select tables** shows 20/20 active with the FK links.
2. **RCA chat** with the prompt "prod-db-01 has been slow for the last hour.
   Find the root cause in vCenter." (`10`–`14`):
   - The agent ran six `create_data` steps: vms, metrics, datastores, events,
     tasks and alarms. All were green and the code executed in the sandbox.
   - Mock `/mock/state` afterwards:
     - Login 6 / Logout 6, CreateContainerView 8 / DestroyView 8
     - QueryPerf 1
     - event and task collectors created, rewound, read and destroyed
     - **0 sessions open**
   - `14-step-2.png`: disk latency, last hour. VMs on ds-prod-01 average
     52–55 ms with a max of 102–103 ms; the control VM prod-db-02 averages
     4.3 ms with a max of 5 ms.
   - `14-step-5.png`: tasks on backup-proxy-01. Three snapshot creations
     (21, 14 and 7 days ago), the failed RemoveSnapshot with its file-lock
     error, and the consolidation now running.
   - The final prose is the stub's scripted text and is labelled as such. The
     tables are the live results.

### Bugs this loop found and fixed (the unit suite was green before each)

- **pyVmomi enum values in DataFrames.**
  - `vim.TaskInfo.State`, `vim.ManagedEntity.Status` and
    `DrsConfigInfo.DrsBehavior` are lazy `str` subclasses that cannot be
    pickled.
  - BOW pickles results out of the code-execution sandbox, so `tasks` and
    `alarms` failed in the product with "Can't pickle …".
  - Fix: `_plain()` turns every object cell into a builtin type.
  - Regression: `test_every_table_returns_its_declared_columns_and_rows` now
    pickles each table and rejects pyVmomi types. It fails without the fix.
- **Default `limit` silently truncated inventory.** 5,000 of 5,019 VMs came
  back while the connection test reported 5,019. Results are now complete by
  default; a too-large metrics query raises "narrow the scope" instead.
- **`RetrieveServiceContent` on every helper call.** pyVmomi makes a round
  trip each time; it is now cached per session.
- **Task errors rendered as a fault repr.** pyVmomi models
  `TaskInfo.error` as the fault itself with its text in `msg`.
- Mock fixes found by the tests:
  - `priorVersions` advertised newer APIs, so 7.0.3 negotiated 8.0.2.
  - Empty arrays must be an empty `<xResponse/>`.
  - Python 3.13 strict X.509 needs an AKI on the leaf certificate.
  - The certificate must persist across restarts.

## Gotchas for the next agent

- **The mock pins its clock at start.** Restart it right before a chat run so
  "the last hour" contains the incident. Stop it with `fuser -k 8989/tcp`.
- **Set `BOW_ENCRYPTION_KEY` before booting.** A backend restart otherwise
  mints a new key, and every saved connection and LLM provider credential
  becomes undecryptable ("Agent failed:").
- **`restart_backend.sh` can leave the old uvicorn in graceful shutdown.**
  The port stays open but unresponsive. Kill the old processes with `-9`.
- **The stub must send the data source id, not the connection id.** The
  planner prompt nests `<connection type="vcenter" id=…>` inside
  `<data_source id=…>`.
- Not verified here:
  - **A real vCenter.** None is available (appliance OVA + Broadcom
    entitlement).
  - **A real-LLM run.** The sandbox's Anthropic key had no credit. With a
    funded key, run `setup_haiku_llm.py` and repeat step 2 of Loop B.
  - The first thing to do against a customer vCenter: Test connection, the
    schema index, then the RCA prompt on a known past incident.
