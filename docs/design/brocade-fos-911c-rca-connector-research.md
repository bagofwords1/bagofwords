# Brocade root cause analysis connector

Build a native, read-only Fabric OS connector that makes diagnostic resources discoverable through `get_tables` and retrieves evidence through `execute_query`. Use PyFOS and Broadcom's Ansible implementation as protocol references. Keep the production request layer independent so it can preserve FOS 9.1 fields, enforce query scope, and distinguish missing evidence from healthy conditions.

The target is FOS **9.1.1c**. The public YANG package available for this assessment is **9.1.0b**. Its schema is a useful baseline, not an exact target-version contract. No switch was queried, no connector was implemented, and no firmware compatibility was verified. This design was assessed on September 13, 2026.

The accompanying [resource inventory](brocade-fos-911c-resource-audit.md) and [field inventory](brocade-fos-911c-resource-audit.json) record 80 YANG files, 177 direct resource candidates across 35 resource-bearing modules, 2,044 expanded leaf paths, and 32 RPC declarations. These are structural counts, not supported endpoint or final table counts. The parser expanded grouping references but reported 305 upstream XPath diagnostics, including one syntax diagnostic; the package did not pass clean semantic validation. Archive fingerprints are recorded in the JSON. [1]

The important design decisions are:

- Present 12 primary RCA tables by default, with the remaining reviewed resources in an advanced diagnostic catalog. Do not expose every YANG node automatically.
- Return all reviewed, supported diagnostic fields for the selected table when `fields` is omitted. Keep projection optional, rows bounded and expensive enrichments explicit.
- Use live module/object discovery to reconcile a bundled schema with the connected switch. Version, hardware, feature, permission and scope are separate availability dimensions.
- Preserve physical and logical identity, source time, retrieval time, units and evidence quality.
- Support the history actually available from MAPS and logs. Do not advertise arbitrary historical port metrics without a separate retained data source.
- Make every result complete within its declared scope or fail visibly. A query result with no rows must not silently mean access was denied.

## Evidence that changes the design

| Finding | Connector consequence |
|---|---|
| PyFOS uses `/rest/brocade-module-version`, outside the ordinary `/rest/running/...` path convention. The module model includes module versions, URIs and supported object names. | Capability discovery needs a dedicated adapter; constructing every URI mechanically is incorrect. [2] |
| PyFOS documents coverage through 9.0.1. The 9.1.0 interface model introduces replacements such as `protocol-speed` and `operational-status-string`. | Preserve the 9.1 schema; do not let older SDK object definitions define field coverage. [3][4] |
| `brocade-logging/raslog` describes logging controls and message definitions. `error-log` contains emitted RASLog events; `audit-log` contains audit events. | Separate event tables from logging configuration tables. [5] |
| MAPS includes minute congestion samples and daily dashboard history. | Include these historical resources, with their native grain and bounded retention. [6] |
| A field's name and YANG numeric type do not establish its metric semantics. | Curate measure kinds, units, aggregation rules and reset behavior. [4][6][7] |
| Official Ansible code handles particular 404 bodies as empty collections and groups several unsupported-feature responses into its empty-result handling. | Maintain a tested endpoint/error classification; generic HTTP-status handling is insufficient for RCA. [8] |

## What `get_tables` should return

In BOW, the client supplies `get_schemas`, `get_schema` and `prompt_schema`; those feed the user/agent-facing table catalog. The Brocade design should use that existing mechanism. The current table representation already supports columns, primary keys, foreign keys and `metadata_json`. Rich semantics must survive all prompt-rendering paths, not merely be persisted in metadata. [15]

Use one table name per logical dataset, not one table per physical switch, FID or individual port. Connection and virtual-fabric scope belong in the query and in returned identity columns. A fabric containing thousands of ports should not create thousands of table definitions.

The default catalog contains the following 12 primary tables. These are curated views, not a one-to-one copy of the REST endpoints. `get_tables` should make this primary set easy to discover and offer an explicit advanced-catalog expansion through the existing catalog mechanism; the full endpoint inventory should not be inserted into every agent prompt.

