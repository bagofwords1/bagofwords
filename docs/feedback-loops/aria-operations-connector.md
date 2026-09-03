# Feedback loop: VMware Aria Operations connector — full sandbox pass (real LLM + Playwright)

Date: 2026-09-03 · Branch: `claude/rca-vmware-aria-connectors-t4udpe`
Research/design doc: `docs/vmware-aria-storage-connectors-analysis.md` (§6e is the build plan)
Evidence: `media/pr/aria-operations-connector/`

## What was built

- `backend/app/data_sources/clients/aria_operations_client.py` — Suite API
  client (`/suite-api/api`): token acquire with `authSource`,
  `Authorization: OpsToken`, `Accept: application/json` on every call, 6-hour
  token cache with one re-acquire on 401, page/pageSize pagination,
  1000-id chunking. 14 fixed virtual tables + discovered wide
  `metrics::<AdapterKind>/<ResourceKind>` tables (stat keys as columns).
  JSON query spec documented in `system_prompt()`.
- `AriaOperationsConfig` / `AriaOperationsUserPassCredentials` in
  `backend/app/schemas/data_sources/configs.py`; `REGISTRY["aria_operations"]`
  (`category="infra"`, `version="beta"`, `requires_license="enterprise"`,
  explicit `client_path`); `aria_operations` added to `ENTERPRISE_DATASOURCES`
  in `backend/app/ee/license.py`.
- Icon `frontend/public/data_sources_icons/aria_operations.png` (resolved by
  the default `<type>.png` rule in `DataSourceIcon.vue`).
- `tools/aria_operations/mock_suite_api.py` + `docker-compose.yaml` — spec-shaped
  mock (official OpenAPI: `vmware/vcf-api-specs`), seeded vSphere + Hitachi
  storage-pack estate and ONE deterministic incident (`batch-etl-01` saturates
  `Pool-07` → `ds_prod_db_01` → `prod-db-01`), with dynamic-threshold bands.
- `backend/tests/unit/test_aria_operations_client.py` (31 tests, `requests`
  boundary mocked); `aria_operations` remote-mode entry in
  `backend/tests/integrations/ds_clients.py`.
- Drivers: `tools/agent/aria_sandbox_setup.py` (API: create agent, wait for
  indexing, activate tables) and `tools/agent/aria_ui_flow.mjs` (Playwright:
  connect wizard + chat with screenshots).

