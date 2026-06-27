# Connectors without Agents

**Status:** Design / investigation (no implementation)
**Branch:** `claude/connectors-without-agents-tnoa6p`

## Goal

Let an **admin** enable a connector (e.g. Monday, Gmail) globally for the org, and let a
**regular user** connect their *own private* connector with their own credentials, pick
the tools they want exposed, and use them directly in a conversation — **without having to
build a full analytical Agent (fka "data source")**.

This is the pattern every mainstream AI platform ships: "Connect Gmail → choose tools →
chat." Today this product can only reach connector tools by wrapping an MCP / Custom-API
connection inside a data source ("Agent"), attaching that Agent to a report, and running the
full heavyweight analysis loop. We want connector tools to be a first-class, lightweight
thing.

---

## Terminology (important — the rename)

The product recently renamed **"data source" → "Agent"** in the UI
(`frontend/pages/old_agents/`, commit `feat(roles): … "Agent" rename`). So:

| Concept | Code entity | What it is |
| --- | --- | --- |
| **Agent** | `DataSource` (`backend/app/models/data_source.py`) | A configured analytical entity: a *container* of connections + a schema/file catalog + instructions + memberships + publish status + per-user creds. **No `type` column** — it is type-agnostic; the type lives on each `Connection`. |
| **Connection** | `Connection` (`backend/app/models/connection.py`) | The actual wire to a system (postgres, snowflake, google_drive, **mcp**, **custom_api**…). Org-owned. Attached to data sources M:N via `domain_connection`. |
| **Tool provider** | registry entries with `is_connection=False` | `mcp` and `custom_api`. `data_shape="tools"`, `catalog_ownership="none"`. Already a distinct concept in `backend/app/schemas/data_source_registry.py` (`tool_provider_types()`, `list_registry(include_tool_providers=…)`). |

So "**connectors without agents**" = **tool-provider connections that are usable on their own,
not buried inside an analytical Agent, and runnable without the full agent loop.**

---

## How it works today (and why a connector needs an Agent)

1. A **report** has attached data sources via `report_data_source_association` (M:N).
2. `DataSourceService.construct_clients(db, ds, user)` builds a client per connection,
   enforcing per-user access (403 if no access) and resolving per-user credentials by
   `auth_policy` (`backend/app/services/data_source_service.py:1806`).
3. `AgentV2.__init__` computes `available_capabilities` by walking
   `report.data_sources → connections → client_class.capabilities`
   (`backend/app/ai/agent_v2.py:315-343`).
4. The planner is handed a tool catalog filtered by capability / mode / platform
   (`ToolRegistry.get_catalog_for_plan_type`, `backend/app/ai/registry.py:100`).
5. The main loop (`AgentV2.main_execution`, `agent_v2.py:1825`) primes schema context,
   scores with the Judge, runs the planner, dispatches tools via `ToolRunner`, and runs the
   post-analysis **knowledge harness** — all the heavyweight machinery.
6. Connector tools specifically:
   - `search_mcps` discovers tools from connections **linked to the report's data sources**.
   - `execute_mcp` validates the `connection_id` is in the allow-list built from
     `report.data_sources → connections` (`execute_mcp.py:110-131`).

**The hard coupling:** every step keys off `report.data_sources`. To use a connector you must
(a) create a Connection, (b) attach it to a DataSource/Agent, (c) attach that Agent to the
report, (d) run the full agent. There is no way to attach a connector directly, and no
lightweight execution path.

---

## What already exists that we can reuse (a lot)

The credential/identity/permission substrate for "private vs global connectors" is **already
built** — it was built for per-user database auth and OAuth data sources:

- **Per-user credentials + OAuth:** `UserConnectionCredentials`
  (`backend/app/models/user_connection_credentials.py`), `Connection.auth_policy ∈
  {system_only, user_required}`, `allowed_user_auth_modes`, and full OAuth flows for Google /
  Microsoft / generic MCP `oauth_app` in `connection_oauth_service.py` +
  `routes/connection_oauth.py`. Encrypted with Fernet (`bow_config.encryption_key`).
- **Per-tool enable/policy:** `ConnectionTool` rows (discovered from the MCP server) with a
  per-data-source overlay (`is_enabled`, `policy ∈ allow|confirm|deny`); surfaced by
  `frontend/components/datasources/ToolsSelector.vue` via `GET/PUT /data_sources/{id}/tools`.
