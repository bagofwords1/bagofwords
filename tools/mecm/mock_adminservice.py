"""Mock Configuration Manager AdminService — OData v4 shapes over HTTPS + real NTLM.

There is no container or SaaS for Configuration Manager (it needs Windows
Server, SQL Server and Active Directory), so — like tools/appdynamics and
tools/aria_operations — this simulates the surface `MecmClient` touches, with
the behaviors documented by Microsoft
(learn.microsoft.com/intune/configmgr/develop/adminservice):

  GET /AdminService/wmi/$metadata            OData CSDL for every WMI class
  GET /AdminService/wmi/<Class>              collection read ($filter/$select/
                                             $orderby/$top/$skip/$count)
  GET /AdminService/wmi/<Class>(<key>)       single-instance read
  GET /AdminService/v1.0/$metadata | Device | Device(<id>)

Faithfulness rules enforced on purpose:
  - HTTPS only, with a self-signed certificate (what a site binds by default),
    so a client that cannot trust a pasted certificate fails loudly.
  - Windows auth is a REAL NTLM handshake (pyspnego acceptor; Negotiate and
    NTLM schemes), connection-based like HTTP.sys: an authenticated keep-alive
    connection stays authenticated. No credentials -> 401 challenge.
  - An account that authenticates but is not a CM administrative user -> 403.
  - Security scopes: CONTOSO\\helpdesk_lon only sees London devices.
  - WMI class names are case-insensitive; v1.0 names are case-sensitive.
  - Lazy properties (SMS_Collection.CollectionRules,
    SMS_ApplicationLatest.SDMPackageXML) only come back on single reads.
  - Every instance carries WMI system properties (__GENUS, __CLASS, __PATH …)
    and @odata.etag; list-valued properties are JSON arrays.
  - String functions are spelled `startswith(Name,'All') eq true`.
  - $apply / $expand on the WMI route -> HTTP 400 OData error.
  - Server-driven paging with @odata.nextLink (MECM_MOCK_PAGE_SIZE, default 100),
    so a client that ignores nextLink loses rows.
  - Only GET: anything else -> 405.

Seeded estate: Contoso, primary site PS1, three offices (London, New York,
Tel Aviv) — 72 workstations + 8 servers, collections, hardware/software
inventory, applications, deployments, software updates and compliance.

Run (from the repo root):
  uv run --project backend python tools/mecm/mock_adminservice.py
  -> https://localhost:8443/AdminService   (self-signed; PEM printed at start
     and written to tools/mecm/.certs/adminservice.pem)
Creds: CONTOSO\\svc_bow / Passw0rd!          (full admin)
       CONTOSO\\helpdesk_lon / Passw0rd!     (scoped to London devices)
       CONTOSO\\notadmin / Passw0rd!         (authenticates, not a CM admin -> 403)
       UPN forms (svc_bow@contoso.lab …) work too.
"""
from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import os
import random
import re
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("MECM_MOCK_PORT", "8443"))
PAGE_SIZE = int(os.environ.get("MECM_MOCK_PAGE_SIZE", "100"))
SITE = "PS1"
DOMAIN = "CONTOSO"
PASSWORD = "Passw0rd!"
# user (upper-case DOMAIN\\name) -> scope: "all" | "london" | None (not a CM admin)
USERS = {f"{DOMAIN}\\SVC_BOW": "all", f"{DOMAIN}\\HELPDESK_LON": "london", f"{DOMAIN}\\NOTADMIN": None}

# ── NTLM credential store (pyspnego acceptor reads NTLM_USER_FILE) ──────────

_user_file = Path(tempfile.gettempdir()) / "bow-mecm-mock-ntlm-users"
_user_file.write_text("".join(
    f"{DOMAIN}:{u.split(chr(92))[1].lower()}:{PASSWORD}\n:{u.split(chr(92))[1].lower()}@contoso.lab:{PASSWORD}\n"
    for u in USERS
))
os.environ["NTLM_USER_FILE"] = str(_user_file)

import spnego  # noqa: E402  (must follow NTLM_USER_FILE)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402

# ── seeded estate ───────────────────────────────────────────────────────────

rng = random.Random(20261030)
# Ages ("last active 3 days ago") are relative to start-up, so the estate never goes stale.
NOW = dt.datetime.now(dt.UTC).replace(minute=0, second=0, microsecond=0)


def iso(d: dt.datetime) -> str:
    return d.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def days_ago(n: float) -> str:
    return iso(NOW - dt.timedelta(days=n))


OFFICES = [("London", "LON", "10.10"), ("NewYork", "NYC", "10.20"), ("TelAviv", "TLV", "10.30")]
MODELS = [("Dell Inc.", "Latitude 7440", 16384), ("Dell Inc.", "OptiPlex 7010", 16384),
          ("HP", "EliteBook 840 G10", 32768), ("Lenovo", "ThinkPad T14 Gen 4", 16384),
          ("Microsoft Corporation", "Surface Laptop 5", 8192)]
OS_WS = [("Microsoft Windows 11 Enterprise", "10.0.22631", "22631", 0.45),
         ("Microsoft Windows 11 Enterprise", "10.0.26100", "26100", 0.25),
         ("Microsoft Windows 10 Enterprise", "10.0.19045", "19045", 0.30)]
FIRST = ["Ava", "Noah", "Mia", "Liam", "Emma", "Oliver", "Sofia", "Elijah", "Isla", "Lucas", "Noa", "Yael",
         "Omer", "Tamar", "Ethan", "Grace", "Leo", "Chloe", "Daniel", "Maya", "Jack", "Ruby", "Adam", "Zoe"]
LAST = ["Smith", "Cohen", "Levi", "Brown", "Taylor", "Wilson", "Davies", "Garcia", "Miller", "Mizrahi",
        "Johnson", "Evans", "Thomas", "Roberts", "Friedman", "Walker"]

devices: list[dict] = []
users: list[dict] = []
rid = 16777220
for office, code, net in OFFICES:
    for i in range(1, 25):
        rid += 1
        first, last = rng.choice(FIRST), rng.choice(LAST)
        user = f"{first[0].lower()}{last.lower()}"
        os_pick = rng.choices(OS_WS, weights=[o[3] for o in OS_WS])[0]
        devices.append({"rid": rid, "name": f"{code}-WS-{i:03d}", "office": office, "net": net, "user": user,
                        "fullname": f"{first} {last}", "role": "workstation", "os": os_pick,
                        "model": rng.choice(MODELS), "client": rng.random() > 0.06})
for name, office, net, caption, ver, build in [
    ("CM01", "London", "10.10", "Microsoft Windows Server 2022 Standard", "10.0.20348", "20348"),
    ("SQL01", "London", "10.10", "Microsoft Windows Server 2022 Standard", "10.0.20348", "20348"),
    ("DC01", "London", "10.10", "Microsoft Windows Server 2019 Datacenter", "10.0.17763", "17763"),
    ("DC02", "NewYork", "10.20", "Microsoft Windows Server 2019 Datacenter", "10.0.17763", "17763"),
    ("FS01", "NewYork", "10.20", "Microsoft Windows Server 2016 Standard", "10.0.14393", "14393"),
    ("APP01", "TelAviv", "10.30", "Microsoft Windows Server 2022 Standard", "10.0.20348", "20348"),
    ("DP01", "TelAviv", "10.30", "Microsoft Windows Server 2022 Standard", "10.0.20348", "20348"),
    ("WEB01", "NewYork", "10.20", "Microsoft Windows Server 2016 Standard", "10.0.14393", "14393"),
]:
    rid += 1
    devices.append({"rid": rid, "name": name, "office": office, "net": net, "user": "svc_admin",
                    "fullname": "Service Admin", "role": "server", "os": (caption, ver, build, 0),
                    "model": ("VMware, Inc.", "VMware20,1", 32768), "client": True})

