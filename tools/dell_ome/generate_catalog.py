#!/usr/bin/env python3
"""Regenerate the frozen Dell OpenManage Enterprise read-only catalog.

The catalog is an execution allow-list for the BOW connector: every table maps
to exactly one GET endpoint of the OME 4.x REST API (OData v4 flavoured), with
the fields, filters and parent identifiers the connector is permitted to use.
Evidence for each table is recorded in tools/dell_ome/SOURCES.md; tables whose
wire shape could not be verified from public evidence are marked "inferred".

Usage (from the repository root):
    python3 tools/dell_ome/generate_catalog.py
"""

import hashlib
import json
from collections import OrderedDict
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "backend/app/data_sources/clients/dell_ome/catalog.json"
CONTRACT = "OME 4.1 REST (OData v4), verified against OME 3.x/4.x public evidence"

I, N, B, S, A, O = "integer", "number", "boolean", "string", "array", "object"

ODATA_CONTROLS = ["$top", "$skip", "$filter", "$orderby"]
PAGE_CONTROLS = ["$top", "$skip"]


def f(fields):
    return OrderedDict((k, {"type": t, "description": d}) for k, t, d in fields)


def flt(fields, names):
    return OrderedDict((n, {"type": fields[n]["type"]}) for n in names)


DEVICE_FIELDS = f([
    ("Id", I, "OME device identifier; parent key for inventory, power, temperature, logs."),
    ("Type", I, "Device type id: 1000 SERVER, 2000 CHASSIS, 3000 STORAGE, 4000 NETWORK_IOM, 8000 STORAGE_IOM, 9000 NETWORK_CONTROLLER (see device_types)."),
    ("Identifier", S, "Service tag or other unique identifier."),
    ("DeviceServiceTag", S, "Dell service tag."),
    ("ChassisServiceTag", S, "Service tag of the enclosing chassis (modular sleds)."),
    ("Model", S, "Hardware model, e.g. PowerEdge R750."),
    ("PowerState", I, "Power state id: 17 ON, 18 OFF, 20 POWERING_ON, 21 POWERING_OFF (OME convention)."),
    ("ManagedState", I, "Managed state id (3000 managed, 2000 unmanaged/monitored, 1000 unknown)."),
    ("Status", I, "Rollup health: 1000 NORMAL, 2000 UNKNOWN, 3000 WARNING, 4000 CRITICAL, 5000 NO_STATUS."),
    ("ConnectionState", B, "True when OME can currently reach the device management endpoint."),
    ("AssetTag", S, "Asset tag."),
    ("SystemId", I, "Dell system id of the platform."),
    ("DeviceName", S, "Display name (usually the iDRAC DNS name or host name)."),
    ("LastInventoryTime", S, "Appliance timestamp text of the last inventory collection."),
    ("LastStatusTime", S, "Appliance timestamp text of the last health status update."),
    ("DeviceSubscription", O, "Subscription/entitlement details when present."),
    ("DeviceCapabilities", A, "Numeric capability ids reported by the device."),
    ("DeviceManagement", A, "Management endpoints: ManagementId, NetworkAddress, MacAddress, ManagementType, InstrumentationName, DnsName, ManagementProfile[]."),
    ("SlotConfiguration", O, "Chassis slot placement for modular devices."),
])
DEVICE_FILTERS = flt(DEVICE_FIELDS, ["Id", "Type", "Identifier", "DeviceServiceTag", "ChassisServiceTag", "Model",
                                     "PowerState", "ManagedState", "Status", "ConnectionState", "AssetTag", "DeviceName"])