| Primary table | Row grain and default contents |
|---|---|
| `switches` | One logical switch; identity, firmware and logical-fabric context. |
| `ports` | One port; state, speed, configuration and directly returned neighbor attributes. |
| `port_statistics` | One port observation; all reviewed traffic, error and sampling fields. |
| `transceivers` | One port's optics observation; local and available peer readings and identity. |
| `connected_devices` | One observed FC registration; WWPN, attachment and directly available device information. Additional FDMI/HBA lookups are explicit enrichments or advanced queries. |
| `fabric_links` | One observed directed switch-port link; neighbor identity and reviewed trunk-membership context. Do not duplicate traffic measures across members or infer unobserved links. |
| `zones` | One zone per defined/effective configuration state; retain zone type and scope. |
| `zone_members` | One member per zone/configuration state, preserving ordinary/principal role and original member syntax. Alias expansion must be explicit and preserve provenance. |
| `health` | One component or policy health assessment, with assessment type and source. |
| `congestion_samples` | One port/time/observation-type sample; credit-stall and oversubscription remain distinguishable. |
| `events` | One event with `event_source` distinguishing RASLog and audit; retain source-specific fields. |
| `hardware` | One component observation with component type and identity; type-specific readings remain nullable. |

Keep different grains separate. For example, port history does not become extra rows in `ports`, and component sensors do not multiply switch rows. Union views such as `events` and `hardware` retain source/type discriminators and source-specific columns; they do not equate unlike measurements. Fixed resource reads documented as constituents of a primary view are allowed, but choosing all fields never initiates recursive relationship traversal. Each view must declare its constituent requests, cost and availability. A missing constituent must produce a visible capability/completeness result, not a silently incomplete union.

The following detailed mapping is the advanced catalog and underlying resource worklist, **not the default table list**. Existing names that match a primary table describe its backing source. Other names identify advanced datasets or normalization inputs. Resource suffixes are observed in the baseline models; unless otherwise noted they use `/rest/running/<module>/<resource>`. Each requires a reviewed GET contract and target-version reconciliation before exposure. [1]

