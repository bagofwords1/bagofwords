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

> **Status: bugs 1 and 2 are FIXED and re-verified** (see "The fix" at the
> bottom for the changes, the regression suites, and the live re-runs against
> both the Fabric tenant and a 5 000-table Postgres).

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

**Re-verified deterministically (2026-07-25 18:20)** through the exact HTTP
endpoint the UI Reload button calls, then state was restored:

```
BEFORE: connection_tables = [dbo.sales, dbo.finance]; agent tables both active
GET /api/data_sources/{fabric_ds}/refresh_schema  as demo2  -> HTTP 200
        log: "Deleted 1 ConnectionTable records for tables no longer in database"
AFTER : connection_tables = [dbo.sales];  agent tables = [dbo.sales]
        demo1 (AllFabric) GET full_schema -> total_tables=1 [dbo.sales]  (was 2)
```

demo2's Reload physically deleted `dbo.finance` from the shared catalog, and
demo1/admin lost it too — reproduces every time.

**Scope — not an OBO-only bug.** `refresh_schema` sets `index_user =
current_user` as the DEFAULT for every connection type, and
`resolve_credentials` then decides what that means. `system_only` returns the
service credentials and is unaffected; **every `user_required` flavor** runs the
canonical crawl as the caller:

| flavor | identity used | affected |
|---|---|---|
| `system_only` | connection service creds | no |
| `user_required` + OAuth/OBO (`"oauth" in allowed_user_auth_modes`) | user's delegated token (Power BI, Fabric, SharePoint, OneDrive, Outlook, ServiceNow) | **yes** |
| `user_required` + per-user user/pass (the "legacy path", `connection_service.py`) | the user's own DB login — Snowflake, Postgres, MSSQL, SAP HANA, Databricks… any variant with `scopes=["system","user"]` | **yes** |
| `user_required` + Kerberos SSO (`_kerberos_delegated_credentials`) | impersonated AD principal — needs **no** stored credential row, so it hits on every user's reload | **yes** |

Safe sub-cases inside `user_required`: caller has no per-user creds
(`index_user=None`), admin/owner with `query_identity="service_account"`,
credential-less connectors, and the owner/admin-with-no-row fallback. So a plain
SQL warehouse with per-user logins and table GRANTs is exactly as exposed as
Fabric — the tenant here just made it easy to see.

**Collateral damage: per-user overlays are left dangling.** Deleting the
canonical row also destroys the `DataSourceTable` it feeds, and every *other*
user's overlay row (`user_data_source_tables.data_source_table_id`) is left
pointing at an id that no longer exists (on Postgres the FK sets it NULL; under
SQLite it simply dangles). Reads scope by
`DataSourceTable.id IN (overlay ids)`, so those users stay blind to the table
**even after an admin restores the catalog** — and re-syncing never healed it,
because the re-link only fired when the column was NULL. Observed live: after
the pre-fix run, `demo1` and the admin both had `dbo.finance` in their overlay
marked accessible, yet `full_schema` returned 1 table for them.

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

Fix direction: keying the cache on the user id fixes the disclosure, but
prefer the resolved **`effective_auth`** class (+ an overlay-table-id
fingerprint) so all `system`/admin callers still collapse to one shared entry
and keep the cache's hit rate; alternatively bypass `_SCHEMA_CACHE` whenever any
attached connection is `user_required`.

**Related latent bug (same file, same theme):** `context_hub.py:315`
constructs `InstructionContextBuilder(...)` **without `current_user`**, so that
builder's own per-user table-accessibility filter
(`instruction_context_builder.py:1307-1367`, reads `user_data_source_tables`)
never runs in the agent path — an instruction referencing a denied table is not
filtered for a restricted user. Identity-scoping was added to the builders but
the `context_hub` wiring/caching around them was never made identity-aware.

### 2b. ~~A report started from one agent's page attaches ALL accessible agents~~ — RETRACTED (harness error)

