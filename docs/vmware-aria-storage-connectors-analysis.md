# VMware Aria Operations 8.18 + Storage (Hitachi, NetApp, Infinidat, IBM) — Connector Research

**Status: the Aria Operations connector (§6e) is implemented** —
`backend/app/data_sources/clients/aria_operations_client.py`, mock in
`tools/aria_operations/`, verified loop in
`docs/feedback-loops/aria-operations-connector.md`. The storage connectors
(§4) remain research. Requested by an RCA (root-cause analysis)
customer running **VMware Aria Operations 8.18.0** with **Hitachi, NetApp,
Infinidat and IBM** storage behind it. **Everything is on-premises** — no
vendor SaaS (Storage Insights, InfiniVerse, Clear Sight, BlueXP/DII) is in
play, and Bag of Words itself will run inside the customer's network. This
doc answers: what APIs exist, what each would let the agent see, how they map
onto our infra-connector pattern, and what to build first.

Date: 2026-09-03. Companion docs with the same shape:
`docs/priority-erp-connector-analysis.md`, `docs/sap-connector-analysis.md`.

---

## 0. Bottom line up front

1. **Build one connector first: `aria_operations` (VMware Aria Operations Suite
   API).** It is the customer's single pane already, its REST API is stable,
   fully documented, unchanged in the VCF Operations 9.x rename, and it exposes
   *everything a management pack collects* through four generic endpoints
   (`/adapterkinds` → `/resourcekinds` → `/statkeys` → `/resources/stats/query`).
   Same build shape as our AppDynamics/Zabbix connectors (fixed virtual-table
   catalog + JSON query spec). Estimate: one connector-sized PR.
2. **Storage comes "for free" through Aria only where a management pack is
   installed and alive — and only Hitachi's is.** Hitachi's pack is
   vendor-maintained and states 8.18+/VCF Operations support. NetApp's pack is
   **end-of-support since Oct 2024**, Broadcom's IBM SVC/Storwize pack is
   **end-of-support since Dec 2023** (IBM's own Spectrum Connect pack last
   changed Apr 2023, no 8.18 statement), and Infinidat's is sporadically
   maintained (last build Sep 2024, listed compatible to 8.16). So the Aria
   connector must discover storage adapter kinds *dynamically* rather than
   hard-code vendors — whatever packs the customer still runs just work — and
   native array connectors are the durable path for NetApp, IBM, Infinidat.
3. **Native array connectors are a second wave, ranked by API quality:**
   - **NetApp ONTAP** — best API of the four: HAL JSON, basic/OAuth2 auth,
     `/api/storage/volumes/{uuid}/metrics` keeps ~1 year of downsampled history
     on-box, EMS events endpoint, official read-only role recipe. Build second.
   - **IBM** — on-prem only, so the SaaS route (Storage Insights, which has
     the history and the official read-only MCP server) is **out**. What is
     left: **Storage Virtualize REST** on the FlashSystem/SVC cluster
     (`:7443/rest/v1/ls*`, a POST-only mirror of the CLI, monitor role) for
     inventory, event log and *current* stats, and **IBM Spectrum Control** if
     the customer runs it (on-prem, multi-vendor, has per-volume performance
     history via REST). Without Spectrum Control, IBM history is limited to
     16 XML stats files per node. See §4.4.
   - **Hitachi VSP** — inventory + alerts via the Configuration Manager REST
     API (public docs, also embedded on the array); **performance history
     lives only in Ops Center Analyzer** (paid, capacity-licensed), whose REST
     API *is* publicly documented and even exposes an E2E topology/bottleneck
     endpoint. Good connector if the customer owns Analyzer — and the Aria
     pack is fed by Analyzer, so if the pack works, Analyzer is there. See §4.2.
   - **Infinidat InfiniBox** — inventory/events/capacity easy; performance is a
     *live sampling* collector API (create → poll → delete) with **no history on
     the array**; history is in the separate InfiniMetrics appliance whose API is
     only community-documented. Lowest priority as a native connector.
4. **Two shortcuts already exist in the product for wave 2:** the `prometheus`
   connector (if the customer runs NetApp Harvest or any exporter) and the
   `custom_api` tool-provider connection (any of these REST APIs can be
   declared endpoint-by-endpoint today, no code). Neither replaces a native
   connector for agent quality, but both unblock a pilot.
5. **MCP presets are not a shortcut here.** The one good official storage
   MCP (IBM's) fronts the SaaS Storage Insights, which this customer does not
   use. NetApp's `ontap-mcp` is self-hostable but its read tools are a thin
   generic `ontap_get`. Hitachi and Infinidat ship nothing. Native connectors
   (or `custom_api` for a pilot) are the path.
6. **On-prem deployment constraints that shape every connector:** private-CA
   or self-signed TLS everywhere (every config needs `verify_ssl`, like
   AppDynamics/Zabbix), LDAP/AD-backed service accounts rather than API keys
   (Aria `authSource`, Hitachi Common Services OIDC, ONTAP basic/cert), and
   the app must be deployable with network reach to management networks
   (Aria on 443, Virtualize on 7443, Hitachi CM on 23451 / Analyzer on 22016,
   ONTAP on 443). See §1b.

---

## 1. What "RCA customer" means for the design

Our infra connectors already carry an RCA posture (`category="infra"`:
appdynamics, zabbix, elasticsearch, splunk, opensearch, prometheus, jaeger,
aws_cost). The planner prompt has a dedicated root-cause loop
(`backend/app/ai/agents/planner/prompt_builder_v3.py:337`): confirm symptom →
decompose → test hypotheses → causal chain, delivered via `create_doc`.

For a VMware + SAN estate the RCA questions look like:

- "VM latency spiked at 02:10 — was it the datastore, the array port, the pool,
  or the host HBA?" → needs the **relationship graph** (VM → datastore → LUN/LDEV
  → pool → array) plus **time-aligned metrics** across layers.
- "Which alerts fired in the 30 minutes before the incident?" → alerts +
  symptoms with start/cancel timestamps.
- "Is this a capacity problem?" → pool/aggregate capacity trend.

Aria Operations is the only system in the list that already *has* the
cross-layer relationship graph (its storage packs create datastore↔LUN
relationships). That is the single strongest argument for building it first:
the array APIs each see only their own box.

### 1b. On-prem consequences

Everything is on-premises, which rules some routes out and adds requirements
to the rest:

| Vendor | Ruled out (cloud) | On-prem path that remains |
|---|---|---|
| VMware | — (Aria 8.18 is on-prem by definition) | Suite API on the appliance |
| NetApp | BlueXP / Data Infrastructure Insights, Active IQ Digital Advisor | ONTAP REST per cluster; AIQUM (on-prem VM) for fleet; Harvest → Prometheus |
| Hitachi | VSP 360 Clear Sight | Configuration Manager (on-prem server or on-array) + Ops Center Analyzer (on-prem) |
| Infinidat | InfiniVerse | InfiniBox REST on-array + InfiniMetrics (on-prem VM) for history |
| IBM | **Storage Insights (SaaS)** — and its MCP server | Storage Virtualize REST on-array + **Spectrum Control** (on-prem) for history |

Cross-cutting requirements for the connectors:
- **TLS:** management interfaces almost always carry private-CA or
  self-signed certs. Every config needs `verify_ssl` (AppDynamics/Zabbix
  already do this) and ideally an optional CA bundle path, since disabling
  verification wholesale is a hard sell to a security team.
- **Identity:** no vendor here issues UI-minted API keys for on-prem use.
  Expect LDAP/AD service accounts (Aria `authSource`, ONTAP `-authentication-
  method password` users, Virtualize *monitor* role, Hitachi Analyzer via
  Basic or Common Services OIDC, InfiniBox `read_only` role). Per-user
  credential scope (`scopes=["system","user"]`) is useful: each analyst's own
  Aria/ONTAP login enforces the vendor's RBAC.
- **Network reach:** BOW must be deployed where it can reach management
  VLANs: Aria 443, ONTAP 443, Virtualize 7443, Hitachi CM 23451 / Analyzer
  22016 / detail view 8443, InfiniBox 443, Spectrum Control 9569. Worth
  listing in the connector help text and the deployment guide.
- **Air-gap:** no outbound calls from any of these connectors (already true
  of our infra connectors); all Python deps are plain `requests`, so the
  air-gapped compose bundle (`docs/feedback-loops/airgap-docker-compose-bundle.md`)
  needs no change.
- **History retention is the customer's problem, not a SaaS's.** ONTAP keeps
  ~1 year on-box; Hitachi/IBM/Infinidat history exists only if the on-prem
  analytics product (Analyzer, Spectrum Control, InfiniMetrics) is deployed.
  Aria itself retains metrics per its own policy (default 6 months) and is
  the fallback history store for any vendor whose pack is installed.

---

## 2. What already exists in the codebase (the baseline)

| Piece | Where | Reuse for this work |
|---|---|---|
| Connector contract | `backend/app/data_sources/clients/base.py` — `DataSourceClient` with `test_connection`, `get_schemas`, `execute_query`, `system_prompt` | Same for all four |
| Infra reference implementations | `appdynamics_client.py` (REST, virtual-table catalog, JSON spec, OAuth token cache), `zabbix_client.py` (JSON-RPC, catalog declared in code, `history_window_days` config) | Aria connector = AppDynamics shape almost 1:1 |
| Dashboard/saved-search replay for RCA | `splunk_client.py`, `elasticsearch_client.py` catalog the team's dashboards as tables so the agent replays the operator's own investigation | Aria has the same concept (dashboards, views, custom groups, alert definitions) — worth cataloging alert definitions at minimum |
| Registry-driven forms | `backend/app/schemas/data_source_registry.py` + `schemas/data_sources/configs.py` — Pydantic config/credentials become the connect form | One `AriaOperationsConfig` + credential variants |
| Test scaffolding | `tools/appdynamics/mock_controller.py` (526-line mock REST controller) + `tests/integrations/ds_clients.py` | Build `tools/aria_operations/mock_suite_api.py` the same way; no Aria license needed in CI |
| Generic REST tool provider | `custom_api_client.py` (`CustomAPIConfig.endpoints`, basic/bearer/none auth, CSRF flow) | Pilot path for any array API without code |
| Prometheus | `prometheus_client.py` | Covers NetApp Harvest / any exporter |
| Skill | `.agents/skills/add-connection-type/SKILL.md` | The build checklist |

Enterprise gating: infra connectors are `requires_license="enterprise"`
(zabbix, splunk, …) or `version="beta"` (appdynamics). New ones should follow
the same flags.

---

## 3. VMware Aria Operations 8.18 — the primary connector

### 3.1 Product facts that matter

| Fact | Detail | Source |
|---|---|---|
| Release | 8.18 GA 2024-07-23 (VCF 5.2 era); latest patch 8.18.7 (2026-05-27) | Broadcom RN |
| Lineage | vCOps → vRealize Operations → Aria Operations (8.10+) → **VCF Operations** (9.0+, GA 2025-06-17). On-prem retained. | Broadcom VCF 9 docs |
| API continuity | 9.0/9.1 still serve `/suite-api/api/...`, same token flow, same headers, same swagger. **Building against 8.18 is not building against a dead API.** | Broadcom 9.x API docs |
| Upgrade trap | 8.18.6/8.18.7 → 9.0.0–9.0.2 is *unsupported* (KB 438994). Customer will likely sit on 8.18.x for a while. | Broadcom KB |
| EOGS | Not fetchable (interactive lifecycle matrix only). Community: ~Oct 2027 with VCF 5.2. Treat as inferred. | endoflife.date |
| Docs | Programming guide on techdocs.broadcom.com (8-18); endpoint reference on developer.broadcom.com (`xapis/vcf-operations-api` is the most detailed). Swagger UI ships on the appliance at `/suite-api/doc/swagger-ui.html` (note `doc`, not `docs`). | official |

