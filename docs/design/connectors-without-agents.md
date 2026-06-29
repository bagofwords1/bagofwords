# Connectors without Agents

**Status:** Design — partially implemented (DCR + tools-only data source shipped; see "Build status").
**Branch:** `claude/connectors-without-agents-tnoa6p`

> **Read this first — FINAL MODEL (authoritative).** The sections below "Final model"
> are earlier investigation/iterations kept for history; where they conflict (e.g.
> "members create their own connections", the `is_connector` boolean), the **Final
> model** wins.

## Final model (authoritative)

### Concept: Integrations vs Agents
- **Agent** — a custom, analytical data source (DB/files) the org builds: schema, instructions,
  evals, publishing. Unchanged.
- **Integration** — a pre-built **app connector** (Monday, Notion, Jira, Linear, Gmail, …):
  shared/public, **per-user identity**, runs through the **same agent loop**, but marked as a
  distinct `kind` and shown in its own UI category. No eval/publish/sub-agent surfaces; no shared
  instruction layer in v1.

A data source gets a discriminator **`kind = "agent" | "integration"`** (replaces the earlier
`is_connector` boolean — keep the same idea, better name; UI label **"Integrations"**).

### How an Integration is set up (OneDrive pattern)
One **shared connection + shared integration agent** per provider (NOT one per user):
- The connection is the shared **OAuth client / app shell** — carries no user data.
- `auth_policy="user_required"`: **each user clicks "Connect" and signs in with their own account**
  → their own token in `UserConnectionCredentials` keyed by `(connection, user)`. The agent runs
  tools as whoever is asking (OBO). Only the *app registration* is "global"; *identity* is per-user.

### Admin adds from the catalog (no auto-seed)
Nothing is seeded automatically. An **admin** adds connectors from the "Add connection" catalog —
the modal surfaces the curated set as named one-click tiles. Picking a tile opens the MCP form
**prefilled** with the provider's server URL + auth, so the admin just clicks **Connect**. The
popular **ready-out-of-box (DCR)** set — **Monday, Notion, Jira/Atlassian, Linear, Sentry** (all
verified DCR-capable, zero admin setup) — needs no client/secret; a "No setup" badge marks them and
the form shows a note that the connector self-registers (DCR) and each user signs in with their own
account. Non-DCR entries (GitHub/Gmail = `oauth_app`, Supabase = `bearer`) are also in the catalog
but require a client/token.

> Earlier iterations auto-seeded the DCR set on org creation (`connector_seed_service`, a startup
> backfill, and a `seed_default_connectors` flag). That was **removed** — admins add connectors
> explicitly. The catalog, DCR machinery, and per-user OAuth are unchanged.

### Per-entry catalog descriptor
Each catalog/registry entry declares:
- `auth`: `dcr` | `oauth_app` | `bearer` | `api_key` | `none`
- **`ready_out_of_box`** (derived): true when no admin action is needed before a user can connect —
  `dcr`, user-supplied `bearer`/`api_key`, or `oauth_app` whose client creds are **bundled** in the
  catalog. False for `admin_oauth`/`system_only` (admin must configure first). Drives UI:
  **"Connect"** vs **"Set up required (admin)"**.
- `auto_seed`: seed on org creation (only the popular ready-out-of-box set).
- governance: `enabled_for_members`, `allowed_auth`, optional role/group scope.

### Auth support (all via the same per-user flow)
- **MCP DCR** — discover (RFC 9728/8414) + dynamically register (RFC 7591); zero admin. *Built + verified vs Notion.*
- **MCP OAuth (admin/bundled app)** — preconfigured/bundled `client_id`; `ensure_mcp_oauth_config` no-ops and uses it. *Supported (same path).*
- **Bearer / API key** — user (or admin) supplies a token/PAT.
- **None** — public MCP.
Shared app/client per connection; per-user token (OBO). `client_secret` optional (public clients).