ALERT_FIELDS = f([
    ("Id", I, "Alert identifier."),
    ("SeverityType", I, "Severity id: 1 UNKNOWN, 2 INFO, 4 NORMAL, 8 WARNING, 16 CRITICAL."),
    ("SeverityName", S, "Severity name."),
    ("AlertDeviceId", I, "Source device id (join to devices.Id)."),
    ("AlertDeviceName", S, "Source device name."),
    ("AlertDeviceType", I, "Source device type id."),
    ("AlertDeviceIpAddress", S, "Source device management address."),
    ("AlertDeviceIdentifier", S, "Source device identifier (service tag)."),
    ("AlertDeviceMacAddress", S, "Source device MAC address."),
    ("AlertDeviceGroupName", S, "Group name associated with the alert when present."),
    ("AlertMessageId", S, "EEMI message id, e.g. PSU0003."),
    ("Message", S, "Alert message text."),
    ("AlertMessageType", S, "Message type (Internal, SNMP, Redfish...)."),
    ("StatusType", I, "Acknowledgement status id: 1000 unacknowledged, 2000 acknowledged."),
    ("StatusName", S, "Acknowledgement status name."),
    ("TimeStamp", S, "Appliance timestamp text of the alert."),
    ("CategoryId", I, "Category id: 1 SYSTEM_HEALTH, 2 STORAGE, 3 UPDATES, 4 AUDIT, 5 CONFIGURATION, 6 WORK_NOTES, 7 MISCELLANEOUS."),
    ("CategoryName", S, "Category name."),
    ("SubCategoryId", I, "Sub-category id."),
    ("SubCategoryName", S, "Sub-category name."),
    ("UserName", S, "User associated with the alert (acknowledgement/audit)."),
])
ALERT_FILTERS = flt(ALERT_FIELDS, ["Id", "SeverityType", "AlertDeviceId", "AlertDeviceName", "AlertDeviceType",
                                   "AlertDeviceIdentifier", "StatusType", "CategoryId", "SubCategoryId",
                                   "SubCategoryName", "Message", "TimeStamp"])

JOB_FIELDS = f([
    ("Id", I, "Job identifier; parent key for job_execution_histories."),
    ("JobName", S, "Job name."),
    ("JobDescription", S, "Job description."),
    ("NextRun", S, "Next scheduled run (appliance timestamp text)."),
    ("LastRun", S, "Last run (appliance timestamp text)."),
    ("StartTime", S, "Start time text."),
    ("EndTime", S, "End time text."),
    ("Schedule", S, "Schedule expression (startnow or cron)."),
    ("State", S, "Enabled or Disabled."),
    ("CreatedBy", S, "Creating user."),
    ("UpdatedBy", S, "Updating user."),
    ("Visible", B, "Visible in the UI."),
    ("Editable", B, "Editable."),
    ("Builtin", B, "Built-in job."),
    ("UserGenerated", B, "Created by a user."),
    ("LastRunStatus.Id", I, "Last run status id: 2020 Scheduled, 2030 Queued, 2040 Starting, 2050 Running, 2060 Completed, 2070 Failed, 2080 New, 2090 Warning, 2100 Aborted, 2101 Paused, 2102 Stopped, 2103 Canceled."),
    ("LastRunStatus.Name", S, "Last run status name."),
    ("JobType.Id", I, "Job type id (e.g. 8 Inventory_Task, 101 Discovery_Task, 5 Update_Task)."),
    ("JobType.Name", S, "Job type name."),
    ("JobType.Internal", B, "Internal job type."),
    ("JobStatus.Id", I, "Current status id (same vocabulary as LastRunStatus)."),
    ("JobStatus.Name", S, "Current status name."),
    ("Targets", A, "Job targets: Id, Data, TargetType{Id,Name}."),
    ("Params", A, "Job parameters as Key/Value pairs; secret-like keys are removed."),
])


def resource(path, description, fields, *, collection=True, parents=None, filters=None, controls=None,
             explode=None, evidence="verified", optional_parents=None, entity=None, identity=("Id",)):
    r = OrderedDict()
    r["path"] = path
    r["collection"] = collection
    r["description"] = description
    r["parents"] = OrderedDict(parents or [])
    r["optional_parents"] = OrderedDict(optional_parents or [])
    r["controls"] = list(controls if controls is not None else (ODATA_CONTROLS if collection else []))
    r["filters"] = OrderedDict(filters or [])
    r["fields"] = fields
    r["explode"] = explode
    r["evidence"] = evidence
    r["entity"] = entity
    r["identity"] = [k for k in identity if k in fields]
    return r


DEV = {"DeviceId": {"type": "integer", "minimum": 0}}
GROUP = {"GroupId": {"type": "integer", "minimum": 0}}
BASE = {"BaselineId": {"type": "integer", "minimum": 0}}
JOB = {"JobId": {"type": "integer", "minimum": 0}}

R = OrderedDict()