for d in devices:
    oct3, oct4 = rng.randint(1, 30), rng.randint(10, 250)
    d["ip"] = f"{d['net']}.{oct3}.{oct4}"
    d["mac"] = ":".join(f"{rng.randint(0, 255):02X}" for _ in range(6))
    d["last_active"] = rng.uniform(0.02, 3) if rng.random() > 0.12 else rng.uniform(15, 60)
    d["disk_size"] = rng.choice([243000, 476000, 952000]) if d["role"] == "workstation" else 102400
    d["disk_free"] = int(d["disk_size"] * (rng.uniform(0.03, 0.09) if rng.random() < 0.15 else rng.uniform(0.2, 0.7)))
    d["boot"] = rng.uniform(0.1, 40)
    d["install"] = rng.uniform(90, 900)
    d["serial"] = "".join(rng.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789") for _ in range(10))
    d["bios"] = f"1.{rng.randint(10, 24)}.{rng.randint(0, 9)}"
    d["hinv"] = rng.uniform(0.2, 8)

seen_users = set()
for d in devices:
    if d["role"] != "workstation" or d["user"] in seen_users:
        continue
    seen_users.add(d["user"])
    rid += 1
    users.append({"rid": 2063597568 + len(users), "user": d["user"], "fullname": d["fullname"], "office": d["office"]})

SOFTWARE = [  # (product, publisher, versions with weights, share of workstations)
    ("Google Chrome", "Google LLC", [("130.0.6723.70", 0.6), ("129.0.6668.90", 0.25), ("118.0.5993.118", 0.15)], 0.95),
    ("Microsoft 365 Apps for enterprise - en-us", "Microsoft Corporation", [("16.0.17928.20156", 0.8), ("16.0.16731.20636", 0.2)], 1.0),
    ("Microsoft Edge", "Microsoft Corporation", [("130.0.2849.56", 0.9), ("124.0.2478.80", 0.1)], 1.0),
    ("Zoom Workplace (64-bit)", "Zoom Video Communications, Inc.", [("6.2.5", 0.7), ("5.15.2", 0.3)], 0.7),
    ("Adobe Acrobat Reader (64-bit)", "Adobe", [("24.003.20180", 0.75), ("23.001.20093", 0.25)], 0.8),
    ("7-Zip 23.01 (x64)", "Igor Pavlov", [("23.01", 1.0)], 0.4),
    ("Java 8 Update 381 (64-bit)", "Oracle Corporation", [("8.0.3810.9", 1.0)], 0.18),
    ("Notepad++ (64-bit x64)", "Notepad++ Team", [("8.6.9", 0.8), ("8.4.2", 0.2)], 0.3),
    ("Slack (Machine - MSI)", "Slack Technologies Inc.", [("4.41.97", 1.0)], 0.5),
    ("VLC media player", "VideoLAN", [("3.0.21", 1.0)], 0.2),
    ("Configuration Manager Client", "Microsoft Corporation", [("5.00.9132.1011", 1.0)], 1.0),
]

collections = [
    {"CollectionID": "SMS00001", "Name": "All Systems", "type": 2, "builtin": True, "rule": lambda d: True},
    {"CollectionID": "SMS00002", "Name": "All Users", "type": 1, "builtin": True, "rule": None},
    {"CollectionID": "SMS00004", "Name": "All Users and User Groups", "type": 1, "builtin": True, "rule": None},
    {"CollectionID": "SMSDM003", "Name": "All Desktop and Server Clients", "type": 2, "builtin": True, "rule": lambda d: d["client"]},
    {"CollectionID": "PS100010", "Name": "London Workstations", "type": 2, "builtin": False,
     "rule": lambda d: d["office"] == "London" and d["role"] == "workstation", "wql": "SMS_R_System.ADSiteName = 'London'"},
    {"CollectionID": "PS100011", "Name": "New York Workstations", "type": 2, "builtin": False,
     "rule": lambda d: d["office"] == "NewYork" and d["role"] == "workstation", "wql": "SMS_R_System.ADSiteName = 'NewYork'"},
    {"CollectionID": "PS100012", "Name": "Tel Aviv Workstations", "type": 2, "builtin": False,
     "rule": lambda d: d["office"] == "TelAviv" and d["role"] == "workstation", "wql": "SMS_R_System.ADSiteName = 'TelAviv'"},
    {"CollectionID": "PS100013", "Name": "All Servers", "type": 2, "builtin": False,
     "rule": lambda d: d["role"] == "server", "wql": "SMS_R_System.OperatingSystemNameandVersion like '%Server%'"},
    {"CollectionID": "PS100014", "Name": "Windows 10 Devices", "type": 2, "builtin": False,
     "rule": lambda d: d["os"][2] == "19045", "wql": "SMS_G_System_OPERATING_SYSTEM.BuildNumber = '19045'"},
    {"CollectionID": "PS100015", "Name": "Windows 11 Devices", "type": 2, "builtin": False,
     "rule": lambda d: d["os"][2] in ("22631", "26100"), "wql": "SMS_G_System_OPERATING_SYSTEM.BuildNumber >= '22000'"},
    {"CollectionID": "PS100016", "Name": "Patch Ring 1 - Pilot", "type": 2, "builtin": False,
     "rule": lambda d: d["role"] == "workstation" and d["rid"] % 6 == 0, "wql": "Direct membership"},
]

UPDATES = [  # (article, title, severity, posted days ago, superseded)
    ("5044284", "2024-10 Cumulative Update for Windows 11 Version 23H2 for x64-based Systems (KB5044284)", 10, 16, False),
    ("5044285", "2024-10 Cumulative Update for Windows 11 Version 24H2 for x64-based Systems (KB5044285)", 10, 16, False),
    ("5044273", "2024-10 Cumulative Update for Windows 10 Version 22H2 for x64-based Systems (KB5044273)", 10, 16, False),
    ("5043064", "2024-09 Cumulative Update for Windows 10 Version 22H2 for x64-based Systems (KB5043064)", 10, 48, True),
    ("5044281", "2024-10 Cumulative Update for Microsoft server operating system version 21H2 for x64-based Systems (KB5044281)", 10, 16, False),
    ("5044020", "2024-10 Cumulative Update for .NET Framework 3.5, 4.8 and 4.8.1 for Windows 11 (KB5044020)", 8, 16, False),
    ("890830", "Windows Malicious Software Removal Tool x64 - v5.129 (KB890830)", 8, 16, False),
    ("2267602", "Security Intelligence Update for Microsoft Defender Antivirus - KB2267602 (Version 1.421.215.0)", 6, 1, False),
    ("5002623", "Security Update for Microsoft Office 2016 (KB5002623)", 8, 30, False),
]


def update_applies(art: str, d: dict) -> bool:
    build = d["os"][2]
    if art in ("5044284",):
        return build == "22631"
    if art in ("5044285",):
        return build == "26100"
    if art in ("5044273", "5043064"):
        return build == "19045"
    if art == "5044281":
        return build == "20348"
    if art == "5044020":
        return build in ("22631", "26100")
    if art == "5002623":
        return d["role"] == "workstation"
    return True


APPS = [  # (CI_ID, name, version, publisher)
    (16777540, "Google Chrome", "130.0.6723.70", "Google LLC"),
    (16777541, "Microsoft 365 Apps for enterprise", "2409", "Microsoft Corporation"),
    (16777542, "Zoom Workplace", "6.2.5", "Zoom Video Communications"),
    (16777543, "Adobe Acrobat Reader", "24.003.20180", "Adobe"),
    (16777544, "7-Zip", "23.01", "Igor Pavlov"),
    (16777545, "Notepad++", "8.6.9", "Notepad++ Team"),
    (16777546, "Slack", "4.41.97", "Slack Technologies"),
]


# ── class definitions: name -> (key, [(prop, edm type)], lazy props, rows fn) ─

def P(*pairs):
    return list(pairs)


S, I32, I64, B, DTO, LS = "Edm.String", "Edm.Int32", "Edm.Int64", "Edm.Boolean", "Edm.DateTimeOffset", "Collection(Edm.String)"
CLASSES: dict[str, dict] = {}


def cls(name: str, key: list[str], props: list[tuple[str, str]], rows: Callable[[], list[dict]],
        lazy: tuple[str, ...] = (), scope_key: str | None = None):
    CLASSES[name] = {"key": key, "props": props, "rows_fn": rows, "lazy": lazy, "scope_key": scope_key, "_rows": None}


def ws(d):  # workstation?
    return d["role"] == "workstation"


cls("SMS_Site", ["SiteCode"], P(("SiteCode", S), ("SiteName", S), ("Version", S), ("BuildNumber", I32),
                                 ("Type", I32), ("ServerName", S), ("ReportingSiteCode", S), ("Status", I32),
                                 ("InstallDir", S), ("TimeZoneInfo", S)),
    lambda: [{"SiteCode": SITE, "SiteName": "Contoso Primary Site", "Version": "5.00.9132.1000", "BuildNumber": 9132,
              "Type": 2, "ServerName": "CM01.contoso.lab", "ReportingSiteCode": "", "Status": 1,
              "InstallDir": "C:\\Program Files\\Microsoft Configuration Manager", "TimeZoneInfo": "000001E0 0000 000B 0001 0002 0000 0000 0000 0000 0000 0000 0003 0002 0002 0000 0000 0000 FFFFFFC4"}])

cls("SMS_R_System", ["ResourceId"], P(
    ("ResourceId", I32), ("Name", S), ("NetbiosName", S), ("ResourceDomainORWorkgroup", S), ("FullDomainName", S),
    ("OperatingSystemNameandVersion", S), ("Build", S), ("Client", I32), ("ClientType", I32), ("ClientVersion", S),
    ("Active", I32), ("Obsolete", I32), ("Decommissioned", I32), ("ADSiteName", S), ("IPAddresses", LS),
    ("MACAddresses", LS), ("SystemOUName", LS), ("LastLogonUserName", S), ("LastLogonUserDomain", S),
    ("CreationDate", DTO), ("AgentTime", LS), ("SMSAssignedSites", LS), ("ResourceType", I32)),
    lambda: [{
        "ResourceId": d["rid"], "Name": d["name"], "NetbiosName": d["name"], "ResourceDomainORWorkgroup": DOMAIN,
        "FullDomainName": "CONTOSO.LAB",
        "OperatingSystemNameandVersion": ("Microsoft Windows NT Workstation " if ws(d) else "Microsoft Windows NT Server ") + d["os"][1][:4],
        "Build": d["os"][1], "Client": 1 if d["client"] else None, "ClientType": 1 if d["client"] else None,
        "ClientVersion": "5.00.9132.1011" if d["client"] else None, "Active": 1 if d["last_active"] < 14 else 0,
        "Obsolete": 0, "Decommissioned": 0, "ADSiteName": d["office"], "IPAddresses": [d["ip"], "fe80::" + d["mac"][-5:].replace(":", "").lower()],
        "MACAddresses": [d["mac"]],
        "SystemOUName": ["CONTOSO.LAB/" + ("WORKSTATIONS" if ws(d) else "SERVERS"), f"CONTOSO.LAB/{'WORKSTATIONS' if ws(d) else 'SERVERS'}/{d['office'].upper()}"],
        "LastLogonUserName": d["user"], "LastLogonUserDomain": DOMAIN, "CreationDate": days_ago(d["install"]),
        "AgentTime": [days_ago(d["last_active"])], "SMSAssignedSites": [SITE] if d["client"] else [], "ResourceType": 5,
    } for d in devices], scope_key="ResourceId")

cls("SMS_R_User", ["ResourceID"], P(("ResourceID", I32), ("UserName", S), ("FullUserName", S), ("WindowsNTDomain", S),
                                    ("UserPrincipalName", S), ("Mail", S), ("department", S), ("CreationDate", DTO)),
    lambda: [{"ResourceID": u["rid"], "UserName": u["user"], "FullUserName": u["fullname"], "WindowsNTDomain": DOMAIN,
              "UserPrincipalName": f"{u['user']}@contoso.lab", "Mail": f"{u['user']}@contoso.com",
              "department": rng.choice(["Finance", "Sales", "Engineering", "HR", "Operations"]), "CreationDate": days_ago(400)}
             for u in users])

cls("SMS_CombinedDeviceResources", ["ResourceID"], P(
    ("ResourceID", I32), ("MachineID", I32), ("Name", S), ("Domain", S), ("IsClient", B), ("IsActive", B),
    ("ClientActiveStatus", I32), ("ClientVersion", S), ("DeviceOS", S), ("DeviceOSBuild", S), ("LastActiveTime", DTO),
    ("LastPolicyRequest", DTO), ("LastHardwareScan", DTO), ("LastSoftwareScan", DTO), ("PrimaryUser", S),
    ("CurrentLogonUser", S), ("CNIsOnline", B), ("SiteCode", S), ("ADLastLogonTime", DTO)),
    lambda: [{
        "ResourceID": d["rid"], "MachineID": d["rid"], "Name": d["name"], "Domain": DOMAIN, "IsClient": d["client"],
        "IsActive": d["last_active"] < 14, "ClientActiveStatus": 1 if d["last_active"] < 14 else 0,
        "ClientVersion": "5.00.9132.1011" if d["client"] else None, "DeviceOS": d["os"][0], "DeviceOSBuild": d["os"][1],
        "LastActiveTime": days_ago(d["last_active"]), "LastPolicyRequest": days_ago(d["last_active"] + 0.01),
        "LastHardwareScan": days_ago(d["hinv"]), "LastSoftwareScan": days_ago(d["hinv"] + 0.5),
        "PrimaryUser": f"{DOMAIN.lower()}\\{d['user']}", "CurrentLogonUser": f"{DOMAIN.lower()}\\{d['user']}" if d["last_active"] < 0.2 else None,
        "CNIsOnline": d["last_active"] < 0.2, "SiteCode": SITE if d["client"] else None, "ADLastLogonTime": days_ago(d["last_active"] + 0.3),
    } for d in devices], scope_key="ResourceID")


def _members(c):
    if c["rule"] is None:
        return []
    return [d for d in devices if c["rule"](d)]


cls("SMS_Collection", ["CollectionID"], P(
    ("CollectionID", S), ("Name", S), ("Comment", S), ("CollectionType", I32), ("MemberCount", I32),
    ("LimitToCollectionID", S), ("LimitToCollectionName", S), ("IsBuiltIn", B), ("LastRefreshTime", DTO),
    ("LastMemberChangeTime", DTO), ("RefreshType", I32), ("CollectionRules", S)),
    lambda: [{
        "CollectionID": c["CollectionID"], "Name": c["Name"], "Comment": "" if c["builtin"] else f"Managed by Contoso IT ({c.get('wql', '')})",
        "CollectionType": c["type"], "MemberCount": len(_members(c)) if c["type"] == 2 else len(users),
        "LimitToCollectionID": "" if c["builtin"] else "SMS00001", "LimitToCollectionName": "" if c["builtin"] else "All Systems",
        "IsBuiltIn": c["builtin"], "LastRefreshTime": days_ago(0.3), "LastMemberChangeTime": days_ago(2),
        "RefreshType": 6 if not c["builtin"] else 2,
        "CollectionRules": f"[{{\"RuleName\": \"{c['Name']}\", \"QueryExpression\": \"{c.get('wql', 'built-in')}\"}}]",
    } for c in collections], lazy=("CollectionRules",))

cls("SMS_FullCollectionMembership", ["CollectionID", "ResourceID"], P(
    ("CollectionID", S), ("ResourceID", I32), ("Name", S), ("Domain", S), ("IsClient", B), ("SiteCode", S),
    ("ResourceType", I32), ("IsDirect", B)),
    lambda: [{"CollectionID": c["CollectionID"], "ResourceID": d["rid"], "Name": d["name"], "Domain": DOMAIN,
              "IsClient": d["client"], "SiteCode": SITE, "ResourceType": 5, "IsDirect": c["CollectionID"] == "PS100016"}
             for c in collections for d in _members(c)], scope_key="ResourceID")

cls("SMS_G_System_OPERATING_SYSTEM", ["ResourceID", "GroupID"], P(
    ("ResourceID", I32), ("GroupID", I32), ("Caption", S), ("Version", S), ("BuildNumber", S), ("OSArchitecture", S),
    ("InstallDate", DTO), ("LastBootUpTime", DTO), ("TotalVisibleMemorySize", I64), ("FreePhysicalMemory", I64),
    ("SystemDirectory", S), ("TimeStamp", DTO)),
    lambda: [{"ResourceID": d["rid"], "GroupID": 1, "Caption": d["os"][0], "Version": d["os"][1], "BuildNumber": d["os"][2],
              "OSArchitecture": "64-bit", "InstallDate": days_ago(d["install"]), "LastBootUpTime": days_ago(d["boot"]),
              "TotalVisibleMemorySize": d["model"][2] * 1024 - 400000, "FreePhysicalMemory": rng.randint(1500000, 9000000),
              "SystemDirectory": "C:\\WINDOWS\\system32", "TimeStamp": days_ago(d["hinv"])}
             for d in devices if d["client"]], scope_key="ResourceID")

cls("SMS_G_System_COMPUTER_SYSTEM", ["ResourceID", "GroupID"], P(
    ("ResourceID", I32), ("GroupID", I32), ("Name", S), ("Manufacturer", S), ("Model", S), ("Domain", S),
    ("DomainRole", I32), ("NumberOfProcessors", I32), ("TotalPhysicalMemory", I64), ("UserName", S), ("TimeStamp", DTO)),
    lambda: [{"ResourceID": d["rid"], "GroupID": 1, "Name": d["name"], "Manufacturer": d["model"][0], "Model": d["model"][1],
              "Domain": "contoso.lab", "DomainRole": 1 if ws(d) else 3, "NumberOfProcessors": 1 if ws(d) else 2,
              "TotalPhysicalMemory": d["model"][2] * 1024, "UserName": f"{DOMAIN}\\{d['user']}" if d["last_active"] < 0.2 else None,
              "TimeStamp": days_ago(d["hinv"])} for d in devices if d["client"]], scope_key="ResourceID")

cls("SMS_G_System_PC_BIOS", ["ResourceID", "GroupID"], P(
    ("ResourceID", I32), ("GroupID", I32), ("Manufacturer", S), ("SMBIOSBIOSVersion", S), ("SerialNumber", S),
    ("ReleaseDate", DTO), ("TimeStamp", DTO)),
    lambda: [{"ResourceID": d["rid"], "GroupID": 1, "Manufacturer": d["model"][0], "SMBIOSBIOSVersion": d["bios"],
              "SerialNumber": d["serial"], "ReleaseDate": days_ago(200 + d["rid"] % 300), "TimeStamp": days_ago(d["hinv"])}
             for d in devices if d["client"]], scope_key="ResourceID")

cls("SMS_G_System_LOGICAL_DISK", ["ResourceID", "GroupID"], P(
    ("ResourceID", I32), ("GroupID", I32), ("DeviceID", S), ("DriveType", I32), ("FileSystem", S),
    ("Size", I64), ("FreeSpace", I64), ("VolumeName", S), ("TimeStamp", DTO)),
    lambda: [{"ResourceID": d["rid"], "GroupID": 1, "DeviceID": "C:", "DriveType": 3, "FileSystem": "NTFS",
              "Size": d["disk_size"], "FreeSpace": d["disk_free"], "VolumeName": "OS", "TimeStamp": days_ago(d["hinv"])}
             for d in devices if d["client"]], scope_key="ResourceID")


def _software_rows():
    out = []
    for d in devices:
        if not d["client"] or not ws(d):
            continue
        g = 0
        for product, publisher, versions, share in SOFTWARE:
            if rng.random() > share:
                continue
            g += 1
            version = rng.choices([v for v, _ in versions], weights=[w for _, w in versions])[0]
            out.append({"ResourceID": d["rid"], "GroupID": g, "ProductName": product, "ARPDisplayName": product,
                        "ProductVersion": version, "Publisher": publisher,
                        "InstallDate": (NOW - dt.timedelta(days=rng.randint(5, 600))).strftime("%Y%m%d"),
                        "InstalledLocation": f"C:\\Program Files\\{product.split(' ')[0]}", "SoftwareCode": "{" + f"{rng.getrandbits(128):032X}" + "}",
                        "TimeStamp": days_ago(d["hinv"])})
    return out


cls("SMS_G_System_INSTALLED_SOFTWARE", ["ResourceID", "GroupID"], P(
    ("ResourceID", I32), ("GroupID", I32), ("ProductName", S), ("ARPDisplayName", S), ("ProductVersion", S),
    ("Publisher", S), ("InstallDate", S), ("InstalledLocation", S), ("SoftwareCode", S), ("TimeStamp", DTO)),
    _software_rows, scope_key="ResourceID")

cls("SMS_ApplicationLatest", ["CI_ID"], P(
    ("CI_ID", I32), ("ModelName", S), ("LocalizedDisplayName", S), ("SoftwareVersion", S), ("Manufacturer", S),
    ("IsDeployed", B), ("IsExpired", B), ("IsLatest", B), ("NumberOfDeployments", I32), ("NumberOfDevicesWithApp", I32),
    ("NumberOfDevicesWithFailure", I32), ("NumberOfDeploymentTypes", I32), ("DateCreated", DTO), ("DateLastModified", DTO),
    ("CreatedBy", S), ("SDMPackageXML", S)),
    lambda: [{"CI_ID": ci, "ModelName": f"ScopeId_6A2D/Application_{ci:x}", "LocalizedDisplayName": n, "SoftwareVersion": v,
              "Manufacturer": p, "IsDeployed": ci != 16777545, "IsExpired": False, "IsLatest": True,
              "NumberOfDeployments": 0 if ci == 16777545 else 1 + (ci % 2), "NumberOfDevicesWithApp": 20 + (ci * 7) % 50,
              "NumberOfDevicesWithFailure": (ci * 3) % 7, "NumberOfDeploymentTypes": 1, "DateCreated": days_ago(300 - ci % 100),
              "DateLastModified": days_ago(ci % 60), "CreatedBy": f"{DOMAIN}\\cmadmin",
              "SDMPackageXML": f"<AppMgmtDigest><Application LogicalName=\"Application_{ci:x}\"><Title>{n}</Title></Application></AppMgmtDigest>"}
             for ci, n, v, p in APPS], lazy=("SDMPackageXML",))


def _deployments():
    out = []
    aid = 16777300
    plan = [("Google Chrome", "SMSDM003", 1, 1), ("Microsoft 365 Apps for enterprise", "SMSDM003", 1, 1),
            ("Zoom Workplace", "PS100010", 2, 1), ("Zoom Workplace", "PS100011", 2, 1), ("Adobe Acrobat Reader", "SMSDM003", 2, 1),
            ("7-Zip", "PS100012", 2, 1), ("Slack", "PS100016", 1, 1),
            ("Software Updates - 2024-10 Patch Tuesday (Pilot)", "PS100016", 1, 5),
            ("Software Updates - 2024-10 Patch Tuesday (Production)", "SMSDM003", 1, 5),
            ("Security Baseline - Windows 11", "PS100015", 1, 6)]
    coll = {c["CollectionID"]: c for c in collections}
    for name, cid, intent, feature in plan:
        aid += 1
        n = len(_members(coll[cid]))
        err = int(n * (0.12 if "Production" in name else 0.04))
        prog = int(n * (0.18 if "Production" in name else 0.05))
        unk = int(n * 0.06)
        out.append({"AssignmentID": aid, "DeploymentID": f"{SITE}2{aid % 100000:05d}", "SoftwareName": name,
                    "CollectionID": cid, "CollectionName": coll[cid]["Name"], "FeatureType": feature, "DeploymentIntent": intent,
                    "NumberTargeted": n, "NumberSuccess": n - err - prog - unk, "NumberInProgress": prog, "NumberErrors": err,
                    "NumberUnknown": unk, "NumberOther": 0, "DeploymentTime": days_ago(20 if feature != 5 else 14),
                    "EnforcementDeadline": days_ago(10 if "Production" in name else 13) if intent == 1 else None,
                    "SummarizationTime": days_ago(0.1)})
    return out


cls("SMS_DeploymentSummary", ["AssignmentID"], P(
    ("AssignmentID", I32), ("DeploymentID", S), ("SoftwareName", S), ("CollectionID", S), ("CollectionName", S),
    ("FeatureType", I32), ("DeploymentIntent", I32), ("NumberTargeted", I32), ("NumberSuccess", I32),
    ("NumberInProgress", I32), ("NumberErrors", I32), ("NumberUnknown", I32), ("NumberOther", I32),
    ("DeploymentTime", DTO), ("EnforcementDeadline", DTO), ("SummarizationTime", DTO)), _deployments)


def _app_asset_details():
    out = []
    coll = {c["CollectionID"]: c for c in collections}
    for dep in CLASSES["SMS_DeploymentSummary"]["rows"]():
        if dep["FeatureType"] != 1:
            continue
        for d in _members(coll[dep["CollectionID"]]):
            r = rng.random()
            status = 1 if r < 0.82 else 2 if r < 0.9 else 5 if r < 0.96 else 4
            out.append({"AppName": dep["SoftwareName"], "AssignmentID": dep["AssignmentID"], "CollectionID": dep["CollectionID"],
                        "CollectionName": dep["CollectionName"], "MachineID": d["rid"], "MachineName": d["name"],
                        "AppStatusType": status, "EnforcementState": {1: 1000, 2: 2000, 5: 5000, 4: 0}[status],
                        "ErrorCode": -2016410860 if status == 5 else 0, "StatusTime": days_ago(rng.uniform(0.1, 10))})
    return out


cls("SMS_AppDeploymentAssetDetails", ["AssignmentID", "MachineID"], P(
    ("AppName", S), ("AssignmentID", I32), ("CollectionID", S), ("CollectionName", S), ("MachineID", I32),
    ("MachineName", S), ("AppStatusType", I32), ("EnforcementState", I32), ("ErrorCode", I64), ("StatusTime", DTO)),
    _app_asset_details, scope_key="MachineID")


def _compliance():
    out = []
    for idx, (art, *_rest) in enumerate(UPDATES):
        for d in devices:
            if not d["client"]:
                continue
            if not update_applies(art, d):
                status = 1
            else:
                missing_p = 0.05 if art in ("2267602", "890830") else (0.35 if d["office"] == "TelAviv" else 0.15)
                status = 2 if rng.random() < missing_p else 3
            out.append({"CI_ID": 16790000 + idx, "MachineID": d["rid"], "Status": status,
                        "LastStatusCheckTime": days_ago(d["hinv"]), "LastEnforcementMessageID": 0 if status != 2 else 9})
    return out


cls("SMS_UpdateComplianceStatus", ["CI_ID", "MachineID"], P(
    ("CI_ID", I32), ("MachineID", I32), ("Status", I32), ("LastStatusCheckTime", DTO), ("LastEnforcementMessageID", I32)),
    _compliance, scope_key="MachineID")


def _updates():
    comp = CLASSES["SMS_UpdateComplianceStatus"]["rows"]()
    out = []
    for idx, (art, title, sev, posted, superseded) in enumerate(UPDATES):
        ci = 16790000 + idx
        rows = [r for r in comp if r["CI_ID"] == ci]
        out.append({"CI_ID": ci, "ArticleID": art, "BulletinID": "", "LocalizedDisplayName": title, "Severity": sev,
                    "SeverityName": {10: "Critical", 8: "Important", 6: "Moderate", 2: "Low"}[sev], "DatePosted": days_ago(posted),
                    "IsDeployed": not superseded, "IsExpired": False, "IsSuperseded": superseded,
                    "NumMissing": sum(r["Status"] == 2 for r in rows), "NumPresent": sum(r["Status"] == 3 for r in rows),
                    "NumNotApplicable": sum(r["Status"] == 1 for r in rows), "NumUnknown": 0, "NumTotal": len(rows),
                    "LocalizedCategoryInstanceNames": ["Security Updates" if sev >= 8 else "Definition Updates",
                                                       "Windows 11" if "Windows 11" in title else "Windows 10" if "Windows 10" in title else "Microsoft Server operating system-21H2" if "server" in title else "Microsoft Defender Antivirus" if "Defender" in title else "Office 2016" if "Office" in title else "Windows"]})
    return out


cls("SMS_SoftwareUpdate", ["CI_ID"], P(
    ("CI_ID", I32), ("ArticleID", S), ("BulletinID", S), ("LocalizedDisplayName", S), ("Severity", I32),
    ("SeverityName", S), ("DatePosted", DTO), ("IsDeployed", B), ("IsExpired", B), ("IsSuperseded", B),
    ("NumMissing", I32), ("NumPresent", I32), ("NumNotApplicable", I32), ("NumUnknown", I32), ("NumTotal", I32),
    ("LocalizedCategoryInstanceNames", LS)), _updates)

cls("SMS_Package", ["PackageID"], P(("PackageID", S), ("Name", S), ("Version", S), ("Manufacturer", S),
                                     ("PkgSourcePath", S), ("PackageType", I32), ("SourceDate", DTO), ("LastRefreshTime", DTO)),
    lambda: [{"PackageID": f"{SITE}00000{i}", "Name": n, "Version": v, "Manufacturer": m,
              "PkgSourcePath": f"\\\\CM01\\Sources\\Packages\\{n.replace(' ', '')}", "PackageType": 0,
              "SourceDate": days_ago(100 + i), "LastRefreshTime": days_ago(10 + i)}
             for i, (n, v, m) in enumerate([("Configuration Manager Client Package", "5.00.9132", "Microsoft"),
                                            ("User State Migration Tool for Windows", "10.0.26100", "Microsoft"),
                                            ("Configuration Manager Client Piloting Package", "5.00.9132", "Microsoft"),
                                            ("Dell Command Update", "5.4.0", "Dell")], start=1)])

cls("SMS_Boundary", ["BoundaryID"], P(("BoundaryID", I32), ("DisplayName", S), ("BoundaryType", I32), ("Value", S),
                                       ("GroupCount", I32), ("SiteSystems", LS)),
    lambda: [{"BoundaryID": 16777216 + i, "DisplayName": f"{o} office", "BoundaryType": 3, "Value": f"{n}.0.1-{n}.255.254",
              "GroupCount": 1, "SiteSystems": ["CM01.contoso.lab" if o == "London" else "DP01.contoso.lab"]}
             for i, (o, _c, n) in enumerate(OFFICES, start=1)])

cls("SMS_BoundaryGroup", ["GroupID"], P(("GroupID", I32), ("Name", S), ("MemberCount", I32), ("SiteSystemCount", I32),
                                         ("DefaultSiteCode", S), ("Description", S)),
    lambda: [{"GroupID": 16777216 + i, "Name": f"BG - {o}", "MemberCount": 1, "SiteSystemCount": 1, "DefaultSiteCode": SITE,
              "Description": f"Content and site assignment for {o}"} for i, (o, _c, _n) in enumerate(OFFICES, start=1)])

cls("SMS_G_System_PROCESSOR", ["ResourceID", "GroupID"], P(("ResourceID", I32), ("GroupID", I32), ("Name", S),
                                                            ("NumberOfCores", I32), ("NumberOfLogicalProcessors", I32), ("MaxClockSpeed", I32)),
    lambda: [{"ResourceID": d["rid"], "GroupID": 1, "Name": "Intel(R) Core(TM) i7-1365U" if ws(d) else "Intel(R) Xeon(R) Gold 6338",
              "NumberOfCores": 10 if ws(d) else 4, "NumberOfLogicalProcessors": 12 if ws(d) else 8, "MaxClockSpeed": 1800 if ws(d) else 2000}
             for d in devices if d["client"]], scope_key="ResourceID")

# Classes that exist on every site but carry nothing interesting here; they make
# "discover all" look like a real provider (hundreds of classes).
for filler in ["SMS_Admin", "SMS_Role", "SMS_SecuredCategory", "SMS_Advertisement", "SMS_TaskSequencePackage",
               "SMS_DistributionPoint", "SMS_DistributionPointInfo", "SMS_ConfigurationItem", "SMS_ConfigurationBaselineInfo",
               "SMS_SCI_SysResUse", "SMS_SiteSystemSummarizer", "SMS_ComponentSummarizer", "SMS_StatusMessage",
               "SMS_G_System_SYSTEM_ENCLOSURE", "SMS_G_System_NETWORK_ADAPTER_CONFIGURATION", "SMS_G_System_VIDEO_CONTROLLER",
               "SMS_G_System_SERVICE", "SMS_G_System_TPM", "SMS_G_System_FIRMWARE", "SMS_ClientHealthSummary",
               "SMS_MaintenanceWindow", "SMS_DriverPackage", "SMS_ImagePackage", "SMS_BootImagePackage", "SMS_SoftwareUpdateGroup"]:
    cls(filler, ["ID"], P(("ID", I32), ("Name", S), ("DateCreated", DTO)), lambda: [])


# v1.0 route (case-sensitive names)
V1 = {
    "Device": {"key": ["MachineId"], "props": P(("MachineId", I64), ("Name", S), ("Domain", S), ("ClientVersion", S),
                                                 ("DeviceOS", S), ("DeviceOSBuild", S), ("IsClient", B), ("IsActive", B),
                                                 ("LastActiveTime", DTO), ("PrimaryUser", S), ("SiteCode", S)),
               "lazy": (), "scope_key": "MachineId", "_rows": None,
               "rows_fn": lambda: [{"MachineId": d["rid"], "Name": d["name"], "Domain": DOMAIN,
                                    "ClientVersion": "5.00.9132.1011" if d["client"] else None, "DeviceOS": d["os"][0],
                                    "DeviceOSBuild": d["os"][1], "IsClient": d["client"], "IsActive": d["last_active"] < 14,
                                    "LastActiveTime": days_ago(d["last_active"]), "PrimaryUser": f"{DOMAIN.lower()}\\{d['user']}",
                                    "SiteCode": SITE if d["client"] else None} for d in devices]},
}


def _rows_of(c: dict) -> list[dict]:
    if c["_rows"] is None:
        c["_rows"] = c["rows_fn"]()
    return c["_rows"]


for _c in list(CLASSES.values()) + list(V1.values()):
    _c["rows"] = (lambda c=_c: _rows_of(c))
# Materialize in dependency order once so randomness is stable across restarts.
for _name in CLASSES:
    CLASSES[_name]["rows"]()
for _c in V1.values():
    _c["rows"]()

LONDON_IDS = {d["rid"] for d in devices if d["office"] == "London"}


# ── OData $filter ───────────────────────────────────────────────────────────

class ODataError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


_TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)|
    (?P<str>'(?:[^']|'')*')|
    (?P<dt>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2}))|
    (?P<num>-?\d+(?:\.\d+)?)|
    (?P<id>[A-Za-z_][A-Za-z0-9_]*)|
    (?P<lp>\()|(?P<rp>\))|(?P<comma>,)