### Provider / auth matrix (probed live, 2026-06)
| Provider | Auth | Admin setup? |
| --- | --- | --- |
| Monday | DCR (`mcp.monday.com/register`) | none — auto-seed |
| Notion | DCR (`mcp.notion.com/register`) | none — auto-seed |
| Jira / Atlassian | DCR (`cf.mcp.atlassian.com/v1/register`, AS = `auth.atlassian.com`) | none — auto-seed |
| Linear | DCR (`mcp.linear.app/register`) | none — auto-seed |
| Sentry | DCR (`mcp.sentry.dev/oauth/register`) | none — auto-seed |
| Gmail | `oauth_app` — Google Cloud OAuth client (no official remote DCR MCP) | admin registers, or bundle in catalog |
| Supabase | `bearer`/PAT (no DCR at root) | user supplies a personal access token |
| GitHub | OAuth (no root AS metadata; needs app) | admin/app |

### Licensing — scoped by `data_shape`
Per-user auth (`user_required` / OAuth / OBO) is:
- **Free** for `data_shape ∈ {tools, files, objects}` — integrations (MCP, OneDrive/GDrive,
  popular apps). "Connect your own app" is table-stakes.
- **Enterprise** for `data_shape == "tables"` — per-user identity / OBO on warehouses/DBs
  (Snowflake, Postgres, Fabric, PowerBI, BigQuery). Plus **governance** (member allowlist,
  role/group scoping, audit) is Enterprise.
Gate change: require the enterprise license for `user_required` **only when `data_shape == "tables"`**
(the two checks in `connection_service.py`).

### Governance (admin decides what/how)
Org-level connector policy (Enterprise for the controls; the basic connect is free):
`member_self_serve: off | catalog_only(default) | allowlisted | any_url`; per-catalog-entry
`enabled_for_members` + `allowed_auth` + optional role scope; host allowlist (also the DCR SSRF
guard). The set of seeded/enabled connectors *is* the allowlist.

### Per-tool policy & isolation
- Integrations are shared (public) but per-user identity → no cross-user data leak (each user's
  token + per-user catalog). Any instructions on a shared integration are org-level guidance only;
  **no personal data on the agent**. (v1: no shared instruction layer on integrations.)
- Tool enable/policy (`allow|confirm|deny`) via `ConnectionTool`; `confirm` → at-prompt confirmation.

### Build status (current branch)
- ✅ Outbound MCP **DCR** (discover + RFC 7591) wired into the authorize route; public-client
  support; verified live vs **Notion** (direct 13/13, app-route 6/6, mock 11/11).
- ✅ tools-only data source runs through the agent (real Claude e2e 13/13); auto tool-discovery on
  create; schema-introspection skip for tool providers.
- ✅ `is_connector` flag + "/agents" badge (consolidate with the registry's existing
  `is_connection=false` / `ui_form="integration"` — one source of truth — when building).
- ❌ **Removed (cleanup):** the `create_private_connector` permission + the dynamic
  `create_data_source` route + its migration. The member-mints-own-connection path conflicted with
  the seeded shared-integration model (per-user connection sprawl). Reverted to admin-only create;
  members get integrations via **Connect** on seeded/admin agents (existing `IntegrationConnectionForm`
  already auto-creates a public agent; `useConnectionSignIn` already does the per-user OAuth/DCR redirect).

**Frontend already provides (reuse — discovered in KnowledgeExplorer audit):**
- `IntegrationConnectionForm.vue` — admin registers an integration, `user_required`+`oauth`, **auto-creates a public agent**.
- `AddConnectionModal.vue` — buckets integrations vs data sources (`is_connection`), routes by `ui_form`.
- `useConnectionSignIn.ts` + `needsSignIn()` + "Connect" badge — per-user OAuth redirect (now triggers DCR).

