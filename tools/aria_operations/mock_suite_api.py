"""Mock VMware Aria Operations Suite API — request/response shapes per the
official OpenAPI spec (`vmware/vcf-api-specs`, VCF Operations 9.1; the same
`/suite-api/api` surface Aria Operations 8.18 serves) and the 8.18 API
Programming Guide.

A real appliance cannot run here (OVA, needs ESXi + a Broadcom entitlement),
so — like tools/appdynamics/mock_controller.py — this simulates the exact
surface `AriaOperationsClient` touches:

  POST /suite-api/api/auth/token/acquire
  GET  /suite-api/api/versions/current
  GET  /suite-api/api/adapterkinds
  GET  /suite-api/api/adapterkinds/{key}/resourcekinds
  GET  /suite-api/api/adapterkinds/{ak}/resourcekinds/{rk}/statkeys
  GET  /suite-api/api/resources                      (+ POST /resources/query)
  GET  /suite-api/api/resources/{id}
  GET  /suite-api/api/resources/{id}/properties      (+ POST /resources/properties/latest/query)
  GET  /suite-api/api/resources/{id}/relationships
  POST /suite-api/api/resources/stats/query          (+ /resources/{id}/stats/query)
  POST /suite-api/api/resources/stats/latest/query   (+ GET /resources/stats/latest)
  GET  /suite-api/api/resources/stats/topn
  GET  /suite-api/api/resources/groups               (+ /groups/{id}/members)
  POST /suite-api/api/alerts/query                   (+ GET /alerts, GET /alerts/{id})
  GET  /suite-api/api/alerts/contributingsymptoms
  POST /suite-api/api/symptoms/query                 (+ GET /symptoms)
  POST /suite-api/api/alertdefinitions/query         (+ GET /alertdefinitions)
  POST /suite-api/api/events                         (push only — the real API has no event READ)

Faithfulness rules enforced on purpose:
  - Every call needs `Authorization: OpsToken <t>` (or the legacy
    `vRealizeOpsToken <t>`); anything else is a 401. Tokens expire
    (ARIA_MOCK_TOKEN_TTL seconds, default 21600 = the real 6h) so
    refresh-on-401 is observable in seconds when the TTL is shrunk.
  - Without `Accept: application/json` the API answers XML (the historic
    default) — a client that forgets the header fails loudly.
  - `pageSize` above 1000 is a 400; `stats/latest` rejects >1000 resourceIds.
  - All timestamps are epoch MILLISECONDS.

Seeded estate: a small vSphere shop (1 vCenter, 2 clusters, 4 hosts, 10 VMs,
3 datastores) plus a Hitachi storage pack (`HitachiStorage`: 1 array,
2 pools, 3 LDEVs, 2 ports) wired VM → Datastore → LDEV → Pool → StorageSystem,
and ONE deterministic incident (see `SCENARIO`): the nightly `batch-etl-01`
job on `ds_batch_01` saturates `Pool-07`; the pool's response time climbs at
T0, `ds_prod_db_01` (same pool) follows at T0+2m, `prod-db-01` disk latency
at T0+3m. Alerts and symptoms fire in that order; dynamic-threshold bands
are exceeded only on the affected objects. `GET /__mock/scenario` returns
T0 for tests.

Run:  uv run --project backend uvicorn mock_suite_api:app --port 8443
      (from this directory; or via docker-compose.yaml)
Creds: LOCAL   admin / Aria!2024
       corp.local (LDAP authSource) svc_bow / Bow!2024
       LOCAL   readonly / Ready!2024   (sees the estate, same as admin)
"""
import hashlib
import math
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

TOKEN_TTL = int(os.environ.get("ARIA_MOCK_TOKEN_TTL", str(6 * 3600)))
COLLECTION_INTERVAL_MS = 5 * 60_000        # Aria collects every 5 minutes
MAX_PAGE_SIZE = 1000
RETENTION_DAYS = 30                        # how far back series are generated

USERS = {
    ("LOCAL", "admin"): "Aria!2024",
    ("LOCAL", "readonly"): "Ready!2024",
    ("corp.local", "svc_bow"): "Bow!2024",
}
_tokens: Dict[str, float] = {}             # token -> expiry epoch seconds

app = FastAPI(title="Mock VMware Aria Operations Suite API (8.18 shapes)")


# ── incident scenario ─────────────────────────────────────────────────────────

def _incident_start_ms() -> int:
    """T0 = today 02:10 UTC if that is ≥ 2h in the past, else yesterday 02:10.
    Override with ARIA_MOCK_INCIDENT_START (epoch ms) for tests."""
    env = os.environ.get("ARIA_MOCK_INCIDENT_START")
    if env:
        return int(env)
    now = datetime.now(timezone.utc)
    t0 = now.replace(hour=2, minute=10, second=0, microsecond=0)
    if now - t0 < timedelta(hours=2):
        t0 -= timedelta(days=1)
    return int(t0.timestamp() * 1000)


SCENARIO = {
    "incident_start_ms": _incident_start_ms(),
    "duration_ms": 40 * 60_000,
    "root_cause": "batch-etl-01 nightly job saturates Pool-07 (shared with ds_prod_db_01)",
}
T0 = SCENARIO["incident_start_ms"]
T_END = T0 + SCENARIO["duration_ms"]
MIN = 60_000


# ── adapter kinds / resource kinds / stat keys ────────────────────────────────

ADAPTER_KINDS = [
    {"key": "VMWARE", "name": "vCenter Adapter", "description": "Manages vCenter Server objects",
     "adapterKindType": "GENERAL", "describeVersion": 1},
    {"key": "HitachiStorage", "name": "Hitachi Infrastructure Management Pack",
     "description": "Hitachi VSP storage systems, pools, LDEVs and ports (fed by Ops Center Analyzer)",
     "adapterKindType": "GENERAL", "describeVersion": 3},
    {"key": "VMWARE_ARIA_OPERATIONS", "name": "VMware Aria Operations Adapter",
     "description": "Self-monitoring", "adapterKindType": "GENERAL", "describeVersion": 1},
]

RESOURCE_KINDS = {
    "VMWARE": [
        ("VMwareAdapter Instance", "vCenter Adapter Instance", "ADAPTER_INSTANCE"),
        ("vSphere World", "vSphere World", "GENERAL"),
        ("VirtualMachine", "Virtual Machine", "GENERAL"),
        ("HostSystem", "Host System", "GENERAL"),
        ("ClusterComputeResource", "Cluster Compute Resource", "GENERAL"),
        ("Datacenter", "Datacenter", "GENERAL"),
        ("Datastore", "Datastore", "GENERAL"),
        ("vCenter", "vCenter Server", "GENERAL"),
    ],
    "HitachiStorage": [
        ("HitachiStorageAdapter Instance", "Hitachi Adapter Instance", "ADAPTER_INSTANCE"),
        ("StorageSystem", "Storage System", "GENERAL"),
        ("Pool", "Storage Pool", "GENERAL"),
        ("LDEV", "Logical Device", "GENERAL"),
        ("Port", "Storage Port", "GENERAL"),
    ],
    "VMWARE_ARIA_OPERATIONS": [
        ("vC-Ops-Node", "Aria Operations Node", "GENERAL"),
    ],
}