### 3.2 Authentication

- **Token flow (build on this):** `POST /suite-api/api/auth/token/acquire`
  with JSON `{"username","password","authSource"}`. `authSource` omitted =
  `LOCAL`; otherwise the *name* of the configured LDAP/AD/vIDM source.
  Response `{"token","validity","expiresAt","roles"}`. Send as
  `Authorization: OpsToken <token>` (current) — `vRealizeOpsToken <token>`
  still accepted on 8.16/8.18/9.x, so either works. Lifetime **6 hours,
  sliding** (extended on each call). Cache + refresh on 401 exactly like
  `AppDynamicsClient._fetch_token`.
- **Basic auth:** deprecated and **off by default on fresh 8.18 installs**
  (requires editing `api-conf.properties` on every node). Do not offer it.
- **SSO users:** vIDM / vCenter-SSO users need an externally obtained SAML
  token (`Authorization: SSO2Token`). Out of scope for v1; the customer should
  create a local or LDAP read-only service account. Ask them which
  `authSource` names exist (needed as a config field).
- **UI-generated API keys:** none exist in Aria Operations (the "API tokens"
  pages belong to Aria Automation / Operations for Applications). So the only
  credential variant is `userpass` (+ `auth_source`). Per-user scope makes
  sense (each analyst's own Aria login = Aria's own RBAC applies).
- Always send `Accept: application/json` — XML is the historic default and
  is deprecated for the next major release.

### 3.3 Endpoint surface → proposed virtual tables

All under `/suite-api/api`. `page` is 0-based, `pageSize` default/max 1000.

| Virtual table | Endpoint | Notes |
|---|---|---|
| `adapter_kinds` | `GET /adapterkinds?retrieveResourceKindInfos=true` | Discovers installed packs incl. storage vendors. |
| `resource_kinds` | `GET /adapterkinds/{ak}/resourcekinds` | Per adapter. |
| `stat_keys` | `GET /adapterkinds/{ak}/resourcekinds/{rk}/statkeys` | Metric dictionary: `key,name,unit,rollupType,description`. This is what lets the agent find "Hitachi pool response time" without us hard-coding it. |
| `resources` | `GET /resources` with `adapterKind[]`, `resourceKind[]`, `name`, `regex`, `parentId[]`, `resourceHealth[]`, `resourceStatus[]` | Inventory: `identifier`, `resourceKey.{name,adapterKindKey,resourceKindKey}`, health, badges. |
| `properties` | `GET /resources/{id}/properties` | Static attributes (serial, model, datastore UUID …). |
| `relationships` | `GET /resources/{id}/relationships?relationshipType=PARENT|CHILD|ALL` | The RCA graph edge list. |
| `metrics` | `POST /resources/stats/query` body `resourceId[]`, `statKey[]`, `begin`/`end` epoch ms, `rollUpType` (SUM/AVG/MIN/MAX/NONE/LATEST/COUNT), `intervalType`, `intervalQuantifier` | Time series. POST avoids URL-length limits; batch ≤1000 resources. |
| `metrics_latest` | `GET /resources/stats/latest?resourceId[]…&statKey[]…` | 1–1000 resources per call; "current value" questions. |
| `alerts` | `POST /alerts/query` (`activeOnly`, `alertCriticality`, `alertStatus`, `startTimeRange`, `resource-query{…}`) | `alertLevel,startTimeUTC,updateTimeUTC,cancelTimeUTC`. |
| `symptoms` | `GET /symptoms?resourceId[]&activeOnly=false` | Finer-grained than alerts; `symptomCriticality,message,kpi`. |
| `alert_definitions` | `GET /alertdefinitions?adapterKind&resourceKind` | Encodes operator thresholds — the Aria analogue of Splunk dashboards for RCA replay. |
| `recommendations` | `GET /recommendations` | Optional. |
| `events` | `/events` | **Unverified** — portal pages 404'd. Verify on the appliance swagger before promising it. |

Design notes:
- Follow AppDynamics: `execute_query` takes a JSON spec
  `{"table": "metrics", "resource_kind": "Datastore", "stat_keys": [...],
  "duration_in_mins": 60, "rollup": "AVG", "interval": "MINUTES"}`; results as
  a DataFrame with epoch-ms timestamps.
- Catalog descriptions should embed the *discovered* adapter kinds and a
  sample of resource kinds per adapter (as AppDynamics embeds app names and
  flow edges) so the planner knows a Hitachi/IBM pack is present.
- No documented rate limit or 429 policy (the 100 req/s figure is Operations
  for Networks). Practical guard: cap `resourceId` batches at 1000 and
  downsample via `rollUpType`/`intervalType`.

### 3.4 Storage visibility through Aria (the management packs)

| Vendor | Pack | Status on 8.18 | What it exposes |
|---|---|---|---|
| **Hitachi** | *Hitachi Infrastructure Management Pack for VMware Operations* (Hitachi-authored; fed by Ops Center Analyzer / Configuration Manager) | **Supported on 8.18+ and VCF Operations** (Hitachi PDF, Feb 2025) | Storage systems, pools, LDEVs, ports, parity groups; VSP One / 5000 / E-series; capacity + performance |
| **NetApp** | *VMware Aria Management Pack for NetApp FAS/AFF* 4.2 (ex-Blue Medora, Broadcom-authored) | **End of general support 2024-10-01** (KB 373307). Last build Apr 2023. No NetApp-authored replacement. | Clusters, nodes, SVMs, aggregates, volumes, LUNs, "110+ metrics", datastore↔storage relationships |
| **Infinidat** | *InfiniBox Management Pack for vROps* 1.2.2 (Infinidat-authored, free) | Last build 2024-09-25; compatibility guide lists up to 8.16; 8.18 unconfirmed | Systems, pools, volumes, hosts, datastore relationships |
| **IBM** | (a) *IBM Storage Management Pack for vROps* via Spectrum Connect 3.11 (IBM-authored: FlashSystem/SVC/Storwize/A9000; **not DS8000**) — (b) Broadcom *MP for IBM SVC and Storwize* 4.2 (ex-Blue Medora, CLI-based) | (a) last changelog Apr 2023, lifecycle matrix stops at 3.10 (2022), no 8.18 statement — **treat as unmaintained**; (b) **EOGS 2023-12-31** (Broadcom KB 326446) though docs still exist under 8.17.1/8.18 | Systems, pools, volumes, hosts, ports, disks; health, events, perf; datastore/VM relationships pushed every 5 min |
| generic | *MP for Storage Devices* (SMI-S) | EOGS 2025-09-30 | — |

**Consequence:** the Aria connector must treat storage adapter kinds as data,
not code. If the customer has the Hitachi pack, day one covers Hitachi with
relationships to VMs; EOL packs (NetApp, IBM) may still be running and still
answer through the same generic endpoints, but they will not survive a 9.x
upgrade. That fragility is exactly why NetApp, IBM and Infinidat are the
candidates for native connectors.

**Questions for the customer before building:** which management packs are
installed (`GET /adapterkinds` answers this in one call), which `authSource`
names exist, and whether they plan to move to VCF Operations 9.x.

### 3.5 AI / MCP / SDK landscape

- No official Aria Operations MCP server. Broadcom "Intelligent Assist for
  VCF" is a support-KB assistant, not an API. Community MCPs exist
  (`vmware-skills/VMware-Aria`, vRabbi's VCF Ops MCP) — useful as a reference
  for which endpoints matter, not as a dependency.
- No maintained Python client (`nagini` is Python-2.7 era). Hand-roll
  `requests` calls as AppDynamics/Zabbix do. Zero new dependencies.

---

## 4. Native storage connectors — per vendor

### 4.1 NetApp (ONTAP) — the best array API; build second

**Which NetApp product:** in a VMware shop "NetApp storage" is ONTAP
(AFF/FAS/ASA) — ONTAP tools for vSphere provisions NFS/VMFS/vVol datastores.
E-Series (SANtricity `/devmgr/v2`) and StorageGRID (`/api/v4`) are different
APIs; confirm with the customer but assume ONTAP.

**Two routes, both official:**

| Route | Base | Scope | History | Auth |
|---|---|---|---|---|
| **ONTAP REST** (per cluster) | `https://<cluster>/api/` | one cluster | on-box, ~1 year downsampled | basic; OAuth2 JWT from external IdP (9.14+); cert |
| **Active IQ Unified Manager (AIQUM)** | `https://<um>/api/v2/` | fleet (all clusters) | `interval` up to `6m` on metrics; 72-h `analytics` | basic; Operator role = read-only |

ONTAP REST facts (verified on docs.netapp.com):
- Inventory: `/api/cluster`, `/api/cluster/nodes`, `/api/svm/svms`,
  `/api/storage/aggregates`, `/api/storage/volumes`, `/api/storage/luns` —
  with `space.*` capacity and (newer ONTAP) embedded `metric`/`statistics`.
- History: `GET /api/storage/volumes/{uuid}/metrics?interval=1h|1d|1w|1m|1y`
  (15 s / 5 min / 30 min / 2 h / 1 day samples), same for aggregates, LUNs,
  `/api/cluster/metrics`, IP interfaces. Fields `iops.{read,write,other,total}`,
  `latency.*` (µs), `throughput.*` (B/s). **Per-object calls only** — no
  fleet bulk metrics endpoint, so the connector must fan out.
- Raw counters: `/api/cluster/counter/tables/{name}/rows` (9.11+; parity with
  ZAPI perf objects at 9.12.1).
- Events: `GET /api/support/ems/events` (`time`, `node.name`, `message.name`,
  `message.severity`, `log_message`), filterable.
- Query language: `fields=`, `max_records` (default 10 000), `order_by`,
  operators `* < > <= >= .. ! |`, follow `_links.next.href`. Avoid
  `fields=*` on big collections; `return_timeout` default 15 s can truncate.
- Read-only role recipe: `security login rest-role create -role X -access
  readonly -api /api` (Harvest/DII docs).
- **Session cap: 20 concurrent sessions per User-Agent** (KB) — set a
  distinctive UA and bound concurrency.
- ZAPI is deferred-indefinitely and auto-suspends on 9.14.1+; 9.18.1 drops it.
  REST-only is correct.

Alternatives already covered by the product:
- **NetApp Harvest** (official OSS, Apache-2.0, monthly releases, v26.08.0)
  exports ONTAP/E-Series/StorageGRID to Prometheus → our `prometheus`
  connector works today with zero code. Harvest also ships an MCP server
  (v25.11+) over the TSDB.
- **`NetApp/ontap-mcp`** (official, Go): read tools are generic (`ontap_get`,
  `search_ontap_endpoints`); the rest mutate. Could be an MCP preset for a
  pilot, but the agent would be writing raw REST paths.

SDK: `netapp-ontap` (BSD-3, tracks ONTAP releases) pulls marshmallow and pins
urllib3; raw `requests` is preferable, as for the other REST connectors.

Proposed catalog: `clusters, nodes, svms, aggregates, volumes, luns,
metrics (object_type + uuid or name + interval), ems_events, counter_tables,
counter_rows`. Config: cluster URL, `verify_ssl`, `default_interval`; creds:
`userpass`, `bearer` (for OAuth2 shops), optionally client cert. A second
config mode for AIQUM (`/api/v2` + gateway proxy) would give fleet-wide
inventory in one connection — decide after asking whether the customer runs
AIQUM.

### 4.2 Hitachi (VSP) — Configuration Manager for inventory, Analyzer for RCA

Caveat: several Hitachi guides (VSP One Block Administrator REST, Ops Center
Administrator, Protector, Analyzer viewpoint) return 401 without a support
login. The two guides that matter here — Configuration Manager REST
(MK-99CFM000) and Analyzer REST (MK-99ANA003) — are public.

**Three APIs, and which does what:**

| API | Base | Gives | Auth | Docs |
|---|---|---|---|---|
| **Ops Center API Configuration Manager** ("CMREST"), also embedded on VSP E/G/F/5000/One Block controllers | `https://<cm>:23451/ConfigurationManager/v1/objects/storages/{storageDeviceId}/…` (on-array: `https://<svp-or-ctl>/ConfigurationManager/v1/…`) | Inventory (`storages`, `ldevs`, `pools`, `ports`, `host-groups`, `parity-groups`, `journals`) and **alerts** (`…/alerts?type=CTL1\|CTL2\|DKC&start=&count=`). **No performance time series.** | `POST …/storages/{id}/sessions` (Basic) → `Authorization: Session <token>`; Basic also accepted on GETs | public |
| **Ops Center Analyzer REST** | `https://<analyzer>:22016/Analytics/v1/…` | Performance metrics with history, events/alerts, **E2E topology and bottleneck analysis** | `Basic`, `HSSO <token>` (1000 s), or `Bearer <OIDC token>` from Ops Center Common Services (300 s) | public |
| **Analyzer detail view** (ex-Data Center Analytics) | `https://<dv>:8443/dbapi.do?action=query…` with an MQL body and `startTime`/`endTime` | Raw time series, alerts | Basic | public at TOC level |

CMREST details: `ldevs` returns 100 by default, up to 16 384 via `count`,
walk further with `headLdevId`; `detailInfoType=qos|externalVolume` adds
detail. Older arrays (G200–G800, G1000/G1500) have no on-box REST and need
the CMREST server, which is a free download. Read-only role: "Storage
Administrator (View Only)".

Analyzer REST details (the RCA-relevant part):
- Filtering with HQL: `?$query=instanceID in [1000,1001] and status eq
  'Warning'`; paging via `page`/`pageSize`.
- Metrics: `GET /Analytics/v1/objects/PerformanceVolume` (also
  `PerformanceNode`, `PerformanceVirtualMachine`) with `$query=MetricType eq
  'RAID_VOLUME_RAIDLDEV_TOTALIOPS'`, `basePointNodeID`, `pointTimeRange`,
  `conflict=peak|average` → peak/average per interval.
- Events: `GET /Analytics/v1/objects/Events` (`level`, `category`
  PERFORMANCE/EVENT/SETTING, `deviceName`, `thresholdValue`).
- **E2E view:** `GET /Analytics/v1/services/E2EView/actions/getTopologyData`
  plus related-resource operations used by Analyzer's own "Analyze
  Bottleneck" — this is the vendor's RCA graph (VM → host → port → LDEV →
  pool → parity group), directly usable by our planner's root-cause loop.