R["appliance_info"] = resource(
    "/api/ApplicationService/Info",
    "Appliance identity and version (used by test_connection).",
    f([
        ("Name", S, "Appliance name."),
        ("Description", S, "Appliance description."),
        ("Vendor", S, "Vendor (Dell)."),
        ("Branding", S, "Product branding."),
        ("Version", S, "Appliance version, e.g. 4.1.0."),
        ("BuildNumber", S, "Build number."),
        ("BuildDate", S, "Build date text."),
        ("Guid", S, "Appliance GUID."),
        ("OperationStatus", S, "Appliance operational status."),
    ]), collection=False, identity=("Guid",), entity="ApplicationService.Info")

R["appliance_network"] = resource(
    "/api/ApplicationService/Network/CurrentAddressConfiguration",
    "Current appliance network address configuration (read-only view of the management interface).",
    f([
        ("InterfaceName", S, "Interface name, e.g. eth0."),
        ("PrimaryInterface", B, "Primary interface."),
        ("EnableNIC", B, "NIC enabled."),
        ("Ipv4Configuration", O, "IPv4 settings: Enable, EnableDHCP, StaticIPAddress, StaticSubnetMask, StaticGateway, DNS servers."),
        ("Ipv6Configuration", O, "IPv6 settings."),
        ("DnsConfiguration", O, "DNS name/domain and registration settings."),
        ("ManagementVLAN", O, "Management VLAN settings."),
        ("Delay", I, "Apply delay in seconds."),
    ]), collection=False, identity=("InterfaceName",), evidence="partial",
    entity="ApplicationService.CurrentAddressConfiguration")

R["devices"] = resource(
    "/api/DeviceService/Devices",
    "All devices managed or monitored by the appliance (servers, chassis, network and storage IOMs).",
    DEVICE_FIELDS, filters=DEVICE_FILTERS, entity="DeviceService.Device")

R["device_summary"] = resource(
    "/api/DeviceService/DeviceSummary",
    "Per device-type health roll-up counts.",
    f([
        ("DeviceType", I, "Device type id."),
        ("TotalCount", I, "Devices of this type."),
        ("NormalCount", I, "Devices with Status 1000."),
        ("WarningCount", I, "Devices with Status 3000."),
        ("CriticalCount", I, "Devices with Status 4000."),
        ("UnknownCount", I, "Devices with Status 2000."),
    ]), controls=[], identity=("DeviceType",), evidence="inferred", entity="DeviceService.DeviceSummary")

R["device_types"] = resource(
    "/api/DeviceService/DeviceType",
    "Device type id to name mapping.",
    f([
        ("DeviceType", I, "Device type id."),
        ("Name", S, "Type name, e.g. SERVER, CHASSIS, NETWORK_IOM."),
        ("Description", S, "Type description."),
    ]), controls=[], identity=("DeviceType",), entity="DeviceService.DeviceType")

R["device_inventory"] = resource(
    "/api/DeviceService/Devices({DeviceId})/InventoryDetails",
    "Detailed inventory of one device, one row per inventory item (processor, memory module, NIC, firmware component...). "
    "Supply parent DeviceId; add parent InventoryType (e.g. serverProcessors, serverMemoryDevices, deviceSoftware, "
    "serverNetworkInterfaces, serverPowerSupplies, serverArrayDisks) to fetch a single inventory type.",
    f([
        ("InventoryType", S, "Inventory section name."),
        ("InventoryItem", O, "One inventory item; keys differ per InventoryType (e.g. ModelName, Status, SerialNumber)."),
    ]), parents=DEV,
    optional_parents={"InventoryType": {"type": "string", "pattern": "^[A-Za-z][A-Za-z0-9]{0,63}$",
                                        "path": "/api/DeviceService/Devices({DeviceId})/InventoryDetails('{InventoryType}')",
                                        "collection": False}},
    controls=[], explode={"array": "InventoryInfo", "as": "InventoryItem"}, identity=(), evidence="partial",
    entity="DeviceService.InventoryDetail")

R["device_hardware_logs"] = resource(
    "/api/DeviceService/Devices({DeviceId})/HardwareLogs",
    "Hardware (Lifecycle/SEL) log entries of one device.",
    f([
        ("Id", I, "Log entry id."),
        ("LogSequenceNumber", I, "Sequence number."),
        ("LogSeverity", I, "Severity id (OME severity vocabulary: 2 INFO, 4 NORMAL, 8 WARNING, 16 CRITICAL)."),
        ("LogCategory", S, "Category."),
        ("LogTimeStamp", S, "Entry timestamp text."),
        ("Message", S, "Message text."),
        ("LogComment", S, "Comment."),
    ]), parents=DEV, controls=PAGE_CONTROLS, evidence="inferred", entity="DeviceService.HardwareLog")

