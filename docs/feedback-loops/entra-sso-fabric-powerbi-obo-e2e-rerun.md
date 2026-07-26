# Feedback Loop — Entra `sso_only` + Fabric/Power BI OBO + Graph connectors + MCP, re-run on current main

Second full pass over the scenario first covered in
`entra-sso-fabric-powerbi-obo-e2e.md` (PR #777), re-run 2026-07-25 against the
**same real `bow14.onmicrosoft.com` tenant** but on **current main**, which has
since taken PRs #778/#779 plus the Salesforce and docx changes. Purpose: confirm
the four fixes from that loop still hold and catch anything that regressed or
was fixed at the wrong layer.

**This run investigates and documents only — no product code was changed.**

## Setup (all through the UI unless noted)

| Step | Result |
|---|---|
| `auth.mode: sso_only` + `entra` OIDC provider (login scopes `openid profile email`, `sync_groups`, `resolve_group_names`), EE license from `BOW_LICENSE_KEY` | `/api/settings` reports `mode: sso_only`, one enabled provider |
| Sign-in page | Local form correctly absent — 0 password inputs, 0 email inputs, a single SSO button. **But see Finding 6** |
| Admin (`yochayettun@…`) first SSO login | Auto-org "Main Org", role `admin` |
| Entra group sync on login | admin → `PowerBI-ServicePrincipals` + one unresolved id; demo1 → **AllFabric**; demo2 → **MinimalFabric**. **See Finding 5** |
| LLM setup (`/settings/models`) | Anthropic provider, **Claude 4.5 Haiku only**, live "Connection successful", saved as Default + Small default |
| Power BI connection (UI, `user_required`, OAuth app = login app) | Test passed; save → **"Discovered 6 model tables in 4s"** |
| Microsoft Fabric connection (UI, `user_required`, warehouse `demo_db`) | Test passed; save → **"Discovered 2 tables in 2s"** (`dbo.sales`, `dbo.finance`) |
| Agent table selection (UI) | Fabric 2/2 active, Power BI 6/6 active |
| Member invites (UI `/settings/members`) | demo1 + demo2 pending memberships created |
| Per-user OBO sign-in, 3 users × 2 connections | **All 6 succeed.** token ~1.0–1.3 s; PBI verify 3.0–3.4 s, overlay 2.5–5.8 s; Fabric verify 0.6–0.7 s, overlay <0.1 s |
| Per-user scoping (UI Tables view) | PBI 6/6/6 for yochay/demo1/demo2. Fabric: yochay 2, demo1 2, **demo2 1 (`dbo.sales` only — DENY on `dbo.finance` honored, no leak)** |
| Reload tables as admin (UI) | 3.1 s; catalog intact — log: `Created 0, updated 0, left-untouched 2` |

### Regression checks on the four fixes from PR #777

| Fix | Status |
|---|---|
| 1. Shared catalog is the union; per-user crawl never prunes | **Holds** — canonical `connection_tables` stayed at 8 rows (2 Fabric + 6 PBI) across every reload |
| 2. Identity-aware context caches (`_schema_identity_key`) | Present in `context_hub.py`; per-user views correct in UI (demo2 = 1 table) |
| 3. Wizard "create connection replaces selection" | Present |
| 4. Entra unresolved-group labelling | **DOES NOT WORK — see Finding 5** |

## Sandbox constraints (environmental, not product bugs)

Both walls from the first loop are still present and were re-verified this run:

1. **The hosted Microsoft login page cannot render in headless Chromium.**
   Clicking "Sign in with entra" in the real browser yields
   `ERR_CONNECTION_RESET` — the egress proxy rejects Chromium's TLS handshake to
   `login.microsoftonline.com`, while curl/httpx through the same proxy get
   HTTP 200. Every interactive Microsoft hop was therefore completed
   server-side with **real Entra tokens** via ROPC, driving the exact production
   code paths (`auth_providers._handle_callback`'s post-token sequence, then the
   browser session established via `/users/sign-in?access_token=…` — the very
   URL the real callback redirects to; and the
   `connections/oauth/callback` sequence for per-connection sign-in).
2. **Raw TDS egress (port 1433) is impossible**, and this sandbox additionally
   ships **no ODBC driver** (`libodbc.so.2` missing until installed). TCP and
   OpenSSL to `p3fxcoawh6auxbgfdjpbqvij7e-…datawarehouse.fabric.microsoft.com:1433`
   both hang and are killed. The Fabric leg therefore ran against an
   out-of-repo, env-guarded shim (`BOW_SANDBOX_FABRIC_SHIM=1`) that patches
   `pyodbc.connect` **only for that hostname**, backed by DuckDB seeded with the
   exact `dbo.sales` / `dbo.finance` rows and emulating the tenant's
   `HAS_PERMS_BY_NAME` GRANT/DENY. Identity is decoded from the **real Entra
   access token** BOW passes via `attrs_before[1256]` (audience + expiry
   checked), so ROPC-acquired user tokens drive it and everything above pyodbc
   is real product code. **Power BI ran fully live** over 443.

---

## What the rest of the scenario produced

### Real e2e reports through the chat UI (real Anthropic Claude 4.5 Haiku)

| # | agent · user | ask | result | wall |
|---|---|---|---|--:|
| 1 | Fabric · demo1 | total sales by region, bar chart | ✅ **US $3,200 / EMEA $1,500** — matches the seeded rows exactly; `create_data` 7.4 s | 14.2 s |
| 2 | Power BI · demo1 | count of SalesPush customers, KPI | ✅ **40 customers** via live DAX on the user's delegated token; `create_data` 6.1 s | 14.1 s |
| 3 | Fabric · demo2 | total budget by department in `finance` | ✅ **no leak** — "the available schema only shows a `dbo.sales` table"; clean clarifying question | 8.1 s |
| 4 | SharePoint · yochay | `list_files` | ✅ all 10 files grouped by type | 14.3 s |
| 5 | SharePoint · yochay | **CSV** `read_file` | ✅ real values — total expenses **$5,729.52** + breakdown | 20.2 s |
| 6 | SharePoint · yochay | **XLSX** `read_file` | ✅ P&L 2014-18, revenue from **$9,324.99M** | 24.2 s |
| 7 | SharePoint · yochay | **DOCX (Hebrew)** `read_file` | ✅ accurate 3-bullet English summary of a Hebrew pilot doc | 24.2 s |
| 8 | SharePoint · yochay | **PNG** `search_files` + read | ✅ found and described — **no HTTP 400** (the wildcard fix holds) | 22.3 s |
| 9 | OneDrive · yochay | `list_files` + **XLSX** read | ✅ 14 files; `Book 1.xlsx` 32 rows × 2 cols | 30.2 s |
| 10 | Outlook · yochay | `list_emails` + `read_email` | ✅ 25 messages, 3 newest read | 20.2 s |
| 11 | SharePoint · **demo1** | isolation | ✅ 403 explained, **zero files leaked** | 8.1 s |
| 12 | Outlook · **demo2** | isolation | ✅ mailbox error explained, **zero mail leaked** | 10.1 s |
| 13 | MCP · demo1 | production orders, company 111 | ✅ identity forwarded (below) | 12.2 s |
| 14 | MCP · demo2 | production orders, company 222 | ✅ identity forwarded, per-user | 10.2 s |
| 15 | PG (5k) · demo1 | count rows in `t_0500` | ✅ in-grant, `create_data` 11.5 s | 22.2 s |
| 16 | PG (5k) · demo2 | count rows in `t_0500` | ✅ **correctly refused** — "the available tables are named t_1001 through t_2500" | 16.2 s |

### Cross-user context isolation — verified at the context layer

demo1's Fabric report primed the schema cache; demo2's ran **97 s later, inside the
300 s TTL** — the exact ordering that leaked before the fix. Reading
`context_snapshots` directly:

```
demo2-finance [initial|pre_tool|post_tool×2|final]:  finance=False  sales=True
demo1-fabric  [initial|pre_tool|post_tool|final]  :  finance=True   sales=True
```

demo2 never sees `dbo.finance` in any snapshot. **Fix 4 (identity-aware caches) holds.**

### MCP — user email over the wire, per user

Real streamable-HTTP echo server on `:3333`; connection created through the UI with
Advanced → header `X-User-Email ← User email` and **locked** metadata `user_email ←
User email`. Test Connection: *"Connected successfully. Found 1 tool(s)"* in 1.0 s.
The capture file shows model-authored args alongside BOW-injected identity, and it
tracks the signed-in user:

```jsonc
// demo1's turn
"received_arguments": { "prompt": "Query production orders for company 111 …",
                        "company": "111",
                        "custom_metadata": { "user_email": "demo1@bow14.onmicrosoft.com" } },
"received_headers":   { "x-user-email": "demo1@bow14.onmicrosoft.com", … }
// demo2's turn  ->  company "222", custom_metadata.user_email / x-user-email = demo2@…
```

### Graph connectors — the tenant gate (unchanged from the first loop)

| | SharePoint site | OneDrive | Mailbox |
|---|---|---|---|
| **yochay** | ✅ 10 files (verify 4.6 s, overlay 4.6 s) | ✅ 14 files (verify 3.0 s, overlay 12.2 s) | ✅ `Connected as YochayEttun@…` |
| **demo1** | ❌ 403 `accessDenied` | ❌ 403 `notAllowed` | ❌ no Exchange mailbox |
| **demo2** | ❌ 403 `accessDenied` | ❌ 403 `notAllowed` | ❌ no Exchange mailbox |

Full multi-user coverage here needs tenant-side changes (M365 licences for
demo1/demo2, site membership, tenant-wide admin consent) — production identity and
billing configuration, deliberately not done. The demo users therefore served as the
**isolation** test, which they passed: `user_data_source_tables` holds rows for
**yochay only** (10 SharePoint + 14 OneDrive), and the file agents return 0 for the
demo users.

**Outlook's per-user test is now correctly actionable** (the first loop's Fix 1):
`Signed in as demo1@…, but this account has no Exchange mailbox (it is inactive,
soft-deleted, or missing a Microsoft 365 mail licence)`.