| Proposed tables | Baseline resource mapping | Row grain and RCA purpose |
|---|---|---|
| `module_versions`, `module_objects` | `/rest/brocade-module-version` | Module; module/object relationship. Establish advertised capabilities. |
| `chassis`, `ha_status` | `brocade-chassis/chassis`, `ha-status` | Chassis; HA observation. Identify model, platform and controller health. |
| `switches`, `logical_switches` | `brocade-fibrechannel-switch/fibrechannel-switch`; `brocade-fibrechannel-logical-switch/fibrechannel-logical-switch` | Logical switch. Preserve switch WWN and FID. |
| `logical_switch_ports` | Logical-switch `port-member-list` and `ge-port-member-list` | Switch/port membership with interface kind; avoid multiplying switch attributes. |
| `fabric_switches`, `fabric_access_gateways` | `brocade-fabric/fabric-switch`, `access-gateway` | Fabric membership as observed by a queried switch. |
| `fc_ports`, `port_statistics` | `brocade-interface/fibrechannel`, `fibrechannel-statistics` | Port; sampled port statistics. State, identity, speed, errors and traffic. |
| `port_neighbors` | `fibrechannel` neighbor fields | Port/neighbor relationship, retaining one-to-many NPIV relationships. |
| `transceivers` | `brocade-media/media-rdp` | Local port optics and available peer optical data. |
| `name_server_devices` | `brocade-name-server/fibrechannel-name-server` | Registered FC device. WWPN, FCID, port-index and registration attributes. |
| `fdmi_hbas`, `fdmi_ports`, `fdmi_hba_ports` | `brocade-fdmi/hba`, `port`, HBA port list | Registered HBA, registered port, and their relationship. Host/driver/firmware clues. |
| `trunk_members`, `trunk_performance`, `trunk_areas` | `brocade-fibrechannel-trunk/trunk`, `performance`, `trunk-area` | Trunk member, trunk statistic, trunk-area membership. |
| `topology_domains`, `topology_routes`, `topology_errors` | `brocade-fibrechannel-switch/topology-domain`, `topology-route`, `topology-error` | Domain reachability, routing alternatives and topology failures. |
| `logical_e_ports`, `logical_e_port_members` | `brocade-interface/logical-e-port` | Logical ISL endpoint and its physical-port relationships. |
| `defined_zone_configs`, `defined_zone_config_members`, `defined_zones`, `defined_zone_members`, `zone_aliases`, `zone_alias_members` | Nested resources under `brocade-zone/defined-configuration` | Configuration, zone and alias relationships kept separate. |
| `effective_zone_config`, `effective_zones`, `effective_zone_members`, `zone_fabric_lock` | `brocade-zone/effective-configuration`, `fabric-lock` | Active access configuration and transaction-lock diagnostics. |
| `maps_health`, `system_resources` | `brocade-maps/switch-status-policy-report`, `system-resources` | Component health and switch management CPU/memory/flash observations. |
| `maps_rules`, `maps_policies`, `maps_policy_rules`, `maps_groups`, `maps_group_members`, `maps_paused_groups`, `maps_fpi_profiles` | `rule`, `maps-policy`, `group`, `paused-cfg`, `fpi-profile` | Explain what was monitored, thresholds, membership and monitoring gaps. |
| `maps_alerts`, `maps_alert_objects` | `brocade-maps/dashboard-rule` | Dashboard rule observation and affected object relationships. Not necessarily one row per individual alert occurrence. |
| `maps_credit_stall_samples`, `maps_oversubscription_samples` | `credit-stall-dashboard`, `oversubscription-dashboard` | Port/minute sample, including monitoring-paused states. |
| `maps_daily_history` | `brocade-maps/dashboard-history` | Normalize category/day/port/metric/value; retain original port-data text for traceability. |
| `raslog_events`, `audit_events` | `brocade-logging/error-log`, `audit-log` | Emitted event, original timestamp, message ID, severity and source context. |
| `logging_message_config`, `logging_settings`, `syslog_servers` | `brocade-logging/raslog`, `log-setting`, `syslog-server` | Explain suppression, forwarding and collection limitations. |
| `blades`, `fans`, `power_supplies`, `sensors`, `wwn_units`, `fru_history` | `brocade-fru/blade`, `fan`, `power-supply`, `sensor`, `wwn`, `history-log` | Hardware condition and component replacement context. |
| `firmware_history`, `time_zone`, `clock_servers`, `ntp_servers` | `brocade-firmware/firmware-history`; `brocade-time/time-zone`, `clock-server`, `ntp-clock-server` | Change context and timestamp alignment. |
| `congestion_devices`, `ag_f_port_congestion`, `ag_n_port_capabilities` | `brocade-fabric-traffic-controller` resources | FPIN/notification evidence and capability negotiation where available. |

This resource worklist preserves broad diagnostic coverage without requiring the agent to navigate all datasets for routine investigations. It is not a fixed promise that every named table will exist on every installation. Actual support should be visible in both catalog levels. Both levels use the same validated `execute_query` contract and all-reviewed-fields default.

The remaining 177-candidate inventory must receive a disposition rather than being silently ignored. Include reviewed reads for Access Gateway mappings, FC routing/LSAN, extension tunnels/circuits and WAN statistics, Ethernet/portchannel statistics, LLDP, traffic-optimizer flows, FICON, licensing status, passive security policies and management configuration. These are conditional on the customer's platform, operating mode and permissions. Generic diagnostic table names can preserve module/resource identity, for example `diag_brocade_extension_tunnel__wan_statistics`.

Do not include operational RPCs in the initial query surface. Exclude password/secret material, SNMP community/authentication secrets, key material, file retrieval and command-like action fields. Mixed resources need field-level decisions: port statistics contain `reset-statistics`, MAPS includes `clear-data` and test-email fields, and effective zoning includes `cfg-action`. Their presence in a readable model does not authorize actions or require exposing those leaves. Conversely, `config true` does not mean the whole resource is unsuitable for reads: active configuration is important diagnostic evidence. [1][4][6][9]

## Schema metadata and discovery

