"""Mock DX NetOps Spectrum OneClick Web Services API (24.3 shapes).

A real OneClick/SpectroSERVER cannot run here (Broadcom entitlement, licensed
Docker tarballs from support.broadcom.com), so — like
tools/aria_operations/mock_suite_api.py — this serves the exact surface
`SpectrumClient` touches, with response bodies shaped after the XML that
Broadcom TechDocs reproduces verbatim for each noun
(docs/research/broadcom-spectrum-connector.md §2.1):

  GET  /spectrum/restful/landscapes
  GET  /spectrum/restful/devices?attr=&landscape=&throttlesize=&start=
  POST /spectrum/restful/models            (rs:model-request, GET tunnelling)
  GET  /spectrum/restful/models?id=<result-set>&start=&throttlesize=
  GET  /spectrum/restful/model/{mh}?attr=
  GET  /spectrum/restful/alarms?attr=&landscape=&throttlesize=&start=
  POST /spectrum/restful/alarms            (rs:alarm-request)
  POST /spectrum/restful/alarms/count      (rs:alarms-count-request)
  GET  /spectrum/restful/alarms/filters
  POST /spectrum/restful/events/getEvents  (rs:get-event-request)
  GET  /spectrum/restful/associations/relation/{rel}/model/{mh}?side=
  GET  /spectrum/restful/connectivity/{ip}
  GET  /spectrum/restful/attribute/{id}/enums
  GET  /spectrum/restful/action/{code}?mh=&attr=&val=   (read-only codes only)
  GET  /spectrum/restful/swagger.json, /api-docs          (the UI's "API Documentation")
  PUT/DELETE on alarms, model                              (403 for read-only users)

Faithfulness rules enforced on purpose:
  - Every call needs `Authorization: Basic <local user>` or
    `Authorization: Bearer <token minted in Token Configuration>`; anything
    else is a 401. The read-only users (`bow_api`) get 403 on PUT/DELETE —
    OneClick admins can restrict REST per user to GET/POST.
  - Request bodies are XML only (`Content-Type: application/xml`); JSON
    bodies are a 415. Responses are XML unless `Accept: application/json`.
  - The default throttle is 1000 items (25.4 KB); a page carries
    `throttle`, `total-*` and either `error="EndOfResults"` or a
    `<link rel="next" href="…"/>` (models: `?id=<result-set>&start=N`;
    devices/alarms/events: `?start=N`). POST result sets expire after
    ten minutes.
  - Attribute values are strings; dates (0x11f4e, 0x1321a) are epoch
    SECONDS; `attr` ids are hex strings exactly as the docs spell them.

ASSUMPTIONS (undocumented; confirm on the first live OneClick and fix here):
  1. JSON rendering of the XML: Jersey "mapped" notation (`@attr`, `$` text,
     repeated children as arrays) — the client never relies on it.
  2. The `next` link spelling (`start` offset + the models `id` result set);
     a GET-tunnelled search (events/getEvents) pages by re-POSTing the same
     body to the `next` href.
  3. `Authorization: Bearer <jwt>` for 24.3.10+ tokens.
  4. Severity enum 0 Normal/Maintenance, 1 Minor, 2 Major, 3 Critical
     (2/3 = Major/Critical per KB 194661; served by /attribute/0x11f56/enums).
  5. Relation handles: Collects = 0x10002 (one informal source); the
     interface relation handle is SPECTRUM_MOCK_IF_RELATION (default
     0x10006, invented — the client takes it from config).

Seeded estate: two landscapes (dc1 primary, dc2), 13 devices, 36 interfaces,
two global collections, a connectivity graph and ONE deterministic incident
(see SCENARIO): at T0 the WAN uplink `rtr-core-01_Gi0/1` goes down; the
firewall behind it stops answering polls (cause 0x10009) and the two access
switches behind the firewall are raised as impact symptoms; fifteen minutes
earlier that same interface already had a Major utilisation alarm. A Minor
alarm in dc2 is unrelated noise. `GET /__mock/scenario` returns T0.

Run:  uv run --project backend uvicorn mock_oneclick:app --port 8880
      (from this directory; or via docker-compose.yaml)
Creds: bow_api / Spectrum!2024   (read-only: GET+POST)
       spectrum / spectrum        (admin: all verbs)
       Bearer: SPECTRUM_MOCK_TOKEN (default below) or POST /__mock/token
"""
import base64
import os
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

RS = "http://www.ca.com/spectrum/restful/schema/request"
FILTER = "http://www.ca.com/spectrum/restful/schema/filter"
RESPONSE = "http://www.ca.com/spectrum/restful/schema/response"
DEFAULT_THROTTLE = 1000
RESULT_SET_TTL = 600            # seconds — "expires after ten minutes of inactivity"
IF_RELATION = os.environ.get("SPECTRUM_MOCK_IF_RELATION", "0x10006")
COLLECTS = "0x10002"
STATIC_TOKEN = os.environ.get(
    "SPECTRUM_MOCK_TOKEN",
    "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJib3dfYXBpIiwiYXVkIjoiQmFnIG9mIFdvcmRzIn0.mock-signature",
)
SPECTRUM_VERSION = "24.3.10"
# The docs show the response namespace as the DEFAULT namespace (no prefix).
ET.register_namespace("", RESPONSE)

USERS = {"bow_api": ("Spectrum!2024", False), "spectrum": ("spectrum", True)}  # pw, is_admin
_tokens: Dict[str, str] = {STATIC_TOKEN: "bow_api"}
_result_sets: Dict[str, Tuple[float, List[dict], List[str]]] = {}   # id -> (expiry, models, attrs)

app = FastAPI(title="Mock DX NetOps Spectrum OneClick Web Services (24.3 shapes)")


# ── attribute dictionary (TechDocs "Alarm and Model Attributes" + KBs) ───────

