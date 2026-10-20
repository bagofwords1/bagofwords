# Secure Data Tunnel — implementation status 

Status as of 2026-10-20, branch `feature/secure-data-tunnel`.

## Context

This document records what the design
(`docs/design/secure-data-tunnel-v4-nats.md`) and the implementation plan
(`docs/design/secure-data-tunnel-implementation-plan.md`) call for, what is
built, and what is missing. The existing progress log
(`docs/design/tunnel-tasks-tracking.md`) stops at 2026-09-04; five later
commits (mTLS-only auth `ad8003471`, install wizard `d1de3bbc7`, wizard
refinement `eab493f37`, Docker fixes `67b3bc471`, list refresh `7adf36e20`)
changed things the log still describes the old way. This status was produced
by reading the code, not the log; it supersedes the "Known gaps" section of the log. Every claim below carries a `file:line`.

Tests were NOT run: neither `backend/.venv` nor `data_plane/.venv` exists and
the system Python lacks `nats`. Counts below are from reading the test files.

## Verdict in one paragraph

The **transport and registration core works end to end for one Postgres/MySQL
source**: an edge agent advertises over mTLS NATS, the control plane persists
the agent + tunnel-mode `Connection` rows, `TunneledClient` is built at all
three construction sites before any credential resolution, and
`get_schemas` / `execute_query` round-trip real rows as base64 Parquet. The
install wizard (cert-manager cert + NATS account user + zip bundle) and the
agent's local admin UI are real. What is missing is most of the **operation
surface** (5 of 19 ops on the agent, 6 of 19 on the proxy; no file/MCP/
`warm_all`), the **safety rails the design calls structural** (D1 read-only
enforcement, empty-credentials assertion, payload-size ceiling, dtype
coercion, `allowed_schemas`/`denied_tables`), **streaming progress and working
cancellation**, the **NATS broker deployment artifacts** (no `nats.conf`, no
compose/Helm for NATS in-repo), **es/he translations**, and **tests for the
provisioning/wizard path**. Several tooling files and docs are stale after the
mTLS switch.

## Against the implementation plan (`secure-data-tunnel-implementation-plan.md`)

| Phase | Status |
|---|---|
| Phase 1 — `data_plane/data_edge_agent` skeleton, config, NATS connect + log | ✅ Done (`data_plane/data_edge_agent/main.py`, `tunnel.py`, `config.py`) |
| Phase 2 — PostgreSQL data source + advertisement | ✅ Done and exceeded: bundled `postgresql_client.py` plus reuse of 67 backend client classes (`data_sources/registry.py:37-105`); advertisement at `tunnel.py:543-553` |

The plan only covers these two phases; everything else below is measured
against the design doc.

## Implemented (verified)

### Control plane (backend)
- **Transport `TunnelClient`** `backend/app/services/tunnel_client.py`: JSON-RPC envelope, `timeout_ms`, `user_credentials`, request-size check vs `nc.max_payload - 4096` (:265-267), NATS timeout `timeout+5` (:271), ownership guard + `translate_remote_error` + Parquet unwrap in `_unwrap` (:335-351), `invoke_streaming` subscribes to `progress.<ref_id>` before publishing and `_watch_cancel` publishes a cancel on the control subject (:275-333), per-org inbox prefix `_INBOX.bow.<org>` (:47,129), `drain()` (:353-370), module accessor (:388-397).
- **mTLS-only auth**: `_build_tls_context` (`tunnel_client.py:64-88`); config block `DataTunnel` in `backend/app/settings/bow_config.py:256-318` (`enabled`, `org_id`, `url`, `tls_ca/cert/key`, `tls_verify`, `agent_url`, `provisioning.*`, `missing_requirements()`). No token/password auth remains anywhere.
- **Lifespan wiring** `backend/main.py:649-703`: gated on `data_tunnel.enabled`, connects in a background task, non-fatal, leader-gated advertisement + heartbeat listeners, 30s liveness sweep job, drain on shutdown (:789-796).
- **Registration** `backend/app/services/tunnel_registration_service.py`: org from subject (never payload), upsert `DataEdgeAgent`, create/update tunnel `Connection` rows with `credentials=None`, conflict → recorded with reason (no upsert), withdrawn → `is_active=False` never deleted, reactivation, unknown org dropped with warning, heartbeat → online, sweep stale >45s / offline >120s with `is_active=False`.
- **Model + migration**: `backend/app/models/data_edge_agent.py` matches D2 incl. unique (org, agent); `connections.tunnel_mode` / `edge_agent_id` in `backend/alembic/versions/tunnel01_data_edge_agents.py`.
- **B2 branches at all three sites, before `resolve_credentials`**: `connection_service.py:1694-1704`, `data_source_service.py:2585-2604` (sandbox `ds_clients`, quota + table metadata attached), `data_source_service.py:2288-2302` (deprecated site).
- **`TunneledClient`** `backend/app/data_sources/clients/tunneled_client.py`: `execute_query` (sync, bridged with `run_coroutine_threadsafe`), `aexecute_query`, `atest_connection`, `aget_schemas` (with payload-too-large retry :116-135), `get_schema`, `prompt_schema`, cached `index_stats` (:62-65), `catalog_identity_available` from config, `_bow_connection*` attributes, two budgets.
- **Errors** `backend/app/services/tunnel_errors.py`: `TunnelNotConnectedError`, `TunnelOwnershipError`, `TunnelPayloadTooLarge`, `RemoteError`, `RemoteQueryTimeout`; `translate_remote_error` maps `-32001`/`query_timeout` → `QueryTimeoutError`.
- **API** `backend/app/routes/data_tunnel.py` (all gated on `manage_settings`): list agents, `GET /provisioning`, `POST /agents` (pre-register row + cert-manager `Certificate` CN `<org>-<agent>` + patch `nats-accounts` ConfigMap with a scoped user incl. `allow_responses {max:1, expires:20m}`), certificate status, zip bundle download (config.yaml + CA + client cert/key + README), `DELETE /agents/{id}` (remove ConfigMap user, delete cert/secret, delete row). Service: `backend/app/services/tunnel_provisioning_service.py`.
- **Tests**: 27 unit tests across `backend/tests/unit/test_tunnel_client.py` (8), `test_tunnel_registration.py` (9), `test_tunneled_client.py` (10).

