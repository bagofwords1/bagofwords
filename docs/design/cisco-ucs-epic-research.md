# Cisco epic — research and plan (no implementation)

Assessed 2026-11-10. Nothing was built. No UCS domain, emulator or Intersight
tenant was queried, so every wire-format claim below comes from vendor docs,
the official SDK source or community reports. Each claim carries a grade:
**[V]** verified from a primary source, **[P]** partial (secondary source,
older doc version, or only part of the claim confirmed), **[I]** inferred.

> **Update 2026-11-10 — ACI APIC.** ACI fabrics sit in the same incident
> path and are in demand from the same customers. APIC is a better fit for our
> pattern (GET-only reads) and shares UCSM's object model (`faultInst`,
> `eventRecord`, `aaaModLR`, DN and filter grammar). Recommendation: **build
> `cisco_apic` first, then `cisco_ucsm` reusing its DN, filter and fault code.**
> Research: [`cisco-apic-connector-research.md`](cisco-apic-connector-research.md);
> the read-only `cisco_apic` connector is implemented (see
> `docs/feedback-loops/cisco-apic-connector.md`).

## TL;DR — decision for the maintainer

- **What "Cisco" means here.** The ask comes from the Nov 5 task list ("רשימת משימות", Drive):
  *"Hp one view, **Cisco ucsm fi**, open manage (dell), checkpoint, Palo Alto,
  F5"*. PR bagofwords1/bagofwords#1634 shipped four of the six and left
  *"Cisco UCS Manager and Palo Alto connectors (researched, not built)"* as a
  follow-up. So the epic is **UCS Manager running on the Fabric
  Interconnects**: blades, rack servers, FIs, IOMs, ports, service profiles,
  faults. It is not Meraki, campus or Webex.
- **There is nothing to adopt.** No UCS Manager MCP server exists, official or
  community (GitHub, PyPI, glama, mcp.so, pulsemcp and mcpservers.org all
  searched) [P: absence of evidence]. There is no official Intersight MCP
  either. The only community Intersight MCP enforces read-only with an env flag
  over a CRUD codebase, which is below our "mutation impossible" bar. **Build
  native**, using the same frozen-catalog pattern as `hpe_oneview`,
  `checkpoint`, `f5_bigip` and `dell_ome`.
- **Phase 1: a `cisco_ucsm` connector** on the UCSM XML API. **Phase 1b: a
  `cisco_intersight` connector**, because Cisco has put UCSM on an EOL path
  (6.0: end of sale 2027-12-31, last support 2030-12-31 [V]) and FIs running
  in Intersight Managed Mode (IMM) have no UCSM API.
- **The biggest design difference from the siblings:** every UCSM call is an
  HTTP POST, including reads. The read-only guarantee therefore has to be an
  allow-list of **XML method names**, not of HTTP verbs. Check Point already
  works this way (POST `show-*`), so that client is the closest template.
- **The biggest risk:** session-cap lockout. UCSM limits sessions per user
  (default reportedly 32 [P]). A connector that leaks sessions locks the
  customer's own GUI and automation out of the shared account. Session hygiene
  is a must-have, not a polish item.

## 1. Where the ask came from (evidence)

| Source | What it says |
|---|---|
| Google Drive "רשימת משימות" (2026-11-05) | Connector list: *Hp one view, Cisco ucsm fi, open manage (dell), checkpoint, Palo Alto, F5* |
| PR bagofwords1/bagofwords#1634 (merged 2026-11-08) | Shipped F5, Check Point, Dell OME and HPE OneView. Follow-ups: *"Cisco UCS Manager and Palo Alto connectors (researched, not built)"* |
| Repo | No UCS or Intersight code. The only Cisco connector is AppDynamics (`appdynamics_client.py`, `docs/design/appdynamics-connector.md`) |
| Linear (team BOW), GitHub issues | No Cisco or UCS issue or project exists yet |

The customer is the same infra-RCA buyer whose incident path crosses
HPE/Dell compute, F5, Check Point, NetApp, Brocade and vCenter. UCS sits in
that path as compute plus the converged LAN/SAN edge, because the FIs carry
both the server Ethernet and the FC uplinks.