- **Global vs private scoping on the Agent already:** `DataSource.owner_user_id`,
  `DataSource.is_public` (private by default!), `DataSourceMembership` (user/group grants),
  `publish_status`. Access enforced by `permission_resolver.user_can_access_data_source`.
- **Tool-provider concept in the registry:** `is_connection=False`, `data_shape="tools"`.
- **Decoupled execution primitives:** `ToolRegistry` and `ToolRunner`
  (`backend/app/ai/runner/tool_runner.py`) have **no dependency on AgentV2 / ContextHub /
  Planner** — given `(tool, args, runtime_ctx, emit)` they run a tool standalone.
- **`execute_mcp` / `search_mcps` are `requires_capability=None`** — always in the catalog,
  so they survive even when there are no analytical capabilities.
- **The agent already tolerates "no analytical data source":** `report` is optional and
  `data_sources` may be empty; tools that need schema degrade gracefully.

**Conclusion:** we are *not* building a new credential, OAuth, permission, or per-tool system.
We are mostly **decoupling** the connector from the Agent container and adding a **lightweight
run path** + the **product UX** (enable global / connect private / pick tools / select in the
prompt box).

---

## The gaps (what actually has to change)

1. **A connector cannot stand alone.** Tool-provider connections must be attached to an
   analytical DataSource; there is no "connector" entity the UI/agent can branch on.
2. **No member-facing "connect my own connector" flow.** Creating a data source requires the
   `create_data_source` permission and the create UI is the heavyweight Agent builder.
3. **PromptBoxV2 only selects analytical data sources** (`DataSourceSelector`), not connectors,
   and has no per-tool picker at prompt time.
4. **No lightweight execution path** — even a connector-only conversation runs the full
   schema-priming / Judge / knowledge-harness loop with an analysis-oriented system prompt.
5. **Connector tools reach the planner only via the generic `execute_mcp` wrapper.** For an
   "AI platform" feel we want the *specific* connector tools (each `ConnectionTool`) presented
   to the planner as first-class tools.

---

## Proposed design

### Core decision: a Connector is a **tools-only DataSource**, plus a lightweight run path

Reuse the `DataSource` row as the connector container (so attachment, per-user creds, access
control, `execute_mcp` allow-list, and `ConnectionTool` overlays all keep working unchanged),
but mark it as tools-only and skip the analytical machinery.

Add a discriminator so UI + agent can branch:

```python
# backend/app/models/data_source.py
kind = Column(String, nullable=False, default="agent")   # "agent" | "connector"
```

A `kind="connector"` data source:
- has only tool-provider connections (`mcp` / `custom_api`),
- has **no** schema catalog, instructions, Judge scoring, or knowledge harness,
- can be **global** (`is_public=True`, admin-created) or **private**
  (`owner_user_id=<user>`, `is_public=False`) — *exact same fields already used for Agents*,
- exposes its `ConnectionTool`s directly to the planner.

> Why reuse `DataSource` rather than invent a parallel `Connector` table + a new
> report↔connector attachment + new access checks: nearly every relevant code path
> (`construct_clients`, `available_capabilities`, `execute_mcp` allow-list,
> `report_data_source_association`, `user_can_access_data_source`, `ToolsSelector`) is keyed on
> `report.data_sources`. A discriminator rides all of that for free; a parallel entity would
> mean re-plumbing each of those. The alternative is recorded below.

### 1. Data model

- `DataSource.kind` discriminator (above). Default `"agent"` keeps every existing row a fully
  analytical agent — zero behavioural change for current data.
- Derive a helper `DataSource.is_connector` (all connections are tool providers) for safety,
  but store `kind` explicitly so an empty connector still classifies correctly.
- No new tables. `report_data_source_association`, `Connection`, `ConnectionTool`,
  `UserConnectionCredentials`, `DataSourceMembership` are all reused as-is.

### 2. Access & ownership (global vs private)

Reuse the existing model verbatim:

| Scenario | Fields | Created by |
| --- | --- | --- |
| **Global connector** (admin enables Monday for everyone) | `kind="connector"`, `is_public=True`, `auth_policy="system_only"` (org creds) **or** `"user_required"` (each user signs in) | admin (`create_data_source` / `manage_connections`) |
| **Private connector** (user connects their own Gmail) | `kind="connector"`, `owner_user_id=<user>`, `is_public=False`, `auth_policy="user_required"` | the user |

Add a lighter permission so members can create **private connectors only** without the full
`create_data_source` governance permission, e.g. `create_private_connector` (gate: result must
be `kind="connector"` + `owner_user_id == current_user` + `is_public=False`). Visibility +
usability filtering already handled by `filter_user_visible_data_sources` /
`filter_user_usable_data_sources` in `data_source_service.py`.

### 3. Credentials / OAuth (incl. OBO for org connectors)

No new credential code. A connector's connection uses `auth_policy="user_required"` → the
existing per-user OAuth flow (`/connections/{id}/oauth/authorize` → callback →
`UserConnectionCredentials`) handles "Connect your Gmail." Global system connectors use
`auth_policy="system_only"` with org creds on the `Connection`.
`construct_clients` / `resolve_credentials_for_connection` already pick the right path per user.

**Org connector that requires per-user identity (OBO).** A common case (confirmed): an
**org-level Agent requires Monday tools**, but each member must act as *themselves* on Monday.
This is exactly today's **On-Behalf-Of** path: a global/org connector with
`auth_policy="user_required"`, and each user's token resolved per-run via
`connection_oauth_service` (`maybe_refresh_oauth_credentials`, OBO token exchange in
`auto_provision_connection_credentials`). `resolve_credentials(db, connection, current_user)`
already: returns the user's token when present, refreshes if near expiry, falls back to the
admin "service_account vs self" identity preference, and 403s a user with no token when the
policy demands self-identity. So **the same connector is shared org-wide but executes with each
member's own Monday identity** — no new work beyond surfacing the "Connect" prompt when a
user hits an org connector they haven't authorized yet (the selector already has this state).

### 4. PromptBoxV2 + selectors (frontend)

- Surface connectors in the prompt box. Two viable UX:
  - **(preferred)** extend `DataSourceSelector` to list connectors in a separate "Connectors /
    Tools" group (it already filters by `usePermissions` and distinguishes connected vs
    needs-credentials states, incl. a "Connect" affordance for `user_required`); or
  - a dedicated `ConnectorSelector` rendered next to it.
- Selected connector IDs flow into the same `data_sources: [...ids]` array PromptBoxV2 already
  sends on report create (`PromptBoxV2.vue:1388`) and persists via `PUT /reports/{id}`
  (`DataSourceSelector.vue:479`). No new report wiring.
- Optional per-prompt **tool picker**: reuse `ToolsSelector.vue`'s `ConnectionTool` overlay
  (enable/policy) so a user can scope which of a connector's tools are active.
- A member-facing **"Add connector"** entry point: a slimmed `MCPConnectionForm.vue`
  (already supports `auth_type: none|bearer|api_key|oauth_app` and per-user OAuth) with a
  "Private (only me)" vs "Shared (org-wide, admin)" toggle.

### 5. Planner

The planner stays — letting the LLM pick *which* connector tool and *with what arguments* is
exactly the "AI platform" experience; we are not building a deterministic tool-runner UI.
Changes:

- **Expose connector tools as first-class tools** to the planner instead of the single
  `execute_mcp` wrapper. Build `ToolDescriptor`s from each enabled `ConnectionTool`
  (name, description, `input_schema`) and add them to the catalog handed to PlannerV3
  (`tool_catalog` in `agent_v2.py:336-361`). At dispatch, route those tool names through the
  existing `execute_mcp` implementation (resolve connection + tool, run). This keeps the
  battle-tested execution/validation path while giving the model precise tools.
  (If simpler first: keep `execute_mcp` + `search_mcps` and just make sure the allow-list
  includes connector data sources — see §7.)
- Add a lighter **system prompt** variant (e.g. a new `mode="connector"` or a prompt branch in
  `prompt_builder_v3.py`) that drops the analyst framing and schema sections when the
  conversation has no analytical data source.

### 6. agent_v2 — lightweight loop