Every table should describe its purpose, row grain, source resource, baseline/target contract status, required scope, stable keys, relationships, field types, units, supported filters, time semantics, response shape, cost and completeness rules. Distinguish `current_state`, `sampled_statistics`, `event_log`, `minute_summary`, `daily_summary` and `derived_relationship`.

Availability should separately track `advertised`, `read_verified`, `unverified`, `permission_denied`, `unsupported`, `feature_disabled` and `temporarily_unavailable`. An advertised object is not proof that the current principal may read it. A valid empty response is a query result, not an unsupported-table state. Keep unverified entries identifiable without representing them as successfully tested resources.

Discovery should establish the authenticated switch identity/version, inspect `/rest/brocade-module-version`, obtain authorized logical-switch scope where permitted, and reconcile object names against the bundled reviewed catalog. Do not follow server-advertised URIs to another authority or permit an advertised object to bypass the allowlist. Use bounded lazy read checks rather than retrieving every diagnostic collection during indexing. Reconcile cached discovery after firmware or credentials change; do not leak one user's readable-scope assessment into another's. [2][8]

Use connection/chassis identity plus logical-switch WWN and FID in keys. `FID=10` or `port=0/12` alone is not globally unique. Preserve the source `fabric-id` on event records, including chassis-context value zero, separately from the FID used to make a request. Some operational lists have no declared YANG key; do not invent global uniqueness for timestamps or event sequence numbers. Use an observation identity for ingestion and document any deduplication heuristic. [5][10]

Primary views use documented readable aliases, such as `port`, `operational_status`, `severity` and `sample_time`. Every alias must retain its exact source-field mapping, type and units in the schema; normalization must detect name collisions. Advanced source-shaped tables preserve source field names, including kebab-case, and use dotted paths for flattened nested scalar fields. Reserved `_bow_` columns carry connection, source WWNs, query FID, retrieval time and query evidence identity. Retain one-to-many relationships in child tables instead of duplicating measures in parent rows. The field names in the baseline inventory are source names; primary aliases are a proposed mapping to finalize during contract verification.

Example metadata proposal, not an implemented interface:

```json
{
  "name": "port_statistics",
  "metadata_json": {
    "source_resource": "brocade-interface/fibrechannel-statistics",
    "scope": "logical_switch",
    "row_grain": "one port observation",
    "baseline_version": "9.1.0b",
    "target_version": "9.1.1c",
    "target_verification": "pending",
    "time_kind": "sampled_statistics",
    "arbitrary_history": false,
    "default_fields": "all_reviewed_supported_diagnostic_columns",
    "supports": ["keys", "fields", "filter", "order_by", "limit"]
  }
}
```

## What `execute_query` should do

Use a JSON query language over registered tables, returning BOW-compatible DataFrames. REST retrieves records; the BOW analysis layer joins and aggregates them. Do not add SQL execution on the switch or accept arbitrary REST paths, methods, headers, credentials or shell commands.

Recommended fields:

| Query property | Meaning |
|---|---|
| `table` | Required exact catalog identifier. |
| `scope` | Explicit `vf_id` for logical-switch resources, constrained by connection authorization. Omit for chassis resources. Never silently substitute FID 128. |
| `keys` | Optional exact key binding using the selected table's schema; e.g. `{"port":"0/12"}` on the primary port table, mapped to the native resource key. |
| `fields` | Optional projection. Omission returns all reviewed, supported diagnostic columns for that table; identity/time evidence is retained even with projection. |
| `filter` | Equality shorthand and typed operators such as `{"severity":{"in":["error","critical"]}}`; operators allowed per column type. No raw vendor expressions. |
| `lookback` | Relative window resolved once per execution, only for tables with supported time semantics. |
| `start_time`, `end_time` | Offset-aware absolute event/sample bounds, mutually exclusive with `lookback`. Use a documented half-open interval. |
| `order_by` | Declared fields and ascending/descending direction. Global ordering requires complete retrieval first. |
| `limit` | Maximum accepted complete result size; exceeding it is a visible error in the initial contract. |

