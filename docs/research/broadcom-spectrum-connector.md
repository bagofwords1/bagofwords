# Broadcom DX NetOps Spectrum — integration research

Status: **research only, nothing implemented** (2026-10-29).
Goal: decide how Bag of Words should connect to DX NetOps Spectrum (formerly
CA Spectrum), Broadcom's network fault-management product, and what the
connector would look like next to `aria_operations`, `appdynamics`, `brocade`.

## TL;DR

- **There is one integration surface worth building on: the OneClick Web
  Services REST API** at `https://<oneclick>:<port>/spectrum/restful/…`.
  It is the only supported, network-reachable, read+write API. Swagger UI
  exists on the server (OneClick → "API Documentation",
  `/spectrum/restful/api-docs?url=/spectrum/restful/swagger.json`) but
  Broadcom publishes **no downloadable OpenAPI file**.
- **A community OpenAPI 3.0.3 spec exists** (Apache-2.0, Itential, Sept 2026):
  `itential/assets` → `Broadcom/DX NetOps Spectrum/OpenAPIs/dx_netops_spectrum-24.3.json`
  (31 operations, 12 tags). Verified against TechDocs below; it is a faithful
  path/param map but has **no response schemas** (XML bodies are modelled as
  strings). Good as a checklist, not as a code generator.
- **Auth:** HTTP Basic with a *local* Spectrum user (works on every version,
  even with SAML/SSO on, as long as the account is in the local user DB).
  From **24.3.10+** an admin can mint a long-lived **JWT bearer token** in
  OneClick → Administration → Token Configuration. 25.4.1 removed Basic for
  the OneClick console/webapp clients only; REST Basic still documented.
- **Wire format:** requests are **XML only**; responses XML or JSON via
  `Accept: application/json`. Everything is keyed by hex **attribute IDs**
  (`0x1006e` = Model Name, `0x11f56` = Severity …) and hex **model handles**.
- **No official MCP server** for Spectrum/NetOps. Broadcom ships one only for
  DX Operational Observability (DXO2, SaaS). Nothing community-built either.
- **Recommendation:** a native `spectrum` data-source client in the
  `aria_operations` style (virtual-table catalog + JSON query spec), read-only
  in v1, Basic + Bearer auth variants, with a small mock Tomcat-like server
  for tests (no public docker image of Spectrum exists).

---

## 1. Product orientation

| Term | Meaning |
|---|---|
| **SpectroSERVER (SS)** | The modelling/fault engine. Holds *models* (devices, ports, containers, services, users …) in a proprietary DB. Several SSs form a **DSS** (distributed) estate. |
| **Landscape** | One SpectroSERVER's model space, identified by a hex handle (e.g. `0x1000000`). Model handles embed the landscape prefix. |
| **OneClick (OC)** | Tomcat web tier: console, web app, and the **REST web services**. The REST API talks to one or many landscapes. |
| **Model / model handle (mh)** | Any managed object. `mh` is a hex id, unique per SS. Model type handle `0x10001`, model class `0x11ee8`. |
| **Attribute** | Typed field on a model, addressed by hex id. Alarm-specific vs model attributes. Enumerations resolvable via `/attribute/<id>/enums`. |
| **Alarm / Event** | Events are raised from traps/polls; EventDisp rules turn them into alarms (severity 1 Minor, 2 Major, 3 Critical; 0 = Normal/Maintenance per KB usage). |
| **Global Collection (GC)** | Saved search/group of models, usable as a query target. |
| **Report Manager / SRM** | Optional MySQL `reporting` DB + `srmdbapi` read-only views (alarm/event history, outages). |
| **DX NetOps Portal / Performance Management** | *Separate product* with its own OpenAPI + token API on port 8381. Not Spectrum. Don't mix docs. |

## 2. Integration surfaces (ranked)

### 2.1 OneClick Web Services REST API — **primary**

- Base: `http(s)://<oneclick-host>:<port>/spectrum/restful/<noun>` (default
  Tomcat ports 8080/8443; the KB basic-auth article shows `/spectrum/rest/`
  which looks like a typo — every TechDocs page and both OpenAPI specs use
  `/spectrum/restful`).
