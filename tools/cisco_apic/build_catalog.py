#!/usr/bin/env python3
"""Build the frozen Cisco APIC catalog from Cisco's published model metadata.

The APIC object model is published per class as JSON ("jsonmeta") at
https://pubhub.devnetcloud.com/media/model-doc-latest/docs/doc/jsonmeta/<pkg>/<Class>.json
That metadata is the authority for class names, DN formats, read privileges,
property names and types, and which properties are `secure` (never exposed).

    python tools/cisco_apic/build_catalog.py --meta-dir /tmp/apic-jsonmeta --fetch

writes backend/app/data_sources/clients/cisco_apic/catalog.json. The metadata
itself is not vendored; only the derived, curated table contract is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

META_URL = "https://pubhub.devnetcloud.com/media/model-doc-latest/docs/doc/jsonmeta/{pkg}/{cls}.json"
OUT = Path(__file__).resolve().parents[2] / "backend/app/data_sources/clients/cisco_apic/catalog.json"

# Properties that are internal bookkeeping on every MO and carry no diagnostic value.
NOISE = {"childAction", "status", "rn", "uid", "userdom", "extMngdBy", "annotation", "lcOwn", "monPolDn"}
INT_BASES = {"Uint8", "Uint16", "Uint32", "Uint64", "Sint8", "Sint16", "Sint32", "Sint64", "UByte"}
FLOAT_BASES = {"Double", "Float"}

# name, class, description, default fields, options
# options: window (time field + required lookback), order (default order-by), node (False to forbid node scoping)
TABLES = [
    # ---------------------------------------------------------------- inventory & versions
    ("fabric_nodes", "fabricNode", "Every controller, spine and leaf registered in the fabric: role, model, serial, running version and fabric state (fabricSt active/inactive/disabled). Start here to map node ids to names.",
     ["dn", "id", "name", "role", "model", "serial", "version", "fabricSt", "adSt", "address", "lastStateModTs"], {"node": False}),
    ("systems", "topSystem", "Per-node system object: management addresses, uptime, last reboot time and reset reason, current clock (skew), cluster time difference and state.",
     ["dn", "id", "name", "role", "podId", "oobMgmtAddr", "address", "version", "state", "systemUpTime", "lastRebootTime", "lastResetReason", "currentTime", "clusterTimeDiff", "serial"], {}),
    ("controller_firmware", "firmwareCtrlrRunning", "Running APIC controller software per controller (version and image timestamp). Compare with switch_firmware for version mismatches.",
     ["dn", "version", "type", "ts"], {}),
    ("switch_firmware", "firmwareRunning", "Running switch software per spine/leaf: version, PE version, BIOS version and image file. Use it to find nodes on a different release than their peers.",
     ["dn", "version", "peVer", "biosVer", "type", "sysFile", "ts"], {}),
    ("controller_cluster", "infraWiNode", "APIC cluster membership as seen by each controller: per-member address, health (fully-fit / data-layer-partially-diverged / ...), operational and admin state, failover status.",
     ["dn", "id", "nodeName", "addr", "health", "operSt", "adminSt", "failoverStatus", "apicMode", "podId", "mutnTs"], {}),
    ("controller_cluster_size", "infraCont", "Configured APIC cluster size and whether the cluster considers itself fit, per controller.",
     ["dn", "size", "fbDmNm", "healthy", "uctDiff"], {}),
    # ---------------------------------------------------------------- hardware
    ("chassis", "eqptCh", "Switch chassis: model, serial, operational state and qualifier.",
     ["dn", "model", "ser", "operSt", "operStQual", "descr"], {}),
    ("power_supplies", "eqptPsu", "Power supplies per switch: operational state, drawn current/power, model and serial. A failed or absent PSU shows operSt fail/shut/absent.",
     ["dn", "id", "model", "ser", "operSt", "fanOpSt", "drawnCurr", "volt", "cap", "vSrc", "descr"], {}),
    ("fan_trays", "eqptFt", "Fan trays per switch: operational state and direction.",
     ["dn", "id", "model", "ser", "operSt", "fanName", "fanletFailString", "descr"], {}),
    ("fans", "eqptFan", "Individual fans: operational state, speed and direction.",
     ["dn", "id", "operSt", "speed", "maxSpeed", "dir", "descr"], {}),
    ("line_cards", "eqptLC", "Line cards (modular spines): model, serial, operational state and software version.",
     ["dn", "id", "model", "ser", "operSt", "pwrSt", "swVer", "hwVer", "upTs", "descr"], {}),
    # ---------------------------------------------------------------- health
    ("fabric_health", "fabricHealthTotal", "Current fabric-wide and per-pod health score (0-100) with previous value and highest fault severity.",
     ["dn", "cur", "prev", "maxSev", "updTs"], {"node": False}),
    ("fabric_health_history", "fabricOverallHealthHist5min", "Five-minute history of the overall fabric health score (min/avg/max per interval). Use it to see when health dropped.",
     ["dn", "index", "repIntvStart", "repIntvEnd", "healthMin", "healthAvg", "healthMax"], {"window": "repIntvStart", "order": "repIntvStart|desc", "node": False}),
    ("health_scores", "healthInst", "Health score of any object (node, interface, tenant, EPG ...): current and previous value, change, and highest severity. The object is the dn minus its trailing /health.",
     ["dn", "cur", "prev", "chng", "twScore", "maxSev", "updTs"], {}),
    # ---------------------------------------------------------------- faults, events, audit
    ("faults", "faultInst", "Active and recently cleared faults on physical and logical objects: code, severity, lifecycle (lc raised/soaking/raised-clearing/retaining), cause, description and timestamps. The primary RCA table; the affected object is the dn minus the trailing /fault-<code>.",
     ["dn", "code", "severity", "lc", "cause", "descr", "type", "domain", "subject", "rule", "ack", "occur", "created", "lastTransition", "highestSeverity", "origSeverity"], {"window": "lastTransition", "optional_window": True, "order": "lastTransition|desc"}),
    ("fault_delegates", "faultDelegate", "Faults delegated to the tenant or policy object a user configured (for example an L3Out or EPG), with the affected internal object in `affected`.",
     ["dn", "code", "severity", "lc", "cause", "descr", "affected", "changeSet", "type", "domain", "created", "lastTransition"], {"window": "lastTransition", "optional_window": True, "order": "lastTransition|desc", "node": False}),
    ("fault_summary", "faultSummary", "Fabric-wide fault counts per fault code: what is noisy right now.",
     ["dn", "code", "count", "severity", "cause", "descr", "domain", "type"], {"node": False}),
    ("fault_history", "faultRecord", "History of fault transitions (raised, cleared, retained) with the affected object. A time window is always applied (default last 24h).",
     ["dn", "code", "severity", "lc", "cause", "descr", "affected", "created", "type", "domain"], {"window": "created", "order": "created|desc", "node": False}),
    ("events", "eventRecord", "Event log: link up/down, endpoint moves, node joins and other state changes, with the affected object. A time window is always applied (default last 24h).",
     ["dn", "code", "cause", "descr", "affected", "severity", "created", "user", "trig"], {"window": "created", "order": "created|desc", "node": False}),
    ("audit_log", "aaaModLR", "Configuration audit log: who changed which object, when, how (ind creation/modification/deletion) and the change set. Use it to correlate changes with the start of an incident. A time window is always applied (default last 24h).",
     ["dn", "created", "user", "ind", "affected", "descr", "changeSet", "cause", "sessionId", "txId"], {"window": "created", "order": "created|desc", "node": False}),
    ("health_history", "healthRecord", "History of health score changes per object. A time window is always applied (default last 24h).",
     ["dn", "affected", "cur", "prev", "chng", "created"], {"window": "created", "order": "created|desc", "node": False}),
    # ---------------------------------------------------------------- interfaces, L1/L2
    ("interfaces", "l1PhysIf", "Physical interface configuration per switch port: admin state, speed, MTU, usage (fabric/epg/discovery/...) and description.",
     ["dn", "id", "adminSt", "speed", "mtu", "usage", "layer", "mode", "descr", "portT"], {}),
    ("interface_status", "ethpmPhysIf", "Physical interface operational state: operSt, operStQual (the reason a port is down, e.g. sfp-missing, link-not-connected, err-disabled), speed, last link change and operational VLANs. The interface is the dn minus the trailing /phys.",
     ["dn", "operSt", "operStQual", "operSpeed", "operDuplex", "lastLinkStChg", "operErrDisQual", "bundleIndex", "usage", "resetCtr"], {}),
    ("port_channels", "pcAggrIf", "Port-channels: admin/oper state, active member ports, last bundled/unbundled member and time.",
     ["dn", "id", "name", "adminSt", "pcMode", "activePorts", "maxActive", "lastBundleMbr", "lastBundleTime", "lastUnbundleMbr", "lastUnbundleTime", "lastSt", "lastStCause"], {}),
    ("port_channel_status", "ethpmAggrIf", "Port-channel operational state: operSt, operStQual (why it is down), active members, speed and last link change. The port-channel is the dn minus the trailing /aggrif.",
     ["dn", "operSt", "operStQual", "operSpeed", "activeMbrs", "numActivePorts", "numMbrUp", "lastLinkStChg"], {}),
    ("vpc_domains", "vpcDom", "vPC domain per leaf: peer status and qualifier, local/operational role, dual-active state, peer IP and keep-alive.",
     ["dn", "id", "peerSt", "peerStQual", "operRole", "oldRole", "dualActiveSt", "peerIp", "localPrio", "virtualIp", "summOperRole"], {}),
    ("vpc_interfaces", "vpcIf", "vPC interfaces: local and remote operational state, compatibility status/qualifier and suspended VLANs.",
     ["dn", "id", "localOperSt", "remoteOperSt", "compatSt", "compatQual", "compatQualStr", "suspVlans", "cfgdVlans"], {}),
    ("lldp_neighbors", "lldpAdjEp", "LLDP neighbors per switch port: neighbor system name, port, management IP and capability. Shows what is cabled where.",
     ["dn", "sysName", "portIdV", "portDesc", "mgmtIp", "chassisIdV", "sysDesc", "capability"], {}),
    ("cdp_neighbors", "cdpAdjEp", "CDP neighbors per switch port: device id, port, platform and management address.",
     ["dn", "devId", "portId", "platId", "sysName", "ver", "cap"], {}),
    ("fabric_links", "fabricLink", "Leaf-to-spine fabric links (node/slot/port at each end) and their link state.",
     ["dn", "n1", "s1", "p1", "n2", "s2", "p2", "linkState", "wiringIssues"], {"node": False}),
    # ---------------------------------------------------------------- underlay & routing
    ("isis_adjacencies", "isisAdjEp", "IS-IS fabric underlay adjacencies per switch: neighbor system id, state, last transition and number of transitions.",
     ["dn", "sysId", "operSt", "lastTrans", "numAdjTrans", "type", "holdExpTs"], {}),
    ("bgp_peers", "bgpPeerEntry", "BGP peer sessions (L3Out and fabric MP-BGP) per switch and VRF: state, previous state, reason, last flap time, connection drops and establishments.",
     ["dn", "addr", "operSt", "prevOperSt", "stReason", "lastFlapTs", "connDrop", "connEst", "rtrId", "type", "holdIntvl", "kaIntvl", "localIp"], {}),
    ("ospf_adjacencies", "ospfAdjEp", "OSPF adjacencies (L3Out) per switch and VRF: neighbor id, peer IP, area and state.",
     ["dn", "id", "peerIp", "area", "operSt", "prio", "bdr", "dr"], {}),
    # ---------------------------------------------------------------- tenants & policy
    ("tenants", "fvTenant", "Tenants.", ["dn", "name", "descr", "nameAlias"], {"node": False}),
    ("application_profiles", "fvAp", "Application profiles per tenant.", ["dn", "name", "descr", "prio"], {"node": False}),
    ("epgs", "fvAEPg", "Endpoint groups (EPGs): pcTag, config status and issues, flood/isolation and preferred-group membership.",
     ["dn", "name", "pcTag", "configSt", "configIssues", "isAttrBasedEPg", "pcEnfPref", "prefGrMemb", "floodOnEncap", "prio", "descr"], {"node": False}),
    ("epg_bridge_domains", "fvRsBd", "EPG to bridge-domain binding: the parent EPG is the dn minus /rsbd; tDn is the bridge domain.",
     ["dn", "tnFvBDName", "tDn", "state", "stateQual", "forceResolve"], {"node": False}),
    ("epg_static_paths", "fvRsPathAtt", "Static port/vPC bindings of EPGs (tDn is the path, encap the VLAN).",
     ["dn", "tDn", "encap", "mode", "instrImedcy", "state", "stateQual"], {"node": False}),
    ("epg_provided_contracts", "fvRsProv", "Contracts provided by EPGs/L3Out external EPGs: parent is the dn minus /rsprov-<contract>.",
     ["dn", "tnVzBrCPName", "tDn", "state", "stateQual", "prio"], {"node": False}),
    ("epg_consumed_contracts", "fvRsCons", "Contracts consumed by EPGs/L3Out external EPGs: parent is the dn minus /rscons-<contract>.",
     ["dn", "tnVzBrCPName", "tDn", "state", "stateQual", "prio"], {"node": False}),
    ("bridge_domains", "fvBD", "Bridge domains: unicast routing, ARP flooding, unknown unicast mode, IP learning limits and endpoint move detection.",
     ["dn", "name", "unicastRoute", "arpFlood", "unkMacUcastAct", "limitIpLearnToSubnets", "epMoveDetectMode", "ipLearning", "seg", "pcTag", "mac", "descr"], {"node": False}),
    ("vrfs", "fvCtx", "VRFs (contexts): policy enforcement preference and direction, scope and segment.",
     ["dn", "name", "pcEnfPref", "pcEnfDir", "scope", "seg", "pcTag", "descr"], {"node": False}),
    ("vrf_bindings", "fvRsCtx", "Bridge-domain to VRF binding: the parent BD is the dn minus /rsctx.",
     ["dn", "tnFvCtxName", "tDn", "state", "stateQual"], {"node": False}),
    ("subnets", "fvSubnet", "Subnets / gateways on bridge domains and EPGs: IP, scope (private/public/shared) and control flags.",
     ["dn", "ip", "scope", "ctrl", "preferred", "virtual", "descr"], {"node": False}),
    ("endpoints", "fvCEp", "Learned endpoints (client endpoints) per EPG: MAC, IP, encapsulation VLAN, the fabric path (leaf/port/vPC) where it is learned and its source. Answers 'where is IP/MAC x'.",
     ["dn", "mac", "ip", "encap", "fabricPathDn", "lcC", "vmmSrc", "hostingServer", "reportingControllerName", "bdDn", "vrfDn", "contName"], {"node": False}),
    ("endpoint_ips", "fvIp", "IP addresses learned on endpoints (an endpoint can have several): the parent endpoint is the dn minus /ip-[...]. ",
     ["dn", "addr", "fabricPathDn", "createTs", "vrfDn", "bdDn", "flags"], {"node": False}),
    ("l3outs", "l3extOut", "L3Outs (external routed networks) per tenant.", ["dn", "name", "enforceRtctrl", "targetDscp", "descr"], {"node": False}),
    ("contracts", "vzBrCP", "Contracts: scope, priority and target DSCP.", ["dn", "name", "scope", "prio", "targetDscp", "configIssues", "descr"], {"node": False}),
    ("contract_subjects", "vzSubj", "Contract subjects: reverse filter ports, consumer/provider match type.",
     ["dn", "name", "revFltPorts", "consMatchT", "provMatchT", "prio", "descr"], {"node": False}),
    ("subject_filters", "vzRsSubjFiltAtt", "Filters attached to contract subjects (action permit/deny): parent subject is the dn minus /rssubjFiltAtt-<filter>.",
     ["dn", "tnVzFilterName", "tDn", "action", "directives", "state", "stateQual"], {"node": False}),
    ("filters", "vzFilter", "Filters (groups of filter entries).", ["dn", "name", "descr", "fwdId", "revId", "txId"], {"node": False}),
    ("filter_entries", "vzEntry", "Filter entries: ether type, IP protocol, source/destination port ranges and stateful flag.",
     ["dn", "name", "etherT", "prot", "sFromPort", "sToPort", "dFromPort", "dToPort", "stateful", "applyToFrag", "tcpRules"], {"node": False}),
    # ---------------------------------------------------------------- statistics (current 5-minute interval)
    ("interface_errors", "eqptIngrErrPkts5min", "Ingress error counters per interface for the current 5-minute interval (CRC, FCS, buffer errors).",
     ["dn", "repIntvStart", "repIntvEnd", "crcLast", "crcCountCum", "crcCountRate", "discardLast", "discardCum", "anyErrorLast", "anyErrorCum"], {}),
    ("interface_drops", "eqptEgrDropPkts5min", "Egress drop counters per interface for the current 5-minute interval (buffer, error, AFD WRED).",
     ["dn", "repIntvStart", "repIntvEnd", "bufferLast", "bufferCum", "bufferRate", "errorLast", "errorCum", "errorRate", "afdWredLast", "afdWredCum"], {}),
    ("interface_traffic", "eqptIngrTotal5min", "Ingress traffic per interface for the current 5-minute interval (bytes, packets, rates and utilization).",
     ["dn", "repIntvStart", "repIntvEnd", "bytesRate", "bytesCum", "pktsRate", "pktsCum", "utilAvg", "utilMax"], {}),
    ("cpu_usage", "procSysCPU5min", "Switch/controller control-plane CPU for the current 5-minute interval (user/kernel/idle).",
     ["dn", "repIntvStart", "repIntvEnd", "userAvg", "userMax", "kernelAvg", "kernelMax", "idleAvg", "idleMin"], {}),
    ("memory_usage", "procSysMem5min", "Switch/controller memory for the current 5-minute interval (used/free).",
     ["dn", "repIntvStart", "repIntvEnd", "usedAvg", "usedMax", "freeAvg", "freeMin"], {}),
    # ---------------------------------------------------------------- virtualization
    ("vms", "compVm", "Virtual machines learned through VMM integration: name, state, OS, management IP.",
     ["dn", "name", "state", "os", "cfgdOs", "guid", "oid"], {"node": False}),
    ("hypervisors", "compHv", "Hypervisors learned through VMM integration: name, state, status, type.",
     ["dn", "name", "state", "type", "mgmtIp", "availOperSt", "guid", "oid"], {"node": False}),
]


def pkg_cls(class_name: str) -> tuple[str, str]:
    for i, ch in enumerate(class_name):
        if ch.isupper():
            return class_name[:i], class_name[i:]
    raise ValueError(class_name)


def load_meta(meta_dir: Path, class_name: str, fetch: bool) -> dict:
    pkg, cls = pkg_cls(class_name)
    path = meta_dir / f"{pkg}_{cls}.json"
    if not path.exists() or path.stat().st_size == 0:
        if not fetch:
            raise SystemExit(f"missing {path}; rerun with --fetch")
        with urllib.request.urlopen(META_URL.format(pkg=pkg, cls=cls), timeout=60) as r:
            path.write_bytes(r.read())
    data = json.loads(path.read_text())
    return data[f"{pkg}:{cls}"]


def field_type(prop: dict) -> str:
    base = (prop.get("baseType") or prop.get("modelType") or "").split(":")[-1]
    values = [v for v in prop.get("validValues", []) if v.get("localName") != "defaultValue"]
    if base == "Date":
        return "datetime"
    if base in FLOAT_BASES and not values:
        return "number"
    if base in INT_BASES and not values:
        return "integer"
    return "string"  # enums, bitmasks, yes/no booleans, DNs, addresses, encaps and text are strings on the wire


def short(text) -> str:
    if isinstance(text, list):
        text = " ".join(t for t in text if isinstance(t, str))
    text = " ".join(str(text or "").split())
    return text[:200]


def build(meta_dir: Path, fetch: bool) -> dict:
    tables = {}
    for name, cls, desc, defaults, opts in TABLES:
        meta = load_meta(meta_dir, cls, fetch)
        props = meta["properties"]
        fields = {}
        for prop, spec in sorted(props.items()):
            if prop in NOISE or spec.get("secure"):
                continue
            entry = {"type": field_type(spec), "label": spec.get("label", "").strip() or prop}
            comment = short(spec.get("comment"))
            if comment:
                entry["description"] = comment
            values = [v["localName"] for v in spec.get("validValues", []) if v.get("localName") not in (None, "defaultValue")]
            if values and len(values) <= 40 and field_type(spec) == "string":
                entry["values"] = values
            fields[prop] = entry
        missing = [f for f in defaults if f not in fields]
        if missing:
            raise SystemExit(f"{name}: default fields not in model: {missing}")
        dn_formats = meta.get("dnFormats", [])
        node_scoped = any(f.startswith("topology/pod-{id}/node-{id}/") for f in dn_formats)
        allow_node = opts.get("node", True) and node_scoped
        window = opts.get("window")
        if window and window not in fields:
            raise SystemExit(f"{name}: window field {window} missing")
        table = {
            "class": cls,
            "description": desc,
            "default_fields": defaults,
            "fields": fields,
            "node_scoped": allow_node,
            "read_access": sorted(meta.get("readAccess", [])),
            "admin_read_only": meta.get("readAccess", []) == ["admin"],
            "dn_example": next((f for f in dn_formats if f.startswith("topology/")), dn_formats[0] if dn_formats else ""),
            "order_by": opts.get("order") or f"{cls}.dn|asc",
            "time_field": window,
            "window_required": bool(window) and not opts.get("optional_window", False),
        }
        if "|" in table["order_by"] and "." not in table["order_by"].split("|")[0]:
            table["order_by"] = f"{cls}.{table['order_by']}"
        tables[name] = table
    contract = {
        "model": "Cisco APIC management information model (model-doc-latest jsonmeta)",
        "model_url": "https://pubhub.devnetcloud.com/media/model-doc-latest/docs/doc/jsonmeta/",
        "reference_release": "APIC 6.x",
        "max_page_size": 500,
        "max_filter_terms": 20,
    }
    body = json.dumps({"contract": contract, "tables": tables}, sort_keys=True)
    return {"contract": contract, "tables": tables, "sha256": hashlib.sha256(body.encode()).hexdigest()}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--meta-dir", type=Path, required=True, help="directory caching <pkg>_<Class>.json metadata files")
    p.add_argument("--fetch", action="store_true", help="download missing metadata from pubhub.devnetcloud.com")
    p.add_argument("--out", type=Path, default=OUT)
    a = p.parse_args()
    a.meta_dir.mkdir(parents=True, exist_ok=True)
    catalog = build(a.meta_dir, a.fetch)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(catalog, indent=1, sort_keys=True) + "\n")
    print(f"wrote {a.out} with {len(catalog['tables'])} tables", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