ATTR_NAMES = {
    "0x10000": "Model_Type_Name", "0x10001": "Model_Type_Handle", "0x10009": "Security_String",
    "0x1000a": "Condition", "0x1006e": "Model_Name", "0x11ee8": "Model_Class",
    "0x11d42": "Landscape_Name", "0x129ac": "Landscape_Handle", "0x129fa": "Model_Handle",
    "0x12d7f": "Network_Address", "0x23000e": "Device_Type", "0x133f4": "System_Name",
    "0x23000c": "Contact_Person", "0x23000d": "Location", "0x1290c": "Criticality",
    "0x1295d": "isManaged", "0x12adb": "GlobalCollectionsList", "0x11564": "Notes",
    "0x10004": "Contact_Status", "0x10032": "sysUpTime",
    # alarm attributes
    "0x11f9c": "Alarm_ID", "0x11f56": "Severity", "0x11f4e": "Creation_Date", "0x1321a": "Last_Occurrence",
    "0x11f4d": "Acknowledged", "0x11f4f": "Alarm_Status", "0x11f50": "Cause_Code", "0x12b4c": "Alarm_Title",
    "0x11f53": "Alarmed_Model_Handle", "0x11f57": "Troubleshooter", "0x12022": "Trouble_Ticket_ID",
    "0x11fc5": "Occurrences", "0x11fc4": "Alarm_Source", "0x11f54": "Primary_Alarm",
    "0x1296e": "Originating_Event", "0x12a04": "Symptom_List", "0x12a05": "Cause_List",
    "0x12a06": "Symptom_Count", "0x12a07": "Cause_Count", "0x12a63": "Web_Context_URL",
    "0x11f9b": "User_Clearable",
    # event attributes
    "0x4820007": "Event_Text",
}
ENUMS = {
    "0x11ee8": {"0": "Unknown", "1": "Other", "2": "Switch", "3": "Router", "4": "Hub", "5": "Workstation",
                "6": "Printer", "7": "Server", "8": "Switch-Router", "9": "Firewall", "10": "Wireless",
                "20": "Interface", "30": "Container"},
    "0x1000a": {"0": "Normal", "1": "Initial", "2": "Maintenance", "3": "Suppressed", "4": "Minor",
                "5": "Major", "6": "Critical"},
    "0x11f56": {"0": "Normal", "1": "Minor", "2": "Major", "3": "Critical", "4": "Suppressed",
                "5": "Maintenance"},
    "0x10004": {"0": "Lost", "1": "Established", "2": "Initial"},
    "0x11fc4": {"0": "Current", "1": "Residual"},
    "0x1290c": {"0": "Low", "5": "Medium", "10": "High"},
}


# ── scenario clock ───────────────────────────────────────────────────────────

def _incident_start_ms() -> int:
    env = os.environ.get("SPECTRUM_MOCK_INCIDENT_START")
    if env:
        return int(env)
    now = datetime.now(timezone.utc)
    t0 = now.replace(hour=3, minute=20, second=0, microsecond=0)
    if now - t0 < timedelta(hours=2):
        t0 -= timedelta(days=1)
    return int(t0.timestamp() * 1000)


T0 = _incident_start_ms()
MIN = 60_000
SCENARIO = {
    "incident_start_ms": T0,
    "root_cause": "WAN uplink rtr-core-01_Gi0/1 went down (LINK DOWN 0x10d35) after 15 minutes of "
                  "saturation; fw-edge-01 stopped answering polls and sw-acc-11/sw-acc-12 lost upstream.",
    "root_cause_alarm": "0x1000003",
}


# ── estate ───────────────────────────────────────────────────────────────────

LANDSCAPES = [
    {"id": "0x100000", "name": "ss-dc1.corp.local", "isPrimary": "true", "spectrumVersion": SPECTRUM_VERSION},
    {"id": "0x200000", "name": "ss-dc2.corp.local", "isPrimary": "false", "spectrumVersion": SPECTRUM_VERSION},
]
LS_NAME = {l["id"]: l["name"] for l in LANDSCAPES}

# (mh, name, model type name, mtype handle, class, ip, device type, location, contact, landscape, gc list, criticality)
_DEVICES = [
    ("0x100010", "rtr-core-01", "Rtr_Cisco", "0x21000c", "3", "10.1.0.1", "Cisco ASR 1001-X", "DC1 / Row A", "noc@corp.local", "0x100000", "Core Network", "10"),
    ("0x100011", "rtr-core-02", "Rtr_Cisco", "0x21000c", "3", "10.1.0.2", "Cisco ASR 1001-X", "DC1 / Row A", "noc@corp.local", "0x100000", "Core Network", "10"),
    ("0x100012", "sw-dist-01", "SwCiscoIOS", "0x210003", "8", "10.1.1.1", "Cisco Catalyst 9500", "DC1 / Row B", "noc@corp.local", "0x100000", "Core Network", "5"),
    ("0x100013", "sw-dist-02", "SwCiscoIOS", "0x210003", "8", "10.1.1.2", "Cisco Catalyst 9500", "DC1 / Row B", "noc@corp.local", "0x100000", "Core Network", "5"),
    ("0x100014", "fw-edge-01", "GnSNMPDev", "0x3b10002", "9", "10.1.2.1", "Palo Alto PA-3260", "DC1 / Edge", "secops@corp.local", "0x100000", "Edge", "10"),
    ("0x100015", "sw-acc-11", "SwCiscoIOS", "0x210003", "2", "10.1.3.11", "Cisco Catalyst 9300", "DC1 / Floor 1", "helpdesk@corp.local", "0x100000", "Access Layer", "0"),
    ("0x100016", "sw-acc-12", "SwCiscoIOS", "0x210003", "2", "10.1.3.12", "Cisco Catalyst 9300", "DC1 / Floor 2", "helpdesk@corp.local", "0x100000", "Access Layer", "0"),
    ("0x100017", "wlc-01", "GnSNMPDev", "0x3b10002", "10", "10.1.4.1", "Cisco 9800 WLC", "DC1 / Row C", "helpdesk@corp.local", "0x100000", "Access Layer", "5"),
    ("0x100018", "srv-esx-01", "Host_Device", "0x1040001", "7", "10.1.5.1", "VMware ESXi host", "DC1 / Row D", "virt@corp.local", "0x100000", "Compute", "5"),
    ("0x200010", "rtr-core-11", "JuniperJUNOSRtr", "0x4b00001", "3", "10.2.0.1", "Juniper MX204", "DC2 / Row A", "noc@corp.local", "0x200000", "Core Network", "10"),
    ("0x200012", "sw-dist-11", "SwCiscoIOS", "0x210003", "8", "10.2.1.1", "Cisco Catalyst 9500", "DC2 / Row B", "noc@corp.local", "0x200000", "Core Network", "5"),
    ("0x200015", "sw-acc-21", "SwCiscoIOS", "0x210003", "2", "10.2.3.21", "Cisco Catalyst 9300", "DC2 / Floor 1", "helpdesk@corp.local", "0x200000", "Access Layer", "0"),
    ("0x200016", "sw-acc-22", "SwCiscoIOS", "0x210003", "2", "10.2.3.22", "Cisco Catalyst 9300", "DC2 / Floor 2", "helpdesk@corp.local", "0x200000", "Access Layer", "0"),
]

# Condition (0x1000a) and contact status (0x10004) per device — the incident
# leaves fw-edge-01 Critical/Lost and the two access switches Suppressed.
_CONDITION = {"0x100014": ("6", "0"), "0x100015": ("3", "1"), "0x100016": ("3", "1"), "0x100010": ("5", "1")}

