# Secure Data Tunnel — Task Tracking

Running log of implementation work on the secure data tunnel (design:
[`secure-data-tunnel-v4-nats.md`](./secure-data-tunnel-v4-nats.md)). Newest
session first.

Status: ✅ done & verified · 🟡 done, not fully verified · ⛔ not started / follow-up

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
- ⛔ **TunneledClient / query transport (B2/B3)** — the read side that actually
  routes queries over the tunnel is not built; this work covers registration and
  visibility only.
- ⛔ **A11 scoped NATS users** — the rig uses a single shared token, so subject
  scoping / per-agent tenancy enforcement is not in effect.