**Built this pass:**
1. ✅ **MCP presets on the registry + `GET /connectors/catalog`** — the named connectors (Notion, Linear, Monday, Sentry, Jira/Atlassian + GitHub/Gmail/Supabase) live as `McpPreset` entries on the registry's `mcp` entry (`presets=[…]`), each `key`/`title`/`server_url`/`transport`/`auth`. No separate catalog module — a preset is just a named instance of `type="mcp"`. Helpers: `mcp_presets()`, `mcp_preset(key)`, `allowed_dcr_hosts()` (DCR SSRF guard).
2. ✅ **Catalog tiles in `AddConnectionModal`** — a "Connectors" section renders the presets as named one-click tiles with provider icons; picking one opens `MCPConnectionForm` **prefilled** (server URL + DCR/oauth_app/bearer). DCR tiles show a "registers itself (DCR)" note in the form. Provider icons flow end-to-end via `connector_key` (connection `config.catalog_key` → list serializer → `DataSourceIcon :connector-key`). The catalog + data-source grid share one scroll container.
3. ✅ **`data_shape`-scoped license gate** (`_user_auth_needs_enterprise`) — per-user auth free for `tools`/`files`/`objects`, Enterprise only for `tables`. *Verified (unit).*
4. ✅ **DCR SSRF guard** — `ensure_mcp_oauth_config` restricts discovery/registration to catalog hosts. *Verified (non-catalog host blocked).*
5. ✅ **Post-connect tool discovery** — OAuth callback refreshes a tool-provider's tools with the user's token so integration agents get callable tools after Connect.
6. ✅ **DCR auth option in `MCPConnectionForm`** — "Sign in (auto-register / DCR)" choice needing only `server_url` (adds a custom DCR MCP; per-user OAuth).

**Removed this pass:**
- ❌ **Org-creation auto-seed** + startup **backfill** + the `seed_default_connectors` flag
  (`connector_seed_service.py` deleted). Admins now add connectors explicitly from the catalog.

**Remaining (optional / follow-up):**
- Bundle real brand SVGs (current `connector_icons/*.svg` are monogram placeholders).
- Consolidate `is_connector` with the registry's `ui_form="integration"` (one source of truth).
- Admin governance policy (member self-serve allowlist) + Enterprise role-scoping/audit; admin host-allowlist for non-catalog DCR URLs.

### Default DCR connectors (recommended zero-setup set)
Added from the catalog as ghost connections (`auth_policy="user_required"`,
`allowed_user_auth_modes=["oauth"]`, no client → DCR), public `integration` agents, `data_shape="tools"`,
per-user auth **free**. All verified DCR-capable (live probe, 2026-06):

| key | title | server_url | registration_endpoint | default_auth | DCR set |
| --- | --- | --- | --- | --- | --- |
| `monday` | Monday | `https://mcp.monday.com/mcp` | `https://mcp.monday.com/register` | `oauth` (DCR) | ✅ |
| `notion` | Notion | `https://mcp.notion.com/mcp` | `https://mcp.notion.com/register` | `oauth` (DCR) | ✅ |
| `atlassian` | Jira / Atlassian | `https://mcp.atlassian.com/v1/sse` | `https://cf.mcp.atlassian.com/v1/register` (AS `auth.atlassian.com`) | `oauth` (DCR) | ✅ |
| `linear` | Linear | `https://mcp.linear.app/mcp` | `https://mcp.linear.app/register` | `oauth` (DCR) | ✅ |
| `sentry` | Sentry | `https://mcp.sentry.dev/mcp` | `https://mcp.sentry.dev/oauth/register` | `oauth` (DCR) | ✅ |

Not in the DCR set (need a client/token — available on demand): **GitHub** (`oauth_app`, bundled or admin
app), **Gmail** (`oauth_app` + Google verification/Workspace approval), **Supabase** (`bearer`/PAT).

---

## Goal

Let an **admin** enable a connector (Monday, Gmail, …) for the org, and let a **regular member**
connect their *own private* connector with their own credentials, choose the tools, and use them
in a conversation — **without having to stand up a full analytical Agent** (schema, instructions,
publishing). The mainstream-AI-platform experience: "Connect Gmail → pick tools → chat."

**Key scoping decision (this revision): no custom run loop.** Connectors run through the
**existing agent loop**, exactly like OneDrive/GDrive do today. "Without agents" means *without the
user configuring a full analytical agent* — **not** a separate execution engine. There is no
lightweight loop, no connector-mode planner prompt, no `_connector_only` branch.