**File agents show 0 tables** (SharePoint 0 · OneDrive 0 · Outlook 0 · Fabric 2 ·
PBI 6) — their documents live under Files, not Tables. That fix holds too.

### Postgres at 5 000 tables, three identities, real per-user DB logins

Fixture: one Postgres, 5 000 tables, three login roles —
`svc_user` **5 000** (org identity) · `u2_user` **t_0001–t_2000** (2 000) ·
`u3_user` **t_1001–t_2500** (1 500, overlapping). demo1 signed in as `u2_user` and
demo2 as `u3_user` through the per-user credential modal.

| step | result | time |
|---|---|--:|
| Connection create (UI) | **"Discovered 5000 tables in 1s"** | 1 s |
| Per-user credential sign-in (UI, ×2) | Test 1.0 s each, save 5.0 s | — |
| Tables view, admin | `Showing 1-100 of 5000` | 4.3 s |
| Tables view, demo1 | `Showing 1-100 of **2000**` | 5.4 s |
| Tables view, demo2 | `Showing 1-100 of **1500**` | 5.6 s |
| Select all 5 000 + Save | `5000/5000 active` | 10.3 s |
| Reload tables, admin | canonical still 5 000, overlays intact | 3.1 s |

Canonical / overlay integrity after both per-user syncs:

```
connection_tables (PG Scale) = 5000          # union preserved
user_data_source_tables:  yochay 32 · demo1 2008 · demo2 1507
                          (2000+6+2)   (1500+6+1)   -> matches the GRANTs exactly
```

**Schema → prompt loading is exactly per-user at 5 000 tables.** The planner's
`schemas_usage` section is a bounded top-N sample (10 tables), and it starts at each
identity's own grant boundary:

```
demo1  schemas_usage -> t_0001 … t_0010      (their grant starts at t_0001)
demo2  schemas_usage -> t_1001 … t_1010      (their grant starts at t_1001)
```

demo2's section never contains t_0001–t_1000, and the model consequently answered
"the available tables are named t_1001 through t_2500". No leakage, no truncation
failure, no slow path.

---

## Findings

Nothing below was fixed — this run documents only. Ordered by severity. Finding 1
was retracted after re-testing; findings 2-5 and 7 carry code-level citations.

### 1. ~~MAJOR (UX) — agents in the `/agents` list cannot be opened by clicking~~ — RETRACTED (harness error)

**This was my mistake, not a product bug.** Clicking an agent row works correctly.
Re-tested three times on the same build with `pageerror`/`console` capture:

```
list populated: true
bounding box: {"x":306,"y":306,"width":217,"height":20}
elementFromPoint chain: ["SPAN.flex-1.text-start.truncate","DIV.group.w-full.flex", …]
URL after click: http://localhost:3000/agents/13b88284-0972-4660-a6b6-2804ecaef05c
right pane empty-state present: false
errors: []                                   ← 3/3 runs identical
```

