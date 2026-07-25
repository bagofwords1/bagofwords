# Feedback Loop — Entra sso_only login + Fabric/Power BI OBO agents + MCP user context, full-stack e2e

Full simulation of an enterprise deployment against the **real**
`bow14.onmicrosoft.com` Entra tenant, run 2026-07-25 in a cloud sandbox:

- `auth.mode: sso_only` with an `entra` OIDC provider (login scopes
  `openid profile email`, `sync_groups: true`, `resolve_group_names: true`),
  Enterprise license from `BOW_LICENSE_KEY` (env).
- Admin (`yochayettun@…`) signs in via SSO → first user → auto-org → `admin`.
- Two connections created through the UI with the master Power BI SP
  (`fb405177-…`) as system credentials and **Require user authentication**
  (`auth_policy=user_required`, modes `["oauth"]`, OAuth app = login app
  `a9010cd3-…`): **Power BI** and **Microsoft Fabric** (warehouse `demo_db`),
  one agent each.
- Members `demo1` (group **AllFabric**) and `demo2` (**MinimalFabric**) invited
  via the UI, sign in via SSO, then connect their own Microsoft identity to both
  connections (OBO), and run real reports through the chat UI with a real
  Anthropic model (Claude 4.5 Haiku).
- An **MCP** connection (real streamable-HTTP echo server) forwards the
  signed-in user's email as a header + locked `custom_metadata`.

## Sandbox constraints (and the workarounds used)

Two egress facts about this sandbox shaped the loop — neither is a product bug:

1. **The hosted Microsoft login page can't render in headless Chromium** (the
   egress proxy rejects Chromium's TLS handshake; curl/python through the same
   proxy work). Every interactive Microsoft hop was therefore completed
   server-side with **real Entra tokens** via the ROPC grant, driving the exact
   production code paths:
   - App SSO login → `UserManager.oauth_callback` + `_sync_oidc_groups_on_login`
     + JWT mint (scratchpad `sso_login_ropc.py`), then the browser session is
     established by visiting `/users/sign-in?access_token=…` — the same URL the
     real callback redirects to.
   - Per-user connection sign-in → the `connections/oauth/callback` sequence
     (upsert `UserConnectionCredentials` → `test_user_connection` → overlay
     sync), as in `tools/agent/e2e_obo_signin_ropc.py`.
2. **Raw TDS egress (port 1433) is impossible** — the proxy CONNECTs but resets
   any non-443 TLS stream (verified with plain OpenSSL, ALPN `tds/8.0`, and
   `Encrypt=strict`). Live Fabric SQL can therefore never work from this sandbox
   (the prior `fabric-obo-second-admin-tables` loop hit the same wall). The
   Fabric leg ran with an **out-of-repo, env-guarded shim**
   (`BOW_SANDBOX_FABRIC_SHIM=1`, scratchpad `shim/sitecustomize.py`) that
   patches `pyodbc.connect` **only for the demo endpoint hostname**, backing it
   with DuckDB seeded with the exact `dbo.sales` / `dbo.finance` data and
   emulating the tenant's `HAS_PERMS_BY_NAME` GRANT/DENY behavior. Identity is
   decoded from the **real Entra access token** BOW passes via
   `attrs_before[1256]` (audience + expiry checked), so ROPC-acquired user
   tokens drive it and everything above pyodbc — connection creation, catalog
   indexing, per-user overlays, reload, agent SQL — runs the real product code.
   Power BI ran fully live (REST/DAX over 443).

## What was validated (all through the UI unless noted)

| Step | Result |
|---|---|
| sso_only sign-in page | Local form hidden, single "Sign in with entra" button; `/api/auth/entra/authorize` 200 with a real `login.microsoftonline.com` authorize URL |
| First SSO user | Auto-org "Main Org", role `admin`, `is_enterprise: true` |
| Group sync on login | demo1 → **AllFabric**, demo2 → **MinimalFabric** created+resolved via Graph `getByIds` on first login |
| LLM setup (`/settings/models`) | Anthropic provider, Claude 4.5 Haiku only, live test passed, `llm_models`: 1 row enabled+default |
| Power BI connection (UI) | Test 5.4s; save → "Discovered 6 model tables in 4s"; unreadable RLS model `geo (verify-rls)` correctly excluded with a backend warning |
| Fabric connection (UI) | Test 3.4s; "Discovered 2 tables in 1s" (`dbo.sales`, `dbo.finance`) |
| Wizard tables step (admin, pre-sign-in) | Full catalog + admin banner shown (per the obo-zero-tables fix); Reload → `/refresh_schema` 200 |
| Member invites (UI `/settings/members`) | demo1/demo2 pending memberships; SSO login attaches them |
| Per-user OBO sign-in (3 users × 2 connections) | All 6 succeed. Timings: token ~1s; PBI test 2.3–3.2s, overlay 2.7–9.4s; Fabric test <1s |
| Per-user scoping | PBI: 6/6/6 model tables for yochay/demo1/demo2. Fabric: yochay 2, demo1 2, **demo2 1 (`dbo.sales` only — DENY on finance honored)** — verified over API (`full_schema`) and in the UI as each user |
| Reload tables as user | demo1 (PBI) and demo2 (Fabric) → `/refresh_schema` 200, per-identity results (see **Bug 1**) |
| e2e report, demo1 × Fabric | "Total Sales Amount by Region" — bar chart US $3,200 / EMEA $1,500 (matches seeded rows), `create_data` 6.0s, fulfilled |
| e2e report, demo1 × Power BI | "SalesPush Total Customer Count" — **live DAX** via the user's delegated token → **40 customers**, 10.4s, KPI card. Whole turn 23s wall-clock |
| e2e report, demo2 × Fabric (finance) | No data leak — execution allowlist blocked every attempt; see **Bug 2** for the context-scoping problem it exposed |
| MCP user-context forwarding | Echo server received `x-user-email: demo1@bow14.onmicrosoft.com` header AND locked `custom_metadata.user_email` alongside model-authored args; MCP test 0.9s, report turn ~35s |