**This was my mistake, not a product bug.** The agent page has *two* buttons
matching "New report": the global one in the left sidebar (x≈12) and the agent
panel's own (x≈1275). The driver script used `.first()` and hit the **sidebar**
one, which by design creates an unscoped report that defaults to every agent.

Verified afterwards: the agent panel's button posts
`{"title":"New report","data_sources":["<that agent>"]}`, and a report created
that way stays scoped to the single agent — through page load *and* after
sending a prompt (`report_data_source_association` still holds exactly one row).
Report scoping works correctly.

### 3. UX — creating a connection inside the agent wizard silently adds a second connection — FIXED

Corrected mechanism (my first description was wrong): `/agents/new` does **not**
pre-select every connection — a fresh load shows "Select connections". What it
does is **auto-select the single pre-existing connection** when the org has
exactly one (`pages/agents/new/index.vue`, `onMounted`), and then
`handleNewConnectionCreated` **appends** the connection you create from the
modal. Creating "Fabric Agent" right after "PBI Agent" therefore produced a
2-connection agent whose Select Tables step showed 8 tables across both
connectors, and Power BI had to be detached afterwards.

Fixed: creating a connection from this screen now **replaces** the selection
with that connection — building the agent on the thing you just created is the
only sensible reading of the action, and the auto-select convenience still
applies when you don't create one.

### 4. Cosmetics / small frictions

- **"1 tables"** — the agent header count didn't singularize (Fabric agent as
  demo2). **Fixed** for English via vue-i18n plural forms
  (`"{n} table | {n} tables"`, same for tools/files/instructions) plus a numeric
  plural choice at the call site that tolerates the "–" placeholder shown while
  counts load. Locales whose message has no `|` render exactly as before, so no
  translation was invented for the other nine languages.
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

## Graph connectors — SharePoint / OneDrive / Outlook Mail (user_required)

Same tenant, same three users, same `user_required` + `oauth` policy. SharePoint
points at `https://bow14.sharepoint.com/sites/employees`; all three agents were
made public.

### The gate: what the tenant actually permits

Probing the real token endpoint per user and scope set first saved a lot of
guessing — the multi-user story here is bounded by tenant licensing/consent, not
by the product:

| | SharePoint site | OneDrive | Mailbox |
|---|---|---|---|
| **yochay** (admin) | ✅ 10 files | ✅ 13 items | ✅ messages |
| **demo1** | ❌ `403 accessDenied` (not a site member; `Sites.Read.All` not consented) | ❌ `403 provisioningNotAllowed` — **no OneDrive** (unlicensed) | ❌ `404 MailboxNotEnabledForRESTAPI` — **no mailbox** |
| **demo2** | ❌ same | ❌ same | ❌ same |

`Files.Read.All`, `Group.Read.All` and `User.Read` are consented for everyone;
`Sites.Read.All` and `Mail.Read` are consented **only for yochay**. Full
multi-user coverage on these three connectors therefore needs tenant-side
changes (assign M365 licences to demo1/demo2, add them to the `employees` site,
and grant tenant-wide admin consent) — deliberately not done here, since that
is production identity/billing configuration.

So the connectors were exercised **end-to-end as yochay**, and the demo users
became the **isolation** test: they must see nothing and fail cleanly.

### Per-user sign-in + catalog

| user | SharePoint | OneDrive | Outlook |
|---|---|---|---|
| yochay | ✅ token 1.0s · verify 3.8s · overlay 7.9s → **10 files** | ✅ token 1.2s · verify 3.7s · overlay 11.9s → **14 files** | ✅ `Connected as YochayEttun@…` |
| demo1 | ❌ 403 at `/sites/{host}:{path}` | ❌ 403 `provisioningNotAllowed` | ⚠️ **reported success** (see fix 1) |
| demo2 | ❌ 403 | ❌ 403 | ⚠️ **reported success** (see fix 1) |