# (key, name, unit, rollup, dataType2, description, baseline)
STAT_KEYS: Dict[Tuple[str, str], List[Tuple]] = {
    ("VMWARE", "VirtualMachine"): [
        ("cpu|usage_average", "CPU|Usage", "%", "AVG", "FLOAT", "CPU usage as a percentage", 22.0),
        ("mem|usage_average", "Memory|Usage", "%", "AVG", "FLOAT", "Memory usage as a percentage", 48.0),
        ("virtualDisk|totalLatency", "Virtual Disk|Total Latency", "ms", "AVG", "FLOAT", "Average total disk latency (read + write) across all virtual disks", 4.0),
        ("virtualDisk|totalReadLatency", "Virtual Disk|Read Latency", "ms", "AVG", "FLOAT", "Average read latency", 3.5),
        ("virtualDisk|totalWriteLatency", "Virtual Disk|Write Latency", "ms", "AVG", "FLOAT", "Average write latency", 4.5),
        ("virtualDisk|commandsAveraged_average", "Virtual Disk|Commands per second", "IOPS", "AVG", "FLOAT", "Average IOPS across all virtual disks", 180.0),
        ("badge|health", "Badge|Health", "", "AVG", "FLOAT", "Health score 0-100", 96.0),
    ],
    ("VMWARE", "HostSystem"): [
        ("cpu|usage_average", "CPU|Usage", "%", "AVG", "FLOAT", "CPU usage as a percentage", 38.0),
        ("mem|usage_average", "Memory|Usage", "%", "AVG", "FLOAT", "Memory usage as a percentage", 61.0),
        ("disk|totalLatency_average", "Disk|Total Latency", "ms", "AVG", "FLOAT", "Average device latency across all storage adapters", 3.0),
        ("net|usage_average", "Network|Usage Rate", "KBps", "AVG", "FLOAT", "Network usage", 2400.0),
    ],
    ("VMWARE", "Datastore"): [
        ("datastore|totalLatency", "Datastore|Total Latency", "ms", "AVG", "FLOAT", "Average total latency observed by hosts on this datastore", 3.0),
        ("datastore|numberReadAveraged_average", "Datastore|Reads per second", "IOPS", "AVG", "FLOAT", "Read IOPS", 420.0),
        ("datastore|numberWriteAveraged_average", "Datastore|Writes per second", "IOPS", "AVG", "FLOAT", "Write IOPS", 310.0),
        ("capacity|used_pct", "Capacity|Used %", "%", "AVG", "FLOAT", "Used capacity percentage", 67.0),
        ("disk|used", "Disk Space|Used", "GB", "LATEST", "FLOAT", "Used disk space", 3400.0),
        ("disk|provisioned", "Disk Space|Provisioned", "GB", "LATEST", "FLOAT", "Provisioned disk space", 5120.0),
    ],
    ("VMWARE", "ClusterComputeResource"): [
        ("cpu|usage_average", "CPU|Usage", "%", "AVG", "FLOAT", "Cluster CPU usage", 41.0),
        ("mem|usage_average", "Memory|Usage", "%", "AVG", "FLOAT", "Cluster memory usage", 63.0),
    ],
    ("VMWARE", "Datacenter"): [
        ("summary|total_number_vms", "Summary|Total Number of VMs", "", "LATEST", "INTEGER", "VM count", 10),
    ],
    ("VMWARE", "vCenter"): [
        ("summary|total_number_hosts", "Summary|Total Number of Hosts", "", "LATEST", "INTEGER", "Host count", 4),
    ],
    ("HitachiStorage", "StorageSystem"): [
        ("total_iops", "Performance|Total IOPS", "IOPS", "AVG", "FLOAT", "Total IOPS on the array", 9800.0),
        ("cache_write_pending_rate", "Performance|Cache Write Pending %", "%", "AVG", "FLOAT", "Write-pending cache rate", 12.0),
        ("capacity_used_pct", "Capacity|Used %", "%", "LATEST", "FLOAT", "Physical capacity used", 58.0),
    ],
    ("HitachiStorage", "Pool"): [
        ("response_time", "Performance|Response Time", "ms", "AVG", "FLOAT", "Average pool response time", 2.0),
        ("total_iops", "Performance|Total IOPS", "IOPS", "AVG", "FLOAT", "Total IOPS on the pool", 3200.0),
        ("read_iops", "Performance|Read IOPS", "IOPS", "AVG", "FLOAT", "Read IOPS", 1900.0),
        ("write_iops", "Performance|Write IOPS", "IOPS", "AVG", "FLOAT", "Write IOPS", 1300.0),
        ("utilization_pct", "Performance|Utilization %", "%", "AVG", "FLOAT", "Pool busy percentage", 35.0),
        ("used_capacity_pct", "Capacity|Used %", "%", "LATEST", "FLOAT", "Used capacity percentage", 71.0),
    ],
    ("HitachiStorage", "LDEV"): [
        ("response_time", "Performance|Response Time", "ms", "AVG", "FLOAT", "Average LDEV response time", 2.2),
        ("total_iops", "Performance|Total IOPS", "IOPS", "AVG", "FLOAT", "Total IOPS on the LDEV", 700.0),
        ("read_hit_rate", "Performance|Read Hit %", "%", "AVG", "FLOAT", "Cache read hit rate", 88.0),
    ],
    ("HitachiStorage", "Port"): [
        ("total_iops", "Performance|Total IOPS", "IOPS", "AVG", "FLOAT", "Total IOPS through the port", 4800.0),
        ("throughput_mbps", "Performance|Throughput", "MBps", "AVG", "FLOAT", "Throughput", 410.0),
    ],
    ("VMWARE_ARIA_OPERATIONS", "vC-Ops-Node"): [
        ("cpu|usage_average", "CPU|Usage", "%", "AVG", "FLOAT", "Node CPU", 18.0),
    ],
}


# ── resources ─────────────────────────────────────────────────────────────────

