# Release Notes

## Version 0.0.425 (June 25, 2026)
- **Personal API keys in the User Profile + MCP modal fix (#451)** — the user profile modal gains an **API Keys** tab (list, one-time-reveal generate, delete) for the `bow_…` tokens used by the MCP server / programmatic access, reusing the per-user `/api/api_keys` endpoints. Also fixes a bug where the MCP modal always showed **"0 API tokens"** when opened from the sidebar: its key-list `watch` wasn't `{ immediate: true }`, so on the already-open (`v-if`) mount the loader never ran. New `profile.apiKeys.*` strings across all 10 locales.
- **Per-model LLM access control (RBAC, enterprise) (#449)** — admins can restrict individual LLM models to specific users, groups, or roles. Models are **open by default**; restriction is opt-in per model (`is_restricted`) and reuses the existing `ResourceGrant` permission machinery. When a model is restricted, only principals with an explicit grant can see and use it; **full admins bypass** restrictions and **members without a grant get 403**. An **always-available guard** prevents restricting the org default / small-default models (400) so an org can't lock itself out, and enforcement is **fail-open** when the `llm_access_control` license feature is inactive (no regression for non-EE installs). Enforced in both `get_models` (list/picker) and `get_model_by_id` (the real completion-selection boundary), with audited `…/models/{id}/access` and `…/models/{id}/restricted` routes. Surfaced as a new **Access** column + grant modal in the models settings table and in the role editor.
- **Apache Druid — API token (bearer) authentication** — the Druid connector now offers an **API Token** auth method alongside Username / Password, for endpoints that authenticate with a bearer token (e.g. Imply Polaris API tokens). The token is sent as `Authorization: Bearer <token>` via the driver's `jwt` path and is mutually exclusive with Basic auth (a token suppresses any user/password). Pick it from the connection's auth-method selector; the token is encrypted at rest like any other credential.

## Version 0.0.424 (June 25, 2026)
- **Fix — single-value cards show the asked-for value over melted KPI tables (#446)** — when `create_data` produces a melted/long KPI result (`Metric | Value | Format`, one row per metric), a single-value `count` / `metric_card` no longer renders the wrong row (the date, or the sum of every metric). The row-selecting default filter is now carried through `create_data` and `agent_v2` (previously dropped), derived deterministically when the viz model omits it (`derive_kpi_row_filter`), and applied in `ToolWidgetPreview` via the view's own `defaultFilters`.
- **Monitoring — surface origin platform in diagnosis/trace (#447)** — the monitoring diagnosis table and trace modal now show where each agent run originated (Slack / Teams / WhatsApp / MCP / Email vs. the web UI). An origin platform icon sits next to each run's message and an origin badge in the trace header (web-UI runs show none); `external_platform` is plumbed through `AgentExecutionSummaryItem`, `ConversationTraceResponse`, and `console_service`. The diagnosis **User** column moved next to the prompt, and **Date** now shows date and time in the org timezone.

## Version 0.0.423 (June 25, 2026)
- **Cost console — LLM spend by user / agent / group over time (#440)** — a new **Cost** tab under `/monitoring` for admins, breaking LLM token and dollar spend down by user, agent (data source), group, model, provider, or feature (scope) over a date range, with KPI totals, a daily cost/tokens trend chart, and a per-dimension breakdown table. Backed by new attribution columns (`organization_id` / `user_id` / `report_id` / `data_source_id`) on `llm_usage_records` — all nullable, so pre-existing rows stay org-scoped and the rest surface as an **"Unattributed"** bucket. Attribution is stamped once per agent run via an ambient context var and snapshotted at record-schedule time, so it survives the background recorder and the worker-thread judge (tool sub-calls included).
- **Follow-up question suggestions (#441)** — after each answer in the web app, the agent proposes a few natural next questions, rendered as minimalist chips below the feedback bar. Gated to web sessions only (Slack/Teams/Email/Excel/scheduled runs are excluded) and to the org setting **`enable_follow_ups`** (on by default, surfaced in AI settings). Suggestions are generated inline at the tail of the agent run on the small/default model, persisted on the completion (`follow_ups` column) and pushed over SSE for instant render, and rehydrated on reload.
- **Report avatar branding + per-model provider logo (#442)** — the assistant avatar in a report now renders the **organization's uploaded brand image** (falling back to the BoW logo), height-bound and aspect-ratio-safe, with a small overlay badge for the **LLM brand** that produced each completion and a `Generated with …` tooltip. Brand resolution is name-first (`claude → anthropic`, `gpt/o1/o3 → openai`, `gemini → google`), falling back to the hosting provider type, so a Claude/GPT model served via Bedrock or a custom OpenAI-compatible endpoint still shows its true model brand. The shipped Anthropic provider icon is replaced with the orange **Claude** mark (square burst for compact slots, burst + wordmark for the provider picker).

## Version 0.0.422 (June 24, 2026)
- **Dark mode** — comprehensive dark theme across the app using Tailwind's `dark:` variant strategy driven by `@nuxtjs/color-mode` (default preference `system`, so the OS setting is respected on first load). Form inputs get explicit dark backgrounds, light PNG illustrations are hidden in favor of icon placeholders, and ECharts render with a dark theme. Choose Light / Dark / System per user from the new profile modal's **Appearance** tab.
- **User profile modal** — a new modal opened from the sidebar user menu (**Profile**), with four sections: **General** (avatar upload/remove, editable full name, email, and a summary of linked external platforms), **Custom Instructions** (a personal note about yourself surfaced to the AI when it works on your behalf — stored as your per-org membership note), **Usage** (per-user tokens/queries/data for the current window, with a clear notice when usage tracking isn't enabled), and **Appearance** (theme + language). Avatars are stored on the user (`users.image_url`, migration `usravatar01`) and now also render in the sidebar and on your messages in reports.
- **Per-user language override** — each user can pick their own interface language from **Appearance**, overriding the organization default for their account only (persisted per browser). The profile modal is fully localized across all supported languages.

## Version 0.0.421 (June 24, 2026)
- **Auto-reindex schedule — interval _or_ fixed time** — a connection's scheduled schema reindex (including QVD sources) can now run **either** on a recurring interval (every N minutes/hours, 10-minute minimum) **or** at a fixed daily time, chosen per connection in the connection detail panel. Fixed times are interpreted in the organization timezone. (Enterprise `scheduled_reindex`.)
- **Organization timezone** — a new **Settings → General** option that sets the org's IANA timezone. Timestamps across the app (reports, monitoring, audit, instructions, integrations, etc.) now render in it, scheduled jobs (reindex times and scheduled reports) fire in it, and the planner is told the current time in it. Storage stays UTC — the timezone only governs schedule interpretation and display; leaving it unset keeps the prior browser-local behavior.

## Version 0.0.420 (June 23, 2026)
- **Fix — BigQuery queries failed with "Please install the 'db-dtypes' package"** — added the `db-dtypes` runtime dependency so BigQuery results convert to dataframes correctly.

## Version 0.0.419 (June 23, 2026)
- **Instructions — scope by run-mode and delivery channel** — instructions can now be restricted to specific agent run-modes (Chat, Deep analytics, Training) and delivery channels (Web app, Slack, Teams, AI mailbox, MCP). The selectors live in a new collapsible **Advanced** section of the instruction editor (empty = applies everywhere). The scoping is honored at prompt-build time so an instruction only loads in the modes/channels it targets, and the fields are versioned (snapshotted into instruction versions and carried through build promotion/diffing).
- **Fix — instruction "Pending review" status was inconsistent across views** — the same instruction could read **Active** in the agent instruction view but **Pending review** in the report agent panel. The list and single-instruction endpoints now derive the pending signal from the same authoritative per-hunk review rule as `/instructions/pending-changes`, so a leftover/already-applied (covered) build no longer over-reports as pending. The status dropdown in the report agent editor also stops showing a value ("Pending review") that wasn't one of its options.
- **Fix — "+" to add an instruction was hidden behind the Review panel** — clicking the **+** on Instructions while the Review feed was open now closes Review and opens the new-instruction editor instead of doing nothing.

## Version 0.0.418 (June 23, 2026)
- **Microsoft Analysis Services (SSAS) data source** — a new enterprise connector for SQL Server Analysis Services over XMLA, supporting both Multidimensional (MDX) and Tabular (DAX/MDX) models.

## Version 0.0.417 (June 21, 2026)
- **Infor OLAP (Infor d/EPM) data source (#425)** — a new enterprise connector for the Infor d/EPM OLAP semantic layer (formerly Infor BI / MIS Alea OLAP), the supported path into on-premise **Infor OLAP 25.x** where native connections are gone and **XMLA is mandatory**. It speaks the standard **XMLA SOAP** contract over HTTP with Basic auth: schema discovery via `Discover` (catalogs, cubes, dimension hierarchies, and measures — each cube surfaced as a `Catalog/Cube` table whose columns carry their MDX `unique_name`), and query execution via `Execute` (Tabular) that runs **MDX** and flattens the rowset into a DataFrame (decoding XMLA `_xHHHH_` escapes). SOAP faults and inline XMLA errors surface as clear errors. Configurable endpoint URL, optional catalog scope, SSL verification, and timeout.
- **Agents — connections footer fixes** — the bottom-left **Connections** footer is no longer pushed off-screen (requiring a scroll) when a top banner is shown: the Knowledge Explorer now sizes itself to the viewport minus the banner height. It also shows an explicit **"Add connection"** CTA in the empty state, and **childless connections** — created but not yet linked to any agent — now appear in the list instead of being hidden until an agent exists.
- **Fix — Tables selector "Save" button hidden until scroll** — the Save bar in the tables selector is now pinned (sticky) to the bottom of its scroll container, so it stays visible without scrolling to the end of long table lists (agent Tables panel, schema wizard, onboarding, etc.).
- **Fix — report tool card flicker** — the `edit_instruction` tool card no longer rapidly flickers between its rendered document and its `v1 → v2` version-diff view during/after an edit stream. The card is keyed on the stable block id so streaming/poll updates no longer remount it.
- **Fix — Microsoft Fabric "Login timeout expired" on cold-start endpoints** — Fabric Warehouse/Lakehouse SQL endpoints are serverless and can be slow to respond on the first connection after the capacity has been idle, routinely exceeding the ODBC driver's short default login timeout (~15s) and surfacing as `HYT00 … Login timeout expired (SQLDriverConnect)`. The Fabric client now sets a generous 60s login timeout (`Connect Timeout` + `pyodbc` `timeout`), adds driver-level `ConnectRetryCount`, and retries transient connection-timeout SQLSTATEs (`HYT00`/`HYT01`/`08001`/`08S01`) a few times with backoff so a cold endpoint gets a chance to wake up. This affects both service-principal and per-user (OBO/Entra) auth.

## Version 0.0.416 (June 21, 2026)
- **Backend dependency management moved from pip to uv (#408)** — `requirements_versioned.txt` is replaced by a PEP 621 `pyproject.toml` + `uv.lock`, the Docker build and CI now use `uv sync --frozen`, and contributors install with `uv sync --extra dev` (see `DEV.md`). uv is from the same Astral toolchain as ruff and is significantly faster than pip/Poetry.
- **Security — resolved all High/Critical dependency vulnerabilities** flagged by Snyk in both the backend (uv) and frontend (yarn) dependency trees.
  - Backend: `cryptography` 46.0.7 → 49.0.0 (out-of-bounds read) and `starlette` 0.50.0 → 1.3.1 (SSRF, resource exhaustion, unsafe reflection, request smuggling, incorrectly-resolved name). Resolving Starlette required matching bumps to `fastapi` (→ 0.138.0), `fastapi-mail` (→ 1.6.5), and `aiosmtplib` (→ 5.1.2), which previously capped it. Backend scan now reports **0 issues**.
  - Frontend: `nuxt` → ^3.21.7 (open redirect), `vite` resolution corrected to `>=7.3.5 <8` (directory traversal — the prior resolution pinned the vulnerable 7.3.3), and a new `ws` resolution `>=8.21.0` (asymmetric resource consumption). Frontend now has **0 High/Critical** issues.
- **Docs** — added `docs/snyk-dependency-scanning.md` (skill-format guide) covering how to scan the uv backend and yarn frontend with the Snyk CLI and apply fixes.

## Version 0.0.415 (June 20, 2026)
- **Knowledge Explorer** — a new three-pane workspace (at `/instructions`) for browsing and managing everything an agent knows: global instructions, skills, per-agent resources, pending reviews, and each agent's tables and tools. Tree navigation with search and filtering (by status, load mode, source, category), inline editing of titles/descriptions/conversation starters, file upload and preview, and a version-history pane with diff view.
- **Agent management** — a guided **New Agent wizard**, a dedicated **Agent Settings** panel, and per-agent **automation settings**, plus a clearer public/private agent distinction surfaced across the UI.
- **Continual & self-learning** — agents can automatically run evals and a retrain/reliability loop (e.g. on instruction or table changes), surfaced in a new **Agent Evals** panel, so higher-autonomy agents keep improving on their own.
- **Skills with smart loading** — instructions can now be authored as **skills** that load on demand: the prompt carries a lightweight skills catalog and the agent reads a skill's full body only when it needs it (`read_skill`), keeping context lean.
- **Suggestions & review workflow** — a **pending-review feed** with **per-hunk tracked changes** (accept or reject individual edits), diff visualization, and an approval flow for instruction suggestions.
- **Better instruction management** — instruction **descriptions**, table-scoped instructions with name-based datasource fallback, and improved reference resolution (connection-table IDs, bare and schema-prefixed table names).

## Version 0.0.414 (June 18, 2026)
- **Fix — long instructions hid the Edit button in the instruction modal** — in the global create/edit instruction modal, a long instruction body made the content area un-scrollable and pushed the action footer (Edit in view mode; Update/Cancel in edit mode) off the bottom of the modal, so the instruction couldn't be edited. The modal now keeps a properly bounded flex layout so the content scrolls internally and the footer stays visible.

## Version 0.0.413 (June 18, 2026)
- feat(mcp): let the agent read MCP server resources (list_mcp_resources + read_mcp_resource)
- fix: prevent 'Cannot use import statement outside a module' in artifact iframe 

## Version 0.0.412 (June 16, 2026)
- **Apache Druid data source** — connect to Apache Druid and query it as a new data source.
- **Trino data source** — connect to the Trino distributed SQL engine and query it as a new data source.
- Agents page redesign - easier navigation around instructions, tools, tables, tc
- **Continual Learning** - trigger evals -> retrain loop on table change or instruction change for high autonomy

## Version 0.0.411 (June 15, 2026)
- **⌘K command palette** — a global **⌘K / Ctrl+K** palette for quick navigation and creation, opened from anywhere in the app. One input searches across **recent reports**, **agents**, and **instructions** (server-side search for reports/instructions, client-side filtering for agents; recents shown by default), with pinned, query-echoing create actions: `New report "…"` (creates and navigates) and a permission-aware `New instruction "…"` / `Suggest instruction "…"` that opens the instruction modal pre-filled with the typed text. No-match queries still surface the create actions.
- **Publishing lifecycle for agents (`publish_status`)** — a manager-set publishing state, distinct from the system-managed connection-health flag: **published** (visible to everyone with access), **draft** (visible only to builders who can `manage` the agent), and **disabled** (hidden everywhere and excluded from AI context). Viewer-aware filtering applies across the data-source/agent selector, schema context, and public (Slack) listing; consumers see only published agents while managers also see drafts. Existing agents are backfilled to *published*.
- **Agent research tools (`search_reports` / `read_report`)** — two read-only planner tools that let the agent discover and read **the current user's own reports**: `search_reports` lists/substring-searches the caller's reports by title with status/mode filters, and `read_report` returns one of the caller's reports (metadata, data sources, artifact summary, conversation). Both are strictly scoped to the caller — any other report, including ones merely shared with the user, returns *not found* (no leak) — and each has its own tool card in the report view.
- **Settings → Channels** — the settings **Integrations** tab is renamed **Channels** across all locales, with a redesigned page (and a new empty state). The **SMTP Server** configuration moves from a modal into its own dedicated settings page.
- **Test Connection for existing LLM providers** — the **Test Connection** button is now available when editing an existing LLM provider, not just when adding a new one. Blank credential fields fall back to the stored (encrypted) values, so you can re-test a saved provider without re-entering secrets.
- **Instruction modal redesign** — the create/edit instruction modal gains a wider split layout with a dedicated, slide-in **analysis panel** (related instructions, impacted prompts, and impact score) and a cleaner global-vs-private form structure.
- **Scheduled tasks** — clicking a scheduled task card now opens its edit modal directly (clicking the report name still navigates to the report), and the modal shows a link back to the report when editing a task tied to one.
- **Fix — second admin sees empty tables on shared OBO/Fabric agents** — on a shared-catalog `user_required` (Fabric/PowerBI/OBO) data source, a second admin with a valid delegated token saw zero tables and *Reload tables* didn't help, because the reload refreshed only the canonical catalog and never the caller's per-user overlay. The shared-catalog reload now also refreshes the caller's overlay so their tables appear immediately, without leaking the canonical catalog to disconnected callers.
- **Fix — race when deleting a data source during background indexing** — deleting a data source while a background connection indexer was re-syncing schema tables could reintroduce `datasource_tables` rows and trigger a foreign-key violation. The delete now re-clears the schema tables and retries until the indexer stops producing rows.
- **Fix — RTL alignment** in the Clarify tool.

## Version 0.0.410 (June 13, 2026)
- **Email the AI analyst (AI Mailbox)** — a new **Email** channel (alongside Slack/Teams/WhatsApp) lets people email the analyst and get answers back. It's IMAP/SMTP under the hood (provider-agnostic: Microsoft 365, Google Workspace, or any self-hosted server) with three auth modes — password/app-password, **Microsoft 365 app-only OAuth** (XOAUTH2), and **Google Workspace** (service account + domain-wide delegation). Inbound mail flows into a report, the agent replies in-thread (with a deep link back to the report), and **attachments are ingested as report files** (size-limited). Configure it from **Settings → Integrations**, with an inline **Test connection** before saving. IMAP is the optional upgrade that turns a send-only mailbox into a two-way channel.
- **Verify-first inbound identity** — by default a new sender must prove they control both the mailbox *and* a BOW account: first contact gets a **verification link** that, clicked while signed in, creates a trusted `email → user` binding (subsequent mail is trusted, like Slack/Teams). A spoofable `From` alone never grants data access. A pre-filter (DMARC/DKIM where available + domain allowlist + loop/auto-reply suppression) drops spoofers and noise first; registered-but-unlinked users, open invites, and signup-admitted domains each get the appropriate link rung, and everything else is **ignored + audited**. Auto-linking without verification is now an explicit, clearly-labeled opt-in.
- **Org SMTP transport** — a dedicated **SMTP Server** setting (separate from the AI Mailbox) becomes the org's transport for *system* mail — share notifications, scheduled-report/prompt results, verification links — overriding the global `bow-config` SMTP. The password is Fernet-encrypted at rest, no-auth/anonymous relays and a `validate_certs` toggle are supported, and there's a pre-save Test connection. Analyst mail always uses the mailbox; system mail never does — the two transports are kept strictly separate.
- **Scheduled schema auto-reindex** — connections can now periodically re-index themselves so tables stay fresh, with a per-connection toggle and a configurable interval (every N hours) in the connection detail modal; the last reindex error is surfaced inline. Scheduled reindexing is an enterprise feature.
- **Per-org license quotas** — licenses can now cap `max_users` and `max_agents` per organization (claims read from the license JWT; missing/negative means unlimited), enforced on user and agent creation.
- **Guaranteed data access on every dashboard** — building on 0.0.409's component ⓘ popover, an always-on, LLM-independent **DataInspector** (a floating "Data" button auto-mounted into the dashboard iframe) lists every visualization with the same **Data**/**Code** tabs, so even fully custom dashboards that never use the prebuilt cards still expose their backing data and query. A bare `<EChart>` outside a SectionCard now also carries the ⓘ popover. Suppressed in headless thumbnail/preview renders.
- **Instruction pill fix** — the report completion pill now includes **system-category** instructions (previously hidden), and partial/pill accepts are reflected correctly in the knowledge group.

## Version 0.0.409 (June 11, 2026)
- **Built-in info popover on dashboard components** — the prebuilt KPICard and SectionCard now carry a small ⓘ popover that surfaces a component's backing data. It opens on a **Data** tab (the actual visualization rows in a compact scrollable table) with a **Code** tab for the generating query, plus metadata above (source, type, row/column counts, active filters) and the viz id in a persistent footer. Both producers wire it automatically: deterministic "Add to Dashboard" codegen emits `viz={viz[N]}`, and the `create_artifact` / `edit_artifact` prompts instruct the model to do the same. The popover is **filter-aware** — when a component renders filtered rows it shows exactly what's on screen ("X of Y rows (filtered)") and only attributes filters that map onto the viz's columns, falling back to the full dataset otherwise.
- **Spark Connect data source** — new connector for querying Spark via the Spark Connect protocol, with partition metadata in the schema, a pre-flight `EXPLAIN` gate (partition-filter + scan-size guard), and a Spark icon in the data-source picker.
- **Scheduled tasks on specific days of the week** — recurring scheduled prompts can now target specific weekdays (e.g. Mon/Wed/Fri) instead of only daily/interval cadences, with localized day labels (including conventional Arabic/Hebrew day-of-week abbreviations).
- **Copy invite link always returns a usable link** — copying a pending member's link now rotates the token and resets the 14-day window if the invite has expired (or had no token), clearing the **Expired** badge; a still-valid link is returned unchanged so an already-emailed link isn't invalidated. No email is sent (that's **Resend**).

## Version 0.0.408 (June 10, 2026)
- **Roles, groups & quotas for not-yet-registered members** — admins can now assign RBAC roles, add to groups, and set a usage-policy (quota) on a *pending* invite (a user who hasn't signed up yet). These are stored against the invite and automatically materialized onto the user when they register, so access is correct on their very first request. Invites can also be pre-assigned at invite time (role/group/quota fields in the Invite modal), and removing a pending member cleans up its role/group/quota assignments.
- **Token-gated invites with expiry + resend** — invite links now carry a single-use token and expire after 14 days. On local/password sign-up the token is required: an invalid, expired, or missing token (for an invited email under closed signups) blocks account creation entirely. SSO/OIDC sign-up is unchanged (the IdP verifies identity, no token needed). A per-row **Resend** action (Members tab, requires `manage_members`) rotates the token, resets the 14-day window, and re-sends — the old link stops working immediately. Admins can also fetch a pending invite's link via an admin-only endpoint (handy when SMTP is off). Pending rows show an **Expired** status when the window lapses.
- **Reliable, human invite & welcome emails** — the invite email is now sent synchronously with retries + a per-attempt timeout (no more silent fire-and-forget), and the outcome (`sent` / `failed` / `skipped_no_smtp`) is surfaced to the admin. New users get a plain-text **welcome email** summarizing the agents (data sources) they can access with a link in. Copy is plain-text and human (no buttons), signed "BOW".
- **Members tab overhaul** — compact, cleaner table; checkbox selection with **bulk actions** (add role, add to group, remove); client-side **pagination**; row **Resend**; the **Actions column is frozen** to the right while the wide table scrolls; borderless inline Role/Quota selects; consistent role-name casing; collapsed group chips ("+N"); wider Note column with tooltip. The **Groups** and **Quotas** tabs now share the same compact styling.
- **Private data sources by default (#364)** — newly created data sources / agents are now private by default (`is_public = false`); only explicitly-added members (and admins) can see them unless opted public. Adding a member to a data source now sends a **delayed "you've been added" email** (5-minute delay, re-validated at send time so an undone add never mails, claimed so exactly one worker sends).
- **MCP search (#366)** — `search_mcps` supports wildcard queries (list everything) and ships a clearer tool description.

## Version 0.0.407 (June 9, 2026)
- Fix "Shared with me" reports linking to the owner's `/reports/:id` page (which renders blank for non-owners) — they now open the read-only shared conversation view at `/c/:token`. Shared reports without a share token are no longer clickable.

## Version 0.0.406 (June 9, 2026)
- SQL Server connections can now pass extra ODBC keywords (e.g. `ApplicationIntent=ReadOnly` to route to a read-only Always On replica) via a new optional **Additional Connection Parameters** key-value editor in the connect form. Security-sensitive keys (Encrypt, credentials, driver, server, database) cannot be overridden, and existing connections are unchanged.

## Version 0.0.405 (June 9, 2026)
- QVD date/timestamp/time fields now load as real DATE/TIMESTAMP/TIME columns instead of raw Excel-style serial numbers, so they filter, sort, and group as dates

## Version 0.0.404 (June 8, 2026)
- Fix duplicate scheduled emails/reports under multi-worker/replica deployments — each cron fire is now claimed once via a DB-backed lock so exactly one worker runs it (also covers cache warmups, payload purge, and LDAP sync)
- License expiry now takes effect without a restart, plus a global expiry-countdown banner and a redesigned license settings page (tier/expiry details, expiring-soon and expired states, renew CTA)
- Small (<10 row) create_data results are no longer sent to Slack/Teams and are auto-collapsed in the report UI, since the agent's text already states the values
- Manage an agent's primary instruction from the agent page: edit, replace with an existing instruction, or start a training session
- Many-series (>8) line/bar/area charts now use a scrollable vertical legend docked on the right instead of an overflowing horizontal one
- Data-source and agent pickers grow to fit long names instead of truncating
- Fix report auto-title silently not saving (mostly on Postgres) when the background task outlived its DB session

## Version 0.0.403 (June 8, 2026)
- **Teams** — a reused Teams 1:1 conversation report (up to 5 days old) now re-syncs its data sources to the user's current access on each message, so grants appear and revocations disappear without waiting out the window.
- **UI** — the data-source members panel relabels the management column to "Management role" and the empty state to "Query only" (was "None"), and clarifies that everyone listed can query the agent and that Remove is what revokes access.

## Version 0.0.402 (June 8, 2026)
- Admin query-identity toggle for delegated (Entra ID / Microsoft Fabric OBO) connections — admins/owners can now choose, per connection, to run queries as the **service account** (the connection's principal) or as **themselves** (their own delegated/OBO token), from the connection detail modal. Default is "Me": the service principal is never used silently for an admin's interactive queries — if they have no personal token yet, the query is blocked and the UI prompts them to Connect. The selection is persisted per (user, connection) and applied consistently across the tables selector (overlay vs shared catalog), the agent's schema context, and query execution (inspect/create data).

## Version 0.0.401 (June 7, 2026)
- Agent run activity chart in /monitoring diagnosis — daily agent executions bucketed by status (success/error) with click-to-filter by day, backed by a new diagnosis timeseries endpoint
- Add a `bagofwords` MCP skill template documenting the core analysis workflow (create report, run tracked queries, build dashboards) for use with the BOW MCP connector
- MCP error handling: tool-level MCP failures (`isError`) now surface the server's real error message instead of `None`, so the agent can correct course instead of retrying blindly — and failed MCP calls no longer show a misleading green ✓ in the trace
- MCP planner context: the `execute_mcp` digest now echoes which underlying tool was called and with what arguments (plus the real error on failure), so the planner stops looping through call variants
- MCP tool UI: the tool card now shows the actual command/input invoked (tool + arguments for `execute_mcp`, query for `search_mcps`, code for `write_csv`), not just the result

## Version 0.0.400 (June 7, 2026)
- Teradata Vantage data source integration — connect Teradata as a data source, with sample queries included in the client description
- Generated-code reuse via `load_step`/`load_entity` — the planner and coder now prefer loading a prior step's results over rebuilding from scratch, reducing redundant code generation
- Fix LLM token-usage undercount in /monitoring (no added latency)

## Version 0.0.399 (June 7, 2026)
- Fix MCP tool results aborting the agent run: materializing a large/tabular MCP result to a file linked it to the report before the file's id was assigned, causing a foreign-key violation that poisoned the shared transaction (surfaced as "transaction is aborted" / agent execution errors). File linking now happens after the id is set and inside a savepoint, so a materialization failure degrades gracefully instead of failing the whole run. Also restores CSV preview generation, which was silently broken.

## Version 0.0.398 (June 6, 2026)
- Inbound webhooks for reports — connect GitHub, Jira, or any other service (Generic catch-all) so external events flow into a report's chat. Configure them from the report Summary tab; each report's webhook count shows in the reports list.
  - Per-webhook signing key with three verification modes: token header (default — a shared secret, works with Jira Cloud and most legacy systems), HMAC signatures (GitHub-native or BOW's own scheme), and URL token (for senders that can only POST). Per-org delivery dedup and rate limiting, plus a one-time URL + key reveal on create/rotate.
  - Optional small-model AI classifier decides whether an event warrants a response — guided by an optional per-webhook prompt plus your org instructions and the report's conversation — and, when it acts, authors the task the agent runs. The event entry shows a live 👀 (working) → ✅ (done) status; declined events are marked "no action needed".
  - Gated org-wide by the new "Report Webhooks" setting (on by default), with org limits for max webhooks and delivery rate.

## Version 0.0.396 (June 6, 2026)
- Star (favorite) reports — starred reports are pinned to the top of /reports. Starring is per-user, so each person keeps their own favorites, and you can star reports shared with you read-only

## Version 0.0.395 (June 6, 2026)
- Native web search for OpenAI and Azure OpenAI (provider-executed, via the Responses API) — opt-in per provider and gated by the org Web Fetch setting, with a live "Searching the web" step (rendered as a tool with the query + cited sources) and source citations

## Version 0.0.394 (June 6, 2026)
- Fix scheduled tasks running one weekday late (cron day-of-week off-by-one vs the scheduler), and the schedule editor showing the wrong day
- Conversation history now records scheduled-task and email actions (so the assistant can dedupe schedules, cancel the right task, and recall what it emailed)

## Version 0.0.393 (June 6, 2026)
- Scheduled tasks: ask the agent to run something on a recurring schedule (e.g. "email me once a week about ...") — new create/cancel scheduled-task tools, reusing the existing scheduled-prompt UI

## Version 0.0.392 (June 5, 2026)
- Major performance & concurrency-reliability improvements (faster completions, fewer stalls under load)

## Version 0.0.391 (June 3, 2026)
- Email sending tool in reports when SMTP is enabled
- Postgres support for materialized views
- Enhance tableau system prompt

## Version 0.0.390 (June 3, 2026)
- Improve tests reliabilty

## Version 0.0.389 (June 2, 2026)
- Security patches/dependecy updates
- OneDrive indexing fixes
- Athena connector: support boto3 default auth and optional S3 output location

## Version 0.0.388 (May 25, 2026)
- Hide intercom for mobile
- Sharepoint/onedrive/Google drive integrations
- Quick integration of agents

## Version 0.0.387 (May 25, 2026)
- Performance improvements

## Version 0.0.386 (May 25, 2026)
- UI improvement for knowledge group
- auto-link teams/slack members

## Version 0.0.384 (May 24, 2026)
- Improve instructions mgmt and creation
- Add web/http tools to code gen

## Version 0.0.383 (May 21, 2026)
- Improve ds selector to support 'auto' mode
- Performance & reliability fixes
- Clarify tool enhancement
- Added new tool: list agent execution in training mode
- Add MCP to multiple agents

## Version 0.0.382 (May 20, 2026)
- speed improvements
- web fetch tool v2

## Version 0.0.381 (May 18, 2026)
- web fetch tool
- custom system prompt for each platform
- add timestamps for completions

## Version 0.0.380 (May 17, 2026)
- Tableau performance and reliability improvements

## Version 0.0.379 (May 16, 2026)
- fix background completion API
- security patches and fixes

## Version 0.0.378 (May 13, 2026)
- Per-member admin-managed `note` (per-org) injected into the planner prompt as `<user_profile>` context
- Bulk import members from Excel/CSV with dry-run preview; idempotent — never touches roles or group memberships
- Local password sign-in now works for admins as a break-glass when `auth.mode = sso_only`
- Cleaner sign-up disabled error message

## Version 0.0.377 (May 13, 2026)
- Allow SMTP without credentials (use_credentials: false) for anonymous/open relays

## Version 0.0.376 (May 11, 2026)
- Fix connection-indexing crashes ("attached to a different loop" / "unknown protocol state 3") on long Postgres-backed indexing runs by giving the background runner its own NullPool engine

## Version 0.0.375 (May 10, 2026)
- Fix MSSQL "0 tables" on case-sensitive / binary collations (e.g. Hebrew_BIN)
- Surface MSSQL schema introspection errors instead of silently returning empty

## Version 0.0.374 (May 10, 2026)
- Enrich instructions mgmt and diff
- Fix filter bug in widget preview

## Version 0.0.373 (May 8, 2026)
- Query timeout settings
- remove answer tool

## Version 0.0.372 (May 7, 2026)
- Fix clarify tool not verbose enough

## Version 0.0.371 (May 7, 2026)
- Agent db writes - performance/reliability
- better signal in create data tool
- instructions ui fixes

## Version 0.0.370 (May 6, 2026)
- Performance/reliability improvements

## Version 0.0.369 (May 3, 2026)
- add usage / quota limits policies organization wide

## Version 0.0.368 (May 2, 2026)
- add locale for additional languages
- improve UI for agent mgmt and data soures
- allow upload files (csv/xls/pdf) to agents

## Version 0.0.367 (May 1, 2026)
- add a new reindexing connection button
- enable mcp tools by default in org settings
- strengthen clarify tool

## Version 0.0.366 (April 27, 2026)
- 70% speed improvements
- better caching for tokens

## Version 0.0.365 (April 26, 2026)
- Performance improvements
- Change to a faster token counter approach
- Planner v3 (native Anthropic tool_use) is now the default; set `BOW_PLANNER=v2` to fall back to the legacy JSON-envelope planner
- Anthropic prompt caching on planner system prompt + tool catalog; `cached_tokens` instrumentation for OpenAI/Azure
- Async DB writes for `finish_tool_execution` + `upsert_block_for_tool` (next planner call no longer blocks on the prior turn's persistence)
- Measured impact (3/3 trial pass rate, identical plans/SQL): per-trial cost -69% on both Haiku 4.5 and Sonnet 4.6; wall-clock -29% on Sonnet, -5% on Haiku; input tokens -73%

## Version 0.0.364 (April 25, 2026)
- feat: evals tools for training mode
- loading mode to when adding new connectins with a large amount of objects
- auto draft new evals when (admin) user thumbs up
- fix bug when submitting a new prompt when completion ends but in agent knowledge harness mode 
- added native support to GPT-5.5


## Version 0.0.363 (April 22, 2026)
- Improve prompting for Azure default guardrails
- Put oauth in the admin settings
- Improve infer widget visualizations to include filter and agg

## Version 0.0.362 (April 20, 2026)
- PBI on-prem server improvements

## Version 0.0.361 (April 20, 2026)
- Remove nuxt from prod deployment and serve static files via FastAPI
- feat: add Power BI reporting server (on-prem)
- feat: add Oracle BI integration

## Version 0.0.360 (April 19, 2026)
- Fix QVD type parsing
- Improving qvd -> duckdb reliability and performance

## Version 0.0.359 (April 19, 2026)
- Enhance Sybase client for better code/timout/error handling
- Add instruction button in Agent panel
- Improve Dockerfile

## Version 0.0.358 (April 18, 2026)
- SSO + OBO for data connections: OIDC login now extracts email from the id_token, syncs groups, and propagates user identity through to the warehouse
- Entra ID native support for the On-Behalf-Of flow, including `offline_access` and hardened OAuth connection handling
- Permission overlay revokes stale rows when a user loses upstream access; data sources returning 403 are skipped instead of failing the run
- SIEM integration with end-to-end test coverage
- Dashboards and Scheduled Tasks promoted to first-class items in the main navigation
- Per-domain signup controls for opening up self-serve access
- New Excel-specific tools for spreadsheet artifacts
- `exportCSV()` available as a sandbox global so artifacts can produce CSV downloads
- Improved dashboard-generation system prompt for more reliable multi-widget layouts
- Evals harness (dogfooding): YAML suites under `tests/evals`, pytest runner, LLM matrix from `LLM_MODEL_DETAILS`, JudgeRule with execution metadata (tokens, iterations, per-tool durations), tag-based filtering, multi-turn support, SSE streaming, and per-turn completions/reasoning in failure reports

## Version 0.0.356 (April 13, 2026)
- Share dashboards / conversations with specific users or globally

## Version 0.0.356 (April 11, 2026)
- BOW for Excel - you can now have BOW inside your excel!
- PowerBI enhancements

## Version 0.0.355 (April 10, 2026)
- Show instruction usage and attribution per turn
- New sidebar in report page to show summary, dashboard and current agent
- New knowledge harness for agentic instruction suggestions
- Faster instructions management
- UI improvements across report and dashboard views
- RBAC: groups, roles, policies, per-data-source permissions, and connection/MCP tools authorization
- LDAP integration for enterprise authentication
- WhatsApp Cloud API integration
- Spider text-to-SQL benchmark eval driver
- Fix: make SMTP password optional in settings
- Added support for a .bowignore file when integrating a git account

## Version 0.0.354 (April 5, 2026)
- New Scheduled Tasks: set up recurring or scheduled tasks within reports
- New "Add to Dashboard" button to instantly add widgets to an artifact
- New "Polish" action for quick dashboard refinements
- Show recent queries and artifact shortcuts above the prompt box
- Improved dashboard generation speed and performance
- Improved agent filtering by prioritizing master tables for more reliable results
- Added sandbox support for better agentic code development
- Display abort status during tool execution

## Version 0.0.353 (March 30, 2026)
- feat: new a2a integration for timbr
- increase timeout in agent harness

## Version 0.0.351 (March 29, 2026)
- WAL mode for SQLite deployments and timeout settings for PostgreSQL
- Performance improvement for the main completion flow
- Add timing metrics across code gen / execution for agent execution traces 

## Version 0.0.350 (March 29, 2026)
- Add ability to integrate custom MCPs
- Add NetSuite native integration

## Version 0.0.349 (March 28, 2026)
- Performance improvements
- additional logging

## Version 0.0.348 (March 26, 2026)
- Improve Sybase integration and SQL Anywhere to use tds config

## Version 0.0.347 (March 25, 2026)
- Improve context compaction to include inspect_data and set a budget of 200k (overriden by known models if exist)
- Add agent indicator/icon to agent trace
- Add download as png button for charts
- Add more filters to reports page and advanced search

## Version 0.0.346 (March 24, 2026)
- Fix bug that images are sent in future completions
- Allow support for secret/access key in Bedrock LLM service

## Version 0.0.345 (March 24, 2026)
- Make test_connection and other data client utils async calls

## Version 0.0.344 (March 23, 2026)
- Fix artifact sandbox: download React development builds in vendor script
- Remove CDN fallbacks for airgapped deployments — missing vendored libs now fail loudly

## Version 0.0.343 (March 22, 2026)
- Set headers/handling for streaming in HTTP calls from front-end
- Improve context mgmt budgeting 
- Fork previous created reports

## Version 0.0.342 (March 22, 2026)
- Fix context bloat when designing dashboards
- Add full SCIM support
- Enhanced audit trail with more activities
- Expose OpenAPI swagger docs
- Improve animation and frontend look and feel when streaming messages
- Send PDF attachment when publishing a dashboard
- Add read_query tool
- Improve dashboard generation and editing
- BOW for Excel initial set up
- New: GPT-5.4 and GPT-5.4-mini native integration

## Version 0.0.341 (March 18, 2026)
- add opentelemetry

## Version 0.0.340 (March 18, 2026)
- create/edit artifact tool improvements

## Version 0.0.339 (March 17, 2026)
- Sybase connector to support owner schema
- Keep alive for long running MCP queries

## Version 0.0.338 (March 16, 2026)
- minor fixes and changes

## Version 0.0.337 (March 15, 2026)
- added support for MSSQL 2008 (ODBC 17)
- improve artifact generation (speed and reliability)
- added support for Sisense BI

## Version 0.0.336 (March 12, 2026)
- feat: notification service for sending emails — supports dashboard sharing, conversation sharing, and scheduled report delivery with optional PDF attachment

## Version 0.0.335 (March 9, 2026)
- fix: improve timbr semantic layer integration
- fix: llm usage chart to show both input and output

## Version 0.0.334 (March 8, 2026)
- feat: add support for snowflake semantic views
- fix: improve mssql integration to support schema
- fix: mcp improvements
- Add support for databricks multi-catalog discovery

## Version 0.0.332 (March 7, 2026)
- Improved MCP-Apps stability and compatibility with Claude
- Enhanced Databricks SQL connector reliability
- Increased OAuth token storage limits
- Added logging to LLM integrations
- Fix connectivity issues via MCP servers

## Version 0.0.330 (March 5, 2026)
- Pre-cache tiktoken encodings in Docker build for airgapped environments
- Added more logging

## Version 0.0.328 (March 5, 2026)
- fix: when gpt-5 is in model_id string, don't add temprature

## Version 0.0.327 (March 4, 2026)
- Allow skip verify_ssl for custom LLM endpoints
- Intrdouce native Bedrock integration, with IAM/API Key auth methods
- Support MCP-Apps! Now using the MCP in MCP-Apps compatible clients will render visualizations and dashboards
- Introducing Timbr AI beta integration

## Version 0.0.325 (March 3, 2026)
- Fix Alembic migration SSL error when using Aurora PostgreSQL with IAM authentication

# Version 0.0.324 (March 2, 2026)
- Default SMTP config
- Improve k8s helm to support custom certs when using Aurora DB as backend

### Version 0.0.322 (March 1, 2026)
- Support long oauth string columns for Entra
- Allow AWS Aurora PG with IAM as backend DB

## Version 0.0.320 (February 24, 2026)
- Improve table lookup
- Improve OAuth MCP integration

## Version 0.0.320 (February 22, 2026)
- Support deployment in airgapped systems
- Improve PowerBI integration
- Improve Thumbnail generatio for Artifacts


## Version 0.0.319 (February 22, 2026)
- Fixed edit connection "Test Connection" to validate new credentials instead of using saved ones
- Credentials in edit mode are now locked by default with a "Change" button to explicitly unlock
- Renamed "Domains" to "Data Agents" in connection detail modal

## Version 0.0.318 (February 22, 2026)
- Added Sybase SQL Anywhere data source connector (enterprise license required)
- Uses FreeTDS ODBC driver for TDS protocol connectivity on port 2638

## Version 0.0.316 (February 21, 2026)
- Added filters for low score agent executions in monitoring/diagnosis
- Enhanced file upload and completion context handling, and special support for images
- Pass images and screenshots to create_artifact tool

## Version 0.0.315 (February 19, 2026)
- Improved organization logo upload
- Power BI: one table per internal table, relationship support, cleaner SharePoint names
## Version 0.0.314 (February 18, 2026)
- Added Microsoft Fabric data source integration (Warehouse and Lakehouse SQL endpoints)
- Azure AD Service Principal authentication support for Fabric
- Added `read_artifact` tool and improved context engineering for designing dashboards

## Version 0.0.313 (February 16, 2026)
- Update license env variable and secret configuration in k8s and docker-compose

## Version 0.0.312 (February 14, 2026)
- Refactor sidebar to use nav config and proper active states
- Improved slides artifact generation 

## Version 0.0.311 (February 13, 2026)
- Multi-connection support: data sources can now have multiple connections
- Added PowerBI and Qlik (QVD) data source integrations (Enterprise)
- Configurable step retention per organization (Enterprise)
- Exclude shared conversations and published reports from step cleanup
- Connection icons shown when describing/inspecting tables
- Schema enrichment with metadata and column comments
- Data agents and example agent templates
- Delete connections support
- Artifact thumbnails
- Added filtering for reports by schedule to easily view reports based on their schedule settings
- Added domain filtering for monitoring diagnosis to filter by specific domains
- Added report thumbnail generation and preview cards on home page for quick visual reference
- Added support for Claude Opus 4.6 model

## Version 0.0.309 (February 4, 2026)
- Create artifact (dashboard/slides) tool is now available via MCP 
- Added support for Databricks SQL
- Add enterprise license management and audit log

## Version 0.0.308 (January 31, 2026)
- Instruction @mentions now only show published instructions from the main build
- Referenced instructions are automatically loaded into AI context when a parent instruction mentions them
- Schema index and full schema now display instruction count per table, guiding the planner to use `describe_tables` for business rules
- Updated MCP `get_context` tool to expose instruction count per table
- **Microsoft Teams Integration**: Full bot support for Teams channels and 1:1 chats
  - Send questions via @mention in channels or direct message the bot
  - Thread-based conversations with report reuse across replies
  - User verification flow with Adaptive Cards
  - Markdown tables, count results, and report links rendered natively in Teams
  - JWT signature verification for inbound webhooks
  - Teams setup UI in Settings > Integrations

## Version 0.0.307 (January 28, 2026)
- Separated code and queries for better UX
- Added created/approved by metadata for instructions

## Version 0.0.306 (January 26, 2026)
- **New Interactive Dashboards**: Dashboards are now generated as executable React/HTML code, enabling rich interactivity, custom styling, and dynamic visualizations
- **Visual Feedback**: Upload screenshots or images with your prompts to show the AI exactly what you want—perfect for requesting design tweaks or pointing out issues
- Dashboard validation now includes automatic screenshot capture, allowing the AI to visually verify the output before finalizing
- Added vision model support for OpenAI, Anthropic, and Google Gemini LLM providers

## Version 0.0.305 (January 24, 2026)
- **Rebuilt Dashboards**: Now fully AI-generated as executable code (React/HTML) with iterative refinement based on conversation history
- Fixed @ mention detection in prompt input (no longer triggers inside existing mentions)

## Version 0.0.304 (January 22, 2026)
- SQLite data source now available in production (previously dev-only)
- Security updates and dependency patches

## Version 0.0.303 (January 22, 2026)
- AI-suggested instructions now show persistent "Published" status with timestamp
- Added checkbox selection when publishing AI suggestions 
- Fixed AI builds not being linked to agent executions

## Version 0.0.302 (January 20, 2026)
- Rename Catalog to Queries
- Show chart and visualization in query page

## Version 0.0.301 (January 20, 2026)
- Support for local DuckDB databases via file:// or absolute path i.e /data/myduck.db
- Set global git repo management

## Version 0.0.300 (January 19, 2026)
- **Slack Integration Enhancements**
  - Thread-based responses: replies now appear in threads instead of separate messages
  - Each thread corresponds to a single report for better conversation continuity
  - Added support for @mentions in channels (in addition to DMs)
  - Visual feedback via emoji reactions: 👀 when processing, ✅ when complete
  - Data source access control: channel mentions query only public data sources, while DMs include private data sources the user has access to

## Version 0.0.298 (January 18, 2026)
- Added guardrails around code execution
- Removed code validation flag, as it's now deterministic and built-in 

# Version 0.0.297 (January 18, 2026)
- Introducing: Training Mode
  - A dedicated mode for documenting and managing your data domain knowledge
  - Explore schemas, inspect data, and create instructions to guide AI behavior
  - New tools: `create_instruction` and `edit_instruction` for real-time instruction management
  - Instructions are versioned and tracked in draft builds until finalized
- Improve DuckDB system prormpt
- HBD!

## Version 0.0.296 (January 12, 2026)
- Added PostHog integration for analytics
- Fix Dockerfile

## Version 0.0.294 (January 12, 2026)
- improve streaming performance
- support heatmap charts
- block sending prompts if no llm or data source/file were set
- improve conversation layout for mobile presentation
- add delete connection

## Version 0.0.293 (January 10, 2026)
- Fix tables page not showing all tables when navigating between pages

## Version 0.0.292 (January 9, 2026)
- Fix demo data sources not loading in Docker container

## Version 0.0.291 (January 6, 2026)
- Improve streaming for final_message
- Fix multi bar chart rendering bug 

## Version 0.0.290 (January 1, 2026)
- Happy new year!
- Connections and data sources are now decoupled. You can attach multiple data sources to a single connection, each with its own tables, instructions, and evals. This brings much greater flexibility, reliability, and organization to your workspace.
- New: Context Selector – easily control which data sources are currently active throughout the application.
- Added ability to share report conversations with others
- Clarify tool and prompt optimizations

## Version 0.0.288 (December 26, 2025)
- UI improvements: eval, build ID
- Added modal to manage test suites
- Added new MCP tools: list, create, and delete instructions

## Version 0.0.286 (December 25, 2025)
- Auto suggest instructions if user provided negative feedback to an answer
- Improve auto-detect uvicorn workers

## Version 0.0.284 (December 23, 2025)
- Git providers: Now support Personal Access Token (PAT) authentication for seamless integration.
- You can now create pull requests and branches for build (instruction versions) directly from the interface.
- Each build now includes integration tests and eval runs to ensure greater reliability and code quality.
- Simplified instruction status life cycle and integrating to buid statuses
- UI/UX upgrades: Enhanced workflows for adding instructions and reviewing builds, making navigation and use smoother.
- Code clean ups and tests

## Version 0.0.282 (December 22, 2025)
- Launched instruction build/versioning system: every instruction update creates a new version, with point-in-time builds (snapshots), approval workflow, diff, and rollback.
- All instructions now tied to builds; `is_main` build sets active instruction set for org, with full history & audit.
- Added `/builds` API: get builds, build diffs, rollback, and detailed version/content lineage for every instruction.
- Test/Eval runs can select which build to use.
- Exposed top-k instructions retrieval API.
- Extensive automated E2E test coverage for build/version/rollback/git flows.

## Version 0.0.280 (December 19, 2025)
- Context and instructions are now unified
- Instructions now show detailed usage statistics
- New rules for instruction application: always apply, or smart based on relevance/search
- Instructions table redesigned—now with filters, git-sourced instructions, and other enhancements
- Improved create/edit instruction workflow with a refreshed design
- Expanded and updated automated end-to-end tests

## Version 0.0.279 (December 17, 2025)
- Added **MCP Server** for integration with Claude, Cursor, and other MCP clients
- Available tools: `create_report`, `get_context`, `inspect_data`, `create_data`
- MCP sessions are fully tracked in reports with tool executions and visualizations
- Added per-user API keys for MCP and external integrations

## Version 0.0.278 (December 15, 2025)
- Enhancing MongoDB integration to support Atlas/SRV connections
- Add more triggers for autogenerate suggestions 
- UI improvements/fixes

## Version 0.0.277 (December 14, 2025)
- Frontend tests (playwright) and CI/CD improvements

## Version 0.0.274 (December 12, 2025)
- Added support for GPT-5.2 model
- Enhanced the describe entity tool for better usability and accuracy
- Fixed a user authentication bug affecting specific environments

## Version 0.0.271 (December 10, 2025)
- Describe entity from catalog - new tool!
- Remove forgot password/etc when SMTP is not available

## Version 0.0.270 (December 10, 2025)

- bug fixes, performance and reliability

## Version 0.0.269 (December 10, 2025)
- Performance and speed

## Version 0.0.268 (December 9, 2025)
- Speed and readme

## Version 0.0.266 (December 8, 2025)
- Added a new **Inspect Data** tool for quickly examining the structure and sample content of a dataset and preview data before generating insights or diagnosing issues
- Docker Compose now bundled for both development and production environments
- Added sample databases to assist onboarding and demos
- Enhanced overall system reliability and robustness

## Version 0.0.265 (December 7, 2025)
- Bug fixes

## Version 0.0.264 (December 6, 2025)
- Enhanced file management and analysis capabilities (supports xls, csv, and pdf files)
- Improved MariaDB improvements
- Add support for loading up to 60K tables when connecting data sources
- Added automated tests for postgres database

## Version 0.0.263 (December 4, 2025)
- System prompt improvements and a new section for analytical standards
- Improvements to custom LLM integration (set default/small default models)
- Data source onboarding improvement

## Version 0.0.262 (December 2, 2025)
- Added data source integration to MongoDB
- Added native support for Custom LLM endpoints (openai compatible)
- Added support for Claude Opus 4.5

## Version 0.0.261 (December 2, 2025)
- Bias partitions in bigquery

## Version 0.0.260 (December 2, 2025)
- Dependencies updates
- Improve instructions list modal 

## Version 0.0.259 (December 1, 2025)
- Introducing Filters in dashboards
- Performance improvements, page loads, indices, reliability, and more
- Improved resources selector in context page (toggle between chunks/files, index status info, and more)
- UI enhancements


## Version 0.0.258 (December 1, 2025)
- Increase anthropic max tokens to 32k
- Impove behavior of reindexing (do not auto-add)

## Version 0.0.257 (November 30, 2025)
- Added Azure Data Explorer data source (thanks @licanhua)
- Improved BigQuery system prompt to consider special syntax guidelines when generating code

## Version 0.0.256 (November 29, 2025)
- Improved visualization features
- Enhanced dashboard creation workflow
- Suggestions now cover more user actions, such as corrections, querying the same tables, and sharing code
- Expanded instruction categories for system, dashboard, and visualizations
- UI improvements for agent trace, observations, and reduced visualization flicker
- Improved data source onboarding and test connections
- Added integration tests for LLMs and popular data sources

## Version 0.0.255 (November 27, 2025)
- Extended user token validity to one week, reducing the need for frequent logins
- Improved evaluation (Evals) features for more robust and insightful testing
- Added support for anonymous MySQL connections


## Version 0.0.254 (Noveber 25, 2025)
- Fix azure llm integration
- Improve mysql authentication 

## Version 0.0.253 (November 24, 2025)
- Gemini 3 Pro Preview added!

## Version 0.0.252 (November 22, 2025)
- Implemented tracking of LLM usage and associated costs in the console dashboard
- Enhanced metadata resource handling:
  - Remove objects no longer found during reindexing
  - Newly discovered objects are no longer auto-activated by default
- Introduced SQLite integration (for testing and development), and expanded test coverage for git repositories, metadata resources, and more
- Improved the process for deleting data sources
- Added bulk archive functionality for reports and revamped the main reports index page

## Version 0.0.251 (November 20, 2025)
- Data sources deletion

## Version 0.0.250 (November 19, 2025)
- Add context estimator when writing prompts

## Version 0.0.249 (November 19, 2025)
- Pinot get tables to use user:pass when creating the HTTP request

## Version 0.0.248 (November 18, 2025)
- Resolve flickering in the Reasoning section and enhance the reliability of data source deletion and modal overlays
- Improve stability and robustness of table auto-activation and deactivation

## Version 0.0.247 (November 17, 2025)
- Instruction labels added for more effective categorization and management
- Instructions can now be auto-enhanced with AI suggestions
- Message display now clearly distinguishes between user and agent responses
- Trace modal correctly navigates to the selected completion ID within the reports page

## Version 0.0.246 (November 16, 2025)
- Snowflake keypair auth
- Repair migrations

## Version 0.0.245 (November 16, 2025)
- Repair migrations

## Version 0.0.244 (November 15, 2025)
- Updating dependencies

## Version 0.0.243 (November 15, 2025)
- Fixing a couple of bugs and renaming release notes to CHANGELOG

## Version 0.0.242 (November 14, 2025)
- Enhanced markdown parser for better handling of complex formatting and edge cases
- Added support for Dataform projects and introduced SQLX file parsing, enriching contextual metadata for queries and models
- Integrated GPT-5.1 as an available LLM by default
- Improved metadata indexing service with additional guardrails for git repository management and error management
- Upgraded user interface for reports and tables

## Version 0.0.241 (November 14, 2025)
- Optimize datbase migrations to include report_type
- Wrap maintenance job with guardrails

## Version 0.0.240 (November 13, 2025)
- Introducing Evals! You can now create and run custom sets of tests on demand to assess system performance. Define your own test cases and assertions, such as:
  - User prompts triggering create_data on table1 and table2
  - Validating that specific data columns (e.g., a, b, c) are present
  - Using custom LLM Judge prompts to automatically determine pass/fail outcomes
- Added the ability to adjust the sample k size for schema tables and metadata resources
- Improved the data source pages for a faster, smoother experience, including enhanced loading indicators and improved item removal
- Unused steps are now auto-deleted after 14 days. You can restore them anytime by rerunning the code.

## Version 0.0.236 (November 13, 2025)
- Added sorting and filtering capabilities to the table selector
- Reduced logging verbosity in production environments
- Enforced strict limits on context section sizes

## Version 0.0.235 (November 12, 2025)
- Added ability to select and deselect items in table and metadata resource selectors
- Enhanced BigQuery integration to allow connections to multiple datasets
- Enforced organization-level uniqueness for data source and LLM provider names
- Allow service json for BigQuery required user auth mode

## Version 0.0.233 (November 11, 2025)
- Improved instructions visibility in prompts' context
- Introduced an "Analysis Panel" for admins when creating or approving instructions:
  - Impact Score Estimation: Evaluate how the new instruction relates to existing prompts and user questions
  - Related Instructions: Identify potential redundancy or conflicts with other instructions
  - Related Metadata Resources: Review if the instruction overlaps or conflicts with current enriched context (such as dbt, markdown, etc.)

## Version 0.0.232 (November 10, 2025)
- Introduced default small models: you can now designate a default "small" model for back-office operations such as evals, judge tasks, instruction generation, and more
- User feedback (thumbs up/down) is now attributed at the table level

## Version 0.0.231 (November 8, 2025)
- Enhanced the UI for agentic retrieval and search for greater clarity and usability
- Refined the agent head prompt to more effectively leverage and guide the use of search tools
- Improved the agent trace user interface for better readability and interaction

## Version 0.0.230 (November 6, 2025)
- Introduced a new create_data tool that is more robust, reliable and accurate data generation
- Enhanced code generation for more accurate and robust SQL and Python outputs
- Improved chart visualizations for clearer and more informative data presentation
- Added new data source integration support: Apache Pinot and Oracle DB
- Table browsing now displays detailed statistics, including usage frequency, scoring, and feedback metrics
- Launched the new `read_resources` tool for intelligent, on-demand searching across all metadata resources
- Added successful executed queries in the same tables for when agent is generating code


## Version 0.0.220 (November 4, 2025)
- Added BigQuery support for `maximum_bytes_billed` for cost guardrails and support for `use_query_cache`
- Improved main AI loop with additional observations from sub-agent create data (code, errors, etc)
- Improved UI for list of instrusctions modal - pagination, visibility, etc

## Version 0.0.219 (November 3, 2025)
- Improved table discovery and retrieval in main agent loop
- Introduced describe_tables tool for better data modeling, with light UI signaling
- Reduced the main agent's context footprint by 5x, significantly faster and leaner
- The create data sub-agent now receives a provided list of tables instead of inferring the data model itself

## Version 0.0.218 (November 1, 2025)
- Fixed issue where the data source form was not fully rendered in the onboarding screen
- Fixed issue where Claude outputs a Python code fence before the actual code

## Version 0.0.217 (November 1, 2025)
- Basic telemetry (configurable in bow-config)

## Version 0.0.215 (October 31, 2025)
- Support multi schema for Postgres client

## Version 0.0.214 (October 30, 2025)
- Support multi-db connection for ClickHouse

## Version 0.0.213 (October 28, 2025)
- Clickhouse fix
- Better rendering of booleans in connection form

## Version 0.0.212 (October 20, 2025)
- Integrate Mentions component and enhance prompt capabilities
- Implement mentions context integration in tools and agents
- Released: Catalog feature for efficient management and discovery of models, metrics, visualizations, and queries. Enables reusable components and enhances AI analyst intelligence
- Fix yarn cache issue in docker image

## Version 0.0.206 (October 19, 2025)
- Bug fix reloading tables in schema

## Version 0.0.205 (October 17, 2025)
- Added support for multiple schemas in Snowflake
- Added `MSSQL` driver into Dockerfile 

## Version 0.0.204 (October 16, 2025)
- Fixed permission issue in Docker when uploading files
- Fixed instructions not showing creator in instruction list

## Version 0.0.203 (October 12, 2025)
- Enhanced the chat interaction and conversation flow with the AI agent
  - Improved prompt capabilities by auto setting thinking levels
  - Enhanced message context with processed data and answer metadata for better LLM interactions
- Optimized CI/CD workflows by integrating GitHub Release automation

## Version 0.0.202 (October 8th, 2025)
- Added DuckDB support for object store files (aws, gcs, azure)
- Added Claude Sonnet 4.5 support

## Version 0.0.200 (September 27, 2025)
- Enhanced data source setup experience for new users
- Redesigned user interface for data source management
- Introduced "require user authentication" option for data sources
- Sample questions for data sources is now customizable
- Added to organizations ability to set judge, autogen instructions and code editing as enabled/disabled
- Added a bunch of AGENTS.md files throughout the repo for faster and better coding

## Version 0.0.199 (September 20, 2025)
- Redesigned application onboarding experience
- Implemented automatic instruction suggestions throughout the onboarding process
- Added support to Tableau as a data source
- Some general updates, bug fixes and new tests and sentry removal

## Version 0.0.198 (September 17, 2025)
- Adding login with OpenID Connect (Okta, etc)
- Updating Helm to allow oidc params and auth mode (hybrid, local or sso)
- Touch up to signin/signup screens
- Fix docker image to include client for openssh

## Version 0.0.197 (September 15, 2025)
- Introduced Tableau data source integration: TDS files can now be imported to enhance contextual information for data sources
- Deprecated AI Rules feature at the data source level, consolidating rule management into the centralized instruction system
- Added support for Google Gemini LLM
- Added verbosity to git integration
- Squashed bugs and improved overall usability


## Version 0.0.196 (September 14, 2025)
- Added inline code editor for queries with full execution capabilities: users can now edit query code, preview data results, visualize outputs, and save changes directly within the interface
- Added widget customization controls for labels, titles, and styling
- Rebuilt query/visualization engine for improved scalability
- Improved dashboard layout, reactivness and synchronization to other visualizations
- Enhanced backend architecture and data modeling to support query versioning and multi-visualization relations
- Added ability to test LLM connection before saving as a new provider

## Version 0.0.195 (September 10, 2025)
- Introducing Deep Analysis: Users can now change from Chat mode to Deep Analytics for doing a more comprehensive open ended analytics research to identify root cause, anomalies, opportunities, and more!
- New Prompt box for both home/report page, including customizing LLM per prompt
- Roles with console/monitoring access can now view the full agent loop trace inside the report chat

## Version 0.0.194 (September 9, 2025)
- **Enhanced Dashboards**
  - Improved dashboard creation, allowing more control on styles and the new dashboards look amazing!
  - User can now select themes (default, retro, hacker, or research)
- Added the answer question tool, allowing agent to search across schema, resources, and other pieces context to come up with the answer
- Improvements to Slack bot integration
- Enhancements around: cron visibility, excel files, and sharing

## Version 0.0.193 (September 6, 2025)
- Introduced automatic instruction suggestion system to enhance AI decision-making and performance. The system generates suggestions triggered by:
  - User clarifications regarding terms, facts, or metrics
  - AI successfully resolving data generation code after encountering multiple failures
- All generated suggestions are stored globally and require administrative review and approval before implementation
- Improved main AI agent planner prompt
- Redesigned and expanded the navigation menu, elevating monitoring and instructions to prominent first-class menu items
- Bug fixes and enhancements

## Version 0.0.192
- Fixed file upload functionality within Docker container environment
- Resolved issues with report rerunning capabilities
- Reduced database logging output to only display warnings and errors

## Version 0.0.190 (August 31, 2025)
- Launched Agent 2.0, a comprehensive redesign of the backend agentic architecture
  - Implements ReAct methodology with single-tool execution per planning cycle
  - Enhanced tool registry featuring comprehensive tracking and governance capabilities
  - Added clarify tool for detecting user queries with undefined metrics/measures or ambiguous requirements
  - Improved error handling, tool schema validation, and enhanced reliability throughout agent execution
  - Comprehensive tracking system for agent executions, tool usage, and AI decision-making processes
- Released Context Management 1.0, providing robust and reliable context tracking for both warm and cold AI interactions
  - Complete monitoring of context utilization patterns
  - Streamlined interface for context construction and management during agent operations
- Enhanced compatibility with LLMs that generate prefix/postfix formatting symbols such as json/``` markers
- Redesigned streaming architecture with server-sent events (SSE) implementation for real-time user prompt processing
- Enhanced admin interface for monitoring agent execution flows and tracking user request patterns
- Introduced new analytics visualization in console dashboard displaying metrics for data request creation (user-initiated), AI clarification requests, and additional operational insights
- Added automated testing for the system
- As this change was signifcant, old reports (in version prior 0.0.190) will be set as read-only.
- Introduced customizable branding and AI identity features, allowing organizations to upload their own logos, remove Bow attribution, and personalize their AI assistant's identity


## Version 0.0.189 (August 25, 2025)
- Enhanced table usage analytics with comprehensive success/failure tracking, performance scoring, and intelligent usage pattern recognition
- Implemented automated TableStats model to capture query performance metrics, execution outcomes, and user satisfaction data in real-time
- Advanced code generation now leverages historical success patterns and proven code snippets, significantly improving accuracy and reliability
- Upgraded AI planner with feedback-driven decision algorithms that incorporate table performance scores and usage data for continuous self-improvement
- Added weighted performance/feedback scoring based on user role (admin vs. rest)
- Added tests covering llm providers, azure backend, and console metrics

## Version 0.0.188 (August 23, 2025)
- Enhanced streaming reliability for data models and query results in chat interface
- Strengthened completion termination handling with comprehensive SIGKILL support across all agent lifecycle stages
- Introduced custom base URL configuration for OpenAI provider deployments
- Resolved console metrics and usage data functionality issues
- Corrected admin permissions to allow deletion (not just archival/rejection) of suggested instructions


## Version 0.0.186 (August 19, 2025)
- Enhanced instructions functionality with support for referencing dbt models, tables and other metadata resources
- Updated data source section with improved views of dbt and other metadata resources
- Fixed various bugs and enhanced overall usability

## Version 0.0.181 (August 10, 2025)
- Added data source visibility controls - admins can now set data sources as public or private within organizations and manage granular access permissions through user memberships
- Improved interface and user experience with differentiated views and controls for administrators versus regular users in the data source management area
- Integrated OpenAI's latest GPT-5 language model into the platform
- Updated Docker image to use Ubuntu base with latest security patches
- Updated Python package dependencies to latest stable versions
- Implemented container vulnerability scanning using Trivy in CI/CD pipeline

## Version 0.0.180 (August 6, 2025)
- Enhanced security by updating Dockerfile with latest vulnerability patches
- Integrated Claude 4 Sonnet and Opus language models
- Implemented full support for Vertica database connectivity and querying
- Added capability to incorporate markdown files from git repositories to enhance data sources with contextual information
- Added support for Azure OpenAI and custom model endpoints
- Added support for AWS Redshift database connectivity

## Version 0.0.177 (July 30, 2025)
- Added comprehensive admin console with three main sections: Explore, Diagnose, and Instructions management
- **Explore**: Organization analytics dashboard with real-time metrics, activity charts, performance tracking, table usage analysis, table joins heatmap, failed queries overview, recent instructions, top users, and prompt type analytics
- **Diagnose**: Advanced troubleshooting interface featuring failed query tracking, negative feedback analysis, instructions effectiveness scoring, detailed trace debugging, and issue categorization with actionable insights
- **Instructions**: Centralized instruction management system with search and filtering capabilities, add/edit functionality, data source associations, and user permission controls
- Added LLM Judge system for automated quality assessment - scores instruction effectiveness and context relevance on a 1-5 scale, evaluates AI response quality against user intent, and provides detailed reasoning for continuous system improvement

## Version 0.0.176 (July 26, 2025)
- Added ability to provide detailed feedback messages when submitting negative feedback on AI completions
- Improved reports main page UI

## Version 0.0.175 (July 26, 2025)
- Added ability for users to suggest new instructions and view published instructions
- Added workflow for admins and privileged users to review, approve, or reject suggested instructions
- Enhanced instruction management with data source associations - instructions can now be set globally or scoped to specific data sources
- Added visibility controls allowing admins to hide certain instructions from unprivileged users

## Version 0.0.174 (July 23rd, 2025)
- Filters and pagination for reports
- Reports are now invisible for other users when not published

## Version 0.0.172 (July 17th, 2025)
- Slack integration! Now admins can integrate their Slack organization account and have users converse with bow via slack. Includes user-level authorization, formatting, charts, and tables
- LookML support for git integration indexing
- Download steps data as CSV is now available in UI
- Added *Instructions*: add custom rules and instructions for LLM calls

## Version 0.0.166 (July 13th, 2025)
- Resolved membership invitation handling for closed deployments with OAuth authentication
- Corrected query count calculation in admin dashboard metrics

## Version 0.0.165 (July 7th, 2025)
- Added admin dashboard with usage analytics, query history tracking, and LLM feedback collection
- Implemented secure password recovery workflow with email verification
- Enhanced Kubernetes deployment configuration with expanded Helm chart coverage and options

## Version 0.0.164 (April 24th, 2025)

- Refactored dashboard visualization capabilities:
  - Improved chart rendering performance and responsiveness
  - Enhanced data handling for large datasets
  - Added better error handling and validation
  - Streamlined chart configuration options
- Fixed candlestick chart bug where single stock data was not properly displayed when no ticker field was present
- Added "File" top level navigation item. You can now see all files uploaded in the org
- You can now mention files outside of the report
- Support older version of Excel (97-03)

## Version 0.0.163 (April 21, 2025)

- Added new charts: area, map, treemap, heatmap, candletick, and more
- Better experience for charts to handle zoom, resize and overall better rendering

## Version 0.0.162 (April 16, 2025)

- Added ability to stop AI generation mid-completion with a graceful shutdown option
- Enhanced application startup reliability with automatic database connection retries
- Moved configuration management to server-side, enabling centralized client configuration
- Introduced support for deploying the application on Kubernetes clusters using Helm charts

## Version 0.0.161 (April 14, 2025)

- Added support to OpenAI GPT-4.1 model series

## Version 0.0.160 (April 12, 2025)

- Enhanced AI reasoning with ReAct framework and advanced planning capabilities
- Added upvote/downvote system for users to provide feedback on AI responses
- Added detailed reasoning explanations for AI responses in both UI and backend
- Improved Completion API to support synchronous jobs and return multiple completions
- Added OpenAPI support for global authentication and organization ID handling
- Enhanced organization settings and key management system
- Added visual source tracing in data modeling interface


## Version 0.0.155 (March 30, 2025)

- Added code validation for generated code
- Added safeguards for planner and coder agents
- Enabled code review for user's own code
- Fixed memory bug
- Added reasoning for planner agent
- Added data preview for LLM to achieve ReAct like flow with code generation
- Added organization settings to control AI features (specific agent skills) and additional settings (LLM viewing data, etc)
- Added df summary for tables
- Refactored code execution to be more robust and handle edge cases better

## Version 0.0.154 (March 24, 2025)

- Added advanced logging infrastructure
- Added e2e tests infrastructure and created first e2e test for user onboarding
- Improved ci/cd to run tests before building image

## Version 0.0.153 (March 22, 2025)

- Added support with dbt (via git repo) models and metrics
- Added context building for dbt models
- Added token usage to plan
- Added x-ray view for completions for admin roles

## Version 0.0.152 (March 16, 2025)

- Added AWS Athena integration
- Fixed bug when generating data source items
- Fixed bug when deleting data sources

## Version 0.0.151 (February 25, 2025)

- Added Claude 3.7 Sonnet to LLM models
- Added sync provider with latest models

## Version 0.0.15 (February 24, 2025)

- Added active toggle to data source tables to hide from context
- Fixed bug when generating data source items
- Add top bar to index page when no LLMs are available

## Version 0.0.14 (January 3, 2025)

- Added basic self-hosting support
- Added printing in code gen for better healing
- Improve answering agent and planner agent
- Replaced highcharts with ECharts
- Added intercom
- Various fixes and improvements

## Version 0.0.13 (December 26, 2024)

- Added prompt guidelines 
- Fixed modify, creation of widgets
- Fix proxy in nuxt/fastapi 
- Improved agents: dashboard, data model, chart, and prompt
- Added email validation for signups
- Dockerized the application
- Kubernetesized the application

## Version 0.0.12 (December 13, 2024)

- Added functionality to rerun dashboard steps, including cron support with configurable intervals
- Enabled automated LLM-generated summaries, starters, and reports for connected data sources
- Integrated Google Sign-In for seamless user authentication
- Added support for nginx reverse proxy
- Redesigned the home page for improved usability
- Added `bow-llm`, an abstracted LLM provider to set as the default
- Enhanced error handling with interactive toasts for better feedback
- Improved agent capabilities for code generation with better data source context and refined JSON parsing
- Enabled dynamic modifications to agent plans
- Resolved the "thinking bug"
- Made LLM provider presets uneditable
- Fixed WebSocket functionality in production
- Completed end-to-end tests for completions and data sources

## Version 0.0.11 (December 5, 2024)

- Completed integrations for Presto, Salesforce, and Google Analytics
- Added support to CRUD model providers and LLM models
- Added Claude AI model support
- Implemented data source credential security
- Enhanced agent capabilities:
  - Added clarification questions feature
  - Fixed dashboard layout generation
  - Fixed chart parameter rendering
  - Improved data model modifications
- UI Improvements:
  - Fixed report title updates
  - Resolved copy-paste styling issues in prompt box
  - Completed memberships interface
  - Enhanced mention component
- Infrastructure updates:
  - Added configuration file support
  - Removed Excel special routes
  - Cleaned up Nuxt from git repository
  - Fixed default menu data source association
  - Removed unique organization name requirement

## Version 0.0.10 (November 28, 2024)

- Edge left menu is now scrollable.  
- Fixed logo scaling issue in Edge browser.  
- Added schema browser for data sources.  
- Enabled manual test connection for data sources.  
- Converted data source list in prompts to a dictionary for better position handling.  
- Added Markdown support for completions in both agent and UI.  
- MySQL, BigQuery, Snowflake, MariaDB, and ClickHouse integrations are complete.  
- Initial scaffold for service type data sources
- Fixed `_build_schemas_context` to run only once during agent initialization.  
- Improved data source error messages.  
- Only active data sources are now displayed.  
- Data sources failing test connection are automatically set to inactive.  
- Introduced a service-type architecture for data source handling in code generation.  
- Permissions module completed.  
- Public dashboard completed.