## 2. Product options considered

| Option | Verdict |
|---|---|
| **Native UCSM XML API connector** | **Yes, Phase 1.** This is the explicit ask and the only source of FSM faults, event and audit logs, port and pinning detail, and stats |
| Native Intersight connector | **Yes, Phase 1b.** Covers IMM domains, which UCSM cannot see, plus fleet inventory, HCL, firmware and telemetry. It does not replace UCSM, for the reasons below |
| Intersight only (it also mirrors UCSM-mode domains) | **No.** Intersight drops flapping faults and **FSM faults** (exactly the "service profile association failed" case), reduces severity to Critical/Warning, polls every 10 min on Essentials, and does not expose UCSM event, audit or stats data [V: Intersight Alarms Reference Guide, 2026-08] |
| Attach a community MCP (`jim-coyne/Intersight_MCP`, `schwarztim/cimc-mcp`) | **No** as the product path. Single maintainers, stdio only, read-only by config flag; the CIMC one explicitly excludes UCSM-managed servers [P]. Useful only as endpoint references |
| Generic Redfish connector | **Later, separate epic.** Redfish exists on standalone CIMC (IMC 3.0+) and on iLO and iDRAC, but **not on UCSM** [P]. One Redfish connector would cover standalone C-series, HPE and Dell bare metal together |

## 3. UCSM XML API — facts the connector depends on

### 3.1 Transport and session

- **One endpoint [V].** Every call is a POST of one XML element to `https://<VIP>/nuova` (443, or 80 if enabled) with no XML declaration. Sources: API Guide ch. 1 and 10; SDK `ucssession.py:263`.
- **Login [V].** `<aaaLogin inName=".." inPassword=".."/>` returns `outCookie`, `outRefreshPeriod` (600), `outPriv` (for example `read-only`), `outDomains`, `outVersion` and `outSessionId`. Failure returns `errorCode="551"` (Guide ch. 10; `methodmeta/AaaLoginMeta.py`).
- **The cookie is an attribute, not an HTTP cookie [V].** Every method element carries `cookie="…"`.
- **Refresh rotates the cookie [V].** `aaaRefresh` needs `inName`, `inPassword` and `inCookie` and returns a *new* cookie. The 600 s idle expiry means the connector must keep the password in memory for the duration of an operation.
- **Logout [V].** `<aaaLogout inCookie=".."/>`. The SDK treats 555 as "already logged out" [P].
- **Session limits.** At most 256 sessions per system [V], plus a configurable per-user cap. Community reports put the per-user default at 32 and show lockouts caused by leaky pollers (`User Reached maximum session limit`) [P].
- **Auth domains [P].** The login name is `ucs-<Domain>\<user>`, and the domain is case-sensitive. `ucs-Local\user` reaches a local account when the native realm is LDAP, RADIUS or TACACS+. Remote login IDs are limited to 32 characters including the prefix.
- **Connect to the cluster VIP [P/I].** The DME runs on the primary FI. `mgmtEntity.leadership` shows which FI is primary.
- **Not-UCSM detection [V].** After login the SDK resolves `networkElement` and rejects the target if none comes back. This catches a CIMC or an IMM FI.
- **No REST or Redfish on UCSM [P].** The documented interfaces are GUI, CLI, XML and SNMP.
- **TLS [V].** ucsmsdk **does not verify certificates** (`ucsdriver.py:84-94`), and UCSM ships a self-signed `default` keyring. We must not inherit that default: verify always, with an optional PEM CA, as every sibling connector does.

### 3.2 Read-only by construction

A POST is not a read signal, so the client must freeze the set of XML **method
element names** it can serialise.

