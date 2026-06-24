# Integrations & Per-User Tool Connections — Design

## Goal

Let users connect their **own** tools (Gmail, Jira, Notion, personal Drive…) and
query them directly — via a chat or an `@mention` — **without** building an Agent.
Keep the existing **Agents** surface and admin **data-source schema sync** exactly
as they are.

Everything is **one `Connection` model underneath**, rendered through three
surfaces split by *who owns it* and *how organized it is*. We never duplicate a
connection; we model capability + audience and render in the right place.

---

## 1. Information architecture — three surfaces

| Surface | What it is | Scope | Ceremony | Lives in |
|---|---|---|---|---|
| **Agents** (`/agents`) | Curated units: connections + tables + tools + instructions + skills + evals | Org / shared | High — reusable, governed | existing master-detail |
| **Integrations** (`/integrations`) | Single personal tools you connect and query directly | **User-personal** | Low — connect & go | new page |
| **Data sources / Connections** | Admin SQL servers (Postgres, Snowflake, BigQuery…), schema-synced | Org / admin | Admin-managed | Agents → Connections panel |

The dividing line is **personal tool vs org data**:

- Admin's many **SQL servers are NOT integrations** — they're
  `ui_form="data_source"`, admin-owned, schema-synced, and live in the
  **Connections** area (the bottom-left "SQLite Chinook / +" panel on the Agents
  page). They feed *agents*. They must **never** appear in the personal
  Integrations catalog, even if they technically support per-user auth.
- **Integrations** are the opposite end: personal, self-serve, no schema sync,
  query-on-demand.

### How the three relate (the graph)

- An **integration** = a single connected tool → query directly or `@mention`.
- An **agent** = a curated bundle that *can include* connections, integration
  tools, tables, and instructions.
- **Promotion path:** an admin can attach a tool/connection into an agent (the
  existing `domain_connection` link). Integrations are the lightweight entry;
  agents are where things get organized and governed. No duplication — same
  `Connection`, two framings.

---

## 2. Concept model (axes on the registry / connection)

These are **independent** — do not conflate them:

| Axis | Field | Values | Meaning |
|---|---|---|---|
| Auth location | `Connection.auth_policy` | `system_only` \| `user_required` | Where credentials live |
| User auth modes | `Connection.allowed_user_auth_modes` | e.g. `["oauth"]` | How a user self-auths |
| Catalog ownership | registry `catalog_ownership` | `shared` \| `per_user` \| `none` | Where the catalog comes from |
| Surface / audience | registry `connect_audience` (+ `ui_form`) | `admin` \| `user` \| `both` | Which surface / who may connect |

**"Admin-first" ≠ "admin-only."** *Admin-first* = who configures the OAuth app.
*Admin-only* = who may connect. Gmail is admin-first **and** user-self-serve;
Snowflake/Fabric/QVD are admin-first **and** admin-only.

Decision rule for a new connector:

1. **Whose resource?** Shared org → can be `system`. Per-user / "act as me" → `user`.
2. **How does it auth?** Service account / key / app-only → system credential.
   Username/pass/keypair the *user* owns → user credential, **form**. OAuth on
   behalf of a user → needs an OAuth **app** → **admin-then-user**.
3. **Self-serve?** `connect_audience="admin"` (data_source) → never in the user
   Integrations catalog. `user`/`both` → eligible, gated by org enablement.

Single signal for "admin-first then user-required": its user-scoped auth variant
is OAuth **and** the provider needs a pre-registered app (no DCR). DCR collapses
the admin step; credential-based `user_required` (Snowflake) never had an app step.

---

## 3. The `/integrations` page design

**Mirror the `/agents` master-detail shell** (not a bare grid) for consistency.

**Left panel — your integrations + catalog**
- **Connected** (top): the user's personal connections — `Gmail •`, `Jira •`,
  personal `Drive •` — each with a live status dot
  (connected / token-expired / needs-reauth). Explicitly "yours."