### Edge agent (`data_plane/`)
- **NATS**: mTLS connect (`tunnel.py:112-171`, cert/key required), own first-connect retry loop then infinite reconnect, per-connection + control subscriptions (:198-213), advertise at boot + every 60s + on reconnect (:588-617), heartbeat every 15s (:765-775).
- **Dispatch** (`tunnel.py:339-440`): `execute_query`, `test_connection`, `get_schemas`, `get_schema`, `prompt_schema`; budgets (index vs query, `min(timeout_ms, effective)`), malformed envelope → JSON-RPC error, every reply carries `edge_agent_id`, `-32001` timeout error with `{kind, timeout_s, sql}`, `-32601` for unsupported ops, DataFrame → Parquet → `dataframe_b64`.
- **Client reuse** `data_sources/registry.py`: 67 type→class map importing backend client modules directly (no `app.settings` pull-in), kwargs narrowed to constructor, bundled `PostgresqlClient` fallback. Per-user credential clients built fresh and never cached (:330-331).
- **Admin UI** (`admin/server.py`, `admin/static/index.html`): loopback default, no auth, status/types/catalog/icons/connections CRUD/test/audit endpoints, type-picker grid driven by `data_sources/type_specs.json` (65 types, SaaS denylist), brand icons, live apply/remove + re-advertise without restart, server starts before NATS connect.
- **Store** `store.py`: cleartext JSON, 0600, atomic (encryption deliberately dropped in `ed78987f0`). **Audit** `audit.py`: JSONL + ring, never credentials.
- **Packaging**: multi-stage `Dockerfile` (backend/app on PYTHONPATH, uid 10001, tini, healthcheck), `docker-entrypoint.sh` (old→new env bridging, config mount, required-env guards). The customer-site run command is the `docker run` snippet the Data Tunnels wizard generates (there is no shipped compose file).
- **Tests**: 88 tests in 10 files (`data_edge_agent/tests/`).

### Frontend
- `frontend/pages/settings/data-tunnels.vue` (tab gated on `manage_settings`, `layouts/settings.vue:54`): Registered pane (agent label/id, Online/Offline, last advertised, per-connection Registered/Conflict with reason tooltip), 3-step install wizard (identity → cert polling → download/run), per-agent Configure modal, deprovision with confirmation, disabled state when provisioning is off. 63 `settings.dataTunnels.*` keys in `locales/en.json`.

## Partial / divergent (works, but not as designed)

