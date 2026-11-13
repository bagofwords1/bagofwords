#!/usr/bin/env python3
"""Synthetic HPE OneView / Synergy Composer REST API for local verification. NOT OneView.

Models the wire contract of a OneView 9.00 appliance (API currentVersion 6800,
minimumVersion 120): /rest/version without auth, POST /rest/login-sessions
returning {"partnerData": {}, "sessionID": ...}, the literal `Auth` header on
every later request, mandatory X-API-Version (412 UNSUPPORTED_API_VERSION
outside the range), OneView-shaped error bodies, start/count paging with
nextPageUri/prevPageUri/total, filter/sort/fields/query controls, and a
refusal (405 HTTP_METHOD_NOT_ALLOWED) of every non-GET except login/logout.

Binds loopback by default. Credentials come from ONEVIEW_USERNAME and
ONEVIEW_PASSWORD. The estate is deterministic: 3 Synergy 12000 frames,
24 SY 480 compute modules with mixed health, 20 profiles (3 NonCompliant),
6 Virtual Connect SE 100Gb F32 interconnects with ports, alerts, tasks,
networks, storage and 24h of hourly utilization samples.
"""

import argparse
import json
import os
import random
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

CURRENT_VERSION = 6800
MINIMUM_VERSION = 120
SOFTWARE_VERSION = "9.00.00-0417420"
DEFAULT_PAGE = 50
NS = uuid.UUID("7d4f3a0e-8c1b-4b5e-9f1a-2c3d4e5f6a7b")