## Loop A — deterministic (no LLM, no appliance)

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
# 1. registry resolves through the explicit client_path
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('aria_operations'))"
# 2. unit tests (token/authSource, OpsToken header, refresh-on-401, pagination,
#    catalog + discovered tables, stat flattening + dt bands, chunking at 1000,
#    relationships BFS direction, alerts window, wide pivot, top-N, validation)
uv run pytest tests/unit/test_aria_operations_client.py -q        # 31 passed
# 3. generic data-source e2e suites still green
uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q   # 11 passed
# 4. the mock + the real client, end to end
(cd ../tools/aria_operations && uv run --project ../../backend uvicorn mock_suite_api:app --port 8443 &)
uv run pytest tests/integrations/ds_clients.py -k aria_operations -v   # with integrations.json pointed at :8443
```

Observed with the mock (client → mock, no LLM): `test_connection` →
"Connected to Aria Operations 8.18.0: 3 adapter kinds visible (VMWARE,
HitachiStorage, VMWARE_ARIA_OPERATIONS)"; `get_schemas` → **24 tables**
(14 fixed + 10 discovered `metrics::` tables for the populated kinds, none
for zero-count kinds, adapter instances, or the self-monitoring adapter);
`relationships` depth 3 from `prod-db-01` → 13 edges incl.
`ds_prod_db_01 → 00:10:A0` and `Pool-07 → 00:10:A0`; `metrics` with
`dt: true` over the incident window returns `dt_min`/`dt_max` and the three
layers' spikes (pool 1.5→15.4 ms, datastore 2.9→27.1 ms, VM 3.8→33.4 ms);
`alerts` in the window → 7 rows in `startTimeUTC` order, batch VM first;
`metrics_topn` ranks `batch-etl-01` first by IOPS; bogus token → one
re-acquire, query succeeds.

Faithfulness rules the mock enforces (each pinned by a unit test on the
client side): missing/expired `OpsToken` → 401; no `Accept: application/json`
(python-requests' default `*/*` included) → XML; `pageSize` > 1000 → 400;
`stats/latest` > 1000 ids → 400.

## Loop B — the running product (real LLM)

Stack: `tools/agent/boot_stack.sh` (backend :8000 sqlite, frontend :3000
production build) + mock on :8443 + `seed_org.py` + `setup_haiku_llm.py`
(Anthropic key from `ANTHROPIC_API_KEY`, never on disk) + enterprise license
from `BOW_LICENSE_KEY`.

1. **Connect wizard (schema-generated form)** — `/agents/new` → catalog
   modal, *Infrastructure* chip → **VMware Aria Operations** tile (logo) →
   form with Aria Operations URL, Auth Source (`LOCAL`), Verify SSL, CA
   Bundle Path, History Window, Max Discovered Metric Tables, Username /
   Password → *Test connection* → **"Connected successfully. Found 24
   tables."** → *Save and Continue* → discovery modal **"Connected ·
   Discovered 24 tables · 0s"** → *Connect* → agent name → *Save & Continue*
   → **Select Tables** lists all 24 (fixed + `metrics::` tables) → *Select
   all* → "Tables updated" → Set Context (overview instruction generated by
   the LLM). Screenshots `01`–`09`.
2. **Topology prompt** ("Which storage sits behind prod-db-01…") — the agent
   queried `relationships` + `resources` (5 steps, 30 s) and answered with the
   6-hop path VM → esx-db-01 → ds_prod_db_01 → LDEV 00:10:A0 → Pool-07 →
   VSP-5600-01 and the two other VMs on Pool-07 (prod-db-02 on the same LDEV,
   batch-etl-01 on 00:10:A1), flagging noisy-neighbour risk unprompted.
   `12-topology-final.png`.
3. **Incident RCA prompt** (the seeded scenario, model Claude Sonnet 5 via the
   provided key) — the agent pulled `metrics` with `dt: true` across the three
   layers, `alerts`/`symptoms` for the window, `metrics::VMWARE/VirtualMachine`,
   and wrote a root-cause document: timeline 02:08 batch-etl-01 IOPS 6× above
   its dynamic threshold → 02:11 Pool-07 CRITICAL (1.6 → 18.4 ms vs 2.7 ms
   band, 91 % utilisation) → 02:13 ds_prod_db_01 24.6 ms → 02:15 prod-db-01
   30.1 ms → 02:16 prod-db-02 → self-resolved 02:51–02:54; **root cause
   (high confidence): batch-etl-01's IOPS surge saturated the shared pool**,
   with QoS / re-placement recommendations. This is exactly the seeded
   ground truth (`GET /__mock/scenario`). `12-rca-final.png`,
   `13-rca-final-full.png`.
4. **Ranking prompt** ("rank datastores by latency, top 5 VMs by IOPS, active
   alerts") — three datastores ranked with capacity, a bar chart of the top-5
   VMs, and the one still-active alert (batch-etl-01 high IOPS, INFORMATION,
   open since 02:08). `12-topn-final.png`.
5. **Same incident prompt on Claude Haiku 4.5** (default switched via
   `POST /llm/models/{id}/set_default`) — see `12-rca-haiku-final.png`; result
   recorded below.
6. **Lower layers** — `completions` all `success`; backend log: every Suite
   API call carried `Authorization: OpsToken` + `Accept: application/json`;
   mock log shows the spread `resources/query`, `relationships`,
   `stats/query` (with `dt`), `alerts/query`, `symptoms/query`, `stats/topn`.

### Haiku 4.5 run

Same incident prompt with **Claude 4.5 Haiku** as the org default
(`12-rca-haiku-final.png`, `13-rca-haiku-final-full.png`): the agent again
pulled the three layers with dynamic thresholds and the alert/symptom
timeline, produced the document with the cross-layer chart, and concluded
"Root cause: Storage Pool (Pool-07) I/O saturation — deviates first
(02:09:20 UTC, 80 s before the VM), 8.6× above its 2.7 ms dynamic threshold,
IOPS 3,126 → 7,504; triggering event batch-etl-01 at 1,090 IOPS (5.2× above
threshold) at 02:08; Pool-03 stable, confirming isolation to Pool-07;
confidence high (95 %)". Both models reach the seeded ground truth; Haiku's
write-up is terser and quotes more raw numbers, Sonnet's adds the
remediation angle (QoS / re-placement).

## What this proves / notes for the next agent

- The registry-driven form, test-connection, background indexing, table
  activation and the agent's tool path all work for a connector whose catalog
  is partly *discovered* (`metrics::` tables). The 24-table count is the
  contract the mock and the client agree on.
- The Aria connector is **enterprise-gated** (`requires_license`), so the
  sandbox needs `BOW_LICENSE_KEY`; without it the tile is locked and creation
  is refused server-side.
- Wizard driving gotchas: with zero connections the catalog modal auto-opens
  on `/agents/new` (clicking "Create new connection" behind it is intercepted
  by the modal's sticky category chips); the connector lives under the
  *Infrastructure* chip; after *Save and Continue* a discovery modal must be
  confirmed with *Connect* before the wizard's step 1 re-renders.
- `pkill -f <pattern>` kills the calling shell when the pattern is in its own
  command line — stop the mock with `fuser -k 8443/tcp` instead.
- Not verified here: a real 8.18 appliance (none available — OVA + Broadcom
  entitlement). Six endpoints used by the client exist in the 9.1 spec but
  are not enumerated in the 8.18 guide (`POST resources/query`,
  `POST symptoms/query`, `POST alertdefinitions/query`, `stats/topn`,
  `stats/dt`, `resources/groups`); each has a GET fallback if a live 8.18
  rejects it. First thing to do at the customer: run the connection test and
  the schema index, then the incident prompt against a real past incident.
