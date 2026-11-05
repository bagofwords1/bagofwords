"""Mock VMware vCenter Server 8.0 U3: the real vim25 SOAP wire plus the REST tagging API.

A real vCenter cannot run in CI (an appliance OVA on ESXi, plus a Broadcom
entitlement). The usual stand-in, govmomi's `vcsim`, is a Go binary. This mock
is pure Python and stays faithful to the wire format a different way: every
request is decoded and every response encoded with **pyVmomi's own SOAP
serializer and type system** (`pyVmomi.SoapAdapter`, the `vim25` type info).
Every property value is a real `vim.*` data object, so a wrong field name
raises at seed time, and the XML on the wire is what pyVmomi (and any other
vim25 client) expects from vCenter.

Surface served (everything `VCenterClient` touches, plus the legacy forms):

  GET  /sdk/vimServiceVersions.xml        API version negotiation (SmartConnect)
  POST /sdk                               SOAP vim25:
       RetrieveServiceContent, SessionManager.Login / Logout,
       ViewManager.CreateContainerView, View.DestroyView,
       PropertyCollector.RetrievePropertiesEx / ContinueRetrievePropertiesEx /
         CancelRetrievePropertiesEx / RetrieveProperties (legacy),
       PerformanceManager.QueryPerfProviderSummary / QueryAvailablePerfMetric /
         QueryPerf / QueryPerfCounter,
       EventManager.QueryEvents / CreateCollectorForEvents,
       EventHistoryCollector.ReadNextEvents / ReadPreviousEvents /
         ResetCollector / RewindCollector / DestroyCollector,
       TaskManager.CreateCollectorForTasks, TaskHistoryCollector.ReadNextTasks /
         DestroyCollector
       plus property reads (via the PropertyCollector) of
       PerformanceManager.perfCounter / historicalInterval,
       EventManager.description, LicenseManager.licenses, Alarm.info, ...
  POST   /api/session                     vAPI session (Basic) -> "token"
  DELETE /api/session
  GET    /api/cis/tagging/category[/{id}]
  GET    /api/cis/tagging/tag[/{id}]
  POST   /api/cis/tagging/tag-association?action=list-attached-objects-on-tags
  GET    /mock/state                      mock-only: open sessions + call counts

Faithfulness rules, enforced on purpose so the client is tested against them:
  - Login sets the `vmware_soap_session` cookie. Every other SOAP call without a
    live session gets a `NotAuthenticated` fault; bad credentials get
    `InvalidLogin`. Sessions are capped per user (VCENTER_MOCK_MAX_SESSIONS,
    default 550, the vAPI default), so a client that leaks sessions fails here
    the way it would on a real vCenter.
  - RetrievePropertiesEx pages at min(maxObjects, 500). Callers must follow
    `token` with ContinueRetrievePropertiesEx. An unknown property path is an
    `InvalidProperty` fault, the same as vpxd.
  - Realtime (20 s) perf exists only for VMs and hosts, for the last hour.
    Historical intervals (300/1800/7200/86400 s) run at statistics level 1:
    only level-1 counters, aggregate instance "" only. A historical QueryPerf
    of more than 256 metrics (vpxd.stats.maxQueryMetrics) is an
    `InvalidArgument` fault on `querySpec.size`.
  - Event and task collectors return at most 1000 per ReadNext*. A larger
    maxCount is `InvalidArgument`. Reads go forward, oldest first.
  - `triggeredAlarmState` propagates to ancestors (datacenter, root folder) the
    way vCenter shows it, so clients must de-duplicate by state key.
  - REST tagging needs the `vmware-api-session-id` header, else 401
    UNAUTHENTICATED. ESXi mode (VCENTER_MOCK_MODE=esxi) has no /api at all.

Seeded estate: two datacenters, a 4-host prod cluster, a 2-host dev cluster,
a standalone edge host, 5 datastores, a distributed switch, about 25 VMs (plus
VCENTER_MOCK_EXTRA_VMS synthetic ones for scale), tags, licenses, alarms, and
ONE deterministic incident (see SCENARIO below).

SCENARIO (relative to NOW, fixed at start or pinned by VCENTER_MOCK_NOW):
  T-21d/-14d/-7d  svc-backup creates snapshots on backup-proxy-01 and never
                  removes them. The delta chain grows on ds-prod-01.
  T-2d            esx-prod-04 enters maintenance mode (administrator) and stays
                  there, so Prod-Cluster runs on 3 hosts.
  T-50m           RemoveSnapshot_Task (consolidation) on backup-proxy-01 fails:
                  file lock. The VM is flagged consolidationNeeded.
  T-45m..T-10m    INCIDENT: ds-prod-01 is 94% full and write latency explodes.
                  prod-db-01 and app-01/02 (all on ds-prod-01) see
                  disk.maxTotalLatency of 60-95 ms (baseline 2-6 ms). The
                  ESXi `esx.problem.scsi.device.io.latency.high` event fires on
                  esx-prod-03, and DRS moves app-02 off esx-prod-03 (CPU 90%+).
  T-40m           Alarm "Datastore usage on disk" on ds-prod-01 goes yellow to red.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import ipaddress
import json
import math
import os
import re
import ssl
import sys
import tempfile
import threading
import traceback
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from pyVmomi import SoapAdapter, VmomiSupport, vim, vmodl
from pyVmomi.VmomiSupport import (GetPropertyInfo, GetVersionFromVersionUri, GetWsdlMethod,
                                  GetWsdlName, GetWsdlType, ManagedObject, Object)

# ── configuration ─────────────────────────────────────────────────────────────

USER = os.environ.get("VCENTER_MOCK_USER", "bow-ro@vsphere.local")
PASSWORD = os.environ.get("VCENTER_MOCK_PASSWORD", "VMware1!RO")
API_VERSION = os.environ.get("VCENTER_MOCK_API_VERSION", "8.0.3.0")
MODE = os.environ.get("VCENTER_MOCK_MODE", "vcenter").lower()       # vcenter | esxi
EXTRA_VMS = int(os.environ.get("VCENTER_MOCK_EXTRA_VMS", "0"))
MAX_SESSIONS = int(os.environ.get("VCENTER_MOCK_MAX_SESSIONS", "550"))
PAGE_CAP = 500                      # server-side cap on RetrievePropertiesEx pages
MAX_QUERY_METRICS = 256             # vpxd.stats.maxQueryMetrics
COLLECTOR_MAX = 1000                # ReadNextEvents / ReadNextTasks maxCount cap
REALTIME = 20
UTC = dt.timezone.utc

_now_env = os.environ.get("VCENTER_MOCK_NOW")
NOW = (dt.datetime.fromtimestamp(int(_now_env), UTC) if _now_env
       else dt.datetime.now(UTC)).replace(microsecond=0)
NOW = NOW - dt.timedelta(seconds=NOW.second % REALTIME)
INCIDENT_START = NOW - dt.timedelta(minutes=45)
INCIDENT_END = NOW - dt.timedelta(minutes=10)

VERSION_NAME = GetVersionFromVersionUri(f"urn:vim25/{API_VERSION}")
GB = 1024 ** 3
MB = 1024 ** 2

# vimServiceVersions.xml: what a vCenter of API_VERSION advertises.
_PRIOR = ["8.0.2.0", "8.0.1.0", "8.0.0.2", "8.0.0.1", "8.0.0.0", "7.0.3.2", "7.0.3.1",
          "7.0.3.0", "7.0.2.1", "7.0.2.0", "7.0.1.0", "7.0.0.0", "6.7.3", "6.7.2", "6.7.1",
          "6.7", "6.5", "6.0"]


def _service_versions_xml() -> str:
    def key(v):
        return tuple(int(x) for x in v.split("."))
    prior = [v for v in _PRIOR if key(v) < key(API_VERSION)]
    items = "".join(f"<version>{v}</version>" for v in prior)
    return ('<?xml version="1.0" encoding="UTF-8" ?>\n<namespaces version="1.0">\n <namespace>\n'
            f'  <name>urn:vim25</name>\n  <version>{API_VERSION}</version>\n'
            f'  <priorVersions>{items}</priorVersions>\n </namespace>\n</namespaces>\n')


# ── deterministic helpers ─────────────────────────────────────────────────────

def _h(*parts) -> float:
    """Stable pseudo-random number in [0, 1) from the given parts."""
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64


def _uuid(*parts) -> str:
    return str(uuid.UUID(hashlib.md5("|".join(map(str, parts)).encode()).hexdigest()))


# ── inventory model ───────────────────────────────────────────────────────────

class MO:
    """One managed object: its pyVmomi class, moid and top-level properties."""

    def __init__(self, cls, moid: str, **props):
        self.cls = cls
        self.moid = moid
        self.props: Dict[str, Any] = props

    @property
    def ref(self):
        return self.cls(self.moid)


OBJECTS: Dict[str, MO] = {}
# Containment edges used by ContainerView (recursive) traversal.
CHILDREN: Dict[str, List[str]] = {}


def add(cls, moid: str, parent: Optional[str] = None, **props) -> MO:
    mo = MO(cls, moid, **props)
    if parent:
        mo.props.setdefault("parent", OBJECTS[parent].ref)
        CHILDREN.setdefault(parent, []).append(moid)
    OBJECTS[moid] = mo
    return mo


def link(container: str, child: str) -> None:
    CHILDREN.setdefault(container, []).append(child)


def ref(moid: str):
    return OBJECTS[moid].ref


def refs(cls, moids: Iterable[str]):
    return cls.Array([ref(m) for m in moids])


# Service singletons (same moids as a real vCenter).
SI_MOIDS = {
    "propertyCollector": vim.PropertyCollector, "ViewManager": vim.view.ViewManager,
    "SessionManager": vim.SessionManager, "PerfMgr": vim.PerformanceManager,
    "EventManager": vim.event.EventManager, "TaskManager": vim.TaskManager,
    "AlarmManager": vim.alarm.AlarmManager, "LicenseManager": vim.LicenseManager,
    "VpxSettings": vim.option.OptionManager, "UserDirectory": vim.UserDirectory,
    "AuthorizationManager": vim.AuthorizationManager, "SearchIndex": vim.SearchIndex,
}
if MODE == "esxi":
    SI_MOIDS = {k: v for k, v in SI_MOIDS.items()}
    SI_MOIDS["ha-perfmgr"] = SI_MOIDS.pop("PerfMgr")
    SI_MOIDS["ha-eventmgr"] = SI_MOIDS.pop("EventManager")
    SI_MOIDS["ha-taskmgr"] = SI_MOIDS.pop("TaskManager")
    SI_MOIDS["ha-license-manager"] = SI_MOIDS.pop("LicenseManager")
    SI_MOIDS["ha-alarm-manager"] = SI_MOIDS.pop("AlarmManager")
    SI_MOIDS["ha-sessionmgr"] = SI_MOIDS.pop("SessionManager")
    SI_MOIDS["ha-property-collector"] = SI_MOIDS.pop("propertyCollector")
for _moid, _cls in SI_MOIDS.items():
    add(_cls, _moid)

PC_MOID = "ha-property-collector" if MODE == "esxi" else "propertyCollector"
PERF_MOID = "ha-perfmgr" if MODE == "esxi" else "PerfMgr"
EVT_MOID = "ha-eventmgr" if MODE == "esxi" else "EventManager"
TASK_MOID = "ha-taskmgr" if MODE == "esxi" else "TaskManager"
LIC_MOID = "ha-license-manager" if MODE == "esxi" else "LicenseManager"
ALARM_MOID = "ha-alarm-manager" if MODE == "esxi" else "AlarmManager"
SESS_MOID = "ha-sessionmgr" if MODE == "esxi" else "SessionManager"
ROOT = "ha-folder-root" if MODE == "esxi" else "group-d1"

INSTANCE_UUID = _uuid("vcenter", "instance")
if MODE == "esxi":
    ABOUT = vim.AboutInfo(
        name="VMware ESXi", fullName="VMware ESXi 8.0.3 build-24022510", vendor="VMware, Inc.",
        version="8.0.3", build="24022510", localeVersion="INTL", localeBuild="000",
        osType="vmnix-x86", productLineId="embeddedEsx", apiType="HostAgent",
        apiVersion=API_VERSION, licenseProductName="VMware ESX Server", licenseProductVersion="8.0")
else:
    ABOUT = vim.AboutInfo(
        name="VMware vCenter Server", fullName="VMware vCenter Server 8.0.3 build-24022515",
        vendor="VMware, Inc.", version="8.0.3", build="24022515", localeVersion="INTL",
        localeBuild="000", osType="linux-x64", productLineId="vpx", apiType="VirtualCenter",
        apiVersion=API_VERSION, instanceUuid=INSTANCE_UUID,
        licenseProductName="VMware VirtualCenter Server", licenseProductVersion="8.0")

add(vim.Folder, ROOT, name="Datacenters" if MODE != "esxi" else "ha-folder-root",
    childType=["Datacenter", "Folder"], overallStatus="green")


def _service_content():
    def r(moid):
        return OBJECTS[moid].ref if moid in OBJECTS else None
    return vim.ServiceInstanceContent(
        rootFolder=r(ROOT), propertyCollector=r(PC_MOID), viewManager=r("ViewManager"),
        about=ABOUT, setting=r("VpxSettings"), userDirectory=r("UserDirectory"),
        sessionManager=r(SESS_MOID), authorizationManager=r("AuthorizationManager"),
        perfManager=r(PERF_MOID), eventManager=r(EVT_MOID), taskManager=r(TASK_MOID),
        alarmManager=r(ALARM_MOID), licenseManager=r(LIC_MOID), searchIndex=r("SearchIndex"))


# ── seed: datacenters, folders, clusters, hosts ──────────────────────────────

def _datacenter(moid: str, name: str, parent: str, prefix: str) -> Dict[str, str]:
    folders = {k: f"{prefix}-{k}" for k in ("vm", "host", "datastore", "network")}
    if MODE == "esxi":
        folders = {"vm": "ha-folder-vm", "host": "ha-folder-host",
                   "datastore": "ha-folder-datastore", "network": "ha-folder-network"}
    add(vim.Datacenter, moid, parent, name=name, overallStatus="green",
        vmFolder=None, hostFolder=None, datastoreFolder=None, networkFolder=None)
    for kind, fid in folders.items():
        child_types = {"vm": ["Folder", "VirtualMachine", "VirtualApp"],
                       "host": ["Folder", "ComputeResource"],
                       "datastore": ["Folder", "Datastore", "StoragePod"],
                       "network": ["Folder", "Network", "DistributedVirtualSwitch"]}[kind]
        add(vim.Folder, fid, moid, name=kind, childType=child_types, overallStatus="green")
        OBJECTS[moid].props[f"{kind}Folder"] = ref(fid)
    return folders


HOST_SPECS = []        # (moid, name, cluster_moid, maintenance, cpu_pct_baseline)
VM_SPECS = []          # dicts, see _vm()
DATASTORES: Dict[str, dict] = {}
NETWORKS: Dict[str, str] = {}


def _host(moid, name, parent_cr, dc_folder_net, maintenance=False, cpu_base=0.35,
          model="PowerEdge R760", vendor="Dell Inc.", cores=32, mem_gb=768, standalone=False):
    sockets = 2
    mhz = 2600
    hw = vim.host.Summary.HardwareSummary(
        vendor=vendor, model=model, uuid=_uuid("host", moid),
        cpuModel="Intel(R) Xeon(R) Gold 6430", cpuMhz=mhz, numCpuPkgs=sockets,
        numCpuCores=cores, numCpuThreads=cores * 2, memorySize=mem_gb * GB,
        numNics=4, numHBAs=2,
        otherIdentifyingInfo=[vim.host.SystemIdentificationInfo(
            identifierValue=f"SN{int(_h(moid) * 1e7):07d}",
            identifierType=vim.ElementDescription(key="ServiceTag", label="Service tag", summary="Service tag"))])
    product = vim.AboutInfo(name="VMware ESXi", fullName="VMware ESXi 8.0.3 build-24022510",
                            vendor="VMware, Inc.", version="8.0.3", build="24022510",
                            osType="vmnix-x86", productLineId="embeddedEsx",
                            apiType="HostAgent", apiVersion=API_VERSION)
    boot = NOW - dt.timedelta(days=40 + int(_h(moid, "boot") * 60))
    cpu_pct = 0.0 if maintenance else cpu_base
    mem_pct = 0.0 if maintenance else 0.55 + _h(moid, "mem") * 0.2
    runtime = vim.host.RuntimeInfo(
        connectionState="connected", powerState="poweredOn", inMaintenanceMode=maintenance,
        bootTime=boot, standbyMode="none", inQuarantineMode=False)
    quick = vim.host.Summary.QuickStats(
        overallCpuUsage=int(cpu_pct * mhz * cores), overallMemoryUsage=int(mem_pct * mem_gb * 1024),
        uptime=int((NOW - boot).total_seconds()), distributedCpuFairness=0, distributedMemoryFairness=0)
    status = "yellow" if moid == "host-12" else "green"
    summary = vim.host.Summary(
        host=None, hardware=hw, runtime=runtime, quickStats=quick, overallStatus=status,
        rebootRequired=False, managementServerIp="10.10.0.5" if MODE != "esxi" else None,
        config=vim.host.Summary.ConfigSummary(
            name=name, port=443, product=product, vmotionEnabled=True, faultToleranceEnabled=False))
    add(vim.HostSystem, moid, parent_cr, name=name, summary=summary, runtime=runtime,
        overallStatus=status, vm=[], datastore=[], network=[])
    OBJECTS[moid].props["summary"].host = ref(moid)
    HOST_SPECS.append((moid, name, parent_cr, maintenance, cpu_base))
    return moid


def _cluster(moid, name, host_folder, hosts: List[Tuple[str, str, bool, float]], rp_moid):
    add(vim.ClusterComputeResource, moid, host_folder, name=name, host=[], datastore=[], network=[],
        overallStatus="yellow" if name == "Prod-Cluster" else "green")
    for hmoid, hname, maint, cpu in hosts:
        _host(hmoid, hname, moid, None, maintenance=maint, cpu_base=cpu)
    add(vim.ResourcePool, rp_moid, moid, name="Resources", owner=ref(moid), vm=[], resourcePool=[],
        overallStatus="green", config=_rp_config(rp_moid, -1, -1, "normal"),
        summary=vim.ResourcePool.Summary(name="Resources", config=_rp_config(rp_moid, -1, -1, "normal"),
                                         quickStats=vim.ResourcePool.Summary.QuickStats()))
    OBJECTS[moid].props["resourcePool"] = ref(rp_moid)
    OBJECTS[moid].props["host"] = refs(vim.HostSystem, [h[0] for h in hosts])
    return moid


def _rp_config(moid, cpu_res, mem_res, level, cpu_limit=-1, mem_limit=-1):
    def alloc(res, limit):
        return vim.ResourceAllocationInfo(
            reservation=max(res, 0), expandableReservation=True, limit=limit,
            shares=vim.SharesInfo(level=level, shares={"low": 2000, "normal": 4000, "high": 8000}.get(level, 4000)))
    return vim.ResourceConfigSpec(entity=ref(moid) if moid in OBJECTS else None,
                                  cpuAllocation=alloc(cpu_res, cpu_limit),
                                  memoryAllocation=alloc(mem_res, mem_limit))


def _child_rp(moid, name, parent_rp, cluster, cpu_res, mem_res, level, cpu_limit=-1, mem_limit=-1):
    add(vim.ResourcePool, moid, parent_rp, name=name, owner=ref(cluster), vm=[], resourcePool=[],
        overallStatus="green", config=None, summary=None)
    cfg = _rp_config(moid, cpu_res, mem_res, level, cpu_limit, mem_limit)
    OBJECTS[moid].props["config"] = cfg
    OBJECTS[moid].props["summary"] = vim.ResourcePool.Summary(
        name=name, config=cfg, quickStats=vim.ResourcePool.Summary.QuickStats())
    OBJECTS[parent_rp].props["resourcePool"] = list(OBJECTS[parent_rp].props["resourcePool"]) + [ref(moid)]
    return moid


def _datastore(moid, name, folder, kind, capacity_tb, free_frac, hosts, uncommitted_tb=0.0, url=None):
    capacity = int(capacity_tb * 1024) * GB
    free = int(capacity * free_frac)
    url = url or (f"ds:///vmfs/volumes/{_uuid('ds', moid)[:8]}-{_uuid('ds', moid)[9:13]}/"
                  if kind == "VMFS" else f"ds:///vmfs/volumes/{_uuid('nfs', moid)[:8]}/")
    summary = vim.Datastore.Summary(
        datastore=None, name=name, url=url, capacity=capacity, freeSpace=free,
        uncommitted=int(uncommitted_tb * 1024) * GB, accessible=True,
        multipleHostAccess=len(hosts) > 1, type=kind, maintenanceMode="normal")
    status = "red" if free_frac < 0.08 else ("yellow" if free_frac < 0.2 else "green")
    add(vim.Datastore, moid, folder, name=name, summary=summary, overallStatus=status, vm=[],
        host=[vim.Datastore.HostMount(key=ref(h), mountInfo=vim.host.MountInfo(
            path=url.replace("ds://", ""), accessMode="readWrite", mounted=True, accessible=True))
              for h in hosts],
        info=None)
    OBJECTS[moid].props["summary"].datastore = ref(moid)
    if kind == "VMFS":
        OBJECTS[moid].props["info"] = vim.host.VmfsDatastoreInfo(
            name=name, url=url, freeSpace=free, maxFileSize=62 * 1024 * GB, timestamp=NOW,
            vmfs=vim.host.VmfsVolume(
                type="VMFS", name=name, capacity=capacity, blockSizeMb=1, maxBlocks=0,
                majorVersion=6, version="6.82", uuid=_uuid("vmfs", moid), ssd=True, local=len(hosts) == 1,
                extent=[vim.host.ScsiDisk.Partition(diskName=f"naa.6000c29{int(_h(moid) * 1e9):09d}", partition=1)]))
    else:
        OBJECTS[moid].props["info"] = vim.host.NasDatastoreInfo(
            name=name, url=url, freeSpace=free, maxFileSize=62 * 1024 * GB, timestamp=NOW,
            nas=vim.host.NasVolume(type="NFS", name=name, capacity=capacity,
                                   remoteHost="nas01.acme.local", remotePath=f"/exports/{name}"))
    for h in hosts:
        OBJECTS[h].props["datastore"] = list(OBJECTS[h].props["datastore"]) + [ref(moid)]
    DATASTORES[moid] = {"name": name, "capacity": capacity, "free": free}
    return moid


def _network(moid, name, folder, hosts, cls=vim.Network, vlan=None, dvs=None):
    props = dict(name=name, summary=vim.Network.Summary(network=None, name=name, accessible=True),
                 host=[], vm=[], overallStatus="green")
    add(cls, moid, folder, **props)
    OBJECTS[moid].props["summary"].network = ref(moid)
    OBJECTS[moid].props["host"] = refs(vim.HostSystem, hosts)
    if cls is vim.dvs.DistributedVirtualPortgroup:
        OBJECTS[moid].props["key"] = moid
        OBJECTS[moid].props["config"] = vim.dvs.DistributedVirtualPortgroup.ConfigInfo(
            key=moid, name=name, numPorts=128, type="earlyBinding",
            policy=vim.dvs.DistributedVirtualPortgroup.PortgroupPolicy(
                blockOverrideAllowed=True, shapingOverrideAllowed=False, vendorConfigOverrideAllowed=False,
                livePortMovingAllowed=False, portConfigResetAtDisconnect=True),
            distributedVirtualSwitch=ref(dvs),
            defaultPortConfig=vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
                vlan=vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(inherited=False, vlanId=vlan or 0)))
        OBJECTS[dvs].props["portgroup"] = list(OBJECTS[dvs].props["portgroup"]) + [ref(moid)]
    elif vlan is not None:
        OBJECTS[moid].props["_vlan"] = vlan
    for h in hosts:
        OBJECTS[h].props["network"] = list(OBJECTS[h].props["network"]) + [ref(moid)]
    NETWORKS[moid] = name
    return moid


# ── seed: VMs ─────────────────────────────────────────────────────────────────

def _vm(moid, name, folder, host, rp, disks: List[Tuple[str, int]], nets: List[str], *,
        cpu=4, mem_gb=16, guest="rhel9_64Guest", guest_full="Red Hat Enterprise Linux 9 (64-bit)",
        power="poweredOn", template=False, snapshots: List[Tuple[str, int]] = (),
        consolidation=False, ip=None, tools="guestToolsRunning", annotation="",
        cpu_load=0.25, created_days=400, hw="vmx-21"):
    devices = [
        vim.vm.device.VirtualLsiLogicSASController(
            key=1000, busNumber=0, sharedBus="noSharing",
            deviceInfo=vim.Description(label="SCSI controller 0", summary="LSI Logic SAS"), device=[]),
    ]
    files = [vim.vm.FileLayoutEx.FileInfo(key=0, name=f"[{DATASTORES[disks[0][0]]['name']}] {name}/{name}.vmx",
                                          type="config", size=3 * 1024, uniqueSize=3 * 1024, accessible=True)]
    disk_layout = []
    file_key = 1
    committed = 0
    snap_extra = 0
    snap_depth = len(snapshots)
    for i, (ds, size_gb) in enumerate(disks):
        dsname = DATASTORES[ds]["name"]
        base = f"[{dsname}] {name}/{name}{'' if i == 0 else '_' + str(i)}.vmdk"
        current = base if snap_depth == 0 else base.replace(".vmdk", f"-{snap_depth:06d}.vmdk")
        dev_key = 2000 + i
        devices.append(vim.vm.device.VirtualDisk(
            key=dev_key, controllerKey=1000, unitNumber=i, capacityInKB=size_gb * 1024 * 1024,
            capacityInBytes=size_gb * GB,
            deviceInfo=vim.Description(label=f"Hard disk {i + 1}", summary=f"{size_gb * 1024 * 1024:,} KB"),
            backing=vim.vm.device.VirtualDisk.FlatVer2BackingInfo(
                fileName=current, datastore=ref(ds), diskMode="persistent",
                thinProvisioned=(i > 0 or "dev" in name), uuid=_uuid("disk", moid, i))))
        used = int(size_gb * GB * (0.55 + _h(moid, i) * 0.3))
        committed += used
        chain = [vim.vm.FileLayoutEx.DiskUnit(fileKey=[file_key, file_key + 1])]
        files.append(vim.vm.FileLayoutEx.FileInfo(key=file_key, name=base, type="diskDescriptor",
                                                  size=600, uniqueSize=600, accessible=True))
        files.append(vim.vm.FileLayoutEx.FileInfo(key=file_key + 1, name=base.replace(".vmdk", "-flat.vmdk"),
                                                  type="diskExtent", size=used, uniqueSize=used, accessible=True))
        file_key += 2
        for d in range(1, snap_depth + 1):
            delta = f"[{dsname}] {name}/{name}{'' if i == 0 else '_' + str(i)}-{d:06d}.vmdk"
            delta_size = int(size_gb * GB * (0.08 + 0.05 * d)) if i == 0 else int(size_gb * GB * 0.02)
            snap_extra += delta_size
            files.append(vim.vm.FileLayoutEx.FileInfo(key=file_key, name=delta, type="diskDescriptor",
                                                      size=500, uniqueSize=500, accessible=True))
            files.append(vim.vm.FileLayoutEx.FileInfo(key=file_key + 1, name=delta.replace(".vmdk", "-delta.vmdk"),
                                                      type="diskExtent", size=delta_size, uniqueSize=delta_size,
                                                      accessible=True))
            chain.append(vim.vm.FileLayoutEx.DiskUnit(fileKey=[file_key, file_key + 1]))
            file_key += 2
        disk_layout.append(vim.vm.FileLayoutEx.DiskLayout(key=dev_key, chain=chain))
    for j, net in enumerate(nets):
        mac = "00:50:56:%02x:%02x:%02x" % (int(_h(moid, j, 1) * 255), int(_h(moid, j, 2) * 255), int(_h(moid, j, 3) * 255))
        net_mo = OBJECTS[net]
        if net_mo.cls is vim.dvs.DistributedVirtualPortgroup:
            backing = vim.vm.device.VirtualEthernetCard.DistributedVirtualPortBackingInfo(
                port=vim.dvs.PortConnection(portgroupKey=net, switchUuid=_uuid("dvs", "dvs-60")))
        else:
            backing = vim.vm.device.VirtualEthernetCard.NetworkBackingInfo(
                deviceName=net_mo.props["name"], network=ref(net))
        devices.append(vim.vm.device.VirtualVmxnet3(
            key=4000 + j, controllerKey=100, unitNumber=7 + j, macAddress=mac, addressType="assigned",
            deviceInfo=vim.Description(label=f"Network adapter {j + 1}", summary=net_mo.props["name"]),
            backing=backing,
            connectable=vim.vm.device.VirtualDevice.ConnectInfo(
                connected=power == "poweredOn", startConnected=True, allowGuestControl=True, status="ok")))
    if snap_depth:
        files.append(vim.vm.FileLayoutEx.FileInfo(key=file_key, name=f"[{DATASTORES[disks[0][0]]['name']}] {name}/{name}.vmsd",
                                                  type="snapshotList", size=1024, uniqueSize=1024, accessible=True))
        file_key += 1
        for d in range(1, snap_depth + 1):
            files.append(vim.vm.FileLayoutEx.FileInfo(
                key=file_key, name=f"[{DATASTORES[disks[0][0]]['name']}] {name}/{name}-Snapshot{d}.vmsn",
                type="snapshotData", size=40 * 1024, uniqueSize=40 * 1024, accessible=True))
            file_key += 1
    boot = NOW - dt.timedelta(days=3 + int(_h(moid, "boot") * 30)) if power == "poweredOn" else None
    provisioned = sum(s for _, s in disks) * GB
    config = vim.vm.ConfigInfo(
        changeVersion=NOW.isoformat(), modified=NOW, name=name, guestFullName=guest_full,
        version=hw, uuid=_uuid("bios", moid), instanceUuid=_uuid("inst", moid),
        npivTemporaryDisabled=True, locationId=_uuid("loc", moid), template=template, guestId=guest,
        alternateGuestName="", annotation=annotation, files=vim.vm.FileInfo(
            vmPathName=f"[{DATASTORES[disks[0][0]]['name']}] {name}/{name}.vmx"),
        flags=vim.vm.FlagInfo(), createDate=NOW - dt.timedelta(days=created_days),
        hardware=vim.vm.VirtualHardware(numCPU=cpu, numCoresPerSocket=min(cpu, 2), memoryMB=mem_gb * 1024,
                                        device=devices),
        cpuAllocation=vim.ResourceAllocationInfo(reservation=0, limit=-1, expandableReservation=False,
                                                 shares=vim.SharesInfo(level="normal", shares=cpu * 1000)),
        memoryAllocation=vim.ResourceAllocationInfo(reservation=0, limit=-1, expandableReservation=False,
                                                    shares=vim.SharesInfo(level="normal", shares=mem_gb * 10240)))
    on = power == "poweredOn"
    runtime = vim.vm.RuntimeInfo(
        host=ref(host), connectionState="connected", powerState=power, bootTime=boot,
        consolidationNeeded=consolidation, maxCpuUsage=cpu * 2600, maxMemoryUsage=mem_gb * 1024,
        faultToleranceState="notConfigured", toolsInstallerMounted=False, onlineStandby=False,
        numMksConnections=0, suspendInterval=0)
    gdisks = []
    if on:
        for i, (_, size_gb) in enumerate(disks):
            cap = int(size_gb * GB * 0.97)
            # the database data volume is genuinely running out of space in the guest
            frac = 0.06 if (name.startswith("prod-db") and i == 1) else 0.25 + _h(moid, "fs", i) * 0.5
            gdisks.append(vim.vm.GuestInfo.DiskInfo(diskPath="/" if i == 0 else f"/data{i}",
                                                    capacity=cap, freeSpace=int(cap * frac), filesystemType="xfs"))
    gnet = []
    if on and ip:
        for j, net in enumerate(nets):
            mac = devices[-len(nets) + j].macAddress
            gnet.append(vim.vm.GuestInfo.NicInfo(
                network=OBJECTS[net].props["name"], macAddress=mac, connected=True, deviceConfigId=4000 + j,
                ipAddress=[ip if j == 0 else ip.rsplit(".", 1)[0] + f".{200 + j}"]))
    guest_info = vim.vm.GuestInfo(
        toolsStatus="toolsOk" if on else "toolsNotRunning",
        toolsVersionStatus2="guestToolsCurrent" if tools == "guestToolsRunning" else "guestToolsNeedUpgrade",
        toolsRunningStatus=tools if on else "guestToolsNotRunning", toolsVersion="12352",
        guestId=guest if on else None, guestFullName=guest_full if on else None,
        hostName=name if on else None, ipAddress=ip if on else None, net=gnet, disk=gdisks,
        guestState="running" if on else "notRunning", guestOperationsReady=on,
        interactiveGuestOperationsReady=False, guestStateChangeSupported=on)
    used_cpu = int(cpu * 2600 * cpu_load) if on else 0
    quick = vim.vm.Summary.QuickStats(
        overallCpuUsage=used_cpu, overallCpuDemand=used_cpu, guestMemoryUsage=int(mem_gb * 1024 * 0.4) if on else 0,
        hostMemoryUsage=int(mem_gb * 1024 * 0.9) if on else 0, uptimeSeconds=int((NOW - boot).total_seconds()) if boot else 0,
        balloonedMemory=0, swappedMemory=0, compressedMemory=0, consumedOverheadMemory=60 if on else 0,
        grantedMemory=mem_gb * 1024 if on else 0, privateMemory=int(mem_gb * 1024 * 0.88) if on else 0,
        sharedMemory=0, staticCpuEntitlement=0, staticMemoryEntitlement=0, distributedCpuEntitlement=0,
        distributedMemoryEntitlement=0, ftLatencyStatus="gray", ftLogBandwidth=-1, ftSecondaryLatency=-1,
        guestHeartbeatStatus="green" if on else "gray")
    status = "yellow" if consolidation else "green"
    summary = vim.vm.Summary(
        vm=None, runtime=runtime, guest=vim.vm.Summary.GuestSummary(
            guestId=guest_info.guestId, guestFullName=guest_info.guestFullName, toolsStatus=guest_info.toolsStatus,
            hostName=guest_info.hostName, ipAddress=guest_info.ipAddress),
        config=vim.vm.Summary.ConfigSummary(
            name=name, template=template, vmPathName=config.files.vmPathName, memorySizeMB=mem_gb * 1024,
            numCpu=cpu, numEthernetCards=len(nets), numVirtualDisks=len(disks), uuid=config.uuid,
            instanceUuid=config.instanceUuid, guestId=guest, guestFullName=guest_full, annotation=annotation),
        storage=vim.vm.Summary.StorageSummary(committed=committed + snap_extra,
                                              uncommitted=max(provisioned - committed, 0),
                                              unshared=committed + snap_extra, timestamp=NOW),
        quickStats=quick, overallStatus=status)
    snap_info = None
    if snapshots:
        nodes = []
        for k, (sname, age_days) in enumerate(snapshots):
            smoid = f"snapshot-{int(moid.split('-')[1]) * 10 + k + 1}"
            add(vim.vm.Snapshot, smoid, None, config=None, vm=vim.VirtualMachine(moid), childSnapshot=[])
            nodes.append(vim.vm.SnapshotTree(
                snapshot=ref(smoid), vm=vim.VirtualMachine(moid), name=sname,
                description=f"Created by svc-backup job BKP-{1000 + k}", id=k + 1,
                createTime=NOW - dt.timedelta(days=age_days), state="poweredOn", quiesced=True,
                backupManifest="", childSnapshotList=[], replaySupported=False))
        for parent_node, child_node in zip(nodes, nodes[1:]):
            parent_node.childSnapshotList = [child_node]
        snap_info = vim.vm.SnapshotInfo(currentSnapshot=nodes[-1].snapshot, rootSnapshotList=[nodes[0]])
    vm_datastores = sorted({ds for ds, _ in disks})
    add(vim.VirtualMachine, moid, folder, name=name, config=config, runtime=runtime, guest=guest_info,
        summary=summary, resourcePool=ref(rp) if rp else None, datastore=refs(vim.Datastore, vm_datastores),
        network=refs(vim.Network, nets), snapshot=snap_info, overallStatus=status,
        layoutEx=vim.vm.FileLayoutEx(file=files, disk=disk_layout, snapshot=[], timestamp=NOW),
        storage=None, triggeredAlarmState=[])
    OBJECTS[moid].props["summary"].vm = ref(moid)
    # Containment: resource pool + host give VMs a second path; views de-duplicate.
    if rp:
        link(rp, moid)
        OBJECTS[rp].props["vm"] = list(OBJECTS[rp].props["vm"]) + [ref(moid)]
    OBJECTS[host].props["vm"] = list(OBJECTS[host].props["vm"]) + [ref(moid)]
    for ds in vm_datastores:
        OBJECTS[ds].props["vm"] = list(OBJECTS[ds].props["vm"]) + [ref(moid)]
    for net in nets:
        OBJECTS[net].props["vm"] = list(OBJECTS[net].props["vm"]) + [ref(moid)]
    VM_SPECS.append({"moid": moid, "name": name, "host": host, "ds": vm_datastores, "power": power,
                     "cpu_load": cpu_load, "cpu": cpu, "mem_gb": mem_gb, "nets": nets})
    return moid


def _seed_vcenter():
    east = _datacenter("datacenter-3", "DC-East", ROOT, "group-e")
    _cluster("domain-c8", "Prod-Cluster", east["host"], [
        ("host-10", "esx-prod-01.acme.local", False, 0.42),
        ("host-11", "esx-prod-02.acme.local", False, 0.48),
        ("host-12", "esx-prod-03.acme.local", False, 0.91),
        ("host-13", "esx-prod-04.acme.local", True, 0.0),
    ], "resgroup-9")
    _cluster("domain-c30", "Dev-Cluster", east["host"], [
        ("host-31", "esx-dev-01.acme.local", False, 0.22),
        ("host-32", "esx-dev-02.acme.local", False, 0.18),
    ], "resgroup-33")
    oc = OBJECTS["domain-c8"].props
    oc["summary"] = vim.ClusterComputeResource.Summary(
        totalCpu=4 * 32 * 2600, totalMemory=4 * 768 * GB, numCpuCores=128, numCpuThreads=256,
        effectiveCpu=3 * 32 * 2600, effectiveMemory=3 * 768 * 1024, numHosts=4, numEffectiveHosts=3,
        overallStatus="yellow", currentFailoverLevel=0, numVmotions=412, currentBalance=212, targetBalance=200,
        usageSummary=vim.cluster.UsageSummary(
            totalCpuCapacityMhz=3 * 32 * 2600, totalMemCapacityMB=3 * 768 * 1024,
            cpuReservationMhz=24000, memReservationMB=131072, cpuDemandMhz=int(3 * 32 * 2600 * 0.61),
            memDemandMB=int(3 * 768 * 1024 * 0.67), statsGenNumber=1, cpuEntitledMhz=0, memEntitledMB=0,
            poweredOffCpuReservationMhz=0, poweredOffMemReservationMB=0))
    oc["configurationEx"] = vim.cluster.ConfigInfoEx(
        dasConfig=vim.cluster.DasConfigInfo(enabled=True, admissionControlEnabled=True, hostMonitoring="enabled",
                                            vmMonitoring="vmMonitoringDisabled"),
        drsConfig=vim.cluster.DrsConfigInfo(enabled=True, defaultVmBehavior="fullyAutomated", vmotionRate=3),
        vsanConfigInfo=vim.vsan.cluster.ConfigInfo(enabled=False))
    dc = OBJECTS["domain-c30"].props
    dc["summary"] = vim.ClusterComputeResource.Summary(
        totalCpu=2 * 32 * 2600, totalMemory=2 * 768 * GB, numCpuCores=64, numCpuThreads=128,
        effectiveCpu=2 * 32 * 2600, effectiveMemory=2 * 768 * 1024, numHosts=2, numEffectiveHosts=2,
        overallStatus="green", currentFailoverLevel=1, numVmotions=33,
        usageSummary=vim.cluster.UsageSummary(
            totalCpuCapacityMhz=2 * 32 * 2600, totalMemCapacityMB=2 * 768 * 1024, cpuReservationMhz=0,
            memReservationMB=0, cpuDemandMhz=int(2 * 32 * 2600 * 0.2), memDemandMB=int(2 * 768 * 1024 * 0.31),
            statsGenNumber=1, cpuEntitledMhz=0, memEntitledMB=0, poweredOffCpuReservationMhz=0,
            poweredOffMemReservationMB=0))
    dc["configurationEx"] = vim.cluster.ConfigInfoEx(
        dasConfig=vim.cluster.DasConfigInfo(enabled=False),
        drsConfig=vim.cluster.DrsConfigInfo(enabled=True, defaultVmBehavior="partiallyAutomated"),
        vsanConfigInfo=vim.vsan.cluster.ConfigInfo(enabled=False))
    _child_rp("resgroup-20", "Tier1-DB", "resgroup-9", "domain-c8", 32000, 262144, "high")
    _child_rp("resgroup-21", "Tier2-App", "resgroup-9", "domain-c8", 0, 0, "normal")
    _child_rp("resgroup-22", "Batch", "resgroup-9", "domain-c8", 0, 0, "low", cpu_limit=20000)

    prod_hosts = ["host-10", "host-11", "host-12", "host-13"]
    dev_hosts = ["host-31", "host-32"]
    _datastore("datastore-40", "ds-prod-01", east["datastore"], "VMFS", 4, 0.06, prod_hosts, uncommitted_tb=1.4)
    _datastore("datastore-41", "ds-prod-02", east["datastore"], "VMFS", 4, 0.55, prod_hosts, uncommitted_tb=0.6)
    _datastore("datastore-42", "ds-backup-nfs", east["datastore"], "NFS", 10, 0.38, prod_hosts)
    _datastore("datastore-43", "ds-dev-01", east["datastore"], "VMFS", 2, 0.41, dev_hosts, uncommitted_tb=0.9)
    _datastore("datastore-44", "esx-prod-01-local", east["datastore"], "VMFS", 0.5, 0.92, ["host-10"])

    add(vim.dvs.VmwareDistributedVirtualSwitch, "dvs-60", east["network"], name="dvs-prod", portgroup=[],
        overallStatus="green", uuid=_uuid("dvs", "dvs-60"),
        summary=vim.DistributedVirtualSwitch.Summary(name="dvs-prod", uuid=_uuid("dvs", "dvs-60"),
                                                     numPorts=512, productInfo=None, hostMember=refs(vim.HostSystem, prod_hosts)))
    _network("dvportgroup-61", "dvpg-prod-app", east["network"], prod_hosts, vim.dvs.DistributedVirtualPortgroup, 110, "dvs-60")
    _network("dvportgroup-62", "dvpg-prod-db", east["network"], prod_hosts, vim.dvs.DistributedVirtualPortgroup, 120, "dvs-60")
    _network("dvportgroup-63", "dvpg-mgmt", east["network"], prod_hosts, vim.dvs.DistributedVirtualPortgroup, 10, "dvs-60")
    _network("network-50", "VM Network", east["network"], dev_hosts)

    add(vim.Folder, "group-v70", east["vm"], name="Production", childType=["Folder", "VirtualMachine"], overallStatus="green")
    add(vim.Folder, "group-v71", east["vm"], name="Dev", childType=["Folder", "VirtualMachine"], overallStatus="green")
    add(vim.Folder, "group-v72", east["vm"], name="Templates", childType=["Folder", "VirtualMachine"], overallStatus="green")

    P, D = "group-v70", "group-v71"
    _vm("vm-101", "prod-db-01", P, "host-10", "resgroup-20", [("datastore-40", 100), ("datastore-40", 1500)],
        ["dvportgroup-62"], cpu=16, mem_gb=128, ip="10.20.120.11", cpu_load=0.55,
        annotation="Primary PostgreSQL 15 for orders. Owner: team-db")
    _vm("vm-102", "prod-db-02", P, "host-11", "resgroup-20", [("datastore-41", 100), ("datastore-41", 1500)],
        ["dvportgroup-62"], cpu=16, mem_gb=128, ip="10.20.120.12", cpu_load=0.18,
        annotation="PostgreSQL 15 replica. Owner: team-db")
    for i, (host, ds) in enumerate([("host-12", "datastore-40"), ("host-11", "datastore-40"),
                                    ("host-12", "datastore-41"), ("host-10", "datastore-41")], start=1):
        _vm(f"vm-{110 + i}", f"app-{i:02d}", P, host, "resgroup-21", [(ds, 80)], ["dvportgroup-61"],
            cpu=8, mem_gb=32, ip=f"10.20.110.{20 + i}", cpu_load=0.5 + 0.1 * (host == "host-12"),
            guest="ubuntu64Guest", guest_full="Ubuntu Linux (64-bit)")
    _vm("vm-120", "backup-proxy-01", P, "host-12", "resgroup-22", [("datastore-40", 200), ("datastore-42", 2000)],
        ["dvportgroup-63"], cpu=8, mem_gb=32, ip="10.20.10.40", cpu_load=0.7,
        snapshots=[("BKP-pre-patch", 21), ("BKP-weekly", 14), ("BKP-weekly", 7)], consolidation=True,
        guest="windows2019srvNext_64Guest", guest_full="Microsoft Windows Server 2022 (64-bit)",
        tools="guestToolsRunning", annotation="Veeam proxy. Owner: team-platform")
    for i in range(1, 4):
        _vm(f"vm-{130 + i}", f"web-{i:02d}", P, ["host-10", "host-11", "host-12"][i - 1], "resgroup-21",
            [("datastore-41", 40)], ["dvportgroup-61"], cpu=4, mem_gb=8, ip=f"10.20.110.{50 + i}",
            cpu_load=0.3, guest="ubuntu64Guest", guest_full="Ubuntu Linux (64-bit)")
    _vm("vm-140", "tmpl-rhel9", "group-v72", "host-11", None, [("datastore-41", 40)], ["dvportgroup-61"],
        cpu=2, mem_gb=4, power="poweredOff", template=True, created_days=200)
    _vm("vm-141", "old-jumpbox", P, "host-10", "resgroup-21", [("datastore-41", 60)], ["dvportgroup-63"],
        cpu=2, mem_gb=8, power="poweredOff", tools="guestToolsNotRunning",
        guest="windows9Server64Guest", guest_full="Microsoft Windows Server 2016 (64-bit)", created_days=2100, hw="vmx-13")
    for i, (host, power) in enumerate([("host-31", "poweredOn"), ("host-32", "poweredOn"),
                                       ("host-31", "poweredOff"), ("host-32", "suspended")], start=1):
        _vm(f"vm-{200 + i}", f"dev-test-{i:02d}", D, host, "resgroup-33", [("datastore-43", 60)], ["network-50"],
            cpu=4, mem_gb=16, power=power, ip=f"192.168.50.{10 + i}" if power == "poweredOn" else None,
            cpu_load=0.15, snapshots=[("before-upgrade", 40)] if i == 2 else ())
    _vm("vm-210", "dev-build-01", D, "host-31", "resgroup-33", [("datastore-43", 200)], ["network-50"],
        cpu=16, mem_gb=64, ip="192.168.50.30", cpu_load=0.6)
    for n in range(EXTRA_VMS):
        host = dev_hosts[n % 2]
        _vm(f"vm-{10000 + n}", f"load-vm-{n:05d}", D, host, "resgroup-33", [("datastore-43", 20)], ["network-50"],
            cpu=1, mem_gb=2, ip=f"10.99.{n // 250}.{n % 250 + 1}", cpu_load=0.05 + _h(n) * 0.2)

    west = _datacenter("datacenter-100", "DC-West", ROOT, "group-w")
    add(vim.ComputeResource, "domain-s101", west["host"], name="esx-edge-01.acme.local", host=[], datastore=[],
        network=[], overallStatus="green",
        summary=vim.ComputeResource.Summary(totalCpu=16 * 2400, totalMemory=256 * GB, numCpuCores=16,
                                            numCpuThreads=32, effectiveCpu=16 * 2400, effectiveMemory=256 * 1024,
                                            numHosts=1, numEffectiveHosts=1, overallStatus="green"))
    _host("host-102", "esx-edge-01.acme.local", "domain-s101", None, cpu_base=0.12, model="ThinkSystem SE350",
          vendor="Lenovo", cores=16, mem_gb=256)
    add(vim.ResourcePool, "resgroup-104", "domain-s101", name="Resources", owner=ref("domain-s101"), vm=[],
        resourcePool=[], overallStatus="green", config=_rp_config("resgroup-104", -1, -1, "normal"),
        summary=vim.ResourcePool.Summary(name="Resources", config=_rp_config("resgroup-104", -1, -1, "normal"),
                                         quickStats=vim.ResourcePool.Summary.QuickStats()))
    OBJECTS["domain-s101"].props.update(host=refs(vim.HostSystem, ["host-102"]), resourcePool=ref("resgroup-104"))
    _datastore("datastore-103", "ds-edge-local", west["datastore"], "VMFS", 1, 0.62, ["host-102"])
    _network("network-105", "VM Network", west["network"], ["host-102"])
    _vm("vm-301", "edge-gw-01", west["vm"], "host-102", "resgroup-104", [("datastore-103", 40)], ["network-105"],
        cpu=2, mem_gb=4, ip="172.16.1.10", cpu_load=0.1, guest="other5xLinux64Guest", guest_full="Other 5.x or later Linux (64-bit)")
    _vm("vm-302", "edge-cache-01", west["vm"], "host-102", "resgroup-104", [("datastore-103", 300)], ["network-105"],
        cpu=4, mem_gb=16, ip="172.16.1.11", cpu_load=0.2, tools="guestToolsSupportedOld")


def _seed_esxi():
    folders = _datacenter("ha-datacenter", "ha-datacenter", ROOT, "ha")
    add(vim.ComputeResource, "ha-compute-res", folders["host"], name="esx-edge-01.acme.local", host=[],
        datastore=[], network=[], overallStatus="green")
    _host("ha-host", "esx-edge-01.acme.local", "ha-compute-res", None, cpu_base=0.12, model="ThinkSystem SE350",
          vendor="Lenovo", cores=16, mem_gb=256)
    add(vim.ResourcePool, "ha-root-pool", "ha-compute-res", name="Resources", owner=ref("ha-compute-res"), vm=[],
        resourcePool=[], overallStatus="green", config=_rp_config("ha-root-pool", -1, -1, "normal"),
        summary=None)
    OBJECTS["ha-compute-res"].props.update(host=refs(vim.HostSystem, ["ha-host"]), resourcePool=ref("ha-root-pool"))
    _datastore("datastore1", "datastore1", folders["datastore"], "VMFS", 1, 0.62, ["ha-host"])
    _network("HaNetwork-VM Network", "VM Network", folders["network"], ["ha-host"])
    _vm("vm-1", "edge-gw-01", folders["vm"], "ha-host", "ha-root-pool", [("datastore1", 40)],
        ["HaNetwork-VM Network"], cpu=2, mem_gb=4, ip="172.16.1.10", cpu_load=0.1)
    _vm("vm-2", "edge-cache-01", folders["vm"], "ha-host", "ha-root-pool", [("datastore1", 300)],
        ["HaNetwork-VM Network"], cpu=4, mem_gb=16, ip="172.16.1.11", cpu_load=0.2)


if MODE == "esxi":
    _seed_esxi()
else:
    _seed_vcenter()

# ── alarms ────────────────────────────────────────────────────────────────────

ALARM_DEFS = [
    ("alarm-1", "Datastore usage on disk", "Default alarm to monitor datastore disk usage", "Datastore"),
    ("alarm-2", "Host CPU usage", "Default alarm to monitor host CPU usage", "HostSystem"),
    ("alarm-3", "Virtual machine consolidation needed status",
     "Default alarm that is triggered when a VM's disks need consolidation", "VirtualMachine"),
    ("alarm-4", "Host connection and power state", "Default alarm to monitor host connection and power state", "HostSystem"),
    ("alarm-5", "Virtual machine CPU usage", "Default alarm to monitor virtual machine CPU usage", "VirtualMachine"),
    ("alarm-6", "Datastore cluster is out of space", "Default alarm to monitor datastore cluster space", "StoragePod"),
]
TRIGGERED: List[vim.alarm.AlarmState] = []
if MODE != "esxi":
    for amoid, name, desc, kind in ALARM_DEFS:
        add(vim.alarm.Alarm, amoid, None, info=vim.alarm.AlarmInfo(
            name=name, systemName=f"alarm.{name.title().replace(' ', '')}Alarm", description=desc, enabled=True,
            key=amoid, alarm=vim.alarm.Alarm(amoid), entity=ref(ROOT), lastModifiedTime=NOW - dt.timedelta(days=300),
            lastModifiedUser="VSPHERE.LOCAL\\Administrator", creationEventId=0,
            expression=vim.alarm.OrAlarmExpression(expression=[vim.alarm.MetricAlarmExpression(
                operator="isAbove", type=GetWsdlType("urn:vim25", kind),
                metric=vim.PerformanceManager.MetricId(counterId=0, instance=""),
                yellow=7500, yellowInterval=300, red=9000, redInterval=300)])))

    def _trigger(key, alarm, entity, status, minutes_ago, acked=False):
        TRIGGERED.append(vim.alarm.AlarmState(
            key=key, entity=ref(entity), alarm=ref(alarm), overallStatus=status,
            time=NOW - dt.timedelta(minutes=minutes_ago), acknowledged=acked,
            acknowledgedByUser="VSPHERE.LOCAL\\oncall" if acked else None,
            acknowledgedTime=NOW - dt.timedelta(minutes=minutes_ago - 5) if acked else None))

    _trigger("alarm-1.datastore-40", "alarm-1", "datastore-40", "red", 40)
    _trigger("alarm-2.host-12", "alarm-2", "host-12", "yellow", 38)
    _trigger("alarm-3.vm-120", "alarm-3", "vm-120", "yellow", 50)
    _trigger("alarm-5.vm-210", "alarm-5", "vm-210", "yellow", 300, acked=True)


def _ancestors(moid: str) -> List[str]:
    out = []
    cur = OBJECTS.get(moid)
    while cur is not None:
        p = cur.props.get("parent")
        if p is None:
            # VMs hang off folders; snapshots/alarms have no parent
            break
        out.append(p._moId)
        cur = OBJECTS.get(p._moId)
    return out


def _triggered_for(moid: str) -> List[vim.alarm.AlarmState]:
    hits = []
    for st in TRIGGERED:
        e = st.entity._moId
        if e == moid or moid in _ancestors(e):
            hits.append(st)
    return hits


# ── licenses ──────────────────────────────────────────────────────────────────

def _key(seed):
    raw = hashlib.sha1(seed.encode()).hexdigest().upper()
    return "-".join(raw[i:i + 5] for i in range(0, 25, 5))


LICENSES = [
    vim.LicenseManager.LicenseInfo(
        licenseKey=_key("vcf"), editionKey="esx.vcf.cpuCoreBased", name="VMware vSphere Foundation",
        total=256, used=192, costUnit="cpuCore",
        properties=[vmodl.KeyAnyValue(key="expirationDate", value=NOW + dt.timedelta(days=210)),
                    vmodl.KeyAnyValue(key="ProductName", value="VMware vSphere Foundation"),
                    vmodl.KeyAnyValue(key="ProductVersion", value="8.0")]),
    vim.LicenseManager.LicenseInfo(
        licenseKey=_key("vc"), editionKey="vc.standard.instance", name="VMware vCenter Server 8 Standard",
        total=1, used=1, costUnit="server",
        properties=[vmodl.KeyAnyValue(key="ProductName", value="VMware VirtualCenter Server"),
                    vmodl.KeyAnyValue(key="ProductVersion", value="8.0")]),
    vim.LicenseManager.LicenseInfo(
        licenseKey="00000-00000-00000-00000-00000", editionKey="eval", name="Evaluation Mode",
        total=0, used=0, costUnit="",
        properties=[vmodl.KeyAnyValue(key="expirationHours", value=0)]),
]
if MODE == "esxi":
    LICENSES = LICENSES[:1]
OBJECTS[LIC_MOID].props["licenses"] = LICENSES

# ── performance manager ───────────────────────────────────────────────────────

VM, HOST, CLU, DS, RP = "VirtualMachine", "HostSystem", "ClusterComputeResource", "Datastore", "ResourcePool"
# (key, unit, statsType, level, perDeviceLevel, entity kinds, instance kind, label)
COUNTER_DEFS = [
    ("cpu.usage.average", "percent", "rate", 1, 3, {VM, HOST, CLU}, "cpu", "CPU usage as a percentage during the interval"),
    ("cpu.usagemhz.average", "megaHertz", "rate", 1, 3, {VM, HOST, CLU, RP}, "cpu", "CPU usage in megahertz during the interval"),
    ("cpu.ready.summation", "millisecond", "delta", 1, 3, {VM, HOST}, "cpu", "Time that the virtual machine was ready, but could not get scheduled to run on the physical CPU"),
    ("cpu.costop.summation", "millisecond", "delta", 2, 3, {VM}, "cpu", "Time the virtual machine is ready to run, but is unable to due to co-scheduling constraints"),
    ("mem.usage.average", "percent", "absolute", 1, 3, {VM, HOST, CLU}, "", "Memory usage as percent of total configured or available memory"),
    ("mem.consumed.average", "kiloBytes", "absolute", 1, 3, {VM, HOST, CLU, RP}, "", "Amount of host physical memory consumed"),
    ("mem.active.average", "kiloBytes", "absolute", 2, 3, {VM, HOST}, "", "Amount of guest physical memory that is being actively read or written"),
    ("mem.vmmemctl.average", "kiloBytes", "absolute", 1, 3, {VM, HOST}, "", "Amount of guest physical memory reclaimed by the balloon driver"),
    ("mem.swapinRate.average", "kiloBytesPerSecond", "rate", 1, 3, {VM, HOST}, "", "Rate at which guest physical memory is swapped in from the swap space"),
    ("disk.usage.average", "kiloBytesPerSecond", "rate", 1, 3, {VM, HOST}, "disk", "Aggregated disk I/O rate"),
    ("disk.maxTotalLatency.latest", "millisecond", "absolute", 1, 3, {VM, HOST}, "", "Highest latency value across all disks used by the host"),
    ("disk.read.average", "kiloBytesPerSecond", "rate", 2, 3, {VM, HOST}, "disk", "Average number of kilobytes read from the disk each second"),
    ("disk.write.average", "kiloBytesPerSecond", "rate", 2, 3, {VM, HOST}, "disk", "Average number of kilobytes written to disk each second"),
    ("virtualDisk.totalWriteLatency.average", "millisecond", "absolute", 2, 2, {VM}, "vdisk", "Average amount of time taken during the collection interval to process a write command"),
    ("virtualDisk.totalReadLatency.average", "millisecond", "absolute", 2, 2, {VM}, "vdisk", "Average amount of time taken during the collection interval to process a read command"),
    ("datastore.totalWriteLatency.average", "millisecond", "absolute", 1, 3, {VM, HOST}, "ds", "Average amount of time for a write operation to the datastore"),
    ("datastore.totalReadLatency.average", "millisecond", "absolute", 1, 3, {VM, HOST}, "ds", "Average amount of time for a read operation from the datastore"),
    ("datastore.numberWriteAveraged.average", "number", "rate", 1, 3, {VM, HOST}, "ds", "Average number of write commands issued per second to the datastore"),
    ("disk.used.latest", "kiloBytes", "absolute", 1, 3, {DS}, "", "Amount of space actually used by the virtual machine or the datastore"),
    ("disk.provisioned.latest", "kiloBytes", "absolute", 1, 3, {DS}, "", "Amount of storage set aside for use by a datastore or a virtual machine"),
    ("disk.capacity.latest", "kiloBytes", "absolute", 1, 3, {DS}, "", "Configured size of the datastore"),
    ("net.usage.average", "kiloBytesPerSecond", "rate", 1, 3, {VM, HOST}, "nic", "Network utilization (combined transmit-rates and receive-rates) during the interval"),
    ("net.droppedRx.summation", "number", "delta", 2, 3, {VM, HOST}, "nic", "Number of receives dropped"),
    ("sys.uptime.latest", "second", "absolute", 1, 3, {VM, HOST}, "", "Total time elapsed, in seconds, since last system startup"),
    ("power.power.average", "watt", "rate", 2, 3, {HOST}, "", "Current power usage"),
    ("clusterServices.effectivecpu.average", "megaHertz", "rate", 1, 3, {CLU}, "", "Total available CPU resources of all hosts within a cluster"),
]
UNIT_LABEL = {"percent": "%", "megaHertz": "MHz", "millisecond": "ms", "kiloBytes": "KB",
              "kiloBytesPerSecond": "KBps", "number": "num", "second": "s", "watt": "W"}
COUNTERS: Dict[int, dict] = {}
_ROLLUP = {"average": "average", "latest": "latest", "summation": "summation", "maximum": "maximum"}
for i, (key, unit, stype, level, pdl, kinds, inst, label) in enumerate(COUNTER_DEFS):
    group, name, rollup = key.split(".")
    cid = 2 + i * 4 + int(_h(key) * 3)          # stable, non-contiguous ids like a real vCenter
    COUNTERS[cid] = {"key": key, "unit": unit, "stats": stype, "level": level, "pdl": pdl,
                     "kinds": kinds, "inst": inst, "info": vim.PerformanceManager.CounterInfo(
                         key=cid, level=level, perDeviceLevel=pdl, rollupType=_ROLLUP[rollup], statsType=stype,
                         nameInfo=vim.ElementDescription(key=name, label=name.capitalize(), summary=label),
                         groupInfo=vim.ElementDescription(key=group, label=group.capitalize(), summary=group),
                         unitInfo=vim.ElementDescription(key=unit, label=UNIT_LABEL[unit], summary=unit))}
BY_KEY = {c["key"]: cid for cid, c in COUNTERS.items()}

if MODE == "esxi":
    HIST = []   # ESXi standalone: realtime only
else:
    HIST = [vim.HistoricalInterval(key=1, samplingPeriod=300, name="Past day", length=86400, level=1, enabled=True),
            vim.HistoricalInterval(key=2, samplingPeriod=1800, name="Past week", length=604800, level=1, enabled=True),
            vim.HistoricalInterval(key=3, samplingPeriod=7200, name="Past month", length=2592000, level=1, enabled=True),
            vim.HistoricalInterval(key=4, samplingPeriod=86400, name="Past year", length=31536000, level=1, enabled=True)]
OBJECTS[PERF_MOID].props.update(
    perfCounter=[c["info"] for c in COUNTERS.values()], historicalInterval=HIST,
    description=vim.PerformanceDescription(
        counterType=[vim.ElementDescription(key=t, label=t, summary=t) for t in ("absolute", "delta", "rate")],
        statsType=[vim.ElementDescription(key=t, label=t, summary=t) for t in ("absolute", "delta", "rate")]))


def _kind(moid: str) -> str:
    return GetWsdlName(OBJECTS[moid].cls)


def _instances(moid: str, inst_kind: str) -> List[str]:
    mo = OBJECTS[moid]
    if inst_kind == "cpu":
        n = mo.props["config"].hardware.numCPU if mo.cls is vim.VirtualMachine else 8
        return [str(i) for i in range(n)]
    if inst_kind == "disk":
        return ["naa.6000c29a1b2c3d4e5f60718293a4b5c6"] if mo.cls is vim.HostSystem else ["scsi0:0"]
    if inst_kind == "vdisk":
        return [f"scsi0:{d.unitNumber}" for d in mo.props["config"].hardware.device
                if isinstance(d, vim.vm.device.VirtualDisk)]
    if inst_kind == "ds":
        return [OBJECTS[d._moId].props["summary"].url.rstrip("/").rsplit("/", 1)[-1]
                for d in mo.props.get("datastore") or []]
    if inst_kind == "nic":
        return ["vmnic0", "vmnic1"] if mo.cls is vim.HostSystem else ["4000"]
    return []


def _available(moid: str, interval: int) -> List[Tuple[int, str]]:
    kind = _kind(moid)
    level = None
    if interval != REALTIME:
        level = next((h.level for h in HIST if h.samplingPeriod == interval), None)
        if level is None:
            return []
    out = []
    for cid, c in COUNTERS.items():
        if kind not in c["kinds"] and not (kind == "ComputeResource" and CLU in c["kinds"]):
            continue
        if level is not None and c["level"] > level:
            continue
        out.append((cid, ""))
        if c["inst"] and (level is None or c["pdl"] <= level):
            out.extend((cid, i) for i in _instances(moid, c["inst"]))
    return out


def _incident_factor(t: dt.datetime) -> float:
    """0 outside the incident, ramps to 1 in 5 minutes and back down."""
    if t < INCIDENT_START or t > INCIDENT_END:
        return 0.0
    ramp = min((t - INCIDENT_START).total_seconds(), (INCIDENT_END - t).total_seconds()) / 300.0
    return max(0.0, min(1.0, ramp))


VM_BY_MOID = {s["moid"]: s for s in VM_SPECS}
ON_DS_PROD_01 = {s["moid"] for s in VM_SPECS if "datastore-40" in s["ds"] and s["power"] == "poweredOn"}


def _value(moid: str, cid: int, instance: str, t: dt.datetime, interval: int) -> int:
    """Deterministic sample, in vCenter's raw units (percent = hundredths)."""
    c = COUNTERS[cid]
    key = c["key"]
    ts = t.timestamp()
    diurnal = 0.5 + 0.5 * math.sin(2 * math.pi * (ts / 86400.0) + _h(moid) * 6.28)
    noise = _h(moid, cid, instance, int(ts)) - 0.5
    inc = _incident_factor(t)
    kind = _kind(moid)
    vm = VM_BY_MOID.get(moid)
    if kind == VM and vm and vm["power"] != "poweredOn":
        return 0 if key != "sys.uptime.latest" else 0
    if key == "cpu.usage.average":
        if kind == VM:
            base = vm["cpu_load"] * 100
        elif kind == HOST:
            base = next((h[4] for h in HOST_SPECS if h[0] == moid), 0.3) * 100
            if moid == "host-12":
                base = 70 + 22 * inc
        else:
            base = 55
        pct = max(0.0, min(100.0, base * (0.8 + 0.25 * diurnal) + noise * 6))
        if kind == HOST and any(h[0] == moid and h[3] for h in HOST_SPECS):
            pct = 0.4
        return int(pct * 100)
    if key == "cpu.usagemhz.average":
        mhz = (vm["cpu"] * 2600 * vm["cpu_load"]) if kind == VM else 30000
        return int(mhz * (0.8 + 0.3 * diurnal) + noise * 200)
    if key == "cpu.ready.summation":
        scale = interval * 1000 / 20000.0
        ready = 60 + 140 * diurnal + (900 * inc if moid in ("vm-112", "vm-113", "host-12") else 0)
        if instance:
            ready /= max(1, vm["cpu"] if vm else 8)
        return int(ready * scale)
    if key == "cpu.costop.summation":
        return int((10 + 20 * diurnal) * interval / 20)
    if key == "mem.usage.average":
        return int((45 + 20 * diurnal + noise * 4) * 100)
    if key in ("mem.consumed.average", "mem.active.average"):
        total_kb = (vm["mem_gb"] if vm else 768) * 1024 * 1024
        frac = 0.85 if key == "mem.consumed.average" else 0.3 + 0.2 * diurnal
        return int(total_kb * frac)
    if key == "mem.vmmemctl.average":
        return 0
    if key == "mem.swapinRate.average":
        return 0
    if key in ("disk.usage.average", "disk.read.average", "disk.write.average"):
        base = 2000 + 6000 * diurnal
        if moid == "vm-120":
            base += 380000 * inc + 40000
        elif moid in ON_DS_PROD_01:
            base += 9000 * inc
        if key == "disk.read.average":
            base *= 0.6
        elif key == "disk.write.average":
            base *= 0.4
        return int(max(0, base + noise * 300))
    if key == "disk.maxTotalLatency.latest":
        lat = 2 + 3 * diurnal + noise
        if moid in ON_DS_PROD_01 or moid in ("host-10", "host-11", "host-12"):
            lat += 60 + 30 * inc * (0.7 + 0.6 * _h(moid, int(ts) // 60)) if inc > 0 else 0
        return int(max(1, lat))
    if key.startswith("virtualDisk.") or key.startswith("datastore.total"):
        lat = 1.5 + 2 * diurnal + noise
        on_bad = False
        if key.startswith("datastore."):
            prod01 = OBJECTS["datastore-40"].props["summary"].url.rstrip("/").rsplit("/", 1)[-1] if "datastore-40" in OBJECTS else None
            on_bad = instance == prod01 or (instance == "" and moid in ON_DS_PROD_01)
        else:
            on_bad = moid in ON_DS_PROD_01
        if on_bad and inc > 0:
            lat += (70 if "Write" in key else 25) * inc
        return int(max(0, lat))
    if key == "datastore.numberWriteAveraged.average":
        return int(150 + 300 * diurnal + (1200 * inc if moid in ON_DS_PROD_01 or moid == "vm-120" else 0))
    if key in ("disk.used.latest", "disk.provisioned.latest", "disk.capacity.latest"):
        d = DATASTORES.get(moid, {"capacity": 0, "free": 0})
        cap_kb = d["capacity"] // 1024
        used_kb = (d["capacity"] - d["free"]) // 1024
        if key == "disk.capacity.latest":
            return cap_kb
        if key == "disk.provisioned.latest":
            return int(used_kb * 1.35)
        # usage grows: 1 week ago ~10 percentage points lower (snapshot growth)
        age = (NOW - t).total_seconds() / 604800.0
        return int(max(0, used_kb - cap_kb * 0.10 * min(age, 4)))
    if key in ("net.usage.average",):
        return int(800 + 3000 * diurnal + noise * 100)
    if key == "net.droppedRx.summation":
        return int(_h(moid, int(ts)) > 0.97)
    if key == "sys.uptime.latest":
        boot = (OBJECTS[moid].props["runtime"].bootTime or NOW)
        return max(0, int((t - boot).total_seconds()))
    if key == "power.power.average":
        return int(320 + 140 * diurnal)
    if key == "clusterServices.effectivecpu.average":
        return int(OBJECTS[moid].props["summary"].effectiveCpu)
    return 0


def _query_perf(specs: List[vim.PerformanceManager.QuerySpec]):
    results = []
    total_metrics = 0
    for spec in specs:
        moid = spec.entity._moId
        if moid not in OBJECTS:
            raise vmodl.fault.ManagedObjectNotFound(obj=spec.entity, msg="The object has already been deleted or has not been completely created")
        kind = _kind(moid)
        interval = spec.intervalId or (REALTIME if kind in (VM, HOST) else (HIST[0].samplingPeriod if HIST else REALTIME))
        if interval == REALTIME and kind not in (VM, HOST):
            raise vmodl.fault.InvalidArgument(invalidProperty="querySpec.interval",
                                              msg="A specified parameter was not correct: querySpec.interval")
        if interval != REALTIME and interval not in [h.samplingPeriod for h in HIST]:
            raise vmodl.fault.InvalidArgument(invalidProperty="querySpec.interval",
                                              msg="A specified parameter was not correct: querySpec.interval")
        avail = _available(moid, interval)
        if spec.metricId:
            wanted = []
            for m in spec.metricId:
                if m.instance == "*":
                    wanted.extend(a for a in avail if a[0] == m.counterId)
                else:
                    if (m.counterId, m.instance) in avail:
                        wanted.append((m.counterId, m.instance))
        else:
            wanted = list(avail)
        if interval != REALTIME:
            total_metrics += len(wanted)
        # timestamps: (start, end]; realtime keeps the last hour only
        end = min(spec.endTime or NOW, NOW)
        retention = 3600 if interval == REALTIME else next(h.length for h in HIST if h.samplingPeriod == interval)
        oldest = NOW - dt.timedelta(seconds=retention)
        start = max(spec.startTime or oldest, oldest)
        epoch_end = int(end.timestamp()) // interval * interval
        stamps = []
        t = epoch_end
        while t > start.timestamp():
            stamps.append(t)
            t -= interval
        stamps.reverse()
        if spec.maxSample:
            stamps = stamps[-spec.maxSample:]
        if not stamps or not wanted:
            continue
        times = [dt.datetime.fromtimestamp(s, UTC) for s in stamps]
        series = [vim.PerformanceManager.IntSeries(
            id=vim.PerformanceManager.MetricId(counterId=cid, instance=inst),
            value=[_value(moid, cid, inst, tt, interval) for tt in times]) for cid, inst in wanted]
        results.append(vim.PerformanceManager.EntityMetric(
            entity=spec.entity,
            sampleInfo=[vim.PerformanceManager.SampleInfo(timestamp=tt, interval=interval) for tt in times],
            value=series))
    if total_metrics > MAX_QUERY_METRICS:
        raise vmodl.fault.InvalidArgument(
            invalidProperty="querySpec.size",
            msg=f"A specified parameter was not correct: querySpec.size (requested {total_metrics} metrics, "
                f"vpxd.stats.maxQueryMetrics is {MAX_QUERY_METRICS})")
    return results


# ── events and tasks ──────────────────────────────────────────────────────────

EVENTS: List[vim.event.Event] = []
TASKS: List[vim.TaskInfo] = []
EVENT_CATEGORY: Dict[str, str] = {}


def _args(moid: Optional[str]) -> Dict[str, Any]:
    """Event entity arguments for an object (and its datacenter / compute resource)."""
    out: Dict[str, Any] = {}
    if not moid:
        return out
    chain = [moid] + _ancestors(moid)
    for m in chain:
        mo = OBJECTS.get(m)
        if mo is None:
            continue
        nm = mo.props.get("name")
        if mo.cls is vim.Datacenter:
            out.setdefault("datacenter", vim.event.DatacenterEventArgument(name=nm, datacenter=mo.ref))
        elif issubclass(mo.cls, vim.ComputeResource):
            out.setdefault("computeResource", vim.event.ComputeResourceEventArgument(name=nm, computeResource=mo.ref))
    mo = OBJECTS[moid]
    if mo.cls is vim.VirtualMachine:
        out["vm"] = vim.event.VmEventArgument(name=mo.props["name"], vm=mo.ref)
        host = mo.props["runtime"].host._moId
        out["host"] = vim.event.HostEventArgument(name=OBJECTS[host].props["name"], host=OBJECTS[host].ref)
        for m in _ancestors(host):
            if issubclass(OBJECTS[m].cls, vim.ComputeResource):
                out["computeResource"] = vim.event.ComputeResourceEventArgument(name=OBJECTS[m].props["name"], computeResource=OBJECTS[m].ref)
            if OBJECTS[m].cls is vim.Datacenter:
                out["datacenter"] = vim.event.DatacenterEventArgument(name=OBJECTS[m].props["name"], datacenter=OBJECTS[m].ref)
    elif mo.cls is vim.HostSystem:
        out["host"] = vim.event.HostEventArgument(name=mo.props["name"], host=mo.ref)
    elif mo.cls is vim.Datastore:
        out["ds"] = vim.event.DatastoreEventArgument(name=mo.props["name"], datastore=mo.ref)
    elif issubclass(mo.cls, vim.Network):
        out["net"] = vim.event.NetworkEventArgument(name=mo.props["name"], network=mo.ref)
    return out


def _event(cls, when: dt.datetime, msg: str, on: Optional[str] = None, user: str = "", category="info", **extra):
    key = len(EVENTS) + 1001
    kw = dict(key=key, chainId=key, createdTime=when, userName=user, fullFormattedMessage=msg)
    kw.update(_args(on))
    kw.update(extra)
    ev = cls(**kw)
    EVENTS.append(ev)
    type_key = extra.get("eventTypeId") or GetWsdlName(cls)
    EVENT_CATEGORY.setdefault(type_key, category)
    return ev


def _task(moid, name, desc_id, entity, user, state, queued: dt.datetime, seconds: int, error_msg=None, progress=None):
    task_key = f"task-{len(TASKS) + 3001}"
    add(vim.Task, task_key, None)
    err = None
    if error_msg:
        # pyVmomi models TaskInfo.error as the MethodFault itself and wraps it into a
        # LocalizedMethodFault (fault + localizedMessage) on the wire, as vpxd does.
        path = re.search(r"'(/vmfs/[^']+)'", error_msg)
        err = vim.fault.FileLocked(msg=error_msg, file=path.group(1) if path else "")
    try:
        method = GetWsdlMethod("urn:vim25", name)      # TaskInfo.name is a ManagedMethod on the wire
    except KeyError:
        method = None                                   # internal tasks (Drm.*) carry only descriptionId
    info = vim.TaskInfo(
        key=task_key, task=ref(task_key), name=method, descriptionId=desc_id, entity=ref(entity) if entity else None,
        entityName=OBJECTS[entity].props.get("name") if entity else None, state=state, cancelled=False,
        cancelable=state in ("running", "queued"), error=err, queueTime=queued,
        startTime=queued + dt.timedelta(seconds=1),
        completeTime=(queued + dt.timedelta(seconds=seconds)) if state in ("success", "error") else None,
        reason=vim.TaskReasonUser(userName=user), eventChainId=len(EVENTS) + 5000, progress=progress,
        activationId=None, changeTag=None, parentTaskKey=None, rootTaskKey=None)
    OBJECTS[task_key].props["info"] = info
    TASKS.append(info)
    return info


def _seed_events():
    if MODE == "esxi":
        _event(vim.event.UserLoginSessionEvent, NOW - dt.timedelta(minutes=5), "User root@127.0.0.1 logged in as pyvmomi",
               user="root", ipAddress="127.0.0.1", userAgent="pyvmomi", locale="en", sessionId=_uuid("s", 1))
        _event(vim.event.VmPoweredOnEvent, NOW - dt.timedelta(hours=3), "edge-gw-01 on esx-edge-01.acme.local in ha-datacenter is powered on",
               "vm-1", user="root", template=False)
        return
    # Background noise: service logins every 15 minutes for 7 days (forces collector paging).
    t = NOW - dt.timedelta(days=7)
    i = 0
    while t < NOW:
        user = "VSPHERE.LOCAL\\svc-monitoring"
        _event(vim.event.UserLoginSessionEvent, t, f"User {user}@10.10.0.20 logged in as python-requests/2.32",
               user=user, ipAddress="10.10.0.20", userAgent="python-requests/2.32", locale="en", sessionId=_uuid("s", i))
        _event(vim.event.UserLogoutSessionEvent, t + dt.timedelta(seconds=30), f"User {user}@10.10.0.20 logged out (login time: {t.isoformat()}, number of API invocations: 14, user agent: python-requests/2.32)",
               user=user, ipAddress="10.10.0.20", userAgent="python-requests/2.32", callCount=14, sessionId=_uuid("s", i), loginTime=t)
        t += dt.timedelta(minutes=15)
        i += 1
    # The snapshot story.
    for k, days in enumerate((21, 14, 7)):
        when = NOW - dt.timedelta(days=days)
        info = _task(None, "CreateSnapshot_Task", "VirtualMachine.createSnapshot", "vm-120",
                     "VSPHERE.LOCAL\\svc-backup", "success", when, 95)
        _event(vim.event.TaskEvent, when, "Task: Create virtual machine snapshot", "vm-120",
               user="VSPHERE.LOCAL\\svc-backup", info=info)
    when = NOW - dt.timedelta(days=2)
    info = _task(None, "EnterMaintenanceMode_Task", "HostSystem.enterMaintenanceMode", "host-13",
                 "VSPHERE.LOCAL\\Administrator", "success", when, 610)
    _event(vim.event.TaskEvent, when, "Task: Enter maintenance mode", "host-13", user="VSPHERE.LOCAL\\Administrator", info=info)
    _event(vim.event.EnteringMaintenanceModeEvent, when, "Host esx-prod-04.acme.local in DC-East is entering maintenance mode",
           "host-13", user="VSPHERE.LOCAL\\Administrator")
    _event(vim.event.EnteredMaintenanceModeEvent, when + dt.timedelta(minutes=10),
           "Host esx-prod-04.acme.local in DC-East has entered maintenance mode", "host-13", user="VSPHERE.LOCAL\\Administrator")
    when = NOW - dt.timedelta(minutes=50)
    info = _task(None, "RemoveSnapshot_Task", "VirtualMachine.removeSnapshot", "vm-120",
                 "VSPHERE.LOCAL\\svc-backup", "error", when, 260,
                 error_msg="An error occurred while consolidating disks: Failed to lock the file. Cannot open the disk "
                           "'/vmfs/volumes/ds-prod-01/backup-proxy-01/backup-proxy-01-000003.vmdk' or one of the snapshot disks it depends on.")
    _event(vim.event.TaskEvent, when, "Task: Remove snapshot", "vm-120", user="VSPHERE.LOCAL\\svc-backup", info=info)
    _event(vim.event.EventEx, when + dt.timedelta(minutes=4, seconds=20),
           "Virtual machine backup-proxy-01 disks consolidation is needed.", "vm-120", category="warning",
           eventTypeId="com.vmware.vc.VmDiskConsolidationNeeded", severity="warning",
           objectId="vm-120", objectType=vim.VirtualMachine, objectName="backup-proxy-01")
    _event(vim.event.AlarmStatusChangedEvent, when + dt.timedelta(minutes=4, seconds=25),
           "Alarm 'Virtual machine consolidation needed status' on backup-proxy-01 changed from Gray to Yellow",
           "vm-120", category="warning", **{"from": "gray"}, to="yellow",
           alarm=vim.event.AlarmEventArgument(name="Virtual machine consolidation needed status", alarm=ref("alarm-3")),
           source=vim.event.ManagedEntityEventArgument(name="Datacenters", entity=ref(ROOT)),
           entity=vim.event.ManagedEntityEventArgument(name="backup-proxy-01", entity=ref("vm-120")))
    _event(vim.event.AlarmStatusChangedEvent, NOW - dt.timedelta(hours=6),
           "Alarm 'Datastore usage on disk' on ds-prod-01 changed from Green to Yellow", "datastore-40",
           category="warning", **{"from": "green"}, to="yellow",
           alarm=vim.event.AlarmEventArgument(name="Datastore usage on disk", alarm=ref("alarm-1")),
           source=vim.event.ManagedEntityEventArgument(name="Datacenters", entity=ref(ROOT)),
           entity=vim.event.ManagedEntityEventArgument(name="ds-prod-01", entity=ref("datastore-40")))
    _event(vim.event.AlarmStatusChangedEvent, NOW - dt.timedelta(minutes=40),
           "Alarm 'Datastore usage on disk' on ds-prod-01 changed from Yellow to Red", "datastore-40",
           category="error", **{"from": "yellow"}, to="red",
           alarm=vim.event.AlarmEventArgument(name="Datastore usage on disk", alarm=ref("alarm-1")),
           source=vim.event.ManagedEntityEventArgument(name="Datacenters", entity=ref(ROOT)),
           entity=vim.event.ManagedEntityEventArgument(name="ds-prod-01", entity=ref("datastore-40")))
    for minutes, before, after in ((42, 1843, 61204), (31, 2210, 89012), (18, 1990, 74550)):
        _event(vim.event.EventEx, NOW - dt.timedelta(minutes=minutes),
               f"Device naa.6000c29a1b2c3d4e5f60718293a4b5c6 performance has deteriorated. I/O latency increased "
               f"from average value of {before} microseconds to {after} microseconds.", "host-12",
               category="warning", eventTypeId="esx.problem.scsi.device.io.latency.high", severity="warning",
               objectId="host-12", objectType=vim.HostSystem, objectName="esx-prod-03.acme.local",
               arguments=[vmodl.KeyAnyValue(key="1", value="naa.6000c29a1b2c3d4e5f60718293a4b5c6"),
                          vmodl.KeyAnyValue(key="2", value=before), vmodl.KeyAnyValue(key="3", value=after)])
    _event(vim.event.AlarmStatusChangedEvent, NOW - dt.timedelta(minutes=38),
           "Alarm 'Host CPU usage' on esx-prod-03.acme.local changed from Green to Yellow", "host-12",
           category="warning", **{"from": "green"}, to="yellow",
           alarm=vim.event.AlarmEventArgument(name="Host CPU usage", alarm=ref("alarm-2")),
           source=vim.event.ManagedEntityEventArgument(name="Datacenters", entity=ref(ROOT)),
           entity=vim.event.ManagedEntityEventArgument(name="esx-prod-03.acme.local", entity=ref("host-12")))
    when = NOW - dt.timedelta(minutes=33)
    info = _task(None, "Drm.ExecuteVMotionLRO", "Drm.ExecuteVMotionLRO", "vm-112", "com.vmware.vcIntegrity", "success", when, 48)
    _event(vim.event.DrsVmMigratedEvent, when + dt.timedelta(seconds=48),
           "Migrating app-02 from esx-prod-03.acme.local to esx-prod-02.acme.local in DC-East", "vm-112",
           user="", category="info",
           sourceHost=vim.event.HostEventArgument(name="esx-prod-03.acme.local", host=ref("host-12")),
           sourceDatacenter=vim.event.DatacenterEventArgument(name="DC-East", datacenter=ref("datacenter-3")),
           sourceDatastore=vim.event.DatastoreEventArgument(name="ds-prod-01", datastore=ref("datastore-40")))
    when = NOW - dt.timedelta(minutes=12)
    _task(None, "ConsolidateVMDisks_Task", "VirtualMachine.consolidateDisks", "vm-120",
          "VSPHERE.LOCAL\\oncall", "running", when, 0, progress=37)
    _event(vim.event.VmPoweredOffEvent, NOW - dt.timedelta(hours=5), "dev-test-03 on esx-dev-01.acme.local in DC-East is powered off",
           "vm-203", user="VSPHERE.LOCAL\\dev1", template=False)
    _event(vim.event.VmSuspendedEvent, NOW - dt.timedelta(hours=4), "dev-test-04 on esx-dev-02.acme.local in DC-East is suspended",
           "vm-204", user="VSPHERE.LOCAL\\dev1", template=False)
    _event(vim.event.VmPoweredOnEvent, NOW - dt.timedelta(days=3), "prod-db-01 on esx-prod-01.acme.local in DC-East is powered on",
           "vm-101", user="VSPHERE.LOCAL\\Administrator", template=False)
    for k in range(6):
        when = NOW - dt.timedelta(hours=8 * k + 1)
        _task(None, "CreateSnapshot_Task", "VirtualMachine.createSnapshot", "vm-202", "VSPHERE.LOCAL\\dev1",
              "success", when, 20)
    EVENTS.sort(key=lambda e: e.createdTime)


_seed_events()
OBJECTS[EVT_MOID].props["description"] = vim.event.EventDescription(
    category=[vim.ElementDescription(key=k, label=k.capitalize(), summary=k) for k in ("info", "warning", "error", "user")],
    # vpxd keys eventInfo by event class; extended events all share key=EventEx and
    # carry their eventTypeId as the first '|' field of fullFormat.
    eventInfo=[vim.event.EventDescription.EventDetail(
        key=GetWsdlType("urn:vim25", k) if "." not in k else vim.event.EventEx, category=c, description=k,
        formatOnDatacenter="", formatOnComputeResource="", formatOnHost="", formatOnVm="",
        fullFormat=k if "." not in k else f"{k}|{k}") for k, c in sorted(EVENT_CATEGORY.items())])
OBJECTS[EVT_MOID].props["latestEvent"] = EVENTS[-1] if EVENTS else None
OBJECTS[TASK_MOID].props["recentTask"] = [t.task for t in TASKS if t.queueTime > NOW - dt.timedelta(minutes=60)]


def _event_type_key(ev) -> str:
    return getattr(ev, "eventTypeId", None) or GetWsdlName(type(ev))


def _entity_ids_of(ev) -> List[str]:
    ids = []
    for attr in ("vm", "host", "ds", "net", "computeResource", "datacenter"):
        arg = getattr(ev, attr, None)
        if arg is None:
            continue
        mo = (getattr(arg, "vm", None) or getattr(arg, "host", None) or getattr(arg, "datastore", None)
              or getattr(arg, "network", None) or getattr(arg, "computeResource", None) or getattr(arg, "datacenter", None))
        if mo is not None:
            ids.append(mo._moId)
    return ids


def _matches_entity(ids: List[str], spec_entity, recursion: str) -> bool:
    if spec_entity is None:
        return True
    target = spec_entity._moId
    if target in ids and recursion in ("self", "all", None, ""):
        return True
    if recursion in ("children", "all"):
        for i in ids:
            anc = _ancestors(i)
            if recursion == "children" and anc[:1] == [target]:
                return True
            if recursion == "all" and target in anc:
                return True
    return False


def _filter_events(spec) -> List[vim.event.Event]:
    out = []
    for ev in EVENTS:
        if spec is not None:
            if spec.time:
                if spec.time.beginTime and ev.createdTime < spec.time.beginTime:
                    continue
                if spec.time.endTime and ev.createdTime > spec.time.endTime:
                    continue
            if spec.eventTypeId and _event_type_key(ev) not in spec.eventTypeId:
                continue
            if spec.category and EVENT_CATEGORY.get(_event_type_key(ev), "info") not in spec.category:
                continue
            if spec.userName and spec.userName.userList and ev.userName not in spec.userName.userList:
                continue
            if spec.entity and not _matches_entity(_entity_ids_of(ev), spec.entity.entity, spec.entity.recursion):
                continue
        out.append(ev)
    return out


def _filter_tasks(spec) -> List[vim.TaskInfo]:
    out = []
    for ti in sorted(TASKS, key=lambda t: t.queueTime):
        if spec is not None:
            if spec.time:
                stamp = {"queuedTime": ti.queueTime, "startedTime": ti.startTime,
                         "completedTime": ti.completeTime}.get(spec.time.timeType or "queuedTime")
                if stamp is None:
                    continue
                if spec.time.beginTime and stamp < spec.time.beginTime:
                    continue
                if spec.time.endTime and stamp > spec.time.endTime:
                    continue
            if spec.state and ti.state not in spec.state:
                continue
            if spec.userName and spec.userName.userList and getattr(ti.reason, "userName", None) not in spec.userName.userList:
                continue
            if spec.entity and ti.entity is not None and not _matches_entity([ti.entity._moId], spec.entity.entity, spec.entity.recursion):
                continue
            if spec.entity and ti.entity is None:
                continue
        out.append(ti)
    return out


# ── property access ───────────────────────────────────────────────────────────

def _prop_type(cls, path: str):
    """Declared type of a dotted property path (raises InvalidProperty if unknown)."""
    t = cls
    for part in path.split("."):
        try:
            info = GetPropertyInfo(t, part)
        except AttributeError:
            raise vmodl.query.InvalidProperty(name=path, msg=f"A specified parameter was not correct: {path}")
        t = info.type
        if issubclass(t, list) and part != path.split(".")[-1]:
            raise vmodl.query.InvalidProperty(name=path, msg=f"A specified parameter was not correct: {path}")
    return t


def _computed(mo: MO, top: str):
    if top == "triggeredAlarmState" and issubclass(mo.cls, vim.ManagedEntity):
        return _triggered_for(mo.moid)
    if top == "childEntity":
        return [OBJECTS[c].ref for c in CHILDREN.get(mo.moid, [])
                if OBJECTS[c].cls not in (vim.ResourcePool,) and not (OBJECTS[c].cls is vim.VirtualMachine and mo.cls is vim.ResourcePool)]
    if top == "view" and mo.cls is vim.view.ContainerView:
        return mo.props["view"]
    return mo.props.get(top)


def _get(mo: MO, path: str):
    ptype = _prop_type(mo.cls, path)
    parts = path.split(".")
    val = _computed(mo, parts[0])
    for part in parts[1:]:
        if val is None:
            return None, ptype
        val = getattr(val, part, None)
    if isinstance(val, list):
        if not val and getattr(ptype, "Item", None) is None:
            return None, ptype
        if not isinstance(val, VmomiSupport.Array) or type(val) is not ptype:
            try:
                val = ptype(val)
            except Exception:
                pass
    return val, ptype


def _all_props(mo: MO) -> List[str]:
    names = []
    t = mo.cls
    while t is not None and hasattr(t, "_propInfo"):
        names.extend(t._propInfo.keys())
        t = t.__bases__[0] if t.__bases__ else None
    return [n for n in dict.fromkeys(names)]


# ── views, property collector state ───────────────────────────────────────────

_lock = threading.RLock()
VIEWS: Dict[str, MO] = {}
RETRIEVE_TOKENS: Dict[str, Tuple[str, List]] = {}
COLLECTORS: Dict[str, dict] = {}
CALLS: Dict[str, int] = {}


def _descendants(root: str) -> List[str]:
    seen, out, stack = set(), [], list(CHILDREN.get(root, []))
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        out.append(m)
        stack.extend(CHILDREN.get(m, []))
    return out


def _create_view(session, container, types, recursive):
    root = container._moId
    if root not in OBJECTS:
        raise vmodl.fault.ManagedObjectNotFound(obj=container, msg="The object has already been deleted or has not been completely created")
    classes = []
    for tname in types or []:
        if isinstance(tname, type):           # deserialized TypeName
            classes.append(tname)
            continue
        try:
            classes.append(GetWsdlType("urn:vim25", tname))
        except KeyError:
            raise vmodl.fault.InvalidArgument(invalidProperty="type", msg=f"A specified parameter was not correct: type ({tname})")
    members = _descendants(root) if recursive else CHILDREN.get(root, [])
    view = [OBJECTS[m].ref for m in members
            if issubclass(OBJECTS[m].cls, vim.ManagedEntity) and (not classes or any(issubclass(OBJECTS[m].cls, c) for c in classes))]
    moid = f"session[{session['key'][:8]}-{uuid.uuid4().hex[:4]}]{uuid.uuid4().hex[:20]}"
    mo = MO(vim.view.ContainerView, moid, container=container, type=classes, recursive=recursive,
            view=vim.ManagedObject.Array(view))
    VIEWS[moid] = mo
    OBJECTS[moid] = mo
    session["views"].add(moid)
    return mo.ref


def _collect(object_specs) -> List[str]:
    named: Dict[str, Any] = {}

    def register(sel_list):
        for s in sel_list or []:
            if s.name:
                named.setdefault(s.name, s)
            if isinstance(s, vmodl.query.PropertyCollector.TraversalSpec):
                register(s.selectSet)
    for os_ in object_specs:
        register(os_.selectSet)
    out: List[str] = []
    seen = set()

    def visit(moid: str, sel_list, depth=0):
        if depth > 40:
            return
        for s in sel_list or []:
            spec = s if isinstance(s, vmodl.query.PropertyCollector.TraversalSpec) else named.get(s.name)
            if spec is None:
                raise vmodl.fault.InvalidArgument(invalidProperty="selectSet", msg=f"A specified parameter was not correct: selectSet ({s.name})")
            mo = OBJECTS.get(moid)
            if mo is None or not issubclass(mo.cls, spec.type):
                continue
            val, _ = _get(mo, spec.path)
            targets = val if isinstance(val, list) else ([val] if val is not None else [])
            for t in targets:
                if not isinstance(t, ManagedObject) or t._moId not in OBJECTS:
                    continue
                key = (t._moId, spec.name or id(spec))
                if key in seen:
                    continue
                seen.add(key)
                if not spec.skip:
                    out.append(t._moId)
                visit(t._moId, spec.selectSet, depth + 1)

    for os_ in object_specs:
        moid = os_.obj._moId
        if moid not in OBJECTS:
            raise vmodl.fault.ManagedObjectNotFound(obj=os_.obj, msg="The object has already been deleted or has not been completely created")
        if not os_.skip:
            out.append(moid)
        visit(moid, os_.selectSet)
    return list(dict.fromkeys(out))


def _retrieve(spec_set) -> List:
    PC = vmodl.query.PropertyCollector
    contents = []
    for spec in spec_set:
        for moid in _collect(spec.objectSet):
            mo = OBJECTS[moid]
            props = []
            matched = False
            for ps in spec.propSet:
                if not issubclass(mo.cls, ps.type):
                    continue
                matched = True
                paths = _all_props(mo) if ps.all else list(ps.pathSet or [])
                for p in paths:
                    val, _ = _get(mo, p)
                    if val is None or (isinstance(val, list) and len(val) == 0 and getattr(type(val), "Item", None) is None):
                        continue
                    props.append(vmodl.DynamicProperty(name=p, val=val))
            if matched:
                contents.append(PC.ObjectContent(obj=mo.ref, propSet=props, missingSet=[]))
    return contents


def _page(contents: List, max_objects: Optional[int]):
    PC = vmodl.query.PropertyCollector
    size = min(max_objects or PAGE_CAP, PAGE_CAP)
    page, rest = contents[:size], contents[size:]
    token = None
    if rest:
        token = uuid.uuid4().hex
        RETRIEVE_TOKENS[token] = (token, rest)
    if not page and not token:
        return None
    return PC.RetrieveResult(objects=page, token=token)


# ── SOAP dispatch ─────────────────────────────────────────────────────────────

SESSIONS: Dict[str, dict] = {}
VERSIONS_SEEN: set = set()      # SOAPAction values clients negotiated, e.g. urn:vim25/8.0.3.0


def _check_max_count(n):
    if n is None or n < 0 or n > COLLECTOR_MAX:
        raise vmodl.fault.InvalidArgument(invalidProperty="maxCount",
                                          msg=f"A specified parameter was not correct: maxCount (must be 0..{COLLECTOR_MAX})")


def _dispatch(method: str, this, args: Dict[str, Any], session: Optional[dict], headers) -> Tuple[Any, Optional[str]]:
    """Run one SOAP method. Returns (result, set_cookie)."""
    moid = this._moId if this is not None else None
    CALLS[method] = CALLS.get(method, 0) + 1
    if method == "RetrieveServiceContent":
        return _service_content(), None
    if method == "Login":
        if args.get("userName") != USER or args.get("password") != PASSWORD:
            raise vim.fault.InvalidLogin(msg="Cannot complete login due to an incorrect user name or password.")
        mine = [s for s in SESSIONS.values() if s["user"] == USER]
        if len(mine) >= MAX_SESSIONS:
            raise vim.fault.NoPermission(msg=f"Session limit reached: {MAX_SESSIONS} sessions for user {USER}",
                                         privilegeId="System.View")
        key = str(uuid.uuid4())
        cookie = hashlib.sha1(key.encode()).hexdigest()
        user_session = vim.UserSession(
            key=key, userName=("VSPHERE.LOCAL\\" + USER.split("@")[0]) if "@" in USER else USER,
            fullName=USER, loginTime=dt.datetime.now(UTC), lastActiveTime=dt.datetime.now(UTC), locale="en",
            messageLocale="en", extensionSession=False, ipAddress="127.0.0.1",
            userAgent=headers.get("User-Agent", ""), callCount=0)
        SESSIONS[cookie] = {"key": key, "user": USER, "views": set(), "info": user_session}
        return user_session, f'vmware_soap_session="{cookie}"; Path=/; HttpOnly; Secure;'
    if session is None:
        raise vim.fault.NotAuthenticated(object=this, privilegeId="System.View", msg="The session is not authenticated.")
    if method == "Logout":
        for v in session["views"]:
            VIEWS.pop(v, None)
            OBJECTS.pop(v, None)
        SESSIONS.pop(session["cookie"], None)
        return None, None
    if method == "Fetch":
        mo = OBJECTS.get(moid)
        if mo is None:
            raise vmodl.fault.ManagedObjectNotFound(obj=this, msg="The object has already been deleted or has not been completely created")
        val, _ = _get(mo, args["prop"])
        return val, None
    if method == "CurrentTime":
        return dt.datetime.now(UTC), None
    if method == "CreateContainerView":
        return _create_view(session, args["container"], args.get("type"), bool(args.get("recursive"))), None
    if method == "DestroyView":
        VIEWS.pop(moid, None)
        OBJECTS.pop(moid, None)
        session["views"].discard(moid)
        return None, None
    if method in ("RetrievePropertiesEx", "RetrieveProperties", "RetrieveContents"):
        contents = _retrieve(args["specSet"])
        if method != "RetrievePropertiesEx":
            return contents, None
        opts = args.get("options")
        return _page(contents, opts.maxObjects if opts else None), None
    if method == "ContinueRetrievePropertiesEx":
        tok = args["token"]
        if tok not in RETRIEVE_TOKENS:
            raise vmodl.fault.InvalidArgument(invalidProperty="token", msg="A specified parameter was not correct: token")
        _, rest = RETRIEVE_TOKENS.pop(tok)
        return _page(rest, None), None
    if method == "CancelRetrievePropertiesEx":
        RETRIEVE_TOKENS.pop(args["token"], None)
        return None, None
    if method == "QueryPerfCounter":
        return [COUNTERS[c]["info"] for c in args.get("counterId") or [] if c in COUNTERS], None
    if method == "QueryPerfProviderSummary":
        kind = _kind(args["entity"]._moId)
        rt = kind in (VM, HOST)
        return vim.PerformanceManager.ProviderSummary(entity=args["entity"], currentSupported=rt,
                                                      summarySupported=bool(HIST) or rt,
                                                      refreshRate=REALTIME if rt else -1), None
    if method == "QueryAvailablePerfMetric":
        ent = args["entity"]
        kind = _kind(ent._moId)
        interval = args.get("intervalId") or (REALTIME if kind in (VM, HOST) else (HIST[0].samplingPeriod if HIST else REALTIME))
        if interval == REALTIME and kind not in (VM, HOST):
            raise vmodl.fault.InvalidArgument(invalidProperty="intervalId", msg="A specified parameter was not correct: intervalId")
        return [vim.PerformanceManager.MetricId(counterId=c, instance=i) for c, i in _available(ent._moId, interval)], None
    if method == "QueryPerf":
        return _query_perf(args["querySpec"]), None
    if method == "QueryEvents":
        evs = _filter_events(args.get("filter"))
        mc = getattr(args.get("filter"), "maxCount", None) or COLLECTOR_MAX
        return evs[-min(mc, COLLECTOR_MAX):], None
    if method in ("CreateCollectorForEvents", "CreateCollectorForTasks"):
        is_events = method == "CreateCollectorForEvents"
        items = _filter_events(args.get("filter")) if is_events else _filter_tasks(args.get("filter"))
        cmoid = f"session[{session['key'][:8]}]{uuid.uuid4().hex[:24]}"
        cls = vim.event.EventHistoryCollector if is_events else vim.TaskHistoryCollector
        COLLECTORS[cmoid] = {"items": items, "pos": 0, "events": is_events}
        OBJECTS[cmoid] = MO(cls, cmoid, filter=args.get("filter"))
        session["views"].add(cmoid)
        return cls(cmoid), None
    if method in ("ReadNextEvents", "ReadNextTasks", "ReadPreviousEvents", "ReadPreviousTasks"):
        col = COLLECTORS.get(moid)
        if col is None:
            raise vmodl.fault.ManagedObjectNotFound(obj=this, msg="The object has already been deleted or has not been completely created")
        n = args.get("maxCount")
        _check_max_count(n)
        if method.startswith("ReadNext"):
            chunk = col["items"][col["pos"]:col["pos"] + n]
            col["pos"] += len(chunk)
        else:
            start = max(0, col["pos"] - n)
            chunk = col["items"][start:col["pos"]]
            col["pos"] = start
        return chunk, None
    if method in ("ResetCollector", "RewindCollector"):
        col = COLLECTORS.get(moid)
        if col is not None:
            col["pos"] = 0 if method == "RewindCollector" else len(col["items"])
        return None, None
    if method == "DestroyCollector":
        COLLECTORS.pop(moid, None)
        OBJECTS.pop(moid, None)
        session["views"].discard(moid)
        return None, None
    if method == "SetCollectorPageSize":
        return None, None
    raise vmodl.fault.MethodNotFound(receiver=this, method=method, msg=f"The method {method} is not supported by this mock.")


# ── wire encoding ─────────────────────────────────────────────────────────────

_SOAP_HEAD = (SoapAdapter.XML_HEADER + "\n" + SoapAdapter.SOAP_ENVELOPE_START + "\n<soapenv:Body>\n")
_SOAP_TAIL = "</soapenv:Body>\n</soapenv:Envelope>"
_VERSION_ATTR = re.compile(r' versionId="[^"]*"')


def _wire(xml: str) -> str:
    # vpxd spells a typed moref in an anyType slot as ManagedObjectReference and
    # does not stamp versionId on every element; match the real wire.
    return _VERSION_ATTR.sub("", xml).replace('xsi:type="ManagedObject"', 'xsi:type="ManagedObjectReference"')


def _encode_response(method: str, result, info, version) -> str:
    body = ""
    if info is None:          # Fetch: the value travels as xsd:anyType with its xsi:type
        if result is not None:
            body = SoapAdapter.SerializeToStr(result, Object(name="returnval", type=object, version=version, flags=0), version)
    elif isinstance(result, list) and not result:
        body = ""                 # an empty array is an empty <xResponse/>, as vpxd sends it
    elif result is not None and info.result is not type(None):
        body = SoapAdapter.SerializeToStr(result, Object(name="returnval", type=info.result, version=version, flags=0), version)
    return _SOAP_HEAD + f'<{method}Response xmlns="urn:vim25">' + _wire(body) + f"</{method}Response>\n" + _SOAP_TAIL


def _encode_fault(fault, version) -> str:
    detail = SoapAdapter.SerializeFaultDetail(fault, version=version)
    name = GetWsdlName(type(fault))
    detail = detail.replace("<obj ", f"<{name}Fault ", 1)
    detail = detail[: detail.rfind("</obj>")] + f"</{name}Fault>"
    msg = SoapAdapter.XmlEscape(fault.msg or name)
    return (_SOAP_HEAD + "<soapenv:Fault>\n<faultcode>ServerFaultCode</faultcode>\n"
            f"<faultstring>{msg}</faultstring>\n<detail>{_wire(detail)}</detail>\n</soapenv:Fault>\n" + _SOAP_TAIL)


def _decode_request(body: bytes):
    root = ET.fromstring(body)
    soap_body = next(c for c in root if c.tag.endswith("}Body"))
    req = list(soap_body)[0]
    method = req.tag.split("}", 1)[-1]
    if method == "Fetch":
        # vmodl's generic property getter, used by pyVmomi 8+ for `mo.someProperty`.
        this, prop = None, None
        for child in req:
            tag = child.tag.split("}", 1)[-1]
            if tag == "_this":
                this = GetWsdlType("urn:vim25", child.attrib.get("type"))(child.text)
            elif tag == "prop":
                prop = child.text
        return method, None, this, {"prop": prop}
    info = GetWsdlMethod("urn:vim25", method).info
    this = None
    args: Dict[str, Any] = {}
    by_name = {p.name: p for p in info.params}
    for child in req:
        tag = child.tag.split("}", 1)[-1]
        if tag == "_this":
            cls = GetWsdlType("urn:vim25", child.attrib.get("type"))
            this = cls(child.text)
            continue
        p = by_name[tag]
        xml = ET.tostring(child, encoding="unicode")
        if issubclass(p.type, list):
            args.setdefault(tag, p.type()).append(SoapAdapter.Deserialize(xml, p.type.Item))
        else:
            args[tag] = SoapAdapter.Deserialize(xml, p.type)
    return method, info, this, args


# ── REST (vAPI) tagging ───────────────────────────────────────────────────────

REST_SESSIONS: Dict[str, str] = {}
CATEGORIES = [
    ("Environment", "SINGLE", ["VirtualMachine", "HostSystem", "Datastore"], ["prod", "dev"]),
    ("Owner", "MULTIPLE", ["VirtualMachine"], ["team-db", "team-app", "team-platform"]),
    ("Backup", "SINGLE", ["VirtualMachine"], ["daily", "none"]),
    ("Tier", "SINGLE", ["VirtualMachine", "Datastore"], ["gold", "silver"]),
]
CAT_IDS = {name: f"urn:vmomi:InventoryServiceCategory:{_uuid('cat', name)}:GLOBAL" for name, *_ in CATEGORIES}
TAG_IDS = {(c, t): f"urn:vmomi:InventoryServiceTag:{_uuid('tag', c, t)}:GLOBAL" for c, _, _, tags in CATEGORIES for t in tags}
ASSOC: Dict[str, List[str]] = {tid: [] for tid in TAG_IDS.values()}


def _tag(cat, tag, *moids):
    for m in moids:
        if m in OBJECTS:
            ASSOC[TAG_IDS[(cat, tag)]].append(m)


if MODE != "esxi":
    prod_vms = [s["moid"] for s in VM_SPECS if s["moid"] in {"vm-101", "vm-102", "vm-111", "vm-112", "vm-113", "vm-114",
                                                             "vm-120", "vm-131", "vm-132", "vm-133"}]
    _tag("Environment", "prod", *prod_vms, "host-10", "host-11", "host-12", "host-13", "datastore-40", "datastore-41")
    _tag("Environment", "dev", "vm-201", "vm-202", "vm-203", "vm-204", "vm-210", "host-31", "host-32", "datastore-43")
    _tag("Owner", "team-db", "vm-101", "vm-102")
    _tag("Owner", "team-app", "vm-111", "vm-112", "vm-113", "vm-114", "vm-131", "vm-132", "vm-133")
    _tag("Owner", "team-platform", "vm-120", "vm-301", "vm-302")
    _tag("Backup", "daily", "vm-101", "vm-102", "vm-111", "vm-112")
    _tag("Backup", "none", "vm-201", "vm-202", "vm-203", "vm-204")
    _tag("Tier", "gold", "vm-101", "vm-102", "datastore-40")
    _tag("Tier", "silver", "vm-111", "vm-112", "vm-113", "vm-114", "datastore-41")


def _rest(handler, method: str, path: str, query: Dict[str, List[str]], body: Optional[dict]):
    if MODE == "esxi":
        return 404, {"error_type": "NOT_FOUND", "messages": []}
    unauth = (401, {"error_type": "UNAUTHENTICATED", "messages": [{"args": [], "default_message": "Authentication required.",
                                                                    "id": "com.vmware.vapi.endpoint.method.authentication.required"}]})
    CALLS[f"REST {method} {re.sub(r'urn:[^/]+', '{id}', path)}"] = CALLS.get(f"REST {method} {re.sub(r'urn:[^/]+', '{id}', path)}", 0) + 1
    if path == "/api/session":
        if method == "POST":
            auth = handler.headers.get("Authorization", "")
            if not auth.startswith("Basic "):
                return unauth
            try:
                user, _, pwd = base64.b64decode(auth[6:]).decode().partition(":")
            except Exception:
                return unauth
            if user != USER or pwd != PASSWORD:
                return unauth
            if sum(1 for u in REST_SESSIONS.values() if u == user) >= MAX_SESSIONS:
                return 503, {"error_type": "SERVICE_UNAVAILABLE", "messages": [{"default_message": "Too many sessions"}]}
            tok = uuid.uuid4().hex
            REST_SESSIONS[tok] = user
            return 201, tok
        tok = handler.headers.get("vmware-api-session-id")
        if tok not in REST_SESSIONS:
            return unauth
        if method == "DELETE":
            REST_SESSIONS.pop(tok, None)
            return 204, None
        return 200, {"user": REST_SESSIONS[tok], "created_time": NOW.isoformat(), "last_accessed_time": NOW.isoformat()}
    tok = handler.headers.get("vmware-api-session-id")
    if tok not in REST_SESSIONS:
        return unauth
    if path == "/api/cis/tagging/category" and method == "GET":
        return 200, list(CAT_IDS.values())
    if path.startswith("/api/cis/tagging/category/") and method == "GET":
        cid = path.rsplit("/", 1)[-1]
        for name, card, types, _ in CATEGORIES:
            if CAT_IDS[name] == cid:
                return 200, {"id": cid, "name": name, "description": f"{name} classification",
                             "cardinality": card, "associable_types": types, "used_by": []}
        return 404, {"error_type": "NOT_FOUND", "messages": [{"default_message": f"Category {cid} not found"}]}
    if path == "/api/cis/tagging/tag" and method == "GET":
        return 200, list(TAG_IDS.values())
    if path.startswith("/api/cis/tagging/tag/") and method == "GET":
        tid = path.rsplit("/", 1)[-1]
        for (cat, tag), t in TAG_IDS.items():
            if t == tid:
                return 200, {"id": tid, "category_id": CAT_IDS[cat], "name": tag, "description": "", "used_by": []}
        return 404, {"error_type": "NOT_FOUND", "messages": [{"default_message": f"Tag {tid} not found"}]}
    if path == "/api/cis/tagging/tag-association" and method == "POST":
        action = (query.get("action") or [""])[0]
        if action != "list-attached-objects-on-tags":
            return 400, {"error_type": "INVALID_ARGUMENT", "messages": [{"default_message": f"Unknown action {action}"}]}
        out = []
        for tid in (body or {}).get("tag_ids") or []:
            if tid in ASSOC:
                out.append({"tag_id": tid, "object_ids": [{"id": m, "type": GetWsdlName(OBJECTS[m].cls)} for m in ASSOC[tid]]})
        return 200, out
    return 404, {"error_type": "NOT_FOUND", "messages": [{"default_message": f"{method} {path} not found"}]}


# ── HTTP server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VMware vCenter Server (mock)"

    def log_message(self, fmt, *args):  # quiet by default
        if os.environ.get("VCENTER_MOCK_LOG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str, extra: Optional[Dict[str, str]] = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/sdk/vimServiceVersions.xml":
            return self._send(200, _service_versions_xml().encode(), "text/xml")
        if url.path == "/mock/state":
            with _lock:
                state = {"mode": MODE, "api_version": API_VERSION, "now": NOW.isoformat(),
                         "incident_start": INCIDENT_START.isoformat(), "incident_end": INCIDENT_END.isoformat(),
                         "soap_sessions": len(SESSIONS), "rest_sessions": len(REST_SESSIONS),
                         "open_views_and_collectors": len(VIEWS) + len(COLLECTORS),
                         "vm_count": sum(1 for o in OBJECTS.values() if o.cls is vim.VirtualMachine),
                         "calls": dict(sorted(CALLS.items()))}
            return self._send(200, json.dumps(state, indent=2).encode(), "application/json")
        if url.path.startswith("/api/"):
            return self._rest("GET", url)
        if url.path in ("/", "/ui"):
            return self._send(200, b"<html><body>VMware vCenter Server (mock)</body></html>", "text/html")
        return self._send(404, b"Not Found", "text/plain")

    def do_DELETE(self):
        return self._rest("DELETE", urlsplit(self.path))

    def do_POST(self):
        url = urlsplit(self.path)
        if url.path.startswith("/api/"):
            return self._rest("POST", url)
        if url.path != "/sdk":
            return self._send(404, b"Not Found", "text/plain")
        raw = self._body()
        soap_action = self.headers.get("SOAPAction", "").strip('"')
        version = GetVersionFromVersionUri(soap_action) if soap_action.startswith("urn:vim25/") else VERSION_NAME
        VERSIONS_SEEN.add(soap_action)
        cookie_hdr = self.headers.get("Cookie") or ""
        m = re.search(r'vmware_soap_session="?([^";]+)"?', cookie_hdr)
        with _lock:
            session = SESSIONS.get(m.group(1)) if m else None
            if session is not None:
                session["cookie"] = m.group(1)
            set_cookie = None
            try:
                method, info, this, args = _decode_request(raw)
                result, set_cookie = _dispatch(method, this, args, session, self.headers)
                body = _encode_response(method, result, info, version)
                status = 200
            except vmodl.MethodFault as fault:
                body, status = _encode_fault(fault, version), 500
            except KeyError as exc:
                fault = vmodl.fault.InvalidRequest(msg=f"Unsupported method or parameter: {exc}")
                body, status = _encode_fault(fault, version), 500
            except Exception as exc:   # a mock bug: report it like vpxd's SystemError, keep serving
                traceback.print_exc()
                fault = vmodl.fault.SystemError(reason=repr(exc), msg=f"A general system error occurred: {exc!r}")
                body, status = _encode_fault(fault, version), 500
        extra = {"Set-Cookie": set_cookie} if set_cookie else {}
        return self._send(status, body.encode("utf-8"), "text/xml; charset=utf-8", extra)

    def _rest(self, method, url):
        raw = self._body()
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return self._send(400, b'{"error_type":"INVALID_ARGUMENT"}', "application/json")
        with _lock:
            status, payload = _rest(self, method, url.path, parse_qs(url.query), body)
        data = b"" if payload is None else json.dumps(payload).encode()
        return self._send(status, data, "application/json")


def make_self_signed_cert(directory: str, hostname: str = "localhost") -> Tuple[str, str, str]:
    """A private CA plus a server cert it signed, like a vCenter VMCA. Returns (ca_pem, cert, key)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def name(cn):
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.ORGANIZATION_NAME, "VMware")])
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    start = dt.datetime.now(UTC) - dt.timedelta(days=1)
    ca = (x509.CertificateBuilder().subject_name(name("CA (mock VMCA)")).issuer_name(name("CA (mock VMCA)"))
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(start).not_valid_after(start + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                       key_encipherment=False, data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .sign(ca_key, hashes.SHA256()))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    sans = [x509.DNSName(hostname), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    cert = (x509.CertificateBuilder().subject_name(name(hostname)).issuer_name(ca.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(start + dt.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    ca_pem = ca.public_bytes(serialization.Encoding.PEM).decode()
    cert_path = os.path.join(directory, "rui.crt")
    key_path = os.path.join(directory, "rui.key")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                  serialization.NoEncryption()))
    with open(os.path.join(directory, "ca.pem"), "w") as f:
        f.write(ca_pem)
    return ca_pem, cert_path, key_path


def serve(host: str = "127.0.0.1", port: int = 8989, tls: bool = True, cert_dir: Optional[str] = None):
    """Start the server in a background thread. Returns (server, ca_pem or None)."""
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    ca_pem = None
    if tls:
        cert_dir = cert_dir or tempfile.mkdtemp(prefix="vcenter-mock-tls-")
        os.makedirs(cert_dir, exist_ok=True)
        paths = [os.path.join(cert_dir, n) for n in ("ca.pem", "rui.crt", "rui.key")]
        if all(os.path.exists(p) for p in paths):
            # keep the certificate across restarts, as a real vCenter does, so a
            # saved connection's CA certificate stays valid
            with open(paths[0]) as f:
                ca_pem = f.read()
            cert, key = paths[1], paths[2]
        else:
            ca_pem, cert, key = make_self_signed_cert(cert_dir)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, ca_pem


if __name__ == "__main__":
    port = int(os.environ.get("VCENTER_MOCK_PORT", "8989"))
    tls = os.environ.get("VCENTER_MOCK_TLS", "1") != "0"
    cert_dir = os.environ.get("VCENTER_MOCK_CERT_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), ".certs"))
    srv, ca = serve(os.environ.get("VCENTER_MOCK_HOST", "0.0.0.0"), port, tls, cert_dir)
    scheme = "https" if tls else "http"
    print(f"mock vCenter ({MODE}, API {API_VERSION}) on {scheme}://localhost:{port}/sdk  user={USER}", flush=True)
    if ca:
        print(f"CA certificate (paste into the connection's CA field): {os.path.join(cert_dir, 'ca.pem')}", flush=True)
    print(f"incident window {INCIDENT_START.isoformat()} .. {INCIDENT_END.isoformat()}  "
          f"({sum(1 for o in OBJECTS.values() if o.cls is vim.VirtualMachine)} VMs, {len(EVENTS)} events, {len(TASKS)} tasks)", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