`user_data_source_tables` holds rows for **yochay only** (10 SharePoint + 14
OneDrive); demo1/demo2 have none, and `full_schema` returns 0 for them on all
three agents. No cross-user leakage.

### LLM tool matrix (real Anthropic Haiku, through the chat UI)

| # | agent · user | file type / tool | result | time |
|---|---|---|---|--:|
| 1 | SharePoint · yochay | `list_files` | ✅ grouped all files by type | 34s |
| 2 | SharePoint · yochay | **CSV** `read_file` | ✅ columns + totals from `2017_Expense_Data.csv` (5,786.05, and it flagged the subtotal row) | 46s |
| 3 | SharePoint · yochay | **XLSX** `read_file` | ✅ real P&L: revenue $9,325M→$9,931M (2014-18), expense breakdown | 19s |
| 4 | SharePoint · yochay | **PDF** `read_file` | ✅ accurate 2-sentence summary of `BOW Customer Deck.pdf` | 33s |
| 5 | SharePoint · yochay | **PDF (Hebrew)** `read_file` | ✅ correct Hebrew summary of the NDA, report titled in Hebrew | 34s |
| 6 | SharePoint · yochay | **PNG** `search_files` | ❌ **bug** — wildcard query 400'd (see fix 2) | 44s |
| 7 | OneDrive · yochay | **XLSX** `list_files`+`read_file` | ✅ `Book 1.xlsx`: monthly sales 2019-11→2022-05, 32 rows | 34s |
| 8 | Outlook · yochay | `list_emails` + `read_email` | ✅ 25 messages; 3 most recent with correct dates | 24s |
| 9 | SharePoint · **demo1** | isolation | ✅ `list_files ✗` → explains the 403, **zero files leaked** | 34s |
| 10 | Outlook · **demo2** | isolation | ✅ `list_emails ✗` → explains the mailbox error, **zero mail leaked** | 19s |

DOCX was covered indirectly (listed and offered) — the dedicated DOCX turn is
the one UI case that hit a dev-server hiccup; CSV/XLSX/PDF/PNG/mail all ran.

### Bugs found — both fixed

**Fix 1 — Outlook "Test Connection" was a false positive.**
`GraphMailClient.test_connection` only called `/me`, which proves the token maps
to a directory user and nothing more. A user with no Exchange licence got a
green **"Connected as demo1@bow14.onmicrosoft.com"**, and then every mail tool
failed at runtime with `MailboxNotEnabledForRESTAPI`. It now probes
`/me/messages?$top=1` too and returns an actionable message. Verified live:

```
yochay (mailbox)     success=True   Connected as YochayEttun@bow14.onmicrosoft.com
demo1  (no mailbox)  success=False  Signed in as demo1@…, but this account has no
                                    Exchange mailbox (… missing a Microsoft 365 mail
                                    licence). Assign a mailbox to use this connection.
```

**Fix 2 — any wildcard search against Graph returned HTTP 400.**
`search_files` interpolates the query into the URL *path*
(`/drives/{id}/root/search(q='…')`), and ASP.NET rejects `*`, `?`, `%`, `&`, `#`
there — even percent-encoded — with
`400 invalidRequest "A potentially dangerous Request.Path value was detected"`.
The model reached for `*.png` **unprompted** and the call failed on *both*
OneDrive and SharePoint, so the agent concluded the PNG did not exist (it did).
Graph drive search is a substring match with no wildcard support, so the term is
now sanitised before encoding. Verified live against the real drive:

```
'*.png'  -> '.png'   -> 1 hit  (sec.gov_…form10-q.htm.png)      # was HTTP 400
'*.xlsx' -> '.xlsx'  -> 4 hits (customers-xl, movies, CFI-P&L…) # was HTTP 400
'2017'   -> '2017'   -> 2 hits (unchanged behaviour)
```

Regression suites: `tests/unit/test_graph_mail_test_connection.py` (4) and
`tests/unit/test_graph_drive_search_term.py` (18).

