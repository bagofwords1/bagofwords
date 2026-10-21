# Secure Data Tunnel — open issues in what is built

Prioritized list as of 2026-10-21, branch `feature/secure-data-tunnel`.
Scope: defects and design divergences in code that already exists. Features
not yet built are tracked in `secure-data-tunnel-status.md` ("Missing").

Verified today: `data_plane` tests 110 passed; backend tunnel unit tests
(`test_tunnel_client`, `test_tunnel_registration`, `test_tunneled_client`,
`test_tunnel_provisioning_accounts`) 47 passed.

Status key: ⛔ open · 🟡 in progress · ✅ fixed

## Tier 1 — breaks the security promise or destroys data

| # | Status | Issue | Evidence | Fix |
|---|---|---|---|---|
| 1 | ⛔ | **Credential isolation is data-dependent, not structural.** `_resolve_tunnel_user_credentials` delegates to `resolve_credentials` (which returns *system* credentials for `system_only` and on the `current_user=None` indexing path) and swallows every exception. Registration never asserts `Connection.credentials` is empty. `connection_schema.py` does not expose `tunnel_mode`/`edge_agent_id`, so nothing refuses a credential/config edit on a tunnel row. Works today only because the column happens to be empty. | `backend/app/services/connection_service.py:2058-2074`; `tunnel_registration_service.py:91-97`; `backend/app/schemas/connection_schema.py` (no tunnel fields) | Read `UserConnectionCredentials` directly; assert-and-refuse non-empty credentials at registration; add read-only tunnel fields to the schema and reject credential/config writes on tunnel rows in the route. Design B2, D1. |
| 2 | ⛔ | **Deprovision hard-deletes the agent's tunnel `Connection` rows**, taking data-source membership, RBAC grants, indexed schema and query history with them. Irreversible. | `backend/app/services/tunnel_registration_service.py:316-326` (`delete_agent`) | Delete only the `DataEdgeAgent` row; deactivate its connections. Connection deletion stays the normal admin action. Design A10, D1. |
| 3 | ⛔ | **Worker NATS grant is too wide.** The sandbox skill provisions the worker with publish on `tunnel.<org>.>`, so a worker can forge advertisements and heartbeats. | `.claude/skills/data-edge-agent-sandbox/SKILL.md:102-103` | Publish only `tunnel.<org>.*.conn.*` and `tunnel.<org>.*.control`; subscribe to advertisements, heartbeat, progress and `_INBOX.bow.<org>.>`. Design A11/E3. |

## Tier 2 — wrong or opaque behavior in normal use

| # | Status | Issue | Evidence | Fix |
|---|---|---|---|---|
| 4 | ⛔ | **Oversized / odd results fail opaquely.** No agent-side payload ceiling against `nc.max_payload`, no dtype coercion before Parquet, and `df.to_parquet` runs on the event loop. A too-large reply fails inside `msg.respond` and is only logged; the control plane sees a NATS timeout. A JSONB/`Decimal`/`bytes` column surfaces as a generic `-32000`. A big frame stalls heartbeats and the control subject while it encodes. | `data_plane/data_edge_agent/tunnel.py:502-510` | Encode in `asyncio.to_thread`; coerce `dict`/`list`→JSON string, `Decimal`→float/str, `bytes`→base64, fail with column+dtype otherwise; check encoded size vs `max_payload - headroom` and return the actionable size error. Design A6. |
| 5 | ⛔ | **Cancel/timeout leaves the statement running on the source for reused backend clients** (MySQL, …): they do not accept the `_on_connect` hook, so only the wait is abandoned. Each codegen retry starts another query on the customer DB. | `data_plane/data_edge_agent/tunnel.py:482-499` | Run `execute_query` in a daemon thread and register `(client, thread_ident)`; cancel via `app.data_sources.query_cancellation.cancel_thread`, which the reused clients already populate in `connect()`. Report the real outcome. Design C3. |
| 6 | ⛔ | **Two timeout layers on tunneled queries.** `QueryCapturingClientWrapper._call_with_timeout` has no opt-out for `TunneledClient`, so a control-plane timeout can fire first and report the wrong failure while holding no connection to cancel. | `backend/app/ai/code_execution/code_execution.py` (no `tunnel` reference) | Skip the wrapper timeout when the wrapped client is a tunnel proxy; keep capture/timing. Design B3/B5. |
| 7 | ⛔ | **Sync bridge is unguarded.** `_run_on_tunnel_loop` has no on-loop deadlock check, does not cancel the future on expiry, and raises `RuntimeError` instead of `TunnelNotConnectedError`. Latent today; a deadlock when it bites. | `backend/app/data_sources/clients/tunneled_client.py:89-95` | Implement `TunnelClient.invoke_sync` per design B3 (`TunnelSyncOnLoopError`, `future.cancel()` on timeout, `TunnelNotConnectedError`) and have the proxy call it. |