def uid(*parts):
    return str(uuid.uuid5(NS, ":".join(str(p) for p in parts)))


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def build_estate(now):
    rng = random.Random(20240601)
    base = now - timedelta(days=400)
    est = {}

    def stamp(obj, uri, type_, category, created_days_ago=None, modified_hours_ago=None):
        created = base + timedelta(days=rng.randint(0, 300)) if created_days_ago is None else now - timedelta(days=created_days_ago)
        modified = now - timedelta(hours=rng.randint(1, 240)) if modified_hours_ago is None else now - timedelta(hours=modified_hours_ago)
        obj.update(uri=uri, type=type_, category=category, created=iso(created), modified=iso(modified), eTag=iso(modified))
        return obj

    # ---- enclosure groups / logical interconnect groups
    eg_uri = "/rest/enclosure-groups/" + uid("eg", 1)
    lig_uri = "/rest/logical-interconnect-groups/" + uid("lig", 1)
    est["enclosure-groups"] = [
        stamp(
            {
                "name": "EG-Prod-Synergy",
                "description": "Production Synergy enclosure group",
                "status": "OK",
                "state": "Normal",
                "enclosureCount": 3,
                "enclosureTypeUri": "/rest/enclosure-types/SY12000",
                "stackingMode": "Enclosure",
                "powerMode": "RedundantPowerFeed",
                "ipAddressingMode": "DHCP",
                "interconnectBayMappingCount": 6,
                "interconnectBayMappings": [
                    {"enclosureIndex": e, "interconnectBay": b, "logicalInterconnectGroupUri": lig_uri if b in (3, 6) else None}
                    for e in (1, 2, 3)
                    for b in (1, 2, 3, 4, 5, 6)
                ],
                "associatedInterconnectGroups": [lig_uri],
                "scopesUri": "/rest/scopes/resources" + eg_uri,
            },
            eg_uri,
            "EnclosureGroupV8",
            "enclosure-groups",
        )
    ]

    # ---- firmware drivers (2 SPPs)
    fw = []
    for i, (name, ver, date) in enumerate(
        [("HPE Synergy Custom SPP 2023.09.01", "SY-2023.09.01", "2023-09-19"), ("HPE Synergy Custom SPP 2024.04.00", "SY-2024.04.00", "2024-04-22")], 1
    ):
        u = "/rest/firmware-drivers/" + uid("fw", i)
        fw.append(
            stamp(
                {
                    "name": name,
                    "version": ver,
                    "baselineShortName": name.replace("HPE Synergy Custom ", ""),
                    "bundleType": "SPP",
                    "bundleSize": 9_500_000_000 + i * 120_000_000,
                    "isoFileName": f"{ver}.iso",
                    "releaseDate": date,
                    "resourceId": uid("fw", i),
                    "resourceState": "Created",
                    "status": "OK",
                    "state": "Created",
                    "uuid": uid("fw", i),
                    "hpsumVersion": "10.6.0",
                    "description": "Synergy Service Pack",
                    "supportedOSList": ["VMware ESXi 8.0", "Red Hat Enterprise Linux 9", "Windows Server 2022"],
                    "supportedLanguages": ["English"],
                    "fwComponents": [
                        {"name": "System ROM", "componentVersion": "I42 v2.90 (02/01/2024)" if i == 2 else "I42 v2.80 (07/19/2023)", "fileName": f"rom-{i}.fwpkg", "swKeyNameList": ["I42"]},
                        {"name": "iLO 5", "componentVersion": "3.03" if i == 2 else "2.98", "fileName": f"ilo5-{i}.fwpkg", "swKeyNameList": ["iLO5"]},
                        {"name": "HPE Synergy 4820C 10/25Gb CNA", "componentVersion": "2024.03.01" if i == 2 else "2023.09.01", "fileName": f"cna-{i}.fwpkg", "swKeyNameList": ["4820C"]},
                        {"name": "Virtual Connect SE 100Gb F32 Module for Synergy", "componentVersion": "3.2.0.1" if i == 2 else "3.1.2.0", "fileName": f"vc-{i}.fwpkg", "swKeyNameList": ["VCSE100"]},
                    ],
                    "hotfixes": [],
                    "parentBundle": None,
                    "locations": {"/rest/firmware-drivers": "appliance"},
                    "lastTaskUri": None,
                    "scopesUri": "/rest/scopes/resources" + u,
                },
                u,
                "firmware-baselines",
                "firmware-drivers",
            )
        )
    est["firmware-drivers"] = fw
    baseline_new, baseline_old = fw[1]["uri"], fw[0]["uri"]

    # ---- logical enclosure, enclosures, interconnects, logical interconnects
    le_uri = "/rest/logical-enclosures/" + uid("le", 1)
    li_uris = ["/rest/logical-interconnects/" + uid("li", i) for i in (1, 2)]
    enclosures, interconnects, servers, sas = [], [], [], []
    frame_serials = ["CZ221905K1", "CZ221905K2", "CZ221905K3"]
    frame_status = ["OK", "Warning", "OK"]
    for e in range(1, 4):
        enc_uri = "/rest/enclosures/" + uid("enc", e)
        ic_uris = {}
        for bay in (1, 2, 3, 4, 5, 6):
            if bay in (3, 6):
                ic_uri = "/rest/interconnects/" + uid("ic", e, bay)
                ic_uris[bay] = ic_uri
                status = "Critical" if (e, bay) == (2, 6) else "OK"
                fw_ver = "3.1.2.0" if (e, bay) == (3, 3) else "3.2.0.1"
                ports = []
                for q in range(1, 9):
                    linked = q <= 4 or (q == 5 and e == 1)
                    pstatus = "Linked" if linked else "Unlinked"
                    health = "Normal" if linked else ("Critical" if (e, bay, q) == (2, 6, 2) else "Unknown")
                    if (e, bay, q) == (2, 6, 2):
                        pstatus, health = "Unlinked", "Critical"
                    port_uri = f"{ic_uri}/ports/Q{q}"
                    ports.append(
                        {
                            "uri": port_uri,
                            "portName": f"Q{q}",
                            "name": f"Q{q}",
                            "portId": f"{ic_uri.rsplit('/', 1)[-1]}:Q{q}",
                            "portType": "Uplink",
                            "portTypeExtended": "External",
                            "portStatus": pstatus,
                            "portStatusReason": "None" if linked else ("LinkDown" if health == "Critical" else "Unknown"),
                            "portHealthStatus": health,
                            "status": "OK" if linked else ("Critical" if health == "Critical" else "Unknown"),
                            "state": "Normal",
                            "enabled": q <= 6,
                            "operationalSpeed": "Speed_100G" if linked else "Speed_0M",
                            "connectorType": "QSFP28" if q <= 6 else "Absent",
                            "associatedUplinkSetUri": "/rest/uplink-sets/" + uid("us", 1 if q <= 2 else 2) if q <= 4 else None,
                            "pairedPortName": None,
                            "lagId": 1 if q <= 2 else (2 if q <= 4 else 0),
                            "bayNumber": bay,
                            "interconnectName": f"Frame-{e}, interconnect {bay}",
                            "neighbor": {
                                "remoteSystemName": f"tor-sw-{e}{'a' if bay == 3 else 'b'}.example.net",
                                "remotePortId": f"Ethernet1/{q}",
                                "remotePortDescription": f"to Frame-{e} ICM{bay} Q{q}",
                                "remoteChassisId": f"00:1c:73:{e:02x}:{bay:02x}:{q:02x}",
                                "remoteMgmtAddress": f"10.10.{e}.{bay}",
                            }
                            if linked
                            else None,
                            "fcPortProperties": None,
                            "subports": [],
                            "vlans": [],
                            "dcbxInfo": {"dcbxStatus": "Enabled" if linked else "Unknown"},
                        }
                    )
                for d in range(1, 13):
                    ports.append(
                        {
                            "uri": f"{ic_uri}/ports/d{d}",
                            "portName": f"d{d}",
                            "name": f"d{d}",
                            "portId": f"{ic_uri.rsplit('/', 1)[-1]}:d{d}",
                            "portType": "Downlink",
                            "portTypeExtended": "Internal",
                            "portStatus": "Linked" if d <= 8 else "Unlinked",
                            "portStatusReason": "None",
                            "portHealthStatus": "Normal",
                            "status": "OK",
                            "state": "Normal",
                            "enabled": True,
                            "operationalSpeed": "Speed_25G" if d <= 8 else "Speed_0M",
                            "connectorType": "Internal",
                            "associatedUplinkSetUri": None,
                            "pairedPortName": None,
                            "lagId": 0,
                            "bayNumber": bay,
                            "interconnectName": f"Frame-{e}, interconnect {bay}",
                            "neighbor": None,
                            "fcPortProperties": None,
                            "subports": [],
                            "vlans": [],
                            "dcbxInfo": None,
                        }
                    )
                interconnects.append(
                    stamp(
                        {
                            "name": f"Frame-{e}, interconnect {bay}",
                            "model": "Virtual Connect SE 100Gb F32 Module for Synergy",
                            "productName": "Virtual Connect SE 100Gb F32 Module for Synergy",
                            "serialNumber": f"TWT{e}{bay}1234{e}{bay}",
                            "partNumber": "867796-B21",
                            "sparePartNumber": "868052-001",
                            "status": status,
                            "state": "Configured",
                            "powerState": "On",
                            "uidState": "Off",
                            "description": None,
                            "interconnectTypeUri": "/rest/interconnect-types/" + uid("ict", "vcse100"),
                            "logicalInterconnectUri": li_uris[0 if bay == 3 else 1],
                            "enclosureUri": enc_uri,
                            "enclosureName": f"Frame-{e}",
                            "enclosureType": "SY12000",
                            "interconnectLocation": {"locationEntries": [{"type": "Enclosure", "value": enc_uri}, {"type": "Bay", "value": str(bay)}]},
                            "firmwareVersion": fw_ver,
                            "hostName": f"vc-frame{e}-icm{bay}",
                            "interconnectIP": f"10.20.{e}.{bay}",
                            "interconnectMAC": f"94:40:c9:{e:02x}:{bay:02x}:00",
                            "portCount": 20,
                            "subPortCount": 0,
                            "stackingDomainId": 100 if bay == 3 else 101,
                            "stackingDomainRole": "Master" if e == 1 else "Slave",
                            "stackingMemberId": e,
                            "migrationState": "NotApplicable",
                            "deviceResetState": "Normal",
                            "ports": ports,
                            "snmpConfiguration": {"enabled": True, "readCommunity": "public-ro", "snmpAccess": ["10.0.0.0/8"], "trapDestinations": []},
                            "qosConfiguration": {"activeQosConfig": {"configType": "Passthrough"}},
                            "enableIgmpSnooping": False,
                            "enableNetworkLoopProtection": True,
                            "enablePauseFloodProtection": True,
                            "scopesUri": "/rest/scopes/resources" + ic_uri,
                        },
                        ic_uri,
                        "interconnectV7",
                        "interconnects",
                    )
                )
            elif bay in (1, 4):
                sas_uri = "/rest/sas-interconnects/" + uid("sas", e, bay)
                ic_uris[bay] = sas_uri
                sas.append(
                    stamp(
                        {
                            "name": f"Frame-{e}, interconnect {bay}",
                            "model": "Synergy 12Gb SAS Connection Module",
                            "productName": "Synergy 12Gb SAS Connection Module",
                            "serialNumber": f"7CE{e}{bay}0987{e}",
                            "partNumber": "755985-B21",
                            "status": "OK",
                            "state": "Configured",
                            "powerState": "On",
                            "uidState": "Off",
                            "description": None,
                            "firmwareVersion": "1.4.3",
                            "hardwareVersion": "A",
                            "interconnectTypeUri": "/rest/sas-interconnect-types/" + uid("sict", 1),
                            "sasLogicalInterconnectUri": "/rest/sas-logical-interconnects/" + uid("sli", e),
                            "enclosureUri": enc_uri,
                            "enclosureName": f"Frame-{e}",
                            "interconnectLocation": {"locationEntries": [{"type": "Enclosure", "value": enc_uri}, {"type": "Bay", "value": str(bay)}]},
                            "refreshState": "NotRefreshing",
                            "deviceResetState": "Normal",
                            "stateReason": "None",
                            "scopesUri": "/rest/scopes/resources" + sas_uri,
                        },
                        sas_uri,
                        "sas-interconnectV2",
                        "sas-interconnects",
                    )
                )
        device_bays = []
        for bay in range(1, 13):
            idx = (e - 1) * 8 + bay
            present = bay <= 8
            srv_uri = "/rest/server-hardware/" + uid("sh", idx) if present else None
            device_bays.append(
                {
                    "bayNumber": bay,
                    "devicePresence": "Present" if present else "Absent",
                    "deviceUri": srv_uri,
                    "profileUri": None,
                    "model": "Synergy 480 Gen11" if present and idx > 16 else ("Synergy 480 Gen10" if present else None),
                    "serialNumber": f"CZ2{e}{bay:02d}74{idx:02d}" if present else None,
                    "bayPowerState": "On" if present else "Off",
                    "powerAllocationWatts": 420 if present else 0,
                    "deviceFormFactor": "HalfHeight" if present else "Unknown",
                }
            )
        enclosures.append(
            stamp(
                {
                    "name": f"Frame-{e}",
                    "serialNumber": frame_serials[e - 1],
                    "uuid": uid("enc", e),
                    "enclosureModel": "Synergy 12000 Frame",
                    "enclosureType": "SY12000",
                    "status": frame_status[e - 1],
                    "state": "Configured",
                    "stateReason": "None",
                    "refreshState": "NotRefreshing",
                    "uidState": "Off",
                    "licensingIntent": "OneView",
                    "description": None,
                    "enclosureGroupUri": eg_uri,
                    "logicalEnclosureUri": le_uri,
                    "vcmMode": False,
                    "applianceBayCount": 2,
                    "deviceBayCount": 12,
                    "fanBayCount": 10,
                    "powerSupplyBayCount": 6,
                    "interconnectBayCount": 6,
                    "partNumber": "797740-B21",
                    "rackName": f"Rack-A{e}",
                    "fwBaselineUri": baseline_new if e != 3 else baseline_old,
                    "fwBaselineName": fw[1]["name"] if e != 3 else fw[0]["name"],
                    "isFwManaged": True,
                    "powerMode": "RedundantPowerFeed",
                    "powerCapacityWatts": 9600,
                    "powerAllocatedWatts": 420 * 8 + 900,
                    "deviceBays": device_bays,
                    "fanBays": [
                        {"bayNumber": b, "devicePresence": "Present", "deviceRequired": True, "model": "Synergy Fan Module", "partNumber": "804942-B21", "serialNumber": f"FAN{e}{b:02d}", "state": "Normal", "status": "Warning" if (e, b) == (2, 7) else "OK"}
                        for b in range(1, 11)
                    ],
                    "powerSupplyBays": [
                        {"bayNumber": b, "devicePresence": "Present" if b <= 4 else "Absent", "model": "HPE 2650W Platinum Hot Plug Power Supply" if b <= 4 else None, "outputCapacityWatts": 2650 if b <= 4 else 0, "serialNumber": f"PSU{e}{b:02d}" if b <= 4 else None, "status": "OK" if b <= 4 else "Unknown"}
                        for b in range(1, 7)
                    ],
                    "interconnectBays": [
                        {"bayNumber": b, "interconnectModel": ("Virtual Connect SE 100Gb F32 Module for Synergy" if b in (3, 6) else "Synergy 12Gb SAS Connection Module" if b in (1, 4) else None), "interconnectUri": ic_uris.get(b), "logicalInterconnectUri": li_uris[0 if b == 3 else 1] if b in (3, 6) else None, "bayPowerState": "On" if b in ic_uris else "Off", "interconnectReady": b in ic_uris, "powerAllocationWatts": 150 if b in ic_uris else 0}
                        for b in range(1, 7)
                    ],
                    "managerBays": [
                        {"bayNumber": b, "role": "Active" if (b == 1) == (e != 2) else "Standby", "model": "Synergy Frame Link Module", "fwVersion": "2.7.0", "fwBuildDate": "2024-03-11", "ipAddress": f"10.30.{e}.{b}", "status": "OK", "devicePresence": "Present", "linkPortState": "Linked", "linkPortStatus": "OK", "mgmtPortState": "Linked", "mgmtPortStatus": "OK", "serialNumber": f"FLM{e}{b}"}
                        for b in (1, 2)
                    ],
                    "applianceBays": [
                        {"bayNumber": b, "devicePresence": "Present" if (e, b) in ((1, 1), (2, 1)) else "Absent", "model": "Synergy Composer2" if (e, b) in ((1, 1), (2, 1)) else None, "serialNumber": f"CMP{e}{b}" if (e, b) in ((1, 1), (2, 1)) else None, "status": "OK" if (e, b) in ((1, 1), (2, 1)) else "Unknown", "poweredOn": (e, b) in ((1, 1), (2, 1))}
                        for b in (1, 2)
                    ],
                    "scopesUri": "/rest/scopes/resources" + enc_uri,
                },
                enc_uri,
                "EnclosureV8",
                "enclosures",
            )
        )
    est["enclosures"] = enclosures
    est["interconnects"] = interconnects
    est["sas-interconnects"] = sas
    est["logical-enclosures"] = [
        stamp(
            {
                "name": "LE-Prod",
                "description": None,
                "status": "Warning",
                "state": "Inconsistent",
                "enclosureGroupUri": eg_uri,
                "enclosureUris": [x["uri"] for x in enclosures],
                "logicalInterconnectUris": li_uris,
                "firmwareBaselineUri": baseline_new,
                "firmware": {"firmwareBaselineUri": baseline_new, "firmwareUpdateOn": "EnclosureOnly", "forceInstallFirmware": False, "logicalInterconnectUpdateMode": "Orchestrated"},
                "ambientTemperatureMode": "Standard",
                "powerMode": "RedundantPowerFeed",
                "scalingState": "Static",
                "deleteFailed": False,
                "ipAddressingMode": "DHCP",
                "scopesUri": "/rest/scopes/resources" + le_uri,
            },
            le_uri,
            "LogicalEnclosureV7",
            "logical-enclosures",
        )
    ]
    est["logical-interconnects"] = [
        stamp(
            {
                "name": f"LE-Prod-LIG-{'A' if i == 0 else 'B'}",
                "description": None,
                "status": "OK" if i == 0 else "Warning",
                "state": "Active",
                "consistencyStatus": "CONSISTENT" if i == 0 else "NOT_CONSISTENT",
                "stackingHealth": "Connected" if i == 0 else "BiConnected",
                "logicalInterconnectGroupUri": lig_uri,
                "enclosureUris": [x["uri"] for x in enclosures],
                "enclosureType": "SY12000",
                "interconnects": [ic["uri"] for ic in interconnects if ic["logicalInterconnectUri"] == li_uris[i]],
                "internalNetworkUris": [],
                "fabricUri": "/rest/fabrics/" + uid("fabric", 1),
                "domainUri": "/rest/domains/" + uid("domain", 1),
                "interconnectMap": {
                    "interconnectMapEntries": [
                        {"location": {"locationEntries": [{"type": "Enclosure", "value": ic["enclosureUri"]}, {"type": "Bay", "value": ic["interconnectLocation"]["locationEntries"][1]["value"]}]}, "interconnectUri": ic["uri"], "permittedInterconnectTypeUri": ic["interconnectTypeUri"], "enclosureIndex": n + 1}
                        for n, ic in enumerate(x for x in interconnects if x["logicalInterconnectUri"] == li_uris[i])
                    ]
                },
                "ethernetSettings": {"enableIgmpSnooping": False, "enableNetworkLoopProtection": True, "enablePauseFloodProtection": True, "macRefreshInterval": 5},
                "igmpSettings": {"enableIgmpSnooping": False, "igmpIdleTimeoutInterval": 260},
                "portMonitor": {"enablePortMonitor": False},
                "snmpConfiguration": {"enabled": True, "readCommunity": "public-ro", "trapDestinations": []},
                "telemetryConfiguration": {"enableTelemetry": True, "sampleCount": 12, "sampleInterval": 300},
                "icmLicenses": {"License": [{"licenseType": "OneView", "state": "Compliant", "consumedCount": 3, "requiredCount": 3}]},
                "securityStandardMode": "Legacy",
                "portFlapProtection": {"enabled": True},
            },
            li_uris[i],
            "logical-interconnectV7",
            "logical-interconnects",
        )
        for i in (0, 1)
    ]

    # ---- server hardware types
    sht = {}
    for gen, name in (("Gen10", "SY 480 Gen10 1"), ("Gen11", "SY 480 Gen11 1")):
        u = "/rest/server-hardware-types/" + uid("sht", gen)
        sht[gen] = u
        est.setdefault("server-hardware-types", []).append(
            stamp(
                {
                    "name": name,
                    "model": f"Synergy 480 {gen}",
                    "family": "Synergy",
                    "generation": gen,
                    "formFactor": "HalfHeight",
                    "platform": "x86",
                    "uefiClass": "2",
                    "status": "OK",
                    "state": "Normal",
                    "description": None,
                    "bootModes": ["UEFI", "UEFIOptimized"] if gen == "Gen11" else ["BIOS", "UEFI", "UEFIOptimized"],
                    "bootCapabilities": ["HardDisk", "PXE", "CD", "USB"],
                    "capabilities": ["ManageBIOS", "VirtualUUID", "VirtualWWN", "VirtualMAC", "FirmwareUpdate"],
                    "adapters": [{"model": "HPE Synergy 4820C 10/25Gb CNA", "deviceType": "Ethernet", "location": "Mezz", "slot": 3, "ports": [{"number": 1, "type": "Ethernet", "maxSpeedMbps": 25000}, {"number": 2, "type": "Ethernet", "maxSpeedMbps": 25000}]}],
                    "storageCapabilities": {"raidLevels": ["RAID0", "RAID1", "RAID5"], "maximumDrives": 2, "controllerModes": ["RAID", "HBA"]},
                },
                u,
                "server-hardware-type-13",
                "server-hardware-types",
            )
        )

    # ---- server hardware + profiles + templates
    spt_uris = ["/rest/server-profile-templates/" + uid("spt", i) for i in (1, 2)]
    profiles = []
    rom = {"Gen10": "I42 v2.90 (02/01/2024)", "Gen11": "U53 v1.60 (01/23/2024)"}
    for idx in range(1, 25):
        e = (idx - 1) // 8 + 1
        bay = (idx - 1) % 8 + 1
        gen = "Gen11" if idx > 16 else "Gen10"
        enc = enclosures[e - 1]
        srv_uri = "/rest/server-hardware/" + uid("sh", idx)
        status = {3: "Warning", 11: "Critical", 14: "Warning", 19: "Critical", 22: "Warning"}.get(idx, "OK")
        power = "Off" if idx in (8, 16, 24) else "On"
        has_profile = idx <= 20
        profile_uri = "/rest/server-profiles/" + uid("sp", idx) if has_profile else None
        if has_profile:
            enc["deviceBays"][bay - 1]["profileUri"] = profile_uri
        ilo_fw = "2.98" if idx in (3, 14) else ("1.59" if gen == "Gen11" else "3.03")
        servers.append(
            stamp(
                {
                    "name": f"Frame-{e}, bay {bay}",
                    "serialNumber": enc["deviceBays"][bay - 1]["serialNumber"],
                    "model": f"Synergy 480 {gen}",
                    "shortModel": f"SY 480 {gen}",
                    "status": status,
                    "state": "ProfileApplied" if has_profile else "NoProfileApplied",
                    "stateReason": "NotApplicable" if has_profile else "Unconfigured",
                    "powerState": power,
                    "uuid": uid("shuuid", idx),
                    "description": None,
                    "mpHostInfo": {"mpHostName": f"ilo-sy480-{idx:02d}.example.net", "mpIpAddresses": [{"address": f"10.40.{e}.{bay + 10}", "type": "DHCP"}]},
                    "mpFirmwareVersion": ilo_fw,
                    "mpModel": "iLO6" if gen == "Gen11" else "iLO5",
                    "memoryMb": 524288 if gen == "Gen11" else 393216,
                    "processorCount": 2,
                    "processorCoreCount": 32 if gen == "Gen11" else 24,
                    "processorType": "Intel(R) Xeon(R) Gold 6448Y" if gen == "Gen11" else "Intel(R) Xeon(R) Gold 6248R CPU @ 3.00GHz",
                    "processorSpeedMhz": 2100 if gen == "Gen11" else 3000,
                    "position": bay,
                    "locationUri": enc["uri"],
                    "serverGroupUri": eg_uri,
                    "serverProfileUri": profile_uri,
                    "serverHardwareTypeUri": sht[gen],
                    "romVersion": rom[gen] if idx not in (11, 19) else ("I42 v2.80 (07/19/2023)" if gen == "Gen10" else "U53 v1.50 (10/01/2023)"),
                    "intelligentProvisioningVersion": "4.10.95" if gen == "Gen11" else "3.95.14",
                    "licensingIntent": "OneView",
                    "hostOsType": "VMware ESXi" if idx <= 12 else ("Red Hat Enterprise Linux" if idx <= 20 else "Unknown"),
                    "partNumber": "P07256-B21" if gen == "Gen11" else "871940-B21",
                    "assetTag": f"AT-{idx:04d}",
                    "formFactor": "HalfHeight",
                    "generation": gen,
                    "maintenanceMode": idx == 22,
                    "refreshState": "NotRefreshing",
                    "uidState": "Off",
                    "signature": {"personalityChecksum": 1000 + idx, "serverHwChecksum": 2000 + idx},
                    "serverSettings": {"firmwareAndDriversInstallState": {"installState": "InstalledAndActive"}, "hpSmartUpdateToolStatus": {"mode": "AutoStage"}},
                    "portMap": {"deviceSlots": [{"deviceName": "HPE Synergy 4820C 10/25Gb CNA", "location": "Mezz", "slotNumber": 3, "physicalPorts": [{"portNumber": 1, "mac": f"94:40:c9:4a:{idx:02x}:01", "type": "Ethernet"}, {"portNumber": 2, "mac": f"94:40:c9:4a:{idx:02x}:02", "type": "Ethernet"}]}]},
                    "scopesUri": "/rest/scopes/resources" + srv_uri,
                },
                srv_uri,
                "server-hardware-12",
                "server-hardware",
            )
        )
        if has_profile:
            template = spt_uris[0] if idx <= 12 else spt_uris[1]
            noncompliant = idx in (3, 11, 14)
            profiles.append(
                stamp(
                    {
                        "name": f"{'esx' if idx <= 12 else 'rhel'}-{idx:02d}",
                        "description": "Production ESXi host" if idx <= 12 else "Production RHEL host",
                        "status": status if status != "Critical" else "Warning",
                        "state": "Normal",
                        "serverHardwareUri": srv_uri,
                        "serverHardwareTypeUri": sht[gen],
                        "enclosureGroupUri": eg_uri,
                        "enclosureUri": enc["uri"],
                        "enclosureBay": bay,
                        "affinity": "Bay",
                        "serialNumber": f"VCGSY{idx:06d}",
                        "uuid": uid("spuuid", idx),
                        "templateCompliance": "NonCompliant" if noncompliant else "Compliant",
                        "serverProfileTemplateUri": template,
                        "iscsiInitiatorName": None,
                        "inProgress": False,
                        "taskUri": "/rest/tasks/" + uid("task", 100 + idx),
                        "refreshState": "NotRefreshing",
                        "serverHardwareReapplyState": "NotApplying",
                        "firmware": {"firmwareBaselineUri": baseline_old if noncompliant else baseline_new, "manageFirmware": True, "firmwareInstallType": "FirmwareAndOSDrivers", "forceInstallFirmware": False, "complianceControl": "Checked", "consistencyState": "Inconsistent" if noncompliant else "Consistent"},
                        "connectionSettings": {"manageConnections": True, "complianceControl": "Checked", "connections": [{"id": 1, "name": "mgmt-a", "functionType": "Ethernet", "networkUri": "/rest/network-sets/" + uid("ns", 1), "portId": "Mezz 3:1-a", "requestedMbps": "2500", "state": "Configured", "status": "OK", "mac": f"94:40:c9:4a:{idx:02x}:01"}, {"id": 2, "name": "mgmt-b", "functionType": "Ethernet", "networkUri": "/rest/network-sets/" + uid("ns", 1), "portId": "Mezz 3:2-a", "requestedMbps": "2500", "state": "Configured", "status": "OK", "mac": f"94:40:c9:4a:{idx:02x}:02"}]},
                        "bios": {"manageBios": True, "complianceControl": "Checked", "overriddenSettings": [{"id": "WorkloadProfile", "value": "Virtualization-MaxPerformance"}]},
                        "boot": {"manageBoot": True, "complianceControl": "Checked", "order": ["HardDisk"]},
                        "bootMode": {"manageMode": True, "mode": "UEFIOptimized", "pxeBootPolicy": "Auto", "secureBoot": "Disabled", "complianceControl": "Checked"},
                        "localStorage": {"controllers": [{"deviceSlot": "Embedded", "mode": "RAID", "logicalDrives": [{"name": "os", "raidLevel": "RAID1", "bootable": True}]}]},
                        "sanStorage": {"manageSanStorage": False, "volumeAttachments": []},
                        "osDeploymentSettings": None,
                        "macType": "Virtual",
                        "wwnType": "Virtual",
                        "serialNumberType": "Virtual",
                        "iscsiInitiatorNameType": "AutoGenerated",
                        "hideUnusedFlexNics": True,
                        "scopesUri": "/rest/scopes/resources" + profile_uri,
                    },
                    profile_uri,
                    "ServerProfileV12",
                    "server-profiles",
                )
            )
    est["server-hardware"] = servers
    est["server-profiles"] = profiles
    est["server-profile-templates"] = [
        stamp(
            {
                "name": "SPT-ESXi-Prod" if i == 0 else "SPT-RHEL-Prod",
                "description": "ESXi production template" if i == 0 else "RHEL production template",
                "serverProfileDescription": "Production ESXi host" if i == 0 else "Production RHEL host",
                "status": "OK",
                "state": "Normal",
                "serverHardwareTypeUri": sht["Gen10"] if i == 0 else sht["Gen11"],
                "enclosureGroupUri": eg_uri,
                "affinity": "Bay",
                "firmware": {"firmwareBaselineUri": baseline_new, "manageFirmware": True, "firmwareInstallType": "FirmwareAndOSDrivers", "forceInstallFirmware": False, "complianceControl": "Checked"},
                "connectionSettings": {"manageConnections": True, "complianceControl": "Checked", "connections": [{"id": 1, "name": "mgmt-a", "functionType": "Ethernet", "networkUri": "/rest/network-sets/" + uid("ns", 1), "portId": "Mezz 3:1-a", "requestedMbps": "2500"}, {"id": 2, "name": "mgmt-b", "functionType": "Ethernet", "networkUri": "/rest/network-sets/" + uid("ns", 1), "portId": "Mezz 3:2-a", "requestedMbps": "2500"}]},
                "bios": {"manageBios": True, "complianceControl": "Checked", "overriddenSettings": [{"id": "WorkloadProfile", "value": "Virtualization-MaxPerformance"}]},
                "boot": {"manageBoot": True, "complianceControl": "Checked", "order": ["HardDisk"]},
                "bootMode": {"manageMode": True, "mode": "UEFIOptimized", "pxeBootPolicy": "Auto", "secureBoot": "Disabled", "complianceControl": "Checked"},
                "localStorage": {"controllers": [{"deviceSlot": "Embedded", "mode": "RAID", "logicalDrives": [{"name": "os", "raidLevel": "RAID1", "bootable": True}]}]},
                "sanStorage": {"manageSanStorage": False, "volumeAttachments": []},
                "osDeploymentSettings": None,
                "macType": "Virtual",
                "wwnType": "Virtual",
                "serialNumberType": "Virtual",
                "iscsiInitiatorNameType": "AutoGenerated",
                "hideUnusedFlexNics": True,
                "scopesUri": "/rest/scopes/resources" + u,
            },
            u,
            "ServerProfileTemplateV8",
            "server-profile-templates",
        )
        for i, u in enumerate(spt_uris)
    ]

    # ---- networks, network sets, uplink sets
    enets = []
    for i, (name, vlan, purpose) in enumerate(
        [("Mgmt-VLAN100", 100, "Management"), ("vMotion-VLAN110", 110, "VMMigration"), ("Prod-App-VLAN200", 200, "General"), ("Prod-DB-VLAN210", 210, "General"), ("iSCSI-A-VLAN300", 300, "ISCSI"), ("iSCSI-B-VLAN301", 301, "ISCSI")],
        1,
    ):
        u = "/rest/ethernet-networks/" + uid("enet", i)
        enets.append(
            stamp(
                {"name": name, "vlanId": vlan, "ethernetNetworkType": "Tagged", "purpose": purpose, "smartLink": True, "privateNetwork": False, "subnetUri": None, "status": "OK", "state": "Active", "description": None, "fabricUri": "/rest/fabrics/" + uid("fabric", 1), "connectionTemplateUri": "/rest/connection-templates/" + uid("ct", i), "scopesUri": "/rest/scopes/resources" + u},
                u,
                "ethernet-networkV4",
                "ethernet-networks",
            )
        )
    est["ethernet-networks"] = enets
    est["fc-networks"] = [
        stamp(
            {"name": f"SAN-{side}", "fabricType": "FabricAttach", "managedSanUri": None, "linkStabilityTime": 30, "autoLoginRedistribution": True, "status": "OK", "state": "Active", "description": None, "fabricUri": "/rest/fabrics/" + uid("fabric", 1), "connectionTemplateUri": "/rest/connection-templates/" + uid("fcct", side), "scopesUri": "/rest/scopes/resources/rest/fc-networks/" + uid("fc", side)},
            "/rest/fc-networks/" + uid("fc", side),
            "fc-networkV4",
            "fc-networks",
        )
        for side in ("A", "B")
    ]
    est["fcoe-networks"] = []
    est["network-sets"] = [
        stamp(
            {"name": "NS-Prod", "networkUris": [n["uri"] for n in enets[:4]], "nativeNetworkUri": enets[0]["uri"], "networkSetType": "Regular", "connectionTemplateUri": "/rest/connection-templates/" + uid("nsct", 1), "status": "OK", "state": "Active", "description": None, "scopesUri": "/rest/scopes/resources/rest/network-sets/" + uid("ns", 1)},
            "/rest/network-sets/" + uid("ns", 1),
            "network-setV5",
            "network-sets",
        )
    ]
    est["uplink-sets"] = [
        stamp(
            {
                "name": f"US-Prod-{'A' if i == 1 else 'B'}",
                "logicalInterconnectUri": li_uris[0 if i == 1 else 1],
                "networkType": "Ethernet",
                "ethernetNetworkType": "Tagged",
                "connectionMode": "Auto",
                "lacpTimer": "Short",
                "fcMode": "NONE",
                "manualLoginRedistributionState": "NotSupported",
                "nativeNetworkUri": None,
                "networkUris": [n["uri"] for n in enets],
                "fcNetworkUris": [],
                "fcoeNetworkUris": [],
                "portConfigInfos": [
                    {"portUri": f"{ic['uri']}/ports/Q{q}", "desiredSpeed": "Auto", "location": {"locationEntries": [{"type": "Enclosure", "value": ic["enclosureUri"]}, {"type": "Bay", "value": ic["interconnectLocation"]["locationEntries"][1]["value"]}, {"type": "Port", "value": f"Q{q}"}]}, "expectedNeighbor": None}
                    for ic in interconnects
                    if ic["logicalInterconnectUri"] == li_uris[0 if i == 1 else 1]
                    for q in (1, 2)
                ],
                "primaryPort": None,
                "reachability": "Reachable" if i == 1 else "NotReachable",
                "status": "OK" if i == 1 else "Warning",
                "state": "Active",
                "description": None,
            },
            "/rest/uplink-sets/" + uid("us", i),
            "uplink-setV7",
            "uplink-sets",
        )
        for i in (1, 2)
    ]

    # ---- facilities
    est["racks"] = [
        stamp(
            {"name": f"Rack-A{e}", "serialNumber": f"RACK{e}001", "partNumber": "H6J66A", "model": "HPE 42U 600x1200mm Enterprise Shock Rack", "uuid": uid("rack", e), "depth": 1200, "height": 2000, "width": 600, "uHeight": 42, "thermalLimit": 10000, "rackMounts": [{"mountUri": enclosures[e - 1]["uri"], "topUSlot": 12, "uHeight": 10, "location": "CenterFront"}], "status": "OK", "state": "Normal", "description": None},
            "/rest/racks/" + uid("rack", e),
            "RackV4",
            "racks",
        )
        for e in (1, 2, 3)
    ]
    est["datacenters"] = [
        stamp(
            {"name": "DC-East-Hall2", "width": 15000, "depth": 20000, "coolingCapacity": 150, "coolingMultiplier": 1.5, "costPerKilowattHour": 0.12, "currency": "USD", "deratingType": "NaJp", "deratingPercentage": 20.0, "defaultPowerLineVoltage": 220, "contents": [{"resourceUri": r["uri"], "x": 1000 + i * 1200, "y": 2000, "rotation": 0} for i, r in enumerate(est["racks"])], "status": "OK", "state": "Normal", "description": None},
            "/rest/datacenters/" + uid("dc", 1),
            "DatacenterV5",
            "datacenters",
        )
    ]
    est["power-devices"] = [
        stamp(
            {"name": f"PDU-A{e}-{side}", "model": "HPE G2 Metered 3Ph 17.3kVA PDU", "serialNumber": f"PDU{e}{side}001", "partNumber": "P9R83A", "uuid": uid("pdu", e, side), "deviceType": "PowerStrip", "capacity": 48, "feedIdentifier": side, "lineVoltage": 208, "phaseType": "ThreePhase", "ratedCapacity": 17300, "powerConnections": [{"connectionUri": enclosures[e - 1]["uri"], "deviceConnection": f"Outlet {n}", "sourceConnection": "Feed " + side} for n in (1, 2)], "refreshState": "NotRefreshing", "uidState": "Off", "managedBy": {"hostName": f"pdu-a{e}-{side.lower()}.example.net", "username": "monitor", "password": "REDACTED"}, "status": "OK" if (e, side) != (2, "B") else "Warning", "state": "Configured", "description": None},
            "/rest/power-devices/" + uid("pdu", e, side),
            "PowerDeviceV4",
            "power-devices",
        )
        for e in (1, 2, 3)
        for side in ("A", "B")
    ]

    # ---- storage
    ss_uri = "/rest/storage-systems/" + uid("ss", 1)
    est["storage-systems"] = [
        stamp(
            {"name": "primera-a670-01", "hostname": "primera-a670-01.example.net", "family": "StoreServ", "mode": "Managed", "status": "OK", "state": "Managed", "description": None, "totalCapacity": 460_000_000_000_000, "storagePoolsUri": "/rest/storage-pools?filter=\"'storageSystemUri'='" + ss_uri + "'\"", "ports": [{"id": "0:3:1", "name": "0:3:1", "mode": "Managed", "portWwn": "20:31:00:02:AC:01:23:45", "expectedNetworkUri": est["fc-networks"][0]["uri"], "actualNetworkUri": est["fc-networks"][0]["uri"], "status": "OK"}, {"id": "1:3:1", "name": "1:3:1", "mode": "Managed", "portWwn": "21:31:00:02:AC:01:23:45", "expectedNetworkUri": est["fc-networks"][1]["uri"], "actualNetworkUri": est["fc-networks"][1]["uri"], "status": "OK"}], "deviceSpecificAttributes": {"firmware": "4.5.14", "model": "HPE Primera A670", "softwareVersion": "4.5.14", "managedDomain": "TestDomain", "managedPools": [{"name": "SSD_r6", "domain": "TestDomain", "deviceType": "SSD", "raidLevel": "RAID6", "freeCapacity": 120_000_000_000_000, "totalCapacity": 460_000_000_000_000}]}, "credentials": {"username": "oneview", "password": "REDACTED"}},
            ss_uri,
            "StorageSystemV11",
            "storage-systems",
        )
    ]
    pool_uri = "/rest/storage-pools/" + uid("pool", 1)
    est["storage-pools"] = [
        stamp(
            {"name": "SSD_r6", "storageSystemUri": ss_uri, "totalCapacity": 460_000_000_000_000, "freeCapacity": 120_000_000_000_000, "allocatedCapacity": 340_000_000_000_000, "isManaged": True, "status": "OK", "state": "Normal", "description": None, "deviceSpecificAttributes": {"domain": "TestDomain", "supportedRaidLevel": "RAID6", "deviceSpeed": "SSD", "capacityLimit": 460_000_000_000_000, "capacityWarningLimit": 414_000_000_000_000, "allocatedCapacity": {"totalAllocatedCapacity": 340_000_000_000_000, "volumeAllocatedCapacity": 330_000_000_000_000, "snapshotAllocatedCapacity": 10_000_000_000_000}}},
            pool_uri,
            "StoragePoolV3",
            "storage-pools",
        )
    ]
    est["storage-volumes"] = [
        stamp(
            {"name": f"vol-db-{i:02d}", "storageSystemUri": ss_uri, "storagePoolUri": pool_uri, "volumeTemplateUri": None, "deviceVolumeName": f"vol-db-{i:02d}", "isShareable": i <= 2, "isPermanent": True, "provisioningType": "Thin", "provisionedCapacity": 2_000_000_000_000 * i, "allocatedCapacity": 900_000_000_000 * i, "requestingRefresh": False, "status": "OK" if i != 4 else "Warning", "state": "Normal", "description": None, "deviceSpecificAttributes": {"iqn": None, "transport": "FC", "copyState": "normal", "isDeduplicated": True, "isCompressed": True, "dataProtectionLevel": "RAID6"}},
            "/rest/storage-volumes/" + uid("vol", i),
            "StorageVolumeV3",
            "storage-volumes",
        )
        for i in range(1, 7)
    ]
    est["scopes"] = [
        stamp({"name": n, "description": d, "status": "OK", "state": "Normal", "applianceId": uid("appliance"), "oldUri": None, "extAttributes": {}}, "/rest/scopes/" + uid("scope", n), "ScopeV3", "scopes")
        for n, d in (("Prod", "Production estate"), ("Lab", "Lab frames"))
    ]

    # ---- alerts (~80), events, tasks (~40)
    alerts, events, tasks = [], [], []
    alert_specs = []
    for s in servers:
        if s["status"] == "Critical":
            alert_specs.append(("Critical", "Active", "Alerts.Hardware.ProcessorStatus", "Thermal" if s["uri"].endswith(uid("sh", 11)) else "Power", f"Processor 2 of {s['name']} reported a thermal fault; the server was throttled.", s, "server-hardware", "Immediate", "Check the processor heat sink and fans; collect an AHS log."))
            alert_specs.append(("Warning", "Active", "Alerts.Hardware.PowerSupplyRedundancyLost", "Power", f"Power redundancy lost on {s['name']}.", s, "server-hardware", "High", "Restore the redundant power feed."))
        elif s["status"] == "Warning":
            alert_specs.append(("Warning", "Active", "Alerts.Firmware.Noncompliant" if s["mpFirmwareVersion"] == "2.98" else "Alerts.Hardware.MemoryStatus", "Firmware" if s["mpFirmwareVersion"] == "2.98" else "Memory", f"{s['name']}: " + ("iLO firmware is below the baseline." if s["mpFirmwareVersion"] == "2.98" else "Correctable memory errors exceeded the threshold on DIMM 7."), s, "server-hardware", "Medium", "Update firmware from the baseline." if s["mpFirmwareVersion"] == "2.98" else "Replace DIMM 7 at the next maintenance window."))
    for p in profiles:
        if p["templateCompliance"] == "NonCompliant":
            alert_specs.append(("Warning", "Active", "Alerts.ServerProfile.TemplateNonCompliant", "Profile", f"Server profile {p['name']} is not compliant with its template.", p, "server-profiles", "Medium", "Review the differences and update the profile from the template."))
    for ic in interconnects:
        if ic["status"] == "Critical":
            alert_specs.append(("Critical", "Active", "Alerts.Interconnect.UplinkDown", "Network", f"Uplink port Q2 on {ic['name']} is down (LinkDown).", ic, "interconnects", "Immediate", "Check the transceiver and the upstream switch port Ethernet1/2."))
        if ic["firmwareVersion"] == "3.1.2.0":
            alert_specs.append(("Warning", "Active", "Alerts.Firmware.Noncompliant", "Firmware", f"{ic['name']} firmware 3.1.2.0 does not match the logical interconnect baseline.", ic, "interconnects", "Medium", "Update the logical interconnect firmware."))
    alert_specs.append(("Warning", "Active", "Alerts.Enclosure.FanStatus", "Thermal", "Fan 7 in Frame-2 is degraded.", enclosures[1], "enclosures", "Medium", "Replace the fan module."))
    alert_specs.append(("Warning", "Active", "Alerts.LogicalInterconnect.Inconsistent", "Network", "Logical interconnect LE-Prod-LIG-B is not consistent with its group.", est["logical-interconnects"][1], "logical-interconnects", "Medium", "Update the logical interconnect from the group."))
    alert_specs.append(("Warning", "Active", "Alerts.UplinkSet.NotReachable", "Network", "Uplink set US-Prod-B has no reachable uplinks.", est["uplink-sets"][1], "uplink-sets", "High", "Check the uplink ports."))
    alert_specs.append(("Warning", "Active", "Alerts.PowerDevice.Load", "Power", "PDU-A2-B load exceeds 80% of rated capacity.", est["power-devices"][3], "power-devices", "Medium", "Rebalance the load."))
    alert_specs.append(("Warning", "Active", "Alerts.StorageVolume.Capacity", "Storage", "Volume vol-db-04 is above 85% allocated.", est["storage-volumes"][3], "storage-volumes", "Low", "Expand the volume."))
    alert_specs.append(("Warning", "Locked", "Alerts.CertificateStatus.NonCompliant", "Certificate Management", "The appliance certificate uses a weak signature algorithm.", {"uri": "/rest/appliance", "name": "Appliance"}, "appliance", "Low", "Refer to the appliance specific documentation for required actions."))
    cleared_pool = [s for s in servers if s["status"] == "OK"]
    for i in range(80 - len(alert_specs)):
        s = cleared_pool[i % len(cleared_pool)]
        kind = i % 4
        alert_specs.append(
            [
                ("OK", "Cleared", "Alerts.Hardware.ServerPowerOn", "Power", f"{s['name']} was powered on.", s, "server-hardware", "None", None),
                ("Warning", "Cleared", "Alerts.Hardware.TemperatureStatus", "Thermal", f"Ambient temperature on {s['name']} returned to normal.", s, "server-hardware", "None", "None"),
                ("Critical", "Cleared", "Alerts.Hardware.NetworkLinkDown", "Network", f"Link on {s['name']} Mezz 3:1 was down and recovered.", s, "server-hardware", "None", "None"),
                ("Unknown", "Cleared", "Alerts.Hardware.Unreachable", "Communication", f"iLO on {s['name']} was unreachable for 2 minutes.", s, "server-hardware", "None", "None"),
            ][kind]
        )
    for i, (sev, state, tid, hc, desc, res, rcat, urgency, action) in enumerate(alert_specs, 1):
        age_h = rng.randint(1, 20) if state == "Active" else rng.randint(30, 24 * 60)
        created = now - timedelta(hours=age_h, minutes=rng.randint(0, 59))
        a_uri = f"/rest/alerts/{i}"
        ev_uri = f"/rest/events/{i}"
        alerts.append(
            {
                "uri": a_uri,
                "type": "AlertResourceV3",
                "category": "alerts",
                "created": iso(created),
                "modified": iso(created + timedelta(hours=1) if state == "Cleared" else created),
                "eTag": iso(created),
                "description": desc,
                "severity": sev,
                "alertState": state,
                "alertTypeID": tid,
                "healthCategory": hc,
                "urgency": urgency,
                "resourceUri": res["uri"],
                "resourceID": res["uri"].rsplit("/", 1)[-1],
                "physicalResourceType": rcat,
                "lifeCycle": False,
                "correctiveAction": action,
                "assignedToUser": "operator1" if (state == "Active" and i % 3 == 0) else None,
                "eventUri": ev_uri,
                "serviceEventSource": False,
                "associatedResource": {"resourceName": res["name"], "resourceUri": res["uri"], "resourceCategory": rcat, "associationType": "HAS_A"},
                "changeLog": [{"uri": f"/rest/alerts/AlertChangeLog/{i}", "userEntered": False, "username": "system", "notes": "Alert created", "alertState": "Active", "created": iso(created)}]
                + ([{"uri": f"/rest/alerts/AlertChangeLog/{i}-c", "userEntered": False, "username": "system", "notes": "Condition cleared", "alertState": "Cleared", "created": iso(created + timedelta(hours=1))}] if state == "Cleared" else []),
            }
        )
        events.append(
            {
                "uri": ev_uri,
                "type": "EventResourceV3",
                "category": "events",
                "created": iso(created),
                "modified": iso(created),
                "eTag": iso(created),
                "description": desc,
                "eventTypeID": "Trap." + tid.split(".")[-1] if rcat == "server-hardware" else "StatusPoll." + tid.split(".")[-1],
                "severity": sev,
                "healthCategory": hc,
                "urgency": urgency,
                "rxTime": iso(created),
                "resourceUri": res["uri"],
                "eventDetails": [{"eventItemName": "ipv4Address", "eventItemValue": "10.40.1.11", "isThisVarbindData": False, "varBindOrderIndex": -1}],
                "serviceEventSource": False,
                "serviceEventDetails": None,
                "associatedResource": {"resourceName": res["name"], "resourceUri": res["uri"], "resourceCategory": rcat},
            }
        )
    est["alerts"] = alerts
    est["events"] = events
    task_specs = []
    for p in profiles:
        task_specs.append(("Update", p, "server-profiles", "Error" if p["templateCompliance"] == "NonCompliant" and p["name"] == "esx-11" else "Completed", "User"))
    for s in servers[:10]:
        task_specs.append(("Refresh", s, "server-hardware", "Completed", "User"))
    task_specs.append(("Update firmware", est["logical-interconnects"][1], "logical-interconnects", "Error", "User"))
    task_specs.append(("Update firmware", enclosures[2], "enclosures", "Error", "User"))
    task_specs.append(("Apply", est["logical-enclosures"][0], "logical-enclosures", "Warning", "User"))
    task_specs.append(("Collect utilization data", servers[0], "server-hardware", "Running", "Background"))
    task_specs.append(("Backup", {"uri": "/rest/appliance", "name": "Appliance"}, "appliance", "Completed", "Appliance"))
    for n in range(40 - len(task_specs)):
        s = servers[n % len(servers)]
        task_specs.append(("Power on" if n % 2 else "Reset", s, "server-hardware", "Completed", "User"))
    for i, (name, res, rcat, state, ttype) in enumerate(task_specs, 1):
        created = now - timedelta(hours=rng.randint(1, 24 * 14), minutes=rng.randint(0, 59))
        tid = uid("task", 100 + i) if i <= 20 else uid("task", 500 + i)
        duration = timedelta(minutes=rng.randint(1, 45))
        errors = []
        if state == "Error":
            errors = [{"errorCode": "FIRMWARE_UPDATE_FAILED" if "firmware" in name.lower() else "PROFILE_UPDATE_FAILED", "message": f"{name} failed for {res['name']}.", "details": "The target did not respond within the expected time." if "firmware" in name.lower() else "The server hardware reported a BIOS settings mismatch.", "recommendedActions": ["Retry the operation.", "Review the associated alerts."], "errorSource": None, "nestedErrors": []}]
        tasks.append(
            {
                "uri": f"/rest/tasks/{tid}",
                "name": name,
                "type": "TaskResourceV2",
                "category": "tasks",
                "created": iso(created),
                "modified": iso(created + duration if state != "Running" else now),
                "eTag": iso(created + duration),
                "taskState": state,
                "taskStatus": {"Completed": f"{name} completed for {res['name']}", "Error": f"{name} failed for {res['name']}", "Warning": f"{name} completed with warnings for {res['name']}", "Running": f"{name} in progress for {res['name']}"}[state],
                "taskType": ttype,
                "stateReason": None if state != "Error" else "ValidationError",
                "percentComplete": 100 if state != "Running" else 40,
                "computedPercentComplete": 100 if state != "Running" else 40,
                "completedSteps": 5 if state != "Running" else 2,
                "totalSteps": 5,
                "expectedDuration": 600,
                "owner": "administrator" if ttype == "User" else "system",
                "userInitiated": ttype == "User",
                "hidden": ttype == "Background",
                "parentTaskUri": None,
                "associatedTaskUri": None,
                "taskErrors": errors,
                "progressUpdates": [{"timestamp": iso(created + timedelta(seconds=30)), "statusUpdate": "Validating", "id": 1}, {"timestamp": iso(created + duration), "statusUpdate": state, "id": 2}],
                "taskOutput": [],
                "associatedResource": {"resourceName": res["name"], "resourceUri": res["uri"], "resourceCategory": rcat, "associationType": "MANAGED_BY"},
            }
        )
    est["tasks"] = tasks

    # ---- index
    index = []
    for cat in ("server-hardware", "enclosures", "interconnects", "server-profiles", "ethernet-networks", "storage-volumes"):
        for r in est[cat]:
            index.append(
                {
                    "uri": r["uri"],
                    "name": r["name"],
                    "category": cat,
                    "type": "IndexResourceV300",
                    "status": r.get("status"),
                    "state": r.get("state"),
                    "description": r.get("description"),
                    "created": r["created"],
                    "modified": r["modified"],
                    "attributes": {k: r[k] for k in ("model", "serialNumber", "status", "state", "powerState", "uuid", "enclosureModel", "vlanId") if k in r},
                    "multiAttributes": {},
                }
            )
    est["index"] = index
    return est


