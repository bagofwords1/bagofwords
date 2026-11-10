# Cisco ACI APIC connector — research (no implementation)

Assessed 2026-11-10. Companion to `docs/design/cisco-ucs-epic-research.md`.
Nothing was built, and no APIC was queried. The DevNet always-on sandbox was
unreachable from this environment.

Each claim carries a grade: **[V]** verified from a primary source or source
code, **[P]** partial (secondary or forum evidence), **[I]** inferred.

## TL;DR

- **Demand.** Infra-RCA customers run ACI fabrics alongside the compute, load-balancer and firewall estates our other connectors cover, and already investigate them through ad-hoc APIC tooling. Typical first questions ("which version runs on each controller and switch?") map to single class reads such as `firmwareCtrlrRunning` and `firmwareRunning`.
- **APIC is a better fit for our frozen-catalog pattern than UCSM.**
  - Reads are plain `GET /api/class|mo|node/...json` [V].
  - Writes are POST [V].
  - The only POST we ever need is `aaaLogin`, and with signature auth we need none at all.
  - So "GET-only plus a catalog allow-list" makes writes impossible, as it does for OneView, F5 and Dell. Three GETs must be blocked explicitly; see §2.
- **It is the same object model as UCSM.** `faultInst`, `eventRecord`, `aaaModLR`, `topSystem`, DNs and a filter grammar (`eq/ne/gt/lt/bw/wcard/anybit/and/or/not`) [V]. The DN, filter and fault-normalisation code can be shared between the two connectors.
- **Recommendation.** Make **`cisco_apic` Phase 1 of the Cisco epic, alongside or ahead of UCSM**, because there is live demand for it. Use signature (certificate) auth by default and about 38 tables. Validate on a customer 6.x fabric with a `read-all` user.
- **Biggest open question.** Per Cisco's model metadata, `faultInst`, `faultRecord`, `eventRecord`, `aaaModLR` and the stats classes list **`admin`** as their read access [V metadata]. Whether a `read-all` user sees them is unknown [P]. If it does not, a read-only account returns empty rows, not a 403 [I]. This is the first thing to test.
- **Lifecycle is safe.** ACI 6.1 maintenance runs to 2027-08-30, and last support is 2029-02-28 [V]. Cisco positions ACI inside "Nexus One", with Nexus Dashboard as the unified plane [P].

## 1. Transport and auth

| Fact | Grade / source |
|---|---|
| `POST /api/aaaLogin.json` with `{"aaaUser":{"attributes":{"name","pwd"}}}` returns `imdata[0].aaaLogin.attributes.{token, refreshTimeoutSeconds (~300–600), version}`. The token comes back as the `APIC-cookie` | [V] REST API Config Guide 4.2(x)+, Cobra `session.py` |
| `GET /api/aaaRefresh.json` rotates the token. `POST /api/aaaLogout.json`. `GET /api/aaaListDomains.json` needs no auth. Common clients re-login on 403 | [V] guide; DataDog `cisco_aci/api.py` |
| Remote login domains use the form `apic:<Domain>\\<user>` | [V] guide |
| **Every login, refresh and logout writes an `aaaSessionLR` audit record.** A per-query login floods the customer's audit trail | [V] model metadata |
| **Signature auth.** Attach an X.509 certificate to a *local* user (`uni/userext/user-<u>/usercert-<name>`). Sign `METHOD + path?query + body` with RSA-SHA256 PKCS#1 v1.5, no separators (the official docs wrongly show a space; Ansible `aci.py:631` notes this). Send the signature in the cookies `APIC-Certificate-Algorithm=v1.0; APIC-Certificate-DN; APIC-Certificate-Fingerprint=fingerprint; APIC-Request-Signature`. There is no login and no session. A trailing newline produces "bad signature" | [V] Ansible `cisco.aci` `module_utils/aci.py`, Cobra docs, Cisco community |
| Signature auth is for local users only, not LDAP, RADIUS or TACACS users | [I] |
| Throttling: `comm-policy → https → throttle 1-100 req/s` applies to the GUI and REST API. Expect 429/503 | [P] guide |
| Multi-APIC cluster: every APIC serves the full API, and there is **no VIP**. Configure 2–3 OOB addresses and fail over | [I]; DataDog keeps one session per URL [V] |
| APICs ship self-signed certificates. Verify always, with an optional PEM CA | [I] |

## 2. Read-only guarantee

- **Allowed:**
  - `GET` on `/api/class/<cls>.json`, `/api/mo/<dn>.json`, `/api/node/class/...` and `/api/node/mo/...`, with URLs built only from catalog templates and query parameters only from a fixed allow-list.
  - `POST /api/aaaLogin.json`, from the auth layer only and only for password mode.
