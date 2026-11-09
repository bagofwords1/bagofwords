# Dell OpenManage Enterprise (OME) connector verification

The BOW connector `dell_ome` talks to the OpenManage Enterprise **REST API
(OData v4 flavoured, `/api/...`)** of OME 3.x/4.x appliances. OME is **not
Redfish**: Redfish is the per-server iDRAC API; OME aggregates servers, chassis
and IOMs behind its own schema (`DeviceService`, `AlertService`, `JobService`,
`UpdateService`, ...). This directory holds a synthetic OME 4.1.0 API for local
verification, the catalog generator and the research record (`SOURCES.md`).

## Start the simulator

```sh
export OME_USERNAME=svc-bow
export OME_PASSWORD=synthetic-only
python3 tools/dell_ome/simulated_api.py --port 18093
python3 tools/dell_ome/simulated_api.py --help
```

Flags: `--port` (default 18093, loopback only), `--max-sessions` (API session
cap, default 6), `--page-size` (records returned when `$top` is absent,
default 50 like the appliance), `--seed`. Set `OME_SIM_LOG=1` for request logs.
Never point it at real credentials; the server only knows the two env vars.

### Handshake exactly as documented

```sh
B=http://127.0.0.1:18093
curl -i -X POST $B/api/SessionService/Sessions -H 'Content-Type: application/json' \
  -d '{"UserName":"svc-bow","Password":"synthetic-only","SessionType":"API"}'
#   HTTP/1.1 201 Created ... X-Auth-Token: <32 hex>  Location: /api/SessionService/Sessions('<Id>')
#   {"Id":"<uuid>","Description":"svc-bow","Name":"API","UserName":"svc-bow","UserId":10078,"Password":null,"Roles":["VIEWER"],...}
curl -H "X-Auth-Token: $TOK" "$B/api/DeviceService/Devices?\$top=50&\$skip=0&\$filter=Status%20eq%204000&\$orderby=Id%20desc"
curl -X DELETE -H "X-Auth-Token: $TOK" "$B/api/SessionService/Sessions('<Id>')"   # 204
```

What the simulator reproduces:

- Session create/delete with `X-Auth-Token` and `Location` headers; Basic auth
  is also accepted on reads (the appliance allows it; the connector does not use it).
