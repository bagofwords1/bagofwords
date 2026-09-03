# Secure Data Tunnel — Task Tracking

Running log of implementation work on the secure data tunnel (design:
[`secure-data-tunnel-v4-nats.md`](./secure-data-tunnel-v4-nats.md)). Newest
session first.

Status: ✅ done & verified · 🟡 done, not fully verified · ⛔ not started / follow-up

---

## 2026-09-03 (cont.) — single tunneled postgres source, functionally complete

Scope: make one tunneled PostgreSQL source fully usable (discover schema + run
queries). Postgres-only; file/MCP operations are out of scope for this type.

- ✅ **B2 branch at both `DataSourceService` sites** — `construct_clients`
  (sandbox `ds_clients`, site 3) and the deprecated `construct_client` (site 2)
  now return a `TunneledClient` for `tunnel_mode` connections, before any
  credential resolution. Quota + stored-table metadata still attach. With this
  the AI-agent/sandbox query path builds a tunneled proxy.
- ✅ **`execute_query` over the tunnel — verified with real data.**
  `select count(*) from lego_sets` → **11673** through
  TunneledClient/agent/postgres; a bad SQL query returns postgres's own error,
  correctly propagated. Needed **pyarrow** added to the edge agent
  (`data_plane/pyproject.toml`) — the query ran but parquet serialization failed
  without it.
- ✅ **warm_all/index_stats** — for postgres both are base no-ops on both sides,
  so `TunneledClient` correctly inherits the no-op `awarm_all` (identical to
  direct mode); no round trip added. (A real warm_all would need proxying for
  source types that implement it.)
- ✅ **Unit tests** — `test_tunneled_client.py` (8): invoke subject/payload,
  ownership guard, remote-error translation, parquet unwrap, payload-too-large;
  proxy instantiable/no-creds, aget_schemas→Tables+stats, aexecute_query→df.
  Backend tunnel tests now 23; agent tests 20.
- 🟡 **Sandbox query path not yet exercised end to end** — needs lego-pg attached
  to a DataSource and a query run through the AI agent; the transport itself is
  proven (execute_query returns real rows) and the construct sites are unit
  covered.
- ⛔ Still out of scope (not needed for one postgres source): file/MCP
  operations, streaming progress/cancel for long indexes, A11 scoped NATS users.

### Query transport — schema discovery over the tunnel (B1/B2/B3, C3)
- ✅ **`TunnelClient.invoke` / `invoke_streaming`** (`tunnel_client.py`, B3) —
  request/reply over `tunnel.<org>.<agent>.conn.<name>`, payload-ceiling check,
  ownership guard, parquet unwrap, per-request progress subject. Errors typed in
  `tunnel_errors.py`.
- ✅ **`TunneledClient`** (`backend/app/data_sources/clients/tunneled_client.py`,
  B1) — `DataSourceClient` proxy holding no credentials; `aget_schemas` +
  sync/async surface; bridges every call onto the tunnel's captured loop
  (`run_coroutine_threadsafe`) since callers run on the indexing/sandbox loops.
- ✅ **B2 branch** at `ConnectionService.construct_client` — `tunnel_mode`
  returns a `TunneledClient`; `_resolve_tunnel_user_credentials` never fetches a
  system credential (isolation is structural).
- ✅ **Edge agent dispatch** (`data_plane/.../tunnel.py`, C3) — real client
  factory (cached per connection), executes `get_schemas` / `test_connection` /
  `execute_query` / `prompt_schema` against the local `PostgresqlClient`,
  serializes `Table` objects; unsupported ops still answer JSON-RPC "not
  implemented".
- ✅ **Verified in-cluster, end to end** — `POST /api/connections/<lego-pg>/refresh`
  → construct_client (tunnel branch) → `aget_schemas` → NATS → edge agent →
  real postgres → **8 lego tables** discovered and upserted as `ConnectionTable`
  rows; indexing status `completed, table_count: 8`. Edge log shows
  `request.received` → `response.sent [get_schemas]`.
- 🟡 Only `get_schemas`/`test_connection`/`execute_query`/`prompt_schema` are
  implemented on the agent; the other A5 operations (files, MCP, warm_all
  streaming progress, cancel) are not yet. B2 branch added at site 1 only
  (`ConnectionService.construct_client`); the two `DataSourceService` sites
  (sandbox `ds_clients`, deprecated) still need the branch for query-time use.
- ⛔ Tests for `TunnelClient.invoke` / `TunneledClient` not yet written (verified
  live only); edge-agent dispatch tests updated (20 pass).

---

## 2026-09-02

### Control plane — `TunnelClient` and advertisement persistence
- ✅ **`TunnelClient`** (`backend/app/services/tunnel_client.py`, design B3/B4) —
  one NATS connection per worker; captures its own loop; `inbox_prefix=_INBOX.bow`;
  infinite reconnect; `drain()` on shutdown; module accessor
  `get_tunnel_client()` / `set_tunnel_client()`.
