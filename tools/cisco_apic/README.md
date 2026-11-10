# Cisco ACI (APIC) connector

Native, read-only connector for the Cisco APIC REST API (catalog generated
from Cisco's APIC 6.x model metadata). No SDK, MCP server or Cisco licence is
needed for local development: `simulated_api.py` in this folder is a loopback
APIC. A read-only pass on a real fabric is still required before GA (see
`SOURCES.md` for what is inferred).

## Use in BOW

Add **Cisco ACI (APIC)** under Connections → Infrastructure. Supply one
controller origin (`https://apic1.example.com`), optionally the other
controllers of the cluster (tried in order when one is unreachable), and one
of two authentication methods:

| Method | What to create on the APIC | Notes |
|---|---|---|
| **Certificate (default)** | A local user with the `read-all` role (domain `all`, read privilege). Attach an X.509 certificate to it (Admin → AAA → Users → *user* → User Certificates). Paste the user name, certificate name and the RSA private key (≥2048 bits, unencrypted PEM). | Every request is signed (`APIC-Request-Signature`); no session is opened and no login records are written. Local users only. |
| **Username / Password** | A local or remote user with `read-all`. For a remote (LDAP/RADIUS/TACACS+) user, set the connection's *Login domain* (sent as `apic:<domain>\\<user>`). | One session per operation: `aaaLogin` → reads → `aaaLogout` in `finally`. Per-user credentials are supported. |

**Faults, events, audit log and statistics need admin read access.** Cisco's
model grants read on `faultInst`, `faultRecord`, `eventRecord`, `aaaModLR` and
the `*5min` statistics classes to the `admin` privilege only. A `read-all`
account receives empty results there (not an error). *Test connection* warns
when no faults are visible, and those tables say so in their descriptions.

Kerberos is not offered: the APIC does not accept Kerberos tickets. An Active
Directory identity works through an LDAP login domain (password method).

## Read-only by construction

The client has one network primitive. It can build only:

- `GET /api/class/<class>.json` and `GET /api/node/class/topology/pod-<p>/node-<n>/<class>.json`
  for the classes in `backend/app/data_sources/clients/cisco_apic/catalog.json`,
  with only `query-target-filter`, `order-by`, `page` and `page-size`;
- `POST /api/aaaLogin.json` and `POST /api/aaaLogout.json` (password method only).

Writes (`POST /api/mo/...`), `DELETE`, subscriptions (`subscription=yes`),
`aaaRefresh`, `rsp-subtree`, `/api/mo` reads, file downloads and any class
outside the catalog are refused before a request is sent. Filters are built
from a JSON query: values cannot contain quotes, backslashes or control
characters, so a value cannot break out of the filter expression. Properties
flagged `secure` in Cisco's model are not in the catalog.

## Tables

59 tables, one APIC class each. `node` = supports `"node": <id>` scoping
(switch-resident class), `window`/`lookback` = time column (window always
applied, default 24h, or optional), `admin read` = model read access is
admin-only.

| Group | Table | Class | Notes |
|---|---|---|---|
| Inventory & versions | `fabric_nodes` | `fabricNode` |  |
|  | `systems` | `topSystem` | node |
|  | `controller_firmware` | `firmwareCtrlrRunning` | node |
|  | `switch_firmware` | `firmwareRunning` | node |
|  | `controller_cluster` | `infraWiNode` | node |
|  | `controller_cluster_size` | `infraCont` | node |
| Hardware | `chassis` | `eqptCh` | node |
|  | `power_supplies` | `eqptPsu` | node |
|  | `fan_trays` | `eqptFt` | node |
|  | `fans` | `eqptFan` | node |
|  | `line_cards` | `eqptLC` | node |
| Health | `fabric_health` | `fabricHealthTotal` |  |
|  | `fabric_health_history` | `fabricOverallHealthHist5min` | window repIntvStart, admin read |
|  | `health_scores` | `healthInst` | node |
|  | `health_history` | `healthRecord` | window created |
| Faults, events, audit | `faults` | `faultInst` | node, lookback lastTransition, admin read |
|  | `fault_delegates` | `faultDelegate` | lookback lastTransition, admin read |
|  | `fault_summary` | `faultSummary` | admin read |
|  | `fault_history` | `faultRecord` | window created, admin read |
|  | `events` | `eventRecord` | window created, admin read |
|  | `audit_log` | `aaaModLR` | window created, admin read |
| Interfaces & L2 | `interfaces` | `l1PhysIf` | node |
|  | `interface_status` | `ethpmPhysIf` | node |
|  | `port_channels` | `pcAggrIf` | node |
|  | `port_channel_status` | `ethpmAggrIf` | node |
|  | `vpc_domains` | `vpcDom` | node |
|  | `vpc_interfaces` | `vpcIf` | node |
|  | `lldp_neighbors` | `lldpAdjEp` | node |
|  | `cdp_neighbors` | `cdpAdjEp` | node |
|  | `fabric_links` | `fabricLink` |  |
| Underlay & routing | `isis_adjacencies` | `isisAdjEp` | node |
|  | `bgp_peers` | `bgpPeerEntry` | node |
|  | `ospf_adjacencies` | `ospfAdjEp` | node |
| Tenants & policy | `tenants` | `fvTenant` |  |
|  | `application_profiles` | `fvAp` |  |
|  | `epgs` | `fvAEPg` |  |
|  | `epg_bridge_domains` | `fvRsBd` |  |
|  | `epg_static_paths` | `fvRsPathAtt` |  |
|  | `epg_provided_contracts` | `fvRsProv` |  |
|  | `epg_consumed_contracts` | `fvRsCons` |  |
|  | `bridge_domains` | `fvBD` |  |
|  | `vrfs` | `fvCtx` |  |
|  | `vrf_bindings` | `fvRsCtx` |  |
|  | `subnets` | `fvSubnet` |  |
|  | `endpoints` | `fvCEp` |  |
|  | `endpoint_ips` | `fvIp` |  |
|  | `l3outs` | `l3extOut` |  |
|  | `contracts` | `vzBrCP` |  |
|  | `contract_subjects` | `vzSubj` |  |
|  | `subject_filters` | `vzRsSubjFiltAtt` |  |
|  | `filters` | `vzFilter` |  |
|  | `filter_entries` | `vzEntry` |  |
| Statistics (current 5-minute interval) | `interface_errors` | `eqptIngrErrPkts5min` | node, admin read |
|  | `interface_drops` | `eqptEgrDropPkts5min` | node, admin read |
|  | `interface_traffic` | `eqptIngrTotal5min` | node, admin read |
|  | `cpu_usage` | `procSysCPU5min` | node |
|  | `memory_usage` | `procSysMem5min` | node |
| Virtualization | `vms` | `compVm` |  |
|  | `hypervisors` | `compHv` |  |

Every row also carries `pod`, `node` and `parent_dn` (the DN minus its last
relative name, bracket-aware: the object a fault, health score, interface
status or binding belongs to) and `_bow_*` provenance (controller, class,
query id, retrieval time, applied window, catalog hash). Times are UTC.

## Query shape

```json
{"table": "faults", "filter": {"severity": {"op": "in", "value": ["critical", "major"]}}, "lookback": "24h"}
{"table": "interface_status", "node": [303, 304], "filter": {"operSt": "down"}}
{"table": "audit_log", "lookback": "2h"}
{"table": "endpoints", "filter": {"ip": "10.10.30.21"}}
{"table": "filter_entries", "filter": {"dn": {"op": "contains", "value": "tn-retail/"}}}
```

Operators: `eq` (scalar), `ne`, `gt`, `ge`, `lt`, `le`, `in` (list, max 20),
`contains` (exact substring; sent to the APIC as a `wcard` superset pattern
and re-checked on the returned rows). At most 20 filter expressions (the APIC
limit). `limit` is a complete-result ceiling: if the APIC's `totalCount`
exceeds it the query fails with `ResultLimitExceeded` before paging; results
are never truncated. Pages are 500 objects (the APIC caps a page at 2000).

## Simulator

```bash
cd backend
APIC_USERNAME=bow-ro APIC_PASSWORD=local-only \
APIC_LIMITED_USERNAME=bow-readall APIC_LIMITED_PASSWORD=local-only \
APIC_CERT_USERNAME=bow-cert APIC_CERT_NAME=bow-signing APIC_CERT_PEM=/path/to/public-or-cert.pem \
  uv run python ../tools/cisco_apic/simulated_api.py --port 18094
```

Connect with `http://127.0.0.1:18094` and *Allow HTTP for a simulator*. The
fabric (one pod, 3 controllers, 2 spines, 4 leaves in two vPC pairs, tenants
`retail` and `payments`) is built relative to start-up time and contains a
planted incident:

| When | What | Where to see it |
|---|---|---|
| −52 min | Fabric uplink eth1/50 on leaf 303 down, `sfp-missing` (spine-2 side `link-failure`); fabric health 98 → 86 | `faults`, `events`, `interface_status`, `fabric_health_history` |
| −38 min | `netops-jlee` changed filter entry `pg-5432` of filter `postgres` (contract `app-to-db`) from postgresql to port 5433 | `audit_log`, `filter_entries` |
| −35 min | EPG `db` health 100 → 72 | `health_scores`, `health_history`, `faults` |
| −34 → −21 min | L3Out `corp-wan` BGP peer 10.200.0.5 on leaf 304 flapping, now idle | `bgp_peers`, `events`, `fault_delegates` |
| −12 days | Leaf 302 downgraded to n9000-16.1(4h) | `switch_firmware`, `faults` (F1371), `events` with `lookback` |
| −6 days | Spine 201 PSU 2 failed | `power_supplies`, `faults` |

Plus three days of background endpoint moves, port events, audit records and
fault history, CRC errors on one server port, and a `read-all` account that
sees empty fault/event/audit/statistics tables. The simulator records every
write attempt and subscription; the tests assert both stay empty.

## Regenerating the catalog

```bash
python3 tools/cisco_apic/build_catalog.py --meta-dir /tmp/apic-jsonmeta --fetch
```

The curated table list lives in `build_catalog.py`; class properties, types,
enum values, read access and DN formats come from Cisco's metadata.