## Bugs / hiccups found

### 1. MAJOR — a restricted user's Reload rewrites (and shrinks) the shared canonical catalog

`ConnectionService.refresh_schema` (`backend/app/services/connection_service.py:956`)
indexes with the **caller's** credentials whenever the caller has a per-user
token (`index_user = current_user`), then **upserts into the shared
`ConnectionTable` catalog and hard-deletes every canonical row the fetch didn't
return** ("Delete ConnectionTable entries for tables that no longer exist",
~line 1224). For identity-scoped sources (Fabric GRANT/DENY, Power BI workspace
permissions) "not visible to this caller" ≠ "no longer exists".

Observed live: demo2 (MinimalFabric, DENY on `dbo.finance`) clicked **Reload**
on the Fabric agent's Tables view →

- `connection_tables` for the Fabric connection dropped `dbo.finance`;
- the agent's shared `datasource_tables` lost `dbo.finance` too;
- demo1 (AllFabric) and the admin then saw **1 table instead of 2** — a
  least-privileged member's reload fail-closed the whole org's catalog;
- recovery is lossy: an admin re-reload recreates `dbo.finance` but with
  `is_active=0` — the agent's table **selection is permanently lost**.

The per-user overlay tables (`user_connection_tables` /
`user_data_source_tables`) are the right sink for identity-scoped fetches; the
canonical catalog should only ever be written from the system identity
(`current_user=None`), or at minimum the delete branch should be skipped when
indexing ran under a delegated identity. Note the same applies to admins: the
admin's reload also indexes under the admin's own token (observed
`identity=yochayettun` in the query log), which happens to be harmless only
because this admin sees everything.

**Is this a recent regression?** The caller-identity canonical write predates
the last week — `refresh_schema` already used
`construct_client(db, connection, current_user)` before `e77829f` (2026-07-15),
which only added the system-creds fallback for *credential-less* callers. But
the last two commits touching this path, `ccdd45f` and `a78225c`
(both 2026-07-24), doubled down on the caller-identity crawl (reusing the
user-fetched catalog for the overlay sync; incremental reload) without guarding
the destructive canonical delete. `ccdd45f` was validated live with an admin +
a second member — on a Power BI tenant where **both users saw the same table
set**, so the shrink was invisible there; this run's Fabric GRANT/DENY split
(demo1 vs demo2) is what exposed it.

### 2. MAJOR — cross-user planner-context leak: the process-wide schema cache ignores identity

demo2 (DENY on `dbo.finance`, overlay = `dbo.sales` only) asked the Fabric
agent for "total budget by department in the finance table". The **planner
context** (`context_snapshots.context_view_json`, `schemas_usage`) contained
`{"name": "dbo.finance", "columns_count": 3, …}` and the model echoed it back
("the table is listed as `dbo.finance` in the Fabric Agent data source").

Root cause (isolated by replay): the schema **builder itself scopes
correctly** — `SchemaContextBuilder` resolves `effective_auth` per user and
serves the per-user overlay (`schema_context_builder.py` `_resolve_user_access`
→ overlay branch). Re-running it for the same report gives:

```
user=demo2 → ['dbo.sales']              # correct
user=None  → ['dbo.sales', 'dbo.finance']  # full canonical
```

But `context_hub.py` caches the **built, identity-scoped** schema section in a
process-wide `_SCHEMA_CACHE` keyed by `(org_id, ds_ids, build_id)` — **the user
is not part of the key** (TTL 300s). Observed live in the backend log:

```
18:00:29  [context_hub:prime_static] schemas done (cache miss)   ← demo1's report builds it (demo1 sees finance)
18:01:06  [context_hub:prime_static] schemas cache hit (age=37.2s) ← demo2's report served demo1's schema view
```