---

## Terminology

"data source" was renamed **"Agent"** in the UI (`frontend/pages/old_agents/`, `'Agent' rename`).

| Concept | Code entity | What it is |
| --- | --- | --- |
| **Agent / data source** | `DataSource` (`backend/app/models/data_source.py`) | A *container* of connections + (optionally) a catalog + instructions + memberships + scoping. **No `type` column** — type lives on each `Connection`. Already **polymorphic** across `data_shape` = `tables \| files \| objects \| tools` and `catalog_ownership` = `shared \| per_user \| none`. |
| **Connection** | `Connection` (`backend/app/models/connection.py`) | The wire to a system (postgres, google_drive, **mcp**, **custom_api**…). Org-owned. M:N to data sources via `domain_connection`. Holds `type`, `config`, encrypted `credentials`, `auth_policy`, and `ConnectionTool` children. |
| **Connector** | a `DataSource` whose connection(s) are **tool providers** | i.e. `data_shape="tools"` (`mcp`/`custom_api`/Gmail-style). Private or org-wide, self-serve. Runs through the normal agent. |

So a **connector is just a tools-only, (usually) private, self-serve data source** — not a new
entity and not a new engine.

---

## The proven pattern we build on: OneDrive / GDrive

OneDrive and Google Drive are **already** per-user connectors that work end-to-end, and they are
the template. From the registry (`data_source_registry.py:677-737`):

> **OneDrive:** *"Agent-attachable data source whose catalog is per-user-owned: each user's
> OneDrive is fully independent… Admin save just registers the OAuth app; per-user catalog is
> fetched after each user signs in."* — `data_shape="files"`, `catalog_ownership="per_user"`,
> `ui_form="integration"`, auth variants `service_principal` (`scopes=["system","user"]`) +
> `oauth` delegated (`scopes=["user"]`).

What they prove is **exactly the model we want**, and it already runs through the normal agent:

1. **Admin registers the OAuth app once** → app credentials (`client_id`/`secret`) at the
   connection level.
2. **Each user signs in individually** (delegated `oauth`) → per-user token in
   `UserConnectionCredentials`.
3. **Per-user catalog** (`catalog_ownership="per_user"`) — each user's content is independent,
   fetched after they sign in.

Connectors = the same machinery, extended from `data_shape="files"` to `data_shape="tools"`
(MCP / Gmail / Monday), plus member self-serve and the catalog UX.

---

## What already exists (reuse — do not rebuild)

- **Per-user credentials + delegated OAuth + OBO:** `UserConnectionCredentials`,
  `Connection.auth_policy ∈ {system_only, user_required}`, `allowed_user_auth_modes`, Google /
  Microsoft / generic-MCP OAuth in `connection_oauth_service.py` (+ refresh, OBO auto-provision).
  Fernet-encrypted.
- **Global vs private scoping:** `DataSource.owner_user_id`, `is_public` (private by default),
  `DataSourceMembership`, enforced by `permission_resolver.user_can_access_data_source`.
- **Per-tool enable/policy:** `ConnectionTool` (`is_enabled`, `policy ∈ allow|confirm|deny`) +
  per-DS overlay, surfaced by `ToolsSelector.vue` (`GET/PUT /data_sources/{id}/tools`).
- **Tool execution through the agent:** `execute_mcp` / `search_mcps` (both
  `requires_capability=None`, always in the catalog) resolve + run tools against connections
  attached to `report.data_sources` (`execute_mcp.py:110-131`). The agent already tolerates a
  data source with no schema.
- **Registry polymorphism:** `data_shape="tools"`, `catalog_ownership="none"`, `is_connection`,
  `tool_provider_types()` already distinguish tool providers from analytical sources.

---

## Scoping model: three orthogonal axes

A connector's behaviour is fully described by three **independent** columns — no new primitives:

1. **Shape** — `data_shape` of the connection(s). `tools` → connector (no schema); `tables/files`
   → analytical. Drives UI/config, **not** a separate loop.