| Allowed (read) [V names] | Never allowed |
|---|---|
| `configResolveDn`, `configResolveDns`, `configResolveClass`, `configResolveClasses`, `configResolveChildren`, `configResolveParent`, `configScope`, `configCountClass`, `configFindDnsByClassId` | `configConfMo(s)`, `configConfMoGroup`, `configConfFiltered`, `configConfRename` |
| Session only: `aaaLogin`, `aaaRefresh`, `aaaLogout` | `lsClone`, `lsInstantiate*`, `lsTemplatise`, `equipmentClone/Instantiate*/Templatise` |
| Optional, after verification on UCSPE: `configResolveClassSorted` + `configResolveContext` / `configReleaseResolveContext` (paging) | `faultAckFault(s)`, `faultResolveFault`, `statsClearInterval`, `configRefreshIdentity`, `trigPerformTokenAction`, `fsmDebugAction`, `aaaChangeSelfPassword` |
| | **Token minting:** `aaaGetNComputeAuthTokenByDn`, `aaaGetKVMLaunchUrl`, `aaaTokenLogin` (these issue KVM/CIMC access) |
| | `eventSubscribe` (long-lived stream; does not fit request/response) |

Defence in depth:

- The catalog loader refuses any method outside the read set, mirroring `checkpoint_client._catalog()`.
- After login, the client refuses the session unless `outPriv` is **exactly** `read-only` [I]. This catches an admin account by mistake.
- Recommended service account: a local user, or `ucs-<domain>\svc`, with only the built-in **read-only** role and no locales. The read-only role gives "read-only access to system configuration with no privileges to modify the system state" [P]. Server-side enforcement could not be tested.

### 3.3 Query model

- **Filters [V]** (Guide ch. 10; SDK `ucsfiltertype.py`):
  - Comparisons: `eq ne gt ge lt le`.
  - `wcard` is a **regex**, not a glob.
  - `anybit` and `allbits` take comma-separated bitmask values.
  - `bw` is inclusive.
  - Composites: `and`, `or`, `not`.
  - Matching is case-sensitive; there is no server flag for case-insensitive matching.
  - The SDK nests and/or groups larger than 10 children, so the server likely limits a group to 10 [I].
- **No paging on `configResolveClass` [V].** A cursor API exists (`*Sorted` with `inSize`, which returns `outContext`/`outTotalSize`, then `configResolveContext`), but ucsmsdk never uses it [V]. Its real-world behaviour is unverified, so it must not be relied on until it has been tested on UCSPE.
- **Size before fetching [V].** `configCountClass` returns `outCount`. Use it as a pre-flight to keep the "complete result or explicit failure" contract, the same `max_rows` semantics as the siblings.
- **`inHierarchical="true"` can return "almost the entire UCS configuration" and is slow [V].** Default to false, and allow true only for stats DNs.
- **A missing DN is not an error [V].** It returns an empty `<outConfig/>`, so "not found" must be distinguished from "healthy".
- **Errors [V].** `errorCode`, `errorDescr` and `invocationResult` on the method, or a top-level `<error>`. Codes: 551 means authentication failed [V]; 552 means the cookie expired, so re-login once [P]; 555 means no session [P].
- **Timestamps [V format].** For example `2015-09-07T10:31:06.608`, with **no offset**. They are probably FI local time per `commDateTime` [I]. Normalise using `topSystem.currentTime` against our clock.
- **Malformed XML [V].** UCSM can emit unescaped `<`, `>` and `&` inside attributes; the SDK has a recovery path (`ucsgenutils.remove_invalid_chars`). Parse with `defusedxml` plus that tolerant pre-pass.

### 3.4 Proposed catalog (about 32 tables, one class each)

Class ids, rn patterns and property names come from `ucsmsdk/mometa/**` 0.9.27 [V]. The full DN examples are assembled from those rn patterns [I].

