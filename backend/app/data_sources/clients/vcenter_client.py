"""VMware vCenter Server / ESXi data source client (vSphere 7.0 – 9.x).

Talks to the vSphere Web Services API (SOAP `vim25` at `https://<vc>/sdk`)
through pyVmomi, the official Python binding, plus the vSphere Automation REST
API (`/api/cis/tagging`) for tags, which the SOAP API does not expose.

Why SOAP and not the REST `/api/vcenter/*` lists: those list calls have no
paging and fail past 4000 VMs / 2500 hosts, return thin summaries, and expose
no performance data, events, tasks or alarms. The SOAP PropertyCollector
returns exactly the properties asked for, pages with `maxObjects` + `token`,
and is the only way to reach PerformanceManager / EventManager / TaskManager.

There is no query language: queries are JSON specs (see `system_prompt`) over
a fixed catalog of virtual tables (`_CATALOG`), modelled on the RVTools tabs
operators already know (vInfo, vDisk, vNetwork, vSnapshot, vHost, ...), plus
time series (`metrics`) and the change timeline (`events`, `tasks`, `alarms`).

Operational rules this client follows on purpose:
  - One login per operation and an explicit Logout afterwards. vCenter caps
    sessions per user (550 vAPI, about 3000 total), and client objects here have
    no close hook, so a cached session would leak until vpxd's idle timeout.
  - Inventory reads use ContainerView + RetrievePropertiesEx with explicit
    property paths (never whole managed objects), paged with maxObjects, and
    the view is destroyed afterwards.
  - Perf queries respect vpxd.stats.maxQueryMetrics (256 metrics per call on
    historical intervals): requests are batched and halved on a size fault.
  - Realtime (20 s) samples exist only for VMs and hosts and only for about an
    hour. Historical intervals usually run at statistics level 1 (aggregate,
    level-1 counters only); `metrics_available` shows what exists.
  - Percent counters arrive in hundredths of a percent; `metrics.value` is
    already divided by 100 for them.
  - Standalone ESXi (apiType HostAgent) has no clusters, tags or historical
    perf; those tables come back empty with a clear note instead of failing.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import ssl
import tempfile
import threading
from collections import deque
from contextlib import contextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

import pandas as pd
import requests

from app.ai.prompt_formatters import ForeignKey, ServiceFormatter, Table, TableColumn
from app.data_sources.clients.base import DataSourceClient
from app.data_sources.clients.progress import IndexingCancelled, ProgressCallback, discovery_progress

UTC = dt.timezone.utc
GB = 1024 ** 3
MAX_ROWS = 200_000            # hard cap on rows returned by one execute_query
DEFAULT_LIMIT = MAX_ROWS      # inventory is complete unless the caller asks for a sample
PAGE_SIZE = 1000              # RetrievePropertiesEx maxObjects
COLLECTOR_PAGE = 1000         # ReadNextEvents / ReadNextTasks maxCount (server cap)
MAX_EVENT_SCAN = 200_000      # events/tasks scanned per query before giving up
MAX_QUERY_METRICS = 256       # vpxd.stats.maxQueryMetrics default (vSphere 6.0+)
MAX_METRIC_ENTITIES = 500     # entities per metrics query without an explicit scope
REALTIME_INTERVAL = 20
REALTIME_WINDOW_S = 3600
NAME_BATCH = 500

# ── catalog ───────────────────────────────────────────────────────────────────
#
# (column, dtype, description). Every table also accepts the common scope
# filters described in system_prompt().

_C = Tuple[str, str, str]

_VM_COLS: List[_C] = [
    ("moid", "str", "Managed object id, e.g. vm-101 (stable while the VM is registered)"),
    ("name", "str", "VM display name in vCenter"),
    ("power_state", "str", "poweredOn | poweredOff | suspended"),
    ("connection_state", "str", "connected | disconnected | orphaned | inaccessible | invalid"),
    ("template", "bool", "True for templates"),
    ("guest_os", "str", "Configured guest OS (config.guestFullName)"),
    ("guest_id", "str", "Guest OS identifier, e.g. rhel9_64Guest"),
    ("num_cpu", "int", "vCPUs"),
    ("cores_per_socket", "int", "Cores per virtual socket"),
    ("memory_mb", "int", "Configured memory (MB)"),
    ("host", "str", "ESXi host currently running the VM"),
    ("cluster", "str", "Cluster of that host (empty for standalone hosts)"),
    ("datacenter", "str", "Datacenter"),
    ("resource_pool", "str", "Resource pool"),
    ("folder", "str", "VM folder"),
    ("datastores", "str", "Comma-separated datastores holding the VM's files"),
    ("networks", "str", "Comma-separated networks / port groups"),
    ("ip_address", "str", "Primary guest IP reported by VMware Tools"),
    ("hostname", "str", "Guest hostname reported by VMware Tools"),
    ("tools_status", "str", "VMware Tools running status, e.g. guestToolsRunning"),
    ("tools_version_status", "str", "VMware Tools version status, e.g. guestToolsCurrent / guestToolsNeedUpgrade"),
    ("hw_version", "str", "Virtual hardware version, e.g. vmx-21"),
    ("overall_status", "str", "vCenter health colour: green | yellow | red | gray"),
    ("cpu_usage_mhz", "int", "Current CPU usage (MHz, quick stats)"),
    ("cpu_max_mhz", "int", "CPU usage ceiling (MHz)"),
    ("guest_memory_usage_mb", "int", "Active guest memory (MB, quick stats)"),
    ("host_memory_usage_mb", "int", "Host memory consumed by the VM (MB, quick stats)"),
    ("uptime_seconds", "int", "Guest uptime in seconds"),
    ("boot_time", "datetime", "Last power-on time (UTC)"),
    ("provisioned_gb", "float", "Provisioned storage (committed + uncommitted, GB)"),
    ("used_gb", "float", "Storage actually used on datastores, including snapshots (GB)"),
    ("snapshot_count", "int", "Number of snapshots"),
    ("snapshot_size_gb", "float", "Disk space held by snapshot delta disks and snapshot files (GB)"),
    ("oldest_snapshot_days", "float", "Age of the oldest snapshot in days"),
    ("consolidation_needed", "bool", "True when the VM's disks need consolidation (failed snapshot removal)"),
    ("created", "datetime", "VM creation time (UTC), when vCenter knows it"),
    ("instance_uuid", "str", "vCenter instance UUID (join key for CMDBs)"),
    ("bios_uuid", "str", "BIOS UUID (as seen in the guest)"),
    ("vm_path", "str", "Path of the .vmx file, e.g. [ds-prod-01] prod-db-01/prod-db-01.vmx"),
    ("annotation", "str", "Notes field"),
]

_CATALOG: Dict[str, dict] = {
    "about": {
        "pk": "instance_uuid", "fks": [],
        "desc": "The endpoint itself: product, version, build, API version, apiType (VirtualCenter for vCenter, HostAgent for standalone ESXi).",
        "columns": [
            ("name", "str", "Product name"), ("full_name", "str", "Product name with version and build"),
            ("version", "str", "Product version"), ("build", "str", "Build number"),
            ("api_type", "str", "VirtualCenter or HostAgent"), ("api_version", "str", "Negotiated vim25 API version"),
            ("instance_uuid", "str", "vCenter instance UUID"), ("os_type", "str", "Appliance OS type"),
            ("vendor", "str", "Vendor"),
        ],
    },
    "datacenters": {
        "pk": "moid", "fks": [],
        "desc": "Datacenters with object counts.",
        "columns": [
            ("moid", "str", "Managed object id"), ("name", "str", "Datacenter name"),
            ("overall_status", "str", "Health colour"), ("cluster_count", "int", "Clusters"),
            ("host_count", "int", "ESXi hosts"), ("vm_count", "int", "VMs and templates"),
            ("datastore_count", "int", "Datastores"), ("network_count", "int", "Networks / port groups"),
        ],
    },
    "clusters": {
        "pk": "moid", "fks": [("datacenter", "datacenters", "name")],
        "desc": "Compute clusters: capacity, demand, HA/DRS/vSAN configuration. Effective values exclude hosts in maintenance mode or disconnected.",
        "columns": [
            ("moid", "str", "Managed object id, e.g. domain-c8"), ("name", "str", "Cluster name"),
            ("datacenter", "str", "Datacenter"), ("overall_status", "str", "Health colour"),
            ("num_hosts", "int", "Hosts in the cluster"), ("num_effective_hosts", "int", "Hosts contributing capacity"),
            ("vm_count", "int", "VMs on the cluster's hosts"),
            ("cpu_cores", "int", "Physical CPU cores"), ("total_cpu_mhz", "int", "Total CPU capacity (MHz)"),
            ("effective_cpu_mhz", "int", "Effective CPU capacity (MHz)"), ("cpu_demand_mhz", "int", "CPU demand (MHz)"),
            ("cpu_demand_pct", "float", "CPU demand as % of effective capacity"),
            ("total_memory_gb", "float", "Total memory (GB)"), ("effective_memory_gb", "float", "Effective memory (GB)"),
            ("memory_demand_gb", "float", "Memory demand (GB)"), ("memory_demand_pct", "float", "Memory demand as % of effective capacity"),
            ("ha_enabled", "bool", "vSphere HA enabled"), ("ha_admission_control", "bool", "HA admission control enabled"),
            ("drs_enabled", "bool", "DRS enabled"), ("drs_behavior", "str", "DRS automation level"),
            ("vsan_enabled", "bool", "vSAN enabled"), ("num_vmotions", "int", "vMotions performed in this cluster"),
        ],
    },
    "hosts": {
        "pk": "moid", "fks": [("cluster", "clusters", "name"), ("datacenter", "datacenters", "name")],
        "desc": "ESXi hosts: hardware, ESXi version, state (maintenance mode, connection), current CPU / memory usage.",
        "columns": [
            ("moid", "str", "Managed object id, e.g. host-10"), ("name", "str", "Host name (FQDN or IP as added)"),
            ("cluster", "str", "Cluster (empty for standalone hosts)"), ("datacenter", "str", "Datacenter"),
            ("connection_state", "str", "connected | disconnected | notResponding"),
            ("power_state", "str", "poweredOn | poweredOff | standBy | unknown"),
            ("in_maintenance_mode", "bool", "True while in maintenance mode"), ("overall_status", "str", "Health colour"),
            ("vendor", "str", "Hardware vendor"), ("model", "str", "Hardware model"), ("serial_number", "str", "Service tag / serial"),
            ("cpu_model", "str", "CPU model"), ("cpu_sockets", "int", "CPU packages"), ("cpu_cores", "int", "Physical cores"),
            ("cpu_threads", "int", "Hardware threads"), ("cpu_mhz", "int", "Core clock (MHz)"),
            ("memory_gb", "float", "Physical memory (GB)"),
            ("cpu_usage_mhz", "int", "Current CPU usage (MHz)"), ("cpu_usage_pct", "float", "Current CPU usage (% of capacity)"),
            ("memory_usage_gb", "float", "Current memory usage (GB)"), ("memory_usage_pct", "float", "Current memory usage (% of physical)"),
            ("esxi_version", "str", "ESXi version"), ("esxi_build", "str", "ESXi build"),
            ("uptime_seconds", "int", "Uptime"), ("boot_time", "datetime", "Boot time (UTC)"),
            ("vm_count", "int", "VMs registered on the host"), ("datastore_count", "int", "Mounted datastores"),
            ("nic_count", "int", "Physical NICs"), ("hba_count", "int", "Storage HBAs"),
            ("reboot_required", "bool", "Host needs a reboot"),
        ],
    },
    "vms": {
        "pk": "moid", "fks": [("host", "hosts", "name"), ("cluster", "clusters", "name"),
                              ("datacenter", "datacenters", "name")],
        "desc": "Virtual machines and templates (RVTools vInfo): configuration, placement, guest info, quick stats, storage use and snapshot burden.",
        "columns": _VM_COLS,
    },
    "vm_disks": {
        "pk": None, "fks": [("vm_moid", "vms", "moid"), ("datastore", "datastores", "name")],
        "desc": "Virtual disks (RVTools vDisk), one row per disk. `on_snapshot_delta` = the VM is writing to a snapshot delta, not the base disk.",
        "columns": [
            ("vm", "str", "VM name"), ("vm_moid", "str", "VM managed object id"), ("label", "str", "Device label, e.g. Hard disk 1"),
            ("capacity_gb", "float", "Provisioned size (GB)"), ("datastore", "str", "Datastore of the current backing file"),
            ("file_name", "str", "Current backing file"), ("thin_provisioned", "bool", "Thin provisioned"),
            ("disk_mode", "str", "persistent | independent_persistent | ..."), ("backing_type", "str", "Flat / RDM / SeSparse ..."),
            ("controller_key", "int", "Controller device key"), ("unit_number", "int", "Unit number on the controller"),
            ("on_snapshot_delta", "bool", "Current backing is a snapshot delta (-00000N.vmdk)"), ("disk_uuid", "str", "Disk UUID"),
        ],
    },
    "vm_nics": {
        "pk": None, "fks": [("vm_moid", "vms", "moid"), ("network", "networks", "name")],
        "desc": "Virtual network adapters (RVTools vNetwork), with the guest IPs on each adapter.",
        "columns": [
            ("vm", "str", "VM name"), ("vm_moid", "str", "VM managed object id"), ("label", "str", "Device label"),
            ("adapter_type", "str", "Vmxnet3 / E1000e / ..."), ("mac_address", "str", "MAC address"),
            ("network", "str", "Network or distributed port group"), ("connected", "bool", "Currently connected"),
            ("start_connected", "bool", "Connect at power on"), ("ip_addresses", "str", "Comma-separated guest IPs on this adapter"),
        ],
    },
    "vm_partitions": {
        "pk": None, "fks": [("vm_moid", "vms", "moid")],
        "desc": "Guest file systems reported by VMware Tools (RVTools vPartition): capacity and free space inside the guest.",
        "columns": [
            ("vm", "str", "VM name"), ("vm_moid", "str", "VM managed object id"), ("path", "str", "Mount point / drive"),
            ("filesystem", "str", "File system type"), ("capacity_gb", "float", "Capacity (GB)"),
            ("free_gb", "float", "Free space (GB)"), ("free_pct", "float", "Free space (%)"),
        ],
    },
    "vm_snapshots": {
        "pk": None, "fks": [("vm_moid", "vms", "moid")],
        "desc": "VM snapshots (RVTools vSnapshot), one row per snapshot in the tree. Old snapshots grow delta disks and slow I/O.",
        "columns": [
            ("vm", "str", "VM name"), ("vm_moid", "str", "VM managed object id"), ("snapshot", "str", "Snapshot name"),
            ("description", "str", "Snapshot description"), ("created", "datetime", "Creation time (UTC)"),
            ("age_days", "float", "Age in days"), ("state", "str", "VM power state captured"), ("quiesced", "bool", "Guest quiesced"),
            ("snapshot_id", "int", "Snapshot id within the VM"), ("parent_snapshot", "str", "Parent snapshot name"),
            ("depth", "int", "Depth in the chain (1 = root)"), ("is_current", "bool", "The VM is running from this snapshot"),
        ],
    },
    "datastores": {
        "pk": "moid", "fks": [("datacenter", "datacenters", "name")],
        "desc": "Datastores (RVTools vDatastore): capacity, free space, thin-provisioning overcommit, type and backing device.",
        "columns": [
            ("moid", "str", "Managed object id, e.g. datastore-40"), ("name", "str", "Datastore name"),
            ("datacenter", "str", "Datacenter"), ("type", "str", "VMFS | NFS | NFS41 | vsan | vvol"),
            ("capacity_gb", "float", "Capacity (GB)"), ("free_gb", "float", "Free space (GB)"), ("used_gb", "float", "Used (GB)"),
            ("used_pct", "float", "Used (% of capacity)"), ("uncommitted_gb", "float", "Thin-provisioned space not yet written (GB)"),
            ("provisioned_gb", "float", "Used + uncommitted (GB)"), ("provisioned_pct", "float", "Provisioned as % of capacity (>100 = overcommitted)"),
            ("accessible", "bool", "Accessible"), ("maintenance_mode", "str", "normal | enteringMaintenance | inMaintenance"),
            ("multiple_host_access", "bool", "Shared by more than one host"), ("host_count", "int", "Hosts mounting it"),
            ("vm_count", "int", "VMs with files on it"), ("overall_status", "str", "Health colour"), ("url", "str", "Datastore URL"),
            ("vmfs_version", "str", "VMFS version"), ("ssd", "bool", "Backed by flash (VMFS)"),
            ("device", "str", "Backing LUN (naa id) or NFS server:path"),
        ],
    },
    "networks": {
        "pk": "moid", "fks": [("datacenter", "datacenters", "name")],
        "desc": "Networks: standard port groups and distributed port groups (with VLAN and switch).",
        "columns": [
            ("moid", "str", "Managed object id"), ("name", "str", "Network / port group name"),
            ("type", "str", "Network | DistributedVirtualPortgroup | OpaqueNetwork"), ("datacenter", "str", "Datacenter"),
            ("switch", "str", "Distributed switch (for distributed port groups)"), ("vlan_id", "int", "VLAN id (distributed port groups)"),
            ("num_ports", "int", "Ports (distributed port groups)"), ("is_uplink", "bool", "Uplink port group"),
            ("host_count", "int", "Hosts attached"), ("vm_count", "int", "VMs attached"), ("accessible", "bool", "Accessible"),
        ],
    },
    "resource_pools": {
        "pk": "moid", "fks": [("cluster", "clusters", "name")],
        "desc": "Resource pools (RVTools vRP): reservations, limits, shares. A limit (-1 = unlimited) can throttle every VM in the pool.",
        "columns": [
            ("moid", "str", "Managed object id"), ("name", "str", "Pool name ('Resources' is the root pool)"),
            ("cluster", "str", "Owning cluster / compute resource"), ("datacenter", "str", "Datacenter"),
            ("parent_pool", "str", "Parent pool"), ("is_root", "bool", "Root pool of the cluster"),
            ("cpu_reservation_mhz", "int", "CPU reservation (MHz)"), ("cpu_limit_mhz", "int", "CPU limit (MHz, -1 = none)"),
            ("cpu_shares_level", "str", "low | normal | high | custom"), ("cpu_shares", "int", "CPU shares"),
            ("cpu_expandable", "bool", "Expandable CPU reservation"),
            ("memory_reservation_mb", "int", "Memory reservation (MB)"), ("memory_limit_mb", "int", "Memory limit (MB, -1 = none)"),
            ("memory_shares_level", "str", "Memory shares level"), ("memory_expandable", "bool", "Expandable memory reservation"),
            ("vm_count", "int", "VMs directly in the pool"), ("child_pool_count", "int", "Child pools"),
            ("overall_status", "str", "Health colour"),
        ],
    },
    "tags": {
        "pk": None, "fks": [],
        "desc": "vSphere tags attached to objects (Automation REST API). Use it to scope by environment, owner, application or tier.",
        "columns": [
            ("category", "str", "Tag category"), ("tag", "str", "Tag name"), ("cardinality", "str", "SINGLE or MULTIPLE"),
            ("object_type", "str", "VirtualMachine | HostSystem | Datastore | ..."), ("object_moid", "str", "Tagged object's moid"),
            ("object_name", "str", "Tagged object's name"), ("tag_description", "str", "Tag description"),
            ("tag_id", "str", "Tag URN"), ("category_id", "str", "Category URN"),
        ],
    },
    "alarms": {
        "pk": None, "fks": [],
        "desc": "Currently triggered alarms (red / yellow) across the inventory, de-duplicated.",
        "columns": [
            ("alarm", "str", "Alarm name"), ("alarm_description", "str", "Alarm description"),
            ("status", "str", "red | yellow"), ("entity", "str", "Object the alarm is on"),
            ("entity_type", "str", "VirtualMachine | HostSystem | Datastore | ..."), ("entity_moid", "str", "Object moid"),
            ("triggered_time", "datetime", "When it triggered (UTC)"), ("acknowledged", "bool", "Acknowledged"),
            ("acknowledged_by", "str", "Who acknowledged it"), ("acknowledged_time", "datetime", "When (UTC)"),
            ("alarm_moid", "str", "Alarm definition moid"), ("key", "str", "Alarm state key"),
        ],
    },
    "events": {
        "pk": "key", "fks": [],
        "desc": "The vCenter event log: logins, power operations, vMotion/DRS moves, alarm transitions, task results, ESXi problems (esx.problem.*). Newest first.",
        "columns": [
            ("created", "datetime", "Event time (UTC)"), ("event_type", "str", "Event class, or the eventTypeId for extended events (e.g. esx.problem.scsi.device.io.latency.high)"),
            ("category", "str", "info | warning | error | user"), ("message", "str", "Full formatted message"),
            ("user", "str", "User who caused it"), ("datacenter", "str", "Datacenter"), ("cluster", "str", "Cluster / compute resource"),
            ("host", "str", "Host"), ("vm", "str", "VM"), ("datastore", "str", "Datastore"), ("network", "str", "Network"),
            ("key", "int", "Event key"), ("chain_id", "int", "Chain id (groups the events of one task)"),
        ],
    },
    "tasks": {
        "pk": "key", "fks": [],
        "desc": "Recent tasks: who did what to which object, state, error and duration (snapshot create/remove, vMotion, maintenance mode, ...). Newest first.",
        "columns": [
            ("key", "str", "Task key"), ("task", "str", "Operation, e.g. VirtualMachine.removeSnapshot"),
            ("entity", "str", "Target object"), ("entity_type", "str", "Target type"), ("entity_moid", "str", "Target moid"),
            ("state", "str", "queued | running | success | error"), ("error", "str", "Error message for failed tasks"),
            ("user", "str", "Initiating user"), ("queued", "datetime", "Queued (UTC)"), ("started", "datetime", "Started (UTC)"),
            ("completed", "datetime", "Completed (UTC)"), ("duration_seconds", "float", "Run time"),
            ("progress", "int", "Progress % for running tasks"), ("cancelled", "bool", "Cancelled"),
        ],
    },
    "licenses": {
        "pk": None, "fks": [],
        "desc": "Licenses in the vCenter license inventory (keys are masked).",
        "columns": [
            ("name", "str", "License name"), ("edition", "str", "Edition key"), ("license_key", "str", "Masked license key"),
            ("total", "int", "Capacity"), ("used", "int", "Used"), ("cost_unit", "str", "cpuCore | cpuPackage | server | vm"),
            ("expires", "datetime", "Expiration (UTC), empty for perpetual"),
        ],
    },
    "perf_counters": {
        "pk": "counter", "fks": [],
        "desc": "Performance counter catalog: the exact `counter` keys to pass to `metrics`, with unit and statistics level.",
        "columns": [
            ("counter", "str", "group.name.rollup, e.g. cpu.usage.average"), ("counter_id", "int", "Numeric id on this vCenter"),
            ("group", "str", "cpu | mem | disk | net | datastore | virtualDisk | sys | power ..."),
            ("name", "str", "Counter name"), ("rollup", "str", "average | latest | summation | maximum | minimum"),
            ("unit", "str", "percent | megaHertz | millisecond | kiloBytes | kiloBytesPerSecond | number | ..."),
            ("stats_type", "str", "rate | absolute | delta"), ("level", "int", "Statistics level (1 = collected at default level)"),
            ("per_device_level", "int", "Level at which per-instance values are kept"), ("description", "str", "What it measures"),
        ],
    },
    "metrics_available": {
        "pk": None, "fks": [],
        "desc": "Which counters and instances actually have data for an object at a given interval (QueryAvailablePerfMetric). Check before querying `metrics` for anything beyond level 1.",
        "columns": [
            ("entity", "str", "Object name"), ("entity_type", "str", "Object type"), ("moid", "str", "Object moid"),
            ("counter", "str", "Counter key"), ("instance", "str", "Instance ('' = aggregate)"), ("unit", "str", "Unit"),
            ("interval_seconds", "int", "Sampling interval (20 = realtime)"), ("level", "int", "Statistics level"),
        ],
    },
    "metrics": {
        "pk": None, "fks": [],
        "desc": "Performance time series from PerformanceManager, long format (one row per object × counter × instance × timestamp). Percent values are 0-100.",
        "columns": [
            ("timestamp", "datetime", "Sample time (UTC)"), ("entity", "str", "Object name"), ("entity_type", "str", "Object type"),
            ("moid", "str", "Object moid"), ("counter", "str", "Counter key"), ("instance", "str", "Instance ('' = aggregate)"),
            ("value", "float", "Value in `unit` (percent already scaled to 0-100)"), ("unit", "str", "Unit"),
            ("interval_seconds", "int", "Sampling interval"),
        ],
    },
}

_ENTITY_TYPES = {
    "vm": "VirtualMachine", "vms": "VirtualMachine", "virtualmachine": "VirtualMachine",
    "host": "HostSystem", "hosts": "HostSystem", "hostsystem": "HostSystem", "esxi": "HostSystem",
    "cluster": "ClusterComputeResource", "clusters": "ClusterComputeResource",
    "clustercomputeresource": "ClusterComputeResource",
    "datastore": "Datastore", "datastores": "Datastore",
    "resourcepool": "ResourcePool", "resource_pool": "ResourcePool", "pool": "ResourcePool",
    "datacenter": "Datacenter",
}
_SCOPE = {"name", "names", "name_regex", "moid", "moids", "datacenter", "cluster", "host", "columns", "limit"}
_WINDOW = {"minutes", "hours", "start_time", "end_time"}
_ALLOWED: Dict[str, set] = {
    "about": {"columns"},
    "datacenters": _SCOPE, "clusters": _SCOPE, "hosts": _SCOPE, "datastores": _SCOPE, "networks": _SCOPE,
    "resource_pools": _SCOPE, "licenses": {"columns", "limit"},
    "vms": _SCOPE | {"power_state", "include_templates"},
    "vm_disks": _SCOPE | {"vm", "power_state"}, "vm_nics": _SCOPE | {"vm", "power_state"},
    "vm_partitions": _SCOPE | {"vm"}, "vm_snapshots": _SCOPE | {"vm", "older_than_days"},
    "tags": {"category", "tag", "object_type", "object_name", "columns", "limit"},
    "alarms": {"status", "entity", "entity_type", "columns", "limit"},
    "events": _WINDOW | {"event_type", "category", "user", "entity", "entity_type", "message_contains", "columns", "limit"},
    "tasks": _WINDOW | {"state", "user", "entity", "entity_type", "task_contains", "columns", "limit"},
    "perf_counters": {"search", "group", "level", "columns", "limit"},
    "metrics_available": {"entity_type", "name", "names", "name_regex", "moid", "moids", "interval", "counter_contains", "columns", "limit"},
    "metrics": _WINDOW | {"entity_type", "name", "names", "name_regex", "moid", "moids", "counters", "counter",
                          "instance", "interval", "rollup_to", "columns", "limit"},
}


def _gb(v) -> Optional[float]:
    return None if v is None else round(v / GB, 2)


def _pct(part, whole) -> Optional[float]:
    if part is None or not whole:
        return None
    return round(100.0 * part / whole, 1)


def _ts(v) -> Optional[pd.Timestamp]:
    if v is None:
        return None
    t = pd.Timestamp(v)
    return t.tz_localize(UTC) if t.tzinfo is None else t.tz_convert(UTC)


def _parse_time(v, field: str) -> dt.datetime:
    """ISO-8601 string or epoch seconds / milliseconds -> aware UTC datetime."""
    if isinstance(v, bool):
        raise ValueError(f"{field} must be an ISO-8601 string or an epoch number.")
    if isinstance(v, (int, float)):
        secs = v / 1000.0 if v > 1e11 else float(v)
        return dt.datetime.fromtimestamp(secs, UTC)
    if isinstance(v, str):
        try:
            t = pd.Timestamp(v)
        except (ValueError, TypeError):
            raise ValueError(f"{field} is not a valid ISO-8601 time: {v!r}")
        t = t.tz_localize(UTC) if t.tzinfo is None else t.tz_convert(UTC)
        return t.to_pydatetime()
    raise ValueError(f"{field} must be an ISO-8601 string or an epoch number.")


def _plain(v):
    """pyVmomi hands back enum values as str subclasses (vim.TaskInfo.State, ...) and
    numbers as int subclasses. They cannot be pickled (lazy types), and BOW pickles
    query results out of its code-execution sandbox, so every cell must be a builtin."""
    if v is None or type(v) in (str, int, float, bool):
        return v
    if isinstance(v, bool):
        return bool(v)
    if isinstance(v, str):
        return str(v)
    if isinstance(v, int):
        return int(v)
    if isinstance(v, float):
        return float(v)
    return v


def _as_list(v) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v if x is not None and str(x) != ""]
    return [str(v)] if str(v) != "" else []


class VCenterClient(DataSourceClient):

    def __init__(
        self,
        host: str,
        port: int = 443,
        username: Optional[str] = None,
        password: Optional[str] = None,
        verify_ssl: bool = True,
        ca_certificate: Optional[str] = None,
        timeout: int = 60,
        include_tags: bool = True,
        default_lookback_hours: int = 24,
        **_ignored,
    ):
        raw = (host or "").strip()
        if not raw:
            raise ValueError("vCenter host is required.")
        if "://" not in raw:
            raw = "https://" + raw
        parts = urlsplit(raw)
        if parts.scheme not in ("https", ""):
            raise ValueError("vCenter is reached over HTTPS only; enter the host name or an https:// URL.")
        self.host = parts.hostname or ""
        if not self.host:
            raise ValueError(f"Could not read a host name from {host!r}.")
        self.port = int(parts.port or port or 443)
        self.username = username or None
        self.password = password or None
        self.verify_ssl = bool(verify_ssl)
        self.ca_certificate = (ca_certificate or "").strip() or None
        self.timeout = int(timeout or 60)
        self.include_tags = bool(include_tags)
        self.default_lookback_hours = max(1, int(default_lookback_hours or 24))
        self._local = threading.local()
        self._ca_dir: Optional[tempfile.TemporaryDirectory] = None
        self._counters: Optional[Dict[str, Any]] = None     # counter key -> CounterInfo
        self._counters_by_id: Dict[int, Any] = {}

    # ── connection plumbing ───────────────────────────────────────────────────

    def _ssl_context(self) -> ssl.SSLContext:
        if not self.verify_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        if self.ca_certificate:
            try:
                return ssl.create_default_context(cadata=self.ca_certificate)
            except ssl.SSLError as e:
                raise RuntimeError(f"The CA certificate is not a valid PEM certificate: {e}")
        return ssl.create_default_context()

    def _requests_verify(self):
        if not self.verify_ssl:
            return False
        if self.ca_certificate:
            if self._ca_dir is None:
                self._ca_dir = tempfile.TemporaryDirectory(prefix="bow-vcenter-ca-")
                with open(os.path.join(self._ca_dir.name, "ca.pem"), "w") as f:
                    f.write(self.ca_certificate)
            return os.path.join(self._ca_dir.name, "ca.pem")
        return True

    def _login(self):
        from pyVim.connect import SmartConnect
        from pyVmomi import vim

        if not (self.username and self.password):
            raise RuntimeError("vCenter requires a username and password (e.g. svc-bow@vsphere.local with the Read-only role).")
        where = f"{self.host}:{self.port}"
        try:
            return SmartConnect(host=self.host, port=self.port, user=self.username, pwd=self.password,
                                sslContext=self._ssl_context(), httpConnectionTimeout=self.timeout,
                                connectionPoolTimeout=-1)
        except vim.fault.InvalidLogin:
            raise RuntimeError(
                f"vCenter rejected the login for '{self.username}' on {where}. Check the password; SSO users "
                "need their domain, e.g. user@vsphere.local or user@corp.example.com.")
        except ssl.SSLCertVerificationError as e:
            raise RuntimeError(
                f"TLS verification failed for {where}: {e.verify_message or e}. vCenter usually presents a "
                "certificate signed by its own VMCA: paste the VMCA root certificate (download it from "
                f"https://{self.host}/certs/download.zip) into 'CA certificate', or turn off 'Verify SSL' "
                "on a trusted network.")
        except ssl.SSLError as e:
            raise RuntimeError(f"TLS error talking to {where}: {e}")
        except (ConnectionRefusedError, socket.gaierror, socket.timeout, TimeoutError, OSError) as e:
            raise RuntimeError(f"Could not reach vCenter at {where}: {e}")
        except vim.fault.NoPermission as e:
            raise RuntimeError(f"vCenter refused the session for '{self.username}': {e.msg}")

    @contextmanager
    def _si(self):
        """One authenticated ServiceInstance per operation; nested calls share it.

        Always logs out on exit so every query leaves zero sessions behind.
        """
        current = getattr(self._local, "si", None)
        if current is not None:
            yield current
            return
        si = self._login()
        self._local.si = si
        self._local.cache = {}
        try:
            yield si
        finally:
            self._local.si = None
            self._local.cache = {}
            try:
                from pyVim.connect import Disconnect
                Disconnect(si)
            except Exception:
                pass

    def _cache(self) -> dict:
        c = getattr(self._local, "cache", None)
        if c is None:
            c = self._local.cache = {}
        return c

    def _content(self, si):
        # si.RetrieveContent() is a SOAP round trip every time; fetch it once per session.
        cache = self._cache()
        if cache.get("content_for") is not si:
            cache["content"] = si.RetrieveContent()
            cache["content_for"] = si
        return cache["content"]

    def _is_esxi(self, si) -> bool:
        return getattr(self._content(si).about, "apiType", "") == "HostAgent"

    # ── PropertyCollector helpers ─────────────────────────────────────────────

    def _retrieve(self, si, types: Sequence, paths_by_type: Dict[Any, List[str]]) -> List[Tuple[Any, Dict[str, Any]]]:
        """ContainerView over the whole inventory + paged RetrievePropertiesEx."""
        from pyVmomi import vim, vmodl
        PC = vmodl.query.PropertyCollector
        content = self._content(si)
        view = content.viewManager.CreateContainerView(content.rootFolder, list(types), True)
        try:
            spec = PC.FilterSpec(
                objectSet=[PC.ObjectSpec(obj=view, skip=True, selectSet=[
                    PC.TraversalSpec(name="traverseView", path="view", skip=False, type=vim.view.ContainerView)])],
                propSet=[PC.PropertySpec(type=t, pathSet=list(p)) for t, p in paths_by_type.items()])
            return self._paged(content.propertyCollector, spec)
        finally:
            try:
                view.DestroyView()
            except Exception:
                pass

    def _retrieve_objects(self, si, objs: Sequence, paths_by_type: Dict[Any, List[str]]) -> List[Tuple[Any, Dict[str, Any]]]:
        """Properties of an explicit list of objects (no traversal)."""
        from pyVmomi import vmodl
        PC = vmodl.query.PropertyCollector
        objs = [o for o in objs if o is not None]
        if not objs:
            return []
        out = []
        content = self._content(si)
        for i in range(0, len(objs), NAME_BATCH):
            chunk = objs[i:i + NAME_BATCH]
            spec = PC.FilterSpec(objectSet=[PC.ObjectSpec(obj=o, skip=False) for o in chunk],
                                 propSet=[PC.PropertySpec(type=t, pathSet=list(p)) for t, p in paths_by_type.items()])
            out.extend(self._paged(content.propertyCollector, spec))
        return out

    @staticmethod
    def _paged(pc, spec) -> List[Tuple[Any, Dict[str, Any]]]:
        from pyVmomi import vmodl
        rows: List[Tuple[Any, Dict[str, Any]]] = []
        result = pc.RetrievePropertiesEx([spec], vmodl.query.PropertyCollector.RetrieveOptions(maxObjects=PAGE_SIZE))
        while result is not None:
            for oc in result.objects or []:
                rows.append((oc.obj, {p.name: p.val for p in (oc.propSet or [])}))
            token = result.token
            result = pc.ContinueRetrievePropertiesEx(token) if token else None
        return rows

    # ── topology (names, parents, datacenter / cluster of anything) ──────────

    def _topology(self, si) -> dict:
        cache = self._cache()
        if "topology" in cache:
            return cache["topology"]
        from pyVmomi import vim
        rows = self._retrieve(si, [vim.Folder, vim.Datacenter, vim.ComputeResource, vim.ResourcePool, vim.HostSystem],
                              {vim.ManagedEntity: ["name", "parent"]})
        names: Dict[str, str] = {}
        kinds: Dict[str, str] = {}
        parents: Dict[str, Optional[str]] = {}
        for obj, props in rows:
            moid = obj._moId
            names[moid] = props.get("name")
            kinds[moid] = type(obj).__name__.rsplit(".", 1)[-1]
            p = props.get("parent")
            parents[moid] = p._moId if p is not None else None
        topo = {"names": names, "kinds": kinds, "parents": parents}
        cache["topology"] = topo
        return topo

    @staticmethod
    def _up(topo: dict, moid: Optional[str], kind: str) -> Optional[str]:
        seen = 0
        while moid and seen < 64:
            if topo["kinds"].get(moid) == kind:
                return topo["names"].get(moid)
            moid = topo["parents"].get(moid)
            seen += 1
        return None

    def _datacenter_of(self, topo, moid):
        return self._up(topo, moid, "Datacenter")

    def _cluster_of_host(self, topo, host_moid):
        parent = topo["parents"].get(host_moid)
        return topo["names"].get(parent) if topo["kinds"].get(parent) == "ClusterComputeResource" else None

    def _name(self, topo, ref) -> Optional[str]:
        if ref is None:
            return None
        return topo["names"].get(ref._moId) or self._cache().get("names", {}).get(ref._moId)

    def _names_for(self, si, refs: Iterable) -> Dict[str, Tuple[str, str]]:
        """moid -> (name, type) for arbitrary managed entities, batched."""
        from pyVmomi import vim
        cache = self._cache().setdefault("names", {})
        kinds = self._cache().setdefault("kinds", {})
        todo, seen = [], set()
        for r in refs:
            if r is None or r._moId in cache or r._moId in seen:
                continue
            seen.add(r._moId)
            todo.append(r)
        if todo:
            for obj, props in self._retrieve_objects(si, todo, {vim.ManagedEntity: ["name"]}):
                cache[obj._moId] = props.get("name")
                kinds[obj._moId] = type(obj).__name__.rsplit(".", 1)[-1]
        out = {}
        for r in refs:
            if r is not None:
                out[r._moId] = (cache.get(r._moId), kinds.get(r._moId) or type(r).__name__.rsplit(".", 1)[-1])
        return out

    # ── test / schemas ────────────────────────────────────────────────────────

    def test_connection(self):
        try:
            from pyVmomi import vim
            with self._si() as si:
                content = self._content(si)
                about = content.about
                counts = {}
                for label, t in (("datacenters", vim.Datacenter), ("clusters", vim.ClusterComputeResource),
                                 ("hosts", vim.HostSystem), ("VMs", vim.VirtualMachine), ("datastores", vim.Datastore)):
                    counts[label] = len(self._retrieve(si, [t], {t: ["name"]}))
                user = getattr(content.sessionManager.currentSession, "userName", None) or self.username
            estate = ", ".join(f"{n} {k}" for k, n in counts.items())
            msg = f"Connected to {about.fullName} (API {about.apiVersion}) as {user}: {estate}."
            if about.apiType == "HostAgent":
                msg += " This is a standalone ESXi host: no clusters, tags or historical performance data."
            elif self.include_tags:
                try:
                    n = len(self._rest_tag_ids())
                    msg += f" Tags API reachable ({n} tags)."
                except Exception as e:
                    msg += f" Tags API not available ({e}); the tags table will be empty."
            return {"success": True, "message": msg}
        except IndexingCancelled:
            raise
        except Exception as e:
            return {"success": False, "message": str(e)}

    def _build_table(self, name: str) -> Table:
        spec = _CATALOG[name]
        columns = [TableColumn(name=c, dtype=d, description=desc) for c, d, desc in spec["columns"]]
        by_name = {c.name: c for c in columns}
        fks = [ForeignKey(column=by_name[col], references_name=ref_table,
                          references_column=TableColumn(name=ref_col, dtype="str"))
               for col, ref_table, ref_col in spec["fks"] if col in by_name]
        pks = [by_name[spec["pk"]]] if spec.get("pk") and spec["pk"] in by_name else []
        return Table(name=name, description=spec["desc"], columns=columns, pks=pks, fks=fks)

    @discovery_progress
    def get_schemas(self, progress_callback: Optional[ProgressCallback] = None) -> List[Table]:
        tables = [self._build_table(n) for n in _CATALOG]
        # Best-effort estate summary in the descriptions; the catalog never depends on it.
        try:
            from pyVmomi import vim
            by = {t.name: t for t in tables}
            with self._si() as si:
                about = self._content(si).about
                by["about"].description += f" This endpoint: {about.fullName}, API {about.apiVersion}."
                steps = [("vms", vim.VirtualMachine), ("hosts", vim.HostSystem), ("clusters", vim.ClusterComputeResource),
                         ("datastores", vim.Datastore), ("datacenters", vim.Datacenter)]
                for i, (tname, t) in enumerate(steps):
                    if progress_callback:
                        progress_callback("inventory", tname, i, len(steps))
                    rows = self._retrieve(si, [t], {t: ["name"]})
                    names = sorted(p.get("name") or "" for _, p in rows)
                    sample = ", ".join(names[:8]) + (f" (+{len(names) - 8} more)" if len(names) > 8 else "")
                    by[tname].description += f" Currently {len(names)}" + (f": {sample}." if names else ".")
                if about.apiType == "HostAgent":
                    for tname in ("clusters", "tags"):
                        by[tname].description += " Not available on a standalone ESXi host."
                    by["metrics"].description += " Standalone ESXi keeps realtime (20 s, last hour) data only."
        except IndexingCancelled:
            raise
        except Exception:
            pass
        return tables

    def get_schema(self, table_name: str) -> Table:
        if table_name not in _CATALOG:
            raise ValueError(f"Unknown vCenter table '{table_name}'. Available: {', '.join(_CATALOG)}.")
        return self._build_table(table_name)

    def prompt_schema(self):
        return ServiceFormatter(self.get_schemas()).table_str

    # ── query entry point ─────────────────────────────────────────────────────

    def _parse_spec(self, query) -> dict:
        if isinstance(query, dict):
            spec = dict(query)
        else:
            try:
                spec = json.loads(query)
            except (TypeError, json.JSONDecodeError):
                raise ValueError('vCenter query must be a JSON object like {"table": "vms", "power_state": "poweredOn"} '
                                 f"— got: {str(query)[:200]}")
        if not isinstance(spec, dict) or not spec.get("table"):
            raise ValueError('vCenter query spec must include a "table" key.')
        table = str(spec["table"])
        if table not in _CATALOG:
            raise ValueError(f"Unknown vCenter table '{table}'. Available: {', '.join(_CATALOG)}.")
        unknown = set(spec) - _ALLOWED[table] - {"table"}
        if unknown:
            raise ValueError(f"Unsupported fields for {table}: {', '.join(sorted(unknown))}. "
                             f"Allowed: {', '.join(sorted(_ALLOWED[table]))}.")
        if "limit" in spec and (isinstance(spec["limit"], bool) or not isinstance(spec["limit"], int) or spec["limit"] <= 0):
            raise ValueError("limit must be a positive integer.")
        for f in ("minutes", "hours", "older_than_days"):
            if f in spec and (isinstance(spec[f], bool) or not isinstance(spec[f], (int, float)) or spec[f] <= 0):
                raise ValueError(f"{f} must be a positive number.")
        if "columns" in spec:
            cols = _as_list(spec["columns"])
            known = {c for c, _, _ in _CATALOG[table]["columns"]}
            bad = [c for c in cols if c not in known]
            if bad:
                raise ValueError(f"Unknown columns for {table}: {', '.join(bad)}. Known: {', '.join(sorted(known))}.")
        return spec

    def execute_query(self, query=None, **kwargs) -> pd.DataFrame:
        """Run a JSON query spec (string or dict) and return a DataFrame. See system_prompt()."""
        if query is None:
            query = kwargs.get("spec") or kwargs
        spec = self._parse_spec(query)
        table = spec["table"]
        limit = min(int(spec.get("limit") or DEFAULT_LIMIT), MAX_ROWS)
        with self._si() as si:
            df = getattr(self, f"_q_{table}")(si, spec, limit)
        catalog = _CATALOG[table]["columns"]
        cols = [c for c, _, _ in catalog]
        if df is None or df.empty:
            df = pd.DataFrame(columns=cols)
        else:
            df = df.reindex(columns=cols)
            for c in df.columns:
                if df[c].dtype == object:
                    df[c] = df[c].map(_plain)
            for c, dtype, _ in catalog:          # keep ints as ints when some rows are empty
                if dtype == "int" and df[c].dtype.kind in "fO":
                    df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
        if spec.get("columns"):
            df = df[_as_list(spec["columns"])]
        return df.head(limit).reset_index(drop=True)

    # ── scope filters (shared by inventory tables) ────────────────────────────

    @staticmethod
    def _scope(df: pd.DataFrame, spec: dict, name_col: str = "name") -> pd.DataFrame:
        if df.empty:
            return df
        names = _as_list(spec.get("name")) + _as_list(spec.get("names"))
        if names and name_col in df:
            df = df[df[name_col].isin(names)]
        if spec.get("name_regex") and name_col in df:
            try:
                rx = re.compile(str(spec["name_regex"]), re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"name_regex is not a valid regular expression: {e}")
            df = df[df[name_col].fillna("").map(lambda s: bool(rx.search(s)))]
        moids = _as_list(spec.get("moid")) + _as_list(spec.get("moids"))
        if moids:
            col = "moid" if "moid" in df else ("vm_moid" if "vm_moid" in df else None)
            if col:
                df = df[df[col].isin(moids)]
        for f in ("datacenter", "cluster", "host"):
            vals = _as_list(spec.get(f))
            if vals and f in df:
                df = df[df[f].isin(vals)]
        if "vm" in spec and "vm" in df:
            df = df[df["vm"].isin(_as_list(spec["vm"]))]
        if spec.get("power_state") and "power_state" in df:
            df = df[df["power_state"].isin(_as_list(spec["power_state"]))]
        return df

    # ── inventory tables ──────────────────────────────────────────────────────

    def _q_about(self, si, spec, limit):
        a = self._content(si).about
        return pd.DataFrame([{"name": a.name, "full_name": a.fullName, "version": a.version, "build": a.build,
                              "api_type": a.apiType, "api_version": a.apiVersion,
                              "instance_uuid": getattr(a, "instanceUuid", None), "os_type": a.osType, "vendor": a.vendor}])

    _VM_PATHS = [
        "name", "parent", "resourcePool", "datastore", "network", "overallStatus",
        "runtime.host", "runtime.powerState", "runtime.connectionState", "runtime.bootTime",
        "runtime.consolidationNeeded", "runtime.maxCpuUsage",
        "config.template", "config.guestFullName", "config.guestId", "config.hardware.numCPU",
        "config.hardware.numCoresPerSocket", "config.hardware.memoryMB", "config.version", "config.instanceUuid",
        "config.uuid", "config.annotation", "config.createDate", "config.files.vmPathName",
        "guest.ipAddress", "guest.hostName", "guest.toolsRunningStatus", "guest.toolsVersionStatus2",
        "summary.storage.committed", "summary.storage.uncommitted", "summary.quickStats.overallCpuUsage",
        "summary.quickStats.guestMemoryUsage", "summary.quickStats.hostMemoryUsage",
        "summary.quickStats.uptimeSeconds", "snapshot", "layoutEx.file", "layoutEx.disk",
    ]

    def _vm_rows(self, si, paths: List[str]) -> List[Tuple[Any, Dict[str, Any]]]:
        from pyVmomi import vim
        return self._retrieve(si, [vim.VirtualMachine], {vim.VirtualMachine: paths})

    def _placement(self, si, topo, props) -> Dict[str, Any]:
        host = props.get("runtime.host")
        host_moid = host._moId if host is not None else None
        return {"host": topo["names"].get(host_moid) if host_moid else None,
                "cluster": self._cluster_of_host(topo, host_moid) if host_moid else None,
                "datacenter": self._datacenter_of(topo, host_moid) if host_moid else self._datacenter_of(topo, getattr(props.get("parent"), "_moId", None))}

    @staticmethod
    def _walk_snapshots(nodes, current, depth=1, parent=None):
        for n in nodes or []:
            yield n, depth, parent, (current is not None and n.snapshot is not None and n.snapshot._moId == current._moId)
            yield from VCenterClient._walk_snapshots(n.childSnapshotList, current, depth + 1, n.name)

    @staticmethod
    def _snapshot_bytes(files, disks) -> int:
        """Bytes held by snapshots: snapshot files + every delta link of each disk chain (chain[0] is the base)."""
        if not files:
            return 0
        size = {f.key: (f.size or 0) for f in files}
        total = sum((f.size or 0) for f in files if f.type in ("snapshotData", "snapshotMemory", "snapshotList"))
        for d in disks or []:
            for unit in (d.chain or [])[1:]:
                total += sum(size.get(k, 0) for k in (unit.fileKey or []))
        return total

    def _q_vms(self, si, spec, limit):
        topo = self._topology(si)
        net_names = self._network_names(si)
        ds_names = self._datastore_names(si)
        now = dt.datetime.now(UTC)
        rows = []
        for obj, p in self._vm_rows(si, self._VM_PATHS):
            snap = p.get("snapshot")
            snaps = list(self._walk_snapshots(snap.rootSnapshotList, snap.currentSnapshot)) if snap else []
            oldest = min((n.createTime for n, *_ in snaps if n.createTime), default=None)
            committed = p.get("summary.storage.committed")
            uncommitted = p.get("summary.storage.uncommitted")
            rp = p.get("resourcePool")
            row = {
                "moid": obj._moId, "name": p.get("name"), "power_state": p.get("runtime.powerState"),
                "connection_state": p.get("runtime.connectionState"), "template": bool(p.get("config.template")),
                "guest_os": p.get("config.guestFullName"), "guest_id": p.get("config.guestId"),
                "num_cpu": p.get("config.hardware.numCPU"), "cores_per_socket": p.get("config.hardware.numCoresPerSocket"),
                "memory_mb": p.get("config.hardware.memoryMB"),
                "resource_pool": topo["names"].get(rp._moId) if rp is not None else None,
                "folder": self._name(topo, p.get("parent")),
                "datastores": ", ".join(sorted(filter(None, (ds_names.get(d._moId) for d in p.get("datastore") or [])))),
                "networks": ", ".join(sorted(filter(None, (net_names.get(n._moId) for n in p.get("network") or [])))),
                "ip_address": p.get("guest.ipAddress"), "hostname": p.get("guest.hostName"),
                "tools_status": p.get("guest.toolsRunningStatus"), "tools_version_status": p.get("guest.toolsVersionStatus2"),
                "hw_version": p.get("config.version"), "overall_status": p.get("overallStatus"),
                "cpu_usage_mhz": p.get("summary.quickStats.overallCpuUsage"), "cpu_max_mhz": p.get("runtime.maxCpuUsage"),
                "guest_memory_usage_mb": p.get("summary.quickStats.guestMemoryUsage"),
                "host_memory_usage_mb": p.get("summary.quickStats.hostMemoryUsage"),
                "uptime_seconds": p.get("summary.quickStats.uptimeSeconds"), "boot_time": _ts(p.get("runtime.bootTime")),
                "provisioned_gb": _gb((committed or 0) + (uncommitted or 0)) if committed is not None else None,
                "used_gb": _gb(committed), "snapshot_count": len(snaps),
                "snapshot_size_gb": _gb(self._snapshot_bytes(p.get("layoutEx.file"), p.get("layoutEx.disk"))),
                "oldest_snapshot_days": round((now - oldest).total_seconds() / 86400, 1) if oldest else None,
                "consolidation_needed": bool(p.get("runtime.consolidationNeeded")),
                "created": _ts(p.get("config.createDate")), "instance_uuid": p.get("config.instanceUuid"),
                "bios_uuid": p.get("config.uuid"), "vm_path": p.get("config.files.vmPathName"),
                "annotation": p.get("config.annotation") or None,
            }
            row.update(self._placement(si, topo, p))
            rows.append(row)
        df = pd.DataFrame(rows)
        if not df.empty and not spec.get("include_templates", True):
            df = df[~df["template"]]
        return self._scope(df, spec).sort_values("name", kind="stable") if not df.empty else df

    def _datastore_names(self, si) -> Dict[str, str]:
        cache = self._cache()
        if "ds_names" not in cache:
            from pyVmomi import vim
            cache["ds_names"] = {o._moId: p.get("name") for o, p in self._retrieve(si, [vim.Datastore], {vim.Datastore: ["name"]})}
        return cache["ds_names"]

    def _network_names(self, si) -> Dict[str, str]:
        cache = self._cache()
        if "net_names" not in cache:
            from pyVmomi import vim
            rows = self._retrieve(si, [vim.Network], {vim.Network: ["name"]})
            cache["net_names"] = {o._moId: p.get("name") for o, p in rows}
            cache["pg_keys"] = {}
            dvpg = [o for o, _ in rows if type(o).__name__.endswith("DistributedVirtualPortgroup")]
            if dvpg:
                for o, p in self._retrieve_objects(si, dvpg, {vim.dvs.DistributedVirtualPortgroup: ["key"]}):
                    cache["pg_keys"][p.get("key") or o._moId] = cache["net_names"].get(o._moId)
        return cache["net_names"]

    def _vm_parent_rows(self, si, spec, extra: List[str]):
        """VM rows for the per-device tables, with the VM-level scope already applied."""
        topo = self._topology(si)
        rows = []
        for obj, p in self._vm_rows(si, ["name", "runtime.host", "runtime.powerState", "parent"] + extra):
            place = self._placement(si, topo, p)
            rows.append((obj, p, {"vm": p.get("name"), "vm_moid": obj._moId, "power_state": p.get("runtime.powerState"), **place}))
        frame = pd.DataFrame([r[2] for r in rows])
        if frame.empty:
            return []
        keep = set(self._scope(frame.rename(columns={"vm": "name"}), {k: v for k, v in spec.items() if k != "vm"},
                               name_col="name")["vm_moid"])
        if spec.get("vm"):
            keep &= set(frame[frame["vm"].isin(_as_list(spec["vm"]))]["vm_moid"])
        return [r for r in rows if r[2]["vm_moid"] in keep]

    def _q_vm_disks(self, si, spec, limit):
        from pyVmomi import vim
        ds_names = self._datastore_names(si)
        out = []
        for obj, p, base in self._vm_parent_rows(si, spec, ["config.hardware.device"]):
            for d in p.get("config.hardware.device") or []:
                if not isinstance(d, vim.vm.device.VirtualDisk):
                    continue
                b = d.backing
                fname = getattr(b, "fileName", None)
                ds = getattr(b, "datastore", None)
                cap = getattr(d, "capacityInBytes", None) or ((d.capacityInKB or 0) * 1024)
                out.append({"vm": base["vm"], "vm_moid": base["vm_moid"], "label": getattr(d.deviceInfo, "label", None),
                            "capacity_gb": _gb(cap), "datastore": ds_names.get(ds._moId) if ds is not None else None,
                            "file_name": fname, "thin_provisioned": getattr(b, "thinProvisioned", None),
                            "disk_mode": getattr(b, "diskMode", None),
                            "backing_type": type(b).__name__.rsplit(".", 1)[-1].replace("BackingInfo", "") if b else None,
                            "controller_key": d.controllerKey, "unit_number": d.unitNumber,
                            "on_snapshot_delta": bool(fname and re.search(r"-\d{6}\.vmdk$", fname)),
                            "disk_uuid": getattr(b, "uuid", None)})
        return pd.DataFrame(out)

    def _q_vm_nics(self, si, spec, limit):
        from pyVmomi import vim
        net_names = self._network_names(si)
        pg_keys = self._cache().get("pg_keys", {})
        out = []
        for obj, p, base in self._vm_parent_rows(si, spec, ["config.hardware.device", "guest.net"]):
            ips_by_mac = {}
            for n in p.get("guest.net") or []:
                if n.macAddress:
                    ips_by_mac[n.macAddress.lower()] = [ip for ip in (n.ipAddress or [])]
            for d in p.get("config.hardware.device") or []:
                if not isinstance(d, vim.vm.device.VirtualEthernetCard):
                    continue
                b = d.backing
                network = None
                if getattr(b, "network", None) is not None:
                    network = net_names.get(b.network._moId)
                elif getattr(b, "port", None) is not None:
                    network = pg_keys.get(b.port.portgroupKey) or b.port.portgroupKey
                elif getattr(b, "deviceName", None):
                    network = b.deviceName
                conn = d.connectable
                mac = (d.macAddress or "").lower()
                out.append({"vm": base["vm"], "vm_moid": base["vm_moid"], "label": getattr(d.deviceInfo, "label", None),
                            "adapter_type": type(d).__name__.rsplit(".", 1)[-1].replace("Virtual", ""),
                            "mac_address": d.macAddress, "network": network,
                            "connected": getattr(conn, "connected", None), "start_connected": getattr(conn, "startConnected", None),
                            "ip_addresses": ", ".join(ips_by_mac.get(mac, [])) or None})
        return pd.DataFrame(out)

    def _q_vm_partitions(self, si, spec, limit):
        out = []
        for obj, p, base in self._vm_parent_rows(si, spec, ["guest.disk"]):
            for g in p.get("guest.disk") or []:
                out.append({"vm": base["vm"], "vm_moid": base["vm_moid"], "path": g.diskPath,
                            "filesystem": getattr(g, "filesystemType", None), "capacity_gb": _gb(g.capacity),
                            "free_gb": _gb(g.freeSpace), "free_pct": _pct(g.freeSpace, g.capacity)})
        return pd.DataFrame(out)

    def _q_vm_snapshots(self, si, spec, limit):
        now = dt.datetime.now(UTC)
        out = []
        for obj, p, base in self._vm_parent_rows(si, spec, ["snapshot"]):
            snap = p.get("snapshot")
            if not snap:
                continue
            for n, depth, parent, current in self._walk_snapshots(snap.rootSnapshotList, snap.currentSnapshot):
                age = (now - n.createTime).total_seconds() / 86400 if n.createTime else None
                out.append({"vm": base["vm"], "vm_moid": base["vm_moid"], "snapshot": n.name,
                            "description": n.description or None, "created": _ts(n.createTime),
                            "age_days": round(age, 1) if age is not None else None, "state": n.state,
                            "quiesced": n.quiesced, "snapshot_id": n.id, "parent_snapshot": parent,
                            "depth": depth, "is_current": current})
        df = pd.DataFrame(out)
        if not df.empty and spec.get("older_than_days"):
            df = df[df["age_days"] >= float(spec["older_than_days"])]
        return df

    def _q_hosts(self, si, spec, limit):
        from pyVmomi import vim
        topo = self._topology(si)
        rows = []
        for obj, p in self._retrieve(si, [vim.HostSystem], {vim.HostSystem: [
                "name", "summary.hardware", "summary.config.product", "summary.quickStats", "summary.rebootRequired",
                "runtime.connectionState", "runtime.powerState", "runtime.inMaintenanceMode", "runtime.bootTime",
                "overallStatus", "vm", "datastore"]}):
            hw = p.get("summary.hardware")
            prod = p.get("summary.config.product")
            qs = p.get("summary.quickStats")
            serial = None
            for ident in getattr(hw, "otherIdentifyingInfo", None) or []:
                key = getattr(ident.identifierType, "key", "")
                if key in ("ServiceTag", "SerialNumberTag", "EnclosureSerialNumberTag") and ident.identifierValue:
                    serial = ident.identifierValue
                    break
            cap_mhz = (hw.cpuMhz * hw.numCpuCores) if hw and hw.cpuMhz and hw.numCpuCores else None
            mem_gb = _gb(hw.memorySize) if hw else None
            used_mem_gb = round(qs.overallMemoryUsage / 1024, 2) if qs and qs.overallMemoryUsage is not None else None
            rows.append({
                "moid": obj._moId, "name": p.get("name"), "cluster": self._cluster_of_host(topo, obj._moId),
                "datacenter": self._datacenter_of(topo, obj._moId),
                "connection_state": p.get("runtime.connectionState"), "power_state": p.get("runtime.powerState"),
                "in_maintenance_mode": bool(p.get("runtime.inMaintenanceMode")), "overall_status": p.get("overallStatus"),
                "vendor": getattr(hw, "vendor", None), "model": getattr(hw, "model", None), "serial_number": serial,
                "cpu_model": getattr(hw, "cpuModel", None), "cpu_sockets": getattr(hw, "numCpuPkgs", None),
                "cpu_cores": getattr(hw, "numCpuCores", None), "cpu_threads": getattr(hw, "numCpuThreads", None),
                "cpu_mhz": getattr(hw, "cpuMhz", None), "memory_gb": mem_gb,
                "cpu_usage_mhz": getattr(qs, "overallCpuUsage", None),
                "cpu_usage_pct": _pct(getattr(qs, "overallCpuUsage", None), cap_mhz),
                "memory_usage_gb": used_mem_gb, "memory_usage_pct": _pct(used_mem_gb, mem_gb),
                "esxi_version": getattr(prod, "version", None), "esxi_build": getattr(prod, "build", None),
                "uptime_seconds": getattr(qs, "uptime", None), "boot_time": _ts(p.get("runtime.bootTime")),
                "vm_count": len(p.get("vm") or []), "datastore_count": len(p.get("datastore") or []),
                "nic_count": getattr(hw, "numNics", None), "hba_count": getattr(hw, "numHBAs", None),
                "reboot_required": p.get("summary.rebootRequired"),
            })
        df = pd.DataFrame(rows)
        return self._scope(df, spec).sort_values("name", kind="stable") if not df.empty else df

    def _q_clusters(self, si, spec, limit):
        from pyVmomi import vim
        topo = self._topology(si)
        host_vms = {o._moId: len(p.get("vm") or []) for o, p in self._retrieve(si, [vim.HostSystem], {vim.HostSystem: ["vm"]})}
        rows = []
        for obj, p in self._retrieve(si, [vim.ClusterComputeResource], {vim.ClusterComputeResource: [
                "name", "summary", "configurationEx", "host", "overallStatus"]}):
            s = p.get("summary")
            cfg = p.get("configurationEx")
            u = getattr(s, "usageSummary", None)
            das = getattr(cfg, "dasConfig", None)
            drs = getattr(cfg, "drsConfig", None)
            vsan = getattr(cfg, "vsanConfigInfo", None)
            eff_mem_gb = round(s.effectiveMemory / 1024, 2) if s is not None and s.effectiveMemory is not None else None
            mem_demand_gb = round(u.memDemandMB / 1024, 2) if u is not None and u.memDemandMB is not None else None
            rows.append({
                "moid": obj._moId, "name": p.get("name"), "datacenter": self._datacenter_of(topo, obj._moId),
                "overall_status": p.get("overallStatus") or getattr(s, "overallStatus", None),
                "num_hosts": getattr(s, "numHosts", None), "num_effective_hosts": getattr(s, "numEffectiveHosts", None),
                "vm_count": sum(host_vms.get(h._moId, 0) for h in p.get("host") or []),
                "cpu_cores": getattr(s, "numCpuCores", None), "total_cpu_mhz": getattr(s, "totalCpu", None),
                "effective_cpu_mhz": getattr(s, "effectiveCpu", None), "cpu_demand_mhz": getattr(u, "cpuDemandMhz", None),
                "cpu_demand_pct": _pct(getattr(u, "cpuDemandMhz", None), getattr(s, "effectiveCpu", None)),
                "total_memory_gb": _gb(getattr(s, "totalMemory", None)), "effective_memory_gb": eff_mem_gb,
                "memory_demand_gb": mem_demand_gb, "memory_demand_pct": _pct(mem_demand_gb, eff_mem_gb),
                "ha_enabled": getattr(das, "enabled", None), "ha_admission_control": getattr(das, "admissionControlEnabled", None),
                "drs_enabled": getattr(drs, "enabled", None), "drs_behavior": getattr(drs, "defaultVmBehavior", None),
                "vsan_enabled": getattr(vsan, "enabled", None), "num_vmotions": getattr(s, "numVmotions", None),
            })
        df = pd.DataFrame(rows)
        if not df.empty:
            if spec.get("cluster"):          # on this table `cluster` is the cluster's own name
                df = df[df["name"].isin(_as_list(spec["cluster"]))]
            df = self._scope(df, {k: v for k, v in spec.items() if k != "cluster"}).sort_values("name", kind="stable")
        return df

    def _q_datastores(self, si, spec, limit):
        from pyVmomi import vim
        topo = self._topology(si)
        rows = []
        for obj, p in self._retrieve(si, [vim.Datastore], {vim.Datastore: [
                "name", "parent", "summary", "info", "host", "vm", "overallStatus"]}):
            s = p.get("summary")
            info = p.get("info")
            cap, free, unc = getattr(s, "capacity", None), getattr(s, "freeSpace", None), getattr(s, "uncommitted", None) or 0
            used = cap - free if cap is not None and free is not None else None
            vmfs = getattr(info, "vmfs", None)
            nas = getattr(info, "nas", None)
            device = None
            if vmfs is not None and vmfs.extent:
                device = ", ".join(e.diskName for e in vmfs.extent)
            elif nas is not None:
                device = f"{nas.remoteHost}:{nas.remotePath}"
            rows.append({
                "moid": obj._moId, "name": p.get("name"),
                "datacenter": self._datacenter_of(topo, getattr(p.get("parent"), "_moId", None)),
                "type": getattr(s, "type", None), "capacity_gb": _gb(cap), "free_gb": _gb(free), "used_gb": _gb(used),
                "used_pct": _pct(used, cap), "uncommitted_gb": _gb(unc),
                "provisioned_gb": _gb((used or 0) + unc) if used is not None else None,
                "provisioned_pct": _pct((used or 0) + unc, cap) if used is not None else None,
                "accessible": getattr(s, "accessible", None), "maintenance_mode": getattr(s, "maintenanceMode", None),
                "multiple_host_access": getattr(s, "multipleHostAccess", None), "host_count": len(p.get("host") or []),
                "vm_count": len(p.get("vm") or []), "overall_status": p.get("overallStatus"), "url": getattr(s, "url", None),
                "vmfs_version": getattr(vmfs, "version", None), "ssd": getattr(vmfs, "ssd", None), "device": device,
            })
        df = pd.DataFrame(rows)
        return self._scope(df, spec).sort_values("name", kind="stable") if not df.empty else df

    def _q_networks(self, si, spec, limit):
        from pyVmomi import vim
        topo = self._topology(si)
        rows = self._retrieve(si, [vim.Network], {vim.Network: ["name", "parent", "summary.accessible", "host", "vm"],
                                                  vim.dvs.DistributedVirtualPortgroup: ["config"]})
        switches = [p["config"].distributedVirtualSwitch for _, p in rows
                    if p.get("config") is not None and p["config"].distributedVirtualSwitch is not None]
        switch_names = {m: n for m, (n, _) in self._names_for(si, switches).items()}
        out = []
        for obj, p in rows:
            cfg = p.get("config")
            vlan = None
            if cfg is not None:
                v = getattr(getattr(cfg, "defaultPortConfig", None), "vlan", None)
                vid = getattr(v, "vlanId", None)
                vlan = vid if isinstance(vid, int) else None
            out.append({
                "moid": obj._moId, "name": p.get("name"), "type": type(obj).__name__.rsplit(".", 1)[-1],
                "datacenter": self._datacenter_of(topo, getattr(p.get("parent"), "_moId", None)),
                "switch": switch_names.get(cfg.distributedVirtualSwitch._moId) if cfg is not None and cfg.distributedVirtualSwitch is not None else None,
                "vlan_id": vlan, "num_ports": getattr(cfg, "numPorts", None) if cfg is not None else None,
                "is_uplink": bool(getattr(cfg, "uplink", False)) if cfg is not None else False,
                "host_count": len(p.get("host") or []), "vm_count": len(p.get("vm") or []),
                "accessible": p.get("summary.accessible"),
            })
        df = pd.DataFrame(out)
        return self._scope(df, spec).sort_values("name", kind="stable") if not df.empty else df

    def _q_resource_pools(self, si, spec, limit):
        from pyVmomi import vim
        topo = self._topology(si)
        out = []
        for obj, p in self._retrieve(si, [vim.ResourcePool], {vim.ResourcePool: [
                "name", "parent", "owner", "config.cpuAllocation", "config.memoryAllocation", "vm", "resourcePool",
                "overallStatus"]}):
            cpu, mem = p.get("config.cpuAllocation"), p.get("config.memoryAllocation")
            parent = p.get("parent")
            is_root = parent is not None and topo["kinds"].get(parent._moId, "").endswith("ComputeResource")
            owner = p.get("owner")
            out.append({
                "moid": obj._moId, "name": p.get("name"), "cluster": self._name(topo, owner),
                "datacenter": self._datacenter_of(topo, obj._moId),
                "parent_pool": None if is_root else self._name(topo, parent), "is_root": is_root,
                "cpu_reservation_mhz": getattr(cpu, "reservation", None), "cpu_limit_mhz": getattr(cpu, "limit", None),
                "cpu_shares_level": getattr(getattr(cpu, "shares", None), "level", None),
                "cpu_shares": getattr(getattr(cpu, "shares", None), "shares", None),
                "cpu_expandable": getattr(cpu, "expandableReservation", None),
                "memory_reservation_mb": getattr(mem, "reservation", None), "memory_limit_mb": getattr(mem, "limit", None),
                "memory_shares_level": getattr(getattr(mem, "shares", None), "level", None),
                "memory_expandable": getattr(mem, "expandableReservation", None),
                "vm_count": len(p.get("vm") or []), "child_pool_count": len(p.get("resourcePool") or []),
                "overall_status": p.get("overallStatus"),
            })
        df = pd.DataFrame(out)
        return self._scope(df, spec).sort_values(["cluster", "name"], kind="stable") if not df.empty else df

    def _q_datacenters(self, si, spec, limit):
        from pyVmomi import vim
        topo = self._topology(si)
        dcs = {m: n for m, n in topo["names"].items() if topo["kinds"].get(m) == "Datacenter"}
        counts = {m: {"cluster_count": 0, "host_count": 0, "vm_count": 0, "datastore_count": 0, "network_count": 0} for m in dcs}

        def dc_of(moid):
            seen = 0
            while moid and seen < 64:
                if moid in dcs:
                    return moid
                moid = topo["parents"].get(moid)
                seen += 1
            return None
        for m, k in topo["kinds"].items():
            d = dc_of(m)
            if d and k == "ClusterComputeResource":
                counts[d]["cluster_count"] += 1
            if d and k == "HostSystem":
                counts[d]["host_count"] += 1
        for label, t in (("vm_count", vim.VirtualMachine), ("datastore_count", vim.Datastore), ("network_count", vim.Network)):
            for o, p in self._retrieve(si, [t], {t: ["parent"]}):
                d = dc_of(getattr(p.get("parent"), "_moId", None))
                if d:
                    counts[d][label] += 1
        status = {o._moId: p.get("overallStatus") for o, p in self._retrieve(si, [vim.Datacenter], {vim.Datacenter: ["overallStatus"]})}
        df = pd.DataFrame([{"moid": m, "name": n, "overall_status": status.get(m), **counts[m]} for m, n in dcs.items()])
        return self._scope(df, {k: v for k, v in spec.items() if k != "datacenter"}).sort_values("name") if not df.empty else df

    # ── tags (vSphere Automation REST) ────────────────────────────────────────

    @contextmanager
    def _rest(self):
        """A vAPI session for the Automation REST API; yields call(method, path, **kw) -> JSON.

        `verify` goes on every request: a Session-level verify is silently overridden by
        REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE in the environment.
        """
        base = f"https://{self.host}:{self.port}"
        verify = self._requests_verify()
        http = requests.Session()
        try:
            r = http.post(f"{base}/api/session", auth=(self.username or "", self.password or ""),
                          verify=verify, timeout=self.timeout)
        except requests.exceptions.SSLError as e:
            http.close()
            raise RuntimeError(f"TLS error on the vSphere REST API: {e}")
        except requests.exceptions.RequestException as e:
            http.close()
            raise RuntimeError(f"vSphere REST API unreachable: {e}")
        if r.status_code >= 400:
            http.close()
            if r.status_code == 404:
                raise RuntimeError("this endpoint has no vSphere Automation API (/api): a standalone ESXi host or vCenter older than 7.0 U2")
            if r.status_code in (401, 403):
                raise RuntimeError(f"the vSphere REST API rejected the login ({r.status_code})")
            raise RuntimeError(f"POST /api/session returned {r.status_code}")
        headers = {"vmware-api-session-id": r.json()}

        def call(method: str, path: str, **kw):
            resp = http.request(method, f"{base}{path}", headers=headers, verify=verify, timeout=self.timeout, **kw)
            if resp.status_code >= 400:
                raise RuntimeError(f"{method} {path} returned {resp.status_code}: {resp.text[:200]}")
            return resp.json() if resp.content else None
        try:
            yield call
        finally:
            try:
                http.delete(f"{base}/api/session", headers=headers, verify=verify, timeout=self.timeout)
            except Exception:
                pass
            http.close()

    def _rest_tag_ids(self) -> List[str]:
        with self._rest() as call:
            return call("GET", "/api/cis/tagging/tag")

    def _q_tags(self, si, spec, limit):
        if self._is_esxi(si) or not self.include_tags:
            return pd.DataFrame()
        from pyVmomi import VmomiSupport
        with self._rest() as call:
            cats = {cid: call("GET", f"/api/cis/tagging/category/{cid}") for cid in call("GET", "/api/cis/tagging/category")}
            tags = {tid: call("GET", f"/api/cis/tagging/tag/{tid}") for tid in call("GET", "/api/cis/tagging/tag")}
            want_cat = set(_as_list(spec.get("category")))
            want_tag = set(_as_list(spec.get("tag")))
            selected = [tid for tid, t in tags.items()
                        if (not want_cat or cats.get(t.get("category_id"), {}).get("name") in want_cat)
                        and (not want_tag or t.get("name") in want_tag)]
            assoc = []
            for i in range(0, len(selected), 100):
                assoc.extend(call("POST", "/api/cis/tagging/tag-association",
                                  params={"action": "list-attached-objects-on-tags"},
                                  json={"tag_ids": selected[i:i + 100]}) or [])
        refs = []
        for a in assoc:
            for o in a.get("object_ids") or []:
                try:
                    cls = VmomiSupport.GetWsdlType("urn:vim25", o.get("type"))
                    refs.append(cls(o.get("id"), si._stub))
                except Exception:
                    continue
        names = self._names_for(si, [r for r in refs if hasattr(r, "_moId")]) if refs else {}
        rows = []
        for a in assoc:
            t = tags.get(a.get("tag_id"), {})
            c = cats.get(t.get("category_id"), {})
            for o in a.get("object_ids") or []:
                rows.append({"category": c.get("name"), "tag": t.get("name"), "cardinality": c.get("cardinality"),
                             "object_type": o.get("type"), "object_moid": o.get("id"),
                             "object_name": (names.get(o.get("id")) or (None,))[0], "tag_description": t.get("description") or None,
                             "tag_id": a.get("tag_id"), "category_id": t.get("category_id")})
        df = pd.DataFrame(rows)
        if not df.empty:
            if spec.get("object_type"):
                df = df[df["object_type"].isin([_ENTITY_TYPES.get(x.lower(), x) for x in _as_list(spec["object_type"])])]
            if spec.get("object_name"):
                df = df[df["object_name"].isin(_as_list(spec["object_name"]))]
            df = df.sort_values(["category", "tag", "object_name"], kind="stable")
        return df

    # ── alarms ────────────────────────────────────────────────────────────────

    def _q_alarms(self, si, spec, limit):
        from pyVmomi import vim
        content = self._content(si)
        states = []
        for _, p in self._retrieve_objects(si, [content.rootFolder], {vim.ManagedEntity: ["triggeredAlarmState"]}):
            states.extend(p.get("triggeredAlarmState") or [])
        uniq: Dict[str, Any] = {}
        for st in states:
            uniq.setdefault(st.key, st)
        states = list(uniq.values())
        names = self._names_for(si, [s.entity for s in states])
        alarm_info = {}
        alarms = [s.alarm for s in states if s.alarm is not None]
        if alarms:
            for o, p in self._retrieve_objects(si, alarms, {vim.alarm.Alarm: ["info.name", "info.description"]}):
                alarm_info[o._moId] = (p.get("info.name"), p.get("info.description"))
        rows = []
        for s in states:
            ename, ekind = names.get(s.entity._moId, (None, None)) if s.entity is not None else (None, None)
            aname, adesc = alarm_info.get(s.alarm._moId, (None, None)) if s.alarm is not None else (None, None)
            rows.append({"alarm": aname or (s.alarm._moId if s.alarm is not None else None), "alarm_description": adesc,
                         "status": s.overallStatus, "entity": ename, "entity_type": ekind,
                         "entity_moid": s.entity._moId if s.entity is not None else None,
                         "triggered_time": _ts(s.time), "acknowledged": bool(s.acknowledged),
                         "acknowledged_by": s.acknowledgedByUser, "acknowledged_time": _ts(s.acknowledgedTime),
                         "alarm_moid": s.alarm._moId if s.alarm is not None else None, "key": s.key})
        df = pd.DataFrame(rows)
        if not df.empty:
            if spec.get("status"):
                df = df[df["status"].isin(_as_list(spec["status"]))]
            if spec.get("entity"):
                df = df[df["entity"].isin(_as_list(spec["entity"]))]
            if spec.get("entity_type"):
                df = df[df["entity_type"].isin([_ENTITY_TYPES.get(x.lower(), x) for x in _as_list(spec["entity_type"])])]
            rank = {"red": 0, "yellow": 1}
            df = df.assign(_r=df["status"].map(lambda s: rank.get(s, 2))).sort_values(
                ["_r", "triggered_time"], ascending=[True, False], kind="stable").drop(columns="_r")
        return df

    # ── events / tasks ────────────────────────────────────────────────────────

    def _window(self, spec) -> Tuple[dt.datetime, dt.datetime]:
        now = dt.datetime.now(UTC)
        end = _parse_time(spec["end_time"], "end_time") if spec.get("end_time") is not None else now
        if spec.get("start_time") is not None:
            start = _parse_time(spec["start_time"], "start_time")
        elif spec.get("minutes"):
            start = end - dt.timedelta(minutes=float(spec["minutes"]))
        elif spec.get("hours"):
            start = end - dt.timedelta(hours=float(spec["hours"]))
        else:
            start = end - dt.timedelta(hours=self.default_lookback_hours)
        if start >= end:
            raise ValueError("start_time must be before end_time.")
        return start, end

    def _resolve_entity(self, si, spec) -> Optional[Any]:
        """Turn {"entity": name, "entity_type": ...} into a managed object reference."""
        if not spec.get("entity"):
            return None
        from pyVmomi import VmomiSupport, vim
        name = str(spec["entity"])
        kind = _ENTITY_TYPES.get(str(spec.get("entity_type", "")).lower(), spec.get("entity_type"))
        types = [VmomiSupport.GetWsdlType("urn:vim25", kind)] if kind else [vim.ManagedEntity]
        matches = [o for o, p in self._retrieve(si, types, {vim.ManagedEntity: ["name"]}) if p.get("name") == name]
        if not matches:
            raise ValueError(f"No vCenter object named {name!r}" + (f" of type {kind}" if kind else "") + ".")
        if len(matches) > 1:
            kinds = sorted({type(m).__name__.rsplit('.', 1)[-1] for m in matches})
            if len(kinds) > 1 and not kind:
                raise ValueError(f"{name!r} matches several object types ({', '.join(kinds)}); add entity_type.")
        return matches[0]

    def _event_categories(self, si) -> Dict[str, str]:
        cache = self._cache()
        if "event_cat" in cache:
            return cache["event_cat"]
        from pyVmomi import vim
        cats: Dict[str, str] = {}
        try:
            em = self._content(si).eventManager
            rows = self._retrieve_objects(si, [em], {vim.event.EventManager: ["description.eventInfo"]})
            for _, p in rows:
                for d in p.get("description.eventInfo") or []:
                    key = d.key
                    key_name = key.__name__.rsplit(".", 1)[-1] if isinstance(key, type) else str(key)
                    if key_name in ("EventEx", "ExtendedEvent") and d.fullFormat and "|" in d.fullFormat:
                        cats[d.fullFormat.split("|", 1)[0]] = d.category
                    else:
                        cats[key_name] = d.category
        except Exception:
            pass
        cache["event_cat"] = cats
        return cats

    def _q_events(self, si, spec, limit):
        from pyVmomi import vim
        if self._content(si).eventManager is None:
            return pd.DataFrame()
        start, end = self._window(spec)
        entity = self._resolve_entity(si, spec)
        f = vim.event.EventFilterSpec(time=vim.event.EventFilterSpec.ByTime(beginTime=start, endTime=end))
        if entity is not None:
            f.entity = vim.event.EventFilterSpec.ByEntity(entity=entity, recursion="all")
        if spec.get("user"):
            f.userName = vim.event.EventFilterSpec.ByUsername(systemUser=False, userList=_as_list(spec["user"]))
        # Category and type are filtered client-side: EventEx/esx.problem events only carry
        # their type in eventTypeId, which the server-side filter does not match on every version.
        cats = self._event_categories(si)
        want_types = set(_as_list(spec.get("event_type")))
        want_cats = set(_as_list(spec.get("category")))
        needle = str(spec.get("message_contains") or "").lower()
        keep: deque = deque(maxlen=limit)
        content = self._content(si)
        collector = content.eventManager.CreateCollectorForEvents(f)
        scanned = 0
        try:
            collector.RewindCollector()
            while scanned < MAX_EVENT_SCAN:
                batch = collector.ReadNextEvents(COLLECTOR_PAGE)
                if not batch:
                    break
                scanned += len(batch)
                for ev in batch:
                    etype = getattr(ev, "eventTypeId", None) or type(ev).__name__.rsplit(".", 1)[-1]
                    cat = getattr(ev, "severity", None) or cats.get(etype) or cats.get(type(ev).__name__.rsplit(".", 1)[-1]) or "info"
                    if want_types and etype not in want_types and not any(etype.startswith(t.rstrip("*")) for t in want_types if t.endswith("*")):
                        continue
                    if want_cats and cat not in want_cats:
                        continue
                    msg = ev.fullFormattedMessage or ""
                    if needle and needle not in msg.lower():
                        continue
                    keep.append((ev, etype, cat))
        finally:
            try:
                collector.DestroyCollector()
            except Exception:
                pass

        def arg(ev, attr):
            a = getattr(ev, attr, None)
            return getattr(a, "name", None) if a is not None else None
        rows = [{"created": _ts(ev.createdTime), "event_type": et, "category": cat, "message": ev.fullFormattedMessage,
                 "user": ev.userName or None, "datacenter": arg(ev, "datacenter"), "cluster": arg(ev, "computeResource"),
                 "host": arg(ev, "host"), "vm": arg(ev, "vm"), "datastore": arg(ev, "ds"), "network": arg(ev, "net"),
                 "key": ev.key, "chain_id": ev.chainId} for ev, et, cat in keep]
        df = pd.DataFrame(rows)
        return df.sort_values(["created", "key"], ascending=False, kind="stable") if not df.empty else df

    def _q_tasks(self, si, spec, limit):
        from pyVmomi import vim
        content = self._content(si)
        if content.taskManager is None:
            return pd.DataFrame()
        start, end = self._window(spec)
        entity = self._resolve_entity(si, spec)
        f = vim.TaskFilterSpec(time=vim.TaskFilterSpec.ByTime(timeType="queuedTime", beginTime=start, endTime=end))
        if entity is not None:
            f.entity = vim.TaskFilterSpec.ByEntity(entity=entity, recursion="all")
        if spec.get("state"):
            f.state = _as_list(spec["state"])
        if spec.get("user"):
            f.userName = vim.TaskFilterSpec.ByUsername(systemUser=False, userList=_as_list(spec["user"]))
        needle = str(spec.get("task_contains") or "").lower()
        keep: deque = deque(maxlen=limit)
        collector = content.taskManager.CreateCollectorForTasks(f)
        scanned = 0
        try:
            collector.RewindCollector()
            while scanned < MAX_EVENT_SCAN:
                batch = collector.ReadNextTasks(COLLECTOR_PAGE)
                if not batch:
                    break
                scanned += len(batch)
                for t in batch:
                    name = t.descriptionId or (getattr(t.name, "info", None) and t.name.info.wsdlName) or str(t.name or "")
                    if needle and needle not in name.lower():
                        continue
                    keep.append((t, name))
        finally:
            try:
                collector.DestroyCollector()
            except Exception:
                pass
        kinds = {}
        if keep:
            kinds = {m: k for m, (_, k) in self._names_for(si, [t.entity for t, _ in keep if t.entity is not None]).items()}
        rows = []
        for t, name in keep:
            err = None
            if t.error is not None:
                err = (getattr(t.error, "localizedMessage", None) or getattr(t.error, "msg", None)
                       or getattr(getattr(t.error, "fault", None), "msg", None) or type(t.error).__name__.rsplit(".", 1)[-1])
            dur = (t.completeTime - t.startTime).total_seconds() if t.completeTime and t.startTime else None
            rows.append({"key": t.key, "task": name, "entity": t.entityName,
                         "entity_type": kinds.get(t.entity._moId) if t.entity is not None else None,
                         "entity_moid": t.entity._moId if t.entity is not None else None, "state": t.state,
                         "error": err, "user": getattr(t.reason, "userName", None), "queued": _ts(t.queueTime),
                         "started": _ts(t.startTime), "completed": _ts(t.completeTime),
                         "duration_seconds": dur, "progress": t.progress, "cancelled": t.cancelled})
        df = pd.DataFrame(rows)
        return df.sort_values("queued", ascending=False, kind="stable") if not df.empty else df

    # ── licenses ──────────────────────────────────────────────────────────────

    @staticmethod
    def _mask(key: Optional[str]) -> Optional[str]:
        if not key:
            return key
        return "*****-" * 4 + key[-5:] if len(key) >= 5 else "*****"

    def _q_licenses(self, si, spec, limit):
        from pyVmomi import vim
        lm = self._content(si).licenseManager
        if lm is None:
            return pd.DataFrame()
        rows = []
        for _, p in self._retrieve_objects(si, [lm], {vim.LicenseManager: ["licenses"]}):
            for lic in p.get("licenses") or []:
                props = {kv.key: kv.value for kv in (lic.properties or [])}
                exp = props.get("expirationDate")
                rows.append({"name": lic.name, "edition": lic.editionKey, "license_key": self._mask(lic.licenseKey),
                             "total": lic.total, "used": lic.used, "cost_unit": lic.costUnit or None,
                             "expires": _ts(exp) if isinstance(exp, dt.datetime) else None})
        return pd.DataFrame(rows)

    # ── performance ───────────────────────────────────────────────────────────

    def _perf_catalog(self, si):
        if self._counters is not None:
            return self._counters
        from pyVmomi import vim
        pm = self._content(si).perfManager
        rows = self._retrieve_objects(si, [pm], {vim.PerformanceManager: ["perfCounter", "historicalInterval"]})
        counters, intervals = {}, []
        for _, p in rows:
            for c in p.get("perfCounter") or []:
                key = f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}"
                counters[key] = c
                self._counters_by_id[c.key] = c
            intervals = list(p.get("historicalInterval") or [])
        self._counters = counters
        self._intervals = sorted(intervals, key=lambda h: h.samplingPeriod)
        return counters

    @staticmethod
    def _counter_key(c) -> str:
        return f"{c.groupInfo.key}.{c.nameInfo.key}.{c.rollupType}"

    def _q_perf_counters(self, si, spec, limit):
        counters = self._perf_catalog(si)
        rows = [{"counter": k, "counter_id": c.key, "group": c.groupInfo.key, "name": c.nameInfo.key,
                 "rollup": c.rollupType, "unit": c.unitInfo.key, "stats_type": c.statsType, "level": c.level,
                 "per_device_level": c.perDeviceLevel, "description": c.nameInfo.summary} for k, c in counters.items()]
        df = pd.DataFrame(rows)
        if not df.empty:
            if spec.get("search"):
                s = str(spec["search"]).lower()
                df = df[df["counter"].str.lower().str.contains(s, regex=False) | df["description"].fillna("").str.lower().str.contains(s, regex=False)]
            if spec.get("group"):
                df = df[df["group"].isin(_as_list(spec["group"]))]
            if spec.get("level"):
                df = df[df["level"] <= int(spec["level"])]
            df = df.sort_values("counter")
        return df

    def _perf_entities(self, si, spec, required: bool = True) -> List[Tuple[Any, str, str]]:
        """(ref, name, kind) for the metric scope. Requires an entity_type."""
        from pyVmomi import VmomiSupport
        raw = spec.get("entity_type")
        if not raw:
            raise ValueError('Add "entity_type": one of VirtualMachine, HostSystem, ClusterComputeResource, Datastore, ResourcePool.')
        kind = _ENTITY_TYPES.get(str(raw).lower(), str(raw))
        try:
            cls = VmomiSupport.GetWsdlType("urn:vim25", kind)
        except KeyError:
            raise ValueError(f"Unknown entity_type {raw!r}.")
        rows = self._retrieve(si, [cls], {cls: ["name"]})
        frame = pd.DataFrame([{"moid": o._moId, "name": p.get("name"), "_o": o} for o, p in rows])
        if frame.empty:
            return []
        scoped = self._scope(frame, {k: spec[k] for k in ("name", "names", "name_regex", "moid", "moids") if k in spec})
        has_scope = any(k in spec for k in ("name", "names", "name_regex", "moid", "moids"))
        if has_scope and scoped.empty:
            raise ValueError(f"No {kind} matches the given name / moid scope.")
        if not has_scope and len(scoped) > MAX_METRIC_ENTITIES:
            raise ValueError(f"{len(scoped)} {kind} objects; scope the query with name / names / name_regex / moid "
                             f"(at most {MAX_METRIC_ENTITIES} without a scope).")
        return [(r["_o"], r["name"], kind) for _, r in scoped.iterrows()]

    def _choose_interval(self, spec, kind: str, start: dt.datetime) -> int:
        raw = spec.get("interval", "auto")
        if raw not in (None, "auto"):
            try:
                return int(raw)
            except (TypeError, ValueError):
                raise ValueError('interval must be "auto", 20 (realtime) or a historical interval in seconds (300, 1800, 7200, 86400).')
        now = dt.datetime.now(UTC)
        realtime_ok = kind in ("VirtualMachine", "HostSystem")
        if realtime_ok and (now - start).total_seconds() <= REALTIME_WINDOW_S + 60:
            return REALTIME_INTERVAL
        for h in self._intervals:
            # a little slack so "the last 7 days" still lands on the 1-week rollup
            if h.enabled and (now - start).total_seconds() <= h.length + 600:
                return h.samplingPeriod
        if self._intervals:
            return self._intervals[-1].samplingPeriod
        return REALTIME_INTERVAL

    def _interval_level(self, interval: int) -> Optional[int]:
        if interval == REALTIME_INTERVAL:
            return None
        return next((h.level for h in self._intervals if h.samplingPeriod == interval), None)

    def _q_metrics_available(self, si, spec, limit):
        self._perf_catalog(si)
        pm = self._content(si).perfManager
        ents = self._perf_entities(si, spec)
        if len(ents) > 50:
            raise ValueError("metrics_available inspects at most 50 objects at a time; narrow the scope.")
        rows = []
        now = dt.datetime.now(UTC)
        for ref, name, kind in ents:
            interval = self._choose_interval(spec, kind, now - dt.timedelta(minutes=30))
            ids = pm.QueryAvailablePerfMetric(entity=ref, intervalId=interval)
            for m in ids or []:
                c = self._counters_by_id.get(m.counterId)
                if c is None:
                    continue
                rows.append({"entity": name, "entity_type": kind, "moid": ref._moId, "counter": self._counter_key(c),
                             "instance": m.instance, "unit": c.unitInfo.key, "interval_seconds": interval, "level": c.level})
        df = pd.DataFrame(rows)
        if not df.empty and spec.get("counter_contains"):
            df = df[df["counter"].str.contains(str(spec["counter_contains"]), case=False, regex=False)]
        return df.sort_values(["entity", "counter", "instance"]) if not df.empty else df

    def _q_metrics(self, si, spec, limit):
        from pyVmomi import vim, vmodl
        counters = self._perf_catalog(si)
        wanted = _as_list(spec.get("counters")) + _as_list(spec.get("counter"))
        if not wanted:
            raise ValueError('Add "counters": e.g. ["cpu.usage.average", "disk.maxTotalLatency.latest"] (see perf_counters).')
        unknown = [c for c in wanted if c not in counters]
        if unknown:
            close = sorted(k for k in counters if any(u.split(".")[0] == k.split(".")[0] for u in unknown))[:12]
            raise ValueError(f"Unknown counter(s): {', '.join(unknown)}. Counter keys are group.name.rollup; "
                             f"e.g. {', '.join(close) or ', '.join(sorted(counters)[:12])}. Query perf_counters for the full list.")
        start, end = self._window(spec if any(k in spec for k in _WINDOW) else {**spec, "minutes": 60})
        ents = self._perf_entities(si, spec)
        if not ents:
            return pd.DataFrame()
        kind = ents[0][2]
        interval = self._choose_interval(spec, kind, start)
        level = self._interval_level(interval)
        if interval == REALTIME_INTERVAL and kind not in ("VirtualMachine", "HostSystem"):
            raise ValueError(f"Realtime (20 s) data exists only for VMs and hosts; {kind} has historical intervals only "
                             f"({', '.join(str(h.samplingPeriod) for h in self._intervals) or 'none'}).")
        if interval == REALTIME_INTERVAL and (dt.datetime.now(UTC) - start).total_seconds() > REALTIME_WINDOW_S + 60 and spec.get("interval") not in (None, "auto"):
            raise ValueError("Realtime data covers only the last hour; use interval 300 or 'auto' for older windows.")
        if interval != REALTIME_INTERVAL and level is None:
            raise ValueError(f"This endpoint has no {interval}s historical interval; available: "
                             f"{', '.join(str(h.samplingPeriod) for h in self._intervals) or 'realtime only'}.")
        instance = spec.get("instance", "")
        instance = "*" if instance in ("*", "all") else str(instance)
        too_deep = []
        if level is not None:
            for k in wanted:
                c = counters[k]
                need = c.perDeviceLevel if instance not in ("",) else c.level
                if need > level:
                    too_deep.append(k)
            if len(too_deep) == len(wanted):
                raise ValueError(
                    f"No data at the {interval}s interval: {', '.join(too_deep)} need statistics level "
                    f"{max(counters[k].perDeviceLevel if instance else counters[k].level for k in too_deep)} but the interval "
                    f"is collected at level {level}. Query a window inside the last hour (realtime keeps every counter for "
                    "VMs and hosts), use aggregate instance \"\", or ask the vSphere admin to raise the statistics level.")
        ids = [vim.PerformanceManager.MetricId(counterId=counters[k].key, instance=instance) for k in wanted]
        per_entity = len(ids) * (8 if instance == "*" else 1)
        batch = max(1, MAX_QUERY_METRICS // max(1, per_entity)) if interval != REALTIME_INTERVAL else 50
        pm = self._content(si).perfManager
        names = {r._moId: (n, k) for r, n, k in ents}
        results = []
        i = 0
        refs = [r for r, _, _ in ents]
        while i < len(refs):
            chunk = refs[i:i + batch]
            specs = [vim.PerformanceManager.QuerySpec(entity=r, metricId=ids, intervalId=interval,
                                                      startTime=start, endTime=end) for r in chunk]
            try:
                results.extend(pm.QueryPerf(querySpec=specs) or [])
            except vmodl.fault.InvalidArgument as e:
                if getattr(e, "invalidProperty", "") == "querySpec.size" and batch > 1:
                    batch = max(1, batch // 2)
                    continue
                raise ValueError(f"vCenter rejected the perf query ({getattr(e, 'invalidProperty', '')}): {e.msg}")
            i += len(chunk)
        rows = []
        for em in results:
            stamps = [_ts(s.timestamp) for s in em.sampleInfo or []]
            n, k = names.get(em.entity._moId, (None, None))
            for series in em.value or []:
                c = self._counters_by_id.get(series.id.counterId)
                if c is None:
                    continue
                scale = 100.0 if c.unitInfo.key == "percent" else 1.0
                ckey = self._counter_key(c)
                for t, v in zip(stamps, series.value or []):
                    if v is None or v < 0:          # -1 = no sample collected
                        continue
                    rows.append({"timestamp": t, "entity": n, "entity_type": k, "moid": em.entity._moId,
                                 "counter": ckey, "instance": series.id.instance, "value": v / scale,
                                 "unit": c.unitInfo.key, "interval_seconds": interval})
        if len(rows) > limit and "limit" not in spec:
            raise ValueError(f"The metrics query returned {len(rows):,} rows (cap {limit:,}). Narrow the objects, counters "
                             "or window, or use a coarser interval.")
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        return df.sort_values(["entity", "counter", "instance", "timestamp"], kind="stable")

    # ── prompts ───────────────────────────────────────────────────────────────

    @property
    def description(self):
        text = ("VMware vCenter Server / ESXi client: inventory (VMs, disks, NICs, guest file systems, snapshots, hosts, "
                "clusters, datastores, networks, resource pools, tags, licenses), performance time series, the event "
                "log, tasks and triggered alarms via the vSphere API.")
        return text + "\n\n" + self.system_prompt()

    def system_prompt(self):
        return """
        ## VMware vCenter Integration
        Query vCenter via `execute_query(query)` where `query` is a JSON string (or dict):

        ```json
        {"table": "vms", "cluster": "Prod-Cluster", "power_state": "poweredOn", "columns": ["name", "host", "datastores", "snapshot_count"]}
        ```

        TABLES: about, datacenters, clusters, hosts, vms, vm_disks, vm_nics, vm_partitions, vm_snapshots,
        datastores, networks, resource_pools, tags, alarms, events, tasks, licenses, perf_counters,
        metrics_available, metrics.

        Inventory tables (vms, hosts, clusters, datastores, networks, resource_pools, datacenters, vm_*) accept:
        `name` / `names` (exact), `name_regex` (case-insensitive), `moid`, `datacenter`, `cluster`, `host`,
        `power_state` (vms + vm_*), `vm` (vm_* tables), `older_than_days` (vm_snapshots), `columns`, `limit`.
        Names are EXACT; use name_regex for partial matches. moids (vm-101, host-12, datastore-40) are the join keys.

        Timeline tables:
        - `events`: `minutes` | `hours` | `start_time`/`end_time` (ISO-8601 UTC or epoch), default last 24 h;
          `entity` (+ `entity_type`) scopes to an object AND its children; `event_type` (class or eventTypeId,
          trailing * allowed, e.g. "esx.problem.*"), `category` (info/warning/error/user), `user`, `message_contains`.
        - `tasks`: same window; `state` (queued/running/success/error), `user`, `entity`, `task_contains`.
        - `alarms`: currently triggered alarms; `status` (red/yellow), `entity`, `entity_type`.

        Performance:
        - `perf_counters` (`search`, `group`) lists exact counter keys `group.name.rollup`.
        - `metrics`: {"table": "metrics", "entity_type": "VirtualMachine", "names": ["prod-db-01"],
          "counters": ["cpu.usage.average", "disk.maxTotalLatency.latest"], "minutes": 60}
          entity_type: VirtualMachine | HostSystem | ClusterComputeResource | Datastore | ResourcePool (always required);
          scope with name/names/name_regex/moid (required above 500 objects); `instance`: "" aggregate (default) or "*";
          `interval`: "auto" (default) | 20 | 300 | 1800 | 7200 | 86400.
          Auto picks realtime 20 s for VMs/hosts in the last hour, else the finest historical rollup covering the window.
          Historical rollups are usually level 1: aggregate values of level-1 counters only. Check `metrics_available`
          for an object before relying on per-instance or level 2+ counters. Datastores and clusters have no realtime data.
          Values: percent counters are 0-100; latency in ms; KBps / KB as named in `unit`.
          Useful counters: cpu.usage.average, cpu.ready.summation (ms per interval; ready % = value / (interval_seconds*1000) * 100),
          mem.usage.average, mem.vmmemctl.average (balloon), disk.maxTotalLatency.latest,
          datastore.totalWriteLatency.average, datastore.totalReadLatency.average, disk.usage.average, net.usage.average,
          disk.used.latest (Datastore, KB).

        RCA playbook:
        1. Locate: `vms` / `hosts` / `datastores` with a name, then use its host, cluster and datastores columns.
        2. Neighbours: who shares the datastore (`vms` filtered on the datastores column) or host (`host` filter).
        3. Same window across layers in `metrics`: VM latency, host latency, datastore latency, host CPU.
        4. What changed: `events` and `tasks` for the window, oldest-first in pandas; failed tasks (`state: error`)
           and `esx.problem.*` events are strong signals; `alarms` for what is red now.
        5. Capacity / hygiene: `datastores.used_pct`, `provisioned_pct`, `vm_snapshots.age_days`,
           `vms.consolidation_needed`, `vm_partitions.free_pct`, hosts `in_maintenance_mode`.

        ```python
        vm   = client.execute_query('{"table": "vms", "name": "prod-db-01"}')
        peers = client.execute_query('{"table": "vms", "power_state": "poweredOn", "columns": ["name", "host", "datastores"]}')
        peers = peers[peers.datastores.str.contains("ds-prod-01")]
        m = client.execute_query('{"table": "metrics", "entity_type": "VirtualMachine", "names": ["prod-db-01", "app-01"], "counters": ["disk.maxTotalLatency.latest"], "minutes": 60}')
        ev = client.execute_query('{"table": "events", "hours": 6, "category": ["warning", "error"]}')
        tk = client.execute_query('{"table": "tasks", "hours": 72, "entity": "backup-proxy-01"}')
        snaps = client.execute_query('{"table": "vm_snapshots", "older_than_days": 3}')
        ```
        Results are complete by default (inventory, events and tasks in the window). Pass `limit` only to sample;
        a limited result is not proof of absence. events/tasks with a limit keep the NEWEST rows.
        """


# Alias so dynamic naming ("vcenter" → "VcenterClient") and the explicit
# client_path both resolve to the same class.
VcenterClient = VCenterClient