# ----------------------------------------------------------------------------- helpers
def error(status, code, message, details=None, actions=()):
    return status, {
        "errorCode": code,
        "message": message,
        "details": details,
        "messageParameters": [],
        "recommendedActions": list(actions),
        "errorSource": None,
        "nestedErrors": [],
        "data": {},
    }


def at(row, dotted):
    for part in dotted.split("."):
        if isinstance(row, list):
            return [at(r, part) for r in row]
        if not isinstance(row, dict):
            return None
        row = row.get(part)
    return row


def coerce(v):
    if isinstance(v, str):
        s = v.strip()
        if (s.startswith("'") and s.endswith("'")) or (s.startswith('"') and s.endswith('"')):
            s = s[1:-1]
        if s.lower() in ("true", "false"):
            return s.lower() == "true"
        try:
            return int(s)
        except ValueError:
            try:
                return float(s)
            except ValueError:
                return s
    return v


TERM = re.compile(r"^\s*([A-Za-z0-9_.]+)\s*(matches|>=|<=|<>|!=|=|>|<)\s*(.+?)\s*$", re.I)


def match_term(row, term):
    m = TERM.match(term)
    if not m:
        return None
    field, op, raw = m.group(1), m.group(2).lower(), coerce(m.group(3))
    actual = at(row, field)
    values = actual if isinstance(actual, list) else [actual]

    def one(v):
        if op == "matches":
            pattern = "^" + re.escape(str(raw)).replace("%", ".*") + "$"
            return v is not None and re.match(pattern, str(v), re.I) is not None
        if op == "=":
            return str(v).lower() == str(raw).lower() if not isinstance(raw, (int, float)) or isinstance(raw, bool) else v == raw
        if op in ("<>", "!="):
            return not one_eq(v)
        if v is None:
            return False
        try:
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                v = float(v)
                r = float(raw)
            else:
                v, r = str(v), str(raw)
            return {">": v > r, "<": v < r, ">=": v >= r, "<=": v <= r}[op]
        except (TypeError, ValueError):
            return False

    def one_eq(v):
        return str(v).lower() == str(raw).lower() if not isinstance(raw, (int, float)) or isinstance(raw, bool) else v == raw

    return any(one(v) for v in values)