MODELS: Dict[str, dict] = {}


def _add_model(mh: str, attrs: dict) -> None:
    attrs = dict(attrs)
    attrs["0x129fa"] = mh
    attrs["0x11d42"] = LS_NAME[attrs["0x129ac"]]
    MODELS[mh] = attrs


for mh, name, mtype, mth, cls, ip, dtype, loc, contact, ls, gc, crit in _DEVICES:
    cond, contact_status = _CONDITION.get(mh, ("0", "1"))
    _add_model(mh, {
        "0x1006e": name, "0x10000": mtype, "0x10001": mth, "0x11ee8": cls, "0x12d7f": ip,
        "0x23000e": dtype, "0x23000d": loc, "0x23000c": contact, "0x129ac": ls, "0x12adb": gc,
        "0x1290c": crit, "0x1295d": "true", "0x1000a": cond, "0x10004": contact_status,
        "0x133f4": f"{name}.corp.local", "0x10032": "9123456", "0x11564": "",
        "_kind": "device",
    })

# Interfaces: `<device>_<ifName>` models (Gen_IF_Port) — the naming the
# connectivity docs show. Topology edges live in CONNECTIVITY below.
_IFS = {
    "rtr-core-01": ["Gi0/0", "Gi0/1", "Gi0/2", "Gi0/3"],
    "rtr-core-02": ["Gi0/0", "Gi0/1", "Gi0/2"],
    "sw-dist-01": ["Te1/1", "Te1/2", "Te1/3"],
    "sw-dist-02": ["Te1/1", "Te1/2", "Te1/3"],
    "fw-edge-01": ["eth1/1", "eth1/2", "eth1/3"],
    "sw-acc-11": ["Gi1/0/1", "Gi1/0/48"],
    "sw-acc-12": ["Gi1/0/1", "Gi1/0/48"],
    "wlc-01": ["Te0/0/0", "Te0/0/1"],
    "srv-esx-01": ["vmnic0", "vmnic1"],
    "rtr-core-11": ["ge-0/0/0", "ge-0/0/1", "ge-0/0/2"],
    "sw-dist-11": ["Te1/1", "Te1/2", "Te1/3"],
    "sw-acc-21": ["Gi1/0/1", "Gi1/0/48"],
    "sw-acc-22": ["Gi1/0/1", "Gi1/0/48"],
}
DEVICE_BY_NAME = {m["0x1006e"]: mh for mh, m in MODELS.items()}
IF_OF_DEVICE: Dict[str, List[str]] = {}
_n = 0
for dev, ifs in _IFS.items():
    dmh = DEVICE_BY_NAME[dev]
    ls = MODELS[dmh]["0x129ac"]
    for i, ifname in enumerate(ifs):
        _n += 1
        mh = f"{int(ls, 16) + 0x1000 + _n:#x}"
        cond = "6" if (dev == "rtr-core-01" and ifname == "Gi0/1") else "0"
        _add_model(mh, {
            "0x1006e": f"{dev}_{ifname}", "0x10000": "Gen_IF_Port", "0x10001": "0x220004", "0x11ee8": "20",
            "0x12d7f": MODELS[dmh]["0x12d7f"], "0x129ac": ls, "0x1000a": cond, "0x1295d": "true",
            "0x12adb": "", "0x1290c": "0", "_kind": "interface", "_device": dmh,
        })
        IF_OF_DEVICE.setdefault(dmh, []).append(mh)

# Containers / global collections (searchable by GC name through 0x12adb).
for mh, name, ls in (("0x100001", "DC1 Site", "0x100000"), ("0x200001", "DC2 Site", "0x200000")):
    _add_model(mh, {"0x1006e": name, "0x10000": "LAN_802_3", "0x10001": "0x1030001", "0x11ee8": "30",
                    "0x12d7f": "", "0x129ac": ls, "0x1000a": "0", "0x1295d": "true", "0x12adb": "",
                    "0x1290c": "0", "_kind": "container"})
SITE_OF_LS = {"0x100000": "0x100001", "0x200000": "0x200001"}

# Connectivity: (left device, left port, right device, right port)
CONNECTIVITY = [
    ("rtr-core-01", "Gi0/0", "rtr-core-02", "Gi0/0"),
    ("rtr-core-01", "Gi0/1", "fw-edge-01", "eth1/1"),
    ("rtr-core-01", "Gi0/2", "sw-dist-01", "Te1/1"),
    ("rtr-core-01", "Gi0/3", "sw-dist-02", "Te1/1"),
    ("rtr-core-02", "Gi0/1", "sw-dist-01", "Te1/2"),
    ("rtr-core-02", "Gi0/2", "sw-dist-02", "Te1/2"),
    ("fw-edge-01", "eth1/2", "sw-acc-11", "Gi1/0/48"),
    ("fw-edge-01", "eth1/3", "sw-acc-12", "Gi1/0/48"),
    ("sw-dist-01", "Te1/3", "wlc-01", "Te0/0/0"),
    ("sw-dist-02", "Te1/3", "srv-esx-01", "vmnic0"),
    ("rtr-core-11", "ge-0/0/0", "sw-dist-11", "Te1/1"),
    ("sw-dist-11", "Te1/2", "sw-acc-21", "Gi1/0/48"),
    ("sw-dist-11", "Te1/3", "sw-acc-22", "Gi1/0/48"),
]


def _if_mh(dev: str, ifname: str) -> str:
    return DEVICE_BY_NAME[f"{dev}_{ifname}"]


DEVICE_BY_NAME = {m["0x1006e"]: mh for mh, m in MODELS.items()}


# ── alarms / events ──────────────────────────────────────────────────────────