The original failures were a **Nuxt dev-mode hydration race in my driver**, not in
the app: the readiness check only waited for `document.body` innerText to exceed a
length threshold, and the static sidebar chrome satisfies that before Vue finishes
hydrating the agent list. The rows were rendered and hit-testable (hence "visible
and enabled", and a correct `elementFromPoint` chain) but their click handlers were
not yet bound, so every variant — `.click()`, `{force:true}`, parent-div, raw
`mouse.click()` — was swallowed. Gating on the agent's own name appearing, then
settling, fixes it.

Lesson for future loops: under `yarn dev`, "text is present" is not "the app is
interactive". Gate on the specific element you are about to drive.

### 2. MAJOR (correctness, display) — restricted user's table counter reads "2/1 active"

On the Fabric agent's Tables view as **demo2** (overlay = `dbo.sales` only) the
header reads:

```
Showing 1-1 of 1
2/1 active          <-- numerator 2, denominator 1
```

**Where:** the agent's Tables view — `frontend/components/datasources/TablesSelector.vue:245`

```vue
{{ selectedCount }}/{{ totalTables }} active
```

Both values come from `GET .../schema` (paginated), and in the backend builder
`data_source_service.get_data_source_schema_paginated`
(`backend/app/services/data_source_service.py`) every count is wrapped in the
user-overlay scoper `_scope(...)` **except the selected count**:

```python
2753  total_tables_result = await db.execute(
2754      _excl(_scope(select(func.count(DataSourceTable.id)).where(...)))   # user-scoped ✔
2759  base_query  = _excl(_scope(select(DataSourceTable).where(...)))       # user-scoped ✔
2760  count_query = _excl(_scope(select(func.count(...)).where(...)))       # user-scoped ✔

2842  selected_count_result = await db.execute(
2843      _excl(select(func.count(DataSourceTable.id)).where(               # ← no _scope()
2844          DataSourceTable.datasource_id == data_source_id,
2845          DataSourceTable.is_active == True))
2846  )
```

The comment above it even states the intent — *"Get total selected count (across ALL
tables, not just filtered)"* — which is right for a **search filter** but wrong for a
**per-user overlay**: it makes the numerator the agent's canonical selection (2, what
the admin activated) while the denominator is the caller's own visible set (1). Hence
"more active than exist". The rows themselves are correct (`Showing 1-1 of 1`, only
`dbo.sales`, no leak) and the `read-only` badge is right — this is display only.

Same shape at 5 k scale (`0/2000` for demo1 vs `0/1500` for demo2), which reads
correctly there only because the numerator happened to be 0 before activation.

### 3. MAJOR (error handling) — Outlook Mail's admin credential test always fails, and dumps raw Graph JSON