""", re.VERBOSE)


def tokenize(text: str) -> list[tuple[str, Any]]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise ODataError(f"Syntax error at position {pos} in '{text}'.")
        pos = m.end()
        kind = m.lastgroup
        val = m.group(kind)
        if kind == "ws":
            continue
        if kind == "str":
            out.append(("lit", val[1:-1].replace("''", "'")))
        elif kind == "dt":
            out.append(("lit", iso(dt.datetime.fromisoformat(val.replace("Z", "+00:00")))))
        elif kind == "num":
            out.append(("lit", float(val) if "." in val else int(val)))
        else:
            out.append((kind, val))
    return out


FUNCS = {
    "startswith": lambda a, b: isinstance(a, str) and a.lower().startswith(str(b).lower()),
    "endswith": lambda a, b: isinstance(a, str) and a.lower().endswith(str(b).lower()),
    "contains": lambda a, b: isinstance(a, str) and str(b).lower() in a.lower(),
    "tolower": lambda a: a.lower() if isinstance(a, str) else a,
    "toupper": lambda a: a.upper() if isinstance(a, str) else a,
}
CMP = {"eq": lambda a, b: a == b, "ne": lambda a, b: a != b, "gt": lambda a, b: a is not None and b is not None and a > b,
       "ge": lambda a, b: a is not None and b is not None and a >= b, "lt": lambda a, b: a is not None and b is not None and a < b,
       "le": lambda a, b: a is not None and b is not None and a <= b}


class FilterParser:
    def __init__(self, text: str, props: list[str], type_name: str):
        self.toks = tokenize(text)
        self.i = 0
        self.props = {p.lower(): p for p in props}
        self.type_name = type_name

    def peek(self, kind=None, val=None):
        if self.i >= len(self.toks):
            return None
        t = self.toks[self.i]
        if kind and t[0] != kind:
            return None
        if val and (t[0] != "id" or t[1].lower() != val):
            return None
        return t

    def take(self):
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self):
        node = self.or_()
        if self.i != len(self.toks):
            raise ODataError(f"Syntax error: unexpected '{self.toks[self.i][1]}'.")
        return node

    def or_(self):
        node = self.and_()
        while self.peek("id", "or"):
            self.take()
            right = self.and_()
            node = (lambda lhs, rhs: lambda row: lhs(row) or rhs(row))(node, right)
        return node

    def and_(self):
        node = self.not_()
        while self.peek("id", "and"):
            self.take()
            right = self.not_()
            node = (lambda lhs, rhs: lambda row: lhs(row) and rhs(row))(node, right)
        return node

    def not_(self):
        if self.peek("id", "not"):
            self.take()
            inner = self.not_()
            return lambda row: not inner(row)
        return self.cmp()

    def cmp(self):
        left = self.value()
        t = self.peek("id")
        if t and t[1].lower() in CMP:
            op = CMP[self.take()[1].lower()]
            right = self.value()
            return lambda row: op(left(row), right(row))
        if t and t[1].lower() in ("in", "has", "any", "all"):
            raise ODataError(f"The operator '{t[1]}' is not supported.")
        return lambda row: bool(left(row))

    def value(self):
        t = self.peek()
        if t is None:
            raise ODataError("Syntax error: expression expected.")
        if t[0] == "lp":
            self.take()
            node = self.or_()
            if not self.peek("rp"):
                raise ODataError("Syntax error: ')' expected.")
            self.take()
            return node
        if t[0] == "lit":
            self.take()
            v = t[1]
            return lambda row: v
        if t[0] == "id":
            self.take()
            name = t[1]
            low = name.lower()
            if low in ("true", "false"):
                return lambda row: low == "true"
            if low == "null":
                return lambda row: None
            if self.peek("lp"):
                if low not in FUNCS:
                    raise ODataError(f"Unknown function '{name}'.")
                self.take()
                args = [self.value()]
                while self.peek("comma"):
                    self.take()
                    args.append(self.value())
                if not self.peek("rp"):
                    raise ODataError("Syntax error: ')' expected.")
                self.take()
                fn = FUNCS[low]
                return lambda row: fn(*[a(row) for a in args])
            if low not in self.props:
                raise ODataError(f"Could not find a property named '{name}' on type 'AdminService.{self.type_name}'.")
            prop = self.props[low]
            return lambda row: row.get(prop)
        raise ODataError(f"Syntax error near '{t[1]}'.")


# ── NTLM, connection-based (HTTP.sys style) ─────────────────────────────────

_conn_lock = threading.Lock()
_conns: dict[tuple[str, int], dict] = {}   # peer -> {"ctx", "user", "t"}


def _challenge(token: str | None = None, scheme: str = "NTLM") -> Response:
    body = b"<html><body>401 - Unauthorized</body></html>"
    if token:
        headers = [("WWW-Authenticate", f"{scheme} {token}")]
    else:
        headers = [("WWW-Authenticate", "Negotiate"), ("WWW-Authenticate", "NTLM")]
    resp = Response(content=body, status_code=401, media_type="text/html")
    for k, v in headers:
        resp.headers.append(k, v)
    return resp


def authenticate(request: Request) -> tuple[str | None, Response | None]:
    peer = tuple(request.scope.get("client") or ("?", 0))
    now = time.time()
    with _conn_lock:
        for k in [k for k, v in _conns.items() if now - v["t"] > 300]:
            _conns.pop(k, None)
        state = _conns.get(peer)
    header = request.headers.get("authorization") or ""
    scheme, _, b64 = header.partition(" ")
    if scheme.lower() in ("ntlm", "negotiate") and b64:
        try:
            token = base64.b64decode(b64)
        except Exception:
            return None, _challenge()
        if token[:8] == b"NTLMSSP\x00" and token[8:12] == b"\x01\x00\x00\x00":
            ctx = spnego.server(protocol="ntlm")
            out = ctx.step(token)
            with _conn_lock:
                _conns[peer] = {"ctx": ctx, "user": None, "t": now}
            return None, _challenge(base64.b64encode(out).decode(), scheme)
        if state and state.get("ctx") is not None and state["user"] is None:
            try:
                state["ctx"].step(token)
            except Exception:
                with _conn_lock:
                    _conns.pop(peer, None)
                return None, _challenge()
            user = str(state["ctx"].client_principal or "")
            if "@" in user and "\\" not in user:
                user = f"{DOMAIN}\\{user.split('@')[0]}"
            state.update(user=user.upper(), t=now)
            return state["user"], None
        return None, _challenge()
    if state and state.get("user"):
        state["t"] = now
        return state["user"], None
    return None, _challenge()


# ── app ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="Mock Configuration Manager AdminService")
BASE = "/AdminService"

# Test knobs (set from tests; env for manual runs).
REQUEST_LOG: list[dict] = []                                  # every authenticated request
REJECT_TOP = os.environ.get("MECM_MOCK_REJECT_TOP") == "1"    # a route that does not implement $top
# Host the nextLink names: unset = this server, "https://other.example" = an alias /
# hostile host, "relative" = a link relative to the collection URL.
NEXTLINK_ORIGIN = os.environ.get("MECM_MOCK_NEXTLINK_ORIGIN")


def odata_error(status: int, message: str, code: str = "") -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code or str(status), "message": message}})


def metadata_xml(classes: dict[str, dict]) -> str:
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">',
             '<edmx:DataServices><Schema Namespace="AdminService" xmlns="http://docs.oasis-open.org/odata/ns/edm">']
    for name, c in classes.items():
        parts.append(f'<EntityType Name="{name}"><Key>' + "".join(f'<PropertyRef Name="{k}"/>' for k in c["key"]) + "</Key>")
        for p, t in c["props"]:
            nullable = ' Nullable="false"' if p in c["key"] else ""
            parts.append(f'<Property Name="{p}" Type="{t}"{nullable}/>')
        parts.append("</EntityType>")
    parts.append('<EntityContainer Name="Container">')
    for name in classes:
        parts.append(f'<EntitySet Name="{name}" EntityType="AdminService.{name}"/>')
    parts.append("</EntityContainer></Schema></edmx:DataServices></edmx:Edmx>")
    return "".join(parts)


def instance(name: str, c: dict, row: dict, single: bool, route: str) -> dict:
    keyval = ",".join(f"{k}={row.get(k)!r}" for k in c["key"])
    out = {"@odata.etag": f'W/"{abs(hash((name, keyval))) % 10**8}"'}
    if route == "wmi":
        out.update({"__GENUS": 2, "__CLASS": name, "__SUPERCLASS": None, "__DYNASTY": name,
                    "__RELPATH": f"{name}.{keyval}", "__PROPERTY_COUNT": len(c["props"]), "__DERIVATION": [],
                    "__SERVER": "CM01", "__NAMESPACE": f"root\\sms\\site_{SITE}",
                    "__PATH": f"\\\\CM01\\root\\sms\\site_{SITE}:{name}.{keyval}",
                    "__LAZYPROPERTIES": list(c["lazy"])})
    for p, _t in c["props"]:
        if p in c["lazy"] and not single:
            continue
        out[p] = row.get(p)
    return out


def scoped(c: dict, rows: list[dict], scope: str) -> list[dict]:
    if scope == "all" or not c.get("scope_key"):
        return rows
    return [r for r in rows if r.get(c["scope_key"]) in LONDON_IDS]


def parse_key(raw: str, c: dict) -> dict:
    raw = raw.strip()
    if "=" in raw:
        out = {}
        for part in raw.split(","):
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip().strip("'")
        return out
    return {c["key"][0]: raw.strip("'")}


@app.api_route(BASE + "/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def adminservice(rest: str, request: Request):
    user, challenge = authenticate(request)
    if challenge is not None:
        return challenge
    scope = USERS.get(user)
    if user not in USERS or scope is None:
        print(f"[mecm-mock] 403 {user} {request.method} {request.url.path}", flush=True)
        return odata_error(403, f"User {user} is not an administrative user of Configuration Manager.", "Forbidden")
    if request.method != "GET":
        REQUEST_LOG.append({"user": user, "method": request.method, "path": rest, "query": request.url.query})
        return odata_error(405, "The requested resource does not support the HTTP method.", "MethodNotAllowed")
    print(f"[mecm-mock] {user} GET /{rest}?{request.url.query}", flush=True)
    REQUEST_LOG.append({"user": user, "method": request.method, "path": rest, "query": request.url.query})

    segs = [s for s in rest.split("/") if s]
    if len(segs) < 2 or segs[0].lower() not in ("wmi", "v1.0"):
        return odata_error(404, "No HTTP resource was found that matches the request URI.", "NotFound")
    route = segs[0].lower()
    registry = CLASSES if route == "wmi" else V1
    if segs[1] == "$metadata":
        return Response(content=metadata_xml(registry), media_type="application/xml")
    if len(segs) > 2:
        return odata_error(400, "Navigation and $count segments are not supported on this resource.", "BadRequest")
    head = segs[1]
    m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)(?:\((.*)\))?$", head)
    if not m:
        return odata_error(404, f"Resource '{head}' not found.", "NotFound")
    cname, key = m.group(1), m.group(2)
    if route == "wmi":
        match = next((n for n in registry if n.lower() == cname.lower()), None)
    else:
        match = cname if cname in registry else None  # case-sensitive
    if not match:
        return odata_error(404, f"The class '{cname}' was not found.", "NotFound")
    c = registry[match]
    rows = scoped(c, c["rows"](), scope)
    params = dict(parse_qsl(request.url.query, keep_blank_values=True))
    for opt in params:
        if opt.startswith("$") and opt not in ("$filter", "$select", "$orderby", "$top", "$skip", "$count"):
            return odata_error(400, f"The query parameter '{opt}' is not supported.", "BadRequest")
    if REJECT_TOP and "$top" in params:
        return odata_error(400, "The query parameter '$top' is not supported.", "BadRequest")
    prop_names = [p for p, _ in c["props"]]

    if key is not None:
        want = parse_key(key, c)
        hit = next((r for r in rows if all(str(r.get(k)) == v for k, v in want.items())), None)
        if hit is None:
            return odata_error(404, f"{match}({key}) not found.", "NotFound")
        body = {"@odata.context": f"https://{request.url.netloc}{BASE}/{route}/$metadata#{match}/$entity"}
        body.update(instance(match, c, hit, True, route))
        return JSONResponse(body)

    try:
        if params.get("$filter"):
            pred = FilterParser(params["$filter"], prop_names, match).parse()
            rows = [r for r in rows if pred(r)]
        if params.get("$orderby"):
            for spec in reversed([s.strip() for s in params["$orderby"].split(",") if s.strip()]):
                field, _, direction = spec.partition(" ")
                prop = next((p for p in prop_names if p.lower() == field.lower()), None)
                if prop is None:
                    raise ODataError(f"Could not find a property named '{field}' on type 'AdminService.{match}'.")
                rows = sorted(rows, key=lambda r: (r.get(prop) is None, r.get(prop) if r.get(prop) is not None else 0),
                              reverse=direction.strip().lower() == "desc")
        select = None
        if params.get("$select"):
            select = [s.strip() for s in params["$select"].split(",") if s.strip()]
            for s in select:
                if s.lower() not in {p.lower() for p in prop_names}:
                    raise ODataError(f"Could not find a property named '{s}' on type 'AdminService.{match}'.")
        total = len(rows)
        skip = int(params.get("$skip") or 0)
        top = int(params["$top"]) if params.get("$top") else None
    except ODataError as e:
        return odata_error(e.status, str(e), "BadRequest")
    except ValueError as e:
        return odata_error(400, f"Invalid query option value: {e}", "BadRequest")

    window = rows[skip:]
    if top is not None:
        window = window[:top]
    page = window[:PAGE_SIZE]
    body: dict[str, Any] = {"@odata.context": f"https://{request.url.netloc}{BASE}/{route}/$metadata#{match}"}
    if params.get("$count", "").lower() == "true":
        body["@odata.count"] = total
    out_rows = []
    for r in page:
        inst = instance(match, c, r, False, route)
        if select:
            sel = {p.lower() for p in select}
            inst = {k: v for k, v in inst.items() if k.startswith(("@", "__")) or k.lower() in sel}
        out_rows.append(inst)
    body["value"] = out_rows
    if len(window) > PAGE_SIZE:
        nxt = dict(params)
        nxt["$skip"] = str(skip + PAGE_SIZE)
        if top is not None:
            nxt["$top"] = str(top - PAGE_SIZE)
        query = urlencode(nxt, quote_via=quote, safe="$,'()")
        if NEXTLINK_ORIGIN == "relative":
            body["@odata.nextLink"] = f"{match}?{query}"   # relative to the collection URL
        else:
            origin = NEXTLINK_ORIGIN or f"https://{request.url.netloc}"
            body["@odata.nextLink"] = f"{origin}{BASE}/{route}/{match}?{query}"
    return JSONResponse(body)


# ── TLS: a self-signed certificate, like the site binds by default ──────────

def ensure_cert(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory / "adminservice.pem", directory / "adminservice.key"
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    pk = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CM01.contoso.lab")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(pk.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.DNSName("CM01.contoso.lab"),
                                                    x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(pk, hashes.SHA256())
    )
    key.write_bytes(pk.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                     serialization.NoEncryption()))
    cert.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert, key


if __name__ == "__main__":
    import uvicorn

    cert, key = ensure_cert(Path(os.environ.get("MECM_MOCK_CERT_DIR", HERE / ".certs")))
    if "--print-cert" in sys.argv:
        print(cert.read_text())
        sys.exit(0)
    print(f"[mecm-mock] https://localhost:{PORT}{BASE}  cert: {cert}  devices={len(devices)} "
          f"classes={len(CLASSES)} page_size={PAGE_SIZE}", flush=True)
    uvicorn.run(app, host=os.environ.get("MECM_MOCK_HOST", "127.0.0.1"), port=PORT,
                ssl_certfile=str(cert), ssl_keyfile=str(key), log_level="warning")