2. **Visibility** — who can see/use it: `private` (`owner_user_id=U`, `is_public=False`) ·
   `org-wide` (`is_public=True`) · `granted` (`DataSourceMembership`).
3. **Execution identity** — whose credentials run it: `auth_policy =`
   `system_only` (one shared credential set on the connection) **or** `user_required` (each user's
   own token, delegated OAuth / OBO).

> **Visibility ⟂ identity.** "Globally available **and** per-user identity" is the OneDrive case:
> `is_public=True` (or memberships) + `auth_policy="user_required"` — the **admin registers the
> app once, each user signs in**. (Note: in `user_required`/`self` mode there is **no silent
> service-account fallback** — a user with no token gets the "Connect" prompt;
> `connection_service.py:1005-1011`. `service_account` mode means everyone shares one identity.)

Per-user **catalog** for data connectors uses the existing `catalog_ownership` axis (`shared` with
`UserDataSourceTable/Column` overlays, or `per_user`). For tool connectors the discovered
`ConnectionTool` set is the "catalog."

---

## Auth onboarding tiers (how a connector first gets OAuth)

All three converge on the **same per-user delegated-token storage** OneDrive uses. They differ only
in *how the OAuth client app comes to exist*. A registry/catalog entry declares which tier it uses.

| Tier | Who registers the OAuth app | Admin setup? | Example |
| --- | --- | --- | --- |
| **A — Admin app** | Admin pre-registers a client at the provider; `client_id`/`secret` stored on the connection | yes, once | OneDrive / GDrive today |
| **B — Catalog app** | A BagOfWords-registered app ships with the catalog entry | no | curated "Google"/"Monday" catalog entry |
| **C — True DCR** | **BagOfWords self-registers dynamically** (RFC 7591) against the server's `/register` | **no** | any DCR-capable remote MCP |

**Tier C / DCR is what unlocks true zero-admin self-serve** (paste/pick an MCP → discover → self
register → sign in). DCR removes the *app registration* step, **not** the per-user *sign-in* step.
Detail in the DCR section below.

---

## Auth reuse — connect once per provider

**Decision:** a user connects a provider **once**; that identity then serves **every** connection
to the same provider, **provided the connections require the same scope set**. So when an Agent is
created/used with **Monday** or **BOW**, it **reuses the token the user already has** for that
provider instead of re-prompting.

Today credentials are keyed `(connection_id, user_id, auth_mode)`
(`user_connection_credentials.py:17`), so a token for one connection is invisible to another. To
get "connect once":

- **Key the grant by provider identity, not connection.** Provider identity = OAuth **issuer**
  (from discovery / catalog entry) + canonical **scope set** (+ resource/audience for MCP, see
  caveat). Connections to the same provider with the same scope share one grant.
- **Resolution order** in `resolve_credentials(db, connection, user)`: per-connection token →
  **provider-identity token (same issuer + scope)** → otherwise "Connect" prompt.
- **Same-scope rule (your call).** Connect-once applies only when scopes match; catalog entries
  for a provider declare a *canonical* scope list so all connectors to that provider are
  scope-compatible by construction. A connection requesting different/broader scope is its own
  authorization.
- **Storage:** a per-user provider-identity record (`UserProviderCredential(user_id, issuer,
  scope_hash, encrypted_token, …)`) that connections resolve against — one token, one refresh
  owner.
- **Caveats:** **audience binding** (RFC 8707) — if a provider mints tokens bound to a specific
  resource, reuse only works when both connections target the *same* server/resource;
  **revocation cascade** — disconnecting the provider drops every connection relying on it (surface
  in the disconnect UI).

---

## Per-tool policy (allow / confirm / deny)

- **Standalone connector → the user sets their own per-tool policy.** For a *private* connector
  this is automatic (the DataSource is theirs → the existing per-DS `ConnectionTool` overlay *is*
  per-user). For a *shared/org* connector used standalone, add a per-user overlay
  `UserConnectionToolPreference(user_id, connection_tool_id, is_enabled, policy)`.
- **Connector inside an Agent → the Agent owner sets the policy** via the existing per-DS
  `ConnectionTool` overlay (`ToolsSelector.vue`). Members inherit it.