- ✅ **Settings** — `NATS_URL` / `NATS_TOKEN` in `backend/app/settings/config.py`
  (empty `NATS_URL` disables the tunnel; control plane uses the TCP client port
  4222, not the agents' websocket 9443).
- ✅ **Wiring** — `backend/main.py` startup connects every worker and starts the
  advertisement listener **leader-gated** (`try_acquire_scheduler_leader`);
  shutdown drains.
- ✅ **Advertisement persistence (A10 / D2)** — the handler now upserts, not just
  logs:
  - `DataEdgeAgent` model + migration `tunnel01` (`data_edge_agents` table;
    `tunnel_mode` / `edge_agent_id` added to `connections`).
  - `register_advertisement()` (`tunnel_registration_service.py`): upserts the
    agent row with the whole advertisement, creates/updates tunnel-mode
    `Connection` rows with **no credentials** (D1), rejects name conflicts,
    deactivates withdrawn connections (never deletes).
  - `org_id` is read from the NATS **subject**, never the payload.
- ✅ **API** — `GET /api/data-tunnels/agents` (`routes/data_tunnel.py`,
  `schemas/data_tunnel_schema.py`), gated on `manage_settings`.
- ✅ **Verified in-cluster** — edge agent → NATS → `TunnelClient` →
  `register_advertisement` → DB: `data_edge_agents` row (`nyc-01`, online) +
  tunnel `Connection` (`lego-pg`, postgresql, credentials NULL); API returns the
  advertised source.

### UI — Data Tunnels settings tab
- ✅ **Tab** — `frontend/pages/settings/data-tunnels.vue` lists each edge agent
  and its advertised data sources (online/stale status, conflict indicator).
- ✅ `layouts/settings.vue` tab entry (`manage_settings`); `locales/en.json` labels.
- ✅ **Verified in a browser** — seeded a loginable admin, opened
  `/settings/data-tunnels`, tab renders the live agent (`nyc-01`, online) and its
  advertised `lego-pg` (postgresql, Registered).
  - Note: a proper RBAC `role_assignment` is required, not just
    `Membership.role="admin"` — `whoami` uses `resolve_permissions_bulk`, which
    (unlike `resolve_permissions`) has no legacy-admin fallback, so a
    direct-DB-seeded admin is authorized by the API gate but shown member-only
    perms by the frontend until `_assign_system_role(..., "admin")` runs.

### Data plane — edge agent
- ✅ **Token auth** — `AgentConfig`/`EdgeAgentTunnel` switched from
  `nats_user`/`nats_password` to `nats_token` (`BOW_EDGE_AGENT_NATS_TOKEN`);
  docs/config/README updated. Verified connect + reject against live NATS.
- ✅ **Re-advertise on reconnect** — `_on_reconnected` now re-advertises
  immediately (design A10 line 699), instead of waiting for the timer.
- ℹ️ Advertisement publisher (boot + 60s timer) was already implemented.

### Tests
- ✅ Backend: `test_tunnel_client.py` (8) + `test_tunnel_registration.py` (7) —
  create / update / withdraw / reactivate / conflict, org-from-subject, malformed
  payload, drain, module accessor.
- ✅ Data plane: `test_tunnel.py` 19 pass (incl. advertise + reconnect).

### k8s test rig & tooling (supporting infra)
- ✅ `tools/agent/mcp-k8s-server.py` migrated to **mcp 2.x** (`MCPServer`).
- ✅ Split runtime into **two pods**, each with its own boot script and MCP tools:
  - `bow-runtime-app` → `boot_stack.sh --dev` (backend + frontend)
  - `bow-runtime-data-edge-agent` → `boot_data_edge_agent.sh` (in-cluster)
  - MCP tools: `bow_runtime_{app,data_edge_agent}_{deploy,start,status,delete}`,
    plus `kubectl_proxy`.
- ✅ `deploy-postgresql.sh` — `--status` prints connection string; defaults to a
  `lego` DB seeded with the LEGO sample dataset.
- ✅ `deploy-nats.sh` — `--status` prints live token/endpoints.
- ✅ `boot_data_edge_agent.sh` — in-cluster by default (cluster DNS, no
  port-forward); fixed `setsid`/`$!` pid capture so `--stop` is reliable.
- ✅ `boot_stack.sh` — `yarn dev --host` so the frontend binds all interfaces;
  same `setsid`/`$!` stop fix.
- ✅ Skills: `deploy-bow-app`, `deploy-bow-data-edge-agent` updated to the new
  tooling.

---

## Known gaps / follow-ups

- ⛔ **Advertisement requires a matching `Organization`** — an advertisement whose
  `org_id` (subject token) has no `Organization.id` in the DB is logged as
  `unknown_org` and dropped. Edge agents must be configured with a real org id.
- ⛔ **Heartbeat / status sweeper (A10/D2)** — `DataEdgeAgent.status` is set to
  `online` on advertisement but nothing flips it to `stale`/`offline` yet.
- 🟡 **TunneledClient / query transport (B1/B2/B3)** — `get_schemas` (schema
  discovery / list tables) works end to end; remaining A5 operations, the two
  other construct sites, streaming progress/cancel, and unit tests are pending.
- ⛔ **A11 scoped NATS users** — the rig uses a single shared token, so subject
  scoping / per-agent tenancy enforcement is not in effect.
