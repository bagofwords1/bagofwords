# Feedback Loop — Entra `sso_only` re-run: OBO agents, MCP identity, Graph connectors, 5 000-table Postgres

Second full pass over the enterprise simulation from
[`entra-sso-fabric-powerbi-obo-e2e.md`](entra-sso-fabric-powerbi-obo-e2e.md),
run **2026-07-26** against the same real `bow14.onmicrosoft.com` tenant on a
fresh sandbox from `main` (`bf29e20`). Goals: confirm the previous loop's fixes
still hold, and push into the areas it left thin — MCP identity forwarding per
user, the three Graph connectors, and a **5 000-table Postgres** with real
per-user DB logins.

Everything user-facing was driven through the **UI with Playwright**; SQL/HTTP
were used only to seed fixtures and to verify DB state.

## Environment

| | |
|---|---|
| Auth | `auth.mode: sso_only`, one `entra` OIDC provider, `sync_groups` + `resolve_group_names` |
| License | Enterprise from `BOW_LICENSE_KEY` → `is_enterprise: true` |
| LLM | Anthropic **Claude 4.5 Haiku** only, configured through `/settings/models`, live test 200 |
| Users | `yochayettun` (admin, first SSO user → auto-org), `demo1` (**AllFabric**), `demo2` (**MinimalFabric**) |
| Connections | Power BI, Microsoft Fabric (`demo_db`), SharePoint, OneDrive, Outlook Mail, Echo MCP, PG 5K — all `user_required`+`oauth` except MCP (`system_only`) and PG 5K (`user_required` + per-user DB login) |

### Sandbox constraints (unchanged, both re-verified this run — neither is a product bug)

1. **Headless Chromium cannot reach `login.microsoftonline.com`** — the egress
   proxy resets Chromium's TLS handshake (`net::ERR_CONNECTION_RESET`) while
   curl/httpx through the same proxy succeed. So the hosted Microsoft login page
   can't render.
   *Workaround this run is tighter than last time:* an out-of-repo,
   env-guarded shim (`BOW_SANDBOX_ROPC=1`) intercepts **only** the
   authorization-code token exchange, and only when the code is a synthetic
   `ropc:<email>` marker, replacing it with a **real ROPC grant against the real
   tenant**. Everything else is untouched product code — the browser really
   clicks "Sign in with entra", the backend really issues state+PKCE cookies,
   and `GET /api/auth/entra/callback` runs for real end to end (identity
   extraction → `oauth_callback` → group sync → OBO auto-provision → profile
   sync → JWT mint → 303 to `/users/sign-in?access_token=…`). Same for the
   per-user connection sign-ins via `/api/connections/oauth/callback`.
2. **Raw TDS egress (port 1433) is impossible** — the proxy answers
   `HTTP/1.1 200 Connection Established` and then resets the stream; port 443 to
   the same host completes a TLS 1.3 handshake. Live Fabric SQL therefore cannot
   work here. `BOW_SANDBOX_FABRIC_SHIM=1` patches `pyodbc.connect` **only** for
   the demo warehouse hostname (discovered live from the Fabric REST API:
   `p3fxcoawh6auxbgfdjpbqvij7e-simszbl3ii5uthh3ulrmrqf4sq.datawarehouse.fabric.microsoft.com`,
   warehouse `demo_db`) and backs it with DuckDB seeded with exactly the
   `dbo.sales` / `dbo.finance` rows, emulating the tenant's
   `HAS_PERMS_BY_NAME` GRANT/DENY. Identity is decoded from the **real Entra
   access token** BOW passes in `attrs_before[1256]` (audience + expiry
   checked), so only genuinely-acquired delegated tokens work and every layer
   above pyodbc is real. **Power BI, SharePoint, OneDrive, Outlook and Graph ran
   fully live over 443.**

## What was validated