- Architecture: stateless, noun-oriented, verbs = HTTP methods. Two request
  styles: **URL requests** (simple, query params) and **GET tunnelling**
  (a POST whose XML body carries a search). TechDocs says both perform the
  same.
- Input XML only (`Content-Type: application/xml`); output XML or JSON
  (`Accept`). JSON responses are a mechanical translation of the XML and not
  documented anywhere — must be observed on a live instance.
- XML namespaces:
  - request `rs` = `http://www.ca.com/spectrum/restful/schema/request`
  - filter (default ns inside `search-criteria`) = `http://www.ca.com/spectrum/restful/schema/filter`
  - response = `http://www.ca.com/spectrum/restful/schema/response`
  - XSDs (`Request.xsd`, `Filter.xsd`, `Response.xsd`) + WADL ship with
    OneClick; sample bodies in `$SPECROOT/RestfulExamples/xml/{Alarms,Models,Events,MTypes}`.

#### Resource map (24.3; cross-checked TechDocs + Itential spec)

| Noun | Ops | Read value for BoW | Notes |
|---|---|---|---|
| `landscapes` | GET | **yes** – list SS landscapes (`id`, `name`, `isPrimary`, `spectrumVersion`) | First call; use for connection test + landscape fan-out. |
| `devices` | GET `?attr=&landscape=&throttlesize=` | **yes** – all device models | = action `FIND_ALL_DEVICE_MODELS (0x1023a)`. Returns `model-response-list` with `mh` + requested attrs. |
| `models` | POST (search, XML) · GET `?id=<result-set>` (page) · PUT (bulk attr update) · GC variant `<rs:gc name=…/>` | **yes** – any model type, filtered | Filter grammar: `filtered-models` with `and/or`, `equals`, `has-substring`, `is-derived-from`, `action-models` (run a SearchManager action e.g. `FIND_DEV_MODELS_BY_IP`). Result set lives 10 min. |
| `models/getAllDevices` | POST | yes | Listed in Itential spec only; verify on live. |
| `model/<mh>` | GET `?attr=` · PUT `?attr=&val=` · POST (create) · DELETE · POST `model/attr/update` | **yes** (GET) | Single-model read of arbitrary attrs; writes out of scope v1. |
| `alarms` | GET `?attr=&landscape=&throttlesize=` · POST (search) · POST `alarms/count` · GET `alarms/filters` · PUT `/<id>` · POST `alarms/update` · DELETE `/<id>` | **yes** – core of the connector | POST body `rs:alarm-request` with `attribute-filter`/search-criteria, `?symptoms=yes|no`, `?lasthour=N`. Count by severity via `rs:alarms-count-request` + named OneClick alarm filter. |
| `events/getEvents` | POST (XML) | **yes** – event history per device model, time-windowed | `rs:get-event-request` with `start-time`/`end-time` (epoch ms), `exclude-events`, `subcomponents-events`, exactly **one** `landscape`, device models only. Paged with `next` link (`start`,`throttlesize`). |
| `events` / `events/<type>/model/<mh>` | POST (create) | no (write) | |
| `associations/relation/<rel>/model/<mh>?side=left|right` | GET (+POST/DELETE) | **yes** – topology/containment graph | Relation handles are hex (e.g. `0x10031`); `has_members`, `Collects`, `Contains` … need a small relation-id table. |
| `connectivity/<ip>` | GET | **yes** – L2/L3 neighbours of a device | Response `connection-response-list` (left element/right element with `mh`, `name`, `type`, `class`, `condition`). Itential spec says `/connectivity/{modelHandle}`; TechDocs says IP. Verify. |
| `attribute/<id>/enums` | GET | **yes** – decode enum ints (model class, condition …) | Only for internal attrs. |
| `action/<code>?mh=&attr=&val=` | GET | partial | Runs SS actions (reconfig, searches). Read-only use: `0x10093 FIND_DEV_MODELS_BY_IP`. Most codes mutate — allowlist. |
| `subscription` | POST/GET/DELETE | optional | Pull/push change feeds for alarms/models/mtypes; `pull-interval` ≥ 5 s, expires after 5 missed pulls or Tomcat restart. Not needed for an on-demand query connector; useful later for streaming. |
| `v3profiles` | POST create/delete | no | SNMPv3 admin. |