| Table | classId | Answers |
|---|---|---|
| `system` | topSystem | Domain name, VIP, cluster mode, current time (clock skew), uptime |
| `fabric_interconnects` | networkElement | Is FI A or B degraded (operability, thermal, OOB IP)? |
| `cluster_state` | mgmtEntity | HA ready? Which FI is primary? Failover reason, version mismatch |
| `chassis` | equipmentChassis | Chassis oper/power/thermal/connectivity |
| `iom` | equipmentIOCard | IOM / fabric-path failure |
| `fex` | equipmentFex | FEX for rack servers |
| `blades` / `rack_servers` | computeBlade / computeRackUnit | Down, mid-FSM or failed discovery; association; `fsmRmtInvErrDescr` |
| `motherboards`, `cpus`, `dimms` | computeBoard, processorUnit, memoryUnit | Component degradation |
| `storage_controllers`, `local_disks`, `virtual_drives` | storageController, storageLocalDisk, storageVirtualDrive | RAID / disk failure |
| `psus`, `fans` | equipmentPsu, equipmentFanModule/equipmentFan | Power-feed or cooling loss (chassis, FI, rack) |
| `adapters`, `host_vnics`, `host_vhbas` | adaptorUnit, adaptorHostEthIf, adaptorHostFcIf | VIC or host vNIC/vHBA down |
| `fi_ethernet_ports`, `fi_fc_ports` | etherPIo, fcPIo | Which uplink or server port is down, and **why** (`stateQual`) |
| `iom_host_ports` | etherServerIntFIo | IOM host-facing ports |
| `uplink_port_channels` | fabricEthLanPc(+Ep) | Port-channel member down |
| `vlans`, `vsans` | fabricVlan, fabricVsan | Config issues, overlap state per FI |
| `service_profiles` | lsServer | **Association failed or pending reboot** (`assocState`, `configState`, `operQualifier`, `fsmStatus`) |
| `service_profile_bindings` | lsBinding | Which physical server a profile is bound to |
| `faults` | faultInst | **Primary RCA table**: code, severity, cause, descr, created, lastTransition, occur, ack. The affected DN is the fault DN minus its last rn |
| `events` | eventRecord | System event timeline (**time filter required**) |
| `audit_log` | aaaModLR | Who changed what and when (**time filter required**) |
| `firmware_running`, `firmware_status` | firmwareRunning, firmwareStatus | Version drift; stuck upgrades |
| `fi_system_stats`, `fi_env_stats` | swSystemStats, swEnvStats | FI CPU/memory/thermal pressure |
| `port_stats` | etherRx/Tx/Err/Loss/PauseStats | CRC/FCS errors, discards, pause (congestion) |
| `fc_port_stats` | fcStats, fcErrStats | FC errors |
| `power_thermal_stats` | equipmentPsuStats, computeMbPowerStats, processorEnvStats | Power and thermal trend (current/min/max/avg) |

**Verify on UCSPE before GA.** The SDK lists the privilege for `faultInst` as
admin/fault/operations, and for `firmwareStatus` and `licenseInstance` as
admin. The UCSM model *should* give every role read access, but this is
unverified [P]. If the read-only role cannot see `faults`, the connector is
useless, so this is the first check to run.

Each table keeps the sibling conventions: frozen fields, `_bow_*`
provenance columns, `limit` as a complete-result ceiling (never silent
truncation), and `lookback` resolved at execution for `faults`, `events`,
`audit_log` and the stats tables (see `relative_date_hint` in
`hpe_oneview_client.py`).

## 4. Intersight connector (Phase 1b) — facts

- **Hosts [V].** `https://intersight.com/api/v1` (US), `https://eu-central-1.intersight.com/api/v1` (EU), or `https://<appliance>/api/v1` for the Connected or Private Virtual Appliance. The config needs a single host field.
- **Auth [V].**
  - API key with HTTP Signature (`Digest` + `Authorization`). v2 keys are RSA (`rsa-sha256`); v3 keys are EC (`hs2019`).
  - OAuth2 client-credentials is also supported.
  - A user can hold at most 3 keys [P].
  - Whether a key can be scoped narrower than its creating user is contradictory in Cisco's own docs. Recommend a dedicated **Read-Only** user [I].