R["device_power"] = resource(
    "/api/DeviceService/Devices({DeviceId})/Power",
    "Current power telemetry of one device (iDRAC power monitoring).",
    f([
        ("instantaneousPower", S, "Instantaneous power reading."),
        ("peakPower", S, "Peak power reading."),
        ("avgPower", S, "Average power reading."),
        ("minimumPower", S, "Minimum power reading."),
        ("powerCap", S, "Configured power cap."),
        ("peakHeadroom", S, "Peak headroom."),
        ("instantaneousHeadroom", S, "Instantaneous headroom."),
        ("unit", S, "Unit of the readings (watt)."),
        ("dateFormat", S, "Timestamp format identifier."),
        ("peakPowerTimeStamp", S, "Timestamp text of the peak."),
        ("minimumPowerTimeStamp", S, "Timestamp text of the minimum."),
        ("systemEnergyConsumption", S, "Energy consumption."),
        ("systemEnergyConsumptionTimeStamp", S, "Energy consumption timestamp text."),
    ]), collection=False, parents=DEV, identity=(), evidence="inferred", entity="DeviceService.PowerUsage")

R["device_temperature"] = resource(
    "/api/DeviceService/Devices({DeviceId})/Temperature",
    "Inlet temperature telemetry of one device.",
    f([
        ("instantaneousTemperature", S, "Instantaneous temperature."),
        ("peakTemperature", S, "Peak temperature."),
        ("avgTemperature", S, "Average temperature."),
        ("instantaneousTemperatureUnit", S, "Unit (celsius)."),
        ("peakTemperatureUnit", S, "Unit (celsius)."),
        ("avgTemperatureUnit", S, "Unit (celsius)."),
        ("peakTemperatureTimeStamp", S, "Peak timestamp text."),
        ("avgTemperatureTimeStamp", S, "Average window timestamp text."),
        ("startTime", S, "Window start text."),
        ("dateFormat", S, "Timestamp format identifier."),
    ]), collection=False, parents=DEV, identity=(), evidence="inferred", entity="DeviceService.Temperature")

R["device_system_uptime"] = resource(
    "/api/DeviceService/Devices({DeviceId})/SystemUpTime",
    "System uptime of one device.",
    f([
        ("systemUpTime", S, "Uptime text as reported by the device."),
        ("startTime", S, "Boot/start timestamp text."),
    ]), collection=False, parents=DEV, identity=(), evidence="inferred", entity="DeviceService.SystemUpTime")

R["alerts"] = resource(
    "/api/AlertService/Alerts",
    "Alert log. Filter by SeverityType (16 critical, 8 warning), StatusType, CategoryId, AlertDeviceId or TimeStamp; "
    "order with TimeStamp desc; use lookback for a recent window.",
    ALERT_FIELDS, filters=ALERT_FILTERS, entity="AlertService.Alert")

R["alert_categories"] = resource(
    "/api/AlertService/AlertCategories",
    "Alert catalogs and their categories (one row per catalog/category pair) with sub-category details.",
    f([
        ("Name", S, "Catalog name, e.g. iDRAC, Application, Networking."),
        ("IsBuiltIn", B, "Built-in catalog."),
        ("Category.Id", I, "Category id (matches alerts.CategoryId)."),
        ("Category.Name", S, "Category name."),
        ("Category.CatalogName", S, "Owning catalog name."),
        ("Category.SubCategoryDetails", A, "Sub-categories: Id, Name, Description."),
    ]), controls=[], explode={"array": "CategoriesDetails", "prefix": "Category."}, identity=("Name",),
    entity="AlertService.AlertCategory")

