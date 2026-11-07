# F5 BIG-IP connector (iControl REST)

Native, read-only HTTPS connector for BIG-IP **TMOS 15.x–17.x** through iControl REST
(`/mgmt/tm/...`). No F5 SDK, MCP server or appliance licence is needed for local
development; a synthetic API in this folder reproduces the wire contract. Real
appliance verification is still required before customer acceptance (role
permissions, partitions, version-specific stat names, remote-auth providers).

## Use in BOW

Add **F5 BIG-IP** under Connections. Supply the management origin
(`https://bigip.example:443`), an account with a read-only role (Guest or Auditor;
remote-auth LDAP/RADIUS/TACACS users work through token login with
`login_provider`), and the trusted PEM CA if the appliance certificate is not
publicly trusted. TLS verification is always on. The HTTP option exists only for
the loopback simulator below.

`get_tables()` exposes 26 tables from the frozen catalog in
`backend/app/data_sources/clients/f5_bigip/catalog.json`:

| Group | Tables |
|---|---|
| LTM config | `virtual_servers`, `pools`, `pool_members`, `nodes`, `irules`, `profiles_http`, `profiles_client_ssl`, `ltm_monitors_http`, `partitions` |
| LTM live status (`/stats`) | `virtual_server_stats`, `pool_stats`, `pool_member_stats`, `node_stats` |
| Certificates | `ssl_certs` (`expirationDate` epoch seconds, `expirationString`) |
| Device / HA | `cm_devices`, `cm_failover_status`, `cm_sync_status` |
| System | `sys_version`, `sys_clock`, `sys_hardware`, `sys_cpu`, `sys_memory`, `sys_memory_tmm` |
| Network | `net_interfaces`, `net_vlans`, `net_self` |

```python
client.execute_query({"table": "pool_member_stats", "filter": {"status.availabilityState": "offline"}})
client.execute_query({"table": "pool_members", "parent": {"pool": "/Common/web_pool"}})
client.execute_query({"table": "virtual_servers", "filter": {"partition": "App1"},
                      "fields": ["fullPath", "destination", "pool", "enabled"]})
client.execute_query({"table": "virtual_server_stats", "name": "/Common/vs_web_http"})
client.execute_query({"table": "ssl_certs", "fields": ["fullPath", "subject", "expirationDate"]})
```

Controls: `table`, `fields`, `filter`, `parent`, `name`, `limit`. `filter.partition`
is pushed to the device as `$filter=partition eq X`; every other filter is an
exact-match (or list-membership) filter applied client-side after a complete fetch.
`limit` is a complete-result ceiling (`ResultLimitExceeded` instead of truncation).
Results are pandas DataFrames with dotted columns for nested scalars, structured
arrays, and `_bow_*` provenance columns. Each query opens a token session
(`POST /mgmt/shared/authn/login`), sends `X-F5-Auth-Token`, re-logs-in once on a 401
(expired token) and always deletes its token (`DELETE /mgmt/shared/authz/tokens/<t>`).

The client is incapable of mutation: an allow-list derived from the catalog only
admits GETs to catalogued collections, objects (`~Partition~name`), `members`
subcollections and `/stats`, plus its own login/logout. No `tmsh`, `bash`,
transactions, raw URLs or SQL.

## Run the simulated API

Choose local throwaway values; never use customer credentials with the simulator.

```bash
# From the repository root
export F5_USERNAME='<local-test-user>'
export F5_PASSWORD='<local-test-password>'
python3 tools/f5_bigip/simulated_api.py --port 18093            # optional: --token-timeout 5
```

```bash
B=http://127.0.0.1:18093
TOKEN=$(curl -s -X POST $B/mgmt/shared/authn/login -H 'Content-Type: application/json' \
  -d "{\"username\":\"$F5_USERNAME\",\"password\":\"$F5_PASSWORD\",\"loginProviderName\":\"tmos\"}" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"]["token"])')
curl -s -H "X-F5-Auth-Token: $TOKEN" "$B/mgmt/tm/ltm/pool?\$top=2&\$filter=partition+eq+Common"
curl -s -H "X-F5-Auth-Token: $TOKEN" "$B/mgmt/tm/ltm/pool/~Common~web_pool/members/stats"
curl -s -X DELETE -H "X-F5-Auth-Token: $TOKEN" "$B/mgmt/shared/authz/tokens/$TOKEN"
```

In BOW create the connection with URL `http://127.0.0.1:18093` and **Allow HTTP for a
simulator** enabled.

### What the simulator models

- Token handshake exactly as documented: `POST /mgmt/shared/authn/login` with
  `{"username","password","loginProviderName"}` → `token.token` (26 upper-case
  alphanumerics), `timeout` 1200 s by default (`--token-timeout` to shorten),
  `PATCH /mgmt/shared/authz/tokens/<t>` `{"timeout": n}` (406 above 36000),
  `DELETE /mgmt/shared/authz/tokens/<t>`, 100-token-per-user limit (400), and HTTP
  Basic as an alternative (real appliances accept both; the connector only uses tokens).
- 401 bodies for a missing header, unknown/expired token and bad login; 404
  `01020036:3: The requested Pool (/Common/nope) was not found.` for unknown objects;
  **403 for every POST/PUT/PATCH/DELETE on `/mgmt/tm`** (the mock refuses mutation).
- OData paging on collections: `$top`, `$skip`, `$select`, `$filter=partition eq X`
  (`+` or `%20`), `ver`, with `currentItemCount`, `itemsPerPage`, `pageIndex`,
  `startIndex`, `totalItems`, `totalPages`, `nextLink`, `previousLink`, `selfLink`
  (host `localhost`, comma in `$select` encoded as `%2C`).
- `expandSubcollections=true` inlining `membersReference.items` (pools) and
  `profilesReference.items` (virtual servers); `/stats` on collections and single
  objects with `entries` → `nestedStats.entries` → `{"value"}` / `{"description"}`.
- Estate: partitions Common + App1; 6 virtual servers (one disabled), 6 pools, 15
  members (two monitor-down, one forced offline, two `user-disabled`, two
  unchecked), 10 nodes, 3 iRules, 4 certificates (one expires in 10 days, one
  expired 30 days ago), 3 client-ssl profiles (one bound to the expiring cert, one to
  the expired cert), 3 HTTP monitors (one echoing an encrypted probe `password`
  that the connector strips), an HA pair (`cm/device` active/standby,
  `cm/failover-status` ACTIVE, `cm/sync-status` In Sync), TMOS 17.1.1, one hot CPU core.

### What it does not model

ASM/APM/GTM modules, `tmsh`/`bash` utilities, transactions, `$select` on stats
endpoints, `$top` on `/stats` collections, data groups, real counters over time
(values are deterministic per object, not changing), remote-auth providers other
than `tmos`, and platform-specific `sys/hardware` groups. See `SOURCES.md` for which
shapes are verified against documentation/fixtures and which are inferred.

## Verify

```bash
cd backend && mkdir -p db
export TESTING=true BOW_DATABASE_URL=sqlite:///db/f5_bigip-tests.db
uv run python -c "from app.schemas.data_source_registry import resolve_client_class; print(resolve_client_class('f5_bigip'))"
uv run pytest tests/unit/test_f5_bigip_client.py -q --db=sqlite
uv run pytest tests/e2e/test_f5_bigip.py tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

The unit suite includes an in-process run of this simulator over real HTTP (every
primary table, a filtered query, a 404, a token expiring between two requests).