- **Query [V].** OData: `$filter`, `$select`, `$top` (**default 100, max 1000**), `$skip`, `$orderby`, `$count`, `$inlinecount`, `$expand`, `$apply`. Responses can be throttled with HTTP 429; Cisco publishes no numeric limits [V].
- **Telemetry [V].** Metrics come from Druid JSON POSTed to `/api/v1/telemetry/TimeSeries` (and TopN/GroupBy/Scan). These are reads sent as POST, so they need an explicit allow-list entry, just like UCSM.
- **Objects [V/P].**
  - `compute/PhysicalSummaries`: its `ManagementMode` is UCSM, Intersight or IntersightStandalone, so one connector also sees UCSM-mode and standalone servers at summary level.
  - `compute/Blades`, `compute/RackUnits`, `equipment/Chasses`, `network/ElementSummaries`
  - `cond/Alarms`, `cond/HclStatuses`, `aaa/AuditRecords`
  - `firmware/RunningFirmwares`, `server/Profiles`, `ether/PhysicalPorts`, `adapter/Units`, `equipment/Psus`, `equipment/Fans`, `workflow/WorkflowInfos`
- **Licensing [P].** Essentials gives 10-min collection with 90-day retention. Advantage gives 1-min collection with 2-year retention. Time-series RCA is weak on Essentials.
- **Sharing a family with UCSM [I].** Use the same table *concepts* (faults/alarms, servers, FIs, ports, profiles, audit) so the agent's playbooks carry over between the two connectors.

## 5. Test and validation strategy

The UCSM wire protocol is easy to fake and hard to obtain for real.

- **CI simulator:** add `tools/cisco_ucsm/simulated_api.py`, a loopback `/nuova` server in the style of `tools/hpe_oneview/simulated_api.py`. It should:
  - enforce the cookie, 600 s expiry, refresh rotation and a per-user session cap, so leaks fail tests;
  - reject every non-allow-listed method with the real error shape;
  - return `<outConfig/>` for a missing DN;
  - emit one malformed-attribute response;
  - plant root causes. Examples: FI-B `mgmtEntity` HA not ready; an uplink `etherPIo` down with `stateQual=sfp-not-present`; a blade with DIMM `operability=inoperable` and a service profile whose `assocState=failed` carries an FSM error; a PSU input lost; plus an `aaaModLR` entry for a VLAN deletion just before the incident.
- **Fixtures:** build them from Guide examples, the SDK prop metadata (every attribute per class) and the inline real responses in `ucsmsdk/tests/common/test_ucsfromxml.py`. Record every source in `tools/cisco_ucsm/SOURCES.md` with verified/partial/inferred grades, as the siblings do.
- **Real shapes:** **UCS Platform Emulator 4.2(2aPE1)** [P]. It is a free download behind a Cisco.com login (EULA) and ships as an OVA VM; there is no 4.3 or 6.x build, and download links were recently broken. That makes it a manual or nightly target, not GitHub CI. A DevNet "UCS Manager Emulator" sandbox exists **by reservation**; whether it is still in the catalog is unconfirmed [P].
- **Before GA:** one read-only pass on a customer domain, as PR #1634 required for its four connectors.

## 6. Risks and unknowns

| # | Unknown | Why it matters | How to close |
|---|---|---|---|
| 1 | Can the read-only role read `faultInst` and `firmwareStatus`? | If not, the connector loses its core RCA table | First check on UCSPE |
| 2 | Per-user session default (32?) on 4.3/6.0 | Lockout risk to the customer | UCSPE + customer; design for it regardless |
| 3 | Does `configResolveClassSorted` paging work? | Large `events` and `audit_log` reads | UCSPE; otherwise rely on count pre-flight + mandatory time filter |
| 4 | Timestamps: FI local time or UTC? | Wrong incident windows | Compare `topSystem.currentTime` on UCSPE |
| 5 | DN escaping for names containing `/` (`[...]`?) | Parent/child lookups | Treat DNs as opaque; verify on UCSPE |
| 6 | Is `/nuova` fully gone in IMM? | Error messaging | Cisco slides strongly imply yes [P]. Detect and say "this domain is in Intersight Managed Mode — use the Intersight connector" |
| 7 | Customer split, UCSM vs IMM | Phase 1b priority | Ask the customer, along with their UCSM version and FI model |
| 8 | Intersight key scoping and rate limits | Least privilege, throttling | Trial tenant |

## 7. Proposed epic breakdown

**Epic: Cisco UCS connectors (read-only, infra RCA)**