R["warranties"] = resource(
    "/api/WarrantyService/Warranties",
    "Warranty entitlements per device (one row per entitlement line). Date comparison in $filter is not supported; "
    "compute expiry in pandas from EndDate/DaysRemaining.",
    f([
        ("Id", I, "Warranty record id."),
        ("DeviceModel", S, "Device model."),
        ("DeviceIdentifier", S, "Service tag (join to devices.DeviceServiceTag)."),
        ("DeviceType", I, "Device type id."),
        ("CountryLookupCode", S, "Country code."),
        ("CustomerNumber", I, "Customer number."),
        ("LocalChannel", S, "Sales channel."),
        ("OrderNumber", S, "Order number."),
        ("ServiceLevelCode", S, "Service level code, e.g. ND."),
        ("ServiceLevelDescription", S, "Service level description, e.g. ProSupport Plus."),
        ("ServiceLevelGroup", I, "Service level group id."),
        ("ServiceProvider", S, "Service provider."),
        ("StartDate", S, "Entitlement start timestamp text."),
        ("EndDate", S, "Entitlement end timestamp text."),
        ("DaysRemaining", I, "Days remaining (negative or zero once expired)."),
        ("Timestamp", S, "Last warranty refresh timestamp text."),
        ("State", S, "Entitlement state text."),
    ]), filters=OrderedDict([("DeviceIdentifier", {"type": S}), ("DeviceModel", {"type": S}),
                             ("ServiceLevelDescription", {"type": S}), ("DeviceType", {"type": I})]),
    evidence="partial", entity="WarrantyService.Warranty")

R["firmware_baselines"] = resource(
    "/api/UpdateService/Baselines",
    "Firmware baselines with their compliance summary. Parent key for firmware_compliance_reports.",
    f([
        ("Id", I, "Baseline id."),
        ("Name", S, "Baseline name."),
        ("Description", S, "Description."),
        ("CatalogId", I, "Catalog id (join to firmware_catalogs.Id)."),
        ("RepositoryId", I, "Repository id."),
        ("RepositoryName", S, "Repository name."),
        ("RepositoryType", S, "Repository type: DELL_ONLINE, HTTPS, CIFS, NFS..."),
        ("TaskId", I, "Compliance check job id (join to jobs.Id)."),
        ("TaskStatusId", I, "Compliance job status id (2060 Completed, 2070 Failed...)."),
        ("LastRun", S, "Last compliance run timestamp text."),
        ("DowngradeEnabled", B, "Downgrades allowed."),
        ("Is64Bit", B, "64-bit DUPs."),
        ("Targets", A, "Baseline targets: Id, Type{Id,Name} (1000 DEVICE, 6000 GROUP)."),
        ("ComplianceSummary.ComplianceStatus", S, "OK, WARNING, CRITICAL, DOWNGRADE, UNKNOWN."),
        ("ComplianceSummary.NumberOfCritical", I, "Devices with critical updates."),
        ("ComplianceSummary.NumberOfWarning", I, "Devices with recommended updates."),
        ("ComplianceSummary.NumberOfNormal", I, "Compliant devices."),
        ("ComplianceSummary.NumberOfDowngrade", I, "Devices ahead of the baseline."),
    ]), controls=PAGE_CONTROLS, entity="UpdateService.Baselines")

R["firmware_compliance_reports"] = resource(
    "/api/UpdateService/Baselines({BaselineId})/DeviceComplianceReports",
    "Firmware compliance of every device in a baseline, flattened to one row per component report "
    "(devices without component reports keep one row with empty Component.* columns).",
    f([
        ("Id", I, "Device compliance report id."),
        ("DeviceId", I, "Device id (join to devices.Id)."),
        ("ServiceTag", S, "Service tag."),
        ("DeviceModel", S, "Model."),
        ("DeviceName", S, "Device name."),
        ("DeviceTypeId", I, "Device type id."),
        ("DeviceTypeName", S, "Device type name."),
        ("FirmwareStatus", S, "Compliant or Non-Compliant."),
        ("ComplianceStatus", S, "OK, WARNING, CRITICAL, DOWNGRADE."),
        ("RebootRequired", B, "Device needs a reboot for the updates."),
        ("Component.Id", I, "Component report id."),
        ("Component.Name", S, "Component name."),
        ("Component.CurrentVersion", S, "Installed version."),
        ("Component.Version", S, "Baseline (catalog) version."),
        ("Component.ComplianceStatus", S, "OK, WARNING, CRITICAL, DOWNGRADE."),
        ("Component.UpdateAction", S, "UPGRADE, DOWNGRADE, EQUAL."),
        ("Component.Criticality", S, "Urgent, Recommended, Optional, Ok."),
        ("Component.RebootRequired", B, "Component update needs a reboot."),
        ("Component.SourceName", S, "Device-side component instance id."),
        ("Component.TargetIdentifier", S, "Catalog component id."),
        ("Component.ComponentType", S, "FIRMWARE, APPLICATION, DRIVER..."),
        ("Component.Path", S, "Catalog path of the DUP."),
        ("Component.PrerequisiteInfo", S, "Prerequisite information."),
        ("Component.ImpactAssessment", S, "Impact assessment."),
        ("Component.UniqueIdentifier", S, "DUP unique identifier."),
        ("Component.Uri", S, "Support URL."),
        ("Component.ComplianceDependencies", A, "Dependency entries."),
    ]), parents=BASE, controls=PAGE_CONTROLS,
    explode={"array": "ComponentComplianceReports", "prefix": "Component."},
    entity="UpdateService.DeviceComplianceReport")