Filters and projection are connector semantics, not a claim that FOS accepts NetApp-style URL parameters. Initially, use collection GETs and apply bounded typed filtering/projection locally; support key-specific URIs or documented GET-body filters only where their exact behavior is verified. PyFOS contains both GET-body and URI-key paths, so GET bodies are not automatically an implementation mistake, but infrastructure may handle them differently. Do not invent `fields`, `offset`, `page_size` or general server-side sorting parameters. [11]

The all-fields default is bounded by the selected table's reviewed schema, not by whatever arbitrary leaves appear in a response. It excludes secrets and action controls and never expands to every related table. Preserve missing/not-applicable values as nullable with availability metadata; do not invent zeros. Newly observed unreviewed fields are schema-drift evidence, not automatically exposed columns. Fields requiring extra lookups must be labeled as optional enrichments and requested explicitly; requesting one must resolve to a reviewed adapter with its own budget. No generic enrichment syntax is committed until those adapters are designed.

All fields does not mean all rows. Filters, row/byte limits and completeness checks remain in force. Do not silently drop columns or truncate rows when the result exceeds a budget. Raise a clear error asking for a narrower filter or explicit projection. Table/column descriptions remain available through `get_tables` and schema inspection so the agent can interpret the returned data without manually selecting every field.

Default query, returning the complete reviewed diagnostic row:

```json
{
  "table": "port_statistics",
  "scope": {"vf_id": 10},
  "filter": {"port": "0/12"}
}
```

The conservative first mapping is a read of `/rest/running/brocade-interface/fibrechannel-statistics?vf-id=10` followed by validated local selection of native `name=0/12` and normalization to the primary schema. A verified keyed-resource adapter can reduce payload later. The final adapter must not issue an unbounded collection read when its byte/row budget is too small.

Optional projection for a focused result:

```json
{
  "table": "ports",
  "scope": {"vf_id": 10},
  "fields": ["port", "operational_status"]
}
```

```json
{
  "table": "events",
  "scope": {"vf_id": 10},
  "lookback": "24h",
  "filter": {"event_source": "raslog", "severity": {"in": ["error", "critical"]}},
  "order_by": [{"field": "event_time", "direction": "asc"}],
  "limit": 2000
}
```

This selects emitted events from `error-log`, then applies the time/severity filter according to the verified endpoint contract. A complete retrieval of the available log is not proof that all events from the last 24 hours were retained. Evidence metadata must distinguish collection completeness from historical coverage.

```json
{
  "table": "congestion_samples",
  "scope": {"vf_id": 10},
  "filter": {"port": "0/12", "observation_type": "credit_stall"},
  "lookback": "30m"
}
```

This uses the timestamped MAPS samples actually retained on the switch. It must not reconstruct samples, interpolate missing minutes, or map monitoring-paused into no-congestion. [6]

Execution stages are validation → scoped session acquisition → capability/resource check → bounded REST reads → response/error classification → schema normalization → local filtering/sorting → completeness checks → DataFrame and durable evidence record. Avoid mutable shared session FID state: simultaneous queries to different virtual fabrics must carry their own request scope.

Start with one configured chassis management endpoint per connection. A query may inspect fabric membership from that switch, but it must not automatically authenticate to every discovered switch. Whole-fabric port evidence requires the relevant authorized connections. BOW can correlate their results afterward.

## Transport and error behavior

Use HTTPS with certificate verification and private-CA support. The inspected vendor reference paths use XML. A first implementation can explicitly use the proven XML representation with an entity-safe parser; JSON support is a separately verified wire-format choice, not a requirement of the public JSON query language. Handle singleton, collection, empty and nested-list XML representations deliberately. [8][11]

PyFOS uses a `Custom_Basic` login header in its session path; the newer Ansible code uses `Basic`. Both POST to `/rest/login`, use the returned Authorization header for subsequent requests and POST to `/rest/logout`. Resolve the target's accepted login scheme from its manual or a smoke test; do not guess that every version requires the same prefix. Authentication is an internal lifecycle exception to diagnostic GET-only execution. Do not copy HTTP defaults, unverified TLS modes, token logging or broad retry behavior. [8][12]