def apply_filter(rows, expr):
    expr = expr.strip()
    if expr.startswith('"') and expr.endswith('"'):
        expr = expr[1:-1]
    out = []
    for row in rows:
        ok = True
        for conj in re.split(r"\s+AND\s+", expr, flags=re.I):
            parts = re.split(r"\s+OR\s+", conj, flags=re.I)
            results = [match_term(row, p) for p in parts]
            if any(r is None for r in results):
                return None
            if not any(results):
                ok = False
                break
        if ok:
            out.append(row)
    return out


def project(row, fields):
    out = {}
    for f in fields:
        if f in row:
            out[f] = row[f]
    for k in ("uri", "type", "category"):
        if k in row:
            out.setdefault(k, row[k])
    return out


# ----------------------------------------------------------------------------- server
class Simulator:
    def __init__(self):
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.estate = build_estate(self.now)
        self.sessions = {}
        self.lock = __import__("threading").Lock()

    def login(self, body):
        user = os.environ.get("ONEVIEW_USERNAME", "")
        pwd = os.environ.get("ONEVIEW_PASSWORD", "")
        if not isinstance(body, dict) or not isinstance(body.get("userName"), str) or not isinstance(body.get("password"), str):
            return error(400, "INVALID_ARGUMENT", "The request body is not valid.", "userName and password are required.", ["Provide userName and password."])
        domain = body.get("authLoginDomain", "local") or "local"
        if domain.lower() != "local":
            return error(400, "AUTHN_LOGINDOMAIN_NOT_FOUND", f"The login domain {domain} does not exist.", None, ["Use the local domain or a configured directory."])
        if not pwd or body["userName"] != user or body["password"] != pwd:
            return error(400, "AUTHN_AUTH_FAIL", "Invalid username or password.", "Authentication failed for the user.", ["Enter correct username and password and try again."])
        token = secrets.token_hex(24)
        with self.lock:
            self.sessions[token] = time.time()
        return 200, {"partnerData": {}, "sessionID": token}

    def logout(self, token):
        with self.lock:
            self.sessions.pop(token, None)
        return 204, None

    def authenticated(self, token):
        with self.lock:
            if token in self.sessions:
                self.sessions[token] = time.time()
                return True
        return False

    def collection(self, path, params, items, base_path):
        members = list(items)
        for expr in params.get("filter", []):
            members = apply_filter(members, expr)
            if members is None:
                return error(400, "INVALID_ARGUMENT", "The filter expression is not valid.", expr[:120], ["Correct the filter syntax."])
        for q in params.get("query", []):
            members = apply_filter(members, q)
            if members is None:
                return error(400, "INVALID_ARGUMENT", "The query expression is not valid.", None, [])
        for expr in params.get("sort", [])[:1]:
            for term in reversed(expr.split(",")):
                field, _, direction = term.partition(":")
                direction = direction or "asc"
                if direction.lower() not in ("asc", "desc", "ascending", "descending"):
                    return error(400, "INVALID_ARGUMENT", "The sort expression is not valid.", term, [])
                members.sort(key=lambda r: (at(r, field) is None, str(at(r, field))), reverse=direction.lower().startswith("desc"))
        try:
            start = int(params.get("start", ["0"])[0])
            count = int(params.get("count", [str(DEFAULT_PAGE)])[0])
        except ValueError:
            return error(400, "INVALID_ARGUMENT", "start and count must be integers.", None, [])
        if start < 0:
            return error(400, "INVALID_ARGUMENT", "start must be non-negative.", None, [])
        # A real appliance treats count=-1 as "all" only on some releases; here it means one default page,
        # which is exactly why clients must follow nextPageUri instead of trusting count.
        if count <= 0 or count > 1000:
            count = DEFAULT_PAGE
        page = members[start : start + count]
        if "fields" in params:
            wanted = [f for f in params["fields"][0].split(",") if f]
            page = [project(r, wanted) for r in page]
        total = len(members)
        qs = {k: v for k, v in params.items() if k not in ("start", "count")}

        def link(s):
            pairs = [("start", str(s)), ("count", str(count))] + [(k, x) for k, vs in qs.items() for x in vs]
            return base_path + "?" + urlencode(pairs)

        category = base_path.rsplit("/", 1)[-1]
        body = {
            "type": f"{category}-list",
            "category": category,
            "members": page,
            "start": start,
            "count": len(page),
            "total": total,
            "prevPageUri": link(max(0, start - count)) if start > 0 else None,
            "nextPageUri": link(start + count) if start + count < total else None,
            "uri": link(start),
            "created": iso(self.now),
            "modified": iso(self.now),
            "eTag": None,
        }
        return 200, body

    def utilization(self, server, params):
        fields = [f for f in params.get("fields", ["AveragePower,PeakPower,AmbientTemperature,CpuUtilization"])[0].split(",") if f]
        view = params.get("view", ["native"])[0]
        step = {"native": 5 * 60, "hour": 3600, "day": 86400}.get(view)
        if step is None:
            return error(400, "INVALID_ARGUMENT", "Unsupported view.", view, ["Use native, hour or day."])
        start, end = self.now - timedelta(hours=24), self.now
        for f in params.get("filter", []):
            for part in f.strip('"').split(","):
                k, _, v = part.partition("=")
                try:
                    dt = datetime.strptime(v.strip("'"), "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
                except ValueError:
                    try:
                        dt = datetime.strptime(v.strip("'"), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
                    except ValueError:
                        return error(400, "INVALID_ARGUMENT", "Invalid date filter.", part, [])
                if k.strip() == "startDate":
                    start = dt
                elif k.strip() == "endDate":
                    end = dt
                else:
                    return error(400, "INVALID_ARGUMENT", "Unsupported utilization filter.", k, [])
        oldest = self.now - timedelta(days=30)
        start = max(start, oldest)
        rng = random.Random(server["uri"])
        base_power = 320 + rng.randint(0, 120)
        hot = server["status"] == "Critical"
        metrics = []
        known = {"AveragePower", "PeakPower", "AmbientTemperature", "CpuUtilization", "CpuAverageFreq", "PowerCap"}
        for name in fields:
            if name not in known:
                return error(400, "INVALID_ARGUMENT", "Unsupported metric.", name, [])
            samples = []
            t = int(start.timestamp()) // step * step
            while t <= int(end.timestamp()):
                hour = (t // 3600) % 24
                load = 0.35 + 0.4 * (1 if 8 <= hour <= 18 else 0.3) + rng.random() * 0.1
                if server["powerState"] == "Off":
                    value = 0 if name != "AmbientTemperature" else 21 + rng.random()
                elif name == "AveragePower":
                    value = round(base_power * (0.6 + load), 1)
                elif name == "PeakPower":
                    value = round(base_power * (0.8 + load), 1)
                elif name == "AmbientTemperature":
                    value = round((29 if hot else 22) + 3 * load + rng.random(), 1)
                elif name == "CpuUtilization":
                    value = round(min(100, 100 * load + (25 if hot else 0)), 1)
                elif name == "CpuAverageFreq":
                    value = round(server["processorSpeedMhz"] * (0.9 + 0.1 * load), 0)
                else:
                    value = None
                samples.append([t * 1000, value])
                t += step
            metrics.append({"metricName": name, "metricSamples": samples, "metricCapacity": 800 if name in ("AveragePower", "PeakPower", "PowerCap") else None})
        return 200, {
            "type": "UtilizationData",
            "uri": server["uri"] + "/utilization",
            "category": "server-hardware",
            "metricList": metrics,
            "resolution": step * 1000,
            "sliceStartTime": iso(start),
            "sliceEndTime": iso(end),
            "newestSampleTime": iso(self.now),
            "oldestSampleTime": iso(oldest),
            "refreshTaskUri": None,
            "isFresh": True,
            "created": iso(self.now),
            "modified": iso(self.now),
            "eTag": None,
        }

    def firmware(self, server):
        gen = server["generation"]
        return {
            "uri": server["uri"] + "/firmware",
            "type": "server-hardware-firmware-1",
            "category": "server-hardware",
            "serverHardwareUri": server["uri"],
            "serverName": server["name"],
            "serverModel": server["model"],
            "state": "Supported",
            "created": server["created"],
            "modified": server["modified"],
            "eTag": server["eTag"],
            "components": [
                {"componentKey": "SystemRom", "componentName": "System ROM", "componentVersion": server["romVersion"], "componentLocation": "System Board"},
                {"componentKey": "iLO", "componentName": server["mpModel"], "componentVersion": server["mpFirmwareVersion"], "componentLocation": "System Board"},
                {"componentKey": "Mezz3", "componentName": "HPE Synergy 4820C 10/25Gb CNA", "componentVersion": "2024.03.01" if gen == "Gen11" or server["status"] == "OK" else "2023.09.01", "componentLocation": "Mezz 3"},
                {"componentKey": "IP", "componentName": "Intelligent Provisioning", "componentVersion": server["intelligentProvisioningVersion"], "componentLocation": "System Board"},
            ],
        }

    def dispatch(self, method, path, params, headers, body, token):
        est = self.estate
        if path == "/rest/version":
            if method != "GET":
                return self.not_allowed(method)
            return 200, {"currentVersion": CURRENT_VERSION, "minimumVersion": MINIMUM_VERSION}
        version = headers.get("X-API-Version")
        if version is None:
            return error(412, "UNSUPPORTED_API_VERSION", "The X-API-Version header is required.", "No X-API-Version header was supplied with the request.", [f"Include an X-API-Version header between {MINIMUM_VERSION} and {CURRENT_VERSION}."])
        try:
            v = int(version)
        except ValueError:
            v = -1
        if not MINIMUM_VERSION <= v <= CURRENT_VERSION:
            return error(412, "UNSUPPORTED_API_VERSION", f"The API version {version} is not supported.", f"Supported versions are {MINIMUM_VERSION} to {CURRENT_VERSION}.", ["Use a supported X-API-Version."])
        if path == "/rest/login-sessions":
            if method == "POST":
                return self.login(body)
            if method == "DELETE":
                if not token or not self.authenticated(token):
                    return error(401, "AUTHN_AUTH_TOKEN_INVALID", "The session token is not valid.", None, ["Log in again."])
                return self.logout(token)
            return self.not_allowed(method)
        if method != "GET":
            return self.not_allowed(method)
        if not token:
            return error(401, "AUTHN_AUTH_TOKEN_INVALID", "No authentication token was supplied.", "Provide the session token in the Auth header.", ["Log in and send the sessionID in the Auth header."])
        if not self.authenticated(token):
            return error(401, "AUTHN_AUTH_TOKEN_INVALID", "The session token is not valid or has expired.", None, ["Log in again."])
        if path == "/rest/appliance/nodeinfo/version":
            return 200, {"type": "NodeVersionDto4", "softwareVersion": SOFTWARE_VERSION, "major": "9", "minor": "00", "build": "0417420", "serialNumber": "CMP11", "modelNumber": "HPE Synergy Composer2", "family": "Synergy", "platformType": "hardware", "compatibility": "9.00", "uri": "/rest/appliance/nodeinfo/version"}
        if path == "/rest/appliance/nodeinfo/status":
            return 200, {"type": "NodeStatusDto", "status": "OK", "uptime": 3_456_000, "haStatus": "Active", "uri": "/rest/appliance/nodeinfo/status"}
        if path == "/rest/appliance/health-status":
            return 200, {
                "type": "ApplianceHealthStatus",
                "category": "appliance",
                "uri": "/rest/appliance/health-status",
                "members": [
                    {"resourceName": "CPU", "status": "OK", "statusMessage": "CPU resources are sufficient.", "available": "8", "capacity": "8"},
                    {"resourceName": "Memory", "status": "OK", "statusMessage": "Memory resources are sufficient.", "available": "11 GB", "capacity": "32 GB"},
                    {"resourceName": "Disk Space", "status": "Warning", "statusMessage": "Disk space usage is above 80 percent.", "available": "41 GB", "capacity": "275 GB"},
                ],
            }
        if path == "/rest/index/resources":
            items = est["index"]
            cats = params.get("category", [])
            if cats:
                items = [r for r in items if r["category"] in cats]
            if "userQuery" in params:
                needle = params["userQuery"][0].lower()
                items = [r for r in items if needle in json.dumps(r).lower()]
            return self.collection(path, params, items, path)
        m = re.fullmatch(r"/rest/server-hardware/\*/firmware", path)
        if m:
            return self.collection(path, params, [self.firmware(s) for s in est["server-hardware"]], path)
        m = re.fullmatch(r"/rest/server-hardware/([^/]+)/(firmware|utilization|environmentalConfiguration)", path)
        if m:
            server = next((s for s in est["server-hardware"] if s["uri"].endswith("/" + m.group(1))), None)
            if not server:
                return error(404, "RESOURCE_NOT_FOUND", "The requested resource could not be found.", path, ["Verify the resource URI."])
            if m.group(2) == "firmware":
                return 200, self.firmware(server)
            if m.group(2) == "utilization":
                return self.utilization(server, params)
            return 200, {"type": "EnvironmentalConfiguration", "powerHistorySupported": True, "thermalHistorySupported": True, "utilizationHistorySupported": True, "capHistorySupported": True, "calibratedMaxPower": 800, "idleMaxPower": 120, "powerCapType": "Manual", "uri": path}
        m = re.fullmatch(r"/rest/([a-z-]+)(?:/([^/]+))?", path)
        if m and m.group(1) in est and m.group(1) != "index":
            items = est[m.group(1)]
            if m.group(2) is None:
                return self.collection(path, params, items, path)
            one = next((r for r in items if r["uri"] == path), None)
            if one is None:
                return error(404, "RESOURCE_NOT_FOUND", "The requested resource could not be found.", path, ["Verify the resource URI."])
            return 200, one
        return error(404, "RESOURCE_NOT_FOUND", "The requested resource could not be found.", path, ["Verify the resource URI."])

    @staticmethod
    def not_allowed(method):
        return error(
            405,
            "HTTP_METHOD_NOT_ALLOWED",
            "The request method is not supported or not applicable for this type of resource.",
            f"The request method {method} is not supported or not applicable for the requested resource.",
            ["Allowed methods include GET. Refer to the API specification to determine the supported HTTP request methods for the resource type."],
        )


def make_handler(sim, verbose=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OneView-Simulator/9.00"

        def log_message(self, fmt, *args):
            if verbose:
                super().log_message(fmt, *args)

        def reply(self, status, body):
            raw = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            if body is not None:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def handle_any(self):
            u = urlsplit(self.path)
            params = {}
            for k, v in parse_qsl(u.query, keep_blank_values=True):
                params.setdefault(k, []).append(unquote(v))
            length = int(self.headers.get("Content-Length") or 0)
            body = None
            if length:
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {}
            token = self.headers.get("Auth")
            status, payload = sim.dispatch(self.command, u.path, params, self.headers, body, token)
            self.reply(status, payload)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_any

    return Handler


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=18093, help="loopback port to listen on (default 18093)")
    p.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1; keep it on loopback)")
    p.add_argument("--verbose", action="store_true", help="log every request")
    a = p.parse_args()
    if not os.environ.get("ONEVIEW_USERNAME") or not os.environ.get("ONEVIEW_PASSWORD"):
        p.error("Set ONEVIEW_USERNAME and ONEVIEW_PASSWORD (local throwaway values).")
    sim = Simulator()
    print(f"Synthetic OneView API listening on http://{a.host}:{a.port} (API {MINIMUM_VERSION}-{CURRENT_VERSION}, appliance {SOFTWARE_VERSION})", flush=True)
    ThreadingHTTPServer((a.host, a.port), make_handler(sim, a.verbose)).serve_forever()


if __name__ == "__main__":
    main()