- **Resolution at run time:** Agent overlay (in-agent) → user pref (standalone) → connection
  default. `policy=confirm` triggers an at-prompt confirmation before the tool runs; `deny` blocks.

Credential *identity* is shared per provider (connect-once); tool *policy* is **not** — it follows
the context above.

---

## Public MCP catalog

A browsable gallery of public MCPs (Monday, Gmail, Linear, Notion, …) for one-click enable, modeled
on the existing `data_source_registry` and the `ui_form="integration"` flow.

- **Catalog = curated registry of connector templates.** Each entry: `key`, `title`, `icon`,
  `description`, `server_url`, `transport`, **onboarding tier (A/B/C)**, canonical `scopes`, and
  (tier B) baked app creds. New `backend/app/schemas/connector_catalog.py` + `GET /connectors/catalog`.
- **"Enable" = instantiate from a template:** create a tools-only `DataSource` + an `mcp`
  `Connection` seeded from the entry, run `refresh-tools` to populate `ConnectionTool`. Scope at
  creation: admin → `is_public=True`; member → `owner_user_id=self`, `is_public=False`.
- **Custom path:** `MCPConnectionForm.vue` remains for anything not in the catalog.

---

## DCR (Dynamic Client Registration) — tier C detail

Outbound DCR is **not implemented today**. (`connection_oauth_service.py` for `conn_type=="mcp"`,
`:136-153`, *requires* pre-stored `authorize_url`/`token_url`/`client_id`/`client_secret`.) Don't
confuse with the **inbound** AS (`oauth_server_service.py`, BagOfWords *as* server for Claude
Desktop/Cursor) — that exists but is a different direction.

Outbound DCR flow:

1. **Discovery** — from `server_url`: `/.well-known/oauth-protected-resource` (RFC 9728) → AS;
   that AS's `/.well-known/oauth-authorization-server` (RFC 8414) → `authorization_endpoint`,
   `token_endpoint`, `registration_endpoint`.
2. **Register (app-level, once per connection)** — POST client metadata to `registration_endpoint`
   (RFC 7591): `redirect_uris=[BOW callback]`, `grant_types`, `token_endpoint_auth_method`,
   `scope`. Store returned `client_id` (+ secret, + `registration_access_token`/`…client_uri` for
   RFC 7592) **encrypted on the Connection**.
3. **Authorize per user** — existing PKCE flow → `UserConnectionCredentials` (and the provider
   identity record for connect-once). DCR changes *who registers the app*, not *who signs in*.
4. **Resource binding** — pass `resource` (RFC 8707) so the token is audience-bound to the server.

New `mcp_dcr_service.py` (or in `connection_oauth_service.py`), invoked lazily on first authorize
when an `mcp` OAuth connection has no `client_id`.

**Guardrail:** DCR only against **catalog entries or an admin host-allowlist** (SSRF / rogue
server); HTTPS + expected issuer; encrypt creds; honour RFC 7592 on connector delete.

---

## What actually changes (gaps over reuse)

Because there's no custom loop and we anchor on OneDrive, the net work is small:

1. **Classify a tools-only data source as a connector** (derive `is_connector` from its
   connections' `data_shape="tools"`; optional thin marker for UX/self-serve gating). **Not an
   execution fork.**
2. **Member self-serve create** of a connector: `create_private_connector` permission
   (gated to tools-only + `owner_user_id=self` + `is_public=False`), and a self-serve create UI
   (catalog gallery + slim `MCPConnectionForm`).
3. **Surface connectors in PromptBoxV2** (`DataSourceSelector` group + "Connect" affordance for
   `user_required`); selected IDs ride the existing `data_sources:[...]` payload.
4. **Connect-once / provider identity** (`UserProviderCredential` + resolution fallback).
5. **Per-user tool policy** for shared connectors used standalone (`UserConnectionToolPreference`).
6. **Catalog** (`connector_catalog` + endpoint + gallery).
7. **DCR** (tier C) for true self-serve.

Everything else — attachment, access control, credential resolution, `execute_mcp`/`search_mcps`,
the agent loop, the planner — is reused unchanged.

---

## Phased plan

- **Phase 0 — connector classification + self-serve create.** Tools-only data source recognized as
  a connector; `create_private_connector`; member create via slim `MCPConnectionForm` (tier A/B
  manual OAuth/bearer). Runs through the existing agent. Delivers stories 3/4/5 minimally.
- **Phase 1 — PromptBoxV2 surfacing.** Connectors group in the selector; "Connect" affordance for
  unauthorized `user_required` connectors. No new report wiring.
- **Phase 2 — connect-once / provider identity.** `UserProviderCredential` + resolution fallback;
  canonical per-provider scopes. Delivers story 6 (no reconnect).
- **Phase 3 — public MCP catalog.** `connector_catalog` + `GET /connectors/catalog` + enable
  endpoint + gallery (admin org-wide / member private).
- **Phase 4 — DCR (tier C).** `.well-known` discovery + RFC 7591 + resource binding + allowlist;
  true zero-admin self-serve.
- **Phase 5 — per-tool policy refinements.** `UserConnectionToolPreference` for shared standalone;
  `confirm` at-prompt step.
- **Phase 6 (optional) — first-class connector tools** to the planner instead of generic
  `execute_mcp` (richer planner UX).

---

## Open decisions

- **Catalog entry ownership:** static curated only, or admins can add org-private catalog entries?
- **DCR target scope:** catalog entries only, or also admin-allowlisted custom URLs?
- **Connector marker:** derive `is_connector` purely from `data_shape`, or store an explicit flag?

---

## Key files

**Backend**
- `backend/app/models/data_source.py` — `owner_user_id`/`is_public`/`publish_status` (reuse); optional connector marker.
- `backend/app/models/connection.py`, `connection_tool.py` — `auth_policy`, tool overlay (reuse).
- `backend/app/schemas/data_source_registry.py` — OneDrive/GDrive entries (`:677-737`) as the template; tool-provider entries (`:878`,`:920`).
- `backend/app/schemas/connector_catalog.py` *(new)* + `GET /connectors/catalog`.
- `backend/app/services/connection_oauth_service.py` — delegated OAuth/OBO (reuse); add `.well-known` discovery + RFC 7591 DCR for `conn_type=="mcp"` (`:136-153`).
- `backend/app/services/connection_service.py` — `resolve_credentials` (`:970-1084`): add provider-identity (connect-once) fallback.
- `backend/app/models/user_provider_credential.py` *(new)* — connect-once identity (issuer+scope).
- `backend/app/models/user_connection_tool_preference.py` *(new)* — per-user standalone tool policy.
- `backend/app/services/data_source_service.py` — `construct_clients` (`:1806`), visibility/usability filters (`:1715`) (reuse).
- `backend/app/ai/tools/implementations/execute_mcp.py` (`:110-131`) + `search_mcps.py` — allow-list (reuse; works once connector is a data source).
- `backend/app/core/permission_resolver.py` — `create_private_connector`.

**Frontend**
- `frontend/components/prompt/PromptBoxV2.vue`, `DataSourceSelector.vue` — surface connectors + "Connect".
- `frontend/components/datasources/ToolsSelector.vue` — per-tool enable/policy (reuse).
- `frontend/components/MCPConnectionForm.vue` — member create + private/shared toggle + catalog gallery.
- `frontend/composables/usePermissions.ts` — gate connector create/use.
- `frontend/pages/settings/integrations/index.vue` — admin global connector management + gallery.

---

## Test cases (the scenarios we discussed)

Given/when/then for the six stories. These drive both automated e2e and manual verification.

1. **Admin Snowflake (service account) → revenue agent for specific users.**
   *Given* a `snowflake` connection `auth_policy="system_only"` on a private Agent with memberships
   to users A,B. *When* A and B query. *Then* both run via the one service account; user C (no
   membership) cannot see the Agent. *Identity:* shared.

2. **Admin Fabric (user-required) for specific users.**
   *Given* a `ms_fabric` Agent, `auth_policy="user_required"`, members A,B. *When* A has signed in,
   B has not. *Then* A queries as themselves; B sees a "Connect" prompt (no silent fallback).

3. **User connects their Gmail (private connector).**
   *Given* member M with `create_private_connector`. *When* M enables Gmail from the catalog and
   signs in. *Then* a tools-only `DataSource` (`owner_user_id=M`, `is_public=False`) exists, M's
   token is stored, Gmail tools are selectable in PromptBoxV2, and the **normal agent** runs them.
   No other user sees it.

4. **User connects their own Snowflake (private agent).**
   *Given* member M. *When* M self-serve-creates a private `snowflake` data source with their own
   creds. *Then* it's `owner_user_id=M`, `is_public=False`, per-user catalog; only M sees/uses it.

5. **User's own Snowflake + admin's Snowflake agent (with sub-agents).**
   *Given* both exist (different names — `uq_data_sources_org_name`). *When* M opens the selector.
   *Then* both appear; M can use either or both in one conversation; identity/catalog resolve per
   data source independently.

6. **Admin agent with Monday (user-required) + user's self-service Monday — connect once.**
   *Given* an org Agent's Monday connection and M's private Monday connector, same issuer + same
   canonical scope. *When* M has signed in to *either*. *Then* M does **not** reconnect for the
   other — the provider-identity token is reused. **But** tool policy does not transfer: in-agent
   uses the agent owner's policy; standalone uses M's own. *(If scopes differ → separate auth.)*

**Additional assertions**
- `policy=confirm` tool → agent asks for confirmation before executing; `deny` → never called.
- Revoking the provider identity drops access for all connections relying on it.

---

## Verification plan (mocks + sandbox feedback loop + manual)

We validate in a fresh cloud sandbox using a `sandbox-feedback-loop-*.md` runbook (same format as
the existing `sandbox-feedback-loop*.md` files), driving **custom connector mocks** end-to-end and
then **manually** confirming in the running app.

**Extend the existing mocks** (`backend/tests/mocks/`):
- `mock_mcp_server.py` — already provides `MockToolProviderClient` with `echo` / `get_records` /
  `search_docs`. Add tools that exercise policy + write semantics (e.g. `send_message` →
  `policy=confirm`) and tabular output.
- `mock_oauth_provider.py` — already mocks Microsoft/Google delegated + OBO. Add:
  - **A "regular" connector mock (tier A/B):** pre-set `client_id`/`secret`, standard
    authorize/token, so we test the OneDrive-style admin-app + per-user sign-in path.
  - **A "true DCR" connector mock (tier C):** advertise `/.well-known/oauth-protected-resource`
    (RFC 9728) and `/.well-known/oauth-authorization-server` (RFC 8414) **including a
    `registration_endpoint`**, accept an RFC 7591 `POST /register` returning a fresh
    `client_id`, then run authorize/token. Assert BagOfWords self-registers (no pre-set client),
    binds `resource` (RFC 8707), and stores the per-user token + provider identity.

**Two custom mock connectors to spin up:**
1. **`mock_regular`** — bearer/admin-app MCP (tier A/B): exercises enable → per-user connect →
   tool call through the agent → per-tool `confirm`.
2. **`mock_dcr`** — DCR MCP (tier C): exercises discovery → dynamic registration → per-user
   connect → connect-once reuse across a second connection to the same mock issuer.

**Runbook (`sandbox-feedback-loop-connectors.md`, to be written alongside implementation):**
- Env setup (Python 3.12, `uv sync --extra dev`, `BOW_DATABASE_URL=sqlite:///db/app.db`) — mirror
  the existing runbooks.
- Seed: org, admin, member; the two mock connectors.
- Scripted e2e per test case above (extend `tests/e2e/test_oauth_mcp.py`,
  `test_mcp_tools.py`, `test_custom_api_tools.py`).
- **Manual pass:** run the app, connect each mock as admin and as member, confirm the selector,
  the "Connect" prompts, the confirm-policy step, connect-once (no second prompt), and a tool call
  end-to-end — and make sure it works before merging.