- Raw records: `GET /Analytics/RAIDAgent/v1/objects/RAID_PI_LDS|RAID_PI_PRCS|
  RAID_PI_CLPS…` (LDEV, MP, cache records; Basic auth only) — what
  xormon/hds2graphite consume.
- Licensing: Analyzer is a **paid, capacity-tiered licence per array**. If
  the customer does not own it, performance history is only reachable via
  the legacy Export Tool 2 CSV dumps (1–15 min samples, a CLI, not an API) —
  not a connector we should build.

Other facts:
- Hitachi's Aria pack (02.11.0, Feb 2025) **collects all its metrics from
  Analyzer**, so "the Aria pack works" implies Analyzer exists — a useful
  tell when qualifying the customer.
- No official Python SDK (`vsp360sdk` is an empty README). The Ansible
  collection `hitachivantara.vspone_block` (v4.8.2, 183 modules) wraps CMREST
  and shows current call shapes; it has alert facts but no performance
  modules. Community `pascalhubacher/HitachiBlockAPI` wraps CMREST. Raw
  `requests` again.
- No Hitachi MCP server for storage. VSP 360 Clear Sight (SaaS AIOps) has no
  customer-facing API.
- Ops Center Administrator (ex-Storage Advisor) has its own token API
  (`POST /v1/security/tokens` → `X-Auth-Token`) but is a provisioning UI;
  not needed.

Proposed shape: one `hitachi_ops_center` connector with two optional
endpoints in config — Configuration Manager URL (+ storageDeviceId list) for
inventory/alerts, Analyzer URL for metrics/events/E2E — each with its own
credential pair; the catalog only includes Analyzer tables when that URL is
set. Catalog: `storage_systems, ldevs, pools, ports, host_groups,
parity_groups, alerts` (CMREST) + `performance (object type + metric type +
time range), events, e2e_topology` (Analyzer).

### 4.3 Infinidat (InfiniBox) — easy inventory, awkward performance

Caveat: support.infinidat.com returned 403 to unauthenticated fetches; the
findings below come from Infinidat's public GitHub/PyPI (`infinisdk`, the CSI
driver, Ansible collection, Postman collection) and third-party monitors.

- **API:** `https://<system>/api/rest/…`. Auth: `POST /api/rest/users/login`
  (JSESSIONID cookie) or HTTP Basic per request. No API-token concept found.
  Use a `read_only`-role user.
- **Query syntax:** `?field=<op>:<value>` with `eq, ne, gt, ge, lt, le, in,
  like`; `fields=`; `sort=`; `page`/`page_size` (**max 1000**). Envelope
  `{"result", "metadata": {"page","page_size","number_of_objects"}, "error"}`.
- **Inventory/capacity:** `system`, `system/capacity`, `pools`, `volumes`,
  `filesystems`, `hosts`, `host_clusters`, `replicas`, `components/{nodes,
  enclosures,drives,fc_ports,eth_ports,…}`, `links`, `qos_policies`.
- **Events:** `GET /api/rest/events` (`code, level, description, timestamp,
  reporter, affected_entity_id`); `events/types` for the code dictionary.
- **Performance = live sampling only:** `POST /api/rest/metrics/collectors`
  `{"type":"COUNTER","collected_fields":["ops","throughput","external_latency_wout_err"],"filters":{"protocol_type":"SAN"}}`
  → poll `GET /api/rest/metrics/collectors/data?collector_id=…` → `DELETE`.
  Infinidat's own CSI driver waits ~5 s then reads once. **No history on the
  array.** History lives in **InfiniMetrics** (free VM appliance, ~1 year,
  10 s granularity) whose REST API
  (`/api/rest/systems/{serial}/monitored_entities/{id}/data/`) is only
  community-documented. InfiniVerse (cloud AIOps) has no public API.
- **SDK:** `infinisdk` (official, BSD-3, released 2026-07) covers auth,
  inventory, events, pagination; **no metrics wrapper**. Raw `requests`
  suffices.
- **No MCP / AI API.** Infinidat was acquired by Lenovo (closed 2026-04-10) —
  roadmap risk for the vROps pack and InfiniMetrics.