def _sec(ms: int) -> str:
    return str(ms // 1000)


def _alarm(aid, mh, sev, title, cause, created_ms, occurrences="1", ack="false", status="",
           primary="true", ticket="", troubleshooter="", symptoms=(), causes=(), event="0x10d35"):
    m = MODELS[mh]
    return {
        "0x11f9c": aid, "0x11f53": mh, "0x11f56": sev, "0x12b4c": title, "0x11f50": cause,
        "0x11f4e": _sec(created_ms), "0x1321a": _sec(created_ms + 2 * MIN), "0x11fc5": occurrences,
        "0x11f4d": ack, "0x11f4f": status, "0x11f54": primary, "0x12022": ticket, "0x11f57": troubleshooter,
        "0x11fc4": "0", "0x11f9b": "true", "0x1296e": event,
        "0x12a04": ",".join(symptoms), "0x12a06": str(len(symptoms)),
        "0x12a05": ",".join(causes), "0x12a07": str(len(causes)),
        "0x12a63": f"https://oneclick.corp.local/spectrum/oneclick.jnlp?alarm={aid}",
        # model attributes travel with the alarm when requested
        "0x1006e": m["0x1006e"], "0x10000": m["0x10000"], "0x11ee8": m["0x11ee8"], "0x12d7f": m["0x12d7f"],
        "0x129ac": m["0x129ac"], "0x11d42": m["0x11d42"], "0x129fa": mh, "0x1000a": m["0x1000a"],
        "0x10001": m["0x10001"], "0x10009": "",
    }


ALARMS: List[dict] = [
    _alarm("0x1000001", _if_mh("rtr-core-01", "Gi0/1"), "2", "HIGH UTILIZATION ON INTERFACE", "0x10e0c",
           T0 - 15 * MIN, occurrences="3", event="0x10e0c",
           status="Inbound utilisation 94% (threshold 85%) for 3 consecutive polls"),
    _alarm("0x1000003", _if_mh("rtr-core-01", "Gi0/1"), "3", "LINK DOWN", "0x10d35", T0,
           causes=(), symptoms=("0x1000004", "0x1000005", "0x1000006"), event="0x10d35",
           status="ifOperStatus down on Gi0/1 (WAN uplink to fw-edge-01)"),
    _alarm("0x1000004", "0x100014", "3", "DEVICE HAS STOPPED RESPONDING TO POLLS", "0x10009", T0 + 2 * MIN,
           primary="false", causes=("0x1000003",), event="0x10701",
           status="Contact lost; last successful poll before T0"),
    _alarm("0x1000005", "0x100015", "3", "DEVICE HAS STOPPED RESPONDING TO POLLS", "0x10009", T0 + 3 * MIN,
           primary="false", causes=("0x1000003",), event="0x10701", status="Impact: upstream fw-edge-01 unreachable"),
    _alarm("0x1000006", "0x100016", "3", "DEVICE HAS STOPPED RESPONDING TO POLLS", "0x10009", T0 + 3 * MIN,
           primary="false", causes=("0x1000003",), event="0x10701", status="Impact: upstream fw-edge-01 unreachable"),
    _alarm("0x1000007", "0x100010", "2", "CPU UTILIZATION HIGH", "0x10e14", T0 + 1 * MIN, occurrences="2",
           event="0x10e14", status="cpu 5min 88% (threshold 80%)", ack="true", troubleshooter="noc-oncall"),
    _alarm("0x2000001", "0x200015", "1", "FAN STATUS NOT NORMAL", "0x10f05", T0 - 6 * 3600 * 1000,
           event="0x10f05", status="Fan 2 degraded", ticket="INC0042311", ack="true", troubleshooter="dc2-ops"),
]
ALARM_BY_ID = {a["0x11f9c"]: a for a in ALARMS}
ALARM_FILTERS = ["All Alarms", "Critical Only", "Critical and Major", "Unacknowledged", "Core Network"]


def _event(eid, mh, etype, when_ms, text, sev=""):
    m = MODELS[mh]
    return {"_id": eid, "_mh": mh, "_ts": when_ms, "0x4820007": text, "_type": etype,
            "0x1006e": m["0x1006e"], "0x10000": m["0x10000"], "0x12d7f": m["0x12d7f"], "0x129ac": m["0x129ac"],
            "0x11f56": sev, "0x129fa": mh}


EVENTS: List[dict] = []
_e = 0


def _ev(mh, etype, when_ms, text, sev=""):
    global _e
    _e += 1
    EVENTS.append(_event(f"0x{0x5000000 + _e:x}", mh, etype, when_ms, text, sev))


# Pre-incident polling noise on every device (every 10 min for 2 h).
for mh, m in list(MODELS.items()):
    if m.get("_kind") != "device":
        continue
    for k in range(12):
        _ev(mh, "0x10d26", T0 - (120 - 10 * k) * MIN, f"{m['0x1006e']} poll ok, sysUpTime {9000000 + k * 600}")
_gi01 = _if_mh("rtr-core-01", "Gi0/1")
for k in range(3):
    _ev(_gi01, "0x10e0c", T0 - (15 - 5 * k) * MIN,
        f"HIGH UTILIZATION: inbound {92 + k}% on rtr-core-01 Gi0/1 (threshold 85%)", "2")
_ev(_gi01, "0x10d35", T0, "LINK DOWN trap: ifIndex 2 (Gi0/1) ifOperStatus=down ifAdminStatus=up", "3")
_ev("0x100010", "0x10e14", T0 + 1 * MIN, "CPU UTILIZATION 88% on rtr-core-01 (threshold 80%) — reroute convergence", "2")
_ev("0x100014", "0x10701", T0 + 2 * MIN, "CONTACT LOST: fw-edge-01 (10.1.2.1) stopped responding to polls", "3")
_ev("0x100015", "0x10701", T0 + 3 * MIN, "CONTACT LOST: sw-acc-11 (10.1.3.11) — upstream fw-edge-01 unreachable", "3")
_ev("0x100016", "0x10701", T0 + 3 * MIN, "CONTACT LOST: sw-acc-12 (10.1.3.12) — upstream fw-edge-01 unreachable", "3")
_ev("0x100011", "0x10d26", T0 + 4 * MIN, "rtr-core-02 took over default route (OSPF reconvergence complete)")
_ev("0x200015", "0x10f05", T0 - 6 * 3600 * 1000, "FAN STATUS NOT NORMAL: fan 2 degraded on sw-acc-21", "1")
EVENTS.sort(key=lambda e: e["_ts"])


# ── XML helpers ──────────────────────────────────────────────────────────────

def _xml_bytes(root: ET.Element) -> bytes:
    return b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + ET.tostring(root, encoding="utf-8")


def _to_json(el: ET.Element):
    """ASSUMPTION 1 — Jersey mapped notation."""
    out: dict = {}
    for k, v in el.attrib.items():
        out["@" + k] = v
    children: Dict[str, list] = {}
    for c in el:
        children.setdefault(c.tag.rsplit("}", 1)[-1], []).append(_to_json(c))
    for k, vs in children.items():
        out[k] = vs if len(vs) > 1 else vs[0]
    if el.text and el.text.strip():
        if out:
            out["$"] = el.text.strip()
        else:
            return el.text.strip()
    return out


def _respond(request: Request, root: ET.Element, status: int = 200) -> Response:
    accept = (request.headers.get("accept") or "").lower()
    if "application/json" in accept:
        tag = root.tag.rsplit("}", 1)[-1]
        return JSONResponse({tag: _to_json(root)}, status_code=status)
    return Response(_xml_bytes(root), status_code=status, media_type="application/xml")


def _error(request: Request, status: int, message: str) -> Response:
    root = ET.Element(f"{{{RESPONSE}}}error-response")
    root.set("error", message)
    return _respond(request, root, status)


def _models_list(request: Request, models: List[dict], attrs: List[str], start: int, throttle: int,
                 next_params: Optional[dict], path: str, total_name: str = "total-models",
                 root_name: str = "model-response-list", wrapper: str = "model-responses",
                 item: str = "model", id_attr: str = "mh", id_key: str = "0x129fa") -> Response:
    page = models[start:start + throttle]
    root = ET.Element(f"{{{RESPONSE}}}{root_name}")
    root.set(total_name, str(len(models)))
    root.set("throttle", str(len(page)))
    end = start + len(page) >= len(models)
    if end:
        root.set("error", "EndOfResults")
    holder = ET.SubElement(root, f"{{{RESPONSE}}}{wrapper}") if wrapper else root
    for m in page:
        el = ET.SubElement(holder, f"{{{RESPONSE}}}{item}")
        el.set(id_attr, m[id_key] if id_key in m else m["_id"])
        for a in attrs:
            v = m.get(a)
            if v is None:
                a_el = ET.SubElement(el, f"{{{RESPONSE}}}attribute")
                a_el.set("id", a)
                a_el.set("error", "NoSuchAttribute")
                continue
            a_el = ET.SubElement(el, f"{{{RESPONSE}}}attribute")
            a_el.set("id", a)
            a_el.text = str(v)
    if not end:
        link = ET.SubElement(root, f"{{{RESPONSE}}}link")
        link.set("rel", "next")
        q = dict(next_params or {})
        q["start"] = str(start + throttle)
        q["throttlesize"] = str(throttle)
        link.set("href", f"{str(request.base_url).rstrip('/')}{path}?{urlencode(q, doseq=True)}")
        link.set("type", "application/xml")
    return _respond(request, root)


# ── auth ─────────────────────────────────────────────────────────────────────

def _auth(request: Request) -> Optional[Tuple[str, bool]]:
    header = request.headers.get("authorization") or ""
    if header.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
        except Exception:
            return None
        rec = USERS.get(user)
        if rec and rec[0] == pw:
            return user, rec[1]
        return None
    if header.startswith("Bearer "):
        user = _tokens.get(header[7:].strip())
        if user:
            return user, USERS[user][1]
    return None


@app.middleware("http")
async def _gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/__mock") or path in ("/health",):
        return await call_next(request)
    if not path.startswith("/spectrum/restful"):
        return await call_next(request)
    who = _auth(request)
    if who is None:
        resp = _error(request, 401, "Authentication required: Basic (local Spectrum user) or Bearer token.")
        resp.headers["WWW-Authenticate"] = 'Basic realm="OneClick"'
        return resp
    if request.method in ("PUT", "DELETE") and not who[1]:
        return _error(request, 403, f"User '{who[0]}' is not permitted to {request.method} via the REST API.")
    if request.method == "POST":
        ctype = (request.headers.get("content-type") or "").lower()
        body = await request.body()
        if body and "xml" not in ctype:
            return _error(request, 415, "Request bodies must be application/xml (the API accepts XML only for input).")
        request.state.body = body
    request.state.user = who
    return await call_next(request)


# ── helpers ──────────────────────────────────────────────────────────────────

def _params(request: Request):
    q = request.query_params
    attrs = q.getlist("attr")
    lss = q.getlist("landscape")
    try:
        throttle = int(q.get("throttlesize") or q.get("throttle") or DEFAULT_THROTTLE)
        start = int(q.get("start") or 0)
    except ValueError:
        throttle, start = DEFAULT_THROTTLE, 0
    throttle = max(1, min(throttle, 10_000))
    return attrs, lss, throttle, start


def _in_landscapes(m: dict, lss: List[str]) -> bool:
    return not lss or m.get("0x129ac") in lss


def _devices(lss: List[str]) -> List[dict]:
    return [m for m in MODELS.values() if m.get("_kind") == "device" and _in_landscapes(m, lss)]


def _parse_xml(body: bytes) -> ET.Element:
    try:
        return ET.fromstring(body)
    except ET.ParseError as e:
        raise ValueError(f"Malformed XML request: {e}")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _eval_filter(node: ET.Element, m: dict) -> bool:
    """Filter.xsd subset: and / or / not / equals / has-substring / has-prefix /
    is-derived-from / greater-than / less-than over <attribute id><value>."""
    name = _local(node.tag)
    kids = list(node)
    if name == "and":
        return all(_eval_filter(k, m) for k in kids)
    if name == "or":
        return any(_eval_filter(k, m) for k in kids)
    if name == "not":
        return not any(_eval_filter(k, m) for k in kids)
    if name in ("equals", "has-substring", "has-prefix", "is-derived-from", "greater-than", "less-than",
                "greater-than-or-equals", "less-than-or-equals", "has-pcre"):
        for k in kids:
            if _local(k.tag) == "model-type":
                return m.get("0x10000") == (k.text or "").strip()
            if _local(k.tag) == "attribute":
                aid = (k.get("id") or "").strip()
                aid = {"AttributeID.MODEL_NAME": "0x1006e", "AttributeID.NETWORK_ADDRESS": "0x12d7f",
                       "AttributeID.MTYPE_HANDLE": "0x10001", "AttributeID.MODEL_HANDLE": "0x129fa"}.get(aid, aid)
                val = (k.findtext("{*}value") or k.findtext("value") or "").strip()
                have = m.get(aid)
                if have is None:
                    return False
                have = str(have)
                if name == "equals":
                    return have == val or (val.startswith("0x") and have.lower() == val.lower())
                if name == "has-substring":
                    return val in have
                if name == "has-prefix":
                    return have.startswith(val)
                if name == "has-pcre":
                    import re
                    return re.search(val, have) is not None
                if name == "is-derived-from":
                    return have.lower() == val.lower()
                try:
                    a, b = float(have), float(val)
                except ValueError:
                    try:
                        a, b = int(have, 16), int(val, 16)
                    except ValueError:
                        return False
                return {"greater-than": a > b, "less-than": a < b,
                        "greater-than-or-equals": a >= b, "less-than-or-equals": a <= b}[name]
        return False
    if name in ("filtered-models", "search-criteria", "target-models", "models-search", "attribute-filter"):
        return all(_eval_filter(k, m) for k in kids)
    if name == "action-models":
        return _action_models(node, m)
    return True


def _action_models(node: ET.Element, m: dict) -> bool:
    action = (node.findtext("{*}action") or "").strip()
    if action in ("FIND_DEV_MODELS_BY_IP", "0x10093"):
        wanted = [(a.findtext("{*}value") or "").strip() for a in node.iter() if _local(a.tag) == "attribute"]
        return m.get("_kind") == "device" and m.get("0x12d7f") in wanted
    if action in ("FIND_ALL_DEVICE_MODELS", "0x1023a"):
        return m.get("_kind") == "device"
    return False


def _select_models(req: ET.Element) -> Tuple[List[dict], List[str], List[str]]:
    lss = [l.get("id") for l in req if _local(l.tag) == "landscape"]
    attrs = [a.get("id") for a in req if _local(a.tag) == "requested-attribute"]
    target = next((t for t in req if _local(t.tag) == "target-models"), None)
    pool = [m for m in MODELS.values() if _in_landscapes(m, lss)]
    if target is None:
        return pool, attrs, lss
    out: List[dict] = []
    handles = [x.get("mh") for x in target if _local(x.tag) == "model"]
    if handles:
        out.extend(MODELS[h] for h in handles if h in MODELS and _in_landscapes(MODELS[h], lss))
    gc = next((x.get("name") for x in target if _local(x.tag) == "gc"), None)
    if gc:
        out.extend(m for m in pool if m.get("0x12adb") == gc)
    search = next((x for x in target if _local(x.tag) == "models-search"), None)
    if search is not None:
        crit = next((x for x in search if _local(x.tag) == "search-criteria"), None)
        out.extend(m for m in pool if crit is None or _eval_filter(crit, m))
    return out, attrs, lss


# ── endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"ok": True, "product": "DX NetOps Spectrum OneClick (mock)", "version": SPECTRUM_VERSION}


@app.get("/__mock/scenario")
def scenario():
    return SCENARIO


@app.post("/__mock/token")
def mint_token(user: str = "bow_api"):
    if user not in USERS:
        return JSONResponse({"error": "unknown user"}, status_code=404)
    tok = "eyJhbGciOiJSUzI1NiJ9." + base64.urlsafe_b64encode(f'{{"sub":"{user}","jti":"{uuid.uuid4()}"}}'.encode()).decode().rstrip("=") + "." + secrets.token_urlsafe(16)
    _tokens[tok] = user
    return {"token": tok, "user": user, "audience": "Bag of Words"}


@app.get("/spectrum/restful/landscapes")
def landscapes(request: Request):
    root = ET.Element(f"{{{RESPONSE}}}landscape-response")
    root.set("total-landscapes", str(len(LANDSCAPES)))
    for l in LANDSCAPES:
        el = ET.SubElement(root, f"{{{RESPONSE}}}landscape")
        for k in ("id", "name", "isPrimary", "spectrumVersion"):
            ET.SubElement(el, f"{{{RESPONSE}}}{k}").text = l[k]
    return _respond(request, root)


@app.get("/spectrum/restful/devices")
def devices(request: Request):
    attrs, lss, throttle, start = _params(request)
    rows = _devices(lss)
    nxt = {"attr": attrs, "landscape": lss}
    return _models_list(request, rows, attrs, start, throttle, nxt, "/spectrum/restful/devices")


@app.post("/spectrum/restful/models")
async def models_search(request: Request):
    try:
        req = _parse_xml(request.state.body)
    except ValueError as e:
        return _error(request, 400, str(e))
    if _local(req.tag) != "model-request":
        return _error(request, 400, "Expected an rs:model-request body.")
    try:
        throttle = int(req.get("throttlesize") or DEFAULT_THROTTLE)
    except ValueError:
        throttle = DEFAULT_THROTTLE
    throttle = max(1, min(throttle, 10_000))
    rows, attrs, _ = _select_models(req)
    rows = [r for r in rows if r.get("_kind") != "_hidden"]
    rsid = uuid.uuid4().hex
    _result_sets[rsid] = (time.time() + RESULT_SET_TTL, rows, attrs)
    return _models_list(request, rows, attrs, 0, throttle, {"id": rsid}, "/spectrum/restful/models")


@app.get("/spectrum/restful/models")
def models_page(request: Request):
    attrs, _, throttle, start = _params(request)
    rsid = request.query_params.get("id")
    rec = _result_sets.get(rsid or "")
    if not rec or rec[0] < time.time():
        return _error(request, 404, "No result set: GET models requires the id from a prior POST (expires after 10 minutes).")
    _, rows, req_attrs = rec
    _result_sets[rsid] = (time.time() + RESULT_SET_TTL, rows, req_attrs)
    return _models_list(request, rows, attrs or req_attrs, start, throttle, {"id": rsid}, "/spectrum/restful/models")


@app.post("/spectrum/restful/models/getAllDevices")
async def models_all_devices(request: Request):
    try:
        req = _parse_xml(request.state.body)
    except ValueError as e:
        return _error(request, 400, str(e))
    attrs = [a.get("id") for a in req if _local(a.tag) == "requested-attribute"]
    lss = [l.get("id") for l in req if _local(l.tag) == "landscape"]
    try:
        throttle = int(req.get("throttlesize") or DEFAULT_THROTTLE)
    except ValueError:
        throttle = DEFAULT_THROTTLE
    rows = _devices(lss)
    rsid = uuid.uuid4().hex
    _result_sets[rsid] = (time.time() + RESULT_SET_TTL, rows, attrs)
    return _models_list(request, rows, attrs, 0, throttle, {"id": rsid}, "/spectrum/restful/models")


@app.get("/spectrum/restful/model/{mh}")
def model_get(request: Request, mh: str):
    attrs, _, _, _ = _params(request)
    m = MODELS.get(mh)
    if not m:
        return _error(request, 404, f"Model {mh} not found.")
    root = ET.Element(f"{{{RESPONSE}}}model-response")
    el = ET.SubElement(root, f"{{{RESPONSE}}}model")
    el.set("mh", mh)
    for a in attrs or [k for k in m if k.startswith("0x")]:
        a_el = ET.SubElement(el, f"{{{RESPONSE}}}attribute")
        a_el.set("id", a)
        if a in m:
            a_el.text = str(m[a])
        else:
            a_el.set("error", "NoSuchAttribute")
    return _respond(request, root)


@app.put("/spectrum/restful/model/{mh}")
@app.delete("/spectrum/restful/model/{mh}")
def model_write(request: Request, mh: str):
    return _error(request, 501, "Writes are not simulated.")


def _alarm_list(request: Request, rows: List[dict], attrs: List[str], start: int, throttle: int, nxt: dict):
    return _models_list(request, rows, attrs, start, throttle, nxt, "/spectrum/restful/alarms",
                        total_name="total-alarms", root_name="alarm-response-list", wrapper="alarm-responses",
                        item="alarm", id_attr="id", id_key="0x11f9c")


@app.get("/spectrum/restful/alarms")
def alarms_get(request: Request):
    attrs, lss, throttle, start = _params(request)
    rows = [a for a in ALARMS if _in_landscapes(a, lss)]
    return _alarm_list(request, rows, attrs, start, throttle, {"attr": attrs, "landscape": lss})


@app.post("/spectrum/restful/alarms")
async def alarms_search(request: Request):
    try:
        req = _parse_xml(request.state.body)
    except ValueError as e:
        return _error(request, 400, str(e))
    if _local(req.tag) != "alarm-request":
        return _error(request, 400, "Expected an rs:alarm-request body.")
    try:
        throttle = int(req.get("throttlesize") or DEFAULT_THROTTLE)
    except ValueError:
        throttle = DEFAULT_THROTTLE
    throttle = max(1, min(throttle, 10_000))
    attrs = [a.get("id") for a in req if _local(a.tag) == "requested-attribute"]
    lss = [l.get("id") for l in req if _local(l.tag) == "landscape"]
    rows = [a for a in ALARMS if _in_landscapes(a, lss)]
    q = request.query_params
    if q.get("symptoms") == "no":
        rows = [a for a in rows if a["0x11f54"] == "true"]
    if q.get("lasthour"):
        try:
            cutoff = time.time() - int(q["lasthour"]) * 3600
            rows = [a for a in rows if int(a["0x11f4e"]) >= cutoff]
        except ValueError:
            pass
    af = next((x for x in req if _local(x.tag) == "attribute-filter"), None)
    if af is not None:
        rows = [a for a in rows if _eval_filter(af, a)]
    target = next((x for x in req if _local(x.tag) == "target-models"), None)
    if target is not None:
        handles = [x.get("mh") for x in target if _local(x.tag) == "model"]
        if handles:
            rows = [a for a in rows if a["0x11f53"] in handles]
        search = next((x for x in target if _local(x.tag) == "models-search"), None)
        if search is not None:
            crit = next((x for x in search if _local(x.tag) == "search-criteria"), None)
            if crit is not None:
                rows = [a for a in rows if _eval_filter(crit, MODELS[a["0x11f53"]])]
    ids = [x.get("id") for x in req if _local(x.tag) == "alarm"]
    if ids:
        rows = [a for a in rows if a["0x11f9c"] in ids]
    filt = next((x.get("name") for x in req if _local(x.tag) == "alarm-filter"), None)
    if filt:
        rows = _apply_named_filter(rows, filt)
    rows = sorted(rows, key=lambda a: int(a["0x11f4e"]))
    return _alarm_list(request, rows, attrs, 0, throttle, {"attr": attrs, "landscape": lss})


def _apply_named_filter(rows: List[dict], name: str) -> List[dict]:
    if name == "Critical Only":
        return [a for a in rows if a["0x11f56"] == "3"]
    if name == "Critical and Major":
        return [a for a in rows if a["0x11f56"] in ("2", "3")]
    if name == "Unacknowledged":
        return [a for a in rows if a["0x11f4d"] == "false"]
    if name == "Core Network":
        return [a for a in rows if MODELS[a["0x11f53"]].get("0x12adb") == "Core Network"
                or MODELS.get(MODELS[a["0x11f53"]].get("_device", ""), {}).get("0x12adb") == "Core Network"]
    return rows


@app.post("/spectrum/restful/alarms/count")
async def alarms_count(request: Request):
    rows = list(ALARMS)
    if request.state.body:
        try:
            req = _parse_xml(request.state.body)
        except ValueError as e:
            return _error(request, 400, str(e))
        filt = next((x.get("name") for x in req.iter() if _local(x.tag) == "alarm-filter"), None)
        if filt:
            if filt not in ALARM_FILTERS:
                return _error(request, 404, f"Alarm filter '{filt}' is not defined in OneClick.")
            rows = _apply_named_filter(rows, filt)
    root = ET.Element(f"{{{RESPONSE}}}alarmcount-response")
    for sev, name in (("3", "critical"), ("2", "major"), ("1", "minor")):
        ET.SubElement(root, f"{{{RESPONSE}}}{name}").text = str(sum(1 for a in rows if a["0x11f56"] == sev))
    return _respond(request, root)


@app.get("/spectrum/restful/alarms/filters")
def alarms_filters(request: Request):
    root = ET.Element(f"{{{RESPONSE}}}alarmfilters-response")
    for f in ALARM_FILTERS:
        ET.SubElement(root, f"{{{RESPONSE}}}alarmfilter").text = f
    return _respond(request, root)


@app.put("/spectrum/restful/alarms/{alarm_id}")
@app.delete("/spectrum/restful/alarms/{alarm_id}")
def alarm_write(request: Request, alarm_id: str):
    return _error(request, 501, "Writes are not simulated.")


@app.post("/spectrum/restful/events/getEvents")
async def events_get(request: Request):
    try:
        req = _parse_xml(request.state.body)
    except ValueError as e:
        return _error(request, 400, str(e))
    if _local(req.tag) != "get-event-request":
        return _error(request, 400, "Expected an rs:get-event-request body.")
    try:
        throttle = max(1, min(int(req.get("throttlesize") or DEFAULT_THROTTLE), 10_000))
    except ValueError:
        throttle = DEFAULT_THROTTLE
    start = int(request.query_params.get("start") or 0)
    f = next((x for x in req if _local(x.tag) == "get-events-filter"), None)
    attrs = [a.get("id") for a in req if _local(a.tag) == "requested-attribute"]
    lss = [l.get("id") for l in req if _local(l.tag) == "landscape"]
    if len(lss) != 1:
        return _error(request, 400, "get-event-request requires exactly one rs:landscape.")
    target = next((x for x in req if _local(x.tag) == "target-models"), None)
    handles = [x.get("mh") for x in target if _local(x.tag) == "model"] if target is not None else []
    if not handles:
        return _error(request, 400, "get-event-request requires rs:target-models with device model handles.")
    bad = [h for h in handles if h not in MODELS or MODELS[h].get("_kind") != "device"]
    if bad:
        return _error(request, 400, f"getEvents supports device models only; not a device: {', '.join(bad)}")
    t_start = int(f.get("start-time") or 0) if f is not None else 0
    t_end = int(f.get("end-time") or 2 ** 62) if f is not None else 2 ** 62
    sub = (f.get("subcomponents-events") if f is not None else "false") == "true"
    exclude = set((f.get("exclude-events") or "").split(",")) - {""} if f is not None else set()
    scope = set(handles)
    if sub:
        for h in handles:
            scope.update(IF_OF_DEVICE.get(h, []))
    rows = [e for e in EVENTS if e["_mh"] in scope and t_start <= e["_ts"] <= t_end
            and e["_type"] not in exclude and e["0x129ac"] == lss[0]]
    page = rows[start:start + throttle]
    root = ET.Element(f"{{{RESPONSE}}}get-event-response-list")
    root.set("total-events", str(len(rows)))
    root.set("throttle", str(len(page)))
    end = start + len(page) >= len(rows)
    if end:
        root.set("error", "EndOfResults")
    holder = ET.SubElement(root, f"{{{RESPONSE}}}event-responses")
    for e in page:
        el = ET.SubElement(holder, f"{{{RESPONSE}}}event")
        el.set("id", e["_id"])
        el.set("mh", e["_mh"])
        el.set("time", str(e["_ts"]))
        el.set("type", e["_type"])
        for a in attrs:
            a_el = ET.SubElement(el, f"{{{RESPONSE}}}attribute")
            a_el.set("id", a)
            if e.get(a) is None:
                a_el.set("error", "NoSuchAttribute")
            else:
                a_el.text = str(e[a])
    if not end:
        link = ET.SubElement(root, f"{{{RESPONSE}}}link")
        link.set("rel", "next")
        link.set("href", f"{str(request.base_url).rstrip('/')}/spectrum/restful/events/getEvents?start={start + throttle}&throttlesize={throttle}")
        link.set("type", "application/xml")
    return _respond(request, root)


@app.post("/spectrum/restful/events")
@app.post("/spectrum/restful/events/{event_type}/model/{mh}")
async def events_create(request: Request, event_type: str = "", mh: str = ""):
    return _error(request, 501, "Event creation is not simulated.")


@app.get("/spectrum/restful/associations/relation/{rel}/model/{mh}")
def associations(request: Request, rel: str, mh: str):
    side = request.query_params.get("side", "left")
    m = MODELS.get(mh)
    if not m:
        return _error(request, 404, f"Model {mh} not found.")
    pairs: List[Tuple[str, str]] = []
    if rel.lower() == IF_RELATION:
        if side == "left":
            pairs = [(mh, i) for i in IF_OF_DEVICE.get(mh, [])]
        elif m.get("_device"):
            pairs = [(m["_device"], mh)]
    elif rel.lower() == COLLECTS:
        if side == "left" and m.get("_kind") == "container":
            pairs = [(mh, d) for d, dm in MODELS.items() if dm.get("_kind") == "device" and dm["0x129ac"] == m["0x129ac"]]
        elif side == "right" and m.get("_kind") == "device":
            pairs = [(SITE_OF_LS[m["0x129ac"]], mh)]
    root = ET.Element(f"{{{RESPONSE}}}association-response-list")
    root.set("total-associations", str(len(pairs)))
    for left, right in pairs:
        el = ET.SubElement(root, f"{{{RESPONSE}}}association")
        el.set("relation", rel)
        ET.SubElement(el, f"{{{RESPONSE}}}leftmh").text = left
        ET.SubElement(el, f"{{{RESPONSE}}}rightmh").text = right
    return _respond(request, root)


@app.get("/spectrum/restful/connectivity/{ip}")
def connectivity(request: Request, ip: str):
    dev = next((mh for mh, m in MODELS.items() if m.get("_kind") == "device" and m["0x12d7f"] == ip), None)
    if not dev:
        return _error(request, 404, f"No device model with address {ip}.")
    name = MODELS[dev]["0x1006e"]
    root = ET.Element(f"{{{RESPONSE}}}connection-response-list")

    def elem(parent, tag, mh):
        m = MODELS[mh]
        e = ET.SubElement(parent, f"{{{RESPONSE}}}{tag}")
        for k, v in (("ipaddress", m["0x12d7f"]), ("mh", mh), ("name", m["0x1006e"]), ("type", m["0x10000"]),
                     ("class", ENUMS["0x11ee8"].get(m["0x11ee8"], m["0x11ee8"])), ("condition", m["0x1000a"])):
            ET.SubElement(e, f"{{{RESPONSE}}}{k}").text = v

    for l_dev, l_if, r_dev, r_if in CONNECTIVITY:
        if name not in (l_dev, r_dev):
            continue
        if name == r_dev:
            l_dev, l_if, r_dev, r_if = r_dev, r_if, l_dev, l_if
        c = ET.SubElement(root, f"{{{RESPONSE}}}connection-response")
        elem(c, "connection-element-left", _if_mh(l_dev, l_if))
        elem(c, "connection-element-right", DEVICE_BY_NAME[r_dev])
        elem(c, "connection-element-right-port", _if_mh(r_dev, r_if))
    return _respond(request, root)


@app.get("/spectrum/restful/attribute/{attr_id}/enums")
def attribute_enums(request: Request, attr_id: str):
    root = ET.Element(f"{{{RESPONSE}}}attribute-enum-response")
    el = ET.SubElement(root, f"{{{RESPONSE}}}attribute")
    el.set("id", attr_id)
    if attr_id not in ENUMS:
        el.set("error", "NotAnEnumeration" if attr_id in ATTR_NAMES else "NoSuchAttribute")
    for k, v in ENUMS.get(attr_id, {}).items():
        e = ET.SubElement(el, f"{{{RESPONSE}}}enum")
        e.set("id", k)
        e.set("value", v)
    return _respond(request, root)


@app.get("/spectrum/restful/action/{code}")
def action(request: Request, code: str):
    q = request.query_params
    if code.lower() == "0x10093":    # FIND_DEV_MODELS_BY_IP (SearchManager)
        wanted = set(q.getlist("val"))
        rows = [m for m in MODELS.values() if m.get("_kind") == "device" and m["0x12d7f"] in wanted]
        return _models_list(request, rows, ["0x1006e", "0x10000", "0x12d7f"], 0, DEFAULT_THROTTLE, None,
                            "/spectrum/restful/action/0x10093")
    return _error(request, 403, f"Action {code} is not permitted for this user (read-only API access).")


@app.get("/spectrum/restful/swagger.json")
def swagger(request: Request):
    return {"swagger": "1.2", "apiVersion": SPECTRUM_VERSION, "basePath": "/spectrum/restful",
            "apis": [{"path": p} for p in ("/landscapes", "/devices", "/models", "/model", "/alarms", "/events",
                                             "/associations", "/connectivity", "/attribute", "/action",
                                             "/subscription", "/v3profiles")]}


@app.get("/spectrum/restful/api-docs")
def api_docs(request: Request):
    return Response("<html><body><h1>Swagger UI (mock)</h1></body></html>", media_type="text/html")


@app.post("/spectrum/restful/subscription")
async def subscription_create(request: Request):
    root = ET.Element(f"{{{RESPONSE}}}subscription-response")
    root.set("subscription-id", str(int(time.time())))
    return _respond(request, root)


@app.get("/spectrum/restful/subscription/{sid}")
def subscription_poll(request: Request, sid: str):
    root = ET.Element(f"{{{RESPONSE}}}subscription-notification-list")
    root.set("subscription-id", sid)
    return _respond(request, root)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("SPECTRUM_MOCK_PORT", "8880")))