Detect "connector-only" turns: `report.data_sources` all `kind="connector"` (or no analytical
source). When so, skip:
- schema priming / top-k schema / `schemas_combined`,
- Judge scoring (`_run_early_scoring_background` / `_run_late_scoring_background`),
- knowledge harness (`_run_knowledge_harness`),
- `available_steps` / artifact-continuation context.

Keep: planner loop, `ToolRunner`, observation feedback, block/SSE streaming. This is mostly
**guarding existing calls** behind a `self._connector_only` flag in `main_execution`, not new
infrastructure — `ToolRegistry` + `ToolRunner` are already standalone.

### 7. Public MCP catalog (marketplace) — *new component*

We want a **browsable catalog of public MCPs** (Monday, Gmail, Linear, Notion, Slack, …) that
an admin (org-wide) or a member (private) can enable in a couple of clicks, rather than typing
a server URL into a raw form. This mirrors the existing **data source registry** pattern.

- **Catalog source = a curated registry of connector templates.** Each entry is a
  pre-configured connector: `key`, display `title`, `icon`, `description`, MCP `server_url`,
  `transport`, `auth_type` (`oauth_app` / `bearer` / `api_key` / `none`), default `scopes`,
  and (for OAuth) `authorize_url` / `token_url` / `audience`. Model it like
  `data_source_registry.REGISTRY` — a static dict the API serves (e.g.
  `backend/app/schemas/connector_catalog.py` + `GET /connectors/catalog`). Versionable,
  ships with the app, and (later) augmentable by org-defined entries.
- **"Enable" = instantiate a connector from a template.** Picking an entry creates a
  `kind="connector"` DataSource + an `mcp` `Connection` seeded from the template's
  `server_url`/`transport`/`auth_type`, then runs `refresh-tools` to populate `ConnectionTool`
  rows. Scope at creation: admin → `is_public=True`; member → `owner_user_id=self`,
  `is_public=False`. OAuth entries then send the user through the existing connect flow.
- **Auth per entry.** OAuth catalog entries default to `auth_policy="user_required"` (each
  user signs in — incl. the OBO org-connector case in §3). API-key/bearer entries can be
  `system_only` (admin pastes one org key) or `user_required` (each user pastes their own),
  chosen at enable time.
- **Frontend.** A "Connectors" gallery (cards with icons) — admin version under
  `settings/integrations`, member version reachable from the prompt box "Add connector".
  Reuse `MCPConnectionForm.vue` as the *advanced/custom* path for anything not in the catalog.
- **Tool granularity follows from the catalog.** Once an MCP is enabled, its tools are the
  discovered `ConnectionTool` rows. Start by routing them through the shipped
  `execute_mcp` + `search_mcps` path (cheap, validated); promote to first-class per-tool
  `ToolDescriptor`s later (§5) for a sharper planner UX. The catalog is the *discovery/enable*
  layer; it is orthogonal to how tools are presented to the planner.

### 8. execute_mcp / search_mcps allow-list

Because connectors are data sources, `report.data_sources → connections` already includes
them, so `execute_mcp`'s allow-list (`execute_mcp.py:110-131`) and `search_mcps`' discovery
keep working with **no change**. (This is the single biggest win of reusing `DataSource`.)

---

## Alternative considered (and rejected): a separate `Connector` entity

Model connectors as their own table with a new `report_connector_association`, new access
checks, and a new per-prompt selection path.

- ➖ Re-implements attachment, visibility/usability filtering, per-user credentials wiring, the
  `execute_mcp` allow-list, and the `ConnectionTool` overlay — each of which is currently
  `DataSource`-keyed.
- ➖ Two parallel "thing you attach to a conversation" systems to keep in sync (RBAC, channels,
  serializers, monitoring).
- ➕ Cleaner conceptual separation; `DataSource` stops being overloaded.

Rejected for v1 because the reuse path ships the same UX with a fraction of the surface area
and risk. The `kind` discriminator leaves the door open to split later if the overload hurts.

---

## Resolved decisions

1. **Member self-serve: YES.** Members may create their **own private** connectors
   (`create_private_connector` permission; gated to `kind="connector"` +
   `owner_user_id == self` + `is_public=False`). Admin retains org-wide (`is_public=True`).