## Tier 3 — functional divergence, limited blast radius

| # | Status | Issue | Evidence | Fix |
|---|---|---|---|---|
| 8 | ⛔ | **Single org only.** One NATS connection for the configured `data_tunnel.org_id`; other orgs' tunneled connections fail at the broker. | `backend/main.py:829-851` | One org-scoped connection per served org, routed by `connection.organization_id`. Design A11/B4. |
| 9 | ⛔ | **`get_schema` ignores `table_name`** and returns every table. | `data_plane/data_edge_agent/tunnel.py:466-469` | Call the real client's `aget_schema(table_name)`. |
| 10 | ⛔ | **`index_stats` is hard-coded `{}`** on the agent, so the proxy's cached stats are always empty. | `data_plane/data_edge_agent/tunnel.py:460-464` | Return `client.index_stats()` with the `get_schemas` response. |
| 11 | ⛔ | **Withdrawn connections are deactivated but not recorded** in `last_advertisement`, so the UI cannot explain a disappearance. | `backend/app/services/tunnel_registration_service.py:108-123` | Record `{"status": "withdrawn"}` per withdrawn name. Design A10/D2. |
| 12 | ⛔ | **Progress is never published** by the agent although `invoke_streaming` already subscribes; long indexes look frozen. | agent `tunnel.py` (no `progress.` publish) | Pass a `progress_callback` that publishes to `tunnel.<org>.<agent>.progress.<ref_id>`. Design A8. |

## Tier 4 — polish and hygiene

| # | Status | Issue | Evidence | Fix |
|---|---|---|---|---|
| 13 | ⛔ | **`es`/`he` catalogs lack all `settings.dataTunnels*` keys** (en 5535 keys, es 5441, he 5461). Fails the catalog sync check in `docs/design/i18n.md`; blocks merge. | `locales/*.json` | Translate the 63 keys; drop the 7 unused en keys and the token mention in `connect.dockerNote`. |
| 14 | ⛔ | **Registered list gaps:** `stale` renders as Offline; `client_version` / `last_heartbeat_at` not shown; conflict reason only in a hover tooltip; no "registered but in no data source" state. | `frontend/pages/settings/data-tunnels.vue` | Render the three statuses, version, heartbeat age, inline conflict text, membership state. Design H15. |
| 15 | ⛔ | **Stale artifacts.** `secure-data-tunnel-implementation-plan.md` and `-system-prompt.md` are staged for deletion but uncommitted. Docstrings/comments still describe token auth, encrypted stores and "Phase 1: transport only" (`tunnel.py:1-12`, `config.py:132`, `admin/server.py:12`, `config.example.yaml:74-75,94`, `tunnel_client.py:5-7`). `tunnel-tasks-tracking.md` still partly describes the old generator. | see list | Commit the doc deletions; refresh comments and the tracking log. |

## Suggested order

Items 1–3 before any new feature work (the feature's pitch rests on item 1).
Then 4–7 together (agent serializer + cancel + bridge, one PR each side).
8–12 as they become relevant to the next source type. 13 before merge.