Bound session count, concurrent reads, response bytes, total rows, execution time and retries. Proposed starting limits are one active request per connection, a 30-second HTTP timeout and a 120-second total query deadline, to be measured against the estate. These are application defaults, not FOS limits. Do not alter switch management settings automatically. Older rate-limiting advice should not be copied to 9.1: Dell's collector documentation explicitly describes changed throttling behavior beginning in 9.1. [13]

Classify errors using status plus structured body and endpoint context. Specific known empty-collection responses can become typed empty frames. Unsupported platform/mode remains unsupported, authorization remains an error, and chassis-not-ready remains unavailable. A generic 404, 400 or 405 must not turn into “healthy/no records.” Retry only transient reads with bounded backoff; do not indiscriminately replay login or other POST operations. Reject redirects and cross-origin continuation links. [8]

Do not assume ONTAP pagination exists. Establish collection completeness and any endpoint-specific continuation mechanism from the target contract. If a collection cannot be completely read inside the budget, fail with a narrowing recommendation. A later explicit top-N operation may intentionally return fewer rows only if its completeness/ranking metadata survives every result path. `DataFrame.attrs` alone is insufficient evidence propagation unless downstream retention is proven.

## Metric and history semantics

The baseline models expose useful traps for a generic generator:

| Field/resource | Interpretation to preserve |
|---|---|
| `in-rate`, `out-rate` | Instantaneous byte rates, despite their counter-like YANG type. Do not difference these as cumulative byte counters. |
| `in-octets`, `out-octets` | Cumulative byte counters; derived rates require a valid elapsed sample interval. |
| `bb-credit-zero` | Counts transitions into/out of the credit-zero state. It is not duration or percentage stalled. |
| `crc-errors`, `in-crc-errors` | Separate source definitions. Do not sum overlapping error counters into a fabricated total. |
| `total-up-time` | Percentage in this model, despite the duration-like name. |
| Trunk `rx-throughput` | Bits per second; do not join as if it had the same units as port byte rates. |
| Optics `rx-power`, `tx-power` | Inherited units are microwatts. Convert to dBm only with an explicit positive-input conversion. |
| `time-generated`, `time-refreshed` | Distinct collection/query and last-refresh times. Both are epoch seconds in the referenced typedef. |
| MAPS daily `port-data` | Encoded port/counter values; daily reset behavior is distinct from lifetime port counters. |

These distinctions are present in the vendor definitions and require type/units resolution plus curated semantic overrides. Preserve uint64 counters without lossy float conversion. Unknown or unsupported values are nullable, never substituted with zero. [4][6][7][14]

For live troubleshooting, use native rate fields first. Optional `port_counter_deltas` can later be a documented derived table requiring an explicit sampling interval, two separate refreshed observations and an elapsed-time calculation. Never clear counters to create a baseline. If a reset, reboot, scope change, non-advancing refresh time or unexplained decrease occurs, mark the delta invalid. A reset followed by rapid increments may not be detectable from two counter values alone; report reset-detection limits rather than certifying continuity without an epoch indicator.

The initial history contract should offer events, audit entries, MAPS minute summaries and daily summaries actually exposed by the connected device. Exact retention, pruning and per-platform limits are not established by this baseline audit. Do not promise a seven-day or thirty-day window. Daily date strings need an explicit switch-time-zone interpretation; invalid dates remain visible parse failures. Distinguish absent samples from confirmed zero events. [5][6]

General historical throughput/error rates and historical topology require retained snapshots or another telemetry source. SANnav is a separate API and permission/version contract; its existence does not automatically grant arbitrary FOS API proxying or every historical metric. Design that as a separate integration after checking the deployed SANnav version and exposed endpoints.

## RCA relationships and workflows

Normalize WWPNs to a stable representation while preserving the source value. Join NetApp FC-interface/initiator WWPNs to name-server devices, then locate the physical switch port through observed fabric-port identity and/or correctly scoped port index. One physical port can represent many NPIV logins; FCID is fabric-scoped and can change. FDMI registration is useful host metadata, not guaranteed authoritative host inventory. [4][10]

For connectivity failures, correlate registered endpoints, physical port state, effective zoning, fabric routes and link events. Defined zones alone do not establish active access. Preserve ordinary versus principal members for peer zoning and preserve member syntax such as WWPN, alias or domain/index. Do not assume that every pair of peer-zone members may communicate. Zoning permission does not prove host LUN visibility: array masking, host state and multipathing remain separate evidence. [9]