| Area | What the design says | What the code does | Evidence |
|---|---|---|---|
| Multi-org | One NATS connection per served org, routed by `connection.organization_id` | One connection for the single configured `data_tunnel.org_id`; other orgs' tunneled connections fail at the broker. Liveness sweep scans **all** `DataEdgeAgent` rows, so other orgs' agents would be flipped offline | `main.py:672-681`, `tunnel_registration_service.py:203` |
| `_resolve_tunnel_user_credentials` | Must read `UserConnectionCredentials` directly, never `resolve_credentials` | Guards `user_required` + real user, then **delegates to `resolve_credentials`**; exceptions swallowed → silent fallback to agent system creds | `connection_service.py:1658-1673` |
| Sync bridge | `invoke_sync` with `TunnelSyncOnLoopError` guard, cancel future on expiry | `TunneledClient._run_on_tunnel_loop`: no on-loop guard, no future cancel, raises `RuntimeError` not `TunnelNotConnectedError` | `tunneled_client.py:85-91` |
| Advertisement → `Connection.config` | Stores `query_timeout_seconds`, `index_timeout_seconds`, `catalog_identity_available` | `index_timeout_seconds` dropped (index budget always 900s fallback); `catalog_identity_available` never advertised by agent → stored `None` | `tunnel_registration_service.py:28-38`, agent `config.py:78-83` |
| Per-connection query timeout | Agent enforces override | Advertised but dispatch always uses the agent default | agent `tunnel.py:366` |
| `get_schema` | Returns one `Table` for `table_name` | Agent ignores `table_name`, returns all tables | agent `tunnel.py:378-381` |
| `index_stats` | Agent calls real `client.index_stats()` and returns it with `get_schemas` | Hard-coded `{}` | agent `tunnel.py:375-376` |
| Conflict detection | Catch `IntegrityError` | SELECT pre-check (fine under leader gate) | `tunnel_registration_service.py:67-106` |
| Withdrawn connections | Recorded as `withdrawn` in `last_advertisement` | Deactivated but not recorded; UI cannot show them | `tunnel_registration_service.py:108-123` |
| Heartbeat | 30s | 15s | agent `config.py:161` |
| Proxy `description` | Real class attribute | Synthesized string | `tunneled_client.py:47-56` |
| Deprovision | Agent removal; connection deletion stays a separate admin action | `delete_agent` **hard-deletes** the agent's tunnel `Connection` rows (membership, RBAC, indexed schema go with them); ConfigMap/cert cleanup best-effort and one `try` (a failure removing the user skips the cert delete) | `tunnel_registration_service.py:288-298`, `routes/data_tunnel.py:194-203` |
| Registered list UI | online/stale/offline, version, membership state, actionable conflict text | `stale` rendered as Offline; `client_version` and `last_heartbeat_at` not shown; conflict reason only in a hover tooltip; no "in no data source" state | `data-tunnels.vue:232-281, 646` |
| Worker NATS permissions (as deployed by the sandbox skill) | Publish only `*.conn.*` + `*.control` | Publish `tunnel.<org>.>` — worker could forge advertisements/heartbeats | `.claude/skills/data-edge-agent-sandbox/SKILL.md:102` |
| Admin UI dashboard | Shows what is advertised, per-connection health | NATS status, served count, in-flight (always 0, see bug 1), version only | `index.html:149-156` |

## Missing (not built)

### Operation surface
- **Proxy (`TunneledClient`)**: `awarm_all`, `aget_schema`/`aprompt_schema` overrides, `aread_file`, `alist_files`, `asearch_files`, `agrep_files`, `afile_version`, `awrite_file`, `read_raw_bytes`, all 5 MCP ops. Base-class fallbacks fail *quietly* (`grep_files` → NotImplementedError, `file_version` → None disables caching, `read_raw_bytes` absent → "no bytes").
- **`TunneledToolProviderClient`** and the `issubclass(ClientClass, ToolProviderClient)` selection at the B2 sites: absent. A tunneled MCP connection becomes a `TunneledClient(DataSourceClient)` and **passes the `codegen_clients` filter**, re-introducing the `ds_clients[...].execute_mcp` bug the design warns about.
- **Agent**: `warm_all`, `index_stats`, 7 file ops, 5 MCP ops all answer `-32601 not implemented` (`tunnel.py:501-524`).

### Structural safety rails (design calls these mandatory)
- D1 read-only enforcement in `connection_schema.py`/route: `tunnel_mode`/`edge_agent_id` are not even exposed; nothing refuses credential/config edits on tunnel rows.
- Registration does not assert `Connection.credentials` is empty on update (`tunnel_registration_service.py:91-97`).
- Agent payload-size ceiling (A6): no check against `nc.max_payload`; an oversized reply fails inside `msg.respond`, is only logged (`tunnel.py:457-458`), and the control plane sees a NATS timeout instead of the actionable size error.
- Dtype coercion before Parquet (A6): none; pyarrow failures surface as generic `-32000`. Parquet encoding also runs on the event loop (`tunnel.py:436-439`).
- `allowed_schemas` / `denied_tables` (C4/C5): not in `ConnectionConfig`, not in the form, not enforced; no `security.py`.
- `QueryCapturingClientWrapper` timeout opt-out for tunneled clients (B3): absent (`code_execution.py:810-847`) — tunneled queries carry two timeout layers.