2. **Connectors attach to Agents too (OBO).** An org-level Agent can *require* connector tools
   (e.g. Monday) while each member executes with their own identity via the existing OBO /
   `user_required` flow (§3). Connectors are therefore both *standalone* and *co-attachable to
   Agents* — the reuse-`DataSource` design gives this for free.
3. **Tool discovery: a catalog of public MCPs** (§7). Curated registry of connector templates;
   one-click enable (org or private). Tools presented to the planner via the shipped
   `execute_mcp`/`search_mcps` path first, first-class per-tool exposure later.
4. **Mixed conversations: YES.** A turn may use an analytical Agent *and* connectors together;
   the lightweight loop (§6) only triggers when there is *no* analytical source.

## Still to confirm

- **Confirmation policy:** honour `ConnectionTool.policy = confirm` for write-ish connector
  actions (Gmail send, Monday create) with an at-prompt confirmation step?
- **Catalog ownership of entries:** ship a static curated catalog only, or also let admins add
  org-private catalog entries (custom MCPs promoted into the gallery)?

---

## Phased plan

- **Phase 0 — plumbing (no UX):** add `DataSource.kind` (+ migration, default `"agent"`);
  classify tool-provider-only sources as connectors; confirm `execute_mcp`/`search_mcps` work
  for a connector attached directly to a report.
- **Phase 1 — lightweight run path:** `_connector_only` detection in `agent_v2.main_execution`;
  guard schema/Judge/knowledge-harness; add the connector system-prompt branch in
  `prompt_builder_v3`.
- **Phase 2 — public MCP catalog (backend):** `connector_catalog` registry + `GET
  /connectors/catalog` + "enable from template" endpoint (creates connector DataSource +
  `mcp` Connection + refresh-tools).
- **Phase 3 — admin global connectors (UX):** connectors gallery in `settings/integrations`;
  enable org-wide (`is_public=True`); system or OBO/`user_required` auth.
- **Phase 4 — private connectors + member self-serve:** `create_private_connector` permission;
  member "Add connector" from the prompt box; "Connect your Gmail" via existing per-user OAuth;
  private/shared toggle in `MCPConnectionForm` (custom path).
- **Phase 5 — PromptBoxV2 selection + tool picker:** connectors group in the selector;
  per-user "Connect" affordance for unauthorized org connectors (OBO); optional per-prompt
  `ConnectionTool` enable/policy.
- **Phase 6 (optional) — first-class connector tools** to the planner instead of `execute_mcp`.

---

## Key files

**Backend**
- `backend/app/models/data_source.py` — add `kind`; `owner_user_id`, `is_public`, `publish_status` already present.
- `backend/app/models/connection.py` — `auth_policy`, `allowed_user_auth_modes`, `ConnectionTool` (reuse).
- `backend/app/schemas/data_source_registry.py` — `mcp`/`custom_api` tool-provider entries (`:878`, `:920`), `tool_provider_types()` (`:1030`); template for the new `connector_catalog`.
- `backend/app/schemas/connector_catalog.py` *(new)* — curated public-MCP catalog; served by `GET /connectors/catalog`.
- `backend/app/services/connection_oauth_service.py` — OBO / per-user token resolve + refresh (reuse for org connectors).
- `backend/app/services/data_source_service.py` — `construct_clients` (`:1806`), visibility/usability filters (`:1715`).
- `backend/app/ai/agent_v2.py` — catalog build + `available_capabilities` (`:315-361`); `main_execution` lightweight guards (`:1825`).
- `backend/app/ai/agents/planner/planner_v3.py` + `prompt_builder_v3.py` — connector prompt branch / first-class tools.
- `backend/app/ai/tools/implementations/execute_mcp.py` (`:110-131`) + `search_mcps.py` — allow-list (works once connector is a data source).
- `backend/app/core/permission_resolver.py` — add `create_private_connector`.

**Frontend**
- `frontend/components/prompt/PromptBoxV2.vue`, `DataSourceSelector.vue` — surface connectors + selection payload.
- `frontend/components/datasources/ToolsSelector.vue` — per-tool enable/policy (reuse).
- `frontend/components/MCPConnectionForm.vue` — member create + private/shared toggle.
- `frontend/composables/usePermissions.ts` — gate connector create/use.
- `frontend/pages/settings/integrations/index.vue` — admin global connector management.