### Smaller findings — all fixed

- **Per-user file connectors listed their documents under Tables.** The
  `exclude_file_source_types` filter recognises a file row by following
  `connection_table_id` to the connection type, and deliberately keeps
  *unlinked* rows (legacy name-keyed tables). Per-user catalogs
  (OneDrive/Outlook/Drive) have no shared `ConnectionTable`, so **every** row is
  unlinked and slipped through — OneDrive's 14 documents appeared under
  **Tables** while SharePoint's linked rows were correctly hidden. Now, when
  every connection on the agent is a file source, unlinked rows are excluded
  too; mixed agents keep the legacy allowance.
  *(The first version of this fix leaned on `NULL NOT IN (subquery)`, which
  silently flips behaviour when the subquery is empty — the new test
  `test_per_user_file_agent_shows_no_tables` caught it, and the condition is now
  explicit.)* Live after the fix: SharePoint 0 · OneDrive 0 · Fabric 2 · PBI 6.
- **`list_emails` exposed the received date as `modified_at`** with no
  description, so the model didn't recognise it and issued an extra `read_email`
  per message just to get dates. The field now documents that it carries the
  **received** date for email items.
- **`POST/PUT /connections` didn't echo `allowed_user_auth_modes`** — the list
  endpoint returned it but both write endpoints hand-build `ConnectionSchema`
  and omitted the field, so API-driven setup looked like it had silently failed.
  Both now echo it (verified: POST and PUT return `['oauth']`).
- **Entra groups that Graph can't resolve were named by raw GUID.** A directory
  role or unreadable group landed in the admin's group list as
  `85f43b45-99ae-…` with no hint of what it was; unresolved ids now render as
  `Unresolved directory group (85f43b45…)`.
- **OTel logged a "Failed to detach context" ERROR with a traceback on every
  streamed completion** (the async generator is closed on a different task than
  the one that attached the span context). It is pure bookkeeping noise — the
  span still ends — but it reads as a real failure in the log, so exactly that
  record is now filtered.
- SharePoint `search_files` also hit one transient `Connection reset by peer`
  from the sandbox egress proxy; the same call succeeds directly, and the agent
  recovered on its own via `list_files` + `read_file`. Environmental, not fixed.

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

---

## The fix

The contract the code now implements:

> The canonical `ConnectionTable` catalog is the **union of every identity's
> view**. Only an **org-identity** crawl is authoritative over it. What each
> user *sees* comes from their own overlay. Usage metrics are org-wide.

**1. `resolve_credentials` reports which identity it used**
(`connection_service.py`). It records `"system"` vs `"user"` in
`self.last_credential_identity` on every resolve path — delegated token,
per-user login, Kerberos impersonation, service-account override. No caller has
to re-derive the rule (which is what made this easy to get wrong).

**2. `refresh_schema` treats a per-user crawl as a per-user view**

- a **per-user** crawl is **create-only**: new tables are unioned into the
  shared catalog, existing rows are left exactly as the org identity last saw
  them, and **nothing is pruned**;
- an **org-identity** crawl is authoritative and prunes — but keeps any row
  still visible to at least one user (`_user_visible_table_names`, which reads
  *both* overlays: connection-level `user_connection_tables` and
  data-source-level `user_data_source_tables`, since different connectors
  populate different ones). A table dropped upstream disappears from every
  identity's view, so it still gets cleaned up on the next org refresh.

**3. Overlay links self-heal** (`data_source_service.py`). The overlay→canonical
re-link now fires whenever the stored `data_source_table_id` differs from the
current canonical row for that name, not only when it is NULL — so users
damaged by the old behavior recover on their next reload instead of staying
permanently blind to a table they can query.