- A session cap: the Nth+1 `POST /api/SessionService/Sessions` returns `400`
  with `@Message.ExtendedInfo[0].MessageId = CUSR1001` ("maximum number of
  sessions has been reached").
- `401` with the OME error envelope for a missing/invalid token or bad login,
  `404` for unknown resources/entity ids, `400` with the envelope for bad
  `$filter`/`$orderby`/query options, inapplicable inventory types or
  unsupported `SessionType`.
- **Every** `POST`/`PUT`/`PATCH`/`DELETE` other than the session handshake is
  refused: `405` (`Allow: GET`) on resources, `403` (privilege error) on
  `/Actions/` URIs. Nothing in the estate can be modified.
- OData envelopes: `@odata.context`, `@odata.count` (total after `$filter`),
  `value[]`, `@odata.type`/`@odata.id` per record, `@odata.nextLink` with
  `$skip`/`$top` only (community clients re-append `$filter` to continuations,
  which is what the connector does).
- `$top`/`$skip` (default page 50, no cap), `$filter` with `eq ne gt ge lt le`,
  `contains(Field,'x')`, `and`/`or`, parentheses, `''` escaping, `/` or `.`
  nested paths (`LastRunStatus/Id eq 2070`), `$orderby Field [asc|desc]`.
  `$select`/`$expand` are rejected (OME does not support them broadly).
- Content type `application/json; odata.metadata=minimal; charset=utf-8`,
  `OData-Version: 4.0`.

### Synthetic estate (deterministic, seed 41)

OME 4.1.0 appliance; 38 devices: 14 PowerEdge R650, 16 R750, 4 MX750c sleds,
2 MX7000 chassis, 2 MX9116n fabric engines. Status mix: 6 critical (4000),
7 warning (3000), 2 unknown (2000); 3 devices with `ConnectionState=false`,
2 powered off. 7 groups (4 built-in TypeId 2000, 2 static user groups,
1 query group). ~150 alerts over the last 7 days (PSU0003/FAN0001/CPU0001
critical, TMP0100/PDR16/MEM0701/NIC100 warning, LOG007/USR0030 info).
44 warranty lines (4 expired, 5 expiring within 30 days). 2 firmware
catalogs, 2 firmware baselines with per-device/per-component compliance
reports (BIOS/PERC urgent, iDRAC/NIC recommended), 30 jobs (inventory, health,
discovery failed with CGEN1009, firmware update failed with CUPD3004) with
execution histories and per-target details, 40 audit entries, 3 templates,
1 configuration baseline with 2 non-compliant devices, 2 MCM domains
(illustrative: a standalone OME would return an empty collection).
Per-server inventory (processors, memory, NICs, PSUs, disks, firmware,
management), hardware logs, power, temperature and uptime are consistent with
the health states (e.g. critical hosts have a failed PSU and a PSU0003 log).

## Use in BOW

Add **Dell OpenManage Enterprise** under Connections: appliance origin
(`https://ome.example` or `http://127.0.0.1:18093` with **Allow HTTP for a
simulator**), a Viewer-role account, optional PEM CA. **Test connection** reads
`/api/ApplicationService/Info` and reports the version and the 27 tables.

```python
client.execute_query({"table": "devices", "filter": {"Status": 4000}, "order_by": ["Id desc"]})
client.execute_query({"table": "alerts", "filter": {"SeverityType": {"op": "ge", "value": 8}}, "lookback": "7d"})
client.execute_query({"table": "device_inventory", "parent": {"DeviceId": 10002, "InventoryType": "serverPowerSupplies"}})
client.execute_query({"table": "firmware_compliance_reports", "parent": {"BaselineId": 50}})
client.execute_query({"table": "job_execution_details", "parent": {"JobId": 10004, "ExecutionHistoryId": 1000400}})
```

Queries are JSON objects (`table`, `fields`, `filter`, `parent`, `order_by`,
`limit`, `lookback`). Filters accept scalars (equality) or
`{"op": "eq|ne|gt|ge|lt|le|contains|in", "value": ...}` on the fields the
catalog lists as filterable; `lookback` works on `alerts`/`audit_logs`.
`limit` is a complete-result ceiling (`@odata.count` above it fails fast).
Every query opens one API session and always deletes it afterwards. Results
are pandas DataFrames with dotted nested scalars, structured arrays, nullable
dtypes and `_bow_*` provenance columns; secret-like keys (`Password`, `Key:
password` job params, tokens) are removed and `@odata.*` annotations dropped.

## Regenerate the catalog

```sh
python3 tools/dell_ome/generate_catalog.py   # writes backend/app/data_sources/clients/dell_ome/catalog.json
```

## Tests

```sh
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/dell_ome-tests.db uv run pytest tests/unit/test_dell_ome_client.py -q --db=sqlite
TESTING=true BOW_DATABASE_URL=sqlite:///db/dell_ome-tests.db uv run pytest tests/e2e/test_dell_ome.py --db=sqlite -q
```

The unit suite includes an in-process run of this simulator on an ephemeral port.

## Not modelled

Power Manager plug-in metrics (`MetricService`), report execution, alert
policies/actions, discovery configuration, identity pools, users/roles,
`$select`/`$expand`/`$count=true`, session inactivity timeouts, HTTPS (the
simulator is plain HTTP on loopback; the connector only allows that with the
explicit simulator flag). Tables marked `inferred`/`partial` in
`SOURCES.md` reproduce the best public evidence and need confirmation on a
real appliance before customer acceptance.