- **Blocked explicitly, even though they are GETs or near-reads:**
  - `subscription=yes`, which opens a websocket subscription [V].
  - `/api/subscriptionRefresh.json` [V].
  - `aaaRefresh` outside the auth layer.
  - `/files/*` (tech-support and backup downloads), `/cobra/_downloads`, `/appcenter` and `/mqapi2` [I].
- **Disallowed as a load guard:** `rsp-subtree=full` at the `uni` or `topology` root [I].
- **Service account:**
  - A local user such as `bow-ro` with the `read-all` role in security domain `all`, read privilege only, and a 2048-bit or larger RSA certificate [P: Qualys guidance, community threads].
  - If records or logs come back empty, the customer decides whether to add `admin` *read*. Read and write privileges are separate in APIC.
- **Secrets:** strip every property flagged `secure:true` in the model (for example `bgpPeer.password`), and never expose the `aaa*` user and key classes. This is why the catalog uses `bgpPeerEntry` and not `bgpPeer` [V metadata].

## 3. Query model

All of these are [V] from the guide and the Cobra request docs unless marked otherwise.

- **Scope:** `query-target=self|children|subtree` and `target-subtree-class=a,b`.
- **Filters:** `query-target-filter=` with `eq ne lt gt le ge bw not and or xor anybit allbits wcard`. A filter takes at most 20 expressions. `wcard` is unreliable on some properties, such as `fvCEp.ip` [P].
- **Response shaping:**
  - `rsp-subtree=no|children|full`, with `rsp-subtree-class` and `rsp-subtree-filter`.
  - `rsp-subtree-include=faults|health|stats|audit-logs|event-logs|fault-records|health-records|relations|tasks`, plus the modifiers `count|required|no-scoped`.
  - `rsp-prop-include=all|naming-only|config-only`.
- **Paging:**
  - `order-by=cls.prop|desc`, `page` (zero-based) and `page-size`. `totalCount` is a **string**.
  - Since 5.1(1) a page holds at most 2000 objects. Always set `page-size`.
- **Time windows:**
  - `time-range=24h|1week|...|yyyy-mm-dd|yyyy-mm-dd` works on record classes in 5.2(3)+.
  - Alternatively filter directly, for example `gt(faultInst.lastTransition,"2025-03-12T14:53:06.041+00:00")`, as DataDog does.
- **Limits:**
  - Classes with more than 100k objects fabric-wide fail intermittently with **503** ("destination is not available" or "result dataset is too big", 90 s). Cisco's advice is to retry 3–5× and filter.
  - Event queries are capped at about 100k records.
- **Errors:** `{"imdata":[{"error":{"attributes":{"code","text"}}}]}` with 400, 403 or 503. A 403 can mean either an expired token or RBAC.
- **Data formats:**
  - Timestamps are ISO-8601 with milliseconds **and an offset** (`2025-03-12T14:50:49.955+00:00`). This is unlike UCSM, which gives no offset.
  - All numbers come back as strings.
- **Switch-resident classes:** `l1PhysIf`, `ethpmPhysIf`, `bgpPeerEntry`, `isisAdjEp` and the stats classes live on the switches.
  - A fabric-wide class query fans out to every switch [I].
  - Prefer the node-scoped form `/api/node/class/topology/pod-1/node-101/<cls>.json` [V, DataDog].
  - Expect slow responses or 503s when a switch is unreachable, which is exactly when RCA runs.

## 4. Proposed catalog (~38 tables)

DN patterns and properties are taken from Cisco's machine-readable model metadata (jsonmeta, https://pubhub.devnetcloud.com/media/model-doc-latest/docs/doc/jsonmeta/<pkg>/<Class>.json) [V]. The jsonmeta can generate the catalog and validate it in CI: it lists `dnFormats`, `readAccess`, the version each class was added in, and `secure` props.

