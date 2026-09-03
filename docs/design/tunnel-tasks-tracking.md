# Secure Data Tunnel — Task Tracking

Running log of implementation work on the secure data tunnel (design:
[`secure-data-tunnel-v4-nats.md`](./secure-data-tunnel-v4-nats.md)). Newest
session first.

Status: ✅ done & verified · 🟡 done, not fully verified · ⛔ not started / follow-up

---

## 2026-09-03 (cont.) — agent-local admin UI (design C4)

Scope: the localhost-only admin UI the design calls for (C4) — the operator's
window onto the agent on the customer's own network, and the one place system
credentials are entered (they never cross the tunnel). Previously `admin_port`
was a reserved config value with nothing behind it.

- ✅ **Encrypted connection store** (`store.py`) — JSON on disk; per-connection
  `credentials` Fernet-encrypted at rest, config kept clear so an operator can
  read what's configured. Key from `BOW_EDGE_AGENT_STORE_KEY`, else a generated
  `<store>.key` (0600). Atomic writes; a wrong key is a loud error, never silent
  credential loss.
- ✅ **aiohttp admin server** (`admin/server.py`) — bound to `admin_host`
  (127.0.0.1 by default; loopback is the security boundary, no auth). JSON API:
  `GET /api/status`, `GET /api/types` (67), `GET/POST/PUT/DELETE
  /api/connections[/{name}]`, `POST /api/connections/{name}/test` + `POST
  /api/test` (unsaved). Credential *values* never leave the process — a
  connection reports only which credential keys are set. Body capped at 256 KiB.
- ✅ **Self-contained SPA** (`admin/static/index.html`) — dashboard (agent
  identity, live NATS status, served/in-flight counts, version) + connection
  cards with Test/Edit/Delete + an add/edit form (type dropdown, host/port/
  database/schema/user/password, advanced-JSON config, per-connection timeout).
  Vanilla JS, no build step. Blank credential fields keep the stored value.
- ✅ **Dynamic connection lifecycle** (`tunnel.py`) — `apply_connection` /
  `remove_connection` add or drop a served connection at runtime (per-connection
  subscription tracking) and re-advertise, so a UI save takes effect with **no
  restart**; `test_connection_config` runs a throwaway client off the loop;
  `status_snapshot` feeds the dashboard.
- ✅ **Wiring** (`main.py`, `config.py`) — store connections merge over the YAML
  config at start-up (store wins by name); admin server starts after subscribe,
  and a busy admin port degrades to "no UI" rather than taking the agent down.
  New config: `admin_enabled`, `admin_host`, `store_path`, `store_key`.
- ✅ **Tests** (`test_store.py` 7, `test_admin_server.py` 10) — encryption at
  rest, wrong-key error, blank-keeps-existing, CRUD, credential hygiene, test
  saved/unsaved. Full data_plane suite: 37 passed.
- ✅ **Verified live in the rig** — served on `127.0.0.1:9192` (9191 is the pod's
  upload server). API + page load; created a `demo-mysql-2` connection through
  the UI → agent logged `connection.applied` and re-advertised 3 connections
  with no restart, control plane persisted `registered=3`, store row carried a
  Fernet `credentials_enc` (no cleartext); Test succeeded for saved + unsaved;
  Delete dropped it back to 2 and re-advertised. Browser screenshots of the
  dashboard and the edit form (credentials shown as `•••• (set)`) confirmed.

Notes / follow-ups: no auth (loopback-only by design); the admin server starts
after the NATS connect succeeds, so while the broker is down the UI is not yet
reachable — acceptable for v1 (its main read is NATS status), worth revisiting.
Real deployments keep the design's `admin_port: 9191`; only the rig moves it to
9192 to avoid the upload server.

**C4 items still NOT built** (this pass did connection CRUD + test + dashboard +
live re-advertise; C4 lists more):
- ⛔ **Setup wizard** — NATS URL/token on first launch. NATS config is still
  file/env only, and the UI can't set it. Note the chicken-and-egg: the server
  only comes up after NATS connects, so a wizard to *fix* a bad NATS URL needs
  the server to start before connect.
- ⛔ **Per-connection security constraints** — `allowed_schemas` / `denied_tables`
  (C4/C5). Not in the form, not in `ConnectionConfig`, and — the bigger half —
  not enforced on either side. A real posture gap, not just a missing input.
- ⛔ **Local audit log** — every operation with timestamp, duration, row counts
  (C4 + C5 "full local audit trail"). Nothing is recorded today.
- 🟡 **Agent-level timeout defaults not editable in the UI** — `default_query_
  timeout_seconds` / `index_timeout_seconds` load from config and ARE advertised
  (build_advertisement carries index_timeout; per-conn query_timeout via
  `advertised()`), but the UI can only set the per-connection override, not the
  agent defaults.

---

## 2026-09-03 (cont.) — edge agent multi–data-source support (reuse backend clients) + MySQL proof

