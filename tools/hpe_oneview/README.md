# HPE OneView / Synergy Composer connector

Native HTTPS REST connector for HPE OneView (catalog reference **OneView 9.00,
X-API-Version 6800**; negotiated versions are clamped to 1200–7200). Synergy
Composer exposes the same API. No SDK, MCP server or HPE licence is needed for
local development. Real appliance verification is still required before
customer acceptance (release, RBAC scopes, firmware/utilization retention).

## Use in BOW

Add **HPE OneView** under Connections. Supply the appliance origin
(`https://oneview.example`), an account with the *Read only* role (any
`authLoginDomain`: `local` or a configured directory), and the trusted PEM CA
if the appliance certificate is private. TLS verification is always on; the
HTTP option is accepted only towards a loopback simulator.

Every query opens a session (`GET /rest/version` → `POST /rest/login-sessions`),
sends the `Auth: <sessionID>` and `X-API-Version` headers on each GET, and
always ends with `DELETE /rest/login-sessions`. The client is incapable of a
mutating request: its single network primitive only allows GET on catalog paths
plus the login handshake.

`get_tables()` exposes 32 tables (see `backend/app/data_sources/clients/hpe_oneview/catalog.json`):

| Table | Endpoint | Notes |
|---|---|---|
| appliance_version | /rest/appliance/nodeinfo/version | one row |
| appliance_health | /rest/appliance/health-status | one row per member; `member` keeps the raw object |
| server_hardware | /rest/server-hardware | health, power, iLO, firmware versions, profile link |
| server_hardware_firmware | /rest/server-hardware/*/firmware (or /{id}/firmware with `parent`) | one row per component |
| server_hardware_utilization | /rest/server-hardware/{id}/utilization | `parent`, `metrics`, `lookback`/`start_time`/`end_time`, `view` |
| server_hardware_types, server_profiles, server_profile_templates | /rest/… | profile compliance, firmware baseline |
| enclosures, enclosure_groups, logical_enclosures | /rest/… | bays kept as arrays |
| interconnects, interconnect_ports, logical_interconnects, sas_interconnects, uplink_sets | /rest/… | `interconnect_ports` flattens `ports[]` |
| ethernet_networks, fc_networks, fcoe_networks, network_sets | /rest/… | |
| racks, datacenters, power_devices | /rest/… | |
| alerts, events, tasks | /rest/alerts, /rest/events, /rest/tasks | `lookback` maps to `created>=`/`<=` filters |
| firmware_drivers, storage_systems, storage_pools, storage_volumes, scopes | /rest/… | |
| index_resources | /rest/index/resources | `category` list + `user_query` free text |

```python
servers = client.execute_query({
    "table": "server_hardware",
    "fields": ["uri", "name", "status", "powerState", "serverProfileUri", "mpFirmwareVersion"],
    "filter": {"status": {"op": "in", "value": ["Critical", "Warning"]}},
    "sort": ["name asc"],
})
alerts = client.execute_query({"table": "alerts", "filter": {"alertState": "Active"}, "lookback": "24h"})
power = client.execute_query({
    "table": "server_hardware_utilization",
    "parent": {"id": servers.iloc[0]["uri"].rsplit("/", 1)[-1]},
    "metrics": ["AveragePower", "AmbientTemperature"], "lookback": "24h", "view": "hour",
})
```

Supported controls: `table`, `fields` (list or `"*"`), `filter`, `parent`,
`sort`, `limit`, `lookback`/`start_time`/`end_time` (alerts, events, tasks,
utilization), `metrics`/`view` (utilization), `category`/`user_query`
(index_resources). Scalar filters are OneView `field='value'`; comparisons are
`{"op": "gt", "value": 10}` with eq/ne/gt/ge/lt/le/like (`matches` with `%`)
and `in` (one `field='a' OR field='b'` filter). Each filter is sent as its own
`filter=` parameter exactly like the HPE Python SDK does. `limit` caps a
**complete** result; the client raises `ResultLimitExceeded` instead of
returning a partial page (it also fails fast when the appliance reports a
`total` above the limit).

Pagination follows `nextPageUri` until it is `null` (never `count=-1`), and
rejects continuations that change origin, path, filters or do not advance.
Secret-looking keys (`password`, `*token*`, `*community*`, `credentials`, …) are
stripped from nested objects before they reach a DataFrame.

## Run the simulator

```bash
export ONEVIEW_USERNAME='<local-test-user>'
export ONEVIEW_PASSWORD='<local-test-password>'
python3 tools/hpe_oneview/simulated_api.py --port 18093      # --help for options
```

Handshake, exactly as the appliance:

```bash
B=http://127.0.0.1:18093
curl -s $B/rest/version                                   # {"currentVersion": 6800, "minimumVersion": 120}
TOKEN=$(curl -s -X POST $B/rest/login-sessions -H 'X-API-Version: 6800' -H 'Content-Type: application/json' \
  -d '{"userName":"'$ONEVIEW_USERNAME'","password":"'$ONEVIEW_PASSWORD'","authLoginDomain":"local","loginMsgAck":true}' \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["sessionID"])')
curl -s "$B/rest/server-hardware?start=0&count=5&filter=%22status%3D%27Critical%27%22&sort=name:asc" \
  -H 'X-API-Version: 6800' -H "Auth: $TOKEN"
curl -s -X DELETE $B/rest/login-sessions -H 'X-API-Version: 6800' -H "Auth: $TOKEN"   # 204
```

Then in BOW create an **HPE OneView** connection with URL `http://127.0.0.1:18093`,
the same username/password and **Allow HTTP for a simulator** enabled.

### What the simulator models

- `GET /rest/version` without auth or version header; every other route requires
  `X-API-Version` in 120–6800 (412 `UNSUPPORTED_API_VERSION` otherwise) and, except
  login, a valid `Auth` token (401 `AUTHN_AUTH_TOKEN_INVALID`). Bad credentials get
  400 `AUTHN_AUTH_FAIL` with the documented message.
- Any non-GET except `POST`/`DELETE /rest/login-sessions` → 405
  `HTTP_METHOD_NOT_ALLOWED` with the body captured from a real appliance.
  Unknown paths → 404 `RESOURCE_NOT_FOUND`.
- Collections: `start`/`count` (default 50, `count=-1` deliberately means one
  default page), `total`, `nextPageUri`/`prevPageUri`/`uri`, repeated `filter`
  parameters (`=`, `<>`, `>`, `>=`, `<`, `<=`, `matches`, `AND`/`OR`), `sort=field:asc|desc`,
  `fields` projection, `/rest/{category}/{id}` by id.
- Estate: 3 Synergy 12000 frames, 24 SY 480 Gen10/Gen11 servers (5 degraded,
  3 powered off, 4 without a profile), 20 profiles (3 NonCompliant), 2 templates,
  6 VC SE 100Gb F32 modules with 20 ports each (one with a down uplink), 6 SAS
  modules, 2 logical interconnects (one inconsistent), 2 SPP baselines (frame 3
  behind), 80 alerts (Active/Cleared/Locked), 80 events, 40 tasks (3 Error),
  networks, uplink sets, racks/datacenter/PDUs, one Primera with pool and volumes,
  and 30 days of utilization samples (native/hour/day views) per server.

### What it does not model

Scopes-based RBAC, `view=tree` task trees, `/rest/index/associations`,
idle-timeout expiry of sessions, ETags/If-Match, and the exact field set of a
particular OneView release. Fields marked `"inferred": true` in the catalog
(appliance health members, SAS interconnect links, rack/datacenter/power-device
details, a few alert/event fields) must be reconciled against a real appliance —
see `SOURCES.md`.

## Tests

```bash
cd backend
uv run pytest tests/unit/test_hpe_oneview_client.py -q --db=sqlite
TESTING=true BOW_DATABASE_URL=sqlite:///db/hpe_oneview-tests.db uv run pytest tests/e2e/test_hpe_oneview.py --db=sqlite -q
```

The unit file mocks `requests.Session.send` and also boots this simulator on an
ephemeral port for a real round trip over HTTP.

## Catalog maintenance

`python3 tools/hpe_oneview/generate_catalog.py` regenerates the frozen catalog
(and its SHA-256). Add a field only with evidence recorded in `SOURCES.md`; a new
endpoint must not silently widen the runtime surface.
