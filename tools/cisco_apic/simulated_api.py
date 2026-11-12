#!/usr/bin/env python3
"""Synthetic Cisco APIC REST API for local verification. NOT an APIC.

Models the wire contract of an APIC 6.x controller cluster for the read path:

* ``POST /api/aaaLogin.json`` ``{"aaaUser":{"attributes":{"name","pwd"}}}`` returns
  ``imdata[0].aaaLogin.attributes.{token, refreshTimeoutSeconds, version}`` and the
  ``APIC-cookie`` cookie; remote users log in as ``apic:<domain>\\\\<user>``.
  ``POST /api/aaaLogout.json`` ends the session; ``GET /api/aaaRefresh.json`` rotates it.
* Signature ("certificate") authentication: the ``APIC-Certificate-*`` and
  ``APIC-Request-Signature`` cookies, RSA PKCS#1 v1.5 / SHA-256 over
  ``METHOD + raw request URI + body`` with no separators. No session is created.
* Class queries ``GET /api/class/<cls>.json``, the ``/api/node/class/...`` alias and
  node-scoped ``/api/node/class/topology/pod-<p>/node-<n>/<cls>.json``; ``/api/mo/<dn>.json``.
* ``query-target-filter`` (eq ne gt ge lt le wcard bw and or not), ``order-by``,
  zero-based ``page`` / ``page-size`` (capped at 2000), ``totalCount`` as a string,
  ``rsp-subtree-include=count`` and the APIC error envelope
  ``{"totalCount":"1","imdata":[{"error":{"attributes":{"code","text"}}}]}``.
* An unpaged class query over more than 5000 objects fails with HTTP 503
  ("result dataset is too big"), as a busy controller does.
* Read-only accounts: every POST other than login/logout, and every DELETE, is refused
  with HTTP 403 and recorded in ``writes``; ``subscription=yes`` is recorded in
  ``subscriptions``. Tests assert both stay empty.
* RBAC blind spot: the restricted account (``APIC_LIMITED_USERNAME``) holds only the
  read-all role, so classes whose read access is admin-only return empty ``imdata``
  instead of an error, as on a real controller.

Timestamps carry the controller's local offset (+02:00) to exercise normalisation.

The fabric is deterministic and relative to start-up time: one pod, three
controllers, two spines and four leaves (two vPC pairs), tenants with an
application whose app-to-db traffic broke after a contract filter change ~38
minutes ago, a fabric uplink down on a leaf (missing optic), a flapping L3Out BGP
peer, one leaf on an older switch release, an old failed power supply, and three
days of background events, faults and audit records.

Credentials come from APIC_USERNAME / APIC_PASSWORD (full read), optionally
APIC_LIMITED_USERNAME / APIC_LIMITED_PASSWORD (read-all only) and, for signature
auth, APIC_CERT_USERNAME / APIC_CERT_NAME / APIC_CERT_PEM (path to the user's X.509
certificate or public key in PEM). Binds loopback by default.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import secrets
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

APIC_VERSION = "6.2(1g)"
SWITCH_VERSION = "n9000-16.2(1g)"
OLD_SWITCH_VERSION = "n9000-16.1(4h)"
LOCAL_TZ = timezone(timedelta(hours=2))
MAX_PAGE_SIZE = 2000
UNPAGED_LIMIT = 5000
SESSION_IDLE = 600

# Classes whose model read access is admin-only (from Cisco's model metadata).
ADMIN_READ = {
    "faultInst", "faultDelegate", "faultSummary", "faultRecord", "eventRecord", "aaaModLR",
    "fabricOverallHealthHist5min", "eqptIngrErrPkts5min", "eqptEgrDropPkts5min", "eqptIngrTotal5min",
}


def ts(dt: datetime) -> str:
    local = dt.astimezone(LOCAL_TZ)
    return local.strftime("%Y-%m-%dT%H:%M:%S.") + f"{local.microsecond // 1000:03d}" + local.strftime("%z")[:3] + ":" + local.strftime("%z")[3:]


def build_fabric(now: datetime) -> dict[str, list[dict]]:
    rng = random.Random(20261110)
    mo: dict[str, list[dict]] = {}

    def add(cls, **attrs):
        attrs = {k: ("" if v is None else str(v)) for k, v in attrs.items()}
        mo.setdefault(cls, []).append(attrs)
        return attrs

    ago = lambda **kw: now - timedelta(**kw)  # noqa: E731

    controllers = [(1, "dc1-apic-1", "10.0.0.1", "FCH2301V0A1"), (2, "dc1-apic-2", "10.0.0.2", "FCH2301V0A2"), (3, "dc1-apic-3", "10.0.0.3", "FCH2301V0A3")]
    spines = [(201, "dc1-spine-1", "N9K-C9364C", "FDO24210SP1"), (202, "dc1-spine-2", "N9K-C9364C", "FDO24210SP2")]
    leaves = [
        (301, "dc1-leaf-1", "N9K-C93180YC-FX", "FDO24310LF1"),
        (302, "dc1-leaf-2", "N9K-C93180YC-FX", "FDO24310LF2"),
        (303, "dc1-leaf-3", "N9K-C93360YC-FX2", "FDO24310LF3"),
        (304, "dc1-leaf-4", "N9K-C93360YC-FX2", "FDO24310LF4"),
    ]
    switches = spines + leaves
    node = lambda n: f"topology/pod-1/node-{n}"  # noqa: E731

    # ------------------------------------------------------------------ nodes, systems, firmware
    for nid, name, _addr, serial in controllers:
        add("fabricNode", dn=node(nid), id=nid, name=name, role="controller", model="APIC-SERVER-M3", serial=serial,
            version=APIC_VERSION, fabricSt="unknown", adSt="on", address=f"10.0.0.{nid}", lastStateModTs=ts(ago(days=41)), vendor="Cisco Systems, Inc", nodeType="unspecified")
        add("topSystem", dn=f"{node(nid)}/sys", id=nid, name=name, role="controller", podId=1, oobMgmtAddr=f"192.168.10.{nid}",
            address=f"10.0.0.{nid}", version=APIC_VERSION, state="in-service", systemUpTime="41:03:12:44.000",
            lastRebootTime=ts(ago(days=41, hours=3)), lastResetReason="unknown", currentTime=ts(now), clusterTimeDiff=0, serial=serial, fabricDomain="dc1-fabric")
        add("firmwareCtrlrRunning", dn=f"{node(nid)}/sys/ctrlrfwstatuscont/ctrlrrunning", version=APIC_VERSION, type="controller", ts=ts(ago(days=41, hours=4)))
        for peer, pname, paddr, _ in controllers:
            add("infraWiNode", dn=f"{node(nid)}/av/node-{peer}", id=peer, nodeName=pname, addr=paddr, health="fully-fit",
                operSt="available", adminSt="in-service", failoverStatus="idle", apicMode="active", podId=1, mutnTs=ts(ago(days=41)))
        add("infraCont", dn=f"{node(nid)}/av", size=3, fbDmNm="dc1-fabric", healthy="yes", uctDiff=0)
    for nid, name, model, serial in switches:
        role = "spine" if nid < 300 else "leaf"
        ver = OLD_SWITCH_VERSION if nid == 302 else SWITCH_VERSION
        short = ver.split("-", 1)[1]
        add("fabricNode", dn=node(nid), id=nid, name=name, role=role, model=model, serial=serial, version=short,
            fabricSt="active", adSt="on", address=f"10.0.{nid // 100}.{nid % 100}", lastStateModTs=ts(ago(days=40)), vendor="Cisco Systems, Inc", nodeType="unspecified")
        uptime_days = 12 if nid == 302 else 40
        add("topSystem", dn=f"{node(nid)}/sys", id=nid, name=name, role=role, podId=1, oobMgmtAddr=f"192.168.10.{nid % 100 + (20 if role == 'spine' else 40)}",
            address=f"10.0.{nid // 100}.{nid % 100}", version=short, state="in-service", systemUpTime=f"{uptime_days}:06:31:10.000",
            lastRebootTime=ts(ago(days=uptime_days, hours=6)), lastResetReason="reset-requested-by-cli-command-reload" if nid == 302 else "unknown",
            currentTime=ts(now - timedelta(seconds=2 if nid == 303 else 0)), clusterTimeDiff=0, serial=serial, fabricDomain="dc1-fabric")
        add("firmwareRunning", dn=f"{node(nid)}/sys/fwstatuscont/running", version=ver, peVer=short.replace("16.", "6.", 1),
            biosVer="07.69" if role == "leaf" else "05.47", type="switch", sysFile=f"bootflash:///aci-{ver.replace('-', '-dk9.', 1)}.bin".replace("(", ".").replace(")", ""),
            ts=ts(ago(days=uptime_days, hours=7)))
        add("eqptCh", dn=f"{node(nid)}/sys/ch", model=model, ser=serial, operSt="online", operStQual="", descr=f"Nexus {model}")
        for psu in (1, 2):
            failed = nid == 201 and psu == 2
            add("eqptPsu", dn=f"{node(nid)}/sys/ch/psuslot-{psu}/psu", id=psu, model="NXA-PAC-1100W-PE2", ser=f"{serial}P{psu}",
                operSt="fail" if failed else "ok", fanOpSt="ok", drawnCurr=0 if failed else 2, volt=0 if failed else 12, cap=1100, vSrc="ac", descr="1100W AC PSU")
        for ft in range(1, 5 if role == "leaf" else 7):
            add("eqptFt", dn=f"{node(nid)}/sys/ch/ftslot-{ft}/ft", id=ft, model="NXA-FAN-30CFM-B", ser=f"{serial}F{ft}", operSt="online", fanName=f"Fan{ft}", fanletFailString="", descr="Fan tray")
            add("eqptFan", dn=f"{node(nid)}/sys/ch/ftslot-{ft}/ft/fan-1", id=1, operSt="online", speed=rng.randint(38, 46), maxSpeed=100, dir="front-to-back", descr="Fan")
        if role == "spine":
            add("eqptLC", dn=f"{node(nid)}/sys/ch/lcslot-1/lc", id=1, model=model, ser=f"{serial}L1", operSt="online", pwrSt="on", swVer=short, hwVer="1.1", upTs=ts(ago(days=40)), descr="64p 40/100G")
        add("procSysCPU5min", dn=f"{node(nid)}/sys/procsys/CDprocSysCPU5min", repIntvStart=ts(ago(minutes=4)), repIntvEnd=ts(now),
            userAvg=round(rng.uniform(4, 9), 2), userMax=round(rng.uniform(9, 14), 2), kernelAvg=round(rng.uniform(2, 4), 2), kernelMax=6.1,
            idleAvg=round(rng.uniform(86, 92), 2), idleMin=round(rng.uniform(80, 86), 2))
        add("procSysMem5min", dn=f"{node(nid)}/sys/procsys/CDprocSysMem5min", repIntvStart=ts(ago(minutes=4)), repIntvEnd=ts(now),
            usedAvg=rng.randint(9_000_000, 11_000_000), usedMax=11_500_000, freeAvg=rng.randint(13_000_000, 15_000_000), freeMin=12_800_000)

    # ------------------------------------------------------------------ interfaces, links, adjacencies
    spine_tep = {201: "10.0.72.64", 202: "10.0.72.65"}
    for nid, _name, *_ in leaves:
        leaf_index = nid - 300
        for port in range(1, 49):
            iface = f"eth1/{port}"
            base = f"{node(nid)}/sys/phys-[{iface}]"
            used = port <= 12
            add("l1PhysIf", dn=base, id=iface, adminSt="up", speed="25G" if used else "inherit", mtu=9216, usage="epg" if used else "discovery",
                layer="Layer2", mode="trunk", descr=f"srv-{leaf_index}-{port:02d}" if used else "", portT="leaf")
            add("ethpmPhysIf", dn=f"{base}/phys", operSt="up" if used else "down", operStQual="none" if used else "link-not-connected",
                operSpeed="25G" if used else "unknown", operDuplex="full" if used else "auto", lastLinkStChg=ts(ago(days=rng.randint(3, 30), minutes=rng.randint(0, 600))),
                operErrDisQual="up", bundleIndex="unspecified", usage="epg" if used else "discovery", resetCtr=rng.randint(0, 3))
            if used:
                add("eqptIngrTotal5min", dn=f"{base}/CDeqptIngrTotal5min", repIntvStart=ts(ago(minutes=4)), repIntvEnd=ts(now),
                    bytesRate=round(rng.uniform(1e6, 9e7), 1), bytesCum=rng.randint(10**11, 10**12), pktsRate=round(rng.uniform(1e3, 9e4), 1),
                    pktsCum=rng.randint(10**8, 10**9), utilAvg=round(rng.uniform(1, 30), 1), utilMax=round(rng.uniform(30, 60), 1))
                crc = 1840 if (nid == 304 and port == 7) else 0
                add("eqptIngrErrPkts5min", dn=f"{base}/CDeqptIngrErrPkts5min", repIntvStart=ts(ago(minutes=4)), repIntvEnd=ts(now),
                    crcLast=crc, crcCountCum=crc * 30, crcCountRate=round(crc / 300, 2), discardLast=0, discardCum=rng.randint(0, 20), anyErrorLast=crc, anyErrorCum=crc * 30)
                add("eqptEgrDropPkts5min", dn=f"{base}/CDeqptEgrDropPkts5min", repIntvStart=ts(ago(minutes=4)), repIntvEnd=ts(now),
                    bufferLast=0, bufferCum=rng.randint(0, 50), bufferRate=0, errorLast=0, errorCum=0, errorRate=0, afdWredLast=0, afdWredCum=0)
            if port in (1, 2):
                add("lldpAdjEp", dn=f"{base.replace('/phys-[', '/lldp/inst/if-[')}/adj-1", sysName=f"esx-{leaf_index}{port:02d}.dc1.example",
                    portIdV=f"vmnic{port - 1}", portDesc="VMware vSwitch uplink", mgmtIp=f"192.168.50.{leaf_index * 10 + port}",
                    chassisIdV=f"00:50:56:{leaf_index:02x}:{port:02x}:01", sysDesc="VMware ESXi 8.0.3", capability="bridge")
        for uplink, spine in ((49, 201), (50, 202)):
            iface = f"eth1/{uplink}"
            base = f"{node(nid)}/sys/phys-[{iface}]"
            down = nid == 303 and uplink == 50
            add("l1PhysIf", dn=base, id=iface, adminSt="up", speed="100G", mtu=9366, usage="fabric", layer="Layer3", mode="trunk", descr="", portT="fab")
            add("ethpmPhysIf", dn=f"{base}/phys", operSt="down" if down else "up", operStQual="sfp-missing" if down else "none",
                operSpeed="unknown" if down else "100G", operDuplex="auto" if down else "full",
                lastLinkStChg=ts(ago(minutes=52) if down else ago(days=40)), operErrDisQual="up", bundleIndex="unspecified", usage="fabric", resetCtr=1 if down else 0)
            spine_port = leaf_index
            add("fabricLink", dn=f"topology/pod-1/lnkcnt-{spine}/lnk-{nid}-1-{uplink}-to-{spine}-1-{spine_port}" if not down else f"topology/pod-1/lnkcnt-{spine}/lnk-{nid}-1-{uplink}-to-{spine}-1-{spine_port}-stale",
                n1=nid, s1=1, p1=uplink, n2=spine, s2=1, p2=spine_port, linkState="invalid" if down else "ok", wiringIssues="")
            if not down:
                add("isisAdjEp", dn=f"{node(nid)}/sys/isis/inst-default/dom-overlay-1/if-[{iface}.{uplink - 48}]/adj-p2p-adj-{spine_tep[spine]}",
                    sysId=f"0000.{spine:04d}.0001", operSt="up", lastTrans=ts(ago(days=40)), numAdjTrans=1, type="p2p", holdExpTs=ts(now + timedelta(seconds=30)))
                add("lldpAdjEp", dn=f"{node(nid)}/sys/lldp/inst/if-[{iface}]/adj-1", sysName=f"dc1-spine-{spine - 200}", portIdV=f"Eth1/{spine_port}",
                    portDesc="topology/pod-1/paths-" + str(spine), mgmtIp=f"192.168.10.{spine % 100 + 20}", chassisIdV=f"e8:eb:34:{spine % 100:02x}:00:01", sysDesc="Cisco Nexus Operating System (NX-OS) Software", capability="bridge,router")
            add("eqptIngrTotal5min", dn=f"{base}/CDeqptIngrTotal5min", repIntvStart=ts(ago(minutes=4)), repIntvEnd=ts(now),
                bytesRate=0 if down else round(rng.uniform(5e8, 2e9), 1), bytesCum=rng.randint(10**12, 10**13), pktsRate=0 if down else 4.2e5, pktsCum=rng.randint(10**9, 10**10),
                utilAvg=0 if down else round(rng.uniform(5, 18), 1), utilMax=0 if down else 24.0)
    for spine, *_ in spines:
        for leaf, *_ in leaves:
            iface = f"eth1/{leaf - 300}"
            down = spine == 202 and leaf == 303
            add("ethpmPhysIf", dn=f"{node(spine)}/sys/phys-[{iface}]/phys", operSt="down" if down else "up", operStQual="link-failure" if down else "none",
                operSpeed="unknown" if down else "100G", operDuplex="full", lastLinkStChg=ts(ago(minutes=52) if down else ago(days=40)), operErrDisQual="up", bundleIndex="unspecified", usage="fabric", resetCtr=0)
            add("l1PhysIf", dn=f"{node(spine)}/sys/phys-[{iface}]", id=iface, adminSt="up", speed="100G", mtu=9366, usage="fabric", layer="Layer3", mode="trunk", descr="", portT="fab")

    # vPC pairs 301/302 (domain 10) and 303/304 (domain 20)
    for pair, dom in (((301, 302), 10), ((303, 304), 20)):
        for i, nid in enumerate(pair):
            add("vpcDom", dn=f"{node(nid)}/sys/vpc/inst/dom-{dom}", id=dom, peerSt="up", peerStQual="success", operRole="primary" if i == 0 else "secondary",
                oldRole="none-established", dualActiveSt="false", peerIp=f"10.0.3.{pair[1 - i] % 100}", localPrio=32667, virtualIp=f"10.0.96.{dom}", summOperRole="cfg-master-oper-master" if i == 0 else "cfg-slave-oper-slave")
            for vpc_id in (1, 2):
                add("vpcIf", dn=f"{node(nid)}/sys/vpc/inst/dom-{dom}/if-{vpc_id}", id=vpc_id, localOperSt="up", remoteOperSt="up", compatSt="pass",
                    compatQual="all-params-passed", compatQualStr="Compatibility check passed", suspVlans="", cfgdVlans="1100-1130")
            for pc in (1, 2):
                add("pcAggrIf", dn=f"{node(nid)}/sys/aggr-[po{pc}]", id=f"po{pc}", name=f"{'db' if pc == 1 else 'app'}-vpc", adminSt="up", pcMode="active",
                    activePorts=2, maxActive=16, lastBundleMbr=f"eth1/{10 + pc}", lastBundleTime=ts(ago(days=30)), lastUnbundleMbr="", lastUnbundleTime="1970-01-01T00:00:00.000+00:00",
                    lastSt="up", lastStCause="none")
                add("ethpmAggrIf", dn=f"{node(nid)}/sys/aggr-[po{pc}]/aggrif", operSt="up", operStQual="none", operSpeed="50G",
                    activeMbrs=f"eth1/{10 + pc},eth1/{12 + pc}", numActivePorts=2, numMbrUp=2, lastLinkStChg=ts(ago(days=30)))

    # L3Out BGP peers on the border leaves 303/304
    for nid, peer, flapping in ((303, "10.200.0.1", False), (304, "10.200.0.5", True)):
        add("bgpPeerEntry", dn=f"{node(nid)}/sys/bgp/inst/dom-retail:retail-vrf/peer-[{peer}/32]/ent-[{peer}]", addr=peer,
            operSt="idle" if flapping else "established", prevOperSt="established" if flapping else "openconfirm",
            stReason="none", lastFlapTs=ts(ago(minutes=21) if flapping else ago(days=40)), connDrop=14 if flapping else 0,
            connEst=15 if flapping else 1, rtrId=peer.replace("10.200.0", "10.255.255"), type="ebgp", holdIntvl=180, kaIntvl=60, localIp=f"10.200.0.{2 if nid == 303 else 6}",
            updateElapsedTs="1970-01-01T00:00:00.000+00:00" if flapping else ts(ago(minutes=1)))
        add("ospfAdjEp", dn=f"{node(nid)}/sys/ospf/inst-default/dom-mgmt:oob/if-[eth1/47]/adj-10.250.0.{nid % 100}", id=f"10.250.0.{nid % 100}",
            peerIp=f"10.250.1.{nid % 100}", area="0.0.0.0", operSt="full", prio=1, bdr="0.0.0.0", dr="0.0.0.0")
        add("cdpAdjEp", dn=f"{node(nid)}/sys/cdp/inst/if-[eth1/48]/adj-1", devId=f"dc1-wan-rtr-{nid % 100 - 2}", portId="TenGigabitEthernet0/0/1",
            platId="cisco ISR4451-X/K9", sysName=f"dc1-wan-rtr-{nid % 100 - 2}", ver="Cisco IOS XE Software, Version 17.12.04", cap="router,switch,igmp-filter")
    for spine, *_ in spines:
        for nid, *_ in leaves:
            add("bgpPeerEntry", dn=f"{node(spine)}/sys/bgp/inst/dom-overlay-1/peer-[10.0.3.{nid % 100}]/ent-[10.0.3.{nid % 100}]",
                addr=f"10.0.3.{nid % 100}", operSt="established", prevOperSt="openconfirm", stReason="none", lastFlapTs=ts(ago(days=40)),
                connDrop=0, connEst=1, rtrId=f"10.0.3.{nid % 100}", type="ibgp", holdIntvl=180, kaIntvl=60, localIp=spine_tep[spine])

    # ------------------------------------------------------------------ tenants and policy
    for tn in ("common", "infra", "mgmt", "retail", "payments"):
        add("fvTenant", dn=f"uni/tn-{tn}", name=tn, descr="", nameAlias="")
    add("fvCtx", dn="uni/tn-retail/ctx-retail-vrf", name="retail-vrf", pcEnfPref="enforced", pcEnfDir="ingress", scope=2392064, seg=2392064, pcTag=16386, descr="")
    add("fvCtx", dn="uni/tn-payments/ctx-pay-vrf", name="pay-vrf", pcEnfPref="enforced", pcEnfDir="ingress", scope=2588672, seg=2588672, pcTag=16387, descr="")
    add("fvAp", dn="uni/tn-retail/ap-checkout", name="checkout", descr="Online checkout", prio="unspecified")
    add("fvAp", dn="uni/tn-payments/ap-gateway", name="gateway", descr="Card gateway", prio="unspecified")
    epgs = [("retail", "checkout", "web", 49153, "1101"), ("retail", "checkout", "app", 49154, "1102"), ("retail", "checkout", "db", 49155, "1103"), ("payments", "gateway", "api", 49160, "1120")]
    for tn, ap, epg, tag, vlan in epgs:
        edn = f"uni/tn-{tn}/ap-{ap}/epg-{epg}"
        add("fvAEPg", dn=edn, name=epg, pcTag=tag, configSt="applied", configIssues="", isAttrBasedEPg="no", pcEnfPref="unenforced", prefGrMemb="exclude", floodOnEncap="disabled", prio="unspecified", descr="")
        bd = f"{epg}-bd"
        add("fvRsBd", dn=f"{edn}/rsbd", tnFvBDName=bd, tDn=f"uni/tn-{tn}/BD-{bd}", state="formed", stateQual="none", forceResolve="yes")
        add("fvBD", dn=f"uni/tn-{tn}/BD-{bd}", name=bd, unicastRoute="yes", arpFlood="no", unkMacUcastAct="proxy", limitIpLearnToSubnets="yes",
            epMoveDetectMode="garp", ipLearning="yes", seg=15925000 + tag, pcTag=32770 + tag % 100, mac="00:22:BD:F8:19:FF", descr="")
        vrf = "retail-vrf" if tn == "retail" else "pay-vrf"
        add("fvRsCtx", dn=f"uni/tn-{tn}/BD-{bd}/rsctx", tnFvCtxName=vrf, tDn=f"uni/tn-{tn}/ctx-{vrf}", state="formed", stateQual="none")
        octet = {"web": 10, "app": 20, "db": 30, "api": 40}[epg]
        add("fvSubnet", dn=f"uni/tn-{tn}/BD-{bd}/subnet-[10.10.{octet}.1/24]", ip=f"10.10.{octet}.1/24", scope="private", ctrl="nd", preferred="no", virtual="no", descr="")
        for pair in ((301, 302), (303, 304)):
            add("fvRsPathAtt", dn=f"{edn}/rspathAtt-[topology/pod-1/protpaths-{pair[0]}-{pair[1]}/pathep-[{epg}-vpc]]",
                tDn=f"topology/pod-1/protpaths-{pair[0]}-{pair[1]}/pathep-[{epg}-vpc]", encap=f"vlan-{vlan}", mode="regular", instrImedcy="immediate", state="formed", stateQual="none")
        hosts = 8 if epg != "db" else 3
        for h in range(1, hosts + 1):
            ip = f"10.10.{octet}.{20 + h}"
            mac = f"00:50:56:{octet:02X}:{h:02X}:{tag % 256:02X}"
            pair = (303, 304) if epg in ("db", "api") else (301, 302)
            path = f"topology/pod-1/protpaths-{pair[0]}-{pair[1]}/pathep-[{epg}-vpc]"
            add("fvCEp", dn=f"{edn}/cep-{mac}", mac=mac, ip=ip, encap=f"vlan-{vlan}", fabricPathDn=path, lcC="learned,vmm", vmmSrc="", hostingServer=f"esx-{h}.dc1.example",
                reportingControllerName="vcenter-dc1", bdDn=f"uni/tn-{tn}/BD-{bd}", vrfDn=f"uni/tn-{tn}/ctx-{vrf}", contName="vcenter-dc1", name=mac)
            add("fvIp", dn=f"{edn}/cep-{mac}/ip-[{ip}]", addr=ip, fabricPathDn=path, createTs=ts(ago(days=rng.randint(2, 20))), vrfDn=f"uni/tn-{tn}/ctx-{vrf}", bdDn=f"uni/tn-{tn}/BD-{bd}", flags="")
            add("compVm", dn=f"comp/prov-VMware/ctrlr-[dc1-vmm]-vcenter-dc1/vm-vm-{octet}{h:02d}", name=f"{tn}-{epg}-{h:02d}", state="poweredOn", os="Ubuntu Linux (64-bit)", cfgdOs="ubuntu64Guest", guid=f"4211{octet}{h:04d}", oid=f"vm-{octet}{h:02d}")
    for h in range(1, 7):
        add("compHv", dn=f"comp/prov-VMware/ctrlr-[dc1-vmm]-vcenter-dc1/hv-host-{h}", name=f"esx-{h}.dc1.example", state="connected", type="hv", mgmtIp=f"192.168.50.{h}", availOperSt="available", guid=f"host-{h}", oid=f"host-{h}")

    # Contracts: web -> app (http-8080), app -> db (postgres). The postgres filter entry was changed to 5433.
    contracts = [("web-to-app", "http-8080", "8080"), ("app-to-db", "postgres", "5433")]
    for name, flt, port in contracts:
        add("vzBrCP", dn=f"uni/tn-retail/brc-{name}", name=name, scope="context", prio="unspecified", targetDscp="unspecified", configIssues="", descr="")
        add("vzSubj", dn=f"uni/tn-retail/brc-{name}/subj-default", name="default", revFltPorts="yes", consMatchT="AtleastOne", provMatchT="AtleastOne", prio="unspecified", descr="")
        add("vzRsSubjFiltAtt", dn=f"uni/tn-retail/brc-{name}/subj-default/rssubjFiltAtt-{flt}", tnVzFilterName=flt, tDn=f"uni/tn-retail/flt-{flt}", action="permit", directives="", state="formed", stateQual="none")
        add("vzFilter", dn=f"uni/tn-retail/flt-{flt}", name=flt, descr="", fwdId=rng.randint(10, 99), revId=rng.randint(10, 99), txId=rng.randint(10**15, 10**16))
        entry = "pg-5432" if flt == "postgres" else "tcp-8080"
        add("vzEntry", dn=f"uni/tn-retail/flt-{flt}/e-{entry}", name=entry, etherT="ip", prot="tcp", sFromPort="unspecified", sToPort="unspecified",
            dFromPort=port, dToPort=port, stateful="no", applyToFrag="no", tcpRules="")
    add("fvRsProv", dn="uni/tn-retail/ap-checkout/epg-app/rsprov-web-to-app", tnVzBrCPName="web-to-app", tDn="uni/tn-retail/brc-web-to-app", state="formed", stateQual="none", prio="unspecified")
    add("fvRsCons", dn="uni/tn-retail/ap-checkout/epg-web/rscons-web-to-app", tnVzBrCPName="web-to-app", tDn="uni/tn-retail/brc-web-to-app", state="formed", stateQual="none", prio="unspecified")
    add("fvRsProv", dn="uni/tn-retail/ap-checkout/epg-db/rsprov-app-to-db", tnVzBrCPName="app-to-db", tDn="uni/tn-retail/brc-app-to-db", state="formed", stateQual="none", prio="unspecified")
    add("fvRsCons", dn="uni/tn-retail/ap-checkout/epg-app/rscons-app-to-db", tnVzBrCPName="app-to-db", tDn="uni/tn-retail/brc-app-to-db", state="formed", stateQual="none", prio="unspecified")
    add("l3extOut", dn="uni/tn-retail/out-corp-wan", name="corp-wan", enforceRtctrl="export", targetDscp="unspecified", descr="WAN edge on dc1-leaf-3/4")

    # ------------------------------------------------------------------ health
    add("fabricHealthTotal", dn="topology/health", cur=86, prev=98, maxSev="major", updTs=ts(ago(minutes=50)))
    add("fabricHealthTotal", dn="topology/pod-1/health", cur=86, prev=98, maxSev="major", updTs=ts(ago(minutes=50)))
    start = (now - timedelta(hours=72)).replace(second=0, microsecond=0)
    start -= timedelta(minutes=start.minute % 5)
    t, index = start, 0
    while t + timedelta(minutes=5) <= now:
        degraded = t >= now - timedelta(minutes=52)
        avg = 86 if degraded else 98
        add("fabricOverallHealthHist5min", dn=f"topology/HDfabricOverallHealth5min-{index}", index=index, repIntvStart=ts(t), repIntvEnd=ts(t + timedelta(minutes=5)),
            healthMin=avg - (1 if degraded else 0), healthAvg=avg, healthMax=avg if degraded else 99, cnt=10)
        t += timedelta(minutes=5)
        index += 1
    for dn, cur, prev in [
        (f"{node(303)}/sys/health", 79, 97), (f"{node(304)}/sys/health", 88, 98), (f"{node(201)}/sys/health", 94, 94),
        ("uni/tn-retail/health", 81, 100), ("uni/tn-retail/ap-checkout/epg-db/health", 72, 100), ("uni/tn-retail/ap-checkout/epg-app/health", 90, 100),
        ("uni/tn-retail/ap-checkout/epg-web/health", 100, 100), ("uni/tn-payments/health", 100, 100), (f"{node(301)}/sys/health", 99, 99), (f"{node(302)}/sys/health", 98, 98),
    ]:
        add("healthInst", dn=dn, cur=cur, prev=prev, chng=cur - prev, twScore=cur, maxSev="major" if cur < 90 else "cleared", updTs=ts(ago(minutes=35)))

    # ------------------------------------------------------------------ faults (active), records, events, audit
    def fault(dn, code, severity, cause, descr, created, ftype="operational", domain="infra", subject="", rule="", lc="raised", occur=1):
        add("faultInst", dn=f"{dn}/fault-{code}", code=code, severity=severity, lc=lc, cause=cause, descr=descr, type=ftype, domain=domain, subject=subject,
            rule=rule, ack="no", occur=occur, created=ts(created), lastTransition=ts(created + timedelta(seconds=150)), highestSeverity=severity, origSeverity=severity, prevSeverity=severity)

    fault(f"{node(303)}/sys/phys-[eth1/50]/phys", "F1394", "major", "interface-physical-down",
          "Physical Interface eth1/50 on Node 303 is down, reason: sfp-missing", ago(minutes=52), subject="port-down", rule="ethpm-if-port-down-fabric")
    fault(f"{node(202)}/sys/phys-[eth1/3]/phys", "F1394", "major", "interface-physical-down",
          "Physical Interface eth1/3 on Node 202 is down, reason: link-failure", ago(minutes=52), subject="port-down", rule="ethpm-if-port-down-fabric")
    fault(f"{node(304)}/sys/bgp/inst/dom-retail:retail-vrf/peer-[10.200.0.5/32]/ent-[10.200.0.5]", "F0299", "minor", "oper-state-change",
          "BGP peer is not established, current state Idle", ago(minutes=21), subject="oper-state-err", rule="bgp-peer-entry-oper-state-err", occur=14)
    fault(f"{node(201)}/sys/ch/psuslot-2/psu", "F0413", "minor", "equipment-psu-missing",
          "PSU in slot 2 failed or absent (power supply is not drawing power)", ago(days=6, hours=3), ftype="environmental", subject="equipment-psu-missing", rule="eqpt-psu-missing")
    fault(f"{node(302)}/sys", "F1371", "warning", "version-mismatch",
          "Node 302 is running n9000-16.1(4h) while the fabric default firmware is n9000-16.2(1g)", ago(days=12), ftype="config", subject="firmware", rule="firmware-running-version-mismatch")
    fault("uni/tn-retail/ap-checkout/epg-db", "F0956", "warning", "health-degraded",
          "Health score of EPG db in tenant retail dropped to 72", ago(minutes=35), ftype="operational", domain="tenant", subject="health", rule="health-degraded")
    fault(f"{node(304)}/sys/phys-[eth1/7]/phys", "F381328", "warning", "threshold-crossed",
          "TCA: CRC error count on eth1/7 crossed the minor threshold", ago(hours=5), subject="counter", rule="tca-eqpt-ingr-err-pkts-crc")
    for i in range(14):
        n = (301, 302, 303, 304)[i % 4]
        fault(f"{node(n)}/sys/phys-[eth1/{20 + i}]/phys", "F0546", "info" if i % 3 else "warning", "port-down",
              f"Port is down on eth1/{20 + i}: link-not-connected (unused port)", ago(days=rng.randint(4, 30)), subject="port-down", rule="ethpm-if-port-down-no-infra", lc="retaining" if i % 5 == 0 else "raised")
    add("faultDelegate", dn="uni/tn-retail/out-corp-wan/fd-[topology/pod-1/node-304/sys/bgp/inst/dom-retail:retail-vrf/peer-[10.200.0.5/32]/ent-[10.200.0.5]]-fault-F0299",
        code="F0299", severity="minor", lc="raised", cause="oper-state-change", descr="BGP peer 10.200.0.5 of L3Out corp-wan on node 304 is not established",
        affected="topology/pod-1/node-304/sys/bgp/inst/dom-retail:retail-vrf/peer-[10.200.0.5/32]/ent-[10.200.0.5]", changeSet="operSt (Old: established, New: idle)",
        type="operational", domain="tenant", created=ts(ago(minutes=21)), lastTransition=ts(ago(minutes=19)))
    counts: dict[str, list] = {}
    for f in mo["faultInst"]:
        counts.setdefault(f["code"], [0, f])[0] += 1
    for code, (count, f) in counts.items():
        add("faultSummary", dn=f"fltcode-{code}", code=code, count=count, severity=f["severity"], cause=f["cause"], descr=f["descr"][:60], domain=f["domain"], type=f["type"])

    rec_id = [8_589_934_000]

    def next_id():
        rec_id[0] += rng.randint(1, 5)
        return rec_id[0]

    def event(created, code, cause, descr, affected, severity="info", user="internal", ind="state-transition", changeset=""):
        rid = next_id()
        add("eventRecord", dn=f"subj-[{affected}]/rec-{rid}", id=rid, code=code, cause=cause, descr=descr, affected=affected, severity=severity,
            created=ts(created), user=user, trig="oper", ind=ind, changeSet=changeset, txId=rid * 3)

    def audit(created, user, ind, affected, descr, changeset, cause="transition"):
        rid = next_id()
        add("aaaModLR", dn=f"subj-[{affected}]/mod-{rid}", id=rid, created=ts(created), user=user, ind=ind, affected=affected, descr=descr,
            changeSet=changeset, cause=cause, sessionId=f"{rng.getrandbits(32):08x}", txId=rid * 7, code="E4206326", severity="info", trig="config")

    def frec(created, code, severity, lc, cause, descr, affected):
        rid = next_id()
        add("faultRecord", dn=f"subj-[{affected}]/fr-{rid}", code=code, severity=severity, lc=lc, cause=cause, descr=descr, affected=affected,
            created=ts(created), type="operational", domain="infra", id=rid)

    def hrec(created, affected, cur, prev):
        rid = next_id()
        add("healthRecord", dn=f"subj-[{affected}]/hr-{rid}", affected=affected, cur=cur, prev=prev, chng=cur - prev, created=ts(created), id=rid)

    # Background over 72h: endpoint moves, link flaps on server ports, routine config changes.
    users = ["admin", "netops-amartin", "netops-jlee", "automation-terraform"]
    t = now - timedelta(hours=72)
    while t < now - timedelta(minutes=70):
        t += timedelta(minutes=rng.randint(4, 14))
        n = rng.choice((301, 302, 303, 304))
        port = rng.randint(1, 12)
        kind = rng.random()
        if kind < 0.45:
            ip = f"10.10.{rng.choice((10, 20))}.{rng.randint(21, 28)}"
            event(t, "E4208903", "endpoint-moved", f"Endpoint {ip} moved within the fabric", f"uni/tn-retail/ap-checkout/epg-{'web' if '.10.' in ip else 'app'}/cep-00:50:56:0A:0{rng.randint(1, 8)}:01")
        elif kind < 0.75:
            aff = f"{node(n)}/sys/phys-[eth1/{port}]/phys"
            event(t, "E4205125", "port-up", f"Interface eth1/{port} on node {n} is up", aff)
        elif kind < 0.9:
            aff = f"uni/tn-{rng.choice(('retail', 'payments'))}/ap-{rng.choice(('checkout', 'gateway'))}"
            audit(t, rng.choice(users), rng.choice(("modification", "creation")), aff, f"Application profile {aff.rsplit('-', 1)[-1]} modified", "descr (Old: , New: managed by automation)")
        else:
            aff = f"{node(n)}/sys/phys-[eth1/{port}]/phys"
            frec(t, "F0546", "warning", "raised", "port-down", f"Port is down on eth1/{port}", aff)
            frec(t + timedelta(seconds=40), "F0546", "cleared", "retaining", "port-down", f"Port is down on eth1/{port}", aff)
        if rng.random() < 0.3:
            hrec(t, f"{node(n)}/sys", rng.randint(95, 99), rng.randint(95, 99))
    # Older history, beyond any 24h window: a firmware install on node 302 twelve days ago.
    audit(ago(days=12, hours=1), "admin", "modification", "uni/fabric/maintpol-leaf302-downgrade", "Maintenance policy leaf302-downgrade modified", "version (Old: n9000-16.2(1g), New: n9000-16.1(4h))")
    event(ago(days=12), "E4208219", "node-rebooted", "Node 302 rebooted for firmware install", f"{node(302)}/sys", severity="warning")

    # The incident timeline (newest first in queries).
    leaf3_uplink = f"{node(303)}/sys/phys-[eth1/50]/phys"
    event(ago(minutes=52), "E4205126", "port-down", "Interface eth1/50 on node 303 is down: sfp-missing", leaf3_uplink, severity="major")
    event(ago(minutes=52), "E4205126", "port-down", "Interface eth1/3 on node 202 is down: link-failure", f"{node(202)}/sys/phys-[eth1/3]/phys", severity="major")
    event(ago(minutes=51, seconds=40), "E4210472", "isis-adjacency-down", "IS-IS adjacency to 10.0.72.65 on eth1/50.2 went down", f"{node(303)}/sys/isis/inst-default/dom-overlay-1", severity="major")
    frec(ago(minutes=52), "F1394", "major", "raised", "interface-physical-down", "Physical Interface eth1/50 on Node 303 is down, reason: sfp-missing", leaf3_uplink)
    hrec(ago(minutes=51), f"{node(303)}/sys", 79, 97)
    audit(ago(minutes=38), "netops-jlee", "modification", "uni/tn-retail/flt-postgres/e-pg-5432",
          "FilterEntry pg-5432 modified", "dFromPort (Old: postgresql, New: 5433), dToPort (Old: postgresql, New: 5433)")
    event(ago(minutes=36), "E4218192", "epg-pctag-rule-update", "Zoning rules for contract app-to-db updated on nodes 301, 302, 303, 304", "uni/tn-retail/brc-app-to-db")
    hrec(ago(minutes=35), "uni/tn-retail/ap-checkout/epg-db", 72, 100)
    hrec(ago(minutes=35), "uni/tn-retail/ap-checkout/epg-app", 90, 100)
    frec(ago(minutes=35), "F0956", "warning", "raised", "health-degraded", "Health score of EPG db in tenant retail dropped to 72", "uni/tn-retail/ap-checkout/epg-db")
    for i in range(14):
        when = ago(minutes=34) + timedelta(seconds=90 * i)
        bgp = f"{node(304)}/sys/bgp/inst/dom-retail:retail-vrf/peer-[10.200.0.5/32]/ent-[10.200.0.5]"
        event(when, "E4208011", "bgp-peer-state-change", "BGP peer 10.200.0.5 state changed from Established to Idle (hold timer expired)", bgp, severity="minor")
    audit(ago(minutes=9), "netops-amartin", "modification", "uni/tn-retail/out-corp-wan", "L3Out corp-wan description modified", "descr (Old: , New: WAN edge on dc1-leaf-3/4)")
    return mo


# ---------------------------------------------------------------------------- filter grammar
class FilterError(ValueError):
    pass


def parse_filter(text: str):
    pos = 0

    def ws():
        nonlocal pos
        while pos < len(text) and text[pos].isspace():
            pos += 1

    def expr():
        nonlocal pos
        ws()
        m = re.compile(r"[a-z]+").match(text, pos)
        if not m:
            raise FilterError("expected operator")
        op = m.group(0)
        pos = m.end()
        ws()
        if pos >= len(text) or text[pos] != "(":
            raise FilterError("expected (")
        pos += 1
        args = []
        while True:
            ws()
            if pos < len(text) and text[pos] == ")":
                pos += 1
                break
            if text[pos] == '"':
                end = text.find('"', pos + 1)
                if end < 0:
                    raise FilterError("unterminated string")
                args.append(("str", text[pos + 1:end]))
                pos = end + 1
            else:
                m2 = re.compile(r"([A-Za-z][A-Za-z0-9]*)\.([A-Za-z][A-Za-z0-9]*)").match(text, pos)
                if m2 and (m2.end() >= len(text) or text[m2.end()] != "("):
                    args.append(("prop", m2.group(1), m2.group(2)))
                    pos = m2.end()
                else:
                    args.append(expr())
            ws()
            if pos < len(text) and text[pos] == ",":
                pos += 1
        return (op, args)

    tree = expr()
    ws()
    if pos != len(text):
        raise FilterError("trailing characters")
    return tree


def _when(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _cmp(a, b):
    try:
        return (float(a) > float(b)) - (float(a) < float(b))
    except (TypeError, ValueError):
        pass
    da, db = _when(a), _when(b)
    if da and db and da.tzinfo and db.tzinfo:
        return (da > db) - (da < db)
    return (a > b) - (a < b)


def evaluate(tree, cls, obj, known_props):
    op, args = tree
    if op in ("and", "or"):
        results = [evaluate(a, cls, obj, known_props) for a in args]
        return all(results) if op == "and" else any(results)
    if op == "not":
        return not evaluate(args[0], cls, obj, known_props)
    if not args or args[0][0] != "prop":
        raise FilterError(f"{op} needs a property")
    _, pcls, prop = args[0]
    if pcls != cls:
        raise FilterError(f"class {pcls} does not match {cls}")
    if prop not in known_props:
        raise FilterError(f"Unknown property {prop} for class {cls}")
    actual = obj.get(prop, "")
    values = [a[1] for a in args[1:] if a[0] == "str"]
    if op in ("eq", "ne", "gt", "ge", "lt", "le"):
        if len(values) != 1:
            raise FilterError(f"{op} takes one value")
        c = _cmp(actual, values[0])
        return {"eq": c == 0 or actual == values[0], "ne": not (c == 0 or actual == values[0]), "gt": c > 0, "ge": c >= 0, "lt": c < 0, "le": c <= 0}[op]
    if op == "wcard":
        try:
            return re.search(values[0], actual) is not None
        except re.error as e:
            raise FilterError(f"bad wcard pattern: {e}") from None
    if op == "bw":
        return _cmp(actual, values[0]) >= 0 and _cmp(actual, values[1]) <= 0
    raise FilterError(f"unsupported operator {op}")


def count_terms(tree):
    op, args = tree
    if op in ("and", "or", "not"):
        return sum(count_terms(a) for a in args if isinstance(a, tuple) and isinstance(a[0], str) and a[0] not in ("str", "prop"))
    return 1


# ---------------------------------------------------------------------------- simulator
def error_body(code, text):
    return {"totalCount": "1", "imdata": [{"error": {"attributes": {"code": str(code), "text": text}}}]}


class Simulator:
    def __init__(self):
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.mo = build_fabric(self.now)
        self.props = {cls: set().union(*(set(o) for o in objs)) | {"dn"} for cls, objs in self.mo.items()}
        # A real APIC knows every model property of a class, not just the ones seeded here.
        catalog = Path(__file__).resolve().parents[2] / "backend/app/data_sources/clients/cisco_apic/catalog.json"
        if catalog.exists():
            for table in json.loads(catalog.read_text())["tables"].values():
                self.props.setdefault(table["class"], {"dn"}).update(table["fields"])
        self.sessions: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.log: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str]] = []
        self.subscriptions: list[str] = []
        self.logins = 0
        self.fail_next: list[int] = []  # statuses to return for the next query requests (tests)
        self.users = {}
        if os.environ.get("APIC_USERNAME") and os.environ.get("APIC_PASSWORD"):
            self.users[os.environ["APIC_USERNAME"]] = (os.environ["APIC_PASSWORD"], "admin-read")
        if os.environ.get("APIC_LIMITED_USERNAME") and os.environ.get("APIC_LIMITED_PASSWORD"):
            self.users[os.environ["APIC_LIMITED_USERNAME"]] = (os.environ["APIC_LIMITED_PASSWORD"], "read-all")
        self.domains = {"corp-ad"}
        self.cert_user = os.environ.get("APIC_CERT_USERNAME")
        self.cert_dn = None
        self.cert_key = None
        if self.cert_user and os.environ.get("APIC_CERT_NAME") and os.environ.get("APIC_CERT_PEM"):
            self.cert_dn = f"uni/userext/user-{self.cert_user}/usercert-{os.environ['APIC_CERT_NAME']}"
            with open(os.environ["APIC_CERT_PEM"], "rb") as pem:
                self.cert_key = self._load_public_key(pem.read())

    @staticmethod
    def _load_public_key(pem: bytes):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        if b"CERTIFICATE" in pem:
            return x509.load_pem_x509_certificate(pem).public_key()
        return serialization.load_pem_public_key(pem)

    # ------------------------------------------------------------------ auth
    def login(self, body):
        try:
            attrs = body["aaaUser"]["attributes"]
            name, pwd = attrs["name"], attrs["pwd"]
        except (KeyError, TypeError):
            return 400, error_body(400, "Invalid request: expected aaaUser with name and pwd"), {}
        m = re.fullmatch(r"apic:([^\\]+)\\\\?(.+)", name)
        if m:
            if m.group(1) not in self.domains:
                return 401, error_body(401, "Login domain does not exist"), {}
            name = m.group(2)
        user = self.users.get(name)
        if not user or not secrets.compare_digest(user[0], pwd):
            return 401, error_body(401, "Username or password is incorrect - FAILED local authentication"), {}
        token = secrets.token_urlsafe(48)
        with self.lock:
            self.sessions[token] = {"user": name, "role": user[1], "seen": time.time()}
            self.logins += 1
        body = {"totalCount": "1", "imdata": [{"aaaLogin": {"attributes": {
            "token": token, "refreshTimeoutSeconds": str(SESSION_IDLE), "maximumLifetimeSeconds": "86400", "guiIdleTimeoutSeconds": "1200",
            "restTimeoutSeconds": "90", "creationTime": str(int(time.time())), "firstLoginTime": str(int(time.time())), "userName": name,
            "remoteUser": "true" if m else "false", "unixUserId": "15374", "sessionId": secrets.token_hex(8), "lastName": "", "firstName": "",
            "version": APIC_VERSION, "buildTime": "Fri Sep 05 2026", "node": "topology/pod-1/node-1"}, "children": []}}]}
        return 200, body, {"Set-Cookie": f"APIC-cookie={token}; path=/; HttpOnly; Secure"}

    def session_for(self, cookies):
        token = cookies.get("APIC-cookie")
        if not token:
            return None
        with self.lock:
            s = self.sessions.get(token)
            if not s or time.time() - s["seen"] > SESSION_IDLE:
                self.sessions.pop(token, None)
                return None
            s["seen"] = time.time()
            return s

    def signature_session(self, method, raw_uri, body, cookies):
        if "APIC-Request-Signature" not in cookies:
            return None
        if not self.cert_key:
            return "invalid"
        if cookies.get("APIC-Certificate-Algorithm") != "v1.0" or cookies.get("APIC-Certificate-DN") != self.cert_dn or cookies.get("APIC-Certificate-Fingerprint") != "fingerprint":
            return "invalid"
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            signature = base64.b64decode(cookies["APIC-Request-Signature"], validate=True)
            self.cert_key.verify(signature, (method + raw_uri).encode() + (body or b""), padding.PKCS1v15(), hashes.SHA256())
        except (InvalidSignature, ValueError):
            return "invalid"
        return {"user": self.cert_user, "role": "admin-read", "seen": time.time()}

    # ------------------------------------------------------------------ queries
    def class_query(self, cls, scope, params, session):
        if cls not in self.mo:
            return 400, error_body(400, f"Unknown class '{cls}'")
        objs = self.mo[cls]
        if scope:
            objs = [o for o in objs if o["dn"].startswith(scope + "/")]
        if session["role"] != "admin-read" and cls in ADMIN_READ:
            objs = []
        if "query-target-filter" in params:
            try:
                tree = parse_filter(params["query-target-filter"])
                if count_terms(tree) > 20:
                    return 400, error_body(400, "Query filter has more than 20 expressions")
                objs = [o for o in objs if evaluate(tree, cls, o, self.props[cls])]
            except FilterError as e:
                return 400, error_body(121, f"Invalid filter: {e}")
        if "order-by" in params:
            terms = []
            for part in params["order-by"].split(","):
                m = re.fullmatch(r"([A-Za-z0-9]+)\.([A-Za-z0-9]+)(?:\|(asc|desc))?", part)
                if not m or m.group(1) != cls:
                    return 400, error_body(400, f"Invalid order-by '{part}'")
                if m.group(2) not in self.props[cls]:
                    return 400, error_body(400, f"Unknown property {m.group(2)} in order-by")
                terms.append((m.group(2), m.group(3) == "desc"))
            for prop, desc in reversed(terms):
                def key(o, p=prop):
                    v = o.get(p, "")
                    d = _when(v)
                    if d and d.tzinfo:
                        return (0, d.timestamp(), "")
                    try:
                        return (0, float(v), "")
                    except ValueError:
                        return (1, 0.0, v)
                objs = sorted(objs, key=key, reverse=desc)
        total = len(objs)
        if params.get("rsp-subtree-include") == "count":
            return 200, {"totalCount": "1", "imdata": [{"moCount": {"attributes": {"count": str(total), "dn": "cnt"}}}]}
        if "page-size" in params:
            try:
                size, page = int(params["page-size"]), int(params.get("page", "0"))
            except ValueError:
                return 400, error_body(400, "page and page-size must be integers")
            if size < 1 or page < 0:
                return 400, error_body(400, "Invalid page or page-size")
            if size > MAX_PAGE_SIZE:
                return 400, error_body(400, f"page-size cannot exceed {MAX_PAGE_SIZE}")
            objs = objs[page * size:(page + 1) * size]
        elif total > UNPAGED_LIMIT:
            return 503, error_body(503, "Unable to deliver the message, Resolve timeout: result dataset is too big")
        return 200, {"totalCount": str(total), "imdata": [{cls: {"attributes": dict(o)}} for o in objs]}

    def dispatch(self, method, raw_uri, body, cookies):
        u = urlsplit(raw_uri)
        path = unquote(u.path)
        params = dict(parse_qsl(u.query, keep_blank_values=True))
        self.log.append((method, raw_uri))
        if path == "/api/aaaLogin.json":
            if method != "POST":
                return 400, error_body(400, "Login requires POST"), {}
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                payload = None
            return self.login(payload)
        if path == "/api/aaaListDomains.json" and method == "GET":
            return 200, {"totalCount": "2", "imdata": [{"name": "DefaultAuth"}, {"name": "corp-ad"}]}, {}
        session = self.signature_session(method, raw_uri, body, cookies) or self.session_for(cookies)
        if session == "invalid":
            return 403, error_body(403, "Signature verification failed"), {}
        if session is None:
            return 403, error_body(403, "Token was invalid (Error: Token timeout)"), {}
        if path == "/api/aaaLogout.json":
            if method != "POST":
                return 400, error_body(400, "Logout requires POST"), {}
            with self.lock:
                self.sessions.pop(cookies.get("APIC-cookie", ""), None)
            return 200, {"totalCount": "0", "imdata": []}, {}
        if path == "/api/aaaRefresh.json":
            token = secrets.token_urlsafe(48)
            with self.lock:
                old = self.sessions.pop(cookies.get("APIC-cookie", ""), None)
                if old:
                    self.sessions[token] = dict(old, seen=time.time())
            return 200, {"totalCount": "1", "imdata": [{"aaaLogin": {"attributes": {"token": token, "refreshTimeoutSeconds": str(SESSION_IDLE)}}}]}, {"Set-Cookie": f"APIC-cookie={token}; path=/"}
        if method != "GET":
            self.writes.append((method, raw_uri))
            return 403, error_body(403, "Write access denied: user has read-only privileges"), {}
        if params.get("subscription") == "yes":
            self.subscriptions.append(raw_uri)
            return 200, {"subscriptionId": str(secrets.randbits(63)), "totalCount": "0", "imdata": []}, {}
        if self.fail_next:
            status = self.fail_next.pop(0)
            return status, error_body(status, "Unable to deliver the message, destination is not available"), {}
        m = re.fullmatch(r"/api/(?:node/)?class/(?:(topology/pod-\d+/node-\d+)/)?([A-Za-z0-9]+)\.json", path)
        if m:
            return (*self.class_query(m.group(2), m.group(1), params, session),) + ({},)
        m = re.fullmatch(r"/api/(?:node/)?mo/(.+)\.json", path)
        if m:
            dn = m.group(1)
            for cls, objs in self.mo.items():
                for o in objs:
                    if o["dn"] == dn:
                        if session["role"] != "admin-read" and cls in ADMIN_READ:
                            return 200, {"totalCount": "0", "imdata": []}, {}
                        return 200, {"totalCount": "1", "imdata": [{cls: {"attributes": dict(o)}}]}, {}
            return 200, {"totalCount": "0", "imdata": []}, {}
        return 400, error_body(400, f"Request URL {path} is not a supported API path"), {}


def make_handler(sim, verbose=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nginx"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            if verbose:
                super().log_message(fmt, *args)

        def handle_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            cookies = {}
            for part in (self.headers.get("Cookie") or "").split(";"):
                if "=" in part:
                    k, v = part.strip().split("=", 1)
                    cookies[k] = v
            status, payload, headers = sim.dispatch(self.command, self.path, body, cookies)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = handle_any

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=18094, help="loopback port to listen on (default 18094)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; keep it on loopback)")
    p.add_argument("--verbose", action="store_true", help="log every request")
    a = p.parse_args()
    if not (os.environ.get("APIC_USERNAME") and os.environ.get("APIC_PASSWORD")) and not os.environ.get("APIC_CERT_PEM"):
        p.error("Set APIC_USERNAME and APIC_PASSWORD (local throwaway values) and/or the APIC_CERT_* variables.")
    sim = Simulator()
    total = sum(len(v) for v in sim.mo.values())
    print(f"Synthetic APIC API listening on http://{a.host}:{a.port} (APIC {APIC_VERSION}, {total} objects in {len(sim.mo)} classes)", flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(sim, a.verbose)).serve_forever()


if __name__ == "__main__":
    main()