Scope: make the edge agent serve any on-prem data source Bow supports, not just
PostgreSQL, and prove it end to end with a second engine (MySQL). Chosen
strategy: **reuse the control plane's real clients** (`backend/app/data_sources/
clients/*.py` on `PYTHONPATH`) rather than reimplement per-type clients in
`data_plane` (design B1, Step 5).

- ✅ **Reuse registry** (`data_plane/data_edge_agent/data_sources/registry.py`) —
  `resolve_client_class` imports the real backend client class for a type via an
  embedded `type → "module.Class"` map (67 entries, mirroring
  `data_source_registry.py`). Imports the client **module directly** rather than
  the backend registry, which transitively pulls in `app.settings`
  (fastapi_mail / pydantic-settings) — the "no client import drags in app config"
  check. Bundled `PostgresqlClient` remains a fallback for a stripped image.
- ✅ **`construct_client` narrows kwargs** to the client constructor's signature,
  so a connection can carry agent-side keys the client doesn't know.
- ✅ **`PYTHONPATH=$ROOT/backend`** exported in `boot_data_edge_agent.sh`; the
  agent's `pyproject.toml` gained `pydantic-settings` + `pymysql`.
- ✅ **`_on_connect` compatibility fix** (`data_plane/.../tunnel.py`) — the
  source-side statement-cancel hook (A9/C3) is postgres-specific; the dispatch
  now passes `_on_connect` only when the client's `execute_query` accepts it.
  Reused backend clients (MysqlClient, …) that lack it still get
  cancel-on-timeout by abandoning the wait — just no source-side statement kill.
- ✅ **MySQL end-to-end proof** — deployed a `mysql:8.4` Deployment/Service in
  the rig (demo DB: `products`, `orders`), added a `demo-mysql` tunneled
  connection. Verified from the **control plane** over NATS: advertisement
  persisted (registered=2), `test_connection` ok via the status sweep,
  `TunneledClient.aget_schemas` → 2 tables, `aexecute_query` (GROUP BY) → correct
  result via parquet round-trip, all served by the backend's own `MysqlClient`.
- ✅ **Resolution coverage** — postgres/mysql/mariadb resolve to real backend
  classes; clickhouse/mongodb miss only for absent drivers (add the driver to
  `data_plane` to enable), which is the intended per-type gating.

Gap: `config.yaml` (repo) and the `boot_data_edge_agent.sh` default heredoc both
carry the `demo-mysql` entry now, but the live rig config (`/tmp/bow-agent/
edge-agent.yaml`) is hand-pinned to the real org UUID — the generated default
uses the `cust-b` placeholder org, which won't match a seeded backend org.

---

## 2026-09-03 (cont.) — production-hardening: heartbeat, cancel, error translation

The three control-plane items that make a single postgres source production-solid.

- ✅ **Heartbeat / status & connection deactivation (B4/A10) — verified live.**
  Edge agent publishes to `tunnel.<org>.<agent>.heartbeat` every 15s
  (`heartbeat_forever`). Control plane: leader-gated `start_heartbeat_listener`
  → `record_heartbeat` (updates `last_heartbeat_at`, status→online) + a
  leader-gated 30s scheduler job `sweep_stale_agents` that flips status to
  `stale` (>45s) / `offline` (>120s) and sets `Connection.is_active=False` on
  offline. Verified in-cluster: stopping the agent walked online→stale (t+90s)→
  offline+is_active=0 (t+150s). Unit-tested (record + sweep).
- ✅ **Remote-error translation (B3) — done, unit-tested.** Edge agent reports a
  query timeout as JSON-RPC `-32001` with `{kind:query_timeout, timeout_s, sql}`;
  `translate_remote_error` rebuilds `QueryTimeoutError` so the codegen retry loop
  behaves as in direct mode.
- 🟡 **Cancellation (A9/C3) — implemented, not live-verified.** Control plane
  `_watch_cancel` polls the caller's `cancel_check` and publishes a cancel to the
  control subject. Edge agent keeps an `_in_flight` registry and, for
  execute_query, runs in a tracked thread that registers psycopg2
  `connection.cancel()`; the control handler cancels by `ref_id`, and a timeout
  now **abandons the wait AND cancels the statement on the source**. Unit-adjacent
  only; a live mid-query cancel was not driven.
- Tests: 21 backend + 20 agent = 41 pass.

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

## Known gaps / follow-ups (control-plane, bow-app side)

Assessed 2026-09-03 against the architecture doc. Registration, the Data Tunnels
tab, schema discovery, and query execution are done; the items below are not.

### Makes a single postgres source production-solid
- ✅ **Heartbeat / status transitions (B4, A10)** — done and verified live.
- ✅ **Remote-error translation (B3)** — done (query timeout → QueryTimeoutError).
- 🟡 **Cancellation (A9)** — implemented (control-plane cancel publish + edge-agent
  statement cancel via psycopg2); not live-verified.
- ⛔ **D1 read-only enforcement in the API (B2)** — the read-only rule for tunneled
  connections is UI-only; not enforced in `connection_schema.py`/route, and
  `register_advertisement` sets `credentials=None` but does not assert-and-refuse
  a tunnel row that has credentials.

### Needed only beyond one postgres source
- ⛔ **Remaining A5 operations on `TunneledClient` (B1)** — file ops
  (`read_file`/`list_files`/`search_files`/`grep_files`/`file_version`/
  `write_file`/`read_raw_bytes`) and MCP ops are unimplemented.
- ⛔ **`TunneledToolProviderClient` (B1)** — second proxy for tunneled MCP tool
  providers; not present.
- 🟡 **Streaming progress (A8)** — `invoke_streaming` subscribes to the progress
  subject but the agent never publishes; long indexes show no live progress.

### Posture / infra
- 🟡 **Per-user credentials for `user_required` tunneled connections (A7)** —
  `_resolve_tunnel_user_credentials` handles `system_only` (returns None,
  correct); the per-user forwarding path is thin.
- ⛔ **A11 scoped NATS users** — single shared token, so per-agent subject scoping
  / tenancy is not broker-enforced.
- 🟡 **Loop-ownership sync bridge (B3)** — uses `run_coroutine_threadsafe` rather
  than the design's exact `invoke_sync` + stashed-loop; works on the verified
  path.
- ⛔ **Advertisement requires a matching `Organization`** — an advertisement whose
  `org_id` has no `Organization.id` is logged `unknown_org` and dropped.
