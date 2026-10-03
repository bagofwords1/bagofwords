# NetApp ONTAP 9.14.1P9 root-cause investigation connector — implementation plan

Revised: 2026-09-13. Status: approved design, now implemented with simulated-API verification; see [the feedback loop](../feedback-loops/netapp-ontap-connector.md) for the delivered scope and observed results. Development does not require a lab. Production acceptance requires customer-run validation.

Build a native `netapp_ontap` connector whose purpose is **root-cause analysis of the customer's ONTAP infrastructure**. An investigator must discover relevant diagnostic resources through `get_tables`, inspect their schemas, and retrieve evidence through `execute_query`. The initial 36 curated tables are a convenient entry point, not the coverage ceiling. Use ONTAP REST directly; the official MCP implementation is reference code, never a runtime dependency.

Proceed without AWS, a simulator, a NetApp account, or paid verification infrastructure. Develop against the frozen 9.14.1 Swagger with meaningful HTTP-boundary tests and explicitly synthetic fixtures. Supply a customer-run read-only validation command before production acceptance. Mock success is not proof of 9.14.1P9 compatibility.

## Goal and definition of coverage

The connector must support investigating latency, throughput degradation, capacity exhaustion, hardware faults, connectivity and access failures, replication failures, and relevant configuration changes. It retrieves evidence; it must not infer a proven cause merely from correlated metrics or an EMS event.

“Query everything” means account for the complete version-specific diagnostic surface and expose all reviewed passive diagnostic resources supported by the target platform and credentials. It does not mean permit arbitrary methods, raw file/LUN contents, credentials, state-changing GET controls, or unbounded production crawls. Private CLI-only diagnostic gaps must be named and assessed individually; a missing diagnostic family cannot disappear behind a successful connection test.

Track two independent statuses for every resource: implementation coverage (`queryable`, `special_adapter_required`, `excluded_with_reason`) and observed device availability (`unverified`, `available`, `empty`, `permission_denied`, `unsupported`, `not_configured`, `transient_error`). A Swagger entry proves a documented contract, not device availability. Absence of results is never automatically evidence that a fault or resource does not exist.

Required RCA evidence families:

| Family | Evidence and investigation purpose |
|---|---|
| Identity and topology | Cluster, node, SVM, volume, aggregate and protocol relationships; locate the affected path |
| Performance and QoS | Current metrics, retained series, counter definitions/samples and policy limits; distinguish workload pressure from shared-resource symptoms |
| Capacity | Logical/physical/provisioned space, snapshots, quotas, efficiency and tiering; identify the exhausted resource without double counting |
| Hardware and network | Disks, shelves, sensors, HA, Ethernet/IP/FC interfaces, routes and exposed fabric observations |
| SAN, NAS, NVMe and S3 | Mappings, initiators, sessions and protocol configuration where exposed; explain access and service failures |
| Protection | SnapMirror relationships/transfers, snapshot policies, consistency groups and relevant MetroCluster state |
| Events and changes | EMS, jobs, exposed audit records and configuration evidence; construct an incident timeline |
| Name services and diagnostic security state | DNS/name-service configuration, export/access policies and relevant non-secret security observations |

All families require an explicit coverage decision in the release report. Deployment order may be incremental, but “36 tables complete” is not the broad-RCA acceptance criterion. Host, application and Brocade evidence remain separate sources; retain correlation identifiers without claiming ONTAP alone can prove their behavior.

## Evidence and scope

