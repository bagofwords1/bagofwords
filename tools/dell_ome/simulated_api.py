#!/usr/bin/env python3
"""Synthetic Dell OpenManage Enterprise 4.1 REST API for local verification.

This is NOT OpenManage Enterprise. It is a stdlib HTTP server that reproduces
the wire contract the BOW connector depends on (paths, verbs, headers, session
handshake, OData envelopes, $top/$skip/$filter/$orderby, error bodies) over a
deterministic synthetic estate (~40 devices, groups, alerts, warranties,
firmware baselines and compliance, jobs, audit logs, templates, MCM domains).

Binds loopback only. Credentials come from OME_USERNAME / OME_PASSWORD. Only
session creation (POST) and session deletion (DELETE) mutate anything; every
other mutating verb is refused. See tools/dell_ome/README.md and SOURCES.md.
"""

import argparse
import base64
import json
import os
import random
import re
import threading
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, quote, unquote, urlsplit

DEFAULT_PAGE = 50  # OME collections return 50 records when $top is absent (see SOURCES.md).

GENERAL = "A general error has occurred. See ExtendedInfo for more information."
CONTENT_TYPE = "application/json; odata.metadata=minimal; charset=utf-8"


def ts(dt):
    """OME appliance timestamp text, e.g. 2023-06-07 09:33:07.161 (UTC, no zone)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def error_body(message_id, message, resolution, severity="Critical", related=None):
    return {
        "error": {
            "code": "Base.1.0.GeneralError",
            "message": GENERAL,
            "@Message.ExtendedInfo": [
                {
                    "MessageId": message_id,
                    "RelatedProperties": related or [],
                    "Message": message,
                    "MessageArgs": [],
                    "Severity": severity,
                    "Resolution": resolution,
                }
            ],
        }
    }


ERR_GENERAL = error_body("CGEN1008", "Unable to process the request because an error occurred.",
                         "Retry the operation. If the issue persists, contact your system administrator.")
ERR_UNAUTHORIZED = error_body("CGEN1001", "Unable to complete the operation because the user is not authorized.",
                              "Log in with a valid user account or X-Auth-Token and retry the operation.")
ERR_LOGIN = error_body("CUSR1002", "Unable to complete the operation because an invalid username and/or password is entered, and therefore authentication failed.",
                       "Enter valid user name and password and retry the operation.", "Warning")
ERR_SESSION_CAP = error_body("CUSR1001", "Unable to create a session because the maximum number of sessions has been reached.",
                             "Wait for an existing session to expire or close an existing session, then retry the operation.")
ERR_NOT_FOUND = error_body("CGEN1004", "Unable to complete the operation because the requested URI is invalid.",
                           "Enter a valid URI and retry the operation.")
ERR_PRIVILEGE = error_body("CGEN1005", "Unable to complete the operation because the user does not have the required privilege.",
                           "Log in with an account that has the required privilege and retry the operation.")


def err_bad_request(message, related=None):
    return error_body("CGEN1002", message, "Correct the request and retry the operation.", related=related)


# ---------------------------------------------------------------------------
# Synthetic estate
# ---------------------------------------------------------------------------


class Estate:
    def __init__(self, seed=41):
        self.rng = random.Random(seed)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.build()

    # -- helpers ------------------------------------------------------------
    def ago(self, **kw):
        return self.now - timedelta(**kw)

    def tag(self, i):
        letters = "ABCDEFGHJKLMNPQRSTVWXYZ"
        r = random.Random(1000 + i)
        return "".join(r.choice(letters + "0123456789") for _ in range(7))

    def build(self):
        self.info = {
            "Name": "OpenManage Enterprise",
            "Description": "OpenManage Enterprise (synthetic appliance)",
            "Vendor": "Dell",
            "Branding": "OME",
            "Version": "4.1.0",
            "BuildNumber": "2024.03.18.1",
            "BuildDate": "2024-03-18",
            "Guid": "7d1b2e2c-9f4a-4a6e-9e73-5b7c2c0f0a41",
            "OperationStatus": "AVAILABLE",
        }
        self.network = {
            "Delay": 0,
            "DnsConfiguration": {"DnsDomainName": "lab.example", "DnsName": "ome-sim", "RegisterWithDNS": True,
                                 "UseDHCPForDNSDomainName": False},
            "EnableNIC": True,
            "InterfaceName": "ens160",
            "PrimaryInterface": True,
            "Ipv4Configuration": {"Enable": True, "EnableDHCP": False, "StaticAlternateDNSServer": "192.0.2.11",
                                  "StaticGateway": "192.0.2.1", "StaticIPAddress": "192.0.2.20",
                                  "StaticPreferredDNSServer": "192.0.2.10", "StaticSubnetMask": "255.255.255.0",
                                  "UseDHCPForDNSServerNames": False},
            "Ipv6Configuration": {"Enable": False, "EnableAutoConfiguration": True, "StaticAlternateDNSServer": "",
                                  "StaticGateway": "", "StaticIPAddress": "", "StaticPreferredDNSServer": "",
                                  "StaticPrefixLength": 0, "UseDHCPForDNSServerNames": True},
            "ManagementVLAN": {"EnableVLAN": False, "Id": 1},
        }
        self.device_types = [
            {"DeviceType": 1000, "Name": "SERVER", "Description": "Server Device"},
            {"DeviceType": 2000, "Name": "CHASSIS", "Description": "Chassis Device"},
            {"DeviceType": 3000, "Name": "STORAGE", "Description": "Storage Device"},
            {"DeviceType": 4000, "Name": "NETWORK_IOM", "Description": "NETWORK IO Module Device"},
            {"DeviceType": 8000, "Name": "STORAGE_IOM", "Description": "Storage IOM Device"},
            {"DeviceType": 9000, "Name": "NETWORK_CONTROLLER", "Description": "Network Controller"},
        ]
        self.build_devices()
        self.build_groups()
        self.build_inventory()
        self.build_alerts()
        self.build_warranties()
        self.build_firmware()
        self.build_jobs()
        self.build_audit()
        self.build_templates()
        self.build_domains()

    # -- devices --------------------------------------------------------------
    def build_devices(self):
        rng = self.rng
        self.devices = []
        models = ["PowerEdge R650"] * 14 + ["PowerEdge R750"] * 16 + ["PowerEdge MX750c"] * 4
        # Health plan: index -> status. 5 critical, 6 warning, 2 unknown (offline), rest normal.
        plan = {1: 4000, 7: 4000, 12: 4000, 19: 4000, 27: 4000, 3: 3000, 9: 3000, 15: 3000, 22: 3000, 30: 3000,
                33: 3000, 5: 2000, 24: 2000}
        offline = {5, 24, 29}
        chassis_ids = [10101, 10102]
        for i, model in enumerate(models):
            dev_id = 10001 + i
            stag = self.tag(i)
            status = plan.get(i, 1000)
            modular = model.endswith("MX750c")
            ip = f"192.0.2.{100 + i}"
            name = f"{'mx' if modular else 'srv'}-{i + 1:02d}.lab.example"
            self.devices.append({
                "Id": dev_id,
                "Type": 1000,
                "Identifier": stag,
                "DeviceServiceTag": stag,
                "ChassisServiceTag": self.tag(100 + (i % 2)) if modular else None,
                "Model": model,
                "PowerState": 18 if i in (12, 29) else 17,
                "ManagedState": 3000,
                "Status": status,
                "ConnectionState": i not in offline,
                "AssetTag": f"AST-{dev_id}" if i % 3 == 0 else None,
                "SystemId": 2360 if "R650" in model else (2400 if "R750" in model else 2381),
                "DeviceName": name,
                "LastInventoryTime": ts(self.ago(hours=rng.randint(1, 30))),
                "LastStatusTime": ts(self.ago(minutes=rng.randint(1, 240))),
                "DeviceSubscription": None,
                "DeviceCapabilities": [1, 2, 3, 4, 5, 6, 7, 8, 9, 41, 1101] if not modular else [1, 2, 3, 4, 5, 6, 9, 41],
                "SlotConfiguration": {"ChassisName": f"MX7000-{(i % 2) + 1}", "SlotNumber": str((i % 8) + 1),
                                      "SlotType": "2000"} if modular else {},
                "DeviceManagement": [{
                    "ManagementId": 50000 + i,
                    "NetworkAddress": ip,
                    "MacAddress": f"50:9a:4c:{i:02x}:aa:{(i * 7) % 256:02x}",
                    "ManagementType": 2,
                    "InstrumentationName": name.split(".")[0].upper(),
                    "DnsName": f"idrac-{stag.lower()}.lab.example",
                    "ManagementProfile": [{
                        "ManagementProfileId": 50000 + i,
                        "ProfileId": "WSMAN_OOB",
                        "ManagementId": 50000 + i,
                        "AgentName": "iDRAC",
                        "Version": "7.00.60.00" if i % 2 else "6.10.30.00",
                        "ManagementURL": f"https://{ip}:443",
                        "HasCreds": 0,
                        "Status": 1000 if i not in offline else 4000,
                        "StatusDateTime": ts(self.ago(minutes=rng.randint(1, 240))),
                    }],
                }],
                "_chassis_id": chassis_ids[i % 2] if modular else None,
            })
        for n, cid in enumerate(chassis_ids):
            stag = self.tag(100 + n)
            self.devices.append({
                "Id": cid, "Type": 2000, "Identifier": stag, "DeviceServiceTag": stag, "ChassisServiceTag": None,
                "Model": "PowerEdge MX7000", "PowerState": 17, "ManagedState": 3000,
                "Status": 3000 if n == 1 else 1000, "ConnectionState": True, "AssetTag": None, "SystemId": 2031,
                "DeviceName": f"MX7000-{n + 1}", "LastInventoryTime": ts(self.ago(hours=2 + n)),
                "LastStatusTime": ts(self.ago(minutes=5 + n)), "DeviceSubscription": None,
                "DeviceCapabilities": [1, 2, 3, 4, 5, 9, 41, 1101], "SlotConfiguration": {},
                "DeviceManagement": [{"ManagementId": 60000 + n, "NetworkAddress": f"192.0.2.{50 + n}",
                                      "MacAddress": f"d4:04:e6:0{n}:11:22", "ManagementType": 2,
                                      "InstrumentationName": f"MX-{stag}", "DnsName": f"mx7000-{n + 1}.lab.example",
                                      "ManagementProfile": [{"ManagementProfileId": 60000 + n, "ProfileId": "MSM_OOB",
                                                             "ManagementId": 60000 + n, "AgentName": "OME-M",
                                                             "Version": "2.10.10", "ManagementURL": f"https://192.0.2.{50 + n}:443",
                                                             "HasCreds": 0, "Status": 1000,
                                                             "StatusDateTime": ts(self.ago(minutes=5))}]}],
                "_chassis_id": None,
            })
        for n in range(2):
            sid = 10201 + n
            stag = self.tag(200 + n)
            self.devices.append({
                "Id": sid, "Type": 4000, "Identifier": stag, "DeviceServiceTag": stag,
                "ChassisServiceTag": self.tag(100 + n), "Model": "MX9116n Fabric Engine", "PowerState": 17,
                "ManagedState": 3000, "Status": 4000 if n == 1 else 1000, "ConnectionState": True, "AssetTag": None,
                "SystemId": 0, "DeviceName": f"MX9116n-{n + 1}", "LastInventoryTime": ts(self.ago(hours=3)),
                "LastStatusTime": ts(self.ago(minutes=9)), "DeviceSubscription": None,
                "DeviceCapabilities": [1, 2, 3, 41], "SlotConfiguration": {"ChassisName": f"MX7000-{n + 1}", "SlotNumber": "A1", "SlotType": "4000"},
                "DeviceManagement": [{"ManagementId": 61000 + n, "NetworkAddress": f"192.0.2.{60 + n}",
                                      "MacAddress": f"20:04:0f:0{n}:33:44", "ManagementType": 2,
                                      "InstrumentationName": f"MX9116n-{n + 1}", "DnsName": "",
                                      "ManagementProfile": [{"ManagementProfileId": 61000 + n, "ProfileId": "OS10_OOB",
                                                             "ManagementId": 61000 + n, "AgentName": "OS10",
                                                             "Version": "10.5.4.2", "ManagementURL": f"https://192.0.2.{60 + n}:443",
                                                             "HasCreds": 0, "Status": 1000, "StatusDateTime": ts(self.ago(minutes=9))}]}],
                "_chassis_id": None,
            })
        self.by_id = {d["Id"]: d for d in self.devices}

    def public_device(self, d):
        out = {k: v for k, v in d.items() if not k.startswith("_")}
        out["HardwareLogs@odata.navigationLink"] = f"/api/DeviceService/Devices({d['Id']})/HardwareLogs"
        out["InventoryDetails@odata.navigationLink"] = f"/api/DeviceService/Devices({d['Id']})/InventoryDetails"
        out["SubSystemHealth@odata.navigationLink"] = f"/api/DeviceService/Devices({d['Id']})/SubSystemHealth"
        return out

    def device_summary(self):
        rows = []
        for t in self.device_types:
            devs = [d for d in self.devices if d["Type"] == t["DeviceType"]]
            if not devs:
                continue
            rows.append({
                "DeviceType": t["DeviceType"], "TotalCount": len(devs),
                "NormalCount": sum(d["Status"] == 1000 for d in devs),
                "WarningCount": sum(d["Status"] == 3000 for d in devs),
                "CriticalCount": sum(d["Status"] == 4000 for d in devs),
                "UnknownCount": sum(d["Status"] == 2000 for d in devs),
            })
        return rows

    # -- groups ---------------------------------------------------------------
    def build_groups(self):
        servers = [d for d in self.devices if d["Type"] == 1000]
        prod = [d["Id"] for d in servers[:8]]
        lab = [d["Id"] for d in servers[20:28]]
        r750 = [d["Id"] for d in servers if d["Model"] == "PowerEdge R750"]

        def grp(gid, name, desc, type_id, membership, parent, members, visible=True):
            return {
                "Id": gid, "Name": name, "Description": desc,
                "GlobalStatus": max((self.by_id[m]["Status"] for m in members), default=1000) if members else 1000,
                "ParentId": parent, "CreationTime": ts(self.ago(days=200)), "UpdatedTime": ts(self.ago(days=3)),
                "CreatedBy": "system" if type_id == 2000 else "admin", "UpdatedBy": "system" if type_id == 2000 else "ops-user",
                "Visible": visible, "DefinitionId": 0, "DefinitionDescription": None,
                "TypeId": type_id, "MembershipTypeId": membership, "HasAttributes": False, "_members": members,
            }
        self.groups = [
            grp(500, "All Devices", "All devices managed by the appliance", 2000, 24, 0, [d["Id"] for d in self.devices]),
            grp(1000, "Servers", "All server devices", 2000, 24, 500, [d["Id"] for d in servers]),
            grp(1010, "Chassis", "All chassis devices", 2000, 24, 500, [d["Id"] for d in self.devices if d["Type"] == 2000]),
            grp(1020, "Network Devices", "All network devices", 2000, 24, 500, [d["Id"] for d in self.devices if d["Type"] == 4000]),
            grp(10101, "Production DB", "Production database hosts (static)", 3000, 12, 0, prod),
            grp(10102, "Lab Rack 12", "Lab rack 12 (static)", 3000, 12, 0, lab),
            grp(10103, "All R750 Servers", "Query group: Model eq PowerEdge R750", 3000, 24, 0, r750),
        ]
        self.groups_by_id = {g["Id"]: g for g in self.groups}

    def public_group(self, g):
        out = {k: v for k, v in g.items() if not k.startswith("_")}
        out["Devices@odata.navigationLink"] = f"/api/GroupService/Groups({g['Id']})/Devices"
        out["AllLeafDevices@odata.navigationLink"] = f"/api/GroupService/Groups({g['Id']})/AllLeafDevices"
        out["SubGroups@odata.navigationLink"] = f"/api/GroupService/Groups({g['Id']})/SubGroups"
        return out

    # -- inventory / telemetry ---------------------------------------------
    def build_inventory(self):
        self.inventory = {}
        self.hwlogs = {}
        self.power = {}
        self.temperature = {}
        self.uptime = {}
        for d in self.devices:
            did = d["Id"]
            if d["Type"] != 1000:
                self.inventory[did] = [
                    {"InventoryType": "deviceManagement", "InventoryInfo": [{
                        "ManagementId": d["DeviceManagement"][0]["ManagementId"], "Device": did,
                        "IpAddress": d["DeviceManagement"][0]["NetworkAddress"],
                        "MacAddress": d["DeviceManagement"][0]["MacAddress"], "ManagementType": 2,
                        "InstrumentationName": d["DeviceManagement"][0]["InstrumentationName"],
                        "DnsName": d["DeviceManagement"][0]["DnsName"], "EndPointAgents": []}]},
                    {"InventoryType": "deviceSoftware", "InventoryInfo": [{
                        "Version": d["DeviceManagement"][0]["ManagementProfile"][0]["Version"], "InstallationDate": "2024-01-10T00:00:00Z",
                        "Status": "Installed", "SoftwareType": "FRMW", "VendorId": "DELL", "SubDeviceId": "0", "SubVendorId": "0",
                        "ComponentId": "25227", "PciDeviceId": "0", "DeviceDescription": "Management Module Firmware",
                        "InstanceId": "DCIM:INSTALLED#MM.Embedded.1"}]},
                ]
                self.hwlogs[did] = []
                continue
            critical = d["Status"] == 4000
            warning = d["Status"] == 3000
            bios = "1.14.2" if did % 3 else "1.12.1"
            idrac = d["DeviceManagement"][0]["ManagementProfile"][0]["Version"]
            cpus = [{
                "Id": 1000 + n, "Family": "Intel(R) Xeon(R)", "MaxSpeed": 3600, "CurrentSpeed": 2300,
                "SlotNumber": f"CPU.Socket.{n + 1}", "Status": 1000, "NumberOfCores": 28, "NumberOfEnabledCores": 28,
                "BrandName": "Intel", "ModelName": "Intel(R) Xeon(R) Gold 6330 CPU @ 2.00GHz",
                "InstanceId": f"CPU.Socket.{n + 1}", "Voltage": "1.8"} for n in range(2)]
            dimms = []
            for n in range(16):
                dimm_status = 3000 if (warning and n == 5) else 1000
                dimms.append({
                    "Id": 2000 + n, "Name": f"DIMM.Socket.{'AB'[n % 2]}{n // 2 + 1}", "BankName": f"{'AB'[n % 2]}",
                    "Size": 32768.0, "Status": dimm_status, "Manufacturer": "Samsung", "PartNumber": "M393A4K40DB3-CWE",
                    "SerialNumber": f"{did}{n:04d}", "TypeDetails": "DDR4 DIMM", "ManufacturerDate": "Mon Jan 10 07:00:00 2022 UTC",
                    "Speed": 3200, "CurrentOperatingSpeed": 2933, "Rank": "2", "InstanceId": f"DIMM.Socket.{'AB'[n % 2]}{n // 2 + 1}",
                    "DeviceDescription": f"DIMM {'AB'[n % 2]}{n // 2 + 1}"})
            software = [
                {"Version": bios, "InstallationDate": "2024-02-02T00:00:00Z", "Status": "Installed", "SoftwareType": "FRMW",
                 "VendorId": "DELL", "SubDeviceId": "0", "SubVendorId": "0", "ComponentId": "159", "PciDeviceId": "0",
                 "DeviceDescription": "BIOS", "InstanceId": "DCIM:INSTALLED#741__BIOS.Setup.1-1"},
                {"Version": idrac, "InstallationDate": "2024-02-02T00:00:00Z", "Status": "Installed", "SoftwareType": "FRMW",
                 "VendorId": "DELL", "SubDeviceId": "0", "SubVendorId": "0", "ComponentId": "25227", "PciDeviceId": "0",
                 "DeviceDescription": "Integrated Dell Remote Access Controller", "InstanceId": "DCIM:INSTALLED#iDRAC.Embedded.1-1#IDRACinfo"},
                {"Version": "22.31.6" if did % 2 else "21.85.21", "InstallationDate": "2023-11-11T00:00:00Z", "Status": "Installed",
                 "SoftwareType": "FRMW", "VendorId": "15B3", "SubDeviceId": "0", "SubVendorId": "15B3", "ComponentId": "104815",
                 "PciDeviceId": "1017", "DeviceDescription": "Mellanox ConnectX-5 EN 25GbE Dual-port", "InstanceId": "DCIM:INSTALLED#701__NIC.Slot.1-1-1"},
                {"Version": "52.26.0-5179" if did % 5 else "52.21.0-4606", "InstallationDate": "2023-11-11T00:00:00Z", "Status": "Installed",
                 "SoftwareType": "FRMW", "VendorId": "1000", "SubDeviceId": "0", "SubVendorId": "1028", "ComponentId": "103560",
                 "PciDeviceId": "10E2", "DeviceDescription": "PERC H755 Front", "InstanceId": "DCIM:INSTALLED#304_C_RAID.SL.3-1"},
            ]
            psus = [{
                "Id": 3000 + n, "Name": f"PS{n + 1} Status", "Status": 4000 if (critical and n == 1) else 1000,
                "State": "Present", "Location": f"PSU.Slot.{n + 1}", "OutputWatts": 1400, "RedundancyState": "Lost" if critical else "Full",
                "Model": "PWR SPLY,1400W,RDNT,DELTA", "SerialNumber": f"CN{did}{n}", "FirmwareVersion": "00.1B.53",
                "InstanceId": f"PSU.Slot.{n + 1}", "InputVoltage": 0 if (critical and n == 1) else 230} for n in range(2)]
            disks = [{
                "Id": 4000 + n, "DiskNumber": f"Disk {n}", "Name": f"Solid State Disk 0:1:{n}", "Status": 3000 if (warning and n == 2) else 1000,
                "State": "Online", "Size": "1.75 TB", "BusType": "SAS", "MediaType": "Solid State Drive", "Manufacturer": "SAMSUNG",
                "Model": "MZILT1T9HBJR0D3", "SerialNumber": f"S5{did}{n:02d}", "Revision": "GXC3", "RemainingReadWriteEndurance": "98",
                "PredictiveFailureState": "Smart Alert Present" if (warning and n == 2) else "No",
                "InstanceId": f"Disk.Bay.{n}:Enclosure.Internal.0-1:RAID.SL.3-1"} for n in range(4)]
            nics = [{
                "NicId": f"NIC.Slot.1-{p}-1", "VendorName": "Mellanox Technologies", "Ports": [{
                    "Id": f"NIC.Slot.1-{p}-1", "ProductName": "Mellanox ConnectX-5 EN 25GbE", "LinkStatus": "Up" if p == 1 else "Down",
                    "LinkSpeed": 25000, "Partitions": [{"Fqdd": f"NIC.Slot.1-{p}-1", "CurrentMacAddress": f"b8:ce:f6:{did % 256:02x}:0{p}:01"}]}]}
                for p in (1, 2)]
            self.inventory[did] = [
                {"InventoryType": "serverDeviceCards", "InventoryInfo": [{"Id": 5000, "SlotNumber": "1", "Manufacturer": "Mellanox", "Description": "ConnectX-5", "DatabusWidth": "x16", "SlotLength": "Half", "SlotType": "PCIe", "Status": 1000}]},
                {"InventoryType": "serverProcessors", "InventoryInfo": cpus},
                {"InventoryType": "serverNetworkInterfaces", "InventoryInfo": nics},
                {"InventoryType": "serverMemoryDevices", "InventoryInfo": dimms},
                {"InventoryType": "serverPowerSupplies", "InventoryInfo": psus},
                {"InventoryType": "serverArrayDisks", "InventoryInfo": disks},
                {"InventoryType": "deviceSoftware", "InventoryInfo": software},
                {"InventoryType": "deviceManagement", "InventoryInfo": [{
                    "ManagementId": d["DeviceManagement"][0]["ManagementId"], "Device": did,
                    "IpAddress": d["DeviceManagement"][0]["NetworkAddress"], "MacAddress": d["DeviceManagement"][0]["MacAddress"],
                    "ManagementType": 2, "InstrumentationName": d["DeviceManagement"][0]["InstrumentationName"],
                    "DnsName": d["DeviceManagement"][0]["DnsName"], "EndPointAgents": [{"ManagementProfileId": d["DeviceManagement"][0]["ManagementId"], "ProfileId": "WSMAN_OOB", "AgentName": "iDRAC", "Version": idrac, "ManagementURL": d["DeviceManagement"][0]["ManagementProfile"][0]["ManagementURL"], "HasCreds": 0, "Status": 1000, "StatusDateTime": ts(self.ago(minutes=10))}]}]},
            ]
            logs = []
            seq = 1
            for h in range(10, 0, -1):
                logs.append({"Id": did * 100 + seq, "LogSequenceNumber": seq, "LogSeverity": 2, "LogCategory": "System",
                             "LogTimeStamp": ts(self.ago(hours=h * 7)), "Message": "The system inventory collection completed.", "LogComment": ""})
                seq += 1
            if critical:
                logs.append({"Id": did * 100 + seq, "LogSequenceNumber": seq, "LogSeverity": 16, "LogCategory": "Power",
                             "LogTimeStamp": ts(self.ago(hours=5, minutes=did % 50)), "Message": "PSU0003: The power input for power supply 2 is lost.", "LogComment": ""})
                seq += 1
                logs.append({"Id": did * 100 + seq, "LogSequenceNumber": seq, "LogSeverity": 16, "LogCategory": "Power",
                             "LogTimeStamp": ts(self.ago(hours=5, minutes=(did % 50) - 1)), "Message": "RDU0012: Power supply redundancy is lost.", "LogComment": ""})
                seq += 1
            if warning:
                logs.append({"Id": did * 100 + seq, "LogSequenceNumber": seq, "LogSeverity": 8, "LogCategory": "Storage",
                             "LogTimeStamp": ts(self.ago(hours=20, minutes=did % 40)), "Message": "PDR16: Predictive failure reported for Solid State Disk 0:1:2 in disk drive bay 1.", "LogComment": ""})
                seq += 1
            self.hwlogs[did] = logs
            peak = 410 + (did % 90)
            inst = 0 if not d["ConnectionState"] else 260 + (did % 70)
            self.power[did] = {
                "instantaneousPower": str(inst), "peakPower": str(peak), "avgPower": str(inst - 20 if inst else 0), "minimumPower": "118",
                "powerCap": "1300", "peakHeadroom": str(1300 - peak), "instantaneousHeadroom": str(1300 - inst), "unit": "watt", "dateFormat": "CIM",
                "peakPowerTimeStamp": ts(self.ago(days=2, hours=did % 20)), "minimumPowerTimeStamp": ts(self.ago(days=6)),
                "systemEnergyConsumption": str(1200 + did % 500), "systemEnergyConsumptionTimeStamp": ts(self.ago(minutes=4)),
            }
            inlet = 37 if warning else 22 + (did % 5)
            self.temperature[did] = {
                "instantaneousTemperature": str(inlet), "peakTemperature": str(inlet + 6), "avgTemperature": str(inlet + 1),
                "instantaneousTemperatureUnit": "celsius", "peakTemperatureUnit": "celsius", "avgTemperatureUnit": "celsius",
                "peakTemperatureTimeStamp": ts(self.ago(days=1, hours=did % 12)), "avgTemperatureTimeStamp": ts(self.ago(minutes=4)),
                "startTime": ts(self.ago(days=7)), "dateFormat": "CIM",
            }
            days = (did % 60) + 1
            self.uptime[did] = {"systemUpTime": f"{days} days {did % 24:02d}:{did % 60:02d}:{(did * 7) % 60:02d}",
                                "startTime": ts(self.ago(days=days))}

    # -- alerts --------------------------------------------------------------
    def build_alerts(self):
        rng = self.rng
        servers = [d for d in self.devices if d["Type"] == 1000]
        templates = [
            (16, "PSU0003", 1, "System Health", 7700, "Power Supply", "The power input for power supply 2 is lost."),
            (16, "FAN0001", 1, "System Health", 50, "Fan", "Fan 3 RPM is less than the lower critical threshold."),
            (8, "TMP0100", 1, "System Health", 110, "Temperature", "The system inlet temperature is greater than the upper warning threshold."),
            (8, "PDR16", 2, "Storage", 90, "Devices", "Predictive failure reported for Solid State Disk 0:1:2 in disk drive bay 1."),
            (8, "MEM0701", 1, "System Health", 80, "Memory", "Correctable memory error rate exceeded for DIMM_B3."),
            (2, "LOG007", 4, "Audit", 41, "Auto System Reset", "The previous log entry was repeated 1 times."),
            (4, "PSU0001", 1, "System Health", 7700, "Power Supply", "Power supply 2 is restored."),
            (2, "USR0030", 4, "Audit", 35, "Users", "Successfully logged in using root, from 192.0.2.5 and GUI."),
            (16, "CPU0001", 1, "System Health", 65, "Processor", "CPU 1 has an internal error (IERR)."),
            (8, "NIC100", 1, "System Health", 100, "Network", "The NIC in Slot 1 Port 2 network link is down."),
        ]
        self.alerts = []
        for n in range(150):
            if n < 30:
                dev = rng.choice([d for d in servers if d["Status"] == 4000])
                sev_filter = (16, 4)
            elif n < 80:
                dev = rng.choice([d for d in servers if d["Status"] in (3000, 4000)])
                sev_filter = (8, 16)
            else:
                dev = rng.choice(servers)
                sev_filter = (2, 4, 8)
            sev, mid, cat_id, cat_name, sub_id, sub_name, msg = rng.choice([t for t in templates if t[0] in sev_filter])
            when = self.ago(minutes=rng.randint(5, 7 * 24 * 60))
            self.alerts.append({
                "Id": 30001 + n, "SeverityType": sev,
                "SeverityName": {1: "Unknown", 2: "Info", 4: "Normal", 8: "Warning", 16: "Critical"}[sev],
                "AlertDeviceId": dev["Id"], "AlertDeviceName": dev["DeviceName"], "AlertDeviceType": dev["Type"],
                "AlertDeviceIpAddress": dev["DeviceManagement"][0]["NetworkAddress"], "AlertDeviceIdentifier": dev["DeviceServiceTag"],
                "AlertDeviceMacAddress": dev["DeviceManagement"][0]["MacAddress"], "AlertDeviceAssetTag": dev["AssetTag"],
                "AlertDeviceGroup": 500, "AlertDeviceGroupName": "All Devices",
                "DefinitionId": 7000 + (hash(mid) % 500), "CatalogName": "iDRAC", "CategoryId": cat_id, "CategoryName": cat_name,
                "SubCategoryId": sub_id, "SubCategoryName": sub_name,
                "StatusType": 2000 if n % 9 == 0 else 1000, "StatusName": "Acknowledged" if n % 9 == 0 else "Not-Acknowledged",
                "TimeStamp": ts(when), "_when": when, "Message": msg, "EemiMessage": msg, "RecommendedAction": "Review the device hardware log.",
                "AlertMessageId": mid, "AlertVarBindDetails": "", "AlertMessageType": "Redfish", "MessageArgs": "",
                "UserName": "admin" if n % 9 == 0 else "",
            })
        self.alerts.sort(key=lambda a: a["_when"], reverse=True)

    # -- warranties -------------------------------------------------------------
    def build_warranties(self):
        rng = self.rng
        self.warranties = []
        wid = 70001
        for i, d in enumerate(self.devices):
            lines = 1 if i % 7 else 2
            for line in range(lines):
                if i in (1, 7, 24, 33):
                    end = self.ago(days=rng.randint(10, 400))
                elif i in (3, 9, 15, 22, 30):
                    end = self.now + timedelta(days=rng.randint(3, 29))
                else:
                    end = self.now + timedelta(days=rng.randint(60, 900))
                start = end - timedelta(days=365 * (3 + line))
                remaining = (end - self.now).days
                self.warranties.append({
                    "Id": wid, "DeviceModel": d["Model"], "DeviceIdentifier": d["DeviceServiceTag"], "DeviceType": d["Type"],
                    "CountryLookupCode": "US", "CustomerNumber": 12345678, "LocalChannel": "Direct", "OrderNumber": f"5{1000000 + i}",
                    "ServiceLevelCode": "ND" if line == 0 else "P4",
                    "ServiceLevelDescription": "ProSupport Plus Mission Critical" if line == 0 else "Keep Your Hard Drive",
                    "ServiceLevelGroup": 5 if line == 0 else 8, "ServiceProvider": "DELL",
                    "StartDate": ts(start), "EndDate": ts(end), "DaysRemaining": remaining, "Timestamp": ts(self.ago(hours=6)),
                    "State": "Expired" if remaining < 0 else "Active",
                })
                wid += 1

    # -- firmware ---------------------------------------------------------------
    def build_firmware(self):
        self.catalogs = [{
            "Id": 22, "Filename": "catalog.xml", "SourcePath": "catalog/catalog.gz", "Status": "Completed", "BaseLocation": "downloads.dell.com",
            "ManifestVersion": "24.03.00", "ManifestIdentifier": "7a8b9c", "ReleaseDate": "2024-03-01 00:00:00.000", "ReleaseIdentifier": "R24.03",
            "PredecessorIdentifier": "R24.02", "LastUpdated": ts(self.ago(days=4)), "NextUpdate": ts(self.now + timedelta(days=3)),
            "BundlesCount": 1288, "TaskId": 10020, "Schedule": {"StartTime": None, "EndTime": None, "Cron": "0 0 0 ? * sun *"},
            "Repository": {"Id": 12, "Name": "Dell Online Catalog", "Description": "Validated stack catalog", "Source": "downloads.dell.com",
                           "DomainName": None, "Username": None, "Password": None, "CheckCertificate": True, "RepositoryType": "DELL_ONLINE"},
            "AssociatedBaselines": [{"BaselineId": 50, "BaselineName": "Prod DB baseline"}, {"BaselineId": 51, "BaselineName": "R750 fleet baseline"}],
        }, {
            "Id": 23, "Filename": "lab.xml", "SourcePath": "repo/lab.xml", "Status": "Completed", "BaseLocation": None,
            "ManifestVersion": "24.01.00", "ManifestIdentifier": "11ffee", "ReleaseDate": "2024-01-15 00:00:00.000", "ReleaseIdentifier": "LAB",
            "PredecessorIdentifier": None, "LastUpdated": ts(self.ago(days=40)), "NextUpdate": None, "BundlesCount": 120, "TaskId": 10021,
            "Schedule": None,
            "Repository": {"Id": 13, "Name": "Lab CIFS repo", "Description": "Lab share", "Source": "fs01.lab.example", "DomainName": "LAB",
                           "Username": "svc-ome", "Password": None, "CheckCertificate": False, "RepositoryType": "CIFS"},
            "AssociatedBaselines": [],
        }]
        prod = self.groups_by_id[10101]["_members"]
        r750 = self.groups_by_id[10103]["_members"]
        self.baselines = []
        self.compliance = {}
        for bid, name, cat, repo, members, target in [
            (50, "Prod DB baseline", 22, self.catalogs[0], prod, [{"Id": 10101, "Type": {"Id": 6000, "Name": "GROUP"}}]),
            (51, "R750 fleet baseline", 22, self.catalogs[0], r750, [{"Id": m, "Type": {"Id": 1000, "Name": "DEVICE"}} for m in r750]),
        ]:
            reports = []
            crit = warn = normal = 0
            for n, did in enumerate(members):
                d = self.by_id[did]
                sw = {s["DeviceDescription"]: s["Version"] for inv in self.inventory[did] if inv["InventoryType"] == "deviceSoftware" for s in inv["InventoryInfo"]}
                comps = []
                cid = bid * 10000 + n * 10

                def comp(name, cur, base, crit_name, src, tid, reboot=True, ctype="FIRMWARE"):
                    status = "OK" if cur == base else ("CRITICAL" if crit_name == "Urgent" else "WARNING")
                    return {"Id": cid + len(comps), "Name": name, "CurrentVersion": cur, "Version": base, "ComplianceStatus": status,
                            "UpdateAction": "EQUAL" if cur == base else "UPGRADE", "Criticality": "Ok" if cur == base else crit_name,
                            "RebootRequired": reboot and cur != base, "SourceName": src, "TargetIdentifier": tid, "ComponentType": ctype,
                            "Path": f"FOLDER0{tid}M/1/{name.replace(' ', '_')}_{base}.EXE", "PrerequisiteInfo": "", "ImpactAssessment": "",
                            "UniqueIdentifier": f"{tid}-{base}", "Uri": f"https://www.dell.com/support/home/drivers/driversdetails?driverid={tid}",
                            "ComplianceDependencies": []}
                comps.append(comp("BIOS", sw["BIOS"], "1.14.2", "Urgent", "DCIM:INSTALLED#741__BIOS.Setup.1-1", "159"))
                comps.append(comp("Integrated Dell Remote Access Controller", sw["Integrated Dell Remote Access Controller"], "7.00.60.00", "Recommended", "DCIM:INSTALLED#iDRAC.Embedded.1-1#IDRACinfo", "25227", reboot=False))
                comps.append(comp("Mellanox ConnectX-5 EN 25GbE Dual-port", sw["Mellanox ConnectX-5 EN 25GbE Dual-port"], "22.31.6", "Recommended", "DCIM:INSTALLED#701__NIC.Slot.1-1-1", "104815"))
                comps.append(comp("PERC H755 Front", sw["PERC H755 Front"], "52.26.0-5179", "Urgent", "DCIM:INSTALLED#304_C_RAID.SL.3-1", "103560"))
                statuses = {c["ComplianceStatus"] for c in comps}
                dev_status = "CRITICAL" if "CRITICAL" in statuses else ("WARNING" if "WARNING" in statuses else "OK")
                crit += dev_status == "CRITICAL"
                warn += dev_status == "WARNING"
                normal += dev_status == "OK"
                reports.append({
                    "Id": bid * 100 + n, "DeviceId": did, "ServiceTag": d["DeviceServiceTag"], "DeviceModel": d["Model"], "DeviceName": d["DeviceName"],
                    "DeviceTypeId": 1000, "DeviceTypeName": "SERVER", "FirmwareStatus": "Compliant" if dev_status == "OK" else "Non-Compliant",
                    "ComplianceStatus": dev_status, "RebootRequired": any(c["RebootRequired"] for c in comps), "ComponentComplianceReports": comps,
                })
            self.compliance[bid] = reports
            self.baselines.append({
                "Id": bid, "Name": name, "Description": f"{name} (synthetic)", "CatalogId": cat, "RepositoryId": repo["Repository"]["Id"],
                "RepositoryName": repo["Repository"]["Name"], "RepositoryType": repo["Repository"]["RepositoryType"], "TaskId": 10022 + (bid - 50),
                "TaskStatusId": 2060, "LastRun": ts(self.ago(hours=8 + (bid - 50))), "DowngradeEnabled": False, "Is64Bit": True, "FilterNoRebootRequired": False,
                "Targets": target,
                "ComplianceSummary": {"ComplianceStatus": "CRITICAL" if crit else ("WARNING" if warn else "OK"), "NumberOfCritical": crit,
                                      "NumberOfWarning": warn, "NumberOfNormal": normal, "NumberOfDowngrade": 0},
            })

    # -- jobs -----------------------------------------------------------------------
    def build_jobs(self):
        rng = self.rng
        self.jobs = []
        self.histories = {}
        self.history_details = {}
        servers = [d for d in self.devices if d["Type"] == 1000]
        kinds = [
            ("Default Inventory Task", "Inventory_Task", 8, True, 2060),
            ("Default Health Task", "Health_Task", 20, True, 2060),
            ("Discovery-Lab-Rack-12", "Discovery_Task", 101, False, 2070),
            ("Firmware update: Prod DB BIOS", "Update_Task", 5, False, 2070),
            ("Firmware update: R750 NIC", "Update_Task", 5, False, 2090),
            ("Refresh compliance: Prod DB baseline", "Compliance_Task", 61, True, 2060),
            ("Warranty refresh", "Warranty_Task", 42, True, 2060),
            ("Default Console Update Execution Task", "ConsoleUpdateExecution_Task", 50, True, 2060),
        ]
        for n in range(30):
            name, tname, tid, internal, last = kinds[n % len(kinds)]
            if n >= len(kinds):
                name = f"{name} #{n // len(kinds) + 1}"
                if tname == "Update_Task" and n % 3 == 0:
                    last = 2060
            jid = 10001 + n
            targets = [{"Id": d["Id"], "Data": "", "TargetType": {"Id": 1000, "Name": "DEVICE"}} for d in rng.sample(servers, 3)] if tname in ("Update_Task", "Inventory_Task") else []
            if tname == "Update_Task":
                targets = [{"Id": d["Id"], "Data": "", "TargetType": {"Id": 1000, "Name": "DEVICE"}} for d in servers[:3]] if "BIOS" in name else [{"Id": self.groups_by_id[10103]["_members"][0], "Data": "", "TargetType": {"Id": 1000, "Name": "DEVICE"}}]
            params = []
            if tname == "Update_Task":
                params = [{"JobId": jid, "Key": "operationName", "Value": "INSTALL_FIRMWARE"}, {"JobId": jid, "Key": "complianceUpdate", "Value": "true"},
                          {"JobId": jid, "Key": "stagingValue", "Value": "false"}, {"JobId": jid, "Key": "signVerify", "Value": "true"}]
            if tname == "Discovery_Task":
                params = [{"JobId": jid, "Key": "discoveryTargets", "Value": "192.0.2.200-192.0.2.210"}, {"JobId": jid, "Key": "password", "Value": "REDACTED-BY-APPLIANCE"}]
            last_run = self.ago(hours=rng.randint(1, 96))
            self.jobs.append({
                "Id": jid, "JobName": name, "JobDescription": name, "NextRun": ts(self.now + timedelta(hours=24)) if internal else None,
                "LastRun": ts(last_run), "StartTime": None, "EndTime": None, "Schedule": "0 0 0/1 1/1 * ? *" if internal else "startnow",
                "State": "Enabled", "CreatedBy": "system" if internal else "admin", "UpdatedBy": "system" if internal else "admin",
                "Visible": True, "Editable": not internal, "Builtin": internal, "UserGenerated": not internal,
                "Targets": targets, "Params": params,
                "LastRunStatus": {"Id": last, "Name": {2060: "Completed", 2070: "Failed", 2090: "Warning"}[last]},
                "JobType": {"Id": tid, "Name": tname, "Internal": internal},
                "JobStatus": {"Id": 2020 if internal else 2080, "Name": "Scheduled" if internal else "New"},
            })
            hists = []
            for h in range(2 if not internal else 1):
                hid = jid * 100 + h
                when = last_run - timedelta(hours=24 * h)
                status = last if h == 0 else 2060
                hists.append({"Id": hid, "JobName": name, "Progress": "100", "StartTime": ts(when), "EndTime": ts(when + timedelta(minutes=4)),
                              "LastUpdateTime": ts(when + timedelta(minutes=4)), "ExecutedBy": "system" if internal else "admin", "JobId": jid,
                              "JobStatus": {"Id": status, "Name": {2060: "Completed", 2070: "Failed", 2090: "Warning"}[status]}})
                details = []
                keys = [self.by_id[t["Id"]]["DeviceManagement"][0]["NetworkAddress"] for t in targets] or ["192.0.2.201", "192.0.2.202"]
                for k, key in enumerate(keys):
                    if status == 2070 and tname == "Update_Task":
                        value = ("Running\nFirmware update job started.\nDownloading package BIOS_1.14.2.EXE.\n: ========== EEMI Code: CUPD3004 ==========\n"
                                 "Message: Unable to update the firmware because the iDRAC Lifecycle Controller is not in a ready state.\n"
                                 "Recommended actions: Reset the iDRAC and retry the operation.\n=======================================\nTask Failed. Completed With Errors.")
                    elif status == 2070:
                        value = ("Running\nDiscovery of target {0} started.\nDiscovery target resolved to IP  {0} .\n: ========== EEMI Code: CGEN1009 ==========\n"
                                 "Message: Unable to perform the requested action because the device management endpoint authentication over WSMAN, REDFISH failed.\n"
                                 "Recommended actions: Make sure the credentials associated with the device management endpoint are valid and retry the operation.\n"
                                 "=======================================\nTask Failed. Completed With Errors.").format(key)
                    elif status == 2090:
                        value = "Running\nFirmware update job started.\nPackage applied; reboot pending.\nTask Completed With Warnings."
                    else:
                        value = "Running\nTask started.\nTask completed successfully."
                    details.append({"Id": hid * 10 + k, "Progress": "100", "StartTime": ts(when), "EndTime": ts(when + timedelta(minutes=3)),
                                    "ElapsedTime": "00:03:00", "Key": key, "Value": value, "ExecutionHistoryId": hid,
                                    "IdBaseEntity": targets[k]["Id"] if k < len(targets) else 0,
                                    "JobStatus": {"Id": status, "Name": {2060: "Completed", 2070: "Failed", 2090: "Warning"}[status]}})
                self.history_details[hid] = details
            self.histories[jid] = hists

    # -- audit ----------------------------------------------------------------------
    def build_audit(self):
        rng = self.rng
        entries = [
            (2, "Audit", "CUSR1100", "Successfully logged in using admin, from 192.0.2.5 and GUI.", "admin", "192.0.2.5"),
            (2, "Audit", "CUSR1100", "Successfully logged in using svc-bow, from 127.0.0.1 and API.", "svc-bow", "127.0.0.1"),
            (8, "Audit", "CUSR1101", "Unable to log in because the user name or password is invalid: ops-user from 192.0.2.77.", "ops-user", "192.0.2.77"),
            (2, "Configuration", "CJOB1000", "Job 'Firmware update: Prod DB BIOS' was created.", "admin", "192.0.2.5"),
            (2, "Configuration", "CDEV6020", "Device group 'Production DB' was updated.", "ops-user", "192.0.2.9"),
            (2, "Audit", "CUSR1103", "User admin logged out from GUI.", "admin", "192.0.2.5"),
            (8, "Configuration", "CAPP1120", "Appliance network configuration was modified.", "admin", "192.0.2.5"),
        ]
        self.audit = []
        for n in range(40):
            sev, cat, mid, msg, user, ip = entries[n % len(entries)]
            self.audit.append({"Id": 90001 + n, "Severity": sev, "Message": msg, "Category": cat, "UserName": user, "IpAddress": ip,
                               "MessageArgs": "", "MessageID": mid, "CreatedDate": ts(self.ago(minutes=rng.randint(10, 7 * 24 * 60)))})
        self.audit.sort(key=lambda a: a["CreatedDate"], reverse=True)

    # -- templates / configuration baselines ---------------------------------------
    def build_templates(self):
        self.templates = [
            {"Id": 1, "Name": "iDRAC Enable Performance Profile for Virtualization", "Description": "Tune workload for Performance Optimized Virtualization",
             "SourceDeviceId": 0, "TypeId": 2, "ViewTypeId": 4, "TaskId": 0, "HasIdentityAttributes": False, "Status": 0, "IdentityPoolId": 0,
             "IsBuiltIn": True, "IsPersistencePolicyValid": False, "IsStatelessAvailable": False, "CreatedBy": "system",
             "CreationTime": "1970-01-31 00:00:56.372144", "LastUpdatedBy": None, "LastUpdatedTime": "1970-01-31 00:00:56.372144"},
            {"Id": 8, "Name": "Prod DB golden config", "Description": "Captured from srv-01", "SourceDeviceId": 10001, "TypeId": 2, "ViewTypeId": 2,
             "TaskId": 10050, "HasIdentityAttributes": True, "Status": 2060, "IdentityPoolId": 0, "IsBuiltIn": False, "IsPersistencePolicyValid": True,
             "IsStatelessAvailable": True, "CreatedBy": "admin", "CreationTime": ts(self.ago(days=30)), "LastUpdatedBy": "admin", "LastUpdatedTime": ts(self.ago(days=12))},
            {"Id": 9, "Name": "Prod DB compliance template", "Description": "BIOS/iDRAC compliance", "SourceDeviceId": 10001, "TypeId": 2, "ViewTypeId": 1,
             "TaskId": 10051, "HasIdentityAttributes": False, "Status": 2060, "IdentityPoolId": 0, "IsBuiltIn": False, "IsPersistencePolicyValid": False,
             "IsStatelessAvailable": False, "CreatedBy": "admin", "CreationTime": ts(self.ago(days=29)), "LastUpdatedBy": "admin", "LastUpdatedTime": ts(self.ago(days=29))},
        ]
        prod = self.groups_by_id[10101]["_members"]
        reports = []
        for n, did in enumerate(prod):
            d = self.by_id[did]
            reports.append({"Id": did, "DeviceName": d["DeviceName"], "ServiceTag": d["DeviceServiceTag"], "Model": d["Model"], "DeviceType": 1000,
                            "ComplianceStatus": "NONCOMPLIANT" if n in (1, 6) else "COMPLIANT", "InventoryTime": ts(self.ago(hours=9))})
        self.config_compliance = {60: reports}
        self.config_baselines = [{
            "Id": 60, "Name": "Prod DB config baseline", "Description": "Compliance of production DB hosts", "TemplateId": 9,
            "TemplateName": "Prod DB compliance template", "TemplateType": 2, "LastRun": ts(self.ago(hours=9)), "TaskId": 10052, "TaskStatus": 2060,
            "PercentageComplete": "100", "BaselineTargets": [{"Id": 10101, "Type": {"Id": 6000, "Name": "GROUP"}}],
            "ConfigComplianceSummary": {"ComplianceStatus": "NONCOMPLIANT", "NumberOfCritical": 2, "NumberOfWarning": 0,
                                        "NumberOfNormal": len(prod) - 2, "NumberOfIncomplete": 0},
        }]

    # -- MCM domains -----------------------------------------------------------------
    def build_domains(self):
        self.domains = []
        for n, cid in enumerate([10101, 10102]):
            d = self.by_id[cid]
            self.domains.append({
                "Id": 1 + n, "DeviceId": cid, "PublicAddress": [d["DeviceManagement"][0]["NetworkAddress"]], "Name": d["DeviceName"],
                "Description": "MX7000 chassis", "Identifier": d["DeviceServiceTag"], "DomainTypeId": 4000, "DomainTypeValue": "MSM_GROUP",
                "DomainRoleTypeId": 3000 if n == 0 else 3001, "DomainRoleTypeValue": "LEAD" if n == 0 else "MEMBER", "Version": "2.10.10",
                "Local": n == 0, "GroupId": "6b3b1c5e-4a1d-4f0d-9b2a-1a2b3c4d5e6f", "GroupName": "LAB-MCM", "BackupLead": n == 1,
                "BackupLeadHealth": 1000, "Capabilities": [{"Id": 1000, "Name": "FABRIC"}, {"Id": 2000, "Name": "DEPLOY"}],
            })


# ---------------------------------------------------------------------------
# OData helpers
# ---------------------------------------------------------------------------


class FilterError(Exception):
    pass


_TOKEN = re.compile(r"\s*(?:(\()|(\))|('(?:[^']|'')*')|(-?\d+(?:\.\d+)?)|([A-Za-z_][A-Za-z0-9_./]*)|(,))")


def tokenize(text):
    pos, out = 0, []
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN.match(text, pos)
        if not m:
            raise FilterError(f"Unexpected character at position {pos}.")
        pos = m.end()
        if m.group(1):
            out.append(("lp", "("))
        elif m.group(2):
            out.append(("rp", ")"))
        elif m.group(3):
            out.append(("str", m.group(3)[1:-1].replace("''", "'")))
        elif m.group(4):
            v = m.group(4)
            out.append(("num", float(v) if "." in v else int(v)))
        elif m.group(5):
            out.append(("id", m.group(5)))
        elif m.group(6):
            out.append(("comma", ","))
    return out


def get_path(record, name):
    cur = record
    for part in name.replace("/", ".").split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise FilterError(f"Property '{name}' is not supported by this resource.")
        cur = cur[part]
    return cur


class FilterParser:
    """Subset of OData $filter: comparisons, contains(), and/or, parentheses."""

    OPS = {"eq", "ne", "gt", "ge", "lt", "le"}

    def __init__(self, text):
        self.toks = tokenize(text)
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else (None, None)

    def take(self, kind=None):
        tok = self.peek()
        if tok[0] is None or (kind and tok[0] != kind):
            raise FilterError("Malformed $filter expression.")
        self.i += 1
        return tok

    def parse(self):
        pred = self.expr()
        if self.peek()[0] is not None:
            raise FilterError("Malformed $filter expression.")
        return pred

    def expr(self):
        left = self.term()
        while self.peek() == ("id", "and") or self.peek() == ("id", "or"):
            op = self.take()[1]
            right = self.term()
            if op == "and":
                left = (lambda a, b: lambda r: a(r) and b(r))(left, right)
            else:
                left = (lambda a, b: lambda r: a(r) or b(r))(left, right)
        return left

    def term(self):
        kind, val = self.peek()
        if kind == "lp":
            self.take("lp")
            inner = self.expr()
            self.take("rp")
            return inner
        if kind == "id" and val == "contains":
            self.take("id")
            self.take("lp")
            field = self.take("id")[1]
            self.take("comma")
            needle = self.take("str")[1]
            self.take("rp")
            return lambda r: isinstance(get_path(r, field), str) and needle.lower() in get_path(r, field).lower()
        if kind == "id" and val == "not":
            self.take("id")
            inner = self.term()
            return lambda r: not inner(r)
        field = self.take("id")[1]
        op = self.take("id")[1].lower()
        if op not in self.OPS:
            raise FilterError(f"Operator '{op}' is not supported.")
        vk, value = self.take()
        if vk == "id":
            if value in ("true", "false"):
                value = value == "true"
            elif value == "null":
                value = None
            else:
                raise FilterError("Malformed $filter literal.")
        elif vk not in ("str", "num"):
            raise FilterError("Malformed $filter literal.")

        def pred(r, field=field, op=op, value=value):
            actual = get_path(r, field)
            if op == "eq":
                return actual == value or (isinstance(actual, str) and isinstance(value, str) and actual.lower() == value.lower())
            if op == "ne":
                return actual != value
            if actual is None or value is None:
                return False
            try:
                return {"gt": actual > value, "ge": actual >= value, "lt": actual < value, "le": actual <= value}[op]
            except TypeError:
                raise FilterError(f"Cannot compare property '{field}' with the supplied literal.") from None
        return pred


def apply_odata(records, params, path, default_page=DEFAULT_PAGE):
    """Apply $filter/$orderby/$skip/$top; return (page, total, next_link)."""
    allowed = {"$top", "$skip", "$filter", "$orderby", "$count"}
    unknown = set(params) - allowed
    if unknown:
        raise FilterError(f"Query option '{sorted(unknown)[0]}' is not supported.")
    rows = list(records)
    if params.get("$filter"):
        pred = FilterParser(params["$filter"]).parse()
        rows = [r for r in rows if pred(r)]
    if params.get("$orderby"):
        for term in reversed([t.strip() for t in params["$orderby"].split(",") if t.strip()]):
            parts = term.split()
            if len(parts) > 2 or (len(parts) == 2 and parts[1].lower() not in ("asc", "desc")):
                raise FilterError("Malformed $orderby expression.")
            key = parts[0]
            rows.sort(key=lambda r: (get_path(r, key) is None, get_path(r, key) if get_path(r, key) is not None else 0),
                      reverse=len(parts) == 2 and parts[1].lower() == "desc")
    try:
        skip = int(params.get("$skip", 0))
        top = int(params.get("$top", default_page))
    except ValueError:
        raise FilterError("$top and $skip must be integers.") from None
    if skip < 0 or top < 0:
        raise FilterError("$top and $skip must be non-negative.")
    total = len(rows)
    page = rows[skip:skip + top]
    next_link = None
    if skip + top < total and top > 0:
        # Mirrors appliance behaviour observed by community clients: the continuation
        # carries only $skip/$top; clients must re-apply $filter/$orderby themselves.
        next_link = f"{path}?$skip={skip + top}&$top={top}"
    return page, total, next_link


def strip_private(rec):
    return {k: v for k, v in rec.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class SessionStore:
    def __init__(self, max_sessions):
        self.lock = threading.Lock()
        self.max = max_sessions
        self.by_token = {}
        self.by_id = {}

    def create(self, username, ip):
        with self.lock:
            if len(self.by_id) >= self.max:
                return None, None
            sid = str(uuid.uuid4())
            token = uuid.uuid4().hex
            now = ts(datetime.now(timezone.utc))
            body = {"Id": sid, "Description": username, "Name": "API", "UserName": username, "UserId": 10078, "Password": None,
                    "Roles": ["VIEWER"], "IpAddress": ip, "StartTimeStamp": now, "LastAccessedTimeStamp": now, "DomainName": None,
                    "ClientType": "API", "DirectoryGroup": []}
            self.by_token[token] = body
            self.by_id[sid] = token
            return token, body

    def valid(self, token):
        with self.lock:
            return token in self.by_token

    def delete(self, sid):
        with self.lock:
            token = self.by_id.pop(sid, None)
            if token:
                self.by_token.pop(token, None)
            return token is not None

    def list(self):
        with self.lock:
            return list(self.by_token.values())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OME-Simulator/4.1.0"
    estate = None
    sessions = None
    username = ""
    password = ""
    page_size = DEFAULT_PAGE

    def log_message(self, *args):
        if os.environ.get("OME_SIM_LOG"):
            super().log_message(*args)

    # -- plumbing --------------------------------------------------------------
    def reply(self, status, body=None, headers=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", CONTENT_TYPE)
            self.send_header("OData-Version", "4.0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def authenticated(self):
        token = self.headers.get("X-Auth-Token")
        if token and self.sessions.valid(token):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
            except Exception:
                return False
            return bool(self.password) and user == self.username and pwd == self.password
        return False

    def params(self):
        pairs = parse_qsl(urlsplit(self.path).query, keep_blank_values=True)
        out = {}
        for k, v in pairs:
            if k in out:
                raise FilterError(f"Duplicate query option '{k}'.")
            out[k] = v
        return out

    def collection(self, path, records, entity, id_key="Id", id_fmt="{}"):
        page, total, next_link = apply_odata(records, self.params(), path, self.page_size)
        value = []
        for r in page:
            rec = {"@odata.type": f"#{entity}"}
            if id_key and id_key in r:
                rec["@odata.id"] = f"{path}({id_fmt.format(r[id_key])})"
            rec.update(strip_private(r))
            value.append(rec)
        body = {"@odata.context": f"/api/$metadata#Collection({entity})", "@odata.count": total, "value": value}
        if next_link:
            body["@odata.nextLink"] = next_link
        self.reply(200, body)

    def singleton(self, path, record, entity):
        if self.params():
            raise FilterError("Query options are not supported for this resource.")
        body = {"@odata.context": f"/api/$metadata#{entity}", "@odata.type": f"#{entity}", "@odata.id": path}
        body.update(strip_private(record))
        self.reply(200, body)

    # -- verbs ---------------------------------------------------------------------
    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/SessionService/Sessions":
            return self.create_session()
        self.refuse_mutation(path)

    def do_DELETE(self):
        path = urlsplit(self.path).path
        m = re.fullmatch(r"/api/SessionService/Sessions\('([^']+)'\)", unquote(path))
        if m:
            if not self.authenticated():
                return self.reply(401, ERR_UNAUTHORIZED)
            if not self.sessions.delete(m.group(1)):
                return self.reply(404, ERR_NOT_FOUND)
            return self.reply(204)
        self.refuse_mutation(path)

    def do_PUT(self):
        self.refuse_mutation(urlsplit(self.path).path)

    def do_PATCH(self):
        self.refuse_mutation(urlsplit(self.path).path)

    def refuse_mutation(self, path):
        self.read_body()
        if not self.authenticated():
            return self.reply(401, ERR_UNAUTHORIZED)
        if "/Actions/" in path:
            # The simulated account holds the Viewer role: actions are a privilege failure.
            return self.reply(403, ERR_PRIVILEGE)
        self.reply(405, ERR_GENERAL, {"Allow": "GET"})

    def create_session(self):
        try:
            payload = json.loads(self.read_body() or b"{}")
        except ValueError:
            return self.reply(400, err_bad_request("Unable to process the request because the request body is not valid JSON."))
        if not isinstance(payload, dict) or not payload.get("UserName") or not payload.get("Password"):
            return self.reply(400, err_bad_request("Unable to process the request because the UserName and Password are required.",
                                                   ["UserName", "Password"]))
        if payload.get("SessionType", "API") != "API":
            return self.reply(400, err_bad_request("Unable to process the request because the SessionType is not supported.", ["SessionType"]))
        if not self.password or payload["UserName"] != self.username or payload["Password"] != self.password:
            return self.reply(401, ERR_LOGIN)
        token, body = self.sessions.create(payload["UserName"], self.client_address[0])
        if token is None:
            return self.reply(400, ERR_SESSION_CAP)
        body = {"@odata.context": "/api/$metadata#SessionService.Session", "@odata.type": "#SessionService.Session",
                "@odata.id": f"/api/SessionService/Sessions('{body['Id']}')", **body}
        self.reply(201, body, {"X-Auth-Token": token, "Location": f"/api/SessionService/Sessions('{body['Id']}')"})

    def do_GET(self):
        raw_path = urlsplit(self.path).path
        path = unquote(raw_path)
        if not self.authenticated():
            return self.reply(401, ERR_UNAUTHORIZED)
        try:
            self.route(path)
        except FilterError as e:
            self.reply(400, err_bad_request(str(e)))

    # -- routing ---------------------------------------------------------------------
    def route(self, path):
        e = self.estate

        def dev(did):
            d = e.by_id.get(did)
            if d is None:
                self.reply(404, ERR_NOT_FOUND)
            return d

        if path == "/api/SessionService/Sessions":
            return self.collection(path, self.sessions.list(), "SessionService.Session", id_fmt="'{}'")
        if path == "/api/ApplicationService/Info":
            return self.singleton(path, e.info, "ApplicationService.Info")
        if path == "/api/ApplicationService/Network/CurrentAddressConfiguration":
            return self.singleton(path, e.network, "ApplicationService.CurrentAddressConfiguration")
        if path == "/api/ApplicationService/AuditLogs":
            return self.collection(path, e.audit, "ApplicationService.AuditLog")
        if path == "/api/DeviceService/Devices":
            return self.collection(path, [e.public_device(d) for d in e.devices], "DeviceService.Device")
        if path == "/api/DeviceService/DeviceSummary":
            return self.collection(path, e.device_summary(), "DeviceService.DeviceSummary", id_key=None)
        if path == "/api/DeviceService/DeviceType":
            return self.collection(path, e.device_types, "DeviceService.DeviceType", id_key=None)
        m = re.fullmatch(r"/api/DeviceService/Devices\((\d+)\)(?:/(\w+))?(?:\('(\w+)'\))?", path)
        if m:
            d = dev(int(m.group(1)))
            if d is None:
                return
            sub, arg = m.group(2), m.group(3)
            base = f"/api/DeviceService/Devices({d['Id']})"
            if sub is None:
                return self.singleton(base, e.public_device(d), "DeviceService.Device")
            if sub == "InventoryDetails":
                if arg:
                    inv = next((i for i in e.inventory[d["Id"]] if i["InventoryType"] == arg), None)
                    if inv is None:
                        return self.reply(400, err_bad_request(f"Unable to retrieve inventory because the inventory type '{arg}' is not applicable for the device."))
                    return self.singleton(f"{base}/InventoryDetails('{arg}')", inv, "DeviceService.InventoryDetail")
                return self.collection(f"{base}/InventoryDetails", e.inventory[d["Id"]], "DeviceService.InventoryDetail", id_key="InventoryType", id_fmt="'{}'")
            if sub == "InventoryTypes":
                return self.singleton(f"{base}/InventoryTypes", {"InventoryTypes": [i["InventoryType"] for i in e.inventory[d["Id"]]]}, "DeviceService.InventoryTypes")
            if sub == "HardwareLogs":
                return self.collection(f"{base}/HardwareLogs", e.hwlogs[d["Id"]], "DeviceService.HardwareLog")
            if sub in ("Power", "Temperature", "SystemUpTime"):
                if d["Type"] != 1000:
                    return self.reply(404, ERR_NOT_FOUND)
                store = {"Power": (e.power, "DeviceService.PowerUsage"), "Temperature": (e.temperature, "DeviceService.Temperature"),
                         "SystemUpTime": (e.uptime, "DeviceService.SystemUpTime")}[sub]
                return self.singleton(f"{base}/{sub}", store[0][d["Id"]], store[1])
            if sub == "SubSystemHealth":
                rows = [{"Id": 1, "SubSystem": "System", "RollupStatus": d["Status"]}, {"Id": 2, "SubSystem": "Power", "RollupStatus": d["Status"] if d["Status"] == 4000 else 1000},
                        {"Id": 3, "SubSystem": "Storage", "RollupStatus": d["Status"] if d["Status"] == 3000 else 1000}]
                return self.collection(f"{base}/SubSystemHealth", rows, "DeviceService.SubSystemHealth")
            return self.reply(404, ERR_NOT_FOUND)
        if path == "/api/AlertService/Alerts":
            return self.collection(path, e.alerts, "AlertService.Alert")
        m = re.fullmatch(r"/api/AlertService/Alerts\((\d+)\)", path)
        if m:
            a = next((x for x in e.alerts if x["Id"] == int(m.group(1))), None)
            return self.singleton(path, a, "AlertService.Alert") if a else self.reply(404, ERR_NOT_FOUND)
        if path == "/api/AlertService/AlertCategories":
            cats = [
                {"Name": "Application", "IsBuiltIn": True, "CategoriesDetails": [
                    {"Id": 4, "Name": "Audit", "CatalogName": "Application", "SubCategoryDetails": [{"Id": 35, "Name": "Users", "Description": "Users"}]},
                    {"Id": 5, "Name": "Configuration", "CatalogName": "Application", "SubCategoryDetails": [{"Id": 85, "Name": "Application", "Description": "Application"}]},
                    {"Id": 3, "Name": "Updates", "CatalogName": "Application", "SubCategoryDetails": [{"Id": 112, "Name": "Firmware", "Description": "Firmware"}]}]},
                {"Name": "iDRAC", "IsBuiltIn": True, "CategoriesDetails": [
                    {"Id": 1, "Name": "System Health", "CatalogName": "iDRAC", "SubCategoryDetails": [
                        {"Id": 7700, "Name": "Power Supply", "Description": "Power Supply"}, {"Id": 50, "Name": "Fan", "Description": "Fan"},
                        {"Id": 110, "Name": "Temperature", "Description": "Temperature"}, {"Id": 80, "Name": "Memory", "Description": "Memory"},
                        {"Id": 65, "Name": "Processor", "Description": "Processor"}, {"Id": 100, "Name": "Network", "Description": "Network"}]},
                    {"Id": 2, "Name": "Storage", "CatalogName": "iDRAC", "SubCategoryDetails": [{"Id": 90, "Name": "Devices", "Description": "Devices"}]},
                    {"Id": 4, "Name": "Audit", "CatalogName": "iDRAC", "SubCategoryDetails": [{"Id": 41, "Name": "Auto System Reset", "Description": "Auto System Reset"}, {"Id": 35, "Name": "Users", "Description": "Users"}]}]},
            ]
            return self.collection(path, cats, "AlertService.AlertCategory", id_key="Name", id_fmt="'{}'")
        if path == "/api/WarrantyService/Warranties":
            return self.collection(path, e.warranties, "WarrantyService.Warranty")
        if path == "/api/UpdateService/Baselines":
            rows = [dict(b, **{"DeviceComplianceReports@odata.navigationLink": f"/api/UpdateService/Baselines({b['Id']})/DeviceComplianceReports"}) for b in e.baselines]
            return self.collection(path, rows, "UpdateService.Baselines")
        m = re.fullmatch(r"/api/UpdateService/Baselines\((\d+)\)(?:/(\w+))?", path)
        if m:
            b = next((x for x in e.baselines if x["Id"] == int(m.group(1))), None)
            if b is None:
                return self.reply(404, ERR_NOT_FOUND)
            if m.group(2) is None:
                return self.singleton(path, b, "UpdateService.Baselines")
            if m.group(2) == "DeviceComplianceReports":
                return self.collection(path, e.compliance[b["Id"]], "UpdateService.DeviceComplianceReport")
            return self.reply(404, ERR_NOT_FOUND)
        if path == "/api/UpdateService/Catalogs":
            return self.collection(path, e.catalogs, "UpdateService.Catalogs")
        if path == "/api/JobService/Jobs":
            return self.collection(path, e.jobs, "JobService.Job")
        m = re.fullmatch(r"/api/JobService/Jobs\((\d+)\)(?:/ExecutionHistories(?:\((\d+)\)(?:/ExecutionHistoryDetails)?)?)?", path)
        if m:
            j = next((x for x in e.jobs if x["Id"] == int(m.group(1))), None)
            if j is None:
                return self.reply(404, ERR_NOT_FOUND)
            if path.endswith(f"Jobs({j['Id']})"):
                return self.singleton(path, j, "JobService.Job")
            if m.group(2) is None:
                rows = [dict(h, **{"ExecutionHistoryDetails@odata.navigationLink": f"/api/JobService/Jobs({j['Id']})/ExecutionHistories({h['Id']})/ExecutionHistoryDetails"}) for h in e.histories[j["Id"]]]
                return self.collection(path, rows, "JobService.ExecutionHistory")
            hid = int(m.group(2))
            if hid not in e.history_details or not any(h["Id"] == hid for h in e.histories[j["Id"]]):
                return self.reply(404, ERR_NOT_FOUND)
            if path.endswith("/ExecutionHistoryDetails"):
                return self.collection(path, e.history_details[hid], "JobService.ExecutionHistoryDetail")
            return self.singleton(path, next(h for h in e.histories[j["Id"]] if h["Id"] == hid), "JobService.ExecutionHistory")
        if path == "/api/GroupService/Groups":
            return self.collection(path, [e.public_group(g) for g in e.groups], "GroupService.Group")
        m = re.fullmatch(r"/api/GroupService/Groups\((\d+)\)(?:/(\w+))?", path)
        if m:
            g = e.groups_by_id.get(int(m.group(1)))
            if g is None:
                return self.reply(404, ERR_NOT_FOUND)
            if m.group(2) is None:
                return self.singleton(path, e.public_group(g), "GroupService.Group")
            if m.group(2) in ("Devices", "AllLeafDevices"):
                members = [e.public_device(e.by_id[i]) for i in g["_members"]]
                return self.collection(path, members, "DeviceService.Device")
            if m.group(2) == "SubGroups":
                return self.collection(path, [e.public_group(x) for x in e.groups if x["ParentId"] == g["Id"]], "GroupService.Group")
            return self.reply(404, ERR_NOT_FOUND)
        if path == "/api/TemplateService/Templates":
            return self.collection(path, e.templates, "TemplateService.Template")
        if path == "/api/TemplateService/Baselines":
            return self.collection(path, e.config_baselines, "TemplateService.Baseline")
        m = re.fullmatch(r"/api/TemplateService/Baselines\((\d+)\)/DeviceConfigComplianceReports", path)
        if m:
            reports = e.config_compliance.get(int(m.group(1)))
            if reports is None:
                return self.reply(404, ERR_NOT_FOUND)
            return self.collection(path, reports, "TemplateService.DeviceConfigComplianceReport")
        if path == "/api/ManagementDomainService/Domains":
            return self.collection(path, e.domains, "ManagementDomainService.Domain")
        if path == "/api/ReportService/ReportDefs":
            return self.collection(path, [], "ReportService.ReportDef")
        self.reply(404, ERR_NOT_FOUND)


def build_server(port, username, password, max_sessions=6, page_size=DEFAULT_PAGE, seed=41):
    handler = type("OmeHandler", (Handler,), {
        "estate": Estate(seed), "sessions": SessionStore(max_sessions), "username": username,
        "password": password, "page_size": page_size,
    })
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=18093, help="loopback port to listen on (default 18093)")
    p.add_argument("--max-sessions", type=int, default=6, help="concurrent API session cap enforced on POST /api/SessionService/Sessions (default 6)")
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE, help="records per page when $top is absent (default 50, like the appliance)")
    p.add_argument("--seed", type=int, default=41, help="seed for the deterministic synthetic estate")
    a = p.parse_args()
    username, password = os.environ.get("OME_USERNAME"), os.environ.get("OME_PASSWORD")
    if not username or not password:
        p.error("Set OME_USERNAME and OME_PASSWORD in the environment (synthetic credentials only).")
    server = build_server(a.port, username, password, a.max_sessions, a.page_size, a.seed)
    print(f"Synthetic OpenManage Enterprise API listening on http://127.0.0.1:{a.port} (sessions cap {a.max_sessions})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