def _rid(name: str) -> str:
    """Stable UUID-looking identifier from a name (real ids are UUIDs)."""
    h = hashlib.sha1(name.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def R(ak: str, rk: str, name: str, health: str = "GREEN", props: Optional[dict] = None,
      identifiers: Optional[dict] = None, health_value: float = 96.0) -> dict:
    return {
        "identifier": _rid(f"{ak}:{rk}:{name}"),
        "adapterKind": ak, "resourceKind": rk, "name": name,
        "health": health, "healthValue": health_value,
        "props": props or {}, "identifiers": identifiers or {},
    }


RESOURCES: List[dict] = [
    R("VMWARE", "VMwareAdapter Instance", "vc-prod-01 adapter", identifiers={"VCURL": "https://vc-prod-01.corp.local"}),
    R("VMWARE", "vCenter", "vc-prod-01", props={"summary|version": "8.0.3", "summary|build": "24322831"}),
    R("VMWARE", "Datacenter", "DC-Prod", props={"summary|total_number_vms": "10"}),
    R("VMWARE", "ClusterComputeResource", "CL-DB", props={"config|drs|enabled": "true", "config|ha|enabled": "true"}),
    R("VMWARE", "ClusterComputeResource", "CL-App", props={"config|drs|enabled": "true", "config|ha|enabled": "true"}),
    R("VMWARE", "HostSystem", "esx-db-01.corp.local", props={"summary|hardware|model": "PowerEdge R760", "cpu|numCpu": "64", "summary|version": "8.0.3"}),
    R("VMWARE", "HostSystem", "esx-db-02.corp.local", props={"summary|hardware|model": "PowerEdge R760", "cpu|numCpu": "64", "summary|version": "8.0.3"}),
    R("VMWARE", "HostSystem", "esx-app-01.corp.local", props={"summary|hardware|model": "PowerEdge R660", "cpu|numCpu": "48", "summary|version": "8.0.3"}),
    R("VMWARE", "HostSystem", "esx-app-02.corp.local", props={"summary|hardware|model": "PowerEdge R660", "cpu|numCpu": "48", "summary|version": "8.0.3"}),
    R("VMWARE", "Datastore", "ds_prod_db_01", props={"summary|url": "ds:///vmfs/volumes/6650a1b2-1/", "config|ds_type": "VMFS", "summary|datastore|naa": "naa.60060e80072a2c0000302a2c000010a0", "summary|capacity_gb": "5120"}),
    R("VMWARE", "Datastore", "ds_batch_01", props={"summary|url": "ds:///vmfs/volumes/6650a1b2-2/", "config|ds_type": "VMFS", "summary|datastore|naa": "naa.60060e80072a2c0000302a2c000010a1", "summary|capacity_gb": "4096"}),
    R("VMWARE", "Datastore", "ds_prod_app_01", props={"summary|url": "ds:///vmfs/volumes/6650a1b2-3/", "config|ds_type": "VMFS", "summary|datastore|naa": "naa.60060e80072a2c0000302a2c000020b0", "summary|capacity_gb": "8192"}),
]

VMS = [
    # name, host, datastore, guest, vcpu, cmdb-ish uuid seed
    ("prod-db-01", "esx-db-01.corp.local", "ds_prod_db_01", "Red Hat Enterprise Linux 9 (64-bit)", 16),
    ("prod-db-02", "esx-db-02.corp.local", "ds_prod_db_01", "Red Hat Enterprise Linux 9 (64-bit)", 16),
    ("batch-etl-01", "esx-db-02.corp.local", "ds_batch_01", "Red Hat Enterprise Linux 9 (64-bit)", 8),
    ("prod-app-01", "esx-app-01.corp.local", "ds_prod_app_01", "Ubuntu Linux (64-bit)", 8),
    ("prod-app-02", "esx-app-01.corp.local", "ds_prod_app_01", "Ubuntu Linux (64-bit)", 8),
    ("prod-app-03", "esx-app-02.corp.local", "ds_prod_app_01", "Ubuntu Linux (64-bit)", 8),
    ("prod-app-04", "esx-app-02.corp.local", "ds_prod_app_01", "Ubuntu Linux (64-bit)", 8),
    ("prod-web-01", "esx-app-01.corp.local", "ds_prod_app_01", "Ubuntu Linux (64-bit)", 4),
    ("prod-web-02", "esx-app-02.corp.local", "ds_prod_app_01", "Ubuntu Linux (64-bit)", 4),
    ("mon-collector-01", "esx-app-02.corp.local", "ds_prod_app_01", "Ubuntu Linux (64-bit)", 2),
]
for i, (name, host, ds, guest, vcpu) in enumerate(VMS):
    uuid = _rid(f"instanceUuid:{name}")
    RESOURCES.append(R("VMWARE", "VirtualMachine", name, props={
        "config|instanceUuid": uuid, "summary|MOID": f"vm-{1000 + i}",
        "summary|guest|fullName": guest, "config|hardware|numCpu": str(vcpu),
        "summary|runtime|host": host, "summary|config|datastore": ds,
        "summary|runtime|powerState": "Powered On",
    }))

RESOURCES += [
    R("HitachiStorage", "HitachiStorageAdapter Instance", "Ops Center Analyzer adapter", identifiers={"AnalyzerHost": "analyzer.corp.local"}),
    R("HitachiStorage", "StorageSystem", "VSP-5600-01", props={"serialNumber": "60123", "model": "VSP 5600", "microcode": "90-09-21", "svp": "10.20.30.40"}),
    R("HitachiStorage", "Pool", "Pool-03", props={"poolId": "3", "poolType": "HDP", "storageSerial": "60123", "driveType": "SSD", "totalCapacityGB": "65536"}),
    R("HitachiStorage", "Pool", "Pool-07", props={"poolId": "7", "poolType": "HDP", "storageSerial": "60123", "driveType": "SSD", "totalCapacityGB": "32768"}),
    R("HitachiStorage", "LDEV", "00:10:A0", props={"ldevId": "4256", "poolId": "7", "storageSerial": "60123", "naaId": "naa.60060e80072a2c0000302a2c000010a0", "capacityGB": "5120", "label": "ds_prod_db_01"}),
    R("HitachiStorage", "LDEV", "00:10:A1", props={"ldevId": "4257", "poolId": "7", "storageSerial": "60123", "naaId": "naa.60060e80072a2c0000302a2c000010a1", "capacityGB": "4096", "label": "ds_batch_01"}),
    R("HitachiStorage", "LDEV", "00:20:B0", props={"ldevId": "8368", "poolId": "3", "storageSerial": "60123", "naaId": "naa.60060e80072a2c0000302a2c000020b0", "capacityGB": "8192", "label": "ds_prod_app_01"}),
    R("HitachiStorage", "Port", "CL1-A", props={"portId": "CL1-A", "portType": "FIBRE", "speedGbps": "32", "storageSerial": "60123"}),
    R("HitachiStorage", "Port", "CL2-A", props={"portId": "CL2-A", "portType": "FIBRE", "speedGbps": "32", "storageSerial": "60123"}),
    R("VMWARE_ARIA_OPERATIONS", "vC-Ops-Node", "aria-ops-01", props={"summary|version": "8.18.0"}),
]

BY_ID = {r["identifier"]: r for r in RESOURCES}
BY_KEY = {(r["adapterKind"], r["resourceKind"], r["name"]): r for r in RESOURCES}


def rid(ak: str, rk: str, name: str) -> str:
    return BY_KEY[(ak, rk, name)]["identifier"]


# parent -> children (CHILD direction). The Datastore→LDEV edge is what a
# storage management pack creates; it is the bridge between the two adapters.
EDGES: List[Tuple[str, str]] = []


def _edge(p: Tuple[str, str, str], c: Tuple[str, str, str]) -> None:
    EDGES.append((rid(*p), rid(*c)))


VM_ = lambda n: ("VMWARE", "VirtualMachine", n)
HOST_ = lambda n: ("VMWARE", "HostSystem", n)
DS_ = lambda n: ("VMWARE", "Datastore", n)
CL_ = lambda n: ("VMWARE", "ClusterComputeResource", n)
LDEV_ = lambda n: ("HitachiStorage", "LDEV", n)
POOL_ = lambda n: ("HitachiStorage", "Pool", n)
ARRAY = ("HitachiStorage", "StorageSystem", "VSP-5600-01")

_edge(("VMWARE", "VMwareAdapter Instance", "vc-prod-01 adapter"), ("VMWARE", "vCenter", "vc-prod-01"))
_edge(("VMWARE", "vCenter", "vc-prod-01"), ("VMWARE", "Datacenter", "DC-Prod"))
_edge(("VMWARE", "Datacenter", "DC-Prod"), CL_("CL-DB"))
_edge(("VMWARE", "Datacenter", "DC-Prod"), CL_("CL-App"))
for h in ("esx-db-01.corp.local", "esx-db-02.corp.local"):
    _edge(CL_("CL-DB"), HOST_(h))
for h in ("esx-app-01.corp.local", "esx-app-02.corp.local"):
    _edge(CL_("CL-App"), HOST_(h))
for name, host, ds, _, _ in VMS:
    _edge(HOST_(host), VM_(name))
    _edge(VM_(name), DS_(ds))            # VM uses datastore
    _edge(HOST_(host), DS_(ds))          # host mounts datastore
_edge(DS_("ds_prod_db_01"), LDEV_("00:10:A0"))
_edge(DS_("ds_batch_01"), LDEV_("00:10:A1"))
_edge(DS_("ds_prod_app_01"), LDEV_("00:20:B0"))
_edge(("HitachiStorage", "HitachiStorageAdapter Instance", "Ops Center Analyzer adapter"), ARRAY)
_edge(ARRAY, POOL_("Pool-03"))
_edge(ARRAY, POOL_("Pool-07"))
_edge(ARRAY, ("HitachiStorage", "Port", "CL1-A"))
_edge(ARRAY, ("HitachiStorage", "Port", "CL2-A"))
_edge(POOL_("Pool-07"), LDEV_("00:10:A0"))
_edge(POOL_("Pool-07"), LDEV_("00:10:A1"))
_edge(POOL_("Pool-03"), LDEV_("00:20:B0"))
EDGES = list(dict.fromkeys(EDGES))
CHILDREN: Dict[str, List[str]] = {}
PARENTS: Dict[str, List[str]] = {}
for p, c in EDGES:
    CHILDREN.setdefault(p, []).append(c)
    PARENTS.setdefault(c, []).append(p)


# ── incident: which (resource, statKey) deviate, when, and by how much ────────
# (start offset ms, end offset ms, multiplier). The order of onset IS the
# root-cause chain: batch VM IOPS → pool → LDEV/datastore → db VM latency.
IMPACT: Dict[Tuple[str, str], Tuple[int, int, float]] = {
    (rid(*VM_("batch-etl-01")), "virtualDisk|commandsAveraged_average"): (-3 * MIN, 40 * MIN, 6.0),
    (rid(*VM_("batch-etl-01")), "cpu|usage_average"): (-3 * MIN, 40 * MIN, 2.5),
    (rid(*LDEV_("00:10:A1")), "total_iops"): (-3 * MIN, 40 * MIN, 5.5),
    (rid(*POOL_("Pool-07")), "total_iops"): (0, 40 * MIN, 2.4),
    (rid(*POOL_("Pool-07")), "write_iops"): (0, 40 * MIN, 3.0),
    (rid(*POOL_("Pool-07")), "utilization_pct"): (0, 40 * MIN, 2.6),
    (rid(*POOL_("Pool-07")), "response_time"): (0, 40 * MIN, 9.0),
    (rid(*ARRAY), "cache_write_pending_rate"): (0, 40 * MIN, 3.5),
    (rid(*LDEV_("00:10:A0")), "response_time"): (2 * MIN, 41 * MIN, 8.0),
    (rid(*DS_("ds_prod_db_01")), "datastore|totalLatency"): (2 * MIN, 41 * MIN, 8.0),
    (rid(*DS_("ds_batch_01")), "datastore|totalLatency"): (0, 40 * MIN, 7.0),
    (rid(*DS_("ds_batch_01")), "datastore|numberWriteAveraged_average"): (-3 * MIN, 40 * MIN, 5.0),
    (rid(*VM_("prod-db-01")), "virtualDisk|totalLatency"): (3 * MIN, 42 * MIN, 7.5),
    (rid(*VM_("prod-db-01")), "virtualDisk|totalWriteLatency"): (3 * MIN, 42 * MIN, 8.5),
    (rid(*VM_("prod-db-01")), "virtualDisk|totalReadLatency"): (3 * MIN, 42 * MIN, 6.0),
    (rid(*VM_("prod-db-01")), "badge|health"): (4 * MIN, 42 * MIN, 0.45),
    (rid(*VM_("prod-db-02")), "virtualDisk|totalLatency"): (3 * MIN, 42 * MIN, 5.0),
    (rid(*HOST_("esx-db-01.corp.local")), "disk|totalLatency_average"): (3 * MIN, 42 * MIN, 6.0),
}
# Health during the incident (what the resource list shows while it lasts) —
# the seeded "now" is after the incident, so every object is back to GREEN
# except the batch VM, which stays YELLOW (its workload is still high).
BY_KEY[VM_("batch-etl-01")]["health"] = "YELLOW"
BY_KEY[VM_("batch-etl-01")]["healthValue"] = 74.0


def _baseline(rk_key: Tuple[str, str], stat: str) -> Optional[float]:
    for row in STAT_KEYS.get(rk_key, []):
        if row[0] == stat:
            return float(row[6])
    return None


def _value_at(res: dict, stat: str, ts: int) -> Optional[float]:
    base = _baseline((res["adapterKind"], res["resourceKind"]), stat)
    if base is None:
        return None
    # Per-resource offset so two VMs never share a series.
    h = int(hashlib.sha256(f"{res['identifier']}:{stat}".encode()).hexdigest()[:8], 16)
    base = base * (1 + ((h % 400) / 1000.0 - 0.2))
    # Diurnal wobble + deterministic noise (±8%).
    hour = (ts // 3_600_000) % 24
    diurnal = 1 + 0.12 * math.sin((hour - 6) / 24 * 2 * math.pi)
    n = int(hashlib.sha256(f"{res['identifier']}:{stat}:{ts // COLLECTION_INTERVAL_MS}".encode()).hexdigest()[:6], 16)
    noise = 1 + ((n % 160) / 1000.0 - 0.08)
    v = base * diurnal * noise
    imp = IMPACT.get((res["identifier"], stat))
    if imp:
        s, e, mult = imp
        if T0 + s <= ts <= T0 + e:
            # ramp over the first 4 minutes, plateau, drop at the end
            frac = min(1.0, (ts - (T0 + s)) / (4 * MIN))
            v = v * (1 + (mult - 1) * frac)
    return round(v, 2)


def _dt_band(res: dict, stat: str, ts: int) -> Tuple[float, float]:
    """Dynamic threshold: the 'normal' band Aria learned — baseline ±35%."""
    base = _baseline((res["adapterKind"], res["resourceKind"]), stat) or 0.0
    h = int(hashlib.sha256(f"{res['identifier']}:{stat}".encode()).hexdigest()[:8], 16)
    base = base * (1 + ((h % 400) / 1000.0 - 0.2))
    hour = (ts // 3_600_000) % 24
    diurnal = 1 + 0.12 * math.sin((hour - 6) / 24 * 2 * math.pi)
    v = base * diurnal
    return round(v * 0.65, 2), round(v * 1.35, 2)


# ── alerts / symptoms / definitions ───────────────────────────────────────────

ALERT_DEFINITIONS = [
    {"id": "AlertDefinition-HitachiStorage-Pool-RT", "name": "Storage pool response time is high",
     "description": "Pool response time is above the dynamic threshold for 3 consecutive cycles.",
     "adapterKindKey": "HitachiStorage", "resourceKindKey": "Pool", "type": 16, "subType": 18,
     "waitCycles": 1, "cancelCycles": 1, "severity": "CRITICAL", "impact": "HEALTH"},
    {"id": "AlertDefinition-HitachiStorage-Pool-Util", "name": "Storage pool utilization is high",
     "description": "Pool busy rate above 80%.", "adapterKindKey": "HitachiStorage",
     "resourceKindKey": "Pool", "type": 16, "subType": 18, "waitCycles": 2, "cancelCycles": 1,
     "severity": "WARNING", "impact": "RISK"},
    {"id": "AlertDefinition-VMWARE-Datastore-Latency", "name": "Datastore has high latency",
     "description": "Datastore total latency is above 20 ms.", "adapterKindKey": "VMWARE",
     "resourceKindKey": "Datastore", "type": 16, "subType": 18, "waitCycles": 1, "cancelCycles": 1,
     "severity": "IMMEDIATE", "impact": "HEALTH"},
    {"id": "AlertDefinition-VMWARE-VM-DiskLatency", "name": "Virtual machine has disk I/O latency problem",
     "description": "One or more virtual disks report latency above the dynamic threshold.",
     "adapterKindKey": "VMWARE", "resourceKindKey": "VirtualMachine", "type": 16, "subType": 18,
     "waitCycles": 1, "cancelCycles": 1, "severity": "WARNING", "impact": "HEALTH"},
    {"id": "AlertDefinition-VMWARE-VM-HighIOPS", "name": "Virtual machine has unusually high IOPS workload",
     "description": "Disk commands per second is above the dynamic threshold.", "adapterKindKey": "VMWARE",
     "resourceKindKey": "VirtualMachine", "type": 16, "subType": 18, "waitCycles": 1, "cancelCycles": 2,
     "severity": "INFORMATION", "impact": "EFFICIENCY"},
    {"id": "AlertDefinition-VMWARE-Host-CPU", "name": "Host has CPU contention",
     "description": "Host CPU usage above 90% for 15 minutes.", "adapterKindKey": "VMWARE",
     "resourceKindKey": "HostSystem", "type": 16, "subType": 18, "waitCycles": 3, "cancelCycles": 1,
     "severity": "WARNING", "impact": "HEALTH"},
]

SYMPTOM_DEFINITIONS = {
    "SymptomDefinition-Pool-RT-DT": ("Pool response time above dynamic threshold", "response_time"),
    "SymptomDefinition-Pool-Util": ("Pool utilization above 80%", "utilization_pct"),
    "SymptomDefinition-DS-Latency": ("Datastore total latency above 20 ms", "datastore|totalLatency"),
    "SymptomDefinition-VM-DiskLatency-DT": ("Virtual disk latency above dynamic threshold", "virtualDisk|totalLatency"),
    "SymptomDefinition-VM-IOPS-DT": ("Disk commands per second above dynamic threshold", "virtualDisk|commandsAveraged_average"),
}


def _alert(i: int, res_key, defn_id: str, start_off: int, cancel_off: Optional[int]) -> dict:
    d = next(a for a in ALERT_DEFINITIONS if a["id"] == defn_id)
    rid_ = rid(*res_key)
    start = T0 + start_off
    cancel = (T0 + cancel_off) if cancel_off is not None else 0
    return {
        "alertId": _rid(f"alert:{i}"), "resourceId": rid_,
        "alertDefinitionId": defn_id, "alertDefinitionName": d["name"],
        "alertLevel": d["severity"], "alertImpact": d["impact"],
        "status": "CANCELED" if cancel else "ACTIVE",
        "controlState": "OPEN", "type": d["type"], "subType": d["subType"],
        "startTimeUTC": start, "updateTimeUTC": cancel or start,
        "cancelTimeUTC": cancel, "ownerId": None, "ownerName": None,
    }


ALERTS = [
    _alert(1, VM_("batch-etl-01"), "AlertDefinition-VMWARE-VM-HighIOPS", -2 * MIN, None),      # still active
    _alert(2, POOL_("Pool-07"), "AlertDefinition-HitachiStorage-Pool-RT", 1 * MIN, 42 * MIN),
    _alert(3, POOL_("Pool-07"), "AlertDefinition-HitachiStorage-Pool-Util", 2 * MIN, 41 * MIN),
    _alert(4, DS_("ds_prod_db_01"), "AlertDefinition-VMWARE-Datastore-Latency", 3 * MIN, 43 * MIN),
    _alert(5, DS_("ds_batch_01"), "AlertDefinition-VMWARE-Datastore-Latency", 1 * MIN, 41 * MIN),
    _alert(6, VM_("prod-db-01"), "AlertDefinition-VMWARE-VM-DiskLatency", 5 * MIN, 44 * MIN),
    _alert(7, VM_("prod-db-02"), "AlertDefinition-VMWARE-VM-DiskLatency", 6 * MIN, 44 * MIN),
    # Unrelated background noise, a day earlier, so "everything in the window" needs filtering.
    _alert(8, HOST_("esx-app-02.corp.local"), "AlertDefinition-VMWARE-Host-CPU", -26 * 60 * MIN, -25 * 60 * MIN),
]


def _symptom(i: int, res_key, sd_id: str, crit: str, start_off: int, cancel_off: Optional[int], msg: str) -> dict:
    name, stat = SYMPTOM_DEFINITIONS[sd_id]
    return {
        "id": _rid(f"symptom:{i}"), "resourceId": rid(*res_key), "symptomDefinitionId": sd_id,
        "symptomCriticality": crit, "message": msg, "kpi": True, "statKey": stat,
        "startTimeUTC": T0 + start_off,
        "cancelTimeUTC": (T0 + cancel_off) if cancel_off is not None else 0,
        "updateTimeUTC": T0 + (cancel_off if cancel_off is not None else start_off),
        "alarmInfo": name, "faultDevices": [],
    }


SYMPTOMS = [
    _symptom(1, VM_("batch-etl-01"), "SymptomDefinition-VM-IOPS-DT", "INFORMATION", -2 * MIN, None,
             "Virtual Disk|Commands per second is 1090 IOPS, above the dynamic threshold of 210 IOPS"),
    _symptom(2, POOL_("Pool-07"), "SymptomDefinition-Pool-RT-DT", "CRITICAL", 1 * MIN, 42 * MIN,
             "Performance|Response Time is 18.4 ms, above the dynamic threshold of 2.7 ms"),
    _symptom(3, POOL_("Pool-07"), "SymptomDefinition-Pool-Util", "WARNING", 2 * MIN, 41 * MIN,
             "Performance|Utilization % is 91 %, above 80 %"),
    _symptom(4, DS_("ds_prod_db_01"), "SymptomDefinition-DS-Latency", "IMMEDIATE", 3 * MIN, 43 * MIN,
             "Datastore|Total Latency is 24.6 ms, above 20 ms"),
    _symptom(5, DS_("ds_batch_01"), "SymptomDefinition-DS-Latency", "IMMEDIATE", 1 * MIN, 41 * MIN,
             "Datastore|Total Latency is 21.9 ms, above 20 ms"),
    _symptom(6, VM_("prod-db-01"), "SymptomDefinition-VM-DiskLatency-DT", "WARNING", 5 * MIN, 44 * MIN,
             "Virtual Disk|Total Latency is 30.1 ms, above the dynamic threshold of 5.4 ms"),
    _symptom(7, VM_("prod-db-02"), "SymptomDefinition-VM-DiskLatency-DT", "WARNING", 6 * MIN, 44 * MIN,
             "Virtual Disk|Total Latency is 20.3 ms, above the dynamic threshold of 5.1 ms"),
]
# alert -> contributing symptom ids (same order as ALERTS[0..6])
CONTRIBUTING = {ALERTS[i]["alertId"]: [SYMPTOMS[i]["id"]] for i in range(7)}

CUSTOM_GROUPS = [
    {"id": _rid("group:prod-db"), "name": "Production Databases", "policy": "Tier-1 policy",
     "members": [rid(*VM_("prod-db-01")), rid(*VM_("prod-db-02"))]},
    {"id": _rid("group:tier1-storage"), "name": "Tier-1 Storage", "policy": "Storage policy",
     "members": [rid(*POOL_("Pool-07")), rid(*POOL_("Pool-03")), rid(*DS_("ds_prod_db_01")), rid(*DS_("ds_prod_app_01"))]},
    {"id": _rid("group:batch"), "name": "Batch & ETL", "policy": "Default policy",
     "members": [rid(*VM_("batch-etl-01")), rid(*DS_("ds_batch_01"))]},
]


# ── auth / content negotiation ────────────────────────────────────────────────

def _check_auth(request: Request) -> Optional[Response]:
    auth = request.headers.get("authorization") or ""
    m = re.match(r"^(OpsToken|vRealizeOpsToken)\s+(\S+)$", auth)
    if not m:
        return JSONResponse({"message": "Unauthorized", "httpStatusCode": 401,
                             "apiErrorCode": 401, "moreInformation": []}, status_code=401)
    token = m.group(2)
    exp = _tokens.get(token)
    if not exp or exp < time.time():
        _tokens.pop(token, None)
        return JSONResponse({"message": "Invalid or expired token", "httpStatusCode": 401,
                             "apiErrorCode": 401, "moreInformation": []}, status_code=401)
    # Sliding expiry: each call extends the token by TOKEN_TTL (the real API does this).
    _tokens[token] = time.time() + TOKEN_TTL
    return None


def _require_json(request: Request) -> Optional[Response]:
    """The real API's historic default is XML; JSON needs an Accept header."""
    # `*/*` (what curl / python-requests send by default) counts as "no
    # preference" and gets the historic XML default, so a client that forgets
    # the explicit header is caught by the mock, not by the customer.
    accept = request.headers.get("accept") or ""
    if "application/json" not in accept and "application/*" not in accept:
        return Response(content='<?xml version="1.0"?><ops:error xmlns:ops="http://webservice.vmware.com/vRealizeOpsMgr/1.0/">'
                                "<!-- send Accept: application/json for JSON --></ops:error>",
                        media_type="application/xml")
    return None


def _guard(request: Request) -> Optional[Response]:
    return _check_auth(request) or _require_json(request)


def _page(request: Request, body: Optional[dict] = None) -> Tuple[int, int, Optional[Response]]:
    q = request.query_params
    try:
        page = int(q.get("page", 0))
        page_size = int(q.get("pageSize", MAX_PAGE_SIZE))
    except ValueError:
        return 0, 0, JSONResponse({"message": "page/pageSize must be integers"}, status_code=400)
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        return 0, 0, JSONResponse({"message": f"pageSize must be between 1 and {MAX_PAGE_SIZE}",
                                   "httpStatusCode": 400, "apiErrorCode": 1500}, status_code=400)
    return page, page_size, None


def _paged(items: List[Any], page: int, page_size: int, key: str, extra: Optional[dict] = None) -> dict:
    start = page * page_size
    out = {key: items[start:start + page_size],
           "pageInfo": {"totalCount": len(items), "page": page, "pageSize": page_size},
           "links": [{"href": "/suite-api/api/", "rel": "SELF", "name": "current"}]}
    if extra:
        out.update(extra)
    return out


def _resource_json(r: dict) -> dict:
    return {
        "creationTime": T0 - 90 * 24 * 3_600_000,
        "resourceKey": {
            "name": r["name"], "adapterKindKey": r["adapterKind"], "resourceKindKey": r["resourceKind"],
            "resourceIdentifiers": [{"identifierType": {"name": k, "dataType": "STRING", "isPartOfUniqueness": True},
                                     "value": v} for k, v in r["identifiers"].items()],
        },
        "resourceStatusStates": [{"adapterInstanceId": _rid("adapter-instance:" + r["adapterKind"]),
                                  "resourceStatus": "DATA_RECEIVING", "resourceState": "STARTED",
                                  "statusMessage": ""}],
        "resourceHealth": r["health"], "resourceHealthValue": r["healthValue"],
        "dtEnabled": True, "monitoringInterval": 5, "monitoringIntervalSeconds": 300,
        "badges": [{"type": "HEALTH", "color": r["health"], "score": r["healthValue"]},
                   {"type": "RISK", "color": "GREEN", "score": 88.0},
                   {"type": "EFFICIENCY", "color": "GREEN", "score": 92.0}],
        "relatedResources": [], "links": [], "identifier": r["identifier"],
    }


def _lst(q, name: str) -> List[str]:
    """Repeated query params (`resourceId=a&resourceId=b`) and comma lists."""
    vals = q.getlist(name) if hasattr(q, "getlist") else []
    out: List[str] = []
    for v in vals:
        out.extend(x for x in v.split(",") if x)
    return out


def _filter_resources(f: dict) -> List[dict]:
    rows = RESOURCES
    def aslist(v):
        if v is None:
            return []
        return v if isinstance(v, list) else [v]
    if aslist(f.get("resourceId")):
        ids = set(aslist(f["resourceId"]))
        rows = [r for r in rows if r["identifier"] in ids]
    if aslist(f.get("adapterKind")):
        ak = set(aslist(f["adapterKind"]))
        rows = [r for r in rows if r["adapterKind"] in ak]
    if aslist(f.get("resourceKind")):
        rk = set(aslist(f["resourceKind"]))
        rows = [r for r in rows if r["resourceKind"] in rk]
    if aslist(f.get("name")):
        names = set(aslist(f["name"]))
        rows = [r for r in rows if r["name"] in names]
    if aslist(f.get("regex")):
        pats = [re.compile(p) for p in aslist(f["regex"])]
        rows = [r for r in rows if any(p.search(r["name"]) for p in pats)]
    if aslist(f.get("parentId")):
        kids = set()
        for pid in aslist(f["parentId"]):
            kids.update(CHILDREN.get(pid, []))
        rows = [r for r in rows if r["identifier"] in kids]
    if aslist(f.get("resourceHealth")):
        hs = set(aslist(f["resourceHealth"]))
        rows = [r for r in rows if r["health"] in hs]
    if f.get("propertyName"):
        pn, pv = f["propertyName"], f.get("propertyValue")
        rows = [r for r in rows if pn in r["props"] and (pv is None or r["props"][pn] == pv)]
    pc = f.get("propertyConditions") or {}
    conds = pc.get("conditions") or []
    if conds:
        conj = (pc.get("conjunctionOperator") or "AND").upper()

        def ok(r, c):
            val = r["props"].get(c.get("key"))
            op = (c.get("operator") or "EQ").upper()
            want = c.get("stringValue")
            if op == "EXISTS":
                return val is not None
            if op == "NOT_EXISTS":
                return val is None
            if val is None:
                return False
            if op == "EQ":
                return val == want
            if op == "NOT_EQ":
                return val != want
            if op in ("LIKE", "CONTAINS"):
                return (want or "") in val
            if op == "STARTS_WITH":
                return val.startswith(want or "")
            if op == "ENDS_WITH":
                return val.endswith(want or "")
            if op == "REGEX":
                return re.search(want or "", val) is not None
            if op == "IN":
                return val in (want or "").split(",")
            return False
        rows = [r for r in rows if (all if conj == "AND" else any)(ok(r, c) for c in conds)]
    return rows


# ── endpoints: auth & meta ────────────────────────────────────────────────────

@app.post("/suite-api/api/auth/token/acquire")
async def acquire_token(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"message": "Malformed request body"}, status_code=400)
    src = body.get("authSource") or "LOCAL"
    user, pw = body.get("username"), body.get("password")
    if USERS.get((src, user)) != pw:
        return JSONResponse({"message": "Invalid username/password", "httpStatusCode": 401,
                             "apiErrorCode": 1201, "moreInformation": [{"name": "authSource", "value": src}]},
                            status_code=401)
    token = secrets.token_urlsafe(32)
    exp = time.time() + TOKEN_TTL
    _tokens[token] = exp
    return {"token": token, "validity": int(exp * 1000), "expiresAt": datetime.fromtimestamp(exp, timezone.utc).strftime("%A, %B %d, %Y %I:%M:%S %p UTC"),
            "roles": ["Administrator"] if user == "admin" else ["ReadOnly"]}


@app.get("/suite-api/api/versions/current")
async def version_current(request: Request):
    if (err := _guard(request)) is not None:
        return err
    return {"major": 8, "minor": 18, "minorMinor": 0, "patch": 0, "releaseName": "8.18.0",
            "buildNumber": 24025145, "humanlyReadableReleaseDate": "July 23, 2024",
            "releasedDate": 1721692800000, "description": "VMware Aria Operations 8.18"}


@app.get("/__mock/scenario")
async def scenario():
    return {**SCENARIO, "incident_start_iso": datetime.fromtimestamp(T0 / 1000, timezone.utc).isoformat(),
            "token_ttl_seconds": TOKEN_TTL}


# ── endpoints: adapter kinds ──────────────────────────────────────────────────

@app.get("/suite-api/api/adapterkinds")
async def adapterkinds(request: Request):
    if (err := _guard(request)) is not None:
        return err
    wanted = set(_lst(request.query_params, "adapterKindKey"))
    with_rk = (request.query_params.get("retrieveResourceKindInfos") or "true").lower() != "false"
    out = []
    for ak in ADAPTER_KINDS:
        if wanted and ak["key"] not in wanted:
            continue
        row = {**ak, "identifiers": [], "links": []}
        if with_rk:
            row["resourceKinds"] = [k for k, _, _ in RESOURCE_KINDS.get(ak["key"], [])]
        out.append(row)
    return {"adapter-kind": out}


@app.get("/suite-api/api/adapterkinds/{key}")
async def adapterkind(request: Request, key: str):
    if (err := _guard(request)) is not None:
        return err
    for ak in ADAPTER_KINDS:
        if ak["key"] == key:
            return {**ak, "identifiers": [], "links": [],
                    "resourceKinds": [k for k, _, _ in RESOURCE_KINDS.get(key, [])]}
    return JSONResponse({"message": f"Adapter kind {key} not found"}, status_code=404)


@app.get("/suite-api/api/adapterkinds/{key}/resourcekinds")
async def resourcekinds(request: Request, key: str):
    if (err := _guard(request)) is not None:
        return err
    if key not in RESOURCE_KINDS:
        return JSONResponse({"message": f"Adapter kind {key} not found"}, status_code=404)
    page, size, bad = _page(request)
    if bad:
        return bad
    rkt = request.query_params.get("resourceKindType")
    rows = [{"key": k, "name": n, "adapterKind": key, "adapterKindName": next(a["name"] for a in ADAPTER_KINDS if a["key"] == key),
             "resourceKindType": t, "resourceKindSubType": "NONE", "resourceIdentifierTypes": [], "links": []}
            for k, n, t in RESOURCE_KINDS[key] if not rkt or t == rkt]
    return _paged(rows, page, size, "resource-kind", {"adapterKind": key})


@app.get("/suite-api/api/adapterkinds/{ak}/resourcekinds/{rk}/statkeys")
async def statkeys(request: Request, ak: str, rk: str):
    if (err := _guard(request)) is not None:
        return err
    if ak not in RESOURCE_KINDS or rk not in {k for k, _, _ in RESOURCE_KINDS[ak]}:
        return JSONResponse({"message": f"Resource kind {ak}/{rk} not found"}, status_code=404)
    rows = STAT_KEYS.get((ak, rk), [])
    return {"resourceTypeAttributes": [
        {"key": key, "name": name, "description": desc, "unit": unit, "rollupType": roll,
         "dataType": "STRING", "dataType2": dt2, "instanceType": "AGGREGATED", "defaultMonitored": True,
         "monitoring": True, "property": False, "attributeKey": key, "dtSubType": "linear",
         "localizations": [], "unitLocalizations": []}
        for key, name, unit, roll, dt2, desc, _ in rows]}


# ── endpoints: resources ──────────────────────────────────────────────────────

@app.get("/suite-api/api/resources")
async def resources_get(request: Request):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    q = request.query_params
    f = {k: _lst(q, k) for k in ("name", "regex", "adapterKind", "resourceKind", "resourceId", "parentId", "resourceHealth")}
    if q.get("propertyName"):
        f["propertyName"], f["propertyValue"] = q.get("propertyName"), q.get("propertyValue")
    rows = _filter_resources(f)
    return _paged([_resource_json(r) for r in rows], page, size, "resourceList")


@app.post("/suite-api/api/resources/query")
async def resources_query(request: Request):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    body = await request.json()
    rows = _filter_resources(body or {})
    return _paged([_resource_json(r) for r in rows], page, size, "resourceList")


# NB: declared before /resources/{res_id} so 'groups' is not captured as an id.
@app.get("/suite-api/api/resources/groups")
async def groups(request: Request):
    if (err := _guard(request)) is not None:
        return err
    wanted = set(_lst(request.query_params, "groupId"))
    return {"groups": [_group_json(g) for g in CUSTOM_GROUPS if not wanted or g["id"] in wanted]}


@app.get("/suite-api/api/resources/groups/{group_id}/members")
async def group_members(request: Request, group_id: str):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    g = next((g for g in CUSTOM_GROUPS if g["id"] == group_id), None)
    if not g:
        return JSONResponse({"message": "Group not found", "httpStatusCode": 404}, status_code=404)
    return _paged([_resource_json(BY_ID[m]) for m in g["members"]], page, size, "resourceList")



@app.get("/suite-api/api/resources/{res_id}")
async def resource_get(request: Request, res_id: str):
    if (err := _guard(request)) is not None:
        return err
    r = BY_ID.get(res_id)
    return _resource_json(r) if r else JSONResponse({"message": "Resource not found", "httpStatusCode": 404}, status_code=404)


@app.get("/suite-api/api/resources/{res_id}/properties")
async def resource_properties(request: Request, res_id: str):
    if (err := _guard(request)) is not None:
        return err
    r = BY_ID.get(res_id)
    if not r:
        return JSONResponse({"message": "Resource not found", "httpStatusCode": 404}, status_code=404)
    return {"resourceId": res_id, "property": [{"name": k, "value": v} for k, v in r["props"].items()]}


@app.post("/suite-api/api/resources/properties/latest/query")
async def properties_latest_query(request: Request):
    if (err := _guard(request)) is not None:
        return err
    body = await request.json()
    ids = body.get("resourceIds") or []
    keys = body.get("propertyKeys") or []
    out = []
    for i in ids:
        r = BY_ID.get(i)
        if not r:
            continue
        contents = [{"statKey": k, "timestamps": [T_END + 60 * MIN], "values": [v], "data": []}
                    for k, v in r["props"].items() if not keys or k in keys]
        out.append({"resourceId": i, "property-contents": {"property-content": contents}})
    return {"values": out}


@app.get("/suite-api/api/resources/{res_id}/relationships")
async def relationships(request: Request, res_id: str):
    if (err := _guard(request)) is not None:
        return err
    if res_id not in BY_ID:
        return JSONResponse({"message": "Resource not found", "httpStatusCode": 404}, status_code=404)
    page, size, bad = _page(request)
    if bad:
        return bad
    rt = (request.query_params.get("relationshipType") or "ALL").upper()
    if rt not in ("PARENT", "CHILD", "ALL"):
        return JSONResponse({"message": "relationshipType must be PARENT, CHILD or ALL"}, status_code=400)
    ids: List[str] = []
    if rt in ("PARENT", "ALL"):
        ids += PARENTS.get(res_id, [])
    if rt in ("CHILD", "ALL"):
        ids += CHILDREN.get(res_id, [])
    rows = [_resource_json(BY_ID[i]) for i in ids]
    return _paged(rows, page, size, "resourceList", {"relationshipType": rt})


@app.get("/suite-api/api/resources/{res_id}/statkeys")
async def resource_statkeys(request: Request, res_id: str):
    if (err := _guard(request)) is not None:
        return err
    r = BY_ID.get(res_id)
    if not r:
        return JSONResponse({"message": "Resource not found"}, status_code=404)
    return {"stat-key": [{"key": row[0]} for row in STAT_KEYS.get((r["adapterKind"], r["resourceKind"]), [])]}


# ── endpoints: stats ──────────────────────────────────────────────────────────

_INTERVAL_MS = {"SECONDS": 1_000, "MINUTES": 60_000, "HOURS": 3_600_000, "DAYS": 86_400_000,
                "WEEKS": 7 * 86_400_000, "MONTHS": 30 * 86_400_000, "YEARS": 365 * 86_400_000}


def _series(res: dict, stat: str, begin: int, end: int, roll: str, itype: Optional[str], iq: int,
            dt: bool) -> Optional[dict]:
    if _baseline((res["adapterKind"], res["resourceKind"]), stat) is None:
        return None
    first = (begin // COLLECTION_INTERVAL_MS) * COLLECTION_INTERVAL_MS
    raw = []
    ts = first
    while ts <= end:
        if ts >= begin:
            raw.append((ts, _value_at(res, stat, ts)))
        ts += COLLECTION_INTERVAL_MS
        if len(raw) > 20_000:
            break
    if itype and itype in _INTERVAL_MS and roll not in ("NONE", None):
        bucket = _INTERVAL_MS[itype] * max(1, iq)
        groups: Dict[int, List[float]] = {}
        for t, v in raw:
            groups.setdefault((t // bucket) * bucket, []).append(v)
        agg = {"SUM": sum, "AVG": lambda xs: sum(xs) / len(xs), "MIN": min, "MAX": max,
               "LATEST": lambda xs: xs[-1], "COUNT": len}.get(roll, lambda xs: sum(xs) / len(xs))
        raw = [(t, round(float(agg(vs)), 2)) for t, vs in sorted(groups.items())]
    out = {"statKey": {"key": stat}, "timestamps": [t for t, _ in raw], "data": [v for _, v in raw],
           "rollUpType": roll or "AVG",
           "intervalUnit": {"intervalType": itype or "MINUTES", "quantifier": iq if itype else 5}}
    if dt:
        bands = [_dt_band(res, stat, t) for t, _ in raw]
        out["dtTimestamps"] = [t for t, _ in raw]
        out["minThresholdData"] = [b[0] for b in bands]
        out["maxThresholdData"] = [b[1] for b in bands]
    return out


def _stats_query(body: dict, ids: List[str]) -> dict:
    now = int(time.time() * 1000)
    end = int(body.get("end") or now)
    begin = int(body.get("begin") or (end - 24 * 3_600_000))
    begin = max(begin, now - RETENTION_DAYS * 86_400_000)
    roll = (body.get("rollUpType") or "AVG").upper()
    itype = (body.get("intervalType") or None)
    iq = int(body.get("intervalQuantifier") or 1)
    dt = bool(body.get("dt"))
    keys = body.get("statKey") or []
    values = []
    for i in ids:
        r = BY_ID.get(i)
        if not r:
            continue
        stats = []
        for k in (keys or [row[0] for row in STAT_KEYS.get((r["adapterKind"], r["resourceKind"]), [])]):
            s = _series(r, k, begin, end, roll, itype.upper() if itype else None, iq, dt)
            if s:
                stats.append(s)
        values.append({"resourceId": i, "stat-list": {"stat": stats}})
    return {"values": values}


@app.post("/suite-api/api/resources/stats/query")
async def stats_query(request: Request):
    if (err := _guard(request)) is not None:
        return err
    body = await request.json()
    ids = body.get("resourceId") or []
    if not ids:
        return JSONResponse({"message": "resourceId is required", "httpStatusCode": 400}, status_code=400)
    return _stats_query(body, ids)


@app.post("/suite-api/api/resources/{res_id}/stats/query")
async def stats_query_one(request: Request, res_id: str):
    if (err := _guard(request)) is not None:
        return err
    body = await request.json()
    return _stats_query(body, [res_id])


def _latest(ids: List[str], keys: List[str], max_samples: int) -> dict:
    now = int(time.time() * 1000)
    values = []
    for i in ids:
        r = BY_ID.get(i)
        if not r:
            continue
        stats = []
        for k in (keys or [row[0] for row in STAT_KEYS.get((r["adapterKind"], r["resourceKind"]), [])]):
            if _baseline((r["adapterKind"], r["resourceKind"]), k) is None:
                continue
            last = (now // COLLECTION_INTERVAL_MS) * COLLECTION_INTERVAL_MS
            ts = [last - (max_samples - 1 - n) * COLLECTION_INTERVAL_MS for n in range(max_samples)]
            stats.append({"statKey": {"key": k}, "timestamps": ts, "data": [_value_at(r, k, t) for t in ts],
                          "rollUpType": "LATEST", "intervalUnit": {"intervalType": "MINUTES", "quantifier": 5}})
        values.append({"resourceId": i, "stat-list": {"stat": stats}})
    return {"values": values}


@app.post("/suite-api/api/resources/stats/latest/query")
async def stats_latest_query(request: Request):
    if (err := _guard(request)) is not None:
        return err
    body = await request.json()
    ids = body.get("resourceId") or []
    if not ids or len(ids) > MAX_PAGE_SIZE:
        return JSONResponse({"message": "resourceId must contain 1..1000 items", "httpStatusCode": 400}, status_code=400)
    return _latest(ids, body.get("statKey") or [], max(1, int(body.get("maxSamples") or 1)))


@app.get("/suite-api/api/resources/stats/latest")
async def stats_latest_get(request: Request):
    if (err := _guard(request)) is not None:
        return err
    ids = _lst(request.query_params, "resourceId")
    if not ids or len(ids) > MAX_PAGE_SIZE:
        return JSONResponse({"message": "resourceId must contain 1..1000 items", "httpStatusCode": 400}, status_code=400)
    return _latest(ids, _lst(request.query_params, "statKey"), max(1, int(request.query_params.get("maxSamples") or 1)))


@app.get("/suite-api/api/resources/stats/topn")
async def stats_topn(request: Request):
    if (err := _guard(request)) is not None:
        return err
    q = request.query_params
    try:
        top_n = int(q.get("topN"))
    except (TypeError, ValueError):
        return JSONResponse({"message": "topN is required", "httpStatusCode": 400}, status_code=400)
    ids = _lst(q, "resourceId")
    keys = _lst(q, "statKey")
    if not ids or not keys:
        return JSONResponse({"message": "resourceId and statKey are required", "httpStatusCode": 400}, status_code=400)
    group_by = (q.get("groupBy") or "STATKEY").upper()
    order = (q.get("sortOrder") or "DESCENDING").upper()
    body = {k: q.get(k) for k in ("begin", "end", "rollUpType", "intervalType", "intervalQuantifier") if q.get(k)}
    data = _stats_query({**body, "statKey": keys}, ids)
    # Aggregate each (resource, stat) series to one number per the roll-up.
    rows = []
    roll = (body.get("rollUpType") or "AVG").upper()
    for v in data["values"]:
        for s in v["stat-list"]["stat"]:
            d = s["data"]
            if not d:
                continue
            val = {"SUM": sum(d), "MIN": min(d), "MAX": max(d), "LATEST": d[-1], "COUNT": len(d)}.get(roll, sum(d) / len(d))
            rows.append((s["statKey"]["key"], v["resourceId"], round(val, 2), s["timestamps"][-1]))
    groups: Dict[str, List[Tuple]] = {}
    for stat, r_id, val, ts in rows:
        groups.setdefault(stat if group_by == "STATKEY" else r_id, []).append((stat, r_id, val, ts))
    out = []
    for gk, items in groups.items():
        items.sort(key=lambda x: x[2], reverse=(order == "DESCENDING"))
        out.append({"groupKey": gk, "links": [], "resourceStats": [
            {"resourceId": r_id, "stat-list": {"stat": [{"statKey": {"key": stat}, "timestamps": [ts], "data": [val],
                                                         "rollUpType": roll}]}}
            for stat, r_id, val, ts in items[:top_n]]})
    return {"groupBy": group_by, "sortOrder": order, "resourceStatGroups": out}


@app.get("/suite-api/api/resources/{res_id}/stats/dt")
async def stats_dt(request: Request, res_id: str):
    if (err := _guard(request)) is not None:
        return err
    q = request.query_params
    ids = _lst(q, "resourceId") or [res_id]
    keys = _lst(q, "statKey")
    if not keys:
        return JSONResponse({"message": "statKey is required", "httpStatusCode": 400}, status_code=400)
    body = {"statKey": keys, "dt": True}
    for k in ("begin", "end"):
        if q.get(k):
            body[k] = q.get(k)
    return _stats_query(body, ids)


# ── endpoints: groups ─────────────────────────────────────────────────────────

def _group_json(g: dict) -> dict:
    return {"id": g["id"], "autoResolveMembership": False, "policy": g["policy"],
            "resourceKey": {"name": g["name"], "adapterKindKey": "Container", "resourceKindKey": "Environment",
                            "resourceIdentifiers": []},
            "membershipDefinition": {"includedResources": g["members"], "excludedResources": [], "rules": []},
            "links": []}


# ── endpoints: alerts / symptoms / definitions ────────────────────────────────

def _resource_scope(rq: Optional[dict], include_children: bool) -> Optional[set]:
    """Resolve an embedded resource-query to a set of ids (None = no scope)."""
    if not rq:
        return None
    ids = {r["identifier"] for r in _filter_resources(rq)}
    if include_children:
        frontier = list(ids)
        while frontier:
            n = frontier.pop()
            for c in CHILDREN.get(n, []):
                if c not in ids:
                    ids.add(c)
                    frontier.append(c)
    return ids


def _in_range(ts: int, rng: Optional[dict]) -> bool:
    if not rng:
        return True
    lo, hi = rng.get("startTime"), rng.get("endTime")
    if lo is not None and ts < int(lo):
        return False
    if hi is not None and ts > int(hi):
        return False
    return True


def _filter_alerts(body: dict) -> List[dict]:
    rows = ALERTS
    if body.get("activeOnly"):
        rows = [a for a in rows if a["status"] != "CANCELED"]
    if body.get("alertId"):
        s = set(body["alertId"]); rows = [a for a in rows if a["alertId"] in s]
    if body.get("alertDefinitionId"):
        s = set(body["alertDefinitionId"]); rows = [a for a in rows if a["alertDefinitionId"] in s]
    if body.get("alertCriticality"):
        s = set(body["alertCriticality"]); rows = [a for a in rows if a["alertLevel"] in s]
    if body.get("alertStatus"):
        s = set(body["alertStatus"]); rows = [a for a in rows if a["status"] in s]
    if body.get("alertImpact"):
        s = set(body["alertImpact"]); rows = [a for a in rows if a["alertImpact"] in s]
    if body.get("alertName"):
        rows = [a for a in rows if body["alertName"].lower() in a["alertDefinitionName"].lower()]
    scope = _resource_scope(body.get("resource-query"), bool(body.get("includeChildrenResources")))
    if scope is not None:
        rows = [a for a in rows if a["resourceId"] in scope]
    rows = [a for a in rows if _in_range(a["startTimeUTC"], body.get("startTimeRange"))]
    rows = [a for a in rows if _in_range(a["updateTimeUTC"], body.get("updateTimeRange"))]
    if body.get("cancelTimeRange"):
        rows = [a for a in rows if a["cancelTimeUTC"] and _in_range(a["cancelTimeUTC"], body["cancelTimeRange"])]
    return rows


@app.post("/suite-api/api/alerts/query")
async def alerts_query(request: Request):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    body = await request.json()
    return _paged(_filter_alerts(body or {}), page, size, "alerts")


@app.get("/suite-api/api/alerts")
async def alerts_get(request: Request):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    q = request.query_params
    body: dict = {"activeOnly": (q.get("activeOnly") or "false").lower() == "true"}
    for k in ("alertId", "alertDefinitionId", "alertCriticality", "alertStatus"):
        if _lst(q, k):
            body[k] = _lst(q, k)
    if _lst(q, "resourceId"):
        body["resource-query"] = {"resourceId": _lst(q, "resourceId")}
    return _paged(_filter_alerts(body), page, size, "alerts")


@app.get("/suite-api/api/alerts/contributingsymptoms")
async def contributing_symptoms(request: Request):
    if (err := _guard(request)) is not None:
        return err
    ids = _lst(request.query_params, "id")
    if not ids:
        return JSONResponse({"message": "id is required", "httpStatusCode": 400}, status_code=400)
    out = []
    for a in ids:
        syms = CONTRIBUTING.get(a, [])
        out.append({"alertId": a, "contributingSymptoms": {"contributingSymptoms": [
            {"symptomId": s, "symptomSetId": _rid("symptomset:" + a),
             "symptomDefinitionsIds": [next(x["symptomDefinitionId"] for x in SYMPTOMS if x["id"] == s)],
             "alertConditions": [{"id": _rid("cond:" + s), "severity": next(x["symptomCriticality"] for x in SYMPTOMS if x["id"] == s),
                                  "waitCycles": 1, "cancelCycles": 1, "condition": {}}]}
            for s in syms]}})
    return {"contributingSymptoms": out}


@app.get("/suite-api/api/alerts/{alert_id}")
async def alert_get(request: Request, alert_id: str):
    if (err := _guard(request)) is not None:
        return err
    a = next((a for a in ALERTS if a["alertId"] == alert_id), None)
    return a if a else JSONResponse({"message": "Alert not found", "httpStatusCode": 404}, status_code=404)


def _filter_symptoms(body: dict) -> List[dict]:
    rows = SYMPTOMS
    if body.get("activeOnly"):
        rows = [s for s in rows if not s["cancelTimeUTC"]]
    if body.get("alarmCriticality"):
        s = set(body["alarmCriticality"]); rows = [x for x in rows if x["symptomCriticality"] in s]
    scope = _resource_scope(body.get("resource-query"), bool(body.get("includeChildrenResources")))
    if scope is not None:
        rows = [x for x in rows if x["resourceId"] in scope]
    if body.get("cancelTimeRange"):
        rows = [x for x in rows if x["cancelTimeUTC"] and _in_range(x["cancelTimeUTC"], body["cancelTimeRange"])]
    return rows


@app.post("/suite-api/api/symptoms/query")
async def symptoms_query(request: Request):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    body = await request.json()
    return _paged(_filter_symptoms(body or {}), page, size, "symptom")


@app.get("/suite-api/api/symptoms")
async def symptoms_get(request: Request):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    q = request.query_params
    body: dict = {"activeOnly": (q.get("activeOnly") or "true").lower() == "true"}
    if _lst(q, "resourceId"):
        body["resource-query"] = {"resourceId": _lst(q, "resourceId")}
    return _paged(_filter_symptoms(body), page, size, "symptom")


def _defn_json(d: dict) -> dict:
    return {"id": d["id"], "name": d["name"], "description": d["description"],
            "adapterKindKey": d["adapterKindKey"], "resourceKindKey": d["resourceKindKey"],
            "type": d["type"], "subType": d["subType"], "waitCycles": d["waitCycles"], "cancelCycles": d["cancelCycles"],
            "forVCDTenants": False,
            "states": [{"severity": d["severity"], "impact": {"impactType": "BADGE", "detail": d["impact"].lower()},
                        "base-symptom-set": {}, "recommendationPriorityMap": {}}]}


def _filter_defs(body: dict) -> List[dict]:
    rows = ALERT_DEFINITIONS
    if body.get("ids"):
        s = set(body["ids"]); rows = [d for d in rows if d["id"] in s]
    if body.get("adapterKinds"):
        s = set(body["adapterKinds"]); rows = [d for d in rows if d["adapterKindKey"] in s]
    if body.get("resourceKinds"):
        s = set(body["resourceKinds"]); rows = [d for d in rows if d["resourceKindKey"] in s]
    return [_defn_json(d) for d in rows]


@app.post("/suite-api/api/alertdefinitions/query")
async def alertdefinitions_query(request: Request):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    body = await request.json()
    return _paged(_filter_defs(body or {}), page, size, "alertDefinitions")


@app.get("/suite-api/api/alertdefinitions")
async def alertdefinitions_get(request: Request):
    if (err := _guard(request)) is not None:
        return err
    page, size, bad = _page(request)
    if bad:
        return bad
    q = request.query_params
    body = {"ids": _lst(q, "id"),
            "adapterKinds": [q.get("adapterKind")] if q.get("adapterKind") else [],
            "resourceKinds": [q.get("resourceKind")] if q.get("resourceKind") else []}
    return _paged(_filter_defs(body), page, size, "alertDefinitions")


@app.post("/suite-api/api/events")
async def push_event(request: Request):
    """Push-only, exactly like the real API: there is no event READ endpoint."""
    if (err := _guard(request)) is not None:
        return err
    return Response(status_code=201)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("ARIA_MOCK_PORT", "8443")))
