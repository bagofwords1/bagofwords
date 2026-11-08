# Evidence record — Dell OpenManage Enterprise connector

Target: OME 4.1 (REST API, OData v4 flavoured). The official PDF guides on
`dl.dell.com/topicspdf/...API-Guide*.pdf` and the HTML topics on
`dell.com/support/manuals` returned HTTP 403 / "Data is not available for the
Topic" from this environment (Akamai blocks the egress proxy; the HTML pages
only served their table of contents). Everything below therefore comes from
Dell's own open-source clients, which carry real request/response shapes, plus
community material. Each table in `catalog.json` carries an `evidence` field:

- **verified** — exact field names seen in Dell sample code / fixtures.
- **partial** — endpoint and most fields verified; some fields recalled from
  the API guide and not re-confirmed.
- **inferred** — endpoint path verified (sample scripts / guide TOC) but the
  response shape could not be confirmed; field names are best effort and MUST
  be checked on a real appliance.

## Sources used

1. `dell/dellemc-openmanage-ansible-modules` (cloned, `collections` branch):
   - `plugins/module_utils/ome.py` — session creation `POST SessionService/Sessions`
     with `{'UserName','Password','SessionType':'API'}`, token read from the
     `X-Auth-Token` **response header**, `Id` from the body, logout
     `DELETE SessionService/Sessions('{Id}')`; default headers
     `Content-Type/Accept: application/json`; `get_all_items_with_pagination`
     follows `@odata.nextLink` and reads `@odata.count`; `get_all_report_details`
     pages with `$top`/`$skip`; `_validate_odata_filter` (operators
     `eq ne gt lt ge le`, `and`/`or`, parentheses, `''` escaping);
     `DeviceServiceTag eq '...'` filter; `DeviceService/DeviceType` → `DeviceType`/`Name`;
     job status map 2020..2103; `JobService/Jobs({id})/ExecutionHistories`.
   - `plugins/modules/ome_session.py` — 201 on create, 204 on delete, sample
     session body (`Id, Description, Name, UserName, UserId, Password, Roles,
     IpAddress, StartTimeStamp, LastAccessedTimeStamp, DirectoryGroup`), error
     envelope `error.code/message/@Message.ExtendedInfo[MessageId, Message,
     MessageArgs, RelatedProperties, Resolution, Severity]`.
   - `plugins/modules/ome_device_info.py` — full device record sample
     (`Id, Type, Identifier, DeviceServiceTag, ChassisServiceTag, Model,
     PowerState, ManagedState, Status, ConnectionState, AssetTag, SystemId,
     DeviceName, LastInventoryTime, LastStatusTime, DeviceSubscription,
     DeviceCapabilities, DeviceManagement[...ManagementProfile[...]],
     SlotConfiguration`), paths `Devices({Id})/InventoryDetails`,
     `InventoryDetails('{InventoryType}')`, `SubSystemHealth`.
   - `plugins/modules/ome_job_info.py` — job, ExecutionHistories and
     ExecutionHistoryDetails samples (`Key`, `Value` with EEMI text, `Progress`,
     `ElapsedTime`, `IdBaseEntity`, `JobStatus{Id,Name}`).
   - `plugins/modules/ome_firmware_baseline_info.py`,
     `ome_firmware_baseline_compliance_info.py` — baseline and
     `Baselines({Id})/DeviceComplianceReports` samples incl.
     `ComponentComplianceReports[...]`, `ComplianceSummary{...}`, `Targets[]`.
   - `plugins/modules/ome_firmware_catalog.py` — catalog record sample
     (`Repository{...}`, `Schedule`, `AssociatedBaselines`, ...).
   - `plugins/modules/ome_template_info.py` — template sample;
     `ome_configuration_compliance_info.py` — `TemplateService/Baselines`,
     `.../DeviceConfigComplianceReports` sample (`Id, DeviceName, DeviceType,
     Model, ServiceTag, ComplianceStatus, InventoryTime`).
   - `plugins/modules/ome_alert_policies_category_info.py` and its unit test —
     `AlertService/AlertCategories` shape (`Name, IsBuiltIn, CategoriesDetails[
     Id, Name, CatalogName, SubCategoryDetails[Id, Name, Description]]`,
     `@odata.id` with quoted name).
   - `plugins/modules/ome_application_network_address.py` —
     `Network/CurrentAddressConfiguration`-style body fields.
   - `plugins/modules/ome_application_network_settings.py` — session settings
     sample: API `MaxSessions` 32, `MaxSessionsAllowed` 100 (so the cap is
     configurable, 1–100; GUI default 6).
   - `plugins/modules/ome_diagnostics.py`, `ome_smart_fabric.py`, unit tests —
     `ManagementDomainService/Domains` fields (`DeviceId`, `DomainRoleTypeValue`
     LEAD/MEMBER/STANDALONE, `PublicAddress[]`, `Version`).
   - `plugins/inventory/ome_inventory.py` — `AllLeafDevices@odata.navigationLink`,
     `SubGroups@odata.navigationLink` on group records.