1. **UCSM — protocol client + simulator.** `cisco_ucsm_client.py` with the method allow-list, XML builder (no string interpolation), `defusedxml` with tolerant recovery, session per operation with `aaaLogout` in `finally`, a single in-flight request per domain, refresh at about 540 s, the `outPriv` check, not-UCSM/IMM detection, TLS always verified with an optional PEM CA, and typed errors (551/552/555/max-sessions). Also `tools/cisco_ucsm/{simulated_api.py,README.md,SOURCES.md}`.
2. **UCSM — catalog and query semantics.** `cisco_ucsm/catalog.json` (the ~32 tables above) and JSON→`inFilter` translation (with the 10-child group limit). Add a `configCountClass` pre-flight against `max_rows`, mandatory `lookback`/time filters on `events`, `audit_log` and stats, `_bow_*` provenance, timestamp normalisation, and the fault→affected-DN derivation.
3. **UCSM — product wiring.** `CiscoUcsmConfig` (VIP URL, optional auth domain, CA, timeout, max_rows, loopback-only `allow_http`) plus credentials in `configs.py`, `REGISTRY["cisco_ucsm"]` (category `infra`, `beta`, explicit `client_path`), icon, unit/e2e/integration tests, and `docs/feedback-loops/cisco-ucsm-connector.md` with a live Haiku RCA run against the simulator (**sandbox-feedback-loop**, **ui-evidence**).
4. **UCSM — UCSPE validation pass.** Close risks 1–5 above and update the `SOURCES.md` grades.
5. **Intersight — client + catalog.** HTTP Signature v2/v3 and OAuth2 client-credentials, configurable host, OData paging (`$top` ≤ 1000), 429 backoff, an allow-list of GETs plus the telemetry POSTs, and alarms/summary/HCL/firmware/audit/profile/port tables named to match the UCSM concepts.
6. **Intersight — product wiring + trial-tenant validation.**
7. **Docs and release.** Docs page (**docs-update**), `VERSION`/`CHANGELOG.md` (**release-notes**), and "how to create a read-only UCSM/Intersight service account".

Explicitly **out of scope** for this epic, as candidates for a later "Cisco DC fabric" epic:

- NX-API REST (standalone Nexus). ACI APIC has moved into scope: see `cisco-apic-connector-research.md`.
- Nexus Dashboard.
- Secure Firewall FMC.
- Meraki, Catalyst Center, ThousandEyes and Splunk, where attaching Cisco's official MCP servers is cheaper than a native build. Meraki's official MCP is read-only by design [V]; ThousandEyes' is not [V].
- Palo Alto, the other leftover item from the same list, which is a separate epic.

## Sources