| Step | Result |
|---|---|
| `sso_only` sign-in page | No local form (0 password inputs), single **"Sign in with entra"** button; `/api/auth/entra/authorize` 200 with a real `login.microsoftonline.com` URL |
| First SSO user | Auto-org "Main Org", role `admin`, `is_enterprise: true` |
| Group sync on login | demo1 → **AllFabric**, demo2 → **MinimalFabric** (see **Bug 1** for the third group) |
| Power BI connection (UI) | Test → "Connected successfully. Found 6 model tables"; save → **"Discovered 6 model tables in 4s"**; unreadable RLS model `geo (verify-rls)` excluded with a backend warning (correct) |
| Fabric connection (UI) | Test 3.1s; **"Discovered 2 tables in 1s"** |
| PG 5K connection (UI) | Test 1.0s; **"Discovered 5000 tables in 1s"** |
| SharePoint connection (UI) | Test 6.1s; **"Discovered 10 files in 6s"** |
| OneDrive / Outlook (UI) | "Connected successfully… no admin-side catalog for this connector", auto-created public agents |
| MCP connection (UI) | Streamable HTTP, test **1.1s**, "Found 1 tool(s)" |
| Per-user OBO sign-in | 3 users × PBI + Fabric = 6/6 succeed. Graph: yochay 3/3; demo1/demo2 correctly fail (tenant consent/licensing — see table below) |
| Per-user scoping | PBI 6/6/6 · Fabric yochay 2, demo1 2, **demo2 1** (`dbo.sales` only — DENY honored) · PG 5K yochay 5000, demo1 2000, demo2 1500 — verified in the UI, over `full_schema`, and in `user_data_source_tables` |
| **Reload tables** (prev. Bug 1) | **Fixed and holding.** demo2's reload logged *"crawled with the CALLER's own credentials … nothing is pruned"*, `Created 0, updated 0, left-untouched 1`; canonical stayed `[dbo.sales, dbo.finance]` and demo1/admin kept 2 tables. 5 reloads across 3 identities, zero drift |
| Reload timings | demo1 × PBI **3.7s** · admin × PBI **3.4s** · demo2 × Fabric **0.1s** · admin × Fabric **0.2s** |
| **Cross-user context** (prev. Bug 2) | **Fixed and holding.** demo1 primed the Fabric schema cache first (both tables); demo2's very next report carried **`dbo.sales` only** in every `context_snapshots` row (`initial`/`pre_tool`/`post_tool`/`final`) |
| Wizard connection replacement (prev. Bug 3) | **Fixed and holding** — creating "Echo MCP" from `/agents/new` replaced the auto-selected "Power BI" instead of appending |
| Singular "1 table" (prev. Bug 4) | **Fixed and holding** — demo2's Fabric agent header reads "1 table" |
| File-connector documents under Tables (prev. fix) | **Holding** — SharePoint/OneDrive/Outlook agents all show `0 tables`; SharePoint's Tables panel says "No tables found" while its 10 documents stay reachable through the file tools |

### End-to-end reports (real Anthropic Haiku, chat UI only)

| # | agent · user | result | turn |
|---|---|---|--:|
| 1 | Fabric · **demo1** | Bar chart "Total Sales by Region" — **US $3,200 / EMEA $1,500** (exactly the seeded rows), `create_data` 8.1s | 17.2s |
| 2 | Power BI · **demo1** | **Live DAX** through the user's delegated token → KPI card **40 customers**, `create_data` 7.2s | 17.1s |
| 3 | Power BI · **demo2** | Top-5 `SalesPush/Sales` rows with a computed Total column, real values | 20.2s |
| 4 | Fabric · **yochay** | `dbo.finance` → Engineering **$500,000**, Marketing **$200,000** (matches seed) | 14.1s |
| 5 | Fabric · **demo2** (denied table) | **No leak.** Context held `dbo.sales` only; the model said so plainly and asked a clean clarifying question | 29.2s |
| 6 | Fabric · **demo2** (explicit `SELECT * FROM dbo.finance`) | **Blocked by BOW in 0.8s** — "dbo.finance does not exist in the indexed schema" — before any query ran | 17.1s |
| 7 | PG 5K · **demo1** | `t_0001` → alpha/beta, correct | 26.1s |
| 8 | PG 5K · **demo2** | see **Bug 4** | 20.1s |
| 9 | MCP · **demo1** | tool call **422ms**; identity arrived over the wire (below) | 14.1s |
| 10 | MCP · **demo2** | same, with demo2's identity | 11.1s |

### MCP — user identity over the wire (per user)

Real streamable-HTTP echo server on `:3333`; connection built in the UI with
Advanced → header `X-User-Email ← user.email` and **locked** metadata field
`user_email ← user.email`. The capture file shows BOW-injected identity sitting
next to model-authored arguments, and it changes per signed-in user:

```jsonc
// demo1's turn
"received_arguments": {
  "prompt": "Query production orders for company 111 for the week of 2026-07-20 to 2026-07-26",
  "company": "111",                                                  // model-authored
  "custom_metadata": { "user_email": "demo1@bow14.onmicrosoft.com" } // BOW-injected (locked)
},
"received_headers": { "x-user-email": "demo1@bow14.onmicrosoft.com", … }

// demo2's turn — same connection, same agent
"custom_metadata": { "user_email": "demo2@bow14.onmicrosoft.com" }
"x-user-email": "demo2@bow14.onmicrosoft.com"
```