The Outlook Mail connector is explicitly per-user ("Each user signs in individually
to access their own data — no shared service account"), yet **"Test credentials"**
issues `GET /v1.0/me` with the **app-only** (client-credentials) token. Graph
rejects that by definition, and the raw response body is rendered into the form:

```
Graph https://graph.microsoft.com/v1.0/me?$select=userPrincipalName,displayName → 400
{"error":{"code":"BadRequest","message":"/me request is only valid with delegated
authentication flow.","innerError":{"date":"2026-07-25T22:12:38",
"request-id":"ab36532b-2141-443a-b9d9-d86174fe9c85",
"client-request-id":"ab36532b-2141-443a-b9d9-d86174fe9c85"}}}
```

**Confirmed in code, not just observed.** `GraphMailClient.test_connection`
(`backend/app/data_sources/clients/graph_mail_client.py:88-93`) calls `/me`
unconditionally and returns the bare exception text on failure:

```python
88  def test_connection(self) -> dict:
89      try:
90          me = self._get("/me?$select=userPrincipalName,displayName")
91          who = me.get("userPrincipalName") or me.get("displayName") or "Microsoft account"
92      except Exception as e:
93          return {"success": False, "message": str(e)}      # ← raw Graph body reaches the UI
```

The sibling client **already solved exactly this case**.
`GraphDriveClient.test_connection` (`graph_drive_client.py:532-554`) branches on
`_user_token_provided` and documents the very error verbatim in its docstring:

> *"Admin-only (service-principal credentials, no user token yet): just verify the
> token can be acquired. Drive/root access needs a user token, which arrives after a
> user completes OAuth — **testing it now would fail with `/me request is only valid
> with delegated authentication flow` for OneDrive**…"*

So OneDrive returns *"…successfully. Each user sees their own files after signing in
— no admin-side catalog for this connector"* while Outlook, lacking the same guard,
always throws. The fix is the guard the drive client already has (or skipping the
app-only probe entirely).

Two independent reproductions with different Graph `request-id`s and timestamps
(`ab36532b-…` at 2026-07-25T22:12:38 in this run; `3089e8a6-…` at 2026-07-26T03:10:29
in a separate manual run), so it is deterministic rather than a transient tenant
hiccup. The connection still saves afterwards, so this is a scare rather than a
blocker — but it reads as a hard failure to the admin configuring it.

### 4. MEDIUM (error handling) — raw Graph JSON reaches the end-user chat transcript

demo1 asking the SharePoint agent to list files renders this **in the report**, in
front of the user:

```
live list_files failed: Graph https://graph.microsoft.com/v1.0/sites/bow14.sharepoint.com:/sites/employees
→ 403 {"error":{"code":"accessDenied","message":"Access denied","innerError":
{"date":"2026-07-25T22:24:52","request-id":"4691767c-6b09-4134-902c-9ba04ad89926",
"client-request-id":"4691767c-6b09-4134-902c-9ba04ad89926"}}}
```

**The chain, end to end.** The Graph client raises an exception whose *message* is a
formatted dump of the request URL plus 300 raw response bytes
(`backend/app/data_sources/clients/graph_drive_client.py:220` and `:228`):

```python
raise ValueError(f"Graph {url} → {resp.status_code} {resp.text[:300]}")
```

The tool implementation then stringifies that exception straight into the
observation the user reads (`backend/app/ai/tools/implementations/list_files.py:189`,
and again at `:204`):

```python
data.connection_id, f"live {self._operation_name} failed: {e}"
```

Because `{e}` *is* the URL-plus-JSON string, the whole thing lands in the report
transcript. Same for OneDrive's per-user test
(`GET /me/drive → 403 {"error":{"code":"notAllowed"…`).

**Why it matters:** internal Graph URLs, `request-id` and `client-request-id` values
are meaningless to an end user and look like a crash. The *behaviour* is correct —
nothing leaked, and the model recovered with a sensible clarifying question — it is
purely the presentation. A user-facing summary ("You don't have access to this
SharePoint site") with the raw detail kept in the backend log would be the shape;
`GraphMailClient` already does this for the mailbox case, which is why the three
connectors now disagree: Outlook's per-user path got a friendly message from the
previous loop's Fix 1, SharePoint and OneDrive did not.

### 5. MEDIUM — the Entra "unresolved group" label is dead code; groups still show as raw GUIDs

The previous loop added a fallback so unresolvable Entra group claims render as
`Unresolved directory group (85f43b45…)`:

```python
# app/ee/oidc/group_sync_service.py:65
name = group_names.get(ext_id) or f"Unresolved directory group ({ext_id[:8]}…)"
```

It never fires, because the resolver upstream already substitutes the GUID for
itself:

```python
# app/ee/oidc/graph_client.py:140,145
result[obj["id"]] = obj.get("displayName", obj["id"])   # unresolved -> id
...
for gid in group_ids:
    if gid not in result:
        result[gid] = gid                                # unresolved -> id
```

`group_names.get(ext_id)` is therefore always truthy, and the `or` branch is
unreachable. Observed live on the admin's first login —

```
OIDC group sync: created group '85f43b45-99ae-43a0-a780-a05c119e8b9c' (external_id=85f43b45-…)
```

— and confirmed in the UI (`/settings/members` → Groups), where the roster reads:

```
85f43b45-99ae-43a0-a780-a05c119e8b9c   oidc   1 member
AllFabric                              oidc   1 member
MinimalFabric                          oidc   1 member
PowerBI-ServicePrincipals              oidc   1 member
```

**Why it is unreachable, precisely.** `resolve_group_names_by_ids` guarantees a
non-empty string for *every* requested id, by two separate paths
(`backend/app/ee/oidc/graph_client.py:140` and `:143-145`):

```python
140      for obj in resp.json().get("value", []):
141          result[obj["id"]] = obj.get("displayName", obj["id"])   # resolved-but-nameless -> id

143      # Fill in any IDs that didn't resolve
144      for gid in group_ids:
145          if gid not in result:
146              result[gid] = gid                                    # never-returned -> id
```

Both fallbacks write the **GUID as the display name**. The consumer then does
`group_names.get(ext_id) or "Unresolved directory group (…)"` — and since a GUID
string is always truthy, the `or` branch can never execute. The two Entra cases that
motivated the fix (a directory *role*, and a group the app has no permission to read)
are exactly the ones that take those fallbacks.

**Fix direction:** the labelling has to move up to `graph_client` — leave unresolved
ids out of the dict (or map them to `None`) so the sentinel in `group_sync_service`
becomes reachable; alternatively have the resolver emit the friendly label itself.
Either way the sync service's guard is currently dead code and the admin-facing group
list is the thing that shows the damage.

### 6. MEDIUM (security/UI) — secrets rendered in plain text

Two separate screens show credentials unmasked:

- **Per-user connection credentials** (`Connect <agent>` modal, used for every
  `user_required` non-OAuth connector — Postgres, MSSQL, Snowflake, …): the
  **Password** input is `type="text"`. Screenshot confirms `u2_pw` legible on
  screen. The User field next to it is `type="text"` too, as expected — but the
  password must be `type="password"`.
- **LLM provider API key** (`/settings/models` → New Provider): the key input is a
  plain text field, so the full `sk-ant-api03-…` is legible while typing and after.

### 7. MEDIUM — configured OIDC `label`/`icon` are dropped, so the SSO button shows the raw provider name

`bow-config.yaml` accepts `label` and `icon` on an `OIDCProvider`
(`app/settings/bow_config.py:110-112`), but `GET /api/settings` re-projects the list
to name+enabled only:

```python
# app/routes/bow_settings.py:23-28
"oidc_providers": [ { "name": p.name, "enabled": p.enabled } for p in … ]
```

**What I expected vs what happened.** The config schema advertises the field under a
comment that states its purpose — `# UI niceties` — so the contract a deployer reads
is "set `label` and the SSO button says that":

```yaml
oidc_providers:
  - name: entra
    label: "Sign in with Microsoft"      # ← set in this run's config
    icon: ...
```

```python
# backend/app/settings/bow_config.py:110-113
    # UI niceties
    label: Optional[str] = None
    icon: Optional[str] = None
```

- **Expected:** the sign-in button reads **"Sign in with Microsoft"** (and the icon
  is used), because that is the only thing `label`/`icon` exist for.
- **Actual:** it reads **"Sign in with entra"** — the raw, lowercase provider *key*.

The reason is that the value never leaves the backend: `GET /api/settings`
re-projects the provider list to two fields (`bow_settings.py:23-28`), dropping
`label` and `icon`. Confirmed from both ends — the live payload is
`"oidc_providers":[{"name":"entra","enabled":true}]`, and grepping the frontend shows
no component references a provider `label` at all; the button is composed from the
name. So the config keys are silently inert: no error, no warning, just ignored.

This is the first screen every user of an `sso_only` deployment sees, and "entra" is
an internal identifier rather than a brand name. (The rest of `sso_only` is correct:
0 password inputs, 0 email inputs, no local-registration links, exactly one SSO
button.)

### 8. LOW — `allowed_user_auth_modes` is `null` for non-OAuth `user_required` connections

After creating the Postgres connection through the UI with **Require user
authentication** enabled:

```
('PG Scale', 'postgresql', 'user_required', null)          <-- allowed_user_auth_modes
('Power BI', 'powerbi',    'user_required', '["oauth"]')
('SharePoint','sharepoint','user_required', '["oauth"]')
```

Per-user sign-in works anyway (both demo users authenticated with their own DB
logins), so `null` is evidently treated as "default modes". But the field is
populated for every OAuth connector and empty for the user/pass ones, which makes
the stored policy unreadable and any consumer of that column ambiguous.

### 9. LOW (consistency) — two different connection forms for the same class of connector

SharePoint uses the standard `ConnectForm` (id'd fields, a **Require user
authentication** toggle, **Test Connection** / **Save and Continue**). OneDrive and
Outlook Mail use a different preset-style form (no field ids, dedicated
"OAuth Client ID (override)" rows always visible, **Test credentials** /
**Save Integration**). They also differ in capability: the preset form offers
**"Create a public agent with this integration"** (checked by default — which nicely
resolves the previous loop's "new agents are invisible to members" note), while the
SharePoint form does not, so a SharePoint agent must be created and shared by hand.
Three Microsoft file/mail connectors, three different setup experiences.

### 10. LOW — a plain member can read the whole org roster

demo2 (role `member`) can open `/settings/members` and sees every member's **email,
role, groups, status, last login and last seen**. Write controls are correctly
absent (no Add Member / Remove, and no Roles / Groups / Service Accounts / Quotas
tabs — those are admin-only), so this is disclosure, not privilege escalation. The
left-nav item is also labelled **"Admin"** for a member, which is misleading. Worth
an explicit product decision either way.

### 11. LOW — creating an agent with a duplicate name 409s to a blank white screen

`POST /api/data_sources` returns **409 Conflict** for a name that already exists, and
the wizard renders a completely blank page — no toast, no inline error, no way back
except the browser Back button. (Reproduced by a double-submit; the underlying case
is any duplicate agent name.)

### 12. Cosmetics / small frictions

- **"Select all" doesn't update the counter until Save.** On the Fabric agent the
  counter stayed `0/2 active` after clicking Select all and only became `2/2 active`
  after Save — the selection is applied, but the header lags, so it reads as a no-op.
- **Outlook counts messages as "files"** — `list_emails` renders as
  "Fetching recent emails · **25 files**".
- **Reports are never auto-titled.** Every one of the 16 reports in this run stayed
  "New report" / "untitled report" in the sidebar, which makes the list unusable
  after a handful of turns.
- **Members trigger a burst of 403s on every agent page load** —
  `GET /api/git/repositories`, `GET /api/console/metrics/timeseries`,
  `GET /api/console/metrics/comparison`, `GET /api/data_sources/{id}/connections`.
  Correct RBAC, but the client shouldn't be asking; it's 4 failed requests per page
  view and it fills the browser console with red for every non-admin.
- **`Loading schema…` renders next to a populated table list** on the agent Tables
  view — the list and the loading state disagree for a beat.
- **The "Integrate Models" empty state's primary button says "Update Provider"**
  even when the org has no providers at all.

---

## What held up (no regression)

- `sso_only` gating, first-user auto-org, Entra group sync (AllFabric / MinimalFabric
  resolved correctly), invite → SSO-login attach.
- **Shared catalog is the union / per-user crawl never prunes** — canonical stayed at
  8 rows (Fabric+PBI) and at 5 000 (Postgres) across every reload, from every
  identity. The destructive path is additionally unreachable from the UI for plain
  members: their Tables view is `read-only` with no **Reload tables** button.
- **Identity-aware context caches** — proven at the snapshot level for Fabric
  (demo2 never sees `dbo.finance`) and at 5 k scale for Postgres (demo2's
  `schemas_usage` starts exactly at their grant boundary, `t_1001`).
- **Graph wildcard search** — the PNG turn that returned HTTP 400 last time now
  finds and reads the file.
- **Outlook per-user test** is actionable rather than a false positive.
- **File agents show no tables** (documents stay under Files).
- **Overlay self-healing / per-user overlay sync perf** — 2 000-table and 1 500-table
  overlays built and re-synced without the old N+1 (reload at 5 k: 3.1 s).

## Repro pointers

- Stack: backend on `sqlite:///db/sandbox.db` with `BOW_CONFIG_PATH` pointing at an
  `sso_only` config carrying the `entra` provider; secrets via env
  (`BOW_ENTRA_CLIENT_SECRET`, `BOW_ENCRYPTION_KEY`, `BOW_LICENSE_KEY`).
- Numbered Playwright drivers + the ROPC helpers and the Fabric shim live in the
  session scratchpad — session tooling, not repo code. Screenshots accompany every
  step.
- Finding 1 minimal repro: open `/agents` with at least one agent, click its row,
  observe the URL never changes and the right pane never leaves the empty state;
  then open `/agents/<id>` directly and watch the same agent render fine.
- Finding 2 minimal repro: `user_required` connection where a member's identity sees
  a strict subset; admin activates all tables; sign the member in; open the agent's
  Tables view as them.
