# Elasticsearch on index privileges alone (no cluster `monitor`)

A customer's security team will not grant cluster privileges. Their estate is
several Elasticsearch API keys, each scoped to one index pattern (`eksa*`,
`ekpb*`, …) with index privileges only. Before this pass, the connector could
not be used by them at all: **the connection could not even be saved.**

Everything below was run against a real Elasticsearch 8.15.3 in Docker with
API keys created exactly that way (`"cluster": []`).

## Environment

```bash
dockerd &                                   # remote sandbox has no daemon running
docker run -d --name es-priv -p 9200:9200 \
  -e discovery.type=single-node -e xpack.security.enabled=true \
  -e xpack.security.http.ssl.enabled=false -e ELASTIC_PASSWORD=bowtest123 \
  -e "ES_JAVA_OPTS=-Xms1g -Xmx1g" \
  docker.elastic.co/elasticsearch/elasticsearch:8.15.3

# the sandbox disk sits above the 90% high watermark, so shards never allocate
# and index creation hangs — this is required before seeding, not optional:
curl -u elastic:bowtest123 -XPUT localhost:9200/_cluster/settings \
  -H 'Content-Type: application/json' \
  -d '{"persistent":{"cluster.routing.allocation.disk.threshold_enabled":false}}'
```

Seeded: `eksa-app-2026.08.09` + `…08.10` (rolling pair), a **hidden**
`eksa-archive`, alias `eksa-current`, data stream `eksa-stream-a`, plus
`ekpb-events` and `finance-secret` that the `eksa` key must never see. Keys:

```json
{"name": "eksa-key",
 "role_descriptors": {"r": {"cluster": [],
   "indices": [{"names": ["eksa*"], "privileges": ["read", "view_index_metadata"]}]}}}
```

## Loop A — the reproduction

`GET /` maps to `cluster:monitor/main`, granted only by cluster
`monitor`/`manage`/`all`:

```
403 {"type":"security_exception","reason":"action [cluster:monitor/main] is
     unauthorized for API key ... this action is granted by the cluster
     privileges [monitor,manage,all]"}
```

`test_connection()` returned `{"success": false}` with no `reachable` flag, so
`ConnectionService.create_connection` hard-blocked the save with HTTP 400
(`connection_service.py:304` blocks only when `reachable` is falsy), and every
`connection_status_sweep` pass flipped `is_active` off. The connector's own
calls were never even reached.

## What each privilege actually buys

Measured with three keys, each scoped to `eksa*`, no cluster privileges:

| grant | test_connection | catalog | search / aggs / ES\|QL |
|---|---|---|---|
| `view_index_metadata` only | pass | all 4 tables | **403 on every query** |
| `read` only | pass | 403, no tables | works |
| `read` + `view_index_metadata` | pass | all 4 tables | works |

The metadata-only row is the trap: green test, fully indexed catalog, and then
every question fails. `test_connection` now names it explicitly ("Warning: the
credentials appear to be missing `read` on `eksa*` — queries will fail"), via
`_has_privileges`, which any authenticated user may call for their own grants.

## How ES resolves targets (measured, not assumed)

| request (key scoped to `eksa*`) | result |
|---|---|
| `GET /eksa*,ekpb*/_mapping` | **200** — an unauthorized *wildcard* is filtered out silently |
| `GET /eksa*,finance-secret/_mapping` | **403** — an unauthorized *name* rejects the whole request |
| `GET /eksa*,finance-secret/_mapping?ignore_unavailable=true` | **200**, returns the `eksa*` mappings |
| `GET /nomatch-*/_mapping` | 200 `{}` |
| `POST /finance-secret/_search` | **403** |
| `POST /finance-secret/_search?ignore_unavailable=true` | 200, zero hits |
| `POST /_sql` / `POST /_query` on an unauthorized index | **400 `verification_exception: Unknown index`** |

Two consequences drove the fix: one *named* unreadable target used to cost the
whole catalog, and the lenient params rescue even that case.

## The fix

`backend/app/data_sources/clients/elasticsearch_client.py`

- **Probe ladder in `test_connection`** — `GET /` (version, needs cluster
  `monitor`) → `GET /_security/_authenticate` (any authenticated user, no
  privilege) → `GET /{patterns}/_mapping` (needs only what the connector
  already needs). `reachable: True` on any HTTP answer, so a privilege problem
  never reads as an unreachable host.
- **Per-pattern discovery** with `ignore_unavailable` + `allow_no_indices`
  instead of one comma-joined request.
- **Aliases fetched separately and scoped** — they shared the mappings' try
  block, so an alias 403 returned an empty catalog.
- **Total failure raises** instead of `print()` + `[]`; the message lands on the
  indexing row and in the connection test.
- **Hidden expansion for explicit patterns** — `eksa-archive` (hidden) would
  otherwise vanish from a connection scoped to `eksa*`; this also pulls data
  streams' `.ds-*` backing indices into the bulk call, skipping the fallback.
- **`index_pattern = "*"`** no longer disables the `.`-index filter wholesale.
- 403/401 errors carry an actionable hint, and the configured scope is stated
  in the agent-facing connection description.

## Loop B — live verification

`scratchpad/verify_es.py` (client against the live cluster), then the full
backend at `localhost:8000` with the restricted keys:

```
A. POST /api/data_sources                     200   (was 400 — the actual blocker)
B. GET  .../test_connection                   success, reachable
      "Connected to Elasticsearch as API key 'eksa-key' (owner: elastic). The
       cluster `monitor` privilege is not granted, so the version is unavailable
       — index access is unaffected."
C. catalog  ['eksa-app-*', 'eksa-archive', 'eksa-current', 'eksa-stream-a']
            nothing from ekpb* or finance-secret
D. connection stays is_active / status success  (the sweep no longer disables it)
E. second key -> second connection -> ['ekpb-events'] only
F. a key with no index grants is rejected at the SCHEMA gate, quoting the
   cluster: "Connected but cannot read schema: ... [403] ... action
   [indices:admin/mappings/get] is unauthorized ..."
```

Then a real agent turn (Claude Haiku 4.5) over the restricted connection. The
generated step:

```python
query = '''{"index": "eksa-current", "query": {"match_all": {}},
            "aggs": {"by_level": {"terms": {"field": "level", "size": 100}}},
            "size": 0}'''
df = ds_clients["elastic-eksa:elasticsearch-3"].execute_query(query)
# -> [{"level": "error", "doc_count": 2}, {"level": "warn", "doc_count": 1}]
```

Correct for that alias, through a key holding no cluster privileges.

## What to tell a customer in this shape

One connection per API key, `index_pattern` set to that key's pattern, and per
key:

```json
{ "cluster": [],
  "indices": [{ "names": ["eksa*"], "privileges": ["read", "view_index_metadata"] }] }
```

`read` alone leaves them with no catalog; `view_index_metadata` alone leaves
them with a catalog that cannot answer anything. Both, or neither.

## Unrelated issue observed

Newly indexed tables land with `is_active = 0`, so a fresh connection's agent
reports "no queryable schema" until an admin activates them
(`PUT /api/data_sources/{id}/update_tables_status`). Expected product
behaviour, but it reads as a broken connector on first use — the first agent
run in this pass failed for exactly that reason.
