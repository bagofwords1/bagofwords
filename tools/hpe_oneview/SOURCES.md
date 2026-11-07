# Evidence for the HPE OneView connector

Catalog reference: **HPE OneView 9.00 / Synergy Composer 9.00, X-API-Version 6800**
(the API is backwards compatible; the client negotiates `currentVersion` and
clamps to 1200–7200, the range the catalog's paths and fields are known for).
The official REST API Reference (support.hpe.com/techlib …/oneview9.0/cicf-api/)
is a JavaScript application whose content could not be fetched from this
environment (301 → empty page), so the primary evidence is HPE's own SDKs,
whose request/response handling and fixtures encode the wire format.

## Protocol (verified)

| Fact | Source |
|---|---|
| `GET /rest/version` → `{"currentVersion","minimumVersion"}`, served without `X-API-Version`; SDK uses `currentVersion` when no version is pinned and rejects versions outside `[minimumVersion, currentVersion]` | https://raw.githubusercontent.com/HewlettPackard/oneview-python/master/hpeOneView/connection.py (`get_default_api_version`, `validateVersion`) |
| Headers `X-API-Version`, `Accept: application/json`, `Content-Type: application/json`; session token sent in header `auth` (case-insensitive, i.e. `Auth`) | connection.py `__init__`, `set_session_id`, `post_multipart` (`putheader('auth', …)`); https://raw.githubusercontent.com/HewlettPackard/oneview-golang/master/ov/auth.go (`AuthHeader{… auth}`) |
| Login `POST /rest/login-sessions` body `{"userName","password","authLoginDomain","loginMsgAck":true}` → body `sessionID`; logout `DELETE /rest/login-sessions` | connection.py `login`/`logout`; auth.go `Auth`/`Session` structs; Zabbix "HPE Synergy by HTTP" template JavaScript https://raw.githubusercontent.com/zabbix/zabbix/master/templates/server/hpe_synergy_http/template_server_hpe_synergy_http.yaml (literal `Auth:` header, `X-API-Version`, `DELETE rest/login-sessions`) |
| Login response shape `{"partnerData":{}, "sessionID": …}` | brief + SDK reads `body['sessionID']`; `partnerData` from the brief (not independently verified) |
| Sessions expire 24 h after last use; 401 when `Auth` missing; logout returns 204 | HPE Developer blog "Authenticating against the Composable API" https://developer.hpe.com/blog/authenticating-against-the-composable-api/ |
| Collection envelope `{type, category, members[], start, count, total, prevPageUri, nextPageUri, uri, created, modified, eTag}` | oneview-golang `*List` structs (e.g. `ServerHardwareList`, `EnclosureList`, `TasksList`); connection.py `get` reads `nextPageUri/prevPageUri/total/count` |
| Paging: follow `nextPageUri` until null, stop when `nextPageUri == uri`; `count=-1` requests "all" but the SDK still pages | resource.py `do_requests_to_getall`, `get_next_page`, `build_query_uri` docstring ("the actual number of items in the response might differ from the requested count") |
| Query parameters `start`, `count`, `filter` (repeated: `&filter=`), `query`, `sort`, `view`, `fields`, `scopeUris`; URL-encoded | resource.py `build_query_uri`, `make_query_filter` |
| Filter syntax `"name='value'"` (double-quoted expression, single-quoted literal; single quote inside a literal unsupported), `matches '…%'`, `OR` | resource.py `get_by` (`"\"{0}='{1}'\""`), docstring "Single quote inside a query parameter is not supported"; examples/tasks.py `filter="status=Warning OR status=OK"`; oneview-ansible test `components.componentVersion matches '1.2%'` https://raw.githubusercontent.com/HewlettPackard/oneview-ansible/master/test/test_oneview_server_hardware_facts.py; forum reports `alertState EQ 'Active'` also accepted (not used) |
| Sort `field:ascending`/`field:descending` in SDK tests; the connector sends `field:asc`/`field:desc` (API reference form quoted in the brief; the simulator accepts both) — **direction token partially inferred** | tests/unit/resources/activity/test_alerts.py, test_tasks.py |
| Error body `{errorCode, message, details, messageParameters, recommendedActions[], errorSource, nestedErrors[], data{}}`; 405 `HTTP_METHOD_NOT_ALLOWED` text used verbatim by the simulator | Zabbix issue ZBX-22139 https://support.zabbix.com/si/jira.issueviews:issue-html/ZBX-22139/ZBX-22139.html (real appliance body); oneview-golang `TaskError` struct |
| `AUTHN_AUTH_FAIL` "Invalid username or password." on bad login; `AUTHN_AUTH_TOKEN_INVALID` on a bad token; `RESOURCE_NOT_FOUND`; `UNSUPPORTED_API_VERSION` (412) | brief (HPE REST reference); HTTP status for the login failure (400) **inferred** from the Python SDK raising on `>= 400` without distinguishing 400/401 — the client maps both to `AuthenticationFailed` |

## Resources and fields

| Table | Path | Evidence |
|---|---|---|
| appliance_version | /rest/appliance/nodeinfo/version | oneview-python `appliance_node_information.py`; HPE Community thread "get current firmware version" (fields `softwareVersion`, `modelNumber`, `major`, `minor`, `build`, `platformType`, type `NodeVersionDto4`, `family`, `compatibility`) via search summary; `serialNumber` from the brief |
| appliance_health | /rest/appliance/health-status | oneview-python `appliance_health_status.py` (URI only). Forum "hpe-oneview-nodeinfo" reports CPU/memory/disk entries each with a status message. **Member field names (`resourceName`, `status`, `statusMessage`, `available`, `capacity`) are inferred**; the table keeps the raw `member` object so nothing is lost on a real appliance |
| server_hardware | /rest/server-hardware | oneview-golang `ov/server_hardware.go` `ServerHardware` struct (all scalar fields), `mpHostInfo` from the brief/API reference, Zabbix Synergy template JSONPaths (`memoryMb`, `processorCount`, `powerState`, `processorType`…) |
| server_hardware_firmware | /rest/server-hardware/*/firmware, /{id}/firmware | oneview-python `server_hardware.py` `get_all_firmwares`/`get_firmware`; oneview-golang `ServerFirmware`/`Component` (`componentKey, componentLocation, componentName, componentVersion`, `serverName`, `serverModel`, `serverHardwareUri`) |
| server_hardware_utilization | /rest/server-hardware/{id}/utilization | oneview-python `resource.py` `get_utilization` docstring (`fields=METRIC,…`, `filter=startDate=…,endDate=…`, `view=native|hour|day`, `sliceStartTime/sliceEndTime/newestSampleTime/oldestSampleTime/refreshTaskUri`); oneview-ansible server_hardware_facts utilization option (`AveragePower`, `startDate=`, `view: day`); `metricList[].metricName/metricSamples[[epochMs,value]]/metricCapacity`, `resolution`, `isFresh` from the brief (API reference) — **sample array shape not independently verified** |
| server_hardware_types | /rest/server-hardware-types | oneview-golang `ov/server_hardware_type.go` |
| server_profiles | /rest/server-profiles | oneview-golang `ov/profiles.go` `ServerProfile` + nested option structs |
| server_profile_templates | /rest/server-profile-templates | oneview-python `server_profile_templates.py`; same nested structs as profiles; `serverProfileDescription` from profiles.go |
| enclosures | /rest/enclosures | oneview-golang `ov/enclosure.go` (`Enclosure`, `DeviceBayMap`, `FanBay`, `PowerSupplyBay`, `InterconnectBay`, `ManagerBay`, `ApplianceBay`); Zabbix template JSONPaths |
| enclosure_groups | /rest/enclosure-groups | oneview-golang `ov/enclosure_group.go` |
| logical_enclosures | /rest/logical-enclosures | oneview-golang `ov/logical_enclosure.go` |
| interconnects / interconnect_ports | /rest/interconnects | oneview-golang `ov/interconnect.go` (`Interconnect`, `Port`, `Neighbor`, `FcPortProperties`, `InterconnectLocation`) |
| logical_interconnects | /rest/logical-interconnects | oneview-golang `ov/logical_interconnects.go` |
| sas_interconnects | /rest/sas-interconnects | oneview-python `sas_interconnects.py` (URI, `refreshState`, `deviceResetState`), oneview-ansible-collection unit fixture (name/uri). **`hardwareVersion`, `sasLogicalInterconnectUri` inferred** from the API reference naming; other fields mirror `Interconnect` |
| uplink_sets | /rest/uplink-sets | oneview-golang `ov/uplink_set.go` |
| ethernet_networks / fc_networks / fcoe_networks / network_sets | /rest/… | oneview-golang `ethernet_network.go`, `fc_network.go`, `fcoe_network.go`, `network_set.go` |
| racks | /rest/racks | oneview-python `racks.py` + `tests/unit/resources/facilities/test_racks.py` (`depth`, `height`, `width`, `serialNumber`, `uuid`); **`uHeight`, `thermalLimit`, `rackMounts[]` inferred** |
| datacenters | /rest/datacenters | oneview-python `datacenters.py` (`width`, `depth`); **cooling/cost/derating/`contents[]` inferred** |
| power_devices | /rest/power-devices | oneview-python `power_devices.py` (URI, filters); **`deviceType`, `capacity`, `feedIdentifier`, `lineVoltage`, `phaseType`, `ratedCapacity`, `powerConnections[]`, `managedBy` inferred** |
| alerts | /rest/alerts | oneview-python `alerts.py`, examples/alerts.py (`alertState`, `changeLog[].userEntered/uri`, `notes`, `assignedToUser`, `alertUrgency` on update); oneview-ansible test fixture (`alertState`, `category`, `uri`), playbook filters `alertState='Cleared'`, `urgency='High'`; HPE Community "retrieve certificate alerts" payload (type `AlertResourceV3`, `healthCategory`, `associatedResource{resourceName,resourceUri,resourceCategory,associationType}`, `alertTypeID`, `correctiveAction`, `urgency`, `lifeCycle`) via search summary; `severity`, `physicalResourceType`, `eventUri`, `resourceUri` from the brief. **`resourceID`, `serviceEventSource` inferred** |
| events | /rest/events | oneview-python `events.py` + `tests/unit/resources/activity/test_events.py` (`eventTypeID`, `severity`, `healthCategory`, `rxTime`, `urgency`, `eventDetails[]`, `serviceEventSource`, `serviceEventDetails`, `description`); filter `eventTypeID='StatusPoll.EnclosureStatus'` in examples/events.py. **`resourceUri`, `associatedResource` inferred** |
| tasks | /rest/tasks | oneview-golang `ov/task.go` (`Task`, `AssociatedResource`, `TaskError`, `ProgressUpdate`); oneview-ansible-collection `test_oneview_task_facts.py` fixture (`TaskResourceV2`, `taskState`, `taskStatus`, `owner`, `associatedResource.*`, filter `associatedResource.resourceName='…'`); tests/unit test_tasks.py (`fields=parentTaskUri,owner,name`, `taskState='Running'`) |
| firmware_drivers | /rest/firmware-drivers | oneview-golang `ov/firmware_drivers.go` |
| storage_systems / storage_pools / storage_volumes | /rest/… | oneview-golang `storage_system.go`, `storage_pool.go`, `storage_volume.go` |
| scopes | /rest/scopes | oneview-golang `ov/scope.go` |
| index_resources | /rest/index/resources | oneview-python `index_resources.py` (`category`, `userQuery`, `query`, `fields`, `filter`, `sort`, `padding`, `referenceUri`), `tests/unit/resources/search/test_index_resources.py` (`IndexResourceV300`, `members[]`, `attributes` per brief). **Whether a member's `uri` is the resource URI or an index URI is inferred** (the simulator uses the resource URI) |

Time filters on `created` (`created>='ISO'` / `created<='ISO'`) for alerts,
events and tasks follow the generic filter grammar and are **inferred**
(comparison operators on timestamps are documented for the filter grammar but
no fixture shows them).

## Other URLs consulted

- https://raw.githubusercontent.com/HewlettPackard/oneview-python/master/hpeOneView/resources/resource.py (ResourceHelper: `build_query_uri`, `do_requests_to_getall`, `get_next_page`, `get_utilization`)
- https://raw.githubusercontent.com/HewlettPackard/oneview-python/master/hpeOneView/oneview_client.py (resource registry)
- https://raw.githubusercontent.com/HewlettPackard/oneview-python/master/endpoints-support.md (endpoint/API-version matrix)
- https://raw.githubusercontent.com/HewlettPackard/oneview-ansible-collection/master/plugins/module_utils/oneview.py
- https://raw.githubusercontent.com/HewlettPackard/oneview-ansible-collection/master/tests/unit/test_oneview_{enclosure,interconnect,logical_interconnect,server_profile_template,storage_system,uplink_set,scope,task,firmware_driver,sas_interconnect}_facts.py
- https://raw.githubusercontent.com/HewlettPackard/oneview-ansible/master/test/test_oneview_{alert,server_hardware,datacenter,rack,power_device}_facts.py and examples/oneview_alert_facts.yml
- https://raw.githubusercontent.com/HewlettPackard/terraform-provider-oneview/master/oneview/data_source_task.go, data_source_server_hardware.go
- https://raw.githubusercontent.com/HewlettPackard/oneview-sdk-ruby/master/lib/oneview-sdk/rest.rb, client.rb
- https://raw.githubusercontent.com/zabbix/zabbix/master/templates/server/hpe_synergy_http/README.md
- HPE Community threads (bot-protected, content only via search snippets): "OneView REST API retrieve certificate alerts" (community.hpe.com/t5/hpe-oneview/…/m-p/7033472), "hpe-oneview-nodeinfo" (m-p/7179917), "get current firmware version" (m-p/7039231), "OneView API call fails for some REST queries" (m-p/6918856 — missing X-API-Version symptoms), "rest alerts query" (m-p/7234979)
- POSH-HPEOneView docs: https://hewlettpackard.github.io/POSH-HPEOneView-docs/latest/cmdlets/appliance/get-ovhealthstatus (health covers CPU, memory, disk)