R["firmware_catalogs"] = resource(
    "/api/UpdateService/Catalogs",
    "Firmware catalogs (repository definitions) used by baselines.",
    f([
        ("Id", I, "Catalog id."),
        ("Filename", S, "Catalog file name."),
        ("SourcePath", S, "Path in the repository."),
        ("Status", S, "Catalog status text."),
        ("BaseLocation", S, "Base location."),
        ("ManifestVersion", S, "Manifest version."),
        ("ManifestIdentifier", S, "Manifest identifier."),
        ("ReleaseDate", S, "Release date text."),
        ("ReleaseIdentifier", S, "Release identifier."),
        ("PredecessorIdentifier", S, "Predecessor identifier."),
        ("LastUpdated", S, "Last update timestamp text."),
        ("NextUpdate", S, "Next update timestamp text."),
        ("BundlesCount", I, "Bundles in the catalog."),
        ("TaskId", I, "Catalog refresh job id."),
        ("Schedule", O, "Refresh schedule."),
        ("Repository.Id", I, "Repository id."),
        ("Repository.Name", S, "Repository name."),
        ("Repository.Description", S, "Repository description."),
        ("Repository.Source", S, "Repository source host."),
        ("Repository.DomainName", S, "Share domain."),
        ("Repository.Username", S, "Share user name (never the password)."),
        ("Repository.CheckCertificate", B, "Certificate validation."),
        ("Repository.RepositoryType", S, "DELL_ONLINE, HTTPS, HTTP, CIFS, NFS."),
        ("AssociatedBaselines", A, "BaselineId/BaselineName pairs."),
    ]), controls=PAGE_CONTROLS, entity="UpdateService.Catalogs")

R["jobs"] = resource(
    "/api/JobService/Jobs",
    "Appliance jobs (inventory, discovery, firmware update, compliance...). $orderby is not supported by OME; "
    "sort in pandas. Parent key for job_execution_histories.",
    JOB_FIELDS, controls=["$top", "$skip", "$filter"],
    filters=flt(JOB_FIELDS, ["Id", "JobName", "State", "CreatedBy", "LastRunStatus.Id", "JobType.Id", "JobType.Name", "JobType.Internal", "JobStatus.Id"]),
    entity="JobService.Job")

R["job_execution_histories"] = resource(
    "/api/JobService/Jobs({JobId})/ExecutionHistories",
    "Execution history (runs) of one job.",
    f([
        ("Id", I, "Execution history id; parent key for job_execution_details."),
        ("JobId", I, "Job id."),
        ("JobName", S, "Job name."),
        ("Progress", S, "Progress percentage text."),
        ("StartTime", S, "Run start timestamp text."),
        ("EndTime", S, "Run end timestamp text."),
        ("LastUpdateTime", S, "Last update timestamp text."),
        ("ExecutedBy", S, "User who ran the job."),
        ("JobStatus.Id", I, "Run status id (2060 Completed, 2070 Failed, 2090 Warning...)."),
        ("JobStatus.Name", S, "Run status name."),
    ]), parents=JOB, controls=PAGE_CONTROLS, entity="JobService.ExecutionHistory")

R["job_execution_details"] = resource(
    "/api/JobService/Jobs({JobId})/ExecutionHistories({ExecutionHistoryId})/ExecutionHistoryDetails",
    "Per-target detail of one job run, including the full output text (Value) that explains failures.",
    f([
        ("Id", I, "Detail id."),
        ("ExecutionHistoryId", I, "Execution history id."),
        ("IdBaseEntity", I, "Target entity id (device id when applicable)."),
        ("Key", S, "Target key (device name/IP/service tag)."),
        ("Value", S, "Output text of the run for this target."),
        ("Progress", S, "Progress percentage text."),
        ("StartTime", S, "Start timestamp text."),
        ("EndTime", S, "End timestamp text."),
        ("ElapsedTime", S, "Elapsed time text."),
        ("JobStatus.Id", I, "Status id."),
        ("JobStatus.Name", S, "Status name."),
    ]), parents=OrderedDict([("JobId", JOB["JobId"]), ("ExecutionHistoryId", {"type": "integer", "minimum": 0})]),
    controls=PAGE_CONTROLS, entity="JobService.ExecutionHistoryDetail")

