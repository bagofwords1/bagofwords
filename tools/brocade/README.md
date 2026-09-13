# Brocade RCA verification

The connector targets Fabric OS 9.1.1c using a reviewed public 9.1.0b YANG baseline and live module discovery. This server is an independent **synthetic HTTP API**, not Fabric OS or a switch emulator. It runs on ARM64/M-series Macs and x86 hosts with Docker or Python 3.12. No NetApp/Broadcom account is needed for this synthetic lab.

## Start the simulator

From the repository root:

```sh
docker compose -p bow-brocade-verification -f tools/brocade/compose.yaml up -d --build
```

Alternatively:

```sh
python3 tools/brocade/simulated_api.py --port 18092
```

The simulator listens on loopback port 18092. Synthetic credentials default to `lab-reader` / `synthetic-only`; override using `BROCADE_SIM_USER` and `BROCADE_SIM_PASSWORD`. Never use customer credentials for this server. FIDs 10 and 20 represent separate logical switches with overlapping port names.

Run the verifier using the backend's installed Python environment:

```sh
export BROCADE_USER=lab-reader
export BROCADE_PASSWORD=synthetic-only
cd backend
uv run python ../tools/brocade/verify.py --url http://127.0.0.1:18092 --vf-id 10 --allow-http --output /tmp/brocade-verification.json
BROCADE_TEST_URL=http://127.0.0.1:18092 BROCADE_TEST_VF_IDS=10 BROCADE_TEST_ALLOW_HTTP=true \
  uv run pytest tests/integrations/ds_clients.py -k brocade -v
```

The report contains counts and checks only. To stop the container:

```sh
docker compose -p bow-brocade-verification -f tools/brocade/compose.yaml down
```

For the app, select **Brocade Fabric OS**, enter `http://127.0.0.1:18092`, restrict Fabric IDs to `10,20`, enable **Allow HTTP for a local simulator**, and supply the synthetic credentials. **Test connection** discovers 12 tables. This connector follows the enterprise connector license policy. The application backend must run on the same host for this loopback setup; HTTP to remote hosts is deliberately rejected.

## Production connection

Use the switch HTTPS management origin, a read-only account authorized for the required FIDs and diagnostic resources, and certificate verification. TLS certificate verification is always enabled. For a private CA, mount the PEM CA bundle in the backend/container and set `REQUESTS_CA_BUNDLE` to its path, matching SharePoint Server. With that variable unset or empty, the default Requests trust store is used. There is no per-connection certificate path or disable-verification toggle. `Custom_Basic` session login is the default; `Basic` can be selected for a switch whose documented login endpoint uses that scheme. Both modes use the returned session Authorization header and logout afterward.

A connection covers one management endpoint. The connector does not authenticate to discovered neighbors. Configure additional switch connections for fabric-wide investigations. No SSH, RPC, raw URL execution, counter clearing or configuration writes are exposed.

`get_tables()` / `get_schemas()` returns 12 primary views. `get_tables(include_advanced=True)` adds 156 conditional baseline resources; enable **Include advanced diagnostic tables** to index these into the application. Availability means module-advertised, not permission-tested or hardware-verified. A selected unsupported or denied resource fails explicitly.

## Queries

`execute_query` accepts a JSON object, JSON string, or keyword properties and returns a pandas DataFrame. The shared `sql=` argument accepts the same JSON DSL, not SQL.

```python
# Every reviewed field for the chosen port; no optics/event enrichment requests.
stats = client.execute_query({
    "table": "port_statistics", "scope": {"vf_id": 10},
    "keys": {"port": "0/12"}
})

# Retained events, with an explicit time window and global ordering.
events = client.execute_query({
    "table": "events", "scope": {"vf_id": 10},
    "lookback": "24h", "filter": {"event_source": "raslog"},
    "order_by": [{"field": "event_time", "direction": "asc"}], "limit": 2000
})

# Select an advanced resource without loading it into the primary catalog.
schema = client.get_schema("diag_brocade_maps__dashboard_history")
daily = client.execute_query({"table": schema.name, "scope": {"vf_id": 10}})

# Chassis resources reject a virtual-fabric override.
hardware = client.execute_query({"table": "hardware"})
```

Primary tables: `switches`, `ports`, `port_statistics`, `transceivers`, `connected_devices`, `fabric_links`, `zones`, `zone_members`, `health`, `congestion_samples`, `events`, `hardware`.

Omit `fields` for all reviewed columns. Projection retains declared identity fields, native observation timestamps and `_bow_*` evidence columns. `keys` means scalar equality on declared identity fields; both keys and filters are currently applied locally after a bounded collection GET. Scalar filters support `eq`, `ne`, `in`, `gt`, `gte`, `lt`, `lte`; comparisons must match column types. Arrays remain arrays and do not support scalar filtering. WWPN equality is case-insensitive. `order_by` is a list of `{field, direction}` objects. Time bounds accept `lookback` or an offset-aware `start_time`/`end_time` pair; end is exclusive.

The default ceiling is 10,000 source/normalized rows, 8 MiB per response, a 30-second request timeout and a 120-second query deadline. Exceeding a ceiling or `limit` fails rather than silently truncating evidence. Narrowing local filters cannot rescue a source response that exceeds the transport budget. Keyed URI/GET-body optimizations require separately verified vendor contracts and are not enabled.

Preserve the distinctions: instantaneous rates versus lifetime counters; credit-zero transitions versus stall time; optical power in microwatts; current topology versus historical topology; defined versus effective zoning; collection completeness versus historical retention. MAPS daily values remain native encoded arrays. No interpolated history or counter deltas are manufactured. Unknown fields are not exposed until reviewed; missing fields stay null.

## Baseline regeneration and real-switch acceptance

`generate_catalog.py` needs the public Brocade 9.1.0b YANG package and the build-time `pyang` dependency. It records source hashes, inherited types/units and include/exclude dispositions. Runtime does not depend on pyang or PyFOS.

```sh
python tools/brocade/generate_catalog.py /path/to/yang/9.1.0/9.1.0b
```

Sources: [Brocade YANG](https://github.com/brocade/yang/tree/master/9.1.0/9.1.0b), [PyFOS](https://github.com/brocade/pyfos), [Brocade Ansible](https://github.com/brocade/ansible). Catalog metadata is derived from the vendor models; sources retain their upstream notices/licenses. The chosen logo is credited in the research document.

Before calling this customer-version verified, reconcile the applicable 9.1.1c contract, replay sanitized real responses, and run read-only acceptance against the customer's hardware/roles/FIDs. Supply live credentials through environment variables to the verifier; omit `--allow-http`. Record firmware, model, resources, permissions and response provenance separately. The simulator's `v9.1.1c` identity is synthetic, not a compatibility certificate.