Verdict: a native connector answers *inventory, capacity, events, and
"what is the array doing right now"* well; historical RCA ("latency at 02:10
last Tuesday") needs InfiniMetrics or the Aria pack. Lowest priority of the
four.

### 4.4 IBM — Virtualize REST on-array; Spectrum Control for history (Storage Insights is SaaS, excluded)

**Which IBM product:** in a VMware shop this is almost always FlashSystem /
SAN Volume Controller running **IBM Storage Virtualize** (9.1.x is the current
LTS, GA 2025-07-25; 8.x still common). DS8000 has its own HMC REST API
(`:8452/api/v1/tokens`, `pyds8k`); Ceph/Scale are rare as vSphere primary
storage. Caveat: IBM Docs pages are JS-rendered and several returned only
headers to the research agent; items marked (snippet) need re-verification.

**Route 1 — IBM Storage Virtualize REST API (on the array, official):**
- `https://<cluster>:7443/rest/v1/<target>` (8.4.2+; unversioned `/rest/`
  is legacy v0, auto-redirected). Introduced in 8.1.3. REST Explorer at
  `:7443/rest/explorer`.
- **POST is the only HTTP method.** `POST /rest/v1/auth` with headers
  `X-Auth-Username` / `X-Auth-Password` → JWT in `X-Auth-Token`; lifetime 1 h
  default (configurable 10–120 min); **403 = expired token** (not 401).
  A *monitor*-role user suffices.
- Targets mirror CLI verbs: `lssystem`, `lsvdisk`, `lsmdiskgrp`, `lsmdisk`,
  `lshost`, `lsnode`/`lsnodecanister`, `lsdrive`, `lsenclosure*`, `lsportfc`,
  `lseventlog`; parameters (`filtervalue`, `limit`) go in the JSON body and
  pass through to the CLI. Historic limit: >2000 results unsupported (may
  restart the service) — always page with `filtervalue`/`limit`.
- **Performance:** `lssystemstats` / `lsnodestats` / `lsnodecanisterstats`
  return current values (+ a short `-history` table for some stats). Real
  history is per-node XML files `Nm_/Nv_/Nn_/Nd_stats_<panel>_<date>` in
  `/dumps/iostats`, interval `startstats -interval` (default 15 min), **max 16
  files per type per node**, fetched via `lsdumps`/`cpdumps` + scp (8.7.x adds
  a REST download path). Parsing those is a project; not for v1.
- 8.6.1 removed CIM/SMI-S — REST is the only supported programmatic path.

**Route 2 — IBM Storage Insights (SaaS, official, fleet-wide) — ruled out
for this customer (on-prem only); documented for completeness:**
- `https://insights.ibm.com/restapi/v1/tenants/{tenant}/…`. Admin mints an API
  key (1 day–2 years, ≤5 per user); `POST …/token` with `x-api-key` → token
  in `x-api-token`, **15-minute expiry, not configurable** → cache + refresh
  like our AppDynamics OAuth path. 429 on bursts.
- Endpoints: `storage-systems` (`?storage-type=block|filer|object`),
  `…/volumes`, `…/metrics?types=<metric>&duration=7d`, fleet
  `storage-systems/metrics`, `alerts`, `notifications`; Q1-2026 additions:
  `io-groups/metrics`, `nodes/metrics`, `ip-ports/metrics`. Swagger at
  `insights.ibm.com/restapi/docs/`.
- **Free vs Pro:** APIs work on both but free scope is limited (fewer metrics,
  device alerts only, no performance export). Assume the customer needs
  **Storage Insights Pro** for RCA-grade history; ask.
- **Official MCP server:** `IBM/ibm-storageinsights-mcpserver` (Jun 2025,
  FastMCP, stdio or streamable HTTP): 13 read-only tools
  (`fetch_storage_systems`, `fetch_system_components`, `fetch_system_alerts`,
  `fetch_system_io_rate/response_time/cpu_utilization/capacity`, …). This is
  the one storage MCP we could ship as a preset for a pilot. (The separate
  `IBM/ibm-flashsystems-mcpserver` is a *management* server — creates
  volumes/hosts — not suitable.)

**Route 3 — IBM Spectrum Control (on-prem, official, public) — the on-prem
history source:** still "Spectrum Control" 5.4.13.1 (Jun 2025).
`https://<host>:9569/srm/REST/api/v1/` with **form login**
`POST /srm/j_security_check` + session cookie; `StorageSystems`,
`…/Volumes`, `Volumes/{id}/Performance?granularity=sample&startTime=&endTime=`,
`Switches`, `Servers`. Multi-vendor (Pure, NetApp, Dell, Hitachi per the
supported-products page — snippet, verify). **If the customer has it, it is a
single on-prem fleet source that may cover several of the four vendors** —
ask before building anything IBM-specific. Note the form-login + cookie auth
is a custom flow (not basic/bearer), so `custom_api` cannot front it today.

**SDKs:** `ibm.storage_virtualize` Ansible collection (REST-based, quarterly,
Jul 2026) shows the current call shapes; `ibm-svc-rest-client` (OpenAPI
generated, requires 9.1.3.0) is too new to depend on; `pysvc` is SSH and dead
(2019). Raw `requests` again.

Proposed catalog (Virtualize): `system, nodes, pools (lsmdiskgrp), mdisks,
volumes (lsvdisk), hosts, drives, enclosures, fc_ports, event_log,
system_stats, node_stats`. Config: cluster URL/port 7443, `verify_ssl`; creds:
`userpass` (monitor role). If Spectrum Control is present, a separate
`ibm_spectrum_control` type (`storage_systems, volumes, ports, performance
(volume/system id + granularity + time range), servers, switches`) is the
better first IBM build because it carries history and possibly other
vendors. Ask which the customer has before choosing.

---

## 5. Options compared (per vendor)

| Option | Aria Ops | NetApp | Hitachi | Infinidat | IBM |
|---|---|---|---|---|---|
| A. Native connector | ✅ build first | ✅ second | ✅ if customer owns Analyzer (RCA graph + history); CMREST alone = inventory/alerts only | ⚠️ live-only perf | ✅ Virtualize (live, on-array); Spectrum Control for history if deployed |
| B. Through Aria packs | — | ⚠️ pack EOL Oct 2024 | ✅ vendor-maintained, 8.18 OK | ⚠️ 8.16 listed, 8.18 unconfirmed | ⚠️ Broadcom pack EOL Dec 2023; IBM pack stale (2023) |
| C. `prometheus` connector via exporter | n/a | ✅ Harvest (official) | community exporters only | community (Zabbix/graphite scripts) | IBM `spectrum-virtualize-exporter` + Storage Insights exporter (official-ish, small) |
| D. `custom_api` declared endpoints | possible but token flow needs custom login | ✅ basic auth, plain JSON | ✅ basic/session | ✅ basic | ⚠️ Virtualize: POST-only + custom token header; Spectrum Control: form login + cookie → both unsupported today |
| E. MCP preset | community only | `ontap-mcp` (official, self-hosted, thin) | none | none | ❌ the official one fronts SaaS Storage Insights |

---

## 6. Suggested sequencing

1. **`aria_operations` connector** (enterprise/beta flag). Mock Suite API in
   `tools/aria_operations/` seeded with a vSphere + one storage pack so CI and
   the sandbox loop run without a licence. Catalog: the 11 tables in §3.3,
   storage adapter kinds discovered at index time.
2. **Pilot the storage arrays through Aria + `custom_api`** with the customer
   while wave 2 is built; confirm installed packs and whether they run
   AIQUM / Harvest / InfiniMetrics / Storage Insights.
3. **`netapp_ontap` connector** (per-cluster REST, optional AIQUM mode).
4. **Hitachi (`hitachi_ops_center`) or IBM (`ibm_storage_virtualize`, plus
   `ibm_spectrum_control` if deployed)** next, in the order the customer's
   actual estate dictates — Hitachi first if they own Analyzer (its E2E
   endpoint is the closest thing to Aria's relationship graph among the array
   vendors).
5. **Infinidat** last: inventory/events/capacity connector is cheap, but
   historical performance needs InfiniMetrics and its community-documented
   API.

---

## 6b. Test targets — what we can actually run the sandbox loop against

None of the five ships a Docker image; the vendor SaaS routes are either
terminated (Aria) or excluded (on-prem customer). Our precedent for this
situation is the AppDynamics connector: a doc-shaped mock in `tools/`
(`tools/appdynamics/mock_controller.py`, run from `docker-compose.yaml`)
driven from `integrations.json`, plus a one-off live confirmation against a
real instance. The same split applies here, with better raw material for
some vendors than others.

| Vendor | Real instance we could get | Container / simulator | Spec or recorded fixtures for a mock | CI plan |
|---|---|---|---|---|
| **Aria Ops** | OVA only; download needs an **entitled Broadcom support account** (VCF/VVF). Once you have it: 60-day eval (8.x) / 90-day (9.0). Needs ESXi or nested ESXi; Extra Small node = 2 vCPU / 8 GB. Aria SaaS **terminated Dec 2024**. ISV route = Broadcom TAP (NFR licences). Hands-on Labs are free but console-only (no external API reach). | none | **Official OpenAPI 3 spec, Apache-2.0**: `vmware/vcf-api-specs` → `specifications/vcf-operations/vcf-operations-openapi.json` (also served unauthenticated by any appliance at `/suite-api/doc/openapi/v3/public-api.json`). Recorded responses: `vmware-archive/vrops-export/src/test/resources/` (resources, stats, props, statkeys), `imtrinity94/VMware-REST` (full `/adapterkinds`). No public storage-pack captures. | Seeded FastAPI mock like AppDynamics (needs stateful relationships + deterministic series, which a spec-only Prism mock cannot give); validate shapes against the official spec. Live confirmation: TAP NFR + nested ESXi, or the customer's own instance. |
| **NetApp ONTAP** | **Simulate ONTAP (vsim)** — the real ONTAP image as an OVA + licence file, versions to 9.18.1. Needs a **customer or partner support login** (guest accounts blocked). Officially VMware Workstation/Fusion; community `tcler/ontap-simulator-in-kvm` runs it under KVM (≥16 GB RAM). 6 GB RAM / 40 GB disk per node. ONTAP Select 90-day eval and Lab on Demand also exist (support login). | vsim under KVM is the closest thing to a container; no Docker image. | Full OpenAPI spec public in `NetAppDocs/ontap-restapi` (`swagger-ui/index.html`, inline JSON) and on-cluster at `/docs/api/swagger.yaml`. Fixtures: `ansible-collections/netapp.ontap` unit tests (hundreds of canned REST responses), `NetApp/harvest` `cmd/collectors/rest/testdata/`. | Prism/Mockoon from the official spec for CI; vsim on a KVM runner for the live leg. **Best-served vendor.** |
| **Hitachi** | Nothing self-service for VSP. Ops Center installers are behind the support portal and **need a real array behind them**. VSP One SDS Block has a free trial / AWS PayGo, but its REST surface (`/ConfigurationManager/simple/…`) differs from the VSP / Configuration Manager API. HALO labs are partner-only. | none | No OpenAPI spec; only the public HTML/PDF reference guides (MK-99CFM000, MK-99ANA003). No recorded fixtures in the Ansible/Terraform repos. | Hand-built mock from the docs; live leg only via the customer or a Hitachi partner lab. **Highest test-cost vendor.** |
| **Infinidat** | Nothing. `infinisdk`'s own tests use an internal simulator (`infinisim`, not on PyPI, not on GitHub). InfiniMetrics needs an InfiniBox. | none public | No spec. Response shapes are encoded in `infinisdk` source (binders/fields), which is enough to hand-build a mock. | Mock from `infinisdk` shapes; ask Infinidat/Lenovo partner program for `infinisim`; live leg via the customer. |
| **IBM Virtualize** | **IBM Storage Virtualize for Public Cloud — 60-day free trial on AWS/Azure Marketplace** (v8.6.0.2, same code base, pay for the VMs, 2-node pairs). IBM confirms no on-prem simulator. Spectrum Control has a 90-day POC OVA but needs real storage. | none | On-box REST Explorer is OpenAPI-based but the spec (`cfrest.schema.yaml`) is not published; `IBM/IBMStorageVirtualizeRestAPI` is generated client code without the source spec. Fixtures: `ansible-collections/ibm.storage_virtualize` unit tests (44+ canned `ls*` responses). | Mock from the Ansible fixtures; live leg = a short AWS/Azure trial run (or record it with MockServer and replay). |

Cross-cutting: for spec-backed vendors (Aria, NetApp) run a Prism contract
mock in CI as a `CONTAINER_REGISTRY` entry (`stoplight/prism` image) to
catch shape drift, and keep the seeded FastAPI mock for the agent-facing
scenarios (relationship graph, incident window) that a random-data mock
cannot express. For vendors without a spec (Hitachi, Infinidat, IBM), record
one live session (customer instance or cloud trial) with MockServer /
WireMock and replay it.

**Recommended first step for the sandbox loop:** build the Aria mock from the
official spec plus the `vrops-export` captures, seeded with a vSphere
estate and one synthetic storage adapter kind (resource kinds and stat keys
modelled on the Hitachi pack's object list), so the "VM latency → datastore
→ LDEV → pool" RCA path is exercisable end to end without any licence.

## 6c. Connector design — `get_schemas`, `execute_query`, and tools

The customer's ITOM system of record is **ServiceNow**, which we already
connect (`servicenow_client.py`: Table API, JSON query spec, curated
`incident / change_request / problem / cmdb_ci …` catalog, extensible via the
`tables` config). So the RCA loop the planner runs is:

```
ServiceNow incident (what/when/which CI)
  → ServiceNow change_request + em_alert in the window (what changed / what fired)
  → Aria: resource for that CI → relationships → metrics + alerts across layers
  → array connector: the LUN/LDEV/pool behind the datastore, its own events
  → create_doc: symptom → hypotheses → evidence → root cause
```

Every connector below is a `DataSourceClient` with `capabilities = {QUERY}`,
like all existing infra connectors. Results are DataFrames, so the agent
joins across connections in pandas and feeds `create_data` / `create_doc`.
The *tool provider* shape (`custom_api`/MCP via `execute_mcp`) is the wrong
fit: its results are materialised blobs, not frames, and the planner is told
tool providers do not answer `execute_query`.

### 6c.1 `get_schemas()` — the catalog the planner sees

Follow the AppDynamics/Zabbix pattern: a catalog declared in code, enriched
at index time with what the instance actually contains. Two kinds of table:

- **Fixed core tables** (always present, stable columns).
- **Discovered metric tables**, one per *populated* resource kind, named with
  the Splunk `dashboard::app/name` convention so the agent can tell them
  apart: `metrics::<AdapterKind>/<ResourceKind>`. Their columns are the stat
  keys from `/statkeys` (unit + description in the column description). Only
  kinds with ≥1 resource are emitted; admins activate the ones they want in
  the Tables selector. This is the Prometheus connector's "each metric is a
  table" idea, one level up.

**Aria Operations**

| Table | Source | Columns (pk / fks) |
|---|---|---|
| `adapter_kinds` | `/adapterkinds` | key (pk), name, description, resource_kind_count |
| `resource_kinds` | `/adapterkinds/{ak}/resourcekinds` | adapter_kind (fk), key (pk), name, resource_count |
| `stat_keys` | `/adapterkinds/{ak}/resourcekinds/{rk}/statkeys` | adapter_kind, resource_kind, key, name, unit, rollup_type, description |
| `resources` | `/resources` | id (pk), name, adapter_kind, resource_kind, health, status, state, identifiers (json) |
| `properties` | `/resources/{id}/properties` | resource_id (fk→resources), name, value |
| `relationships` | `/resources/{id}/relationships` | parent_id (fk→resources), child_id (fk→resources), parent_kind, child_kind |
| `metrics` | `POST /resources/stats/query` | resource_id (fk), resource_name, resource_kind, stat_key, timestamp (epoch ms), value |
| `metrics_latest` | `/resources/stats/latest` | resource_id, stat_key, timestamp, value |
| `alerts` | `POST /alerts/query` | alert_id (pk), resource_id (fk), definition_id (fk→alert_definitions), level, status, start_utc, update_utc, cancel_utc, impact |
| `symptoms` | `/symptoms` | id, resource_id (fk), definition_id, criticality, message, start_utc, cancel_utc |
| `alert_definitions` | `/alertdefinitions` | id (pk), name, description, adapter_kind, resource_kind, wait_cycles, cancel_cycles |
| `recommendations` | `/recommendations` | id, description |
| `metrics::<AK>/<RK>` (discovered) | stats/query scoped to one kind | resource_id, resource_name, timestamp, + one column per stat key |

Catalog descriptions embed the discovered estate (installed adapter kinds,
resource counts per kind, top-N resource kinds) the same way AppDynamics
embeds application names and flow edges — so the planner knows on read that
"HitachiStorage/Pool" exists before it queries anything.

**NetApp ONTAP** — `clusters, nodes, svms, aggregates, volumes, luns,
qtrees, ip_interfaces, fc_ports, ems_events, counter_tables` plus
`metrics` (object_type + uuid/name + interval → iops/latency/throughput
series). Columns come straight from the REST field names (`space.size`,
`space.used`, `state`, `svm.name`, …) — flatten dotted fields.

**Hitachi** — CMREST: `storage_systems, ldevs, pools, ports, host_groups,
parity_groups, alerts`. Analyzer (present only if configured):
`performance` (object type + metric type + time range), `events`,
`e2e_topology` (edge list from `E2EView/actions/getTopologyData`).

**Infinidat** — `system, capacity, pools, volumes, filesystems, hosts,
host_clusters, replicas, components, events, metrics_live` (collector
create/poll/delete under the hood), and `infinimetrics_series` only if an
InfiniMetrics URL is configured.

**IBM Virtualize** — `system, nodes, pools, mdisks, volumes, hosts, drives,
enclosures, fc_ports, event_log, system_stats, node_stats`. Spectrum
Control (separate type if present): `storage_systems, volumes, ports,
performance (id + granularity + time range), servers, switches`.

**ServiceNow (already exists — configuration, not code)** — add to the
connection's `tables`: `cmdb_rel_ci` (CI topology), `cmdb_ci_vmware_instance`,
`cmdb_ci_esx_server`, `cmdb_ci_vcenter_datastore`, `cmdb_ci_storage_server`,
`cmdb_ci_storage_pool`, `cmdb_ci_storage_volume`, `em_alert`, `em_event`
(ITOM Event Management), `sys_journal_field` (work notes). The client's
`discover_all` mode already walks the `cmdb_ci` hierarchy.

### 6c.2 `execute_query(query)` — the JSON spec

One spec shape per connector, documented in `system_prompt()` exactly as
AppDynamics does, with epoch-millisecond timestamps and a `duration_in_mins`
/ `start_time` / `end_time` window. Aria examples for the RCA path:

```python
# 1. Find the CI from the ServiceNow incident (existing connector)
inc = sn.execute_query('{"table": "incident", "query": "number=INC0012345", "fields": ["cmdb_ci", "opened_at", "short_description"]}')

# 2. Resolve it in Aria and pull its neighbourhood
vm  = aria.execute_query('{"table": "resources", "name": "prod-db-01", "resource_kind": "VirtualMachine"}')
rel = aria.execute_query('{"table": "relationships", "resource_id": "<vm-id>", "relationship_type": "ALL", "depth": 2}')

# 3. Time-aligned metrics across layers, incident window ±30 min
m = aria.execute_query('{"table": "metrics", "resource_id": ["<vm-id>", "<datastore-id>", "<hitachi-ldev-id>"],
                        "stat_key": ["virtualDisk|totalLatency", "datastore|totalLatency", "response_time"],
                        "start_time": 1756860000000, "end_time": 1756863600000, "rollup": "AVG", "interval": "MINUTES", "interval_quantifier": 1}')

# 4. What fired, and what the operators' own thresholds say
a = aria.execute_query('{"table": "alerts", "resource_id": ["<datastore-id>"], "active_only": false, "start_time": ..., "end_time": ...}')
d = aria.execute_query('{"table": "alert_definitions", "adapter_kind": "HitachiStorage"}')

# 5. Discovered metric table — one row per timestamp, stat keys as columns
p = aria.execute_query('{"table": "metrics::HitachiStorage/Pool", "name": "Pool-07", "duration_in_mins": 120}')

# 6. What changed (existing ServiceNow connector)
chg = sn.execute_query('{"table": "change_request", "query": "start_date>=…^end_date<=…^cmdb_ciIN<ids>", "limit": 50}')
```

Spec rules that matter for RCA quality (all learned from the existing infra
connectors):
- **Window is mandatory on activity tables** (`metrics`, `alerts`,
  `symptoms`, `events`); default to the last 60 min, never unbounded.
- **Batch, don't loop**: `resource_id` and `stat_key` accept lists; the
  client chunks at the API's 1000-resource cap.
- **Name or id everywhere** (`resource_id` *or* `name` + `resource_kind`) —
  the agent usually holds a name from ServiceNow, not an Aria id.
- Hard caps: `MAX_ROWS` (50k) and `DEFAULT_LIMIT` (500) as in AppDynamics.
- Same shape for the arrays, e.g. ONTAP
  `{"table": "metrics", "object": "volumes", "name": "vol_prod_db", "interval": "1d"}`,
  Virtualize `{"table": "event_log", "filtervalue": "status=alert", "limit": 200}`,
  InfiniBox `{"table": "events", "filter": {"level": "in:(ERROR,CRITICAL)", "timestamp": "ge:…"}}`.

### 6c.3 Tools — what is beyond `execute_query`

**File, mail, note and browser capabilities do not apply** to any of these
sources: none of them is a folder of documents, and the planner reasons
worse when a metrics API is dressed up as files (the exact lesson recorded
in `base.py` for mail/notes). Keep `{QUERY}`. What *is* worth adding sits in
three places:

1. **Catalog-level "tribal knowledge" tables (no new tool)** — the Splunk /
   Elasticsearch move: `alert_definitions` (operator thresholds), Aria
   **custom groups** and **dashboards/views** (`/dashboards` on 8.x — verify
   on the appliance swagger before promising) become tables whose
   descriptions carry the definition, so the agent replays the operator's
   investigation instead of inventing one. Same for ServiceNow
   `kb_knowledge` runbooks (already a table) and Hitachi Analyzer's E2E
   bottleneck endpoint.
2. **Correlation keys in `system_prompt()` (no new tool)** — the joins across
   connections are the whole RCA. Each prompt must name them:
   - ServiceNow `cmdb_ci_vmware_instance.vm_inst_id` / `morid` ↔ Aria VM
     properties `config|instanceUuid` / `summary|MOID`; VM name as fallback.
   - ServiceNow `cmdb_ci_vcenter_datastore.name` ↔ Aria Datastore name ↔
     array LUN/LDEV via the storage pack relationship or, natively, via the
     datastore's backing NAA id (`summary|datastore|...`) ↔ ONTAP LUN
     `serial_number`, Virtualize `vdisk_UID`, Hitachi LDEV `naaId`, InfiniBox
     volume `serial`.
   - Array serial number ↔ ServiceNow `cmdb_ci_storage_server.serial_number`.
   - Time: ServiceNow `opened_at` is UTC; Aria/ONTAP/Virtualize timestamps
     are epoch ms / ISO / epoch s respectively — state the conversion.
3. **ServiceNow, adjacent and cheap** — two additions to the *existing*
   connector would sharpen RCA more than any storage tool: `READ_FILE` over
   `sys_attachment` on the incident (logs and screenshots attached by the
   operator) and journal fields (`sys_journal_field`) as a table, so work
   notes are readable. Both are Table/Attachment API calls; the
   ServiceNow client today is `{QUERY}` only.

Not recommended: a bespoke "topology" or "rca" tool. The `relationships`
table plus pandas already gives the graph, and a single-purpose tool would
bypass the tracked-query path that `create_data` and dashboards depend on.
## 6d. Source cards — one page per new connector

Consolidates §3–§6c. ServiceNow is included because it is the ITOM system of
record and the entry point of every RCA, even though it needs configuration
rather than code.

### ServiceNow (exists — configure, extend later)
- **Provides:** incidents, problems, changes, CMDB CIs and relationships, ITOM alerts/events, runbooks, work notes, attachments.
- **Integration:** Table API, JSON spec per table (`servicenow_client.py`).
- **Auth:** existing variants (basic / OAuth).
- **Difficulty:** none for the RCA tables — add `cmdb_rel_ci`, `cmdb_ci_vmware_instance`, `cmdb_ci_esx_server`, `cmdb_ci_vcenter_datastore`, `cmdb_ci_storage_server`, `cmdb_ci_storage_pool`, `cmdb_ci_storage_volume`, `em_alert`, `em_event`, `sys_journal_field` to the connection's `tables`. Low for the two extensions below.
- **Test target:** ServiceNow Personal Developer Instance (free) — already how the connector is tested.
- **get_schemas:** curated + `discover_all` over the `task` and `cmdb_ci` hierarchies (exists).
- **execute_query:** `{"table": "incident", "query": "number=INC0012345", "fields": [...]}`; `{"table": "change_request", "query": "start_date>=…^cmdb_ciIN…"}`; `{"table": "em_alert", "query": "initial_remote_time>=…"}`.
- **Tools:** QUERY today. Add `READ_FILE` over `sys_attachment` (incident logs/screenshots) and expose `sys_journal_field` (work notes) — the two highest-value RCA additions across the whole set.
- **RCA use case:** "INC0012345: DB latency on prod-db-01 since 02:10." → incident row gives CI, window, priority; `change_request` in ±4 h on the CI or its parents gives the change hypothesis; `em_alert` gives what ITOM already correlated; `cmdb_rel_ci` gives the VM → host → datastore → storage volume chain to hand to Aria and the array connector.

### VMware Aria Operations 8.18 (`aria_operations`, build first)
- **Provides:** VM/host/cluster/datastore inventory, metrics with history (default 6 months), alerts, symptoms, alert definitions, the cross-layer relationship graph, and everything installed management packs collect (Hitachi pack alive; NetApp/IBM packs EOL but may still run).
- **Integration:** REST Suite API `/suite-api/api`: `adapterkinds → resourcekinds → statkeys`, `resources`, `relationships`, `properties`, `POST resources/stats/query`, `stats/latest`, `POST alerts/query`, `symptoms`, `alertdefinitions`. JSON via `Accept`.
- **Auth:** `POST auth/token/acquire` (username, password, `authSource` LOCAL or LDAP/AD source name) → `Authorization: OpsToken`, 6 h sliding, cached, refresh on 401. `userpass` variant only, per-user scope allowed.
- **Difficulty:** low — AppDynamics shape, plain `requests`, no SDK.
- **Test target:** no container, SaaS terminated. Seeded FastAPI mock in `tools/aria_operations/` built from the official OpenAPI spec (`vmware/vcf-api-specs`) + recorded responses (`vmware-archive/vrops-export`); live leg via the customer or Broadcom TAP NFR on nested ESXi.
- **get_schemas:** core tables (`adapter_kinds, resource_kinds, stat_keys, resources, properties, relationships, metrics, metrics_latest, alerts, symptoms, alert_definitions, recommendations`) + discovered `metrics::<AdapterKind>/<ResourceKind>` tables with stat keys as columns; catalog description lists installed adapter kinds and resource counts.
- **execute_query:** `{"table": "metrics", "resource_id": [...] | "name"+"resource_kind", "stat_key": [...], "start_time"/"end_time" epoch ms or "duration_in_mins", "rollup": "AVG", "interval": "MINUTES"}`; `{"table": "relationships", "resource_id": "...", "relationship_type": "ALL", "depth": 2}`; `{"table": "alerts", "resource_id": [...], "active_only": false, ...window}`; `{"table": "metrics::HitachiStorage/Pool", "name": "Pool-07", "duration_in_mins": 120}`.
- **Tools:** QUERY. Tribal-knowledge tables: `alert_definitions`, custom groups, dashboards/views (verify `/dashboards` on the appliance swagger). Prompt carries correlation keys: `config|instanceUuid` / `summary|MOID` ↔ ServiceNow `vm_inst_id` / `morid`; datastore name; datastore backing NAA id.
- **RCA use case:** from the ServiceNow CI name → `resources` → `relationships` depth 2 (VM → datastore → LUN/LDEV → pool → array, when a pack is installed) → one `metrics` call for VM disk latency, datastore latency, and pool response time over the incident window → `alerts`/`symptoms` in the same window → the layer where the spike starts first is the suspect; `alert_definitions` says what threshold the operators consider abnormal.

### NetApp ONTAP (`netapp_ontap`, build second)
- **Provides:** clusters, nodes, SVMs, aggregates, volumes, LUNs, capacity, ~1 year on-box metric history, EMS events, raw counters.
- **Integration:** REST per cluster `https://<cluster>/api/` (HAL JSON, `fields`, filters, `_links.next`); `/storage/{volumes|aggregates|luns}/{uuid}/metrics?interval=1h|1d|1w|1m|1y`; `/support/ems/events`; `/cluster/counter/tables`. Optional AIQUM (`/api/v2`) for fleet + gateway proxy.
- **Auth:** basic with a read-only REST role; OAuth2 bearer (external IdP, 9.14+); optional client cert. Cap concurrency ≤20 per User-Agent.
- **Difficulty:** low.
- **Test target:** Simulate ONTAP (real image, OVA + licence, partner/customer login) on Workstation or KVM; public OpenAPI spec for a Prism contract mock; Ansible/Harvest fixtures.
- **get_schemas:** `clusters, nodes, svms, aggregates, volumes, luns, qtrees, ip_interfaces, fc_ports, ems_events, counter_tables, metrics`; columns from REST field names (flattened `space.size`, `space.used`, `state`, `svm.name`).
- **execute_query:** `{"table": "volumes", "filter": {"svm.name": "svm_prod", "state": "online"}, "fields": [...]}`; `{"table": "metrics", "object": "volumes", "name": "vol_prod_db", "interval": "1d"}`; `{"table": "ems_events", "filter": {"message.severity": "alert|error", "time": ">=2026-09-02T02:00:00Z"}}`.
- **Tools:** QUERY. Prompt carries: LUN `serial_number` ↔ datastore NAA id; `volume.name`/`svm.name` ↔ ServiceNow storage volume CI; EMS `time` is ISO-8601 with offset.
- **RCA use case:** datastore `ds_prod_01` sits on LUN `/vol/vol_prod_db/lun0` → `metrics` for that volume and its aggregate at 5-min granularity over the day → `ems_events` in the window (e.g. `wafl.vol.full`, `nvmf.*`, disk errors) → is the latency on the volume only (noisy neighbour on the aggregate?) or on the node (`cluster/metrics`)? Pair with ServiceNow `change_request` on the storage CI.

### Hitachi VSP (`hitachi_ops_center`)
- **Provides:** Configuration Manager — storage systems, LDEVs, pools, ports, host groups, parity groups, alerts. Analyzer (if licensed) — performance history, events, E2E topology / bottleneck analysis.
- **Integration:** CMREST `:23451/ConfigurationManager/v1/objects/storages/{id}/…` (or embedded on the array); Analyzer `:22016/Analytics/v1/objects/{PerformanceVolume|PerformanceNode|Events}` with HQL `$query`, and `services/E2EView/actions/getTopologyData`.
- **Auth:** CMREST session token from basic (`Authorization: Session`); Analyzer basic / HSSO token / OIDC bearer from Common Services.
- **Difficulty:** medium — two endpoints, two auth schemes, Analyzer optional.
- **Test target:** none public; hand-built mock from the public MK-99CFM000 / MK-99ANA003 guides; live leg via the customer or a Hitachi partner lab (HALO).
- **get_schemas:** `storage_systems, ldevs, pools, ports, host_groups, parity_groups, alerts` (+ `performance, events, e2e_topology` when an Analyzer URL is configured).
- **execute_query:** `{"table": "ldevs", "storage_device_id": "900000012345", "filter": {"poolId": 7}, "count": 1000}`; `{"table": "performance", "object": "PerformanceVolume", "metric_type": "RAID_VOLUME_RAIDLDEV_TOTALIOPS", "base_point_node_id": "...", "time_range": "..."}`; `{"table": "e2e_topology", "base_point": "<ldev-id>"}`.
- **Tools:** QUERY. Tribal knowledge: Analyzer's E2E view is the vendor's own RCA graph — expose as a table, not a tool. Prompt carries: LDEV `naaId` ↔ datastore NAA id; array `serialNumber` ↔ ServiceNow storage server CI.
- **RCA use case:** datastore NAA → LDEV → pool → parity group; `performance` for LDEV response time and MP busy over the window; `alerts` (DKC/CTL) in the window; `e2e_topology` from the LDEV to see which hosts share the same port/pool and whether one of them ramped up at 02:10.

### Infinidat InfiniBox (`infinidat_infinibox`, last)
- **Provides:** system, capacity, pools, volumes, filesystems, hosts, replicas, components, events; live performance sampling; history only via InfiniMetrics.
- **Integration:** REST `/api/rest/…` with `field=op:value` filters, `page/page_size` (≤1000); metrics via `POST metrics/collectors` → poll `collectors/data` → `DELETE`; optional InfiniMetrics `/api/rest/systems/{serial}/monitored_entities/{id}/data/` (community-documented).
- **Auth:** session login (`users/login`, JSESSIONID) or basic; `read_only` role.
- **Difficulty:** low for inventory/events, high for history.
- **Test target:** none public (`infinisim` is internal); mock from `infinisdk` source shapes; ask the Lenovo partner program.
- **get_schemas:** `system, capacity, pools, volumes, filesystems, hosts, host_clusters, replicas, components, events, metrics_live` (+ `infinimetrics_series` if configured).
- **execute_query:** `{"table": "volumes", "filter": {"pool_id": "eq:3", "type": "eq:MASTER"}, "fields": [...]}`; `{"table": "events", "filter": {"level": "in:(ERROR,CRITICAL)", "timestamp": "ge:2026-09-02T02:00:00Z"}}`; `{"table": "metrics_live", "protocol": "SAN", "fields": ["ops", "throughput", "external_latency_wout_err"], "seconds": 30}`.
- **Tools:** QUERY. Prompt carries: volume `serial` ↔ datastore NAA id; `system.serial_number` ↔ ServiceNow CI; collector semantics (live only) so the agent does not promise history it cannot get.
- **RCA use case:** capacity/events-led — `events` in the window (pool threshold, node failover, drive errors), `pools` physical vs virtual capacity, `replicas` state (a sync replica catching up during the incident?), and `metrics_live` to confirm whether the problem is still ongoing now; historical latency comes from Aria's pack or InfiniMetrics.

### IBM FlashSystem / SVC (`ibm_storage_virtualize`; `ibm_spectrum_control` if deployed)
- **Provides:** system, nodes, pools, mdisks, volumes, hosts, drives, enclosures, FC ports, event log, current stats. History: Spectrum Control (on-prem) — per-volume performance with `granularity`, multi-vendor.
- **Integration:** `POST https://<cluster>:7443/rest/v1/{lssystem, lsvdisk, lsmdiskgrp, lshost, lsnode, lsdrive, lseventlog, lssystemstats, lsnodestats}` (POST-only, `filtervalue`/`limit` in the body). Spectrum Control `:9569/srm/REST/api/v1/…` with `Volumes/{id}/Performance?granularity=sample&startTime=&endTime=`.
- **Auth:** Virtualize `POST /rest/v1/auth` with `X-Auth-Username`/`X-Auth-Password` → `X-Auth-Token` (1 h, 403 on expiry, monitor role). Spectrum Control form login `j_security_check` + cookie.
- **Difficulty:** medium.
- **Test target:** IBM Storage Virtualize for Public Cloud 60-day trial (AWS/Azure, real code base) for one recorded session; mock from `ibm.storage_virtualize` Ansible fixtures.
- **get_schemas:** `system, nodes, pools, mdisks, volumes, hosts, drives, enclosures, fc_ports, event_log, system_stats, node_stats`; Spectrum Control: `storage_systems, volumes, ports, performance, servers, switches`.
- **execute_query:** `{"table": "volumes", "filtervalue": "mdisk_grp_name=Pool0", "limit": 500}`; `{"table": "event_log", "filtervalue": "status=alert", "limit": 200}`; `{"table": "system_stats", "history": true}`; Spectrum Control `{"table": "performance", "volume_id": 1234, "granularity": "sample", "start_time": ..., "end_time": ...}`.
- **Tools:** QUERY. Prompt carries: `vdisk_UID` ↔ datastore NAA id; `lssystem.id`/name ↔ ServiceNow CI; on-box history limit (16 files) so the agent prefers Spectrum Control or Aria for the timeline.
- **RCA use case:** datastore NAA → `volumes` (`vdisk_UID`) → pool (`mdisk_grp_name`) → `event_log` alerts in the window (e.g. `1920` mirror sync, `1370` drive failure, cache/battery events) → `node_stats` now vs baseline; timeline from Spectrum Control `performance` if present, else from Aria's pack.

### Cross-source RCA scenario (what "done" looks like)
"prod-db-01 slow since 02:10" → ServiceNow incident + CI chain → Aria relationships and a single time-aligned metrics frame across VM, datastore and array pool → the array connector's own event log for that pool → ServiceNow change requests on any CI in the chain → `create_doc`: symptom chart, hypotheses (change, noisy neighbour, array event, capacity), evidence per hypothesis with citations, root cause, confidence, and the operators' own thresholds from `alert_definitions` as the yardstick.
## 6e. Aria Operations — the build plan (verified against the spec)

### What was read, and what changed

Primary sources read directly for this section (not via summaries):
- Broadcom **8.18 API Programming Guide** pages: *Acquire an Authentication
  Token* (confirms `POST /suite-api/api/auth/token/acquire`, body
  `username/password/authSource`, response `token/validity/expiresAt/roles`,
  headers `Authorization: OpsToken <token>` with `vRealizeOpsToken` still
  accepted, six-hour expiry, `authSource` default `LOCAL`), *Generate a List
  of All Metrics* (confirms `GET /adapterkinds/{ak}/resourcekinds/{rk}/statkeys`
  → `resourceTypeAttributes[{key,name,description,unit,rollupType,
  instanceType,dataType2,defaultMonitored}]`, `Accept: application/json`),
  and *Using the API* (swagger at `/suite-api/doc/swagger-ui.html`).
- The **official OpenAPI 3.0.1 spec** `vmware/vcf-api-specs` →
  `vcf-operations-openapi.json` (VCF Operations 9.1.0.0, 343 paths, same
  Suite API; saved as a scratch copy during research). Every endpoint,
  parameter and schema below is taken from it.

Two corrections to §6c:
1. **There is no event *read* API.** `/api/events` and `/api/events/bulk` are
   `POST` only ("Push a single Event into the system"). Drop the `events`
   table; alerts + symptoms are the timeline.
2. **No `/api/dashboards` in the public API.** Drop the dashboards table.
   The replayable operator knowledge that *does* exist: `alertdefinitions`,
   `symptomdefinitions`, custom groups (`/api/resources/groups`), and
   dynamic thresholds (below).

Two additions the spec makes obvious for RCA:
- **Dynamic thresholds**: `stats/query` takes `"dt": true` and the response
  `stat-list.stat[]` then carries `minThresholdData[]` / `maxThresholdData[]`
  / `dtTimestamps[]` beside `timestamps[]` / `data[]` — i.e. Aria's own
  "normal band" per metric. "Was this abnormal?" becomes a column, not a
  judgement call. (`GET /resources/{id}/stats/dt` is the single-resource form.)
- **Top-N**: `GET /resources/stats/topn?topN=&groupBy=RESOURCE|STATKEY&
  sortOrder=&resourceId[]&statKey[]&begin&end&rollUpType` → "which 10
  datastores had the worst latency in the window" in one call.

### Endpoint → table map (final)

| Table | Call | Request (from spec) | Rows produced |
|---|---|---|---|
| `adapter_kinds` | `GET /adapterkinds?retrieveResourceKindInfos=true` | — | `adapter-kind[]`: key, name, description, adapterKindType, resourceKinds[] |
| `resource_kinds` | `GET /adapterkinds/{key}/resourcekinds` (page/pageSize 1000) | `resourceKindType` filter | `resource-kind[]`: key, name, adapterKind, resourceKindType |
| `stat_keys` | `GET /adapterkinds/{ak}/resourcekinds/{rk}/statkeys?statOnly=true` | — | `resourceTypeAttributes[]`: key, name, unit, rollupType, instanceType, dataType2, description |
| `resources` | `POST /resources/query` (page/pageSize) | `resource-query`: name[], regex[], adapterKind[], resourceKind[], parentId[], resourceId[], resourceHealth[], resourceState[], resourceStatus[], propertyConditions{conditions[{key,operator EQ/LIKE/REGEX/…,stringValue}]} | `resourceList[]` flattened: identifier, resourceKey.name, adapterKindKey, resourceKindKey, resourceHealth, resourceHealthValue, resourceStatusStates, badges, creationTime |
| `properties` | `GET /resources/{id}/properties` or `POST /resources/properties/latest/query` `{resourceIds[], propertyKeys[]}` | — | resourceId, name, value |
| `relationships` | `GET /resources/{id}/relationships?relationshipType=PARENT|CHILD|ALL` | client-side BFS to `depth` | parent_id, child_id, + both resourceKeys |
| `metrics` | `POST /resources/stats/query` | `{resourceId[], statKey[], begin, end (epoch ms), rollUpType, intervalType, intervalQuantifier, dt}` | one row per (resourceId, statKey, timestamp): value, and when `dt`: dt_min, dt_max |
| `metrics_latest` | `POST /resources/stats/latest/query` | `{resourceId[], statKey[], maxSamples}` | resourceId, statKey, timestamp, value |
| `metrics_topn` | `GET /resources/stats/topn` | topN, groupBy, sortOrder, resourceId[], statKey[], begin, end, rollUpType | rank, resourceId, statKey, value |
| `alerts` | `POST /alerts/query` | `{activeOnly, alertCriticality[], alertStatus[], alertDefinitionId[], startTimeRange{startTime,endTime}, cancelTimeRange, resource-query{…}, includeChildrenResources}` | alertId, resourceId, alertDefinitionId, alertDefinitionName, alertLevel, status, controlState, alertImpact, startTimeUTC, updateTimeUTC, cancelTimeUTC, statKey |
| `contributing_symptoms` | `GET /alerts/contributingsymptoms?id[]` | — | alertId, symptom rows |
| `symptoms` | `POST /symptoms/query` | `{activeOnly, alarmCriticality[], cancelTimeRange, resource-query{…}}` | id, resourceId, symptomDefinitionId, symptomCriticality, message, kpi, statKey, startTimeUTC, cancelTimeUTC |
| `alert_definitions` | `POST /alertdefinitions/query` | `{adapterKinds[], resourceKinds[], ids[]}` | id, name, description, adapterKindKey, resourceKindKey, type, subType, waitCycles, cancelCycles |
| `custom_groups` | `GET /resources/groups` + `/groups/{id}/members` | — | group id, name, member resourceId |
| `metrics::<AK>/<RK>` (discovered) | resources by kind → stats/query | window + optional name/regex | one row per (resource, timestamp), stat keys as columns |

Response conventions the client must handle: pagination via
`pageInfo.totalCount` + `page`/`pageSize` (max 1000); `stat-list.stat[]`
carries parallel arrays `timestamps[]`/`data[]` (plus `values[]` for string
stats); all times epoch **ms**; enums as above (`alertLevel`
INFORMATION/WARNING/IMMEDIATE/CRITICAL, `resourceHealth` GREEN…GREY).

Endpoints the 9.1 spec has that must be **confirmed on the customer's 8.18
swagger** before we rely on them (all existed in 7.x/8.x per community
usage, but the 8.18 guide does not enumerate them): `POST /resources/query`,
`POST /symptoms/query`, `POST /alertdefinitions/query`, `stats/topn`,
`stats/dt`, `/resources/groups`. Fallbacks exist for each (`GET /resources`
with query params, `GET /symptoms`, `GET /alertdefinitions`).

### Phases

**Phase 0 — customer inputs (no code).** Ask for: the `GET /adapterkinds`
JSON from their instance (tells us which packs are live and gives real
adapter/resource kind keys for the mock), the `authSource` names, a
read-only local or LDAP account, and `GET /suite-api/doc/v2/api-docs?group=Public%20APIs`
(their exact 8.18 OAS2) to diff against the 9.1 spec.

**Phase 1 — mock Suite API** (`tools/aria_operations/mock_suite_api.py`,
FastAPI, `docker-compose.yaml` on port 8443, same conventions as
`tools/appdynamics/mock_controller.py`):
- Implements exactly the table-map endpoints above, shapes from the spec.
- Faithfulness rules enforced on purpose: `Authorization: OpsToken|vRealizeOpsToken`
  required (401 otherwise); tokens expire (`ARIA_MOCK_TOKEN_TTL`, default
  21600 s) so refresh-on-401 is observable; without `Accept: application/json`
  answer XML; `pageSize` capped at 1000; `stats/latest` rejects >1000 ids;
  all timestamps epoch ms.
- Seeded estate: `VMWARE` adapter (vCenter → Datacenter → Cluster → Host →
  VM, Datastore) plus one synthetic storage adapter modelled on the Hitachi
  pack (`HitachiStorage`: StorageSystem → Pool → LDEV, LDEV ↔ Datastore
  relationship), ~40 resources, stat keys with units, and an **incident
  scenario**: pool response time rises at T0, datastore latency at T0+2 min,
  VM disk latency at T0+3 min; a CRITICAL alert on the pool and a WARNING
  symptom on the datastore with matching `startTimeUTC`; dynamic-threshold
  bands that the spike exits. This is the RCA the sandbox loop must solve.

**Phase 2 — client** (`backend/app/data_sources/clients/aria_operations_client.py`):
- Config `AriaOperationsConfig`: `url`, `auth_source` (default `LOCAL`),
  `verify_ssl`, `ca_bundle` (optional path), `history_window_days` (default 7,
  as Zabbix), `max_metric_tables` (cap on discovered `metrics::` tables).
  Credentials `AriaOperationsUserPassCredentials` (`username`, `password`),
  `scopes=["system","user"]`.
- `_token()` cached with `expiresAt`, refresh on 401; `_get/_post` with
  `Accept: application/json`, page loop, `HTTP_TIMEOUT`.
- `test_connection()` = token acquire + `GET /adapterkinds` → "Connected.
  Found N adapter kinds."
- `get_schemas()` = fixed tables + discovered `metrics::` tables, with the
  estate summary in the catalog descriptions; `progress_callback` supported
  (one call per populated resource kind).
- `execute_query(spec)` dispatch per the table map; `name`/`regex`+`resource_kind`
  resolved through `/resources/query`; ids chunked at 1000; `dt` flag;
  `depth` BFS for relationships; DataFrame out; `MAX_ROWS`/`DEFAULT_LIMIT`.
- `system_prompt()`: spec examples for the RCA path (§6c.2), epoch-ms note,
  correlation keys (`config|instanceUuid`, `summary|MOID`, datastore name,
  NAA id property), and "prefer `dt: true` to decide abnormal vs normal".
- Registry: `category="infra"`, explicit `client_path`, `version="beta"`,
  `requires_license="enterprise"` (as zabbix/splunk). Icon + `DataSourceIcon.vue`.
- No new dependency (`requests` only).

**Phase 3 — tests.** `tests/unit/test_aria_operations_client.py` mocking the
`requests` boundary (token refresh on 401, pagination, stat-list flattening,
dt columns, chunking at 1000, XML-without-Accept failure); `ds_clients.py`
remote-mode entry pointing at the mock; `tests/e2e/test_data_source.py` and
`test_connection.py` still green.

**Phase 4 — sandbox feedback loop** (skill `sandbox-feedback-loop`): boot
stack + mock + real LLM; prompts: (a) "map the storage behind VM prod-db-01",
(b) "why did prod-db-01 disk latency spike at T0?" — expected: agent walks
relationships, pulls one `metrics` frame with `dt: true` across the three
layers, finds the pool alert, writes the doc naming the pool; (c) "top 5
datastores by latency last 24 h" via `metrics_topn`. Record as
`docs/feedback-loops/aria-operations-connector.md` with screenshots.

**Phase 5 — live validation** on the customer's 8.18: run the connection
test, the schema index, and prompt (b) against a real past incident; diff
their OAS2 against our assumptions; confirm the six "verify" endpoints.

**Deliverable split:** PR 1 = mock + client + schemas + registry + unit
tests; PR 2 = feedback loop, docs site page, changelog. Rough size: client
~800 lines (AppDynamics is 760), mock ~500 (AppDynamics mock is 526).

## 7. Open questions for the customer

- Which Aria management packs are installed? (One `GET /adapterkinds` call.)
- Which `authSource` names exist; can they create a local/LDAP read-only
  service account? Any plan to upgrade to VCF Operations 9.x?
- NetApp: ONTAP version(s), AIQUM present?, Harvest/Prometheus present?
- Hitachi: do they own **Ops Center Analyzer** (licensed per array)? Which
  VSP models (on-box REST needs E/G/F/5000/One Block; older arrays need the
  CMREST server)? Is the Hitachi Aria pack installed (implies Analyzer)?
- Infinidat: InfiniMetrics deployed? InfiniBox software version?
- IBM: FlashSystem/SVC code level (REST needs ≥8.1.3; `/rest/v1` ≥8.4.2)?
  **Is Spectrum Control deployed** (and does it also monitor the Hitachi /
  NetApp arrays)? Is the 16-file on-box stats retention acceptable, or is
  history expected?
- Deployment: which management networks/ports can the BOW host reach; are
  the management certs private-CA (so we can ship a CA bundle option rather
  than `verify_ssl=false`)?

---

## 8. Method note

Five parallel web-research passes (one per product) against official vendor
docs, followed by a read of our own connector layer. Where a vendor portal
blocked unauthenticated fetches (Hitachi support portal, support.infinidat.com,
several JS-rendered IBM Docs pages, docs.netapp.com — mirrored via the
NetAppDocs GitHub org), the finding is marked as such. Nothing was run
against a live system.

## 9. Sources

### VMware Aria Operations
- https://techdocs.broadcom.com/us/en/vmware-cis/aria/aria-operations/8-18/vmware-aria-operations-api-programming-guide-8-18/getting-started-with-the-api/acquire-an-authentication-token.html
- https://techdocs.broadcom.com/us/en/vmware-cis/aria/aria-operations/8-18/vmware-aria-operations-api-programming-guide-8-18/understanding-the-vr-ops-api/using-the-api-with-vrealize-operations-manager.html
- https://techdocs.broadcom.com/us/en/vmware-cis/aria/aria-operations/8-18/vmware-aria-operations-api-programming-guide-8-18/getting-started-with-the-api/generate-a-list-of-all-metrics-for-the-object.html
- https://techdocs.broadcom.com/us/en/vmware-cis/aria/aria-operations/8-18/vmware-aria-operations-818-release-notes.html
- https://techdocs.broadcom.com/us/en/vmware-cis/aria/aria-operations/8-18/vmware-aria-operations-8187-release-notes.html
- https://developer.broadcom.com/xapis/vcf-operations-api/latest/ (resources, stats/query, stats/latest, properties, relationships, alerts/query, symptoms, alertdefinitions, recommendations, adapterkinds)
- https://knowledge.broadcom.com/external/article?legacyId=77271 (basic auth off by default)
- https://knowledge.broadcom.com/external/article/438994/upgrade-of-aria-operations-8186-or-8187.html
- https://knowledge.broadcom.com/external/article/381187/product-lifecycle-matrix-for-aria-suite.html
- https://techdocs.broadcom.com/us/en/vmware-cis/vcf/vcf-9-0-and-later/9-0/overview-of-vmware-cloud-foundation-9/what-is-vmware-cloud-foundation-and-vmware-vsphere-foundation/vcf-operations-overview.html
- https://knowledge.broadcom.com/external/article/373307/broadcom-is-announcing-end-of-general-su.html (NetApp MP EOGS Oct 2024)
- https://knowledge.broadcom.com/external/article/326446/aria-operations-management-packs-that-ar.html (IBM SVC MP EOGS Dec 2023)
- https://blogs.vmware.com/cloud-foundation/2025/09/30/vcf-operations-management-packs-end-of-general-support/
- https://docs.hitachivantara.com/api/khub/documents/5KckFgKsPLbXGC5dsUEG_A/content (Hitachi MP for VMware Operations, Feb 2025)
- https://www.ibm.com/docs/en/spectrum-connect/3.11.0?topic=requirements-supported-storage-systems
- https://github.com/vmware-skills/VMware-Aria ; https://vrabbi.cloud/post/bringing-the-power-of-mcps-to-the-vi-admins/ (community MCPs)
- https://endoflife.date/vmware-cloud-foundation (community lifecycle)

### Test targets (§6b)
- https://github.com/vmware/vcf-api-specs ; https://github.com/vmware-archive/vrops-export ; https://github.com/imtrinity94/VMware-REST
- https://knowledge.broadcom.com/external/article/369262/vmware-aria-operations-818-sizing-guidel.html
- https://techdocs.broadcom.com/us/en/vmware-cis/aria/aria-operations/8-18/vmware-aria-operations-configuration-guide-8-18/about-vmware-aria-operation-licenses.html (eval mode)
- https://knowledge.broadcom.com/external/article/309138/vmware-end-of-availability-of-perpetual.html (Aria SaaS EoA)
- https://tap.broadcom.com/ ; https://labs.hol.vmware.com/HOL/catalog/lab/26851
- https://raw.githubusercontent.com/vmware/vcf-api-specs/main/specifications/vcf-operations/vcf-operations-openapi.json (read directly for §6e)
- https://kb.netapp.com/Support/NSS/Support_Site/Who_can_access_or_download_ONTAP_Simulator ; https://github.com/tcler/ontap-simulator-in-kvm
- https://github.com/NetAppDocs/ontap-restapi/tree/main/swagger-ui ; https://github.com/ansible-collections/netapp.ontap/tree/main/tests/unit/plugins/modules ; https://github.com/NetApp/harvest/tree/main/cmd/collectors/rest/testdata
- https://github.com/NetAppDocs/ontap-select/blob/main/access-evaluation-software.adoc
- https://github.com/Infinidat/infinisdk/blob/master/doc/events.rst.doctest_context (infinisim usage)
- https://www.hitachivantara.com/en-us/gated-forms/free-trial-of-vsp-one-software-defined-storage ; https://aws.amazon.com/marketplace/pp/prodview-zkfpafpjrns7e
- https://community.ibm.com/community/user/discussion/ibm-svc-trial (no simulator) ; https://marketplace.microsoft.com/en-us/product/ibm-alliance-usa-ny-armonk-hq-ibmstorage-6201192.ibm-svpc-trial-azure?tab=overview ; https://aws.amazon.com/marketplace/reviews/reviews-list/prodview-dolkptipf2ovk
- https://github.com/ansible-collections/ibm.storage_virtualize/tree/develop/tests/unit/plugins/modules ; https://www.ibm.com/support/pages/support-free-90-day-trial-ibm-spectrum-control
- https://stoplight.io/open-source/prism ; https://mockoon.com/cli/ ; https://www.mock-server.com/where/docker.html

### NetApp
- https://docs.netapp.com/us-en/ontap-automation/workflows/prepare_workflows.html
- https://docs.netapp.com/us-en/ontap/authentication/oauth2-rest-api.html
- https://docs.netapp.com/us-en/ontap-restapi/getting_started_with_the_ontap_rest_api.html
- https://docs.netapp.com/us-en/ontap-automation/rest/input_variables.html
- https://docs.netapp.com/us-en/ontap-restapi/get-storage-volumes-metrics.html
- https://docs.netapp.com/us-en/ontap-automation/rest/performance_metrics.html
- https://docs.netapp.com/us-en/ontap-restapi/cluster_counter_tables_endpoint_overview.html
- https://docs.netapp.com/us-en/ontap-restapi/support_ems_events_endpoint_overview.html
- https://kb.netapp.com/on-prem/ontap/DM/REST-API/REST_API_KBs/Deferral_of_ONTAPI_ZAPI_End_of_Availability
- https://kb.netapp.com/on-prem/ontap/DM/REST-API/REST_API_KBs/API_call_to_ONTAP_returns_exceeds_configured_session_limit
- https://docs.netapp.com/us-en/active-iq-unified-manager/api-automation/concept_metrics_apis.html
- https://docs.netapp.com/us-en/active-iq-unified-manager/api-automation/concept_events_api.html
- https://docs.netapp.com/us-en/active-iq-unified-manager/api-automation/concept_gateway_apis.html
- https://github.com/NetApp/harvest ; https://netapp.github.io/harvest/latest/prepare-cdot-clusters/ ; https://netapp.github.io/harvest/latest/mcp/overview/
- https://github.com/NetApp/ontap-mcp ; https://netapp.github.io/ontap-mcp/latest/tools/
- https://pypi.org/project/netapp-ontap/
- https://docs.netapp.com/us-en/ontap-apps-dbs/vmware/vmware-vsphere-vtools.html

### Infinidat
- https://infinisdk.readthedocs.io/en/latest/getting_started.html ; https://infinisdk.readthedocs.io/en/latest/efficient_querying.html
- https://github.com/Infinidat/infinisdk (core/api/api.py, core/field_filter.py, core/events.py, infinibox/*.py)
- https://github.com/Infinidat/infinibox-csi-driver/blob/develop/metrics/performancemetrics.go (metrics collectors)
- https://github.com/Infinidat/api_7_3 (Postman collection)
- https://github.com/Infinidat/ansible-infinidat-collection
- https://repo.infinidat.com/packages/main-stable/index/packages/infinibox-management-pack-for-vrops
- https://www.infinidat.com/en/products-technology/infinimetrics ; https://www.infinidat.com/en/products-technology/infiniverse
- https://github.com/viawest/infinibox-graphite ; https://github.com/vkostr/InfiniBox-Zabbix-integration (community InfiniMetrics API usage)
- https://news.lenovo.com/pressroom/press-releases/lenovo-completes-acquisition-of-infinidat-expanding-enterprise-storage-portfolio-enhancing-ai-driven-data-infrastructure/

### Hitachi
- https://docs.hitachivantara.com/r/en-us/mk-99cfm000/latest (Configuration Manager REST, public)
- https://docs.hitachivantara.com/r/en-us/ops-center-analyzer/10.8.x/mk-99ana003 (Analyzer REST, public)
- https://docs.hitachivantara.com/r/en-us/mk-99ana003/latest/overview/common-specifications-of-the-api-functions/security-and-authentication
- https://docs.hitachivantara.com/r/en-us/mk-99ana003/latest/overview/common-specifications-of-the-api-functions/hql-syntax-relationships-and-operators
- https://docs.hitachivantara.com/r/en-us/mk-99ana003/latest/getting-a-list-of-metrics/getting-a-list-of-metrics-for-volumes
- https://docs.hitachivantara.com/r/en-us/mk-99ana003/latest/performing-operations-related-to-event-information/getting-a-list-of-events
- https://knowledge.hitachivantara.com/Documents/Management_Software/Ops_Center/Analyzer/10.5.x/Ops_Center_Analyzer_REST_API_resources/06_Performing_operations_related_to_resource_information_in_E2E_View
- https://knowledge.hitachivantara.com/Documents/Management_Software/Ops_Center/10.8.x/Analyzer/10.2.x/Ops_Center_Analyzer_REST_API_resources/36_Accessing_RAID_Agent
- https://knowledge.hitachivantara.com/Documents/Management_Software/Ops_Center/API_Configuration_Manager/10.0.x/REST_API_Reference_Guide/18_Monitoring_storage_systems (alerts)
- https://knowledge.hitachivantara.com/Documents/Management_Software/Ops_Center/API_Configuration_Manager/10.2.x/REST_API_Reference_Guide/Volume_allocation/04_Getting_volume_information (ldevs paging)
- https://docs.hitachivantara.com/r/en-us/mk-99ana004/latest (Analyzer detail view REST)
- https://docs.hitachivantara.com/r/en-us/ops-center-api-configuration-manager/11.0.x/mk-99cfm000/running-vsp-one-block-administrator-rest-api-requests-for-vsp-one-b20-storage-systems
- https://github.com/hitachi-vantara/vspone-block-ansible ; https://github.com/pascalhubacher/HitachiBlockAPI
- https://download.hitachivantara.com/download/epcra/adptr0825.pdf (Hitachi MP for VMware Operations user guide)
- https://knowledge.hitachivantara.com/Documents/Storage/VSP_E_Series/93-06-0x/System_Management_Using_Embedded_Interfaces/07_Exporting_storage_system_performance_information (Export Tool)
- https://stor2rrd.com/Hitachi-VSPG-REST_API.php ; https://xormon.com/storage/monitoring/Hitachi/Hitachi-VSPG-VSP-HUS-AMS-HNAS-monitoring.php (community)
- https://docs.hitachivantara.com/r/en-us/mk-99cls000/latest/clear-sight-overview

### IBM
- https://www.ibm.com/docs/en/STKMQV_8.1.3/com.ibm.storage.vflashsystem9000.8.1.3.doc/Spectrum_Virtualize_API_8.1.3.pdf
- https://barrywhytestorage.blog/2020/08/03/tips-and-tricks-using-the-spectrum-virtualize-rest-api/
- https://github.com/IBM/IBMStorageVirtualizeRestAPI/blob/main/IBM_REST_SDK_USAGE_GUIDE.md
- https://www.ibm.com/docs/en/flashsystem-9x00/8.7.x?topic=svra-storage-virtualize-performance-statistics
- https://www.ibm.com/docs/en/svfpc/8.5.x?topic=monitoring-retrieving-statistics-files
- https://www.ibm.com/docs/en/announcements/storage-virtualize-910-delivers-additional-flashsystem-grid-enhancements-improved-operational-resilience
- https://github.com/IBM/spectrum-virtualize-exporter
- https://www.ibm.com/docs/en/storage-insights?topic=configuring-rest-api
- https://www.ibm.com/docs/en/storage-insights?topic=new-change-history
- https://insights.ibm.com/restapi/docs/
- https://github.com/IBM/ibm-storageinsights-mcpserver ; https://www.ibm.com/docs/en/storage-insights?topic=ecosystem-storage-insights-model-context-protocol-mcp-server
- https://github.com/IBM/ibm-flashsystems-mcpserver (management, not read-only)
- https://www.ibm.com/docs/en/spectrum-control/5.4.12?topic=ra-retrieve-data-by-using-rest-apis-web-browser
- https://www.ibm.com/support/pages/ibm-spectrum-control-and-ibm-storage-insights-pro-support-pure-storage
- https://www.ibm.com/docs/en/spectrum-connect/3.10.0?topic=environment-storage-management-pack-vmware-vrealize-operations-manager
- https://techdocs.broadcom.com/us/en/vmware-cis/aria/aria-operations/8-17-1/management-pack-for-ibm-svc-and-storwize.html
- https://github.com/ansible-collections/ibm.storage_virtualize ; https://github.com/IBM/pyds8k