**4. Identity-aware context caches** (`context_hub.py`). `_SCHEMA_CACHE` and
`_INSTRUCTIONS_CACHE` keys gain `_schema_identity_key()`: `"system"` when every
attached connection is `system_only` (so all callers keep sharing one entry and
the cache still pays for itself), `"user:<id>"` as soon as any connection is
`user_required` — and it fails safe to per-user if the connections can't be
inspected. `InstructionContextBuilder` is also finally given `current_user`, so
its per-user table-accessibility filter stops being dead code in the agent path.

### Regression suites

```
tests/e2e/test_shared_catalog_union_per_user.py    4 passed
  [S1] crawl identity='user';   canonical after restricted reload=['finance', 'sales']
  [S2] crawl identity='user';   canonical after user reload=['table1','table2','table3']
  [prune] crawl identity='system'; canonical after org reload=['finance','sales']  (ghost pruned)
  [S3] table_stats before == after == seeded
tests/unit/test_context_hub_identity_cache_key.py  5 passed
tests/e2e/test_obo_admin_catalog_before_signin.py
tests/e2e/test_fabric_second_admin_overlay_repro.py
tests/e2e/test_connection.py                      10 passed (no regressions)
```

### Live re-verification — Fabric (same tenant, same users)

```
demo2 (MinimalFabric, DENY finance) → GET /refresh_schema        HTTP 200
  log: "crawled with the CALLER's own credentials — … nothing is pruned"
       "Created 0, updated 0, left-untouched 1 ConnectionTable records"
  canonical: ['dbo.sales', 'dbo.finance']   ← was ['dbo.sales'] before the fix
admin  → GET /refresh_schema                                      HTTP 200
  canonical: ['dbo.sales', 'dbo.finance']   (stable)

per-user views (GET /full_schema):  admin 2 · demo1 2 · demo2 1 (sales only)
```

Cross-user context isolation, driven through the real cached path
(`ContextHub.prime_static`, demo1 priming the cache first — the exact order that
leaked):

```
[demo1] schema section tables=['dbo.finance','dbo.sales']  identity_key=user:b05216bf…
[demo2] schema section tables=['dbo.sales']                identity_key=user:6de3512f…
[demo1] schema section tables=['dbo.finance','dbo.sales']  (cache hit, still correct)
[cache] distinct keys held=2   → demo2 never sees dbo.finance
```

And at the report level, re-running the same three chat-UI turns: demo2's
`context_snapshots` for the finance question now contain **`dbo.sales` only**
(`finance_in_ctx=False` on both the `initial` and `final` snapshots) — before
the fix the same snapshots carried `dbo.finance` with its column count. demo1's
Power BI turn still answers correctly from live DAX ("40 customers", 23 s), so
the identity-scoped cache costs nothing functionally.

### Live re-verification — Postgres, 5 000 tables, real per-user DB logins

The non-OAuth `user_required` flavor, at scale: one Postgres with 5 000 tables
and six login roles, driven through the real product path
(`refresh_data_source_schema` → `refresh_schema` → overlay sync →
`get_data_source_schema_paginated`). Fixture: `svc_s1` 5 000 · `u2_s1` 2 000 ·
`u3_s1` 1 500 (overlapping) · `svc_s2` **1** · `u2_s2` 3 000 · `u3_s2` 2 500.

| scenario | step | canonical (`connection_tables`) | user2 sees | user3 sees | admin sees | time (before → after perf fix) |
|---|---|--:|--:|--:|--:|--:|
| **S1** org sees all, users see subsets | seed as org identity | **5 000** | — | — | 5 000 | 2.9 → **2.2 s** |
| | user2 (2 000 grants) clicks Reload | **5 000** *(intact)* | 2 000 | — | — | 8.8 → **4.2 s** |
| | user3 (1 500 grants) clicks Reload | **5 000** *(intact)* | 2 000 | 1 500 | 5 000 | 7.5 → **3.9 s** |
| **S2** org sees 1, users see more | seed as org identity | **1** | — | — | — | 0.2 s |
| | user2 (3 000 grants) clicks Reload | **3 000** *(union grew)* | 3 000 | — | — | 13.3 → **4.8 s** |
| | user3 (2 500 grants, offset) clicks Reload | **5 000** *(union complete)* | 3 000 | 2 500 | — | 12.6 → **4.9 s** |
| | org identity reloads (still sees only 1) | **5 000** *(union survives)* | 3 000 | 2 500 | — | 3.4 → **4.2 s** |
| **S3** metrics org-wide | seed `table_stats`, reload as user3 then as org | unchanged | — | — | — | — |
| | `usage_count` per table | `t_0001:100 · t_0002:101 · t_0003:102` — identical before and after; `table_stats` has **no `user_id` column** (keyed by org + data source + table fqn) | | | | |