2. `dell/OpenManage-Enterprise` sample scripts (cloned): `Python/get_device_list.py`
   (201 + `X-Auth-Token`, `@odata.count`, `@odata.nextLink` relative to host),
   `get_alerts.py` (filters `Id, AlertDeviceId, AlertDeviceIdentifier,
   AlertDeviceType, SeverityType, StatusType, CategoryId, SubCategoryId,
   SubCategoryName, AlertDeviceName, Message, TimeStamp ge/le 'YYYY-MM-DD
   HH:MM:SS.ss', AlertDeviceGroup`; `$top/$skip/$orderby`; severity map
   1/2/4/8/16, status map 1000..5000, device type map, category id map),
   `get_warranty_information.py` (`WarrantyService/Warranties`,
   `ServiceLevelDescription`), `get_audit_logs.py` (`ApplicationService/AuditLogs`,
   columns `Severity, Message, Category, UserName, IpAddress, MessageArgs,
   MessageID, CreatedDate`), `get_firmware_baselines.py`, `get_job.py`
   (`ExecutionHistoryDetails@odata.navigationLink`), `get_group_list.py`,
   `get_device_inventory.py`, `get_chassis_inventory.py` (`$count=true&$top=0`).
3. `dell/OpenManage-PowerShell-Modules` (cloned): `Connect-OMEServer.ps1`
   (200/201, `X-Auth-Token`), `Disconnect-OMEServer.ps1` (DELETE → 204),
   `Get-OMEDevice.ps1` (filters quoted/unquoted by type; group paging with
   `$skip/$top`; nextLink re-appended with `&$filter`), `Get-OMEJob.ps1`,
   `Get-OMEGroup.ps1`, `Get-OMEAlert.ps1`, `Get-OMEFirmwareCompliance.ps1`,
   `Get-OMEDeviceDetail.ps1` (`/InventoryTypes`, 400 when type not applicable),
   and the `Private/New-*FromJson.ps1` mappers for appliance info (`Name,
   Description, Vendor, Branding, Version, BuildNumber, BuildDate, Guid,
   OperationStatus`), domains, catalogs, groups (`GlobalStatus` not mapped — see
   partial), jobs, templates, firmware/configuration baselines.
4. `clearminds/clr-openmanage-mcp` (cloned): endpoints used, `$orderby=TimeStamp
   desc` on alerts, note that Jobs does not support `$orderby` and warranties do
   not support date comparison in `$filter`.
5. Dell Community: "OME 3.8.0 API dropping devices" — moderator confirms the
   Devices collection returns **50 records by default**; `$skip=50&$top=50`,
   `$top=1000` examples; guide quote `?$orderby=Identifier&$top=50&$skip=0`.
   <https://www.dell.com/community/Dell-OpenManage-Enterprise/OME-3-8-0-API-dropping-devices/m-p/8077797>
6. Dell Community "OME Rest API" thread (attribute names on device records
   including `Temperature`, `Power`, `SystemUpTime`,
   `HardwareLogs@odata.navigationLink`; numeric codes undocumented) and
   "rest-api-inventory-details" (serverProcessors / serverMemoryDevices /
   deviceManagement `inventoryInfo` field names).
7. Dell support manuals TOC for the 3.2 API guide (endpoint list only):
   `/api/DeviceService/DeviceSummary`, `Devices(Id)/HardwareLogs`,
   `Devices(Id)/Temperature`, `Devices(Id)/Power`, `Devices(Id)/SystemUpTime`,
   `Groups(Id)/Devices`, `Groups(Id)/AllLeafDevices`, `JobService/Jobs(Id)/
   ExecutionHistories`, `ApplicationService/Info`, `WarrantyService/Warranties`,
   `ManagementDomainService/Domains`, `ReportService/ReportDefs`, chapters
   "Data pagination", "Authentication", "Response codes".
   <https://www.dell.com/support/manuals/en-us/dell-openmanage-enterprise/ome-3.2_omem-1.00.10_apiguide/get-method-for-validationerrors>