GROUP_FIELDS = f([
    ("Id", I, "Group id; parent key for group_devices."),
    ("Name", S, "Group name."),
    ("Description", S, "Description."),
    ("GlobalStatus", I, "Rollup health of member devices (same vocabulary as devices.Status)."),
    ("ParentId", I, "Parent group id."),
    ("CreationTime", S, "Creation timestamp text."),
    ("UpdatedTime", S, "Update timestamp text."),
    ("CreatedBy", S, "Creating user."),
    ("UpdatedBy", S, "Updating user."),
    ("Visible", B, "Visible in the UI."),
    ("DefinitionId", I, "Group definition id."),
    ("DefinitionDescription", S, "Definition description."),
    ("TypeId", I, "Group type id: 1000 internal, 2000 built-in system, 3000 user-defined."),
    ("MembershipTypeId", I, "12 static membership, 24 query (dynamic) membership."),
    ("HasAttributes", B, "Group carries attributes."),
])
R["groups"] = resource(
    "/api/GroupService/Groups",
    "Static and query device groups (built-in and user defined).",
    GROUP_FIELDS, filters=flt(GROUP_FIELDS, ["Id", "Name", "Description", "TypeId", "MembershipTypeId", "ParentId"]),
    entity="GroupService.Group")

R["group_devices"] = resource(
    "/api/GroupService/Groups({GroupId})/Devices",
    "Direct member devices of one group (device records).",
    DEVICE_FIELDS, parents=GROUP, controls=PAGE_CONTROLS, entity="DeviceService.Device")

R["group_all_leaf_devices"] = resource(
    "/api/GroupService/Groups({GroupId})/AllLeafDevices",
    "All devices under one group including sub-groups (device records).",
    DEVICE_FIELDS, parents=GROUP, controls=PAGE_CONTROLS, entity="DeviceService.Device")

R["audit_logs"] = resource(
    "/api/ApplicationService/AuditLogs",
    "Appliance audit log (logins, configuration changes, job submissions).",
    f([
        ("Id", I, "Audit entry id."),
        ("Severity", I, "Severity id (OME severity vocabulary)."),
        ("Message", S, "Message text."),
        ("Category", S, "Category text (Audit, Configuration...)."),
        ("UserName", S, "Acting user."),
        ("IpAddress", S, "Client address."),
        ("MessageArgs", S, "Message arguments."),
        ("MessageID", S, "EEMI message id, e.g. CUSR1100."),
        ("CreatedDate", S, "Entry timestamp text."),
    ]), controls=["$top", "$skip", "$filter"],
    filters=OrderedDict([("Severity", {"type": I}), ("Category", {"type": S}), ("UserName", {"type": S}),
                         ("MessageID", {"type": S}), ("CreatedDate", {"type": S})]),
    entity="ApplicationService.AuditLog")

R["templates"] = resource(
    "/api/TemplateService/Templates",
    "Deployment and compliance templates.",
    f([
        ("Id", I, "Template id."),
        ("Name", S, "Template name."),
        ("Description", S, "Description."),
        ("SourceDeviceId", I, "Reference device id."),
        ("TypeId", I, "Template type id (2 server, 4 chassis)."),
        ("ViewTypeId", I, "View type id (2 deployment, 1 compliance, 4 ...)."),
        ("TaskId", I, "Creation job id."),
        ("HasIdentityAttributes", B, "Carries identity attributes."),
        ("Status", I, "Status id."),
        ("IdentityPoolId", I, "Identity pool id."),
        ("IsBuiltIn", B, "Built-in."),
        ("IsPersistencePolicyValid", B, "Persistence policy valid."),
        ("IsStatelessAvailable", B, "Stateless deployment available."),
        ("CreatedBy", S, "Creating user."),
        ("CreationTime", S, "Creation timestamp text."),
        ("LastUpdatedBy", S, "Updating user."),
        ("LastUpdatedTime", S, "Update timestamp text."),
    ]), controls=["$top", "$skip", "$filter"],
    filters=OrderedDict([("Id", {"type": I}), ("Name", {"type": S}), ("ViewTypeId", {"type": I}), ("TypeId", {"type": I})]),
    entity="TemplateService.Template")