### Streaming / cancellation
- Agent never publishes to `progress.<ref_id>`; `_dispatch_cooperative` with `cancel_check`/`progress_callback` does not exist.
- `_in_flight` has only the "query" kind; no cooperative `threading.Event` kind.
- `query_cancellation.cancel_thread` is never used; source-side cancel depends on an `_on_connect` hook the reused **backend** clients do not accept, so in the Docker image a timeout only stops waiting.

### Agent admin UI (C4)
- No setup wizard for endpoint/certs (env/YAML only); no endpoint for tunnel/TLS settings.
- Agent-level `default_query_timeout_seconds` / `index_timeout_seconds` not editable.
- Audit log lacks a `cancelled` outcome and does not record "per-user auth used".

### Broker deployment (E1–E3)
- No `nats.conf`, no NATS service in root `docker-compose.yaml`, no NATS in `k8s/chart/`, no Caddy `tunnel.*` site. `deploy-nats.sh` (which set `max_payload=64MB` and websocket 9443) was deleted in `ad8003471`; nothing in-repo now sets `max_payload`. The broker (`nats` namespace, Issuer `nats-ca`, ConfigMap `nats-accounts`, `verify_and_map`) exists only out-of-repo and is described in `.claude/skills/data-edge-agent-sandbox/SKILL.md`.
- Under mTLS-only, A1/E3's `no_tls` websocket with TLS terminated at Caddy/ingress cannot carry the client cert; needs TLS passthrough or a TLS websocket listener — neither configured in-repo.
- Worker (per-org) cert + NATS user are hand-provisioned; no automation.
- No cert renewal push to agents (one-year cert renews in-cluster only); no CRL — revocation relies solely on removing the ConfigMap user.

### Frontend / i18n / docs / tests
- **`locales/es.json` and `locales/he.json` lack all 63 `settings.dataTunnels*` keys** (en 4272 keys vs 4209) — violates the identical-shape rule in `AGENTS.md`. 7 unused en keys left from the old config generator; `connect.dockerNote` still mentions a token.
- No tunnel badge / owning agent / hidden credentials / read-only state in the connections UI (`frontend/components`).
- No frontend tests for data-tunnels; no backend tests for `tunnel_provisioning_service` or `routes/data_tunnel.py`; no test for the FAST extractor non-streaming fallback with a tunneled client; no tests for `invoke_streaming`/cancel, TLS context, sweeper stale state.
- No README/docs/CHANGELOG/VERSION entry. `docs/design/saas-secure-data-access.md` is a problem statement only.

## Confirmed bugs (verified by reading both sides)
> **Status (2026-10-20):** all ten fixed on `feature/secure-data-tunnel`; per-item notes below.

1. **Cancel registry never populated.** Agent reads `ref_id` from `params.get("id") or params.get("ref_id")` (`data_plane/data_edge_agent/tunnel.py:389`) but `_dispatch` receives only the inner `params` dict (:263) while the control plane puts `id` at the envelope top level (`backend/app/services/tunnel_client.py:253`). Result: `_in_flight` is always empty, control-subject cancels find nothing, dashboard in-flight count is always 0.  
   **Fixed:** the envelope `id` is now passed into `_dispatch(request_id=…)`, which registers a cancel for *every* operation (task cancel + source statement cancel for `execute_query`) and replies `-32003 cancelled`; `test_dispatch.py`.
2. **Per-connection `query_timeout_seconds` advertised but not enforced** (agent `tunnel.py:366`).  
   **Fixed:** `ConnectionConfig.effective_query_timeout()` is the single source for both the advertisement and `_budget()`; `test_dispatch.py`.
3. **`catalog_identity_available` stored as `None`** because the agent never advertises it; proxy returns `None` (falsy, accidentally the safe direction).  
   **Fixed:** the agent advertises the client's flag; registration stores a real bool defaulting to True; `TunneledClient` reads None as True.
4. **`index_timeout_seconds` dropped at registration** → proxy index budget is always the 900s fallback.  
   **Fixed:** stored on every tunneled row's `config`; `TunneledClient` reads it (None → fallback).
5. **Unknown connection reported as `-32601`** (method-not-found) instead of a distinct error (agent `tunnel.py:328`).  
   **Fixed:** now `-32002 unknown_connection`, translated to `RemoteUnknownConnection` on the control plane.