**Cisco UCSM XML API Programmer's Guide (3.1)**
- [Ch. 1, Overview](https://www.cisco.com/c/en/us/td/docs/unified_computing/ucs/sw/api/UCSM_API_Guide/b_UCSM_API_Guide_3_1_chapter_01.html)
- [Ch. 10, Methods and filters](https://www.cisco.com/c/en/us/td/docs/unified_computing/ucs/sw/api/UCSM_API_Guide/b_UCSM_API_Guide_3_1_chapter_010.html)
- [Ch. 11, Method descriptions](https://www.cisco.com/c/en/us/td/docs/unified_computing/ucs/sw/api/UCSM_API_Guide/b_UCSM_API_Guide_3_1_chapter_011.html)
- [Privileges](https://www.cisco.com/c/en/us/td/docs/unified_computing/ucs/sw/api/UCSM_API_Guide/b_UCSM_API_Guide_3_1_chapter_0100.html)

**ucsmsdk 0.9.27** (Apache-2.0, released 2026-08-10, supports UCSM 6.0(2d))
- [PyPI](https://pypi.org/project/ucsmsdk/) and [GitHub](https://github.com/CiscoUcs/ucsmsdk)
- Files read: `ucssession.py`, `ucsdriver.py`, `ucsfilter.py`, `ucsfiltertype.py`, `ucsxmlcodec.py`, `ucsgenutils.py`, `methodmeta/*`, `mometa/**`, `tests/common/test_ucsfromxml.py`

**Lifecycle**
- [UCSM 6.0 EOL](https://www.cisco.com/c/en/us/products/collateral/servers-unified-computing/ucs-manager/ucs-manager-release-6-0-eol.html), [UCSM 4.3 EOL](https://www.cisco.com/c/en/us/products/collateral/servers-unified-computing/ucs-manager/ucs-manager-release-4-3-eol.html), [EOL FAQ](https://www.cisco.com/c/en/us/products/collateral/servers-unified-computing/ucs-manager/ucs-manager-eol-faq.html)
- [About IMM](https://www.cisco.com/c/en/us/td/docs/unified_computing/Intersight/b_Intersight_Managed_Mode_Configuration_Guide/m_review_about_imm.html), [BRKDCN-2938](https://www.ciscolive.com/c/dam/r/ciscolive/emea/docs/2024/pdf/BRKDCN-2938.pdf)

**Sessions and auth (community)**
- [Session-limit lockout thread](https://community.cisco.com/t5/unified-computing-system/failed-login-info-user-reached-maximum-session-limit-ucsm-2-0-2q/m-p/1928904/highlight/true)
- [AD login syntax](https://community.cisco.com/t5/cisco-developed-ucs-integrations-discussions/python-ucs-sdk-and-connecting-to-ucs-that-uses-active-directory/td-p/3558482)
- [LDAP troubleshooting 200092](https://www.cisco.com/c/pt_br/support/docs/servers-unified-computing/ucs-infrastructure-ucs-manager-software/200092-UCSM-LDAP-Troubleshooting-guide.html)

**Emulator and sandbox**
- [UCSPE downloads](https://community.cisco.com/t5/unified-computing-system-knowledge-base/ucs-platform-emulator-downloads-ucspe-4-2-2a-ucspe-4-1-2c/tac-p/4463018)
- [DevNet UCSM/UCSD sandbox](https://developer.cisco.com/docs/ucs-dev-center-ucs-director/sandbox)

**Intersight**
- Developer docs: [authentication](https://developer.cisco.com/docs/intersight/authentication/), [query syntax](https://developer.cisco.com/docs/intersight/query-syntax), [error handling](https://developer.cisco.com/docs/intersight/api-error-handling), [telemetry query syntax](https://developer.cisco.com/docs/intersight/telemetry-apis-query-syntax)
- [Alarms Reference Guide](https://www.cisco.com/c/en/us/td/docs/unified_computing/Intersight/IMM_Alarms_Guide/b_cisco_intersight_alarms_reference_guide.pdf)
- [PhysicalSummary model](https://www.pulumi.com/registry/packages/intersight/api-docs/getcomputephysicalsummary)
- [intersight-auth](https://pypi.org/project/intersight-auth/)

**MCP landscape**
- Community servers: [jim-coyne/Intersight_MCP](https://github.com/jim-coyne/Intersight_MCP), [schwarztim/cimc-mcp](https://github.com/schwarztim/cimc-mcp), [CG-AI-LAB/mcp-redfish](https://github.com/CG-AI-LAB/mcp-redfish)
- Cisco official: [Meraki official MCP](https://github.com/CiscoDevNet/cisco-meraki-mcp-official), [Catalyst Center MCP](https://github.com/cisco-en-programmability/catc-mcp-oss), [ThousandEyes MCP](https://docs.thousandeyes.com/product-documentation/integration-guides/thousandeyes-mcp-server)

**Other**
- [Redfish on IMC](https://blogs.cisco.com/datacenter/cisco-supports-redfish-standard-api-enhances-ucs-programmability)
- [UCS faults reference 4.3](https://www.cisco.com/c/en/us/td/docs/unified_computing/ucs/ucs-manager/Reference-Docs/Faults-Error-Msgs/4-3/b_cisco_ucs_faults_and_error_messages_reference-4-3/m_sem-faults_4-3.html)

**In-repo templates**
- `backend/app/data_sources/clients/checkpoint_client.py` (POST allow-list)
- `hpe_oneview_client.py` (session and lookback)
- `tools/hpe_oneview/`
- `docs/design/brocade-fos-911c-rca-connector-research.md`