R["configuration_baselines"] = resource(
    "/api/TemplateService/Baselines",
    "Configuration compliance baselines (template vs device attributes). Parent key for configuration_compliance_reports.",
    f([
        ("Id", I, "Baseline id."),
        ("Name", S, "Baseline name."),
        ("Description", S, "Description."),
        ("TemplateId", I, "Compliance template id (join to templates.Id)."),
        ("TemplateName", S, "Template name."),
        ("TemplateType", I, "Template type id."),
        ("LastRun", S, "Last compliance run timestamp text."),
        ("TaskId", I, "Compliance job id."),
        ("TaskStatus", I, "Compliance job status id."),
        ("PercentageComplete", S, "Progress text."),
        ("BaselineTargets", A, "Targets: Id, Type{Id,Name}."),
        ("ConfigComplianceSummary.ComplianceStatus", S, "COMPLIANT, NONCOMPLIANT, NOT_INVENTORIED..."),
        ("ConfigComplianceSummary.NumberOfCritical", I, "Non-compliant devices."),
        ("ConfigComplianceSummary.NumberOfWarning", I, "Devices with warnings."),
        ("ConfigComplianceSummary.NumberOfNormal", I, "Compliant devices."),
        ("ConfigComplianceSummary.NumberOfIncomplete", I, "Devices not yet inventoried."),
    ]), controls=PAGE_CONTROLS, evidence="partial", entity="TemplateService.Baseline")

R["configuration_compliance_reports"] = resource(
    "/api/TemplateService/Baselines({BaselineId})/DeviceConfigComplianceReports",
    "Per-device configuration compliance status for one baseline.",
    f([
        ("Id", I, "Device id (join to devices.Id)."),
        ("DeviceName", S, "Device name."),
        ("ServiceTag", S, "Service tag."),
        ("Model", S, "Model."),
        ("DeviceType", I, "Device type id."),
        ("ComplianceStatus", S, "COMPLIANT or NONCOMPLIANT."),
        ("InventoryTime", S, "Inventory timestamp text."),
    ]), parents=BASE, controls=PAGE_CONTROLS, entity="TemplateService.DeviceConfigComplianceReport")

R["management_domains"] = resource(
    "/api/ManagementDomainService/Domains",
    "Multi-chassis management (MCM) domains known to the appliance. Standalone OME appliances typically return no rows.",
    f([
        ("Id", I, "Domain id."),
        ("DeviceId", I, "Chassis device id (join to devices.Id)."),
        ("PublicAddress", A, "Public management addresses."),
        ("Name", S, "Domain name."),
        ("Description", S, "Description."),
        ("Identifier", S, "Chassis service tag."),
        ("DomainTypeId", I, "Domain type id."),
        ("DomainTypeValue", S, "Domain type (MSM_GROUP...)."),
        ("DomainRoleTypeId", I, "Role id."),
        ("DomainRoleTypeValue", S, "LEAD, MEMBER, BACKUPLEAD or STANDALONE."),
        ("Version", S, "OME-Modular version of the chassis."),
        ("Local", B, "Local domain."),
        ("GroupId", S, "MCM group id."),
        ("GroupName", S, "MCM group name."),
        ("BackupLead", B, "Chassis is the backup lead."),
        ("BackupLeadHealth", I, "Backup lead health id."),
        ("Capabilities", A, "Capability ids."),
    ]), controls=PAGE_CONTROLS, entity="ManagementDomainService.Domain")


def main():
    catalog = OrderedDict()
    catalog["contract"] = CONTRACT
    catalog["version"] = 1
    catalog["source"] = "tools/dell_ome/generate_catalog.py; evidence in tools/dell_ome/SOURCES.md"
    catalog["resources"] = R
    body = json.dumps({k: v for k, v in catalog.items()}, indent=1, sort_keys=False)
    catalog["sha256"] = hashlib.sha256(body.encode()).hexdigest()
    OUT.write_text(json.dumps(catalog, indent=1, sort_keys=False) + "\n")
    print(f"wrote {OUT} with {len(R)} tables, sha256 {catalog['sha256'][:12]}")


if __name__ == "__main__":
    main()