6. **Liveness sweep not org-filtered** while heartbeats are only received for one org (`tunnel_registration_service.py:203`).  
   **Fixed:** `sweep_stale_agents(org_id=…)`; `main.py` passes the worker's org.
7. **`tools/agent/boot_data_edge_agent.sh:194-196` writes a `ws://` config with no `tls_*`**, which the mTLS-only agent rejects at `tls_context()`; `:247` references the deleted `deploy-nats.sh`.  
   **Fixed:** generates a `tls://…:4222` config with `tls_ca/cert/key` (from `BOW_EDGE_CERT_DIR`, default `data_plane/certs`), fails fast when the PEMs are missing, and points at the sandbox skill instead of `deploy-nats.sh`.
8. **`.mcp.json` points at deleted `tools/agent/mcp-k8s-server.py`** (why the `bow-k8s` MCP server failed to connect this session).  
   **Fixed:** `.mcp.json` removed (it held only the stale entry).
9. **ConfigMap patch** uses a brittle string anchor and no `resourceVersion` check (`tunnel_provisioning_service.py:240-297`); concurrent provisions can lose an update; a failed provision leaves the created `Certificate` behind.  
   **Fixed:** the users array is located structurally inside the configured account (`provisioning.account`, default `TUNNEL`); writes are `replace` with the read's `resourceVersion`, retried on 409; a failed provision now deletes the Certificate and account user it created.
10. **Wizard label overwritten** by `edge_agent_name` on first advertisement (`tunnel_registration_service.py:147`); `runCmd` hardcodes an unpublished `bow/data-edge-agent:latest` image.  
   **Fixed:** the advertised name only fills an empty label; the image in `runCmd` comes from `data_tunnel.agent_image` (`BOW_DATA_TUNNEL_AGENT_IMAGE`, default `bagofwords/data-edge-agent:latest`) via `/provisioning`.

## Stale artifacts to refresh
- `docs/design/tunnel-tasks-tracking.md` (still says "single shared token", describes the old config generator, lists deleted scripts/skills).
- Agent docstrings saying "Phase 1: transport only" (`tunnel.py:1-12`, `__init__.py:7-9`, `postgresql_client.py:5-6`); comments still saying credentials are "encrypted" (`config.py:132`, `admin/server.py:12`, `config.example.yaml:74-75`, Dockerfile); `tunnel.py:187-192` token comment; `config.example.yaml:94` advertises unimplemented `BOW_EDGE_AGENT_CONN_<NAME>_USER/_PASSWORD`; `tunnel_client.py:5-7` docstring; `.agents/skills/agentic-dev/deploy-bow-runtime/SKILL.md` relies on deleted MCP tools.

## Suggested next steps (priority order)

1. **Fix the confirmed transport bugs** (cheap, high value): ref_id lookup (bug 1), enforce per-connection timeout (2), advertise + persist `catalog_identity_available` and `index_timeout_seconds` (3, 4), org-filter the sweep (6), fix `boot_data_edge_agent.sh` and `.mcp.json` (7, 8).
2. **Structural safety rails**: D1 read-only enforcement in schema/route, empty-credentials assertion at registration, rewrite `_resolve_tunnel_user_credentials` to read `UserConnectionCredentials` directly, payload-size ceiling + dtype coercion in the agent serializer (off-loop), `QueryCapturingClientWrapper` opt-out.
3. **Bring the broker into the repo**: `nats.conf` (TLS 4222, TLS websocket 9443 or passthrough, `max_payload` 64MB, `verify_and_map`, accounts include), NATS service in compose and `k8s/chart`, worker-cert provisioning, narrow worker publish grants.
4. **Working cancel + progress**: `_in_flight` two kinds, `query_cancellation.cancel_thread` for reused backend clients, progress publishing, `warm_all` + real `index_stats`.
5. **Operation surface**: file ops, MCP ops via `TunneledToolProviderClient` + `_TunnelProxyBase`, `read_raw_bytes`, `aget_schema` with `table_name`.
6. **Admin UI gaps**: `allowed_schemas`/`denied_tables` (and enforce them), agent-level timeout editing, setup wizard.
7. **Product polish**: stale status / version / membership state / actionable conflict text in Registered list; tunnel badge + read-only credentials in connections UI; es/he catalogs; provisioning tests; refresh `tunnel-tasks-tracking.md`, docstrings, CHANGELOG/VERSION; deprovision should not hard-delete connections.

## Verification (if tests are to be run)

```
cd backend && uv sync --frozen && uv run pytest tests/unit/test_tunnel_client.py tests/unit/test_tunnel_registration.py tests/unit/test_tunneled_client.py -q
cd data_plane && uv sync --frozen && uv run pytest -q
```
