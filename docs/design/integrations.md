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

## 1. Information architecture — three surfaces, one usage catalog

`/integrations` is the unified **"Sources & tools available to me"** catalog — a
discovery + usage surface, NOT a "things I personally connect" surface. It lists
**everything the user can query**: their personal tools *and* the org data sources
they have access to, distinguished by a **badge + per-type affordance**.

| Surface | What it is | Scope | Ceremony | Lives in |
|---|---|---|---|---|
| **Agents** (`/agents`) | Curated units: connections + tables + tools + instructions + skills + evals | Org / shared | High — reusable, governed | existing master-detail |
| **Integrations** (`/integrations`) | Unified catalog of sources & tools available to me (personal tools + org data) | Personal **and** org (badged) | Low — discover & query | new page |
| **Connections (admin)** | Admin CRUD for SQL servers / data sources, schema sync | Org / admin | Admin-managed | Agents → Connections panel |

The dividing line is no longer *what appears on the page* but the **badge +
affordance**:

- **Personal** items (Gmail, my Jira, personal Drive) — "Connected as you", OAuth
  connect/manage.
- **Org** items (Snowflake, Postgres, BigQuery) — "Org" badge, **read-only
  surface**: no Connect button (admin owns them); "available to you"; admins get a
  **Configure →** deep-link into the admin Connections flow.
- **Available** items (catalog, not yet connected) — Connect.

Admin **CRUD** for data sources still lives in **one place** (the admin Connections
flow); `/integrations` only *surfaces* org data sources read-only. A connector with
`connect_audience="both"` (e.g. Google Drive) can show in both `/agents` and
`/integrations`, and can carry both an Org and a Personal facet.

### How the surfaces relate (the graph)

- An **integration item** = a single connectable/usable source → query directly or
  `@mention`.
- An **agent** = a curated bundle that *can include* connections, integration
  tools, tables, and instructions.
- **Promotion path:** an admin attaches a tool/connection into an agent (the
  existing `domain_connection` link). Integrations are the lightweight "use it now"
  entry; agents are the curated, governed bundles. No duplication — same
  `Connection`, different framings.

### Guardrails (so the unified catalog isn't a mush)
1. **Different affordance per badge.** A non-admin never sees "Connect" on an org
   data source — they can't connect it. Org items read "available," not "connect."
2. **One source of truth for admin CRUD.** `/integrations` surfaces org sources;
   create/edit stays in the admin Connections flow (the Org card "Configure" is a
   deep-link).
3. **Respect RBAC.** Show only the org sources the user can access
   (`DataSourceMembership`, public/membership) — otherwise the page leaks the data
   map.
4. **Two "Add" doors.** "Add integration" (personal, self-serve) ≠ admin "Connect
   data source." The catalog can *show* org items, but adding one is the admin flow.

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

**Left panel — available sources & tools (grouped by badge)**
- **Your connections** (personal): `Gmail •`, `Jira •`, personal `Drive •` — live
  status dot (connected / token-expired / needs-reauth). Explicitly "yours."
- **Workspace data** (org): `Snowflake`, `Postgres`, `BigQuery` the user can access
  — **Org** badge, no connect affordance.
- **Available** (catalog, not yet connected): self-serve connectors with Connect.
- Search + filter (All / Personal / Org / Needs auth) at top.

**Right panel — selected item detail (affordance depends on badge)**
- Header: icon, title, **badge** — "Connected as you · yochay" (personal) or "Org ·
  workspace" (org). Connect/Disconnect for personal; for org, **Configure →**
  (admins only, deep-link to admin Connections).
- **Actions / Tables** — for tool integrations: actions list with per-user toggles
  (`UserConnectionTool`). For org data sources: the tables/catalog (read-only,
  RBAC-scoped).
- **Auth** — personal: your sign-in / token / reconnect. Org: "available via your
  workspace" (no per-user auth, or per-user overlay if `user_required`).
- **Usage** — recent calls + which reports/agents used it.
- **Admin view** (admins only) — org-wide enabled actions + shared OAuth app /
  connection config.

**The crucial CTA — query directly (personal AND org).** Each detail gets a **"New
report"** button that opens a chat scoped to `@<item>` — `@Gmail` or `@Snowflake` —
with no agent required. Org data sources get the **raw** experience (tables, no
curation); the secondary **"Used in N agents →"** points to the curated path. This
is the same `@mention` flow, given a home on this page.

### Badges & ownership semantics
- **Personal** items show **"Connected as you"**, use *your* token at query time;
  another user's `/integrations` shows *their* personal connections, not yours.
- **Org** items show **"Org"**, are shared/admin-owned, RBAC-filtered to what the
  user can access, and are **read-only** here (admin CRUD lives elsewhere).
This visible badge contrast is what lets one catalog hold both without confusion.

### Two doors for "Add"
- `/integrations` **Add integration** → self-serve (personal) connectors only.
- Admin **Connect data source** (SQL servers) stays in the admin Connections flow.
  The catalog *shows* org data sources read-only, but adding one is the admin flow.

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

### Phase 0 — Runtime: resolve a connection without an agent (unblocks everything)
- Relax `resolve_file_client` (and the tool registry) to accept a connection the
  current user can use — **personal** (holds `UserConnectionCredentials`) **or
  org** (an RBAC-accessible data source) — not only `report.data_sources`.
- Inject a mentioned connection's tools/tables into the turn's toolset with no
  DataSource attached, capability-gated, resolved by current user.
- One mechanism, double payoff: serves both personal `@Gmail` and org `@Snowflake`
  standalone queries.
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

1. **DECIDED — `/integrations` is a unified catalog, not personal-only.** It
   surfaces both **personal** tools (badge "Connected as you") and **org** data
   sources (badge "Org", read-only, RBAC-filtered). Org SQL servers (Snowflake,
   etc.) appear in **both** `/agents` and `/integrations`. Admin CRUD stays in the
   admin Connections flow. Remaining sub-question: do we also want a third
   **"Shared tool"** class (an org-shared Jira service account, distinct from a
   data source)? Default: no — shared tools become an agent.

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
