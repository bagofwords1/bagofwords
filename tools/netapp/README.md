# NetApp ONTAP RCA connector

Native HTTPS REST connector for the ONTAP **9.14.1** contract. No MCP server,
AWS resources or NetApp license is needed for local development. Real appliance
verification is still required before customer acceptance, especially 9.14.1P9,
hardware, FC, permissions, QoS coverage and retained history.

## Use in BOW

Add **NetApp ONTAP** under Connections. Supply the cluster management origin,
an account enabled for the HTTP application with read-only diagnostic access,
and the trusted PEM CA if needed. TLS verification is enabled. The HTTP option
is exclusively for an isolated simulator. Credentials use BOW's existing encrypted
system-credential flow; no per-user impersonation is offered.

`get_tables()` and `get_schemas()` expose 466 tables: 35 curated REST resources,
one constituent view, one derived membership table and 429 diagnostic resources. `get_schema(name)`
provides columns, parent requirements and supported filters. Discovery does not
crawl inventory. Device availability starts unverified and is updated when queried.
`coverage_report()` accounts for all 504 Swagger GETs, including 40 excluded
secret/content-oriented operations. This report is not a claim that all tables
are supported or accessible on a particular appliance.

```python
volumes = client.execute_query({
    "table": "volumes",
    "fields": ["uuid", "name", "space.size", "space.used"],
    "filter": {"state": "online"},
    "limit": 500,
})
metrics = client.execute_query({
    "table": "volume_metrics",
    "parent": {"volume.uuid": "<resolved UUID>"},
    "lookback": "24h",
})
```

Queries accept JSON strings or dicts and return pandas DataFrames. Scalar fields
use dotted names; arrays remain structured. Use `get_schema` for an unfamiliar
`diag_...` table and supply its exact parent names. No raw URL, headers, method,
SQL or CLI escape hatch exists. Each call uses one explicitly scoped parent.

Supported controls: `table`, `fields`, `filter`, `parent`, `lookback` or
`start_time`/`end_time`, `interval` for metric history, `order_by` where documented,
`limit`, and `severity` for EMS. Scalar filters mean literal equality; explicit
comparisons are `{"op":"gt","value":10}` with eq/ne/gt/ge/lt/le/like/in.
A positive `limit` caps a **complete** result; excess data raises
`ResultLimitExceeded` rather than returning a misleading partial total.

`volumes` excludes FlexGroup constituents; `volume_constituents` explicitly selects
them for physical layout investigations. Never add both populations into capacity totals.

Time-series retention determines available resolution. Keep timestamp, duration,
status and units. Raw counter rows need the counter definitions and appropriately
timed samples before rate/latency calculation. Current topology is not historical
placement. Historical capacity, configuration changes and events beyond ONTAP
retention require an external evidence store (for example Harvest/Prometheus for
collected metrics). Neither events nor correlations alone establish a cause.

## Run a simulated API

Use two terminals. Choose local throwaway values for the environment variables;
never use customer credentials with the simulator.

```bash
# From repository root
export NETAPP_USERNAME='<local-test-user>'
export NETAPP_PASSWORD='<local-test-password>'
python3 tools/netapp/simulated_api.py --port 18091
```

```bash
# From backend, Python 3.12 environment (uv sync --frozen --extra dev)
export BOW_DATABASE_URL=sqlite:///db/netapp.db
export NETAPP_URL=http://127.0.0.1:18091
export NETAPP_USERNAME='<same-local-test-user>'
export NETAPP_PASSWORD='<same-local-test-password>'
PYTHONPATH=. uv run python ../tools/netapp/verify.py --simulator \
  --queries ../tools/netapp/simulator_queries.json --output /tmp/ontap-report.json
```

The server intentionally implements only representative synthetic scenarios, not
all of ONTAP. It forces two-row pagination and has no write routes. Its data does
not demonstrate actual ONTAP performance, hardware behavior or precise patch
compatibility. `simulator_queries.json` uses synthetic IDs and is not suitable
unchanged for a real cluster.

## Customer acceptance

Run the same verification command **without `--simulator`**, using an HTTPS URL,
local credential environment variables, optional `NETAPP_CA_PEM`, and a locally
prepared JSON list of 1–30 explicit diagnostic queries with actual parent IDs.
Start with cluster identity and narrow inventory queries. Do not sweep every
endpoint on production. No role creation, configuration changes or traffic
workloads are performed by the runner.

The report omits source URL, credentials, filters, IDs and row data. It records
query status, complete row counts, columns, errors and the catalog fingerprint.
Review it locally before sharing. Reconcile representative data against System
Manager/CLI at the same grain/time and record unverified families. A successful
connection or synthetic test is not broad-RCA certification.

## Catalog provenance and maintenance

Source: [NetApp ONTAP 9.14.1 Swagger](https://docs.netapp.com/us-en/ontap-restapi-9141/swagger-ui/index.html).
Canonical parsed JSON SHA-256:
`15e7812a4d2d16255855f68f74ba7213c049b837c6b18f33742372f55ef734be`.

The checked-in catalog is a reduced schema/description derivative of that vendor
specification. Regenerate with `python3 tools/netapp/generate_catalog.py swagger.json`.
Review exclusions and field/parameter profiles before updating it; a new Swagger
entry must not silently expand the runtime surface. MCP request code was inspected
as reference; no MCP code or runtime dependency is included.

The supplied logo is the user's requested [Wikimedia-hosted NetApp logo](https://upload.wikimedia.org/wikipedia/commons/thumb/9/9f/NetApp_logo.svg/250px-NetApp_logo.svg.png).
NetApp names and logos identify the vendor; the connector does not imply vendor
certification.