| Group | Tables (class) | Answers |
|---|---|---|
| Inventory and versions | `fabricNode`, `topSystem`, `firmwareCtrlrRunning`, `firmwareRunning` | Which nodes are inactive or disabled; reboot cause and uptime; clock skew; version mismatch |
| APIC cluster | `infraWiNode`, `infraCont` | Is the controller cluster "fully-fit"? |
| Hardware | `eqptCh`, `eqptPsu`, `eqptFan`/`eqptFt`, `eqptLC` | PSU, fan and line-card faults |
| Health | `fabricHealthTotal`, `fabricOverallHealthHist5min`, `healthInst` (scoped) | Current health; *when* health dropped |
| Faults, events, audit | `faultInst`, `faultDelegate`, `faultSummary`, `faultRecord`, `eventRecord`, `aaaModLR`, `healthRecord`, (`aaaSessionLR`) | Active faults (the primary RCA table); fault and event history (link flaps, endpoint moves); **who changed what before the incident** |
| L1 / L2 | `l1PhysIf`, `ethpmPhysIf` (`operStQual` = why the port is down), `pcAggrIf`, `vpcDom`, `vpcIf`, `lldpAdjEp`/`cdpAdjEp`, `fabricLink` | Port, port-channel and vPC failures; cabling |
| Underlay and routing | `isisAdjEp`, `bgpPeerEntry`, `ospfAdjEp` | Fabric underlay adjacency; L3Out peer flaps (`lastFlapTs`, `stReason`) |
| Tenant and policy | `fvTenant`, `fvAp`, `fvAEPg`, `fvBD`, `fvCtx`, `fvSubnet`, `fvCEp`(+`fvIp`), `l3extOut`, `vzBrCP`/`vzSubj`/`vzFilter`/`vzEntry` | **"Where is IP or MAC x"** (leaf, port, EPG); is the traffic permitted by a contract? |
| Optional | `eqptIngrErrPkts5min`, `eqptEgrDropPkts5min`, `eqptIngrTotal5min`, `procSysCPU5min`, `procSysMem5min`, `compVm`/`compHv`, `infraAccPortP` chain | CRC errors, drops, utilisation, control-plane load, VMM context, access-policy chain |
| Deferred | `epmMacEp`/`epmIpEp` (leaf-local, 28 DN variants, read access `admin`), `coopEpRec`, `actrlRuleHitAg*` | Use `rsp-subtree-include=stats` instead |

These catalog rules carry over from the sibling connectors:

- Huge classes (`faultRecord`, `eventRecord`, `aaaModLR`, `healthRecord`, `fvCEp`, stats) require a `lookback` or time window, `page-size` ≤ 500, `order-by created desc`, and a total-row ceiling.
- Results are complete or fail explicitly; they are never silently truncated.
- `node-{n}` and `pod-{p}` are parsed out of DNs into columns.
- Fault `lc` is `soaking|raised|raised-clearing|retaining` [I]. Label retained faults as cleared.
- A 0-row result on an admin-read class carries "possibly RBAC-filtered" so the agent does not report "healthy".

## 5. DN and parsing gotchas

- **DNs nest brackets** [V]. Examples: `topology/pod-1/node-101/sys/phys-[eth1/1]`, `subnet-[10.1.1.1/24]`, `fd-[resPolCont/.../rtdOutDef-[uni/tn-X/out-Y]/node-101/stpathatt-[N5K-VPC]/nwissues]-fault-F0467`.
  - Never split a DN on `/`; parse with bracket depth.
  - URL-encode brackets, spaces, `#` and `%` in the path.
  - Quote filter values.
- Accept user DNs only when they match a catalog `dnFormat` regex.

## 6. Test and validation

| Source | Use |
|---|---|
| **DataDog `integrations-core/cisco_aci/tests/fixtures/{fabric,faults,tenant,capacity}`**: recorded real APIC JSON [V] | Best offline fixtures for response shapes and envelopes. Check the license before vendoring (likely BSD-3 [I]). Otherwise re-derive the fixtures |
| **Cisco jsonmeta** | Generate and validate the catalog (class names, DN formats, props, `readAccess`, `secure`) in CI, from a pinned snapshot |
| Ansible `cisco.aci` `module_utils/aci.py` | Reference for signature auth and error parsing |
| Loopback simulator `tools/cisco_apic/simulated_api.py` (to build) | Like the siblings: enforce the cookie or signature, page/page-size, bracketed DNs, the 2000-object page cap, a 503 "dataset too big" response, empty rows for RBAC-hidden classes, and refusal of every non-GET and `subscription=yes`. It should also plant root causes: a leaf uplink `ethpmPhysIf` down with an SFP qualifier, a `bgpPeerEntry` flap on an L3Out, a vPC peer-link inconsistency, an `aaaModLR` contract change minutes before an EPG-to-EPG drop, and a firmware mismatch on one node |
| ACI Simulator OVA 6.1(6)/6.2(3) [P] | Optional manual smoke test. It has no data plane, so endpoints, stats and real faults are sparse |
| DevNet ACI always-on (`sandboxapicdc.cisco.com`) [P] | Status unverified and unreachable from here |
| **Customer 6.x fabric, `read-all` user** | Acceptance: run every table once and settle RBAC visibility of faults, records, audit and stats |

Cobra (`acicobra`/`acimodel`) is **not on PyPI**; it is downloaded from an
APIC at `/cobra/_downloads` [V]. Do not depend on it, because the wire
protocol is small. The `cobra` package on PyPI is unrelated. `acitoolkit` is
archived [V].