- **Available** (below): the self-serve catalog (Outlook, Notion, Calendar…) with
  Connect.
- Search + filter (All / Connected / Needs auth) at top.

**Right panel — selected integration detail**
- Header: icon, title, **personal badge** ("Connected as you · yochay"),
  Connect / Disconnect.
- **Actions** — tools list with per-user enable/disable toggles
  (`UserConnectionTool`). (Already built in the modal; promote into the detail
  pane.)
- **Auth** — your sign-in / token status / reconnect.
- **Usage** — recent calls + which reports used it (mirrors the agent header's
  "Activity / tasks").
- **Admin view** (admins only) — which actions are exposed org-wide + shared OAuth
  app config.

**The crucial CTA — query directly.** Each detail gets a **"New report"** button
that opens a chat already scoped to `@<integration>`. That, plus the inline
`@integration` mention from any prompt box, is what makes integrations usable
*without* an agent — the whole point.

### Personal-only semantics
Make "this is yours" unmistakable: integrations show **"Connected as you"**, use
*your* token at query time, and another user's `/integrations` shows *their*
connections, not yours. This is the visible contrast with agents (shared org
units everyone sees).

### Two doors for "Add"
- `/integrations` **Add** → self-serve (personal) connectors only.
- Admin **Connect data source** (SQL servers) stays in the Agents/Connections
  admin flow. Never show Snowflake in the personal Integrations catalog.

---

## 4. The core runtime gap (what makes this non-trivial)

Today tool invocation resolves connections via `report.data_sources` (the agent):
`_file_tool_common.resolve_file_client` builds its allow-list from
`runtime_ctx["report"].data_sources`. To query integrations **without an agent**,
the runtime must resolve & inject a connection's tools by **current user** (the
connections the user holds `UserConnectionCredentials` for), independent of any
DataSource. This is the central new mechanism; everything else is reuse.

---

## 5. Phases

### Phase 0 — Runtime: per-user tool resolution (unblocks everything)
- Relax `resolve_file_client` (and the tool registry) to also accept connections
  the current user has credentials/tools for — not only `report.data_sources`.
- Inject a mentioned connection's tools into the turn's toolset with no DataSource
  attached, capability-gated, resolved by current user.
- **Status: not started** (the highest-value next step).

### Phase 1 — `@integration` mention + catalog backend
- `connect_audience` surface axis; `is_integration`; `list_integration_entries()`.
- `MentionType.CONNECTION` + `INTEGRATIONS` persist branch; user-scoped
  `_get_user_integrations()` (connections the user connected, no agent) +
  `IntegrationMention` schema.
- Gmail connector (`GmailClient`, 4 typed tools) + registry entry + OAuth block.
- `/api/integrations` (catalog + per-user connected state + per-tool toggle).
- **Status: DONE & validated** (API + SQLite + unit tests). PromptBoxV2 emit of the
  `INTEGRATIONS` group is **still open**.

### Phase 2 — Integrations page
- Catalog/connect/toggle UI. **Status: grid + detail modal DONE & screenshot-
  verified.** Next: refactor to the **master-detail layout** in §3 + personal
  badges + "New report" CTA + Usage/Auth panes.

### Phase 3 — Auth scaling
- **Provider-app abstraction**: admin configures Google/Microsoft/Slack app
  **once**; per-service integrations share it + declare own scopes. Refactor
  `connection_oauth_service.get_oauth_params` off per-connection client_id/secret.
- **Scope union / incremental auth** so one Google connect covers
  Gmail+Drive+Calendar.
- **MCP + DCR path**: extend the `mcp` connection type — paste server URL →
  self-register (RFC 7591) → user connects.
- **Status: not started.**

### Phase 4 — Files (attach from Drive/OneDrive)
- Reuse `list_files`/`search_files` → `read_file` → `attach_drive_file_to_session`.
- Net-new: **attach-by-link** — parse a Drive/SharePoint URL → file id.
- **Status: not started.**