#### Attribute IDs we will need (from TechDocs "Alarm and Model Attributes" + KBs)

Model: `0x10000` ModelType Name · `0x10001` ModelType Handle · `0x1006e` Model Name ·
`0x11ee8` Model Class (enum: 2 Switch, 3 Router …) · `0x1000a` Condition ·
`0x12d7f` Network Address · `0x11d42` Landscape Name · `0x129ac` Landscape Handle ·
`0x129fa` Model Handle · `0x23000e` Device Type · `0x133f4` System Name ·
`0x23000c` Contact · `0x23000d` Location · `0x1290c` Criticality ·
`0x1295d` isManaged · `0x12adb` Global Collections list · `0x11564` Notes ·
`0x10032` / `0x12de2` (seen in examples; resolve names on live).

Alarm: `0x11f9c` Alarm ID · `0x11f56` Severity · `0x11f4e` Creation Date ·
`0x1321a` Last Occurrence · `0x11f4d` Acknowledged · `0x11f4f` Alarm Status ·
`0x11f50` Cause Code · `0x12b4c` Alarm Title · `0x11f53` Alarmed Model Handle ·
`0x11f57` Troubleshooter · `0x12022` Trouble Ticket ID · `0x11fc5` Occurrences ·
`0x11fc4` Alarm Source (0 current/1 residual) · `0x11f54` Primary Alarm ·
`0x1296e` Originating Event · `0x12a04/0x12a05` Symptom/Cause lists ·
`0x12a63` Web Context URL · `0x12c05` Secure Domain.

Event (getEvents): `0x4820007` Event text, plus any model attr.

#### Paging & limits

- `throttlesize` on URL or on the `rs:*-request` root. Default throttle is
  **1000** records (25.4 KB); responses carry `throttle`, `total-*`, and
  `error="EndOfResults"` or a `<link rel="next">` with `id` (models result
  set) / `start` (events). KB for 25.x also documents `start` + `throttlesize`
  offset paging for inventory pulls.
- Large inventories: OneClick can take minutes to build XML; Broadcom
  recommends client timeouts ≥ **600 s** and batches of 250–500. Our
  `HTTP_TIMEOUT`/`QUERY_BUDGET_SECONDS` pattern from `aria_operations_client`
  applies.
- Result sets from POST `models` expire after 10 minutes of inactivity.
- Attribute requests are the dominant cost — always request a minimal attr
  set (TechDocs performance note).

#### Authentication & authorization

| Mode | Availability | How | Notes |
|---|---|---|---|
| **HTTP Basic** | all versions | `Authorization: Basic base64(user:pass)` | User must exist in Spectrum's *local* user DB (SSO-only users fail; with SAML the user must be in `non-saml-config.xml`). Spectrum RBAC applies (landscape/role/privileges). |
| **Bearer JWT** | 24.3.10+ (25.4.1 extends to more components) | Admin → Token Configuration → choose expiration + audience → Generate (copy immediately; revocable from the same page). RSA-signed JWT carrying user, iat, exp. `Authorization: Bearer <jwt>` | Expiration options not captured in docs text (UI drop-down). Lets SSO/LDAP users call REST. FT environments must sync primary/secondary DB after minting. |
| REST access policy | all | OneClick admin can restrict REST per user to GET / POST / PUT / DELETE | Our v1 only needs GET + POST (GET-tunnelling), so a read-only API user is realistic. |

Security notes: CVE-2025-69270 (session token in GET query string, fixed in
24.3.9+) — prefer headers, never put tokens in URLs. Always HTTPS; expose a
`verify_ssl` / `ca_bundle` pair like Aria.

### 2.2 Spectrum Report Manager `srmdbapi` (MySQL views) — **secondary / optional**

