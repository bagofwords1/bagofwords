# Integrations & Per-User Tool Connections — Design

## Goal

Let users connect their **own** tools (Gmail, Jira, Notion, Drive…) and use them in
any conversation via `@mention`, **without** having to build an Agent
(DataSource). Keep the existing **Agents** surface and data-source **schema sync**
exactly as they are.

This is a **two-view rendering of one underlying `Connection` model**, not a new
object graph:

- **Agents page** — curated reasoning workspaces (`DataSource`/Domain): connections
  + instructions + skills, with schema sync. Admin-built, shared. *Unchanged.*
- **Integrations page** — a per-user catalog of tool connections, with self-serve
  connect + per-tool enable/disable.

A `Connection`'s *data-ness* and *tool-ness* are **capabilities, not categories**.
Postgres = `QUERY` (schema sync, Agents). Jira = tools (Integrations). Google Drive
= both (`LIST_FILES/READ_FILE` tools **and** `is_document_based` catalog) — so it
legitimately appears in both views. We model capability and render in the right
place; we never duplicate the connection.

## Concept model (axes)

These are **independent** axes already on the registry entry / connection — do not
conflate them:

| Axis | Field | Values | Meaning |
|---|---|---|---|
| Auth location | `Connection.auth_policy` | `system_only` \| `user_required` | Where credentials live |
| User auth modes | `Connection.allowed_user_auth_modes` | e.g. `["oauth"]` | How a user self-auths |
| Catalog ownership | registry `catalog_ownership` | `shared` \| `per_user` \| `none` | Where the catalog comes from |
| Surface / audience | registry `ui_form` (+ `connect_audience`) | `data_source` \| `integration` \| `mcp` \| `custom_api` | Which UI it shows in / who may connect |

**"Admin-first" ≠ "admin-only."** *Admin-first* = who configures the OAuth app
(admin). *Admin-only* = who may connect/use it. Gmail is admin-first **and**
user-self-serve; Snowflake/Fabric/QVD are admin-first **and** admin-only.

Decision rule for a new connector:

1. **Whose resource?** Shared org → can be `system`. Per-user / "act as me" → `user`.
2. **How does it auth?** Service account / key / app-only → system credential.
   Username/pass/keypair the *user* owns → user credential, **form**. OAuth on behalf
   of a user → needs an OAuth **app** → **admin-then-user**.
3. **Self-serve?** `ui_form="data_source"` (Snowflake/Fabric/QVD) is **never** in the
   user Integrations catalog, even if it technically supports per-user auth.
   `ui_form ∈ {integration, mcp, custom_api}` is eligible, gated by org enablement.

The single signal for "admin-first then user-required": **its user-scoped auth
variant is OAuth AND the provider needs a pre-registered app (no DCR).** DCR
collapses the admin step; credential-based `user_required` (Snowflake) never had an
app step.

## The core gap (what makes this non-trivial)

Today tool invocation resolves connections via `report.data_sources` (the agent):
`_file_tool_common.resolve_file_client` builds its allow-list from
`runtime_ctx["report"].data_sources`. To use integrations **without an agent**, the
runtime must resolve & inject a connection's tools by **current user** (the
connections the user has `UserConnectionCredentials` for), independent of any
DataSource. This is the central new mechanism; everything else is reuse.

## Phases

### Phase 0 — Runtime: per-user tool resolution (unblocks everything)
- Relax `_file_tool_common.resolve_file_client` to also accept connections the
  current user has credentials/tools for (not only `report.data_sources`).
- Inject a mentioned connection's tools into the turn's toolset with no DataSource
  attached.

### Phase 1 — `@integration` vertical slice (one provider)
- Frontend: user-scoped **integrations** category in the `@` dropdown
  (`MentionInput.vue`), not gated on a selected agent; emit an `INTEGRATIONS` group
  in the submit payload (`PromptBoxV2.vue`).
- Backend: add `MentionType.CONNECTION`; add the missing `INTEGRATIONS` branch in
  `mention_service.create_completion_mentions` (today it drops tool mentions);
  handle it in `MentionContextBuilder` / `mentions_section.py`.
- New **user-scoped** query in `mention_service` (currently agent-scoped only):
  integrations the user has connected + enabled.
- A first connector with typed tools + registry entry + creds/config schema.

### Phase 2 — Integrations page + connect + toggles
- Catalog page: provider cards, Connect, Connected / Needs-configuration states,
  search. Shows `ui_form ∈ {integration, mcp, custom_api}` only.
- Detail modal: **Actions** list = `ConnectionTool` rows; per-tool enable/disable =
  `UserConnectionTool`; "Admin view" for admin config.
- Empty-state deep-link ("add connection to proceed" → connect → resume).

### Phase 3 — Auth scaling
- **Provider-app abstraction**: admin configures Google/Microsoft/Slack app **once**;
  per-service integrations share it + declare own scopes. Refactor
  `connection_oauth_service.get_oauth_params` off per-connection client_id/secret.
- **Scope union / incremental auth** so one Google connect covers Gmail+Drive+Calendar.
- **MCP + DCR path**: extend the `mcp` connection type — paste server URL →
  self-register (RFC 7591) → user connects.

### Phase 4 — Files (attach from Drive/OneDrive)
- Reuse `list_files`/`search_files` → `read_file` → `attach_drive_file_to_session`.
- Net-new: **attach-by-link** — parse a Drive/SharePoint URL → file id.

### Phase 5 — Fan out connectors
Grouped by shared auth: Google (Calendar, Sheets, Slides, Analytics), Microsoft
(Outlook Mail/Calendar, OneNote), MCP+DCR (Jira, Confluence, Notion, Linear), Slack.

## Out of scope (now)
- Triggers / event-driven runs (provider webhooks live in `webhook_adapters/` +
  `scheduled_prompt_service`). MCP does **not** provide triggers — those come from
  each provider's native webhook/subscription delivered to our receiver.
- Multi-step workflow engine.
- Inline integration (future).

## Reuse vs net-new
Reuse: `Connection`, `UserConnectionCredentials`, `ConnectionTool` /
`UserConnectionTool`, `connection_oauth`, the mention pipeline, file read/attach.
Net-new: Phase 0 per-user injection, the two scoping queries, the Integrations UI,
the provider-app / DCR refactor.