Every assertion the requirements asked for holds:

- **each identity sees exactly its own subset** — 2 000 / 1 500 / 5 000 in S1,
  3 000 / 2 500 in S2, with the overlay row counts matching the DB GRANTs
  exactly (`overlay_u2=2000`, `overlay_u3=1500`, `overlay_u2=3000`,
  `overlay_u3=2500`);
- **the shared catalog is the union** — S2 grows 1 → 3 000 → 5 000 as users
  connect, and an org-identity reload that can still only see 1 table does not
  collapse it;
- **usage metrics stay org-wide** — untouched by reloads from any identity, and
  structurally incapable of being per-user (no user dimension on `TableStats`).

Before the fix, S1's first user reload would have cut the canonical catalog from
5 000 to 2 000 (and the second to 1 500), and S2 could never exceed 1 table.

### Making the reload fast (N+1 removal)

The 5k fixture made the cost obvious, so it was profiled and fixed rather than
left as a note. Instrumenting the SQLAlchemy engine for one user reload of a
2 000-table view:

```
before:  WALL 6.6s | SQL statements 2064 | in-SQL 1.6s
           2000 × "SELECT user_data_source_columns … WHERE user_data_source_table_id = ?"  (1.33s)
after:   WALL 3.7s | SQL statements   68 | in-SQL 0.3s
```

Three changes in `get_user_data_source_schema`:

1. **Batch-load the column overlay.** It was queried once per table inside the
   upsert loop — 2 000 of the 2 064 statements. Now loaded in one chunked
   `IN (…)` pass into a `{table_id: {col_name: row}}` map.
2. **Client-side ids instead of per-row `flush()`.** New overlay rows (and
   user-contributed canonical rows) needed their PK to attach children, so each
   one paid a `flush()` round trip — thousands on a large catalog. `BaseSchema`
   ids are UUIDs generated in Python anyway, so they're now assigned at
   construction and the flushes disappear; SQLAlchemy batches the inserts into a
   handful of executemany statements.
3. **Reuse the same map for the revoke cascade** instead of re-querying columns
   per revoked table.

End-to-end effect on the matrix above: **user reload 8.8 → 4.2 s** (warm) and
**13.3 → 4.8 s** (cold, 3 000 new tables + 9 000 column rows), with identical
results in every scenario (all 19 regression tests still pass).

What's left, in order of remaining cost — none of it round-trip-bound any more,
it's ORM object churn:

- `sync_domain_tables_from_connection` ≈ 1.3 s: it materializes the full
  `DataSourceTable` set several times per sync, including **two identical
  queries** for "rows linked to this connection" (`data_source_service.py`
  ~4693 and ~4736). Deduping those and reusing one snapshot is the next easy win.
- overlay sync ≈ 1.5 s of Python building ~8 000 ORM objects. Core
  `insert()/update()` with mappings (bypassing the unit of work) would cut most
  of it.
- Bigger structural win: skip the sync entirely when nothing changed — the
  canonical refresh already knows it created/updated/pruned nothing, and a
  digest of the user's visible table+column set would let a repeat reload
  short-circuit to a no-op.
- The live source introspection itself is not the problem: 2 000 Postgres tables
  came back in **0.3 s**.