Read-only documented views in the Report Manager MySQL instance:
`v_dim_device_model`, `v_dim_interface_model`, `v_dim_model`, `v_dim_landscape`,
`v_dim_global_collection_member`, `v_dim_alarm_condition`, `v_dim_alarm_title`,
`v_fact_alarm_activity` (1 Set, 2 Ack, 4/5 Clear, 6 Ticketed), `v_fact_alarm_info`
(set_time, clear_time, duration_seconds, TTA…), `v_fact_model_outage`,
`v_dim_event`, `v_fact_event`, SPM test result views, `v_dim_ncm_event`, `v_dim_time`.

Pros: real SQL, history/MTTR/availability analytics the REST API cannot do
(REST has no alarm *history*, only current alarms + events).
Cons: only present when Report Manager is installed; needs a MySQL user and
network path to that box; lags live state. **Our existing `mysql` data source
already covers this** — zero code, just a docs recipe ("point the MySQL
connector at `srmdbapi`").

### 2.3 Kafka event publishing (25.4.1+) — not for v1

Spectrum can publish events to topic `netops.spectrum.events` with a filter
utility. Streaming only, no query. Mention in docs as a future "events lake"
path; BoW has no Kafka consumer today.

### 2.4 SpectrumDataPublisher → DX OI / DXO2 — not applicable

Pushes inventory/alarms into Broadcom's SaaS observability. The **DXO2 MCP
server** (Spring AI, streamable HTTP, permanent user token) exposes alarms,
topology, metrics *there*, not in Spectrum. Only relevant for customers who
also run DXO2 — in that case our generic `mcp` preset machinery could add a
DXO2 preset with `auth="bearer"`. Out of scope for a Spectrum connector.

### 2.5 Legacy APIs — skip

SSORB/CORBA "Development API", `VnmSh` CLI (`show attributes`, `show
associations`), AlarmNotifier scripts, SANM. Not network APIs; CLI only runs on
the SS host. Only use as a reference for attribute names.

## 3. MCP landscape

- No Broadcom MCP server for Spectrum or DX NetOps (searched TechDocs,
  community, GitHub, Oct 2026). Broadcom's only MCP is DXO2 (SaaS).
- `Project516/spectrum-mcp` on GitHub is an unrelated app ("Spectrum" name
  collision — also beware Precisely Spectrum, IBM Spectrum Protect, Charter
  Spectrum, Star Citizen Spectrum in search results).
- Conclusion: we cannot take the "add an `McpPreset`" shortcut; a native
  client is required.

## 4. Open-source clients (prior art)

| Repo | Lang | Notes |
|---|---|---|
| `orgito/spectrum-client` (PyPI `spectrum-client` 0.4.1, 2022) | Python | Basic auth, XML only, `models` search with `rs:model-request` template, `model/<mh>` get/put, `events` create. Good reference for filter XML and attribute constants. Unmaintained, uses removed `getchildren()`. |
| `sprootik/oneclick_client` (PyPI `oneclick-client`) | Python | Thin requests wrapper; shows `Accept: application/json` output and `devices?throttlesize&landscape&attr`. |
| `zarulzakuan/spectrumwrapper` | Go | XML requests, JSON responses, multi-landscape. |
| `itential/assets` (Apache-2.0) | OpenAPI 3.0.3 | `dx_netops_spectrum-24.3.json` (31 ops) + `-latest.json` (29 ops, curated) + 29 Itential workflows. Basic auth only. Copies saved under `/tmp/...scratchpad/spectrum_dl/` for this session; link rather than vendor. |

None is good enough to depend on; we will write our own XML builders (Python
stdlib `xml.etree` + `defusedxml` for parsing, as `brocade_client` does).

## 5. Fit with the Bag of Words connector architecture

Closest reference: **`aria_operations_client.py`** (REST monitoring product, no
query language → JSON query spec over a virtual-table catalog, token caching,
budgeted pagination, enterprise license). Brocade shows the allowlisted
read-only + XML-parsing style.

### Proposed registry entry (sketch, not code)

- `type="spectrum"`, `category="infra"`, `title="DX NetOps Spectrum"`,
  `version="beta"`, `requires_license="enterprise"`,
  `client_path="app.data_sources.clients.spectrum_client.SpectrumClient"`.
- `SpectrumConfig`: `url` (OneClick base, `/spectrum/restful` appended),
  `landscapes` (optional comma list of hex handles; default = all from
  `/landscapes`), `verify_ssl`, `ca_bundle`, `request_timeout_s` (default 600
  per Broadcom), `default_throttlesize` (default 500).
- Credentials, two `AuthVariant`s, scopes `["system","user"]` (Spectrum RBAC
  is per user, so per-user creds are meaningful):
  - `userpass` → Basic (works everywhere; local user required).
  - `bearer` → JWT from Token Configuration (24.3.10+).

### Virtual tables (v1, read-only)

| Table | Source | Key columns |
|---|---|---|
| `landscapes` | GET landscapes | id, name, is_primary, spectrum_version |
| `devices` | GET devices (+attrs) | mh, name, model_type, model_class, condition, ip, device_type, location, contact, sys_name, landscape, is_managed, criticality |
| `models` | POST models search | same + filter: model_type/handle, name substring, ip, GC name, landscape |
| `interfaces` | associations `has_members` (or models search on port classes) | mh, device_mh, name, ip, condition … |
| `model_attributes` | GET model/<mh>?attr= | mh, attr_id, attr_name, value (long format; arbitrary attr ids) |
| `alarms` | POST alarms (search) | alarm_id, severity, title, status, acknowledged, created, last_occurrence, occurrences, model_mh, model_name, ip, model_type, cause_code, troubleshooter, ticket_id, is_primary, source, landscape |
| `alarm_counts` | POST alarms/count | filter_name, critical, major, minor |
| `alarm_filters` | GET alarms/filters | name |
| `events` | POST events/getEvents | event_id, time, type, text, model_mh, landscape (requires `start`/`end`, one landscape, device models) |
| `associations` | GET associations/relation | relation, left_mh, right_mh |
| `connectivity` | GET connectivity/<ip> | local port, remote device/port, type, class |
| `attribute_enums` | GET attribute/<id>/enums | attr_id, enum_id, label |

Columns map attribute IDs → friendly names via a static dictionary (section 2
list); unknown attrs stay as hex keys.

### Things to verify on a live/mock instance before implementation

1. JSON response shapes for `model-response-list`, `alarm-response-list`,
   `get-event-response-list` (undocumented; may wrap single items differently).
2. Exact `next` link / `start` semantics per noun; default throttle (1000).
3. `connectivity/{ip}` vs `{mh}`; `models/getAllDevices` existence.
4. Bearer header format on 24.3.10+ (`Authorization: Bearer <jwt>` assumed;
   TechDocs text truncated).
5. Severity int mapping (KB uses 2/3 as Major/Critical; confirm 1 Minor, 0
   Normal/Maintenance) via `attribute/0x11f56/enums`.
6. Which version the customer runs (24.3.x vs 25.4.x) — 25.4.1 auth changes.

### Testing strategy

No public Spectrum docker image (licensed, Windows/Linux installer only).
Follow the `aria_operations` precedent: a small mock OneClick (`tools/spectrum/mock_oneclick.py`,
XML-in / XML-or-JSON-out, fixture bodies adapted from TechDocs examples) +
`backend/tests/unit/test_spectrum_client.py` mocking `requests`, plus an
`integrations.json` entry for a real customer instance. Feedback loop doc
`docs/feedback-loops/spectrum-connector.md` per the add-connection-type skill.

### Effort estimate

Comparable to the Aria connector minus metrics (Spectrum has no time-series):
client ~900–1200 lines (XML builders, catalog, pagination, auth), schemas +
registry ~120, mock server ~300, tests ~400, icon, docs. Roughly 3–4 agent
days including the live verification pass.

## 6. Sources

Official (Broadcom TechDocs / KB):
- Web Services API Reference 24.3: https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/spectrum/24-3/programming/web-services-api-reference.html
- Swagger page (24.3): https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/dx-netops/24-3/Fault-Monitoring-with-DX-Spectrum/programming/web-services-api-reference/swagger-for-REST-API-documentation.html
- Requests / throttling: https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/spectrum/24-3/programming/web-services-api-reference/how-to-use-the-ca-spectrum-web-services-api/requests.html
- Nouns (24.3, dx-netops path): alarms https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/spectrum/24-3/programming/web-services-api-reference/how-to-use-the-ca-spectrum-web-services-api/restful-resources-nouns/using-alarm-resources.html · devices https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/spectrum/24-3/programming/web-services-api-reference/how-to-use-the-ca-spectrum-web-services-api/restful-resources-nouns/devices.html · models https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/dx-netops/24-3/Fault-Monitoring-with-DX-Spectrum/programming/web-services-api-reference/how-to-use-the-ca-spectrum-web-services-api/restful-resources-nouns/models.html · model https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/dx-netops/24-3/Fault-Monitoring-with-DX-Spectrum/programming/web-services-api-reference/how-to-use-the-ca-spectrum-web-services-api/restful-resources-nouns/model.html · events https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/dx-netops/24-3/Fault-Monitoring-with-DX-Spectrum/programming/web-services-api-reference/how-to-use-the-ca-spectrum-web-services-api/restful-resources-nouns/events.html · associations …/associations.html · connectivity …/connectivity.html · landscapes …/landscapes.html · attribute …/attribute.html · action …/action.html · subscription …/subscription.html · alarm-and-model-attributes …/alarm-and-model-attributes.html
- Token-based auth (24.3.10+), OneClick Administration Pages: https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/dx-netops/24-3/Fault-Monitoring-with-DX-Spectrum/administrating/oneclick-administration/oneclick-administration-pages.html
- 25.4.1 features (Kafka events, bearer auth): https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/spectrum/25-4/release-information/Features-and-Enhancements-2541.html
- KB Basic auth with SSO: https://knowledge.broadcom.com/external/article/416236/accessing-dx-netops-spectrum-apis-with-b.html
- KB restrict REST per user: https://knowledge.broadcom.com/external/article/188959/how-can-i-restrict-rest-api-access-on-a.html
- KB device list / 1000 throttle (25.4): https://knowledge.broadcom.com/external/article/435963/extract-device-list-via-spectrum-rest-ap.html
- KB inventory timeouts, `start`/`throttlesize`: https://knowledge.broadcom.com/external/article/453722/spectrum-unable-to-pull-inventory-via-a.html
- KB critical/major alarms XML: https://knowledge.broadcom.com/external/article/194661/how-to-get-a-list-of-critical-and-major.html
- KB interfaces via associations: https://knowledge.broadcom.com/external/article/235422/how-to-use-spectrum-rest-api-to-get-inte.html
- KB subscription polling: https://knowledge.broadcom.com/external/article/76433/ca-spectrum-getting-the-alarms-count-vi.html
- srmdbapi views: https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/spectrum/24-3/managing-client-applications/report-manager/report-manager-db-schema/srmdbapi-database.html
- DXO2 MCP server (not Spectrum): https://techdocs.broadcom.com/us/en/ca-enterprise-software/it-operations-management/dx-operational-observability/saas/generative-ai/dxo2-mcp-server.html
- Security advisory CVE-2025-69270: https://support.broadcom.com/web/ecx/support-content-notification/-/external/content/SecurityAdvisories/0/36756
- Community: swagger URL https://community.broadcom.com/viewthread?MID=729491 · JSON via Accept https://community.broadcom.com/communities/community-home/digestviewer/viewthread?mid=725848 · token auth timing (May 2025) https://community.broadcom.com/discussion/json-web-token-access-for-rest-api

Third-party:
- Itential OpenAPI + workflows: https://github.com/itential/assets/pull/130 (files under `Broadcom/DX NetOps Spectrum/`)
- https://github.com/orgito/spectrum-client · https://github.com/sprootik/oneclick_client · https://github.com/zarulzakuan/spectrumwrapper