So any user's schema context is served to **every other user** of the same
org + agent set for up to 5 minutes — restricted users inherit broader users'
table lists (names/columns), and broader users can inherit narrower views
(silently degraded reports). The identity-scoped schema work made the cached
value user-dependent; the cache key was never updated to include the identity
(or the resolved `effective_auth` class).

Execution *is* enforced: every `create_data` attempt failed with
`No active tables matched the requested patterns` (the per-user allowlist
excludes finance), so **no data leaked** — but metadata leaks across users,
and context/enforcement disagreement makes the model flail (it concluded "the
connection appears to be inactive" and asked the user to check connectivity
instead of a clean "you don't have access to that table").

Fix direction: add the user id (or `effective_auth` + overlay fingerprint) to
the cache key, or bypass the cache whenever any attached connection is
`user_required`.

### 2b. A report started from one agent's page attaches ALL accessible agents

Every report created via the agent page's **+ New report** got *both* data
sources in `report_data_source_association` (PBI + Fabric — and demo2's Fabric
question was answered with suggestions to use the Power BI tables instead).
If that is by design (reports span agents), the agent-page entry point should
still scope or at least indicate the active agent; today the "per-agent"
framing is only cosmetic.

### 3. UX — agent-create wizard pre-selects every existing connection

On `/agents/new`, the Connections field arrives pre-populated with **all**
existing connections, and creating a new connection from the modal **appends**
to that selection. Creating "Fabric Agent" right after "PBI Agent" silently
produced a 2-connection agent (its Tables step showed 8 tables across both
connectors); the Power BI connection had to be detached afterwards
(`DELETE /data_sources/{id}/connections/{id}`). Expected: pre-select only the
just-created connection (or none).

### 4. Cosmetics / small frictions

- **"1 tables"** — the agent header count doesn't singularize
  (Fabric agent as demo2).
- The empty **"untitled report"** stays behind if a user opens **+ New report**
  and never sends a prompt (one leftover in the run; the first click also
  navigated slowly enough — Nuxt dev — that the editor wasn't interactable for
  ~30s, script-level flake worth knowing about).
- `POST /api/llm/test_connection` logs an opentelemetry
  "Failed to detach context" ERROR traceback on every streaming test — noise
  that looks like a real error in the backend log.
- Agents are created **private** and members get 403 on `full_schema` until the
  admin shares them (`is_public` or membership). Correct RBAC, but nothing in
  the create wizard tells the admin the new agent is invisible to members.
- Entra group sync on the admin's login created one group named by its **UUID**
  (`85f43b45-…`) alongside the resolved `PowerBI-ServicePrincipals` — Graph
  `getByIds` returned only one of the two claim ids (the other is likely a
  directory role or a group the app can't read); worth a fallback label.

## MCP — user email over the wire (validated)

`tests/mocks/echo_mcp_http_server.py` (real streamable-HTTP MCP server) on
`:3333`; MCP connection created through the UI ("Echo MCP", transport
`streamable_http`) with Advanced forwarding rules: header
`X-User-Email ← user.email` and **locked** metadata field
`user_email ← user.email`; Test Connection green in 0.9s; agent "MCP Agent"
created from it and made public. demo1 then ran a report through the chat UI
("Query the production orders for company 111 for this week…") — real Haiku
chose the tool and authored the natural arguments, and the capture file shows
the injected identity arriving over the wire:

```jsonc
"received_arguments": {
  "prompt": "Get production orders for this week (July 20-26, 2026)",  // model-authored
  "company": "111",                                                     // model-authored
  "custom_metadata": { "user_email": "demo1@bow14.onmicrosoft.com" }    // BOW-injected (locked)
},
"received_headers": { "x-user-email": "demo1@bow14.onmicrosoft.com", … }
```

(Mechanism documented in `mcp-user-context-forwarding.md`; this run confirms it
composes with sso_only + Entra-provisioned users end-to-end.)

## Repro pointers

- Stack: `tools/agent/boot_stack.sh --dev` with `BOW_CONFIG_PATH` pointing at a
  config with the `entra` provider enabled and `auth.mode: sso_only`
  (secrets via env: `BOW_ENTRA_CLIENT_SECRET`, `BOW_ENCRYPTION_KEY`,
  `BOW_LICENSE_KEY`).
- The numbered Playwright scripts + ROPC helpers used for this loop live in the
  session scratchpad (01_admin_signin → 09_mcp); they are session tooling, not
  repo code. Screenshots: `/tmp/bow-agent/e2e-media/`.
- Bug 1 minimal repro: user_required Fabric-style connection with a user whose
  identity sees a strict subset of tables → sign that user in → hit
  `GET /data_sources/{id}/refresh_schema` as them → canonical
  `connection_tables` for the connection now equals the subset, and other
  users' views shrink with it.