## 7. Existing MCP servers (none is a by-construction read-only fit)

| Repo | Read-only? | Notes |
|---|---|---|
| CiscoDevNet/mcp_server_cisco_aci_community | No: ~47 tools including create and delete | DevNet community, not a supported Cisco product |
| automateyournetwork/ACI_MCP | No: `*_post` tools | Lists `firmwareRunning`/`firmwareCtrlrRunning` tools |
| **gokhan-ciftci/cisco-aci-mcp-server** | **Claims GET-only** | The only public server with named multi-APIC selection (`APIC_SERVERS[].apicname`). SSE, password auth, MIT |
| jim-coyne/ACI_MCP | Config allow/deny lists over CRUD | Supports X.509 signature auth |
| k3l0-dev/niwashi-mcp, minefuto/a4i, beletea, bavtan, realpaul3907, cpaggen, datacenter | Mixed: flags, confirm prompts or unstated | — |
| Nexus Dashboard: CiscoDevNet/cisco_nd_mcp_community (110 tools incl. delete), beye91/nexus-dashboard-mcp (read-only by default flag) | No / flag | — |

Two things we offer that none of them does: a frozen catalog that makes
writes impossible, RBAC-blind-spot labelling, and a mandatory time
window on huge classes.

## 8. Later

**Nexus Dashboard (Analyze / Insights)** for anomalies, flow telemetry and
PSIRT/bug advisories, which APIC alone does not have. The legacy NDI API
`/sedgeapi/v1/cisco-nir/api/...` still exists in ND 4.3.1 [P]. This is a
separate connector, worth building if the customer runs ND.

## Questions for the customer

1. Which APIC tooling do they use today, which questions does it answer, and is it read-only?
2. How many APIC fabrics are there? How many APICs per cluster? Is OOB reachable from BOW?
3. Is local user plus certificate auth acceptable, or must it be LDAP, RADIUS or TACACS?
4. Do they run Nexus Dashboard / Insights?
5. Are UCS FIs (UCSM or IMM) in the same incident path? That decides the order with `cisco_ucsm`.

## Sources

**Cisco documentation**
- [APIC REST API Configuration Guide 4.2(x)+](https://www.cisco.com/c/en/us/td/docs/dcn/aci/apic/all/apic-rest-api-configuration-guide/cisco-apic-rest-api-configuration-guide-42x-and-later/m_using_the_rest_api.html)
- Model jsonmeta: https://pubhub.devnetcloud.com/media/model-doc-latest/docs/doc/jsonmeta/
- [ACI lifecycle brief](https://www.cisco.com/c/en/us/solutions/collateral/data-center-virtualization/application-centric-infrastructure/softwarep-lifecycle-support-aci-pb.pdf)
- [ACI Simulator releases](https://www.cisco.com/c/en/us/support/cloud-systems-management/application-centric-infrastructure-simulator/multimedia-release-list.html)
- [DevNet ACI always-on](https://developer.cisco.com/sandbox/detail/aci-simulator-always-on)
- [Nexus Dashboard API](https://developer.cisco.com/docs/nexus-dashboard/latest/)
- [NDI 6.4.1 API changelog](https://developer.cisco.com/docs/nexus-dashboard-insights/6-4-1/api-changelog)
- [ACI / Nexus One](https://www.cisco.com/site/us/en/products/networking/cloud-networking/application-centric-infrastructure/)

**Code and SDKs**
- [Cobra](https://github.com/datacenter/cobra) and its docs: [session](https://cobra.readthedocs.io/en/stable/api-ref/session.html), [request](https://cobra.readthedocs.io/en/stable/api-ref/request.html), [install](https://cobra.readthedocs.io/en/stable/install.html)
- [Ansible cisco.aci](https://github.com/CiscoDevNet/ansible-aci) (`plugins/module_utils/aci.py`)
- [DataDog cisco_aci](https://github.com/DataDog/integrations-core/tree/master/cisco_aci)

**Community**
- [Signature auth with Postman](https://community.cisco.com/t5/data-center-documents/cisco-apic-signature-based-transactions-rest-api-using-postman/m-p/3388216)
- [Read access for a non-admin user](https://community.cisco.com/t5/application-centric-infrastructure/cisco-aci-read-access-to-rest-apis-using-non-admin-user/m-p/4034818)
- [Qualys: APIC privilege level](https://docs.qualys.com/en/vm/latest/pc_sigs/privilege_level_to_scan_cisco_apic.htm)

**MCP**
- Registry search: https://registry.modelcontextprotocol.io/v0/servers?search=aci
- Repositories listed in §7