8. Dell Info Hub "A beginner's guide to working with Dell OpenManage Enterprise
   API and Postman" (search snippet): OME 3.10 guide applies to 4.0+ and moved
   to the Dell Developer Portal; `Devices(nnnn)/HardwareLogs` example.

## Verification status per table

| table | path | evidence | note |
|---|---|---|---|
| appliance_info | /api/ApplicationService/Info | verified (PowerShell mapper) | |
| appliance_network | /api/ApplicationService/Network/CurrentAddressConfiguration | partial | body taken from the PUT/GET network-address sample |
| devices | /api/DeviceService/Devices | verified | |
| device_summary | /api/DeviceService/DeviceSummary | **inferred** | path in guide TOC; `*Count` field names unverified |
| device_types | /api/DeviceService/DeviceType | verified (`DeviceType`,`Name`); `Description` inferred | |
| device_inventory | Devices({Id})/InventoryDetails[('{type}')] | partial | `InventoryType`/`InventoryInfo` verified; item keys vary per type |
| device_hardware_logs | Devices({Id})/HardwareLogs | **inferred** | field names recalled from the API guide |
| device_power | Devices({Id})/Power | **inferred** | lowercase field names recalled from the API guide |
| device_temperature | Devices({Id})/Temperature | **inferred** | as above |
| device_system_uptime | Devices({Id})/SystemUpTime | **inferred** | as above |
| alerts | /api/AlertService/Alerts | verified (filters) / partial (record) | the brief's `AlertMessage` is `Message` in the sample scripts |
| alert_categories | /api/AlertService/AlertCategories | verified | |
| warranties | /api/WarrantyService/Warranties | partial | only `ServiceLevelDescription`, `DeviceIdentifier`, `DeviceModel`, `StartDate`, `EndDate` seen in code; other fields from the brief |
| firmware_baselines | /api/UpdateService/Baselines | verified | |
| firmware_compliance_reports | Baselines({Id})/DeviceComplianceReports | verified | no server-side `$filter` assumed |
| firmware_catalogs | /api/UpdateService/Catalogs | verified | |
| jobs | /api/JobService/Jobs | verified | `$orderby` unsupported (MCP note) |
| job_execution_histories | Jobs({Id})/ExecutionHistories | verified | |
| job_execution_details | ExecutionHistories({Id})/ExecutionHistoryDetails | verified | |
| groups | /api/GroupService/Groups | partial | `GlobalStatus` inferred, rest verified |
| group_devices / group_all_leaf_devices | Groups({Id})/Devices, /AllLeafDevices | verified (paths) | device records |
| audit_logs | /api/ApplicationService/AuditLogs | verified | |
| templates | /api/TemplateService/Templates | verified | |
| configuration_baselines | /api/TemplateService/Baselines | partial | `ConfigComplianceSummary`, `TaskStatus`, `PercentageComplete` inferred. **The brief's `/api/ConfigurationService/Baselines` does not exist in any client; the real path is `TemplateService/Baselines`.** |
| configuration_compliance_reports | Baselines({Id})/DeviceConfigComplianceReports | verified | |
| management_domains | /api/ManagementDomainService/Domains | verified (PowerShell mapper) | standalone OME returns an empty collection |

## Items that could not be verified (simulator choices)

- **MessageIds**: `CUSR1001` (session cap, from the brief), `CGEN1001` (401),
  `CGEN1002` (400), `CGEN1004` (404), `CGEN1005` (403) are plausible but
  unverified; only `CGEN1008`, `CGEN1009`, `CUPD3090`, `CUSR1233`, `GEN1234`,
  `IDRAC.2.7.SYS415` appear in Dell code. The connector keys off HTTP status
  (and `CUSR1001` for the session-cap code) — never off message text.
- Status code of the session-cap error: the brief says 400/401; the simulator
  returns **400**.
- Whether `@odata.nextLink` preserves `$filter`: PowerShell clients re-append
  the filter, so the simulator omits it and the connector re-applies its scope.
- `Content-Type: application/json; odata.metadata=minimal` and `OData-Version:
  4.0` headers are standard OData but not confirmed for OME.
- `$top` upper bound: none documented; the connector uses pages of 100.
- `$filter` on nested properties (`LastRunStatus/Id`) follows OData syntax but
  was not confirmed against OME.
- Roles in the session body (`VIEWER`) — Dell samples show `ADMINISTRATOR` /
  `BACKUP_ADMINISTRATOR`.