Downloaded and parsed the complete inline Swagger 2.0 specification from [NetApp's version-specific Swagger UI](https://docs.netapp.com/us-en/ontap-restapi-9141/swagger-ui/index.html). Enumerated all paths and explicit methods, extracted parameter metadata and response schema references, reviewed the resource families, and examined the core response models and exceptional GET behavior.

The specification contains 531 path entries, of which **529 declare operations**. Two entries (`/cluster/ntp`, `/storage/quota`) contain documentation only. There are **1,009 explicit operations: 504 GET, 178 POST, 168 PATCH, 159 DELETE**, plus 1,062 schema definitions. HEAD/OPTIONS and implicit bulk mutation variants are documented globally and not separate operations in these counts.

Deliverables:

- [Complete endpoint inventory](netapp-ontap-9141-endpoints.md): every path, methods, proposed disposition, and selected review notes.
- [Machine-readable inventory](netapp-ontap-9141-endpoints.json): every operation, parameters, version markers, response references, and source fingerprint.

This establishes the public **9.14.1 family** contract. No claim of successful operation on **9.14.1P9** is made. Public documentation can change, and hardware, roles, licensing, and configuration affect results. Before shipping, compare the selected contract with the target cluster's [local Swagger documentation at `/docs/api`](https://docs.netapp.com/us-en/ontap-automation/reference/api_reference.html) and real responses. Do not use the latest ONTAP specification as the compatibility baseline.

## Architecture decision

| Option | Assessment | Decision |
|---|---|---|
| Native REST client using existing HTTP libraries | Precise control of requested fields, permitted query parameters, pagination, normalization, and query budgets | Recommended |
| Official `netapp_ontap` Python library | Supported vendor abstraction; still requires our analytical schemas and semantics | Valid alternative, but unnecessary for this bounded GET surface |
| NetApp ONTAP-MCP source | Request encoding, paging, scoped UUID lookup and Swagger catalog generation patterns | Reference only; no MCP server or preset required |
| Harvest with Prometheus | Existing collector and metric history ecosystem | Prefer our existing Prometheus connector if the customer already has Harvest; complement native inventory |
| Generic Custom API definitions | Useful for connectivity experiments | Insufficient alone for this product's pagination, typed relationships, and metric contracts |

The [official Python library](https://docs.netapp.com/us-en/ontap-automation/python/learn-about-pcl.html) remains a transport alternative. The inspected MCP generic GET handler does not enforce a diagnostic endpoint allowlist; its read-only flag gates tool registration and is not our execution-policy boundary. [Harvest's preparation guide](https://netapp.github.io/harvest/latest/prepare-cdot-clusters/) supplies useful role and certificate reference patterns.

Use `DataSourceClient` and the existing `QUERY` capability. `get_tables()` builds the curated plus diagnostic `Table`/column/relationship catalog; `get_schemas()` delegates to it for the existing indexing contract. `execute_query()` accepts a validated JSON specification and returns a DataFrame. ServiceNow demonstrates the REST-to-table contract; Kubernetes demonstrates fixed catalogs and feature-aware discovery. Do not copy their unrestricted query escape hatches into this connector.

## `get_tables` contract and broad diagnostic discovery

Proposed connector methods:

```python
get_tables() -> list[Table]
get_schemas(progress_callback=None) -> list[Table]  # existing BOW indexing entry point
get_schema(table_name: str) -> Table
execute_query(query: str | dict) -> pandas.DataFrame
```

`get_tables` is the connector's discovery method; do not assume the base class already requires or exposes it as a separate agent tool. The existing base requires `get_schemas`, `get_schema`, `prompt_schema`, and `execute_query`, so integrate through those paths. Both discovery methods must use one catalog and return consistent schemas. No new general-purpose tool API is required for the initial implementation.

The catalog has two tiers, both queryable through the same executor:

1. **Curated tables:** stable names, carefully documented row grains and relationships for common investigations, listed below.
2. **Diagnostic tables:** deterministic `diag_` identifiers mapped to additional reviewed Swagger GET operations. Preserve canonical endpoint identity and reject naming collisions. Include collection, singleton and parent-scoped resources; omit duplicate aliases when a curated table already supplies equivalent access. Detail-only fields remain discoverable through field profiles or a detail adapter. Do not create one table per object UUID.

For each entry expose columns/types, primary/foreign keys where established, source endpoint, row grain, parent requirements, supported filters, units, time semantics, costly-field profiles, implementation status and device availability. Store extended information in compatible table metadata/descriptions and verify that indexing and prompt formatting retain it; do not assume all desired attributes already exist on `Table`.

Generate endpoint/schema candidates from the frozen Swagger, then apply reviewed endpoint, parameter and response-profile rules. Reject unreviewed operations by default. Schemas with arrays, polymorphic results, raw counters or unusual response envelopes require an explicit normalization strategy. Retain structured arrays/objects or use relationship tables; never silently explode arrays and multiply measures.

Discovery must not read every resource or enumerate every object's history. Load the local schema catalog, perform small connection/capability probes, and learn availability lazily. Preserve unavailable/unverified entries in a coverage report rather than representing them as successful empty data. Only executable entries belong in the selectable table catalog. Schema retrieval must work for empty resources. API descriptions are data, not agent instructions.

Keep prompt context bounded: list concise table summaries, retrieve full schemas for selected tables, then query. Verify this works with BOW's existing table selection and schema retrieval. Avoid injecting hundreds of full endpoint descriptions into every prompt.

## Curated starting catalog (36 tables; not the coverage ceiling)

All paths below are prefixed with `/api`. These are 26 primary resource endpoints plus nine historical-series endpoints. Detail endpoints are lookup helpers, not separate catalog tables. These seed the curated experience while the broader diagnostic catalog is implemented and assessed.

| Virtual table | GET endpoint | Row grain / purpose |
|---|---|---|
| `cluster` | `/cluster` | Cluster identity, version, health context |
| `nodes` | `/cluster/nodes` | Node model, serial, HA state, uptime |
| `svms` | `/svm/svms` | Data SVM identity, state, enabled protocols |
| `cluster_space` | `/storage/cluster` | Cluster-level storage summary |
| `aggregates` | `/storage/aggregates` | Local tier capacity, state, owning node |
| `volumes` | `/storage/volumes` | FlexVol/FlexGroup logical volume, space, state |
| `disks` | `/storage/disks` | Disk identity, placement, condition |
| `shelves` | `/storage/shelves` | Shelf identity and hardware health |
| `luns` | `/storage/luns` | LUN identity, serial, volume, size, state |
| `igroups` | `/protocols/san/igroups` | Host access group |
| `initiators` | `/protocols/san/initiators` | SVM-scoped initiator identity |
| `lun_maps` | `/protocols/san/lun-maps` | LUN ↔ igroup, logical unit number |
| `igroup_initiators` | `/protocols/san/igroups/{igroup.uuid}/initiators` | Direct group membership |
| `igroup_children` | `/protocols/san/igroups/{igroup.uuid}/igroups` | Parent ↔ nested group |
| `fc_ports` | `/network/fc/ports` | Physical FC port |
| `fc_interfaces` | `/network/fc/interfaces` | Logical FC interface |
| `fc_logins` | `/network/fc/logins` | Initiator WWPN ↔ target interface |
| `ip_interfaces` | `/network/ip/interfaces` | Logical IP interface |
| `ethernet_ports` | `/network/ethernet/ports` | Physical/VLAN/LAG interface |
| `snapshots` | `/storage/volumes/{volume.uuid}/snapshots` | Snapshot scoped to a volume |
| `snapshot_policies` | `/storage/snapshot-policies` | Protection policy |
| `snapmirror_relationships` | `/snapmirror/relationships` | Replication relationship |
| `snapmirror_policies` | `/snapmirror/policies` | Replication policy |
| `snapmirror_transfers` | `/snapmirror/relationships/{relationship.uuid}/transfers` | Transfer scoped to a relationship |
| `qos_policies` | `/storage/qos/policies` | QoS limits for performance context |
| `ems_events` | `/support/ems/events` | Observed event, node, timestamp, severity |

Additional derived `volume_aggregates` rows preserve the volume-to-aggregate array without duplicating volume capacity. An optional `volume_constituents` diagnostic view uses `/storage/volumes?is_constituent=true`; it must not be unioned into logical volume totals. The standard `volumes` query pins `is_constituent=false`. Child resources are queried for explicitly resolved parent IDs with bounded fan-out, never fetched wholesale during indexing.

Historical tables cover cluster, nodes, aggregates, volumes, LUNs, FC ports, FC interfaces, IP interfaces, and Ethernet ports. They share a metric envelope but retain resource-specific measures; a port does not automatically have volume latency or IOPS. Expose resource-specific table schemas, using one internal history executor.

Schema fields must be explicitly selected from the 9.14.1 models. Core examples: volume `uuid`, `name`, `svm.uuid`, `svm.name`, `style`, `type`, `state`, `space.size`, `space.used`, `space.available`; LUN `uuid`, `name`, `serial_number`, `location.volume.uuid`; SnapMirror `uuid`, `healthy`, `state`, `lag_time`, `unhealthy_reason`, `source`, `destination`. Request only the required subfields of objects. Add expensive enrichments as field profiles instead of making every query retrieve them.

## Full Swagger coverage worklist

| API family | GET operations | Product disposition |
|---|---:|---|
| Storage | 86 | Capacity/hardware plus qtrees, quotas, FlexCache and FabricPool diagnostics; file content/operations excluded |
| Protocols | 159 | SAN relationships; NAS shares/exports, NVMe and S3 diagnostic coverage by enabled protocol |
| Cluster | 50 | Identity/node metrics, sensors, chassis and MetroCluster; specialized counter adapter |
| Network | 42 | Interfaces/port health, exposed fabric topology, routes and broadcast domains |
| SVM | 15 | SVM inventory, peers and migration diagnostics |
| SnapMirror | 6 | Read-side relationships, policies and transfers |
| Support | 29 | EMS observed events and passive support status; no support collection actions |
| Application | 15 | Consistency group membership, snapshots and metrics when deployed |
| Cloud | 2 | FabricPool/cloud target metadata with explicit field selection |
| Resource tags | 4 | Cross-resource grouping and supported inventory tags |
| Name services | 31 | Configuration diagnostics for name resolution and authentication dependencies |
| Security | 65 | Passive audit/access-policy/anti-ransomware observations; secret material and management actions excluded |

The endpoint appendix retains the original MVP/phase-2/deferred labels as historical prioritization, not the revised release scope. Its operation inventory remains useful; every one of the 504 GET operations must receive a revised diagnostic coverage decision during implementation. The family table above is the revised investigation coverage worklist. All 505 explicitly listed write operations remain outside the connector. No original label is an executable allow rule.

The full spec includes 24 GET paths containing `/metrics`: 16 historical collection paths plus eight timestamp detail paths. There is **no** `/storage/volumes/metrics` or qtree `/metrics` path in this version. `/top-metrics/*` is a different feature-dependent surface; it must not be presented as arbitrary historical time series.

## `execute_query` contract and execution design

One JSON query language serves curated and diagnostic tables. Required: `table`. Optional: `fields`, `filter`, `parent`, `start_time`, `end_time`, `lookback`, `interval`, `order_by`, and `limit`, plus the `ems_events`-specific `severity` helper, only where the selected schema supports them. Reject unknown keys. Absolute time bounds and relative `lookback` are mutually exclusive; resolve relative time once per execution so saved queries refresh correctly. `parent` binds reviewed path placeholders and never accepts arbitrary URLs.

Proposed public query shape:

```json
{"table":"volumes","filter":{"svm.name":"production","state":"online"},"fields":["uuid","name","space.size","space.used"],"limit":500}
```

```json
{"table":"volume_metrics","parent":{"volume.uuid":"<uuid>"},"start_time":"<UTC start>","end_time":"<UTC end>","interval":"auto"}
```

```json
{"table":"ems_events","lookback":"24h","severity":["alert","error"],"limit":1000}
```

A diagnostic query uses the same shape, with its catalog-returned `diag_...` table name and declared fields/parents. There is no raw path, method, shell command or custom-header escape hatch. The agent's sequence is discover tables → inspect selected schemas → execute bounded queries → join/analyze DataFrames → cite evidence and identify gaps. ONTAP executes REST filtering; BOW/Pandas performs analytical joins and aggregation, not SQL on the array.

Execution stages: parse and validate → resolve version/resource mapping → pin connection scope → bind parent identities → compile filters/time/field profile → make bounded GET requests → validate response completeness → normalize types/relationships → return DataFrame. Always use the same policy enforcement for diagnostic and curated queries.

For initial release, `limit` is a maximum accepted complete result size, not silent truncation: if continuation or excess records prove more data exists, fail with `ResultLimitExceeded` and a narrowing recommendation. Request another bounded page when necessary to establish completeness. Do not let an incomplete table enter a fleet-wide sum or ranking. Add resumable/partial output only when result metadata is preserved by the full BOW execution/report path. Errors must distinguish authentication, permission, unsupported resource, invalid query, unavailable history, partial response, and exhausted budget.

Field names in the query/schema retain ONTAP dotted paths; flattened DataFrame columns use the same names. Added metadata uses a reserved `_bow_` prefix: source cluster, connection, retrieval timestamp, and where needed parent identity. Empty results retain their declared columns and nullable types. Retain source endpoint/profile, query execution identity and contract fingerprint in an execution evidence record that survives report generation; verify the existing result path before choosing its storage format. Distinguish observation time from retrieval time and attach explicit time/quality metadata to metric rows. Never include secrets in evidence logs.

Validate the table, field list, filter keys, filter values, sort fields, parent selectors, and time range before network access. Query filters use documented ONTAP operators through the HTTP library's parameter encoder; distinguish literal strings from wildcard/operator expressions. The event helper compiles severities using ONTAP's OR operator. Caller fields cannot override pinned scope or inject transport settings, custom headers, arbitrary paths, `return_records`, or control parameters. Count-only support can be added separately and must count every page.

Resolve names using cluster and SVM context; reject ambiguous names. Use UUIDs for history after resolution. Accept explicit parent sets only within the configured fan-out budget. Preserve all join keys even when the caller requests a narrow projection.

Transport defaults proposed for validation: HTTPS/443, certificate verification on, private CA support; page size 500, 30-second HTTP timeout, ONTAP `return_timeout=15`; two concurrent requests per connection, at most three transient retries, overall execution deadline 120 seconds, at most 20 parents per history/child query, and a 10,000-row application budget. These are starting values to measure on the target estate, not ONTAP guarantees. Keep an identifiable, stable User-Agent and reusable connection pools.

Follow `_links.next.href` until completion, regardless of page length. Validate next links against the original HTTPS authority and selected resource/scope; keep continuation parameters intact. Bound pages and detect repeated continuations. Do not use offsets or assume `num_records` is the total. Respect `Retry-After` for throttling and use bounded backoff for transient 429/502/503 responses. A 401/403 is not a transient retry case.

The API can return incomplete data with error details in a successful response. Initially fail a query with a clear partial-result error if completeness cannot be established, rather than silently returning usable-looking totals. Row/parent/deadline ceilings are visible typed failures in the initial contract. Do not rely solely on `DataFrame.attrs` for warnings: downstream preservation must be verified. A permission failure must never become an empty table. [Collection and error contract](https://docs.netapp.com/us-en/ontap-restapi-9141/getting_started_with_the_ontap_rest_api.html).

## Performance and capacity semantics

Use embedded `metric` fields on explicitly supported inventory endpoints for fleet-wide current rankings. Retrieve historical series only for the selected entities; for example, rank volumes by current latency, then request history for the worst five. Do not issue a history request for every volume just to answer a current-state question.

The documented history tiers are:

| Requested tier | Retained window | Sample duration |
|---|---|---|
| `1h` | Most recent hour | 15 seconds |
| `1d` | Most recent day | 5 minutes |
| `1w` | Most recent week | 30 minutes |
| `1m` | Most recent month | 2 hours |
| `1y` | Most recent year | 1 day |

Choose the smallest tier reaching the requested **start time relative to now**, not merely matching the duration between start and end. A ten-minute incident six days ago requires the week tier and cannot be resolved at ten-minute precision from that tier. Compile timestamp filtering where supported and trim safely to the requested interval; preserve buckets' duration and document overlap semantics. Conservative tier selection is needed at boundaries. If the requested resolution is unavailable, report the available resolution; do not interpolate precision. [Volume history](https://docs.netapp.com/us-en/ontap-restapi-9141/get-storage-volumes-metrics.html).

Preserve `timestamp`, `duration`, and `status`. Do not treat missing, partial, backfilled, or invalid-delta samples as zero or unqualified good observations. Preserve raw units: bytes, bytes/second, operations/second, microseconds. Presentation may convert to GiB or milliseconds. Weight aggregate latency by IO volume over compatible sample windows, rather than averaging average latencies indiscriminately. Current topology joined to old metrics represents current placement, not proven placement at incident time. [Performance concepts](https://docs.netapp.com/us-en/ontap-automation/rest/performance_metrics.html).

Do not equate provisioned, logical-used, physical-used, snapshot-used, and aggregate-used capacity. FlexGroup parent and constituent rows represent different aggregation levels. Keep the array of hosting aggregates as a relationship, not a reason to multiply volume size. A snapshot's reported size is not automatically reclaimable space. Request snapshot reclaimability/delta calculations only in a later bounded analysis feature. [Volume models](https://docs.netapp.com/us-en/ontap-restapi-9141/get-storage-volumes.html), [snapshot costs](https://docs.netapp.com/us-en/ontap-restapi-9141/get-storage-volumes-snapshots.html).

Standard volume history contains performance measures, not a general-purpose volume capacity history. Capacity growth forecasts, historical mappings, and long-lived event history need retained evidence. Current data alone cannot answer those questions. Historical evidence readiness is an RCA acceptance criterion, not an assumed benefit of the connector: connect an existing Harvest/Prometheus or other approved store when present; otherwise deliver a separate collector/storage/retention design and report the unsupported lookback explicitly. Do not silently add a new time-series database to this connector implementation. Specify required retention and sampling per incident use case, preserve collection gaps, and capture configuration/topology snapshots for historical joins. Harvest does not automatically retain every event or configuration change.

Raw Counter Manager is required in the diagnostic coverage assessment and needs a specialized adapter where selected: discover table schemas, types, units, dimensions and denominator references. Non-raw counters generally require two timed samples and the correct rate/delta/average/percent formula. Handle zero denominators, resets and restarts. Do not label raw counters as computed IOPS or latency. [Counter schemas](https://docs.netapp.com/us-en/ontap-restapi-9141/get-cluster-counter-tables.html), [counter computation](https://docs.netapp.com/us-en/ontap-automation/migrate/performance-counters.html).

## Relationships that need special treatment

- **SnapMirror:** normal collection results are destination-side. Source-side destination discovery uses `list_destinations_only=true` and has different completeness. Connect the relevant destination cluster for authoritative health and lag. Retain source/destination cluster/SVM identities and distinguish an unobserved relationship from an unprotected volume. [SnapMirror contract](https://docs.netapp.com/us-en/ontap-restapi-9141/get-snapmirror-relationships.html).
- **Nested igroups:** direct initiator membership excludes nested group membership. Build explicit group edges and derive effective membership with cycle protection and completeness checks; do not flatten direct membership as if it were exhaustive. For the later Brocade integration, preserve WWPN/WWNN and LUN serials as strings for subsequent cross-source correlation. A LUN serial-to-VMware-NAA join needs verified encoding/mapping evidence, not a guessed equality.
- **FC fabric topology:** ONTAP can expose fabric switches and active zones through its own viewpoint. This is useful diagnostic enrichment, not proof of full Brocade coverage. Keep source viewpoint and `cache.age`/`cache.update_time`; fetching may initiate an asynchronous cache refresh and return older data. [Swagger fabric overview](https://docs.netapp.com/us-en/ontap-restapi-9141/swagger-ui/index.html#/networking).
- **EMS:** `events` contains observed events; `messages` is the event-definition catalog. Preserve node UUID, index, timestamp and message identity. Event identity should include cluster/node context; index alone is unsafe across nodes. Do not equate an event with an active alert or a proved cause. [Observed events](https://docs.netapp.com/us-en/ontap-restapi-9141/get-support-ems-events.html).

## Authentication, scope and passive-reading policy

Initial setup: cluster management URL, username/password, trusted CA configuration, optional administrative SVM filter, and selected resource packs. Use an account with the **HTTP application** enabled; an ONTAPI-only account is not the REST setup. Recommend a dedicated cluster-scoped service account for complete infrastructure analytics, with a custom REST role limited to required read paths. Do not require admin credentials or programmatically create roles as part of connection testing. [Account and scope model](https://docs.netapp.com/us-en/ontap-automation/rest/rbac_overview.html).

Recommend `scopes=["system"]` for the initial registry auth variant. Per-user credentials are possible later but require permission-aware discovery/caching tests and a defined product use case. A configured SVM filter is a connector query scope, not a replacement for ONTAP RBAC. A truly SVM-scoped account exposes a smaller product view; support that as an explicit subsequent mode instead of reporting cluster-wide success.

Certificate authentication is an appropriate follow-up if the environment requires it. OAuth exists in the 9.14 generation, but a pasted expiring bearer token is not a durable unattended solution. Add OAuth only with configured acquisition/renewal and version-specific identity-provider support. The Swagger basic-auth declaration does not enumerate all deployment-level authentication options. [OAuth support](https://docs.netapp.com/us-en/ontap/authentication/overview-oauth2.html).

Enforce an endpoint + parameter + response-profile allowlist. Reasons established directly in the spec:

- `GET /protocols/ndmp/svms/{svm.uuid}/passwords/{user}` generates and returns an NDMP password: exclude it. [NDMP endpoint](https://docs.netapp.com/us-en/ontap-restapi-9141/get-protocols-ndmp-svms-passwords-.html).
- CIFS domain GET accepts discovery-reset controls; exclude those controls and the corresponding Active Directory reset parameter. [CIFS behavior](https://docs.netapp.com/us-en/ontap-restapi-9141/get-protocols-cifs-domains-.html).
- LUN detail GET can read raw LUN bytes when multipart output and `data.offset`/`data.size` are requested. Pin JSON metadata output and reject these controls. [LUN endpoint](https://docs.netapp.com/us-en/ontap-restapi-9141/get-storage-luns-.html).
- Exclude file content/operations and credential/key endpoints. Disable `/api/private/cli` in the initial transport. Record any diagnostic gap it creates, including the MCP QoS enrichment described below; a future narrowly mapped CLI-backed adapter needs its own schema, permissions, tests and coverage decision. Never infer blanket permission from an HTTP verb.

`test_connection()` should establish TLS/authentication, retrieve cluster identity/version, and make small core resource probes. Report actual scope and unavailable feature packs. Index schemas without exhaustive object/history crawling. Keep cached capabilities scoped to connection and credential identity; distinguish successful-empty, permission-denied, unsupported, not-configured, and transient failure.

## MCP source findings applied to this plan

The [source review](netapp-ontap-mcp-source-review.md) pins commit `7ba6baf2e7e3ea0d458e2519f288c5eb5a2353d8`. Reuse request-encoding, parent binding, next-link pagination and scoped name-to-UUID patterns conceptually. Do not copy the 9.16 catalog, unknown-field suppression, unbounded retrieval, coupled page/result limits, floating-point JSON normalization, or MCP identification requests. Our catalog must enforce execution policy; MCP's descriptive catalog does not provide that boundary.

The MCP QoS tool combines public `/storage/qos/policies` with private CLI reads for cluster-level policies. Public REST equivalence is therefore unproven: label scope/coverage and validate against the customer. Never report complete QoS coverage merely because the public query succeeded.

## Build sequence without a verification lab

1. **Freeze contracts and coverage:** preserve Swagger fingerprint; classify all 504 GET operations with rationale, mapping/profile and normalization strategy. Define table metadata and JSON query validation. Document exceptional GETs and private-CLI gaps. This phase uses public evidence, not customer credentials.
2. **Discovery and transport:** implement the native client, registry/config/auth, get_tables/get_schemas/get_schema, strict mapping, pooled HTTPS, pagination, typed failures and complete-result budgets. Add curated inventory and generated diagnostic resources through the same execution path.
3. **RCA adapters and evidence:** add nested relations, history tiers, EMS/jobs/audit resources where available, protocol/hardware diagnostics and counter schemas/sampling. Validate units, integer precision, quality and temporal joins. Complete a coverage decision for every required family; unresolved specialist adapters are reported as gaps.
4. **Local proof:** run HTTP-boundary unit/contract tests with synthetic fixtures, registry resolution, generic data-source/connection e2e, and the BOW discovery → selected schema → query → analysis path. Use a clearly labelled mock endpoint for local UI QA if useful. Record evidence per repository skills. Never claim simulated values establish device accuracy.
5. **Customer validation kit:** provide a reproducible, GET-only command using the same client and policy rules. The customer supplies credentials locally on a runner with cluster-management access. No write setup and no probing every GET on production. Produce a redacted capability/coverage report and optional sanitized fixtures that remain local until the customer elects to share them.
6. **Production acceptance:** compare actual version/local Swagger; validate representative endpoints from enabled families, role scope, hardware/FC, metric availability and time resolution, pagination at actual estate size, and reconciliation against System Manager/CLI at equivalent times/grains. Address failures and preserve real regression fixtures where sharing is permitted. Assess historical evidence availability and unresolved CLI-only gaps. Until this passes, status is “implemented; awaiting customer validation.”

The user explicitly selected development without a lab. The skill's real-instance integration step remains the production acceptance gate, not a prerequisite to starting or completing local implementation. The subsequent implementation request explicitly authorized a local simulated API; no AWS resources, marketplace subscription or MCP deployment is required.

## Customer-run verification and acceptance scenarios

The kit first verifies TLS/auth and cluster version, then a small representative selection. Parent-dependent checks require explicitly supplied or bounded discovered IDs. No role creation, support-bundle generation, workload generation or configuration changes. Report endpoint/profile, contract fingerprint, role scope, result/completeness status, latency, time coverage, and errors; redact credentials, sensitive query values and object identifiers in exportable reports. Sanitization must preserve join consistency when fixtures are supplied.

| Investigation | Acceptance evidence |
|---|---|
| Slow volume/LUN | Resolve SVM/volume/LUN; compare compatible metric windows with aggregate/node context, QoS and EMS; distinguish evidence from hypothesis |
| Capacity incident | Reconcile logical volume/aggregate/snapshot/quota evidence without FlexGroup or relationship double counting; identify unavailable historical capacity |
| FC connectivity | Correlate LUN maps, igroups, initiator WWPN, FC LIF/port/login and relevant events; explicitly identify missing Brocade/host evidence |
| NAS access | Discover protocol/export/share/name-service diagnostic tables; retrieve applicable policy/configuration and events with scope provenance |
| Replication failure | Resolve source/destination viewpoint, relationship state, unhealthy reasons and available transfer evidence |
| Hardware fault | Retrieve actual node/disk/shelf/sensor evidence where exposed and correlate event times; unsupported families are visible |
| Past incident | Select available time resolution, expose missing history, avoid treating current topology as historical fact |
| Unexpected diagnostic need | Discover a reviewed diagnostic table outside the curated 36, retrieve schema, execute through the same query interface, retain its evidence provenance |

Unresolved gaps that prevent a required customer investigation block acceptance of that investigation; recording a gap is not fulfilling the requirement. Release report separates documented coverage, implemented coverage, mock-tested behavior, customer-observed coverage and remaining gaps. A successful connection test is not broad-RCA certification.

Implementation locations: client and resource catalog under `backend/app/data_sources/clients/`; config and credential classes in `backend/app/schemas/data_sources/configs.py`; registration in `backend/app/schemas/data_source_registry.py`; icon and map; unit/integration tests; `docs/feedback-loops/netapp-ontap-connector.md`. No general backend route or database migration appears necessary for native query support. If existing infra connector licensing conventions apply, keep registry licensing and `backend/app/ee/license.py` consistent; enterprise gating is a product decision, not an API requirement. New product copy follows localization and UI evidence skills.

Required tests should exercise failure modes, not reproduce implementation details:

- Time-limited short/empty pages with continuation; terminal empty page; malformed or cross-authority continuation; page/row budgets.
- HTTP 200 with partial errors, nullable metrics, 401/403 distinctions, retry headers, exhausted transient retries.
- Duplicate names across SVMs, parent ID requirements, multi-aggregate volumes, FlexGroup constituents, nested igroups and source/destination SnapMirror views.
- Old short incident windows, history-tier boundary selection, irregular sample durations, time-zone normalization and invalid metric status.
- Unknown tables/fields/parameters, GET action controls, raw LUN content requests, scope override attempts and no-write dispatch.
- UI/API capacity reconciliation at equivalent grain, stable empty schemas, relationship integrity, and scheduled relative-time refresh.

A simulator can cover inventory/auth behavior if access is available; a real representative array is needed for meaningful hardware/performance evidence. Published mock examples are not proof of actual metric values, hard API limits, field availability, or performance at customer scale.

## Corrections to the earlier repository proposal

This version-specific plan supersedes the NetApp assumptions in `docs/vmware-aria-storage-connectors-analysis.md` section 6f for this target:

- Use this frozen 9.14.1 inventory, not the earlier 587-path latest-spec count.
- Qtree history is absent in this specification.
- LUN `serial_number_hex` is absent from this version's LUN response schema and query parameters; retain `serial_number` and validate any external identifier conversion separately.
- SnapMirror error explanations use `unhealthy_reason`; do not request the proposed nonexistent `last_transfer_error` field.
- History tier choice depends on age of requested start; timestamp is a documented filter on relevant metrics APIs. Do not choose the tier solely from window length.
- The 15-second `return_timeout` creates pagination; it does not justify calling paginated results truncated. No blanket increase to 60 seconds is required.
- Session/concurrency limits must be measured or tied to exact authoritative configuration. Do not encode an assumed universal 20-session or 10,000-record vendor limit in the mock.
- A raw GET escape hatch is not an adequate passive-reading boundary.
- Do not enable per-user auth, static bearer tokens, or cross-source serial joins without their missing operational contracts.

Implementation can begin now against the frozen public 9.14.1 contract. Customer configuration, exact P9 behavior, permissions, physical/FC observations, CLI-only gaps and historical retention remain explicit validation items. No paid verification lab is required.