### Phase 5 — Fan out connectors
Grouped by shared auth: Google (Calendar, Sheets, Slides, Analytics), Microsoft
(Outlook Mail/Calendar, OneNote), MCP+DCR (Jira, Confluence, Notion, Linear), Slack.
- **Status: Gmail only (as the Phase 1 reference connector).**

---

## 6. Out of scope (now)
- **Triggers / event-driven runs.** Provider webhooks already live in
  `webhook_adapters/` (github, jira, generic) + `scheduled_prompt_service` +
  `email_poller_service`. MCP does **not** provide triggers — those come from each
  provider's native webhook/subscription delivered to our receiver. The hard new
  problem when we do this: **principal-based auth for unattended runs** (whose
  token runs the trigger when nobody's present).
- Multi-step workflow engine.
- Inline integration (future).

---

## 7. Reuse vs net-new
Reuse: `Connection`, `UserConnectionCredentials`, `ConnectionTool` /
`UserConnectionTool`, `connection_oauth`, the mention pipeline, file read/attach.
Net-new: Phase 0 per-user injection, the user-scoped scoping queries, the
Integrations UI, the provider-app / DCR refactor.

---

## 8. Open questions / decisions to lock

1. **Are integrations strictly personal, or can a workspace share one?**
   (e.g. a Jira service account everyone uses.) Stated model = **personal only**;
   anything shared becomes an **agent**. If we ever want shared tools, add a
   "Shared by workspace" group to `/integrations` — but default is personal-only.
   → *Need confirmation this stays personal-only.*

2. **Querying an integration with no agent — what report context?** A `@Gmail`
   chat with no DataSource: does it create a lightweight "scratch" report, or a
   first-class report that can later be saved/attached to an agent? → *Recommend
   first-class report; integration is just the tool source.*

3. **Per-user vs org tool enablement precedence.** Admin disables a tool org-wide
   (`ConnectionTool.is_enabled=false`) vs user enables it
   (`UserConnectionTool`). Admin-off should **always** win (hard gate). Confirm
   that ordering.

4. **`connect_audience="both"` connectors** (e.g. Google Drive — admin can wire it
   into an agent *and* a user can connect personally). The same registry entry
   appears in both `/integrations` (personal) and admin Connections. Confirm we
   want it in both, keyed off the per-user vs org credential.

5. **Disconnect semantics.** When a user disconnects an integration, do we revoke
   the provider token, delete `UserConnectionCredentials`, and cascade
   `UserConnectionTool`? (Recommend: delete creds + overlays, best-effort token
   revoke.)

6. **MCP/DCR identity.** For DCR providers (Notion/Jira/Linear), the admin "enables"
   the integration (no app to paste) but does a per-user connect still happen via
   the provider's OAuth? (Yes — DCR removes the *app registration* step, not the
   per-user consent.) Confirm the admin-enable + user-connect split for DCR.

7. **Naming.** "Integrations" vs "Tools" vs "Apps" in the UI. The detail pane calls
   tools "Actions" (matches Langdock/GA mockups). Confirm "Integrations" (page) +
   "Actions" (tools within).

---

## 9. Implementation status (verified)

Built, committed, pushed on `claude/tender-keller-n9mxxi`:
- Dev env + `sandbox-feedback-loop-integrations.md` + `scripts/seed_integrations.py`.
- **Phase 1 backend** — surface axis, Gmail connector, user-scoped `@integration`
  mention, `/api/integrations` (catalog/list/toggle), unit tests. Validated:
  `@Gmail` surfaces in mentions with no agent; `/api/integrations` shows connected
  state + 4 tools; per-tool toggle persists `UserConnectionTool`.
- **Phase 2 UI** — Integrations catalog page + card + detail modal with per-tool
  toggles. Screenshot-verified against the running app.

Next: Phase 0 runtime injection (make `@Gmail` actually give the agent its tools in
a conversation) and the `/integrations` master-detail refactor (§3).