For slow I/O, locate the relevant host/target ports, inspect current rates, link quality and optics, then compare MAPS congestion samples and events with the array incident window. Native MAPS classifications are observations; a high counter or correlation alone is not a proven root cause. FPI thresholds describe policy, not measured per-I/O latency. Avoid attributing all traffic on a shared ISL to one volume.

For a suspected change, use audit events, firmware history and current configuration. Current zoning/topology can explain current reachability, but cannot establish what was configured yesterday without historical evidence. Report that distinction in the investigation output.

## Connector logo

Use the requested [Brocade logo](https://www.pngkey.com/png/full/9-96232_brocade-logo-brocade-communications-systems-inc.png) for the connector's catalog and connection UI. The selected asset is now included in the connector and was visually checked in the catalog and generated form. See the implementation feedback loop for screenshots.

## Verification plan and release criteria

Before claiming customer-version verification, obtain the FOS 9.1.1 API manual/YANG package applicable to 9.1.1c or documented patch compatibility. Reconcile module revisions and field differences against the baseline. A sanitized module-version response, switch model and virtual-fabric layout are enough to narrow the catalog substantially; live credentials are not needed for this design phase.

Use three verification stages, without requiring a physical lab to begin development:

1. **Local simulated API:** build an ARM-compatible container suitable for the M-series Mac and use the sandbox-feedback-loop procedure to exercise the real connector against HTTP responses, authentication lifecycle, virtual-fabric scope and changing diagnostic scenarios. This models the API; it does not run Fabric OS. The public PyFOS Docker setup packages a client and still requires a switch endpoint. No public downloadable FOS 9.1.1c simulator was identified in the research. [3]
2. **Recorded response replay:** when available, seed separate fixtures with sanitized raw REST responses captured from a real 9.1.1c switch. Preserve namespaces, data types, singleton/list shapes, relevant error bodies and consistent anonymized identities across resources; remove credentials and session tokens. Record firmware, model, endpoint, scope and capture provenance. These fixtures validate observed response handling, not live request acceptance. Independently captured fixtures reduce the risk that the simulator and connector share the same incorrect assumption.
3. **Real-switch smoke test:** when customer or partner lab access is available, perform bounded read-only checks of authentication, module discovery, permitted FIDs and representative primary/advanced resources. Record exactly which firmware, hardware, roles and endpoints were checked. Until then, label the result simulation-verified, with replay verification stated separately where applicable; do not claim live 9.1.1c compatibility.

Recorded fixtures and real-switch access are later verification inputs, not prerequisites for building the simulator. No lab has been provisioned, no switch has been accessed and no fixtures have been obtained from the customer during planning.

After implementation is authorized, the simulated API feedback loop should cover:

1. Both recorded authentication conventions as separate fixtures; session expiration and cleanup; no hidden SSH or operation calls.
2. Module discovery's special URI; missing/new objects; changed version; restricted discovery without falsely emptying the catalog.
3. Multiple FIDs with identical port names, concurrent queries and chassis-context event records.
4. XML namespaces, singleton/list/empty shapes, large uint64 values, inherited units and malformed payloads.
5. Exact resource keys, encoded slot/port values, invalid fields/operators, scope override attempts and GET-body/keyed-URI contract cases where enabled.
6. Defined/effective zoning, peer principals, aliases, domain/index members and many-to-many NPIV relationships without measure duplication.
7. Known empty errors versus denied, unsupported, not-ready and transient-busy responses.
8. Complete collection reads, endpoint-specific continuation where applicable, byte/row limits, global ordering and no misleading partial totals.
9. Time filtering, day boundaries, retained-window gaps, monitoring-paused samples, counter resets and stale refresh times.
10. A realistic RCA fixture linking a NetApp target WWPN to a Brocade port, matching congestion/error evidence, and preserving the conclusion's limits.
11. Exactly 12 primary catalog definitions with explicit advanced discovery; both levels enforce the same availability and query policy.
12. Omitted `fields` returns the complete reviewed supported schema; optional projection works; secrets/action controls and unreviewed drift fields stay excluded. Verify nullable missing values, preserved identity/time metadata and explicit failures on oversized results.
13. All-fields queries make only the selected view's declared constituent requests. Extra enrichment lookups require an explicit request; heterogeneous unions preserve type/source and report unavailable constituents.

Accept the connector when every reviewed GET resource has an include/conditional/exclude disposition; every exposed table has a tested request/normalization mapping; forbidden calls are absent from transport logs; and all query failures preserve their meaning. Simulation validates the modeled behavior. A small read-only 9.1.1c smoke test remains necessary to call the connector customer-version verified.

## Sources

Vendor source files were read from downloaded public repository archives. Links below identify the exact source files; archive SHA-256 fingerprints are in the companion JSON. No source branch snapshot should be mistaken for a support commitment for FOS 9.1.1c.

1. Broadcom, [FOS 9.1.0b YANG package](https://github.com/brocade/yang/tree/master/9.1.0/9.1.0b). Structural inventory and resource coverage.
2. Broadcom, [module-version model](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-module-version.yang) and [PyFOS module-version adapter](https://github.com/brocade/pyfos/blob/master/pyfos/pyfos_brocade_module_version.py). Discovery fields and special URI.
3. Broadcom, [PyFOS README](https://github.com/brocade/pyfos). Documented version coverage.
4. Broadcom, [interface model](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-interface.yang). Port fields, statistics, replacements and semantics.
5. Broadcom, [logging model](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-logging.yang). Event records versus logging configuration.
6. Broadcom, [MAPS model](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-maps.yang). Health, policies, minute samples and daily summaries.
7. Broadcom, [media model](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-media.yang) and [trunk model](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-trunk.yang). Optical and throughput units.
8. Broadcom, [Ansible connection](https://github.com/brocade/ansible/blob/master/utils/brocade_connection.py), [URL handling](https://github.com/brocade/ansible/blob/master/utils/brocade_url.py) and [facts](https://github.com/brocade/ansible/blob/master/library/brocade_facts.py). Authentication, XML and error handling.
9. Broadcom, [zoning model](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-zone.yang). Defined/effective configuration, membership and action fields.
10. Broadcom, [logical switch](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fibrechannel-logical-switch.yang), [name server](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-name-server.yang) and [FDMI](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/brocade-fdmi.yang) models. Identity and membership.
11. Broadcom, [PyFOS REST utilities](https://github.com/brocade/pyfos/blob/master/pyfos/pyfos_rest_util.py) and [HTTP utilities](https://github.com/brocade/pyfos/blob/master/pyfos/pyfos_util.py). GET body/URI modes, VF query argument, XML handling and fallback behavior.
12. Broadcom, [PyFOS login](https://github.com/brocade/pyfos/blob/master/pyfos/pyfos_login.py). Login/logout and credential header variants.
13. Dell, [Configuring Brocade switches for REST discovery](https://www.dell.com/support/manuals/en-us/vipr-srm/srm_5100_solg/configuring-brocade-switches-for-rest-discovery?guid=guid-49d8648b-e474-4b68-8f3b-e5cd41da578d&lang=en-us). Version-sensitive throttling configuration.
14. Broadcom, [FC shared types](https://github.com/brocade/yang/blob/master/9.1.0/9.1.0b/fibrechannel-yang-types.yang). Inherited time and identity semantics.
15. BOW repository, `backend/app/data_sources/clients/base.py` and `backend/app/schemas/datasource_table_schema.py`. Existing client/catalog interface; local source inspection.

## Implementation handoff

Implemented on `codex/brocade-rca-connector`: 12 primary views, 156 conditional advanced resources, native allowlisted XML REST reads, the JSON query contract, bounded results with evidence columns, the selected logo, and an ARM-compatible synthetic Docker API. See [the runnable verification guide](../../tools/brocade/README.md) and [feedback loop](../feedback-loops/brocade-connector.md). Module advertisement does not certify a resource as accessible or customer-version verified. Real-response replay and switch acceptance remain separate future evidence.