### Graph connectors — what the tenant actually permits

Re-probed live this run; unchanged from the previous loop, and it bounds the
multi-user story by tenant licensing/consent, not by the product:

| | SharePoint site | OneDrive | Mailbox |
|---|---|---|---|
| **yochay** | ✅ 10 files | ✅ 14 items | ✅ 25 messages |
| **demo1** | ❌ `AADSTS65001` not consented | ❌ `403 provisioningNotAllowed` (no licence) | ❌ `AADSTS65001` not consented |
| **demo2** | ❌ same | ❌ same | ❌ same |

`user_data_source_tables` holds rows for **yochay only** (10 SharePoint + 14
OneDrive); `full_schema` returns `[]` for demo1/demo2 on all three agents. **No
cross-user leakage.** Their failed sign-ins are the isolation test — and they
surfaced **Bug 3** below.

### LLM tool matrix (real Haiku, through the chat UI, as yochay)

| # | file type / tool | result | time |
|---|---|---|--:|
| 1 | `list_files` | ✅ all 10 files grouped by type, sizes correct, even glossed the Hebrew filename | 14.1s |
| 2 | **CSV** `read_file` | ✅ `2017_Expense_Data.csv` columns + total 5,729.52 | 23.2s |
| 3 | **XLSX** `read_file` | ✅ real P&L: revenue $9,325M → $9,931M (2014–18), +1.6%/yr | 23.2s |
| 4 | **PDF** `read_file` | ✅ `BOW Customer Deck.pdf` summarised (one wasted call — **Bug 5**) | 44.3s |
| 5 | **DOCX** `read_file` | ✅ `Bank Hapoalim - BOW AI Pilot.docx` — Hebrew POC proposal, correctly summarised | 23.2s |
| 6 | **PNG** `search_files` | ✅ found (the previous loop's wildcard-400 fix holds) | 11.1s |
| 7 | **OneDrive XLSX** `list_files`+`read_file` | ✅ 14 files, `Book 1.xlsx` read (one wasted call — **Bug 5**) | 35.2s |
| 8 | **Outlook** `list_emails`+`read_email` | ✅ 25 messages, 3 newest with exact dates (one wasted call — **Bug 6**) | 20.2s |
| 9 | **PDF (Hebrew)** `read_file` | ✅ accurate clause-by-clause summary of the NDA (one wasted call — **Bug 5**) | 68.3s |

### 5 000-table Postgres, real per-user DB logins

Fixture: `bigdb` with 5 000 tables (`t_0001`…`t_5000`) and four login roles —
`svc` (connection identity) 5 000, `u_yochay` 5 000, `u_demo1` **t_0001–t_2000**,
`u_demo2` **t_1501–t_3000** (deliberately overlapping, different sets).

| layer | admin | demo1 | demo2 |
|---|--:|--:|--:|
| Postgres GRANTs | 5 000 | 2 000 | 1 500 |
| `user_data_source_tables` | — (service identity) | **2 000** | **1 500** |
| `GET /full_schema` | **5 000** | **2 000** | **1 500** |
| planner schema section starts at | — | `public.t_0001` | **`public.t_1501`** |
| canonical `connection_tables` | **5 000, unchanged** after every per-user sign-in | | |

Timings, end-to-end through the UI: connection test **1.0s**; discovery of
5 000 tables **1s**; "Select all" + save of 5 000 rows ~6s; per-user credential
save **1.5–2.2s** and the full 2 000/1 500-row overlay landed **10.1s** after
the click. That last number is the previous loop's N+1 fix holding at scale.

**The per-user difference is provable right down to the prompt**: demo1's schema
section begins at `public.t_0001` and demo2's at `public.t_1501` — each user's
own first granted table. See **Bug 4** for what else that section showed.

---

## Bugs / hiccups found

> Bugs 1–3 and 7 are **fixed in this branch** and re-verified live. Bugs 4–6 are
> reported with evidence and repro; 4 in particular is a design decision worth a
> deliberate call rather than a quick patch.

### 1. Entra groups that Graph can't resolve are still named by raw GUID — FIXED

The previous loop added a fallback label in `group_sync_service.py:65`
(`f"Unresolved directory group ({ext_id[:8]}…)"`), but it was **dead code**:
`graph_client.resolve_group_names_by_ids` ends with

```python
for gid in group_ids:
    if gid not in result:
        result[gid] = gid      # ← unresolved id echoed back as its own name
```

so `group_names.get(ext_id)` is always truthy. Observed live on the admin's very
first login:

```
OIDC group sync: created group '85f43b45-99ae-43a0-a780-a05c119e8b9c' …
groups table: name=85f43b45-99ae-43a0-a780-a05c119e8b9c
```

**Fix:** the resolver now returns only the ids Graph actually resolved (and only
when `displayName` is non-empty), in both `resolve_group_names_by_ids` and the
`/me/memberOf` overage path. The caller's "Unresolved directory group (…)" label
becomes reachable.

### 2. A user who can't sign in to a `user_required` agent gets a dead-end report — FIXED

`report_service.create_report` deliberately drops `user_required` data sources
the caller can't use (`filter_user_usable_data_sources`) so tools don't 403
mid-run — correct. But the second return value was thrown away:

```python
data_sources, _skipped_unconnected = await ds_service.filter_user_usable_data_sources(...)
```

Observed live: demo1 clicks **+ New report** on the SharePoint agent →
`report_data_source_association` is empty → the composer's send button is
permanently disabled (`canSubmit` needs a data source or a file) and **nothing
on screen says why**. The user can type a question and simply cannot send it.
demo1/demo2 hit this on SharePoint and Outlook; the same reports on PBI/Fabric/MCP
(where their sign-in succeeded) were scoped correctly, which is what isolated it.

**Fix:** `ReportSchema.unconnected_data_sources` now carries the dropped agent
names so the client can say "sign in to *SharePoint Agent*" instead of handing
over an unusable report. Verified:

```
POST /api/reports {"data_sources":["<sharepoint agent>"]}  as demo1
  data_sources attached: 0
  unconnected_data_sources: ['SharePoint Agent']
```

### 3. Table-selector counters — FIXED (two separate defects)

**3a — "0/6 active" after clicking *Select all*.** `selectAllMatching()` /
`deselectAllMatching()` in `TablesSelector.vue` tick the checkboxes and queue a
pending bulk action but never touch `selectedCount`, so the label kept the
server's stale value while every row was visibly checked. Reproduced on the
6-table Power BI agent and the 5 000-table Postgres agent. Now:

```
initial: 6/6 active → Deselect all: 0/6 active → Select all: 6/6 active
```

**3b — "2/1 active" for a restricted user.** In
`get_data_source_schema_paginated`, `total_tables` is wrapped in `_scope(...)`
(the per-user overlay filter) but `selected_count` was not — an org-wide
numerator over a per-user denominator. demo2's Fabric Tables panel showed
`Showing 1-1 of 1` next to `2/1 active`. `selected_count` is now scoped the same
way; demo2 now reads **`1/1 active`**.

### 4. MAJOR (design) — the per-user table allowlist gates `create_data` **per call**, not per table

`create_data`'s `_resolve_tables` resolves each requested name against the
caller's *active* schema, drops the ones that don't match, appends a
**warning**, and fails the call only when **nothing** matched
(`create_data.py:1280-1290`). The generated `generate_df` then calls
`ds_clients[...].execute_query(<free-form SQL>)`, which nothing re-inspects.

So a denied table can ride along in a call that also names an allowed one.
Observed live — demo2 (granted `t_1501`–`t_3000`) asked for `t_0001` **and**
`t_2500`:

```python
# generated code, verbatim
df_t_2500 = ds_clients["PG5K Agent:PG 5K"].execute_query("SELECT * FROM public.t_2500")
# Note: t_0001 is not in the provided schema, but attempting to query it as requested
try:
    df_t_0001 = ds_clients["PG5K Agent:PG 5K"].execute_query("SELECT * FROM public.t_0001")
except Exception as e:
    ...
```

BOW let it run. The only thing that stopped it was Postgres itself:
`psycopg2.errors.InsufficientPrivilege: permission denied for table t_0001`.
Contrast the single-table case on Fabric, where BOW refused before executing
("dbo.finance does not exist in the indexed schema", 0.8s).

**No data leaked in this run** — the DB login *is* the user, so the source's own
ACL was the backstop, and the model's summary even mislabelled t_2500's rows as
t_0001's. The exposure is where the connection identity is **broader** than the
user's BOW-visible set: `query_identity="service_account"`, the owner/admin
system-credential fallback, or any per-user login with wider DB rights than the
overlay. There, BOW's overlay is the *only* gate — and it lets a denied table
through whenever a permitted one is in the same call.

Fix direction: fail the call (or drop the offending group) when **any** named
table fails to resolve for this user, rather than only when all do; and surface
the existing `warnings` to the planner instead of discarding them.

### 5. `read_file` follow-up reads waste a tool call on file-source connections

Three of the nine file turns paid an extra failed call. Two distinct messages,
both after a successful first read:

- `This connection does not support windowed (offset/length) reads.` (SharePoint PDF)
- `'<graph item id>' is not a file attached to this conversation. Pass a session
  file id from <files>, or a connection_id + file id from list_files/search_files
  for a file source.` (OneDrive XLSX, Hebrew PDF)

The model recovers on its own each time, so results are correct — but every
long document costs one wasted round trip, and the second message points at
"session file id" when the actual fix is "pass `connection_id`". Worth either
accepting offset/length on file sources or naming the missing argument in the
error.

### 6. `list_emails` still doesn't show the model the received date

The previous loop documented `modified_at` as carrying the received date for
mail items. The model still doesn't find it. Verbatim from the run:

> The `list_emails` call succeeded and returned 25 emails. The list shows the
> subjects and message IDs… However, the `list_emails` response doesn't
> explicitly show the received dates. Let me read the full details of the 3
> newest emails to get their exact dates.

It then issued a `read_email` per message and got the right answer. The
description fix wasn't enough — the date needs to be visible in the rendered
`list_emails` observation, not just documented in the schema.

### 7. Per-user credential modal rendered the password in plain text — FIXED

`UserDataSourceCredentialsModal.vue` decided masking from JSON-Schema `format`,
but the connector registry marks secrets with `ui:type: password` (what
`ConnectForm` reads). Postgres' per-user `password` field therefore rendered as
`<input type="text">` — the DB password visible on screen as typed. Verified
live before/after:

```
before: [{"y":470,"type":"text"}, {"y":532,"type":"text"}]
after : [{"y":470,"type":"text"}, {"y":532,"type":"password"}]
```

### 8. Smaller frictions (not fixed)

- **OBO auto-provision on every SSO login fails with `AADSTS50013`.**
  `exchange_obo_token` presents the login access token as the OBO assertion, but
  in this standard configuration that token's audience is Microsoft Graph, not
  the app — so Entra rejects the assertion. Two log records per `user_required`
  Entra connection per login (one `ERROR` + one `WARNING`, each with a full
  Entra error blob). Harmless — the explicit per-user sign-in works — but the
  feature is silently inert and the logs read like a real failure.
- **Schema context degenerates at 5 000 tables.** Every entry in
  `schemas_usage.tables_used` came back with `score: 0.0` and
  `selection_reason: "top_k_score"`, i.e. the top-10 fell back to "first ten by
  name" with nothing telling the model how many tables exist or that it can
  search. demo1's `t_0001` question worked only because `t_0001` is in the first
  ten; demo2 found `t_2500` only because the user named it. "Which table has
  budget data?" over 5 000 tables has nothing to work with.
- **A newly created agent has 0 tables selected**, and the only screen that can
  change that selection is the creation wizard's
  `/agents/new/{id}/schema`. The agent page's "N tables" chip is not a link, and
  the tree's Tables panel is **read-only for members**. Clicking straight
  through the wizard yields a silently useless agent.
- **Connection detail shows "Tools 0 · Data Agents 0"** for Echo MCP while the
  same panel says "Discovered 1 tool in 0s" and `domain_connection` links it to
  MCP Agent.
- `GET /api/data_sources/{id}/connections` returns **403 for members on every
  agent**, including public ones the member can query fine. Chat is unaffected;
  it just makes the agent page's network log noisy.
- `sync_domain_tables_from_connection: No ConnectionTable records found, cannot
  sync` warns on every per-user file connector (they have no shared catalog by
  design).
- One transient `Connection reset by peer` from the sandbox egress proxy during
  a SharePoint search; the agent recovered on its own. Environmental.

## Repro pointers

- Stack: `tools/agent/boot_stack.sh --dev` with `BOW_CONFIG_PATH` pointing at a
  config with the `entra` provider enabled and `auth.mode: sso_only`; secrets via
  `BOW_ENTRA_CLIENT_SECRET`, `BOW_ENCRYPTION_KEY`, `BOW_LICENSE_KEY`.
- The numbered Playwright drivers, the ROPC/Fabric shim and the Postgres fixture
  live in the session scratchpad — session tooling, not repo code.
- **Bug 2 repro:** as a member with no per-user credentials on a `user_required`
  agent, click **+ New report** on that agent → the report has no
  `report_data_source_association` and the composer can never be submitted.
- **Bug 4 repro:** `user_required` source where the user can see table B but not
  table A → ask one question naming both → `create_data` runs, warns about A,
  and executes generated SQL that references A.
