# Connections & Agents — full QA pass

**Date:** 2026-08-05 · **Build:** v0.0.525 · **Auditor:** live sandbox (backend :8000 + frontend :3000, real LLM/Anthropic, real Azure creds)
**Scope:** the *connections* surface only — onboarding connect path, `/agents` (KnowledgeExplorer), the CONNECTIONS strip, Add-Connection catalog + forms, the Connection Detail modal (test / reindex / reload / identity / delete), the New-Agent wizard, and agent connection panels. Roles: **admin**, **member**, **agent-creator (owner)**.
**Not covered (called out honestly):** MCP / Custom-API / Browser tool-connector forms end-to-end; custom-queries (RLS) editor; the per-connection members/sharing tab; SSO/identity-provider. These are noted where they touch a finding but were not driven button-by-button.

> This is a findings inventory, not a fix PR. Each item is a bullet with a pointer to the **Appendix** for repro + screenshot + suspected `file:line`. Nothing here has been changed in code. We triage into Linear (label `connectors`) after review.

---

## 1. The actual experience — why this area feels confusing

I set this up the way a new admin would: land on `/agents`, connect Power BI / Fabric / a database, and try to get an agent answering questions. Here is what that felt like, before any individual bug.

**There are two different nouns for the same mental model, and the UI never explains the difference.** You "Add a connection," but that does *not* give you an agent — after successfully connecting SQLite (test passed, 3 tables discovered), the page still said *"Connect your data to create your first agent"* and the agent list was empty. A connection is a data source; an **agent** is a separate object that *wraps* one or more connections and adds table-selection, instructions, tools. Nothing on screen tells you that. So the first-run user reasonably thinks "I connected my data, why is there no agent?" (Finding **B**.)

**The final button of the connect flow is labelled "Connect" — while the header next to it already says "Connected".** After schema discovery finishes, the modal shows a green *Connected* badge and a blue *Connect* button whose real job is "Done / finish." You connect, it says connected, and then it asks you to connect. (Finding **A**.)

**A working connection can report itself as "Disconnected."** Open a Power BI connection that uses user-level auth (OBO): the service principal has already indexed 27 model tables, the modal literally says *"Discovered 27 model tables,"* and yet **Status: Disconnected (red)**, the identity toggle defaults to **"Me,"** and it prompts *"Connect your account to query as yourself."* The status line describes *your personal* sign-in state, not the connection's health — but a viewer can't tell that, so a perfectly healthy connection looks broken. (Finding **C**.)

**The vocabulary for "the things inside a data source" is inconsistent across the page.** The connection modal is shape-aware — it says **Tables** for SQL, **Files** for a folder, **Collections** for Mongo/Elastic, **Tools** for MCP. But the *agent* view directly below hard-codes **"N tables · N tools · N files · N instructions"** for *every* data source. So a folder-of-files agent shows **"0 tables … 0 files"** at the same time (screenshot 10b), and a Mongo agent would say "tables" for what the connection modal calls "Collections." The word for the same concept changes depending on which panel you're looking at. This is the exact "sometimes files, sometimes tables, sometimes objects, not consistent" the report was about. (Finding **D**.)

**"Refresh the catalog" has three different names and two different scopes, all stacked in one modal.** On a user-auth connection the Connection Detail modal can show **Reindex** (service-principal, admin, shared catalog), **Reload my tables** (your own OBO creds, your personal overlay), and the identity toggle **Service account / Me** — plus **Test**, **Edit**, **Auto-reindex schedule**, and **Request rate limit** — all in one scrolling dialog. Each is defensible alone; together, in one modal, it's hard to know which button refreshes *what* for *whom*. (Finding **E**.)

**You cannot save a connection you can't test, and testing may cost money.** *Save and Continue* is hard-disabled until a **Test connection** succeeds, and the Test button's own tooltip warns *"Regular charges may occur."* For a connector the environment can't reach on the test port (our Fabric case — SQL/TDS :1433), there is no way to persist the connection through the UI at all, and no "save anyway / save as draft." (Finding **F**.)

Individually these are small. Together they mean the first ten minutes of connecting data require the user to hold two object models, three refresh verbs, and a status line that means something other than what it says. The detailed findings below are ordered by how much they hurt that first-run.

---

## 2. Environment & fixtures built

- **Connections created (8):** `SQLite Demo (system)` ✓ tested (3 tables) · `PowerBI SP (system)` ✓ tested (27 tables) · `PowerBI (user required)` OBO · `Fabric (user required)` OBO · `Docs Folder (files)` network_dir ✓ (3 files) · `SQLite Extra 1/2/3`. A **system_only Fabric** could not be created through the UI (see Finding F/G).
- **Agents created (22):** 20 seeded across SQLite/PBI (mixed public/private), plus `Docs Agent` (files) and `Shared PBI Agent` (public, on the OBO connection) for the member/OBO path.
- **Roles:** `admin@example.com` (admin) · `member@example.com` (member role: reports+files+view_members, **no** create_data_source/manage_connections).
- **Real services:** Power BI + Azure AD reachable over HTTPS and fully exercised; **Fabric SQL (:1433) is blocked by the sandbox egress policy** (HTTPS-only), so Fabric connectivity itself is untestable here — a *sandbox* limitation, but it surfaced two real product behaviors (F, G).
- Screenshots: `docs/feedback-loops/conn-audit/screens/*.png` (27 shots, referenced per finding).

---

## 3. Findings

Severity: **P1** data-loss/security/crash · **P2** control broken/blocking · **P3** works-but-wrong/misleading · **P4** cosmetic/copy.

### Naming & mental model
- **[D · P2] The "count" nouns are inconsistent between the connection modal and the agent view.** A files/objects/tools data source still shows "N **tables**" in the agent header and tree; a files agent shows "0 tables" *and* "0 files" simultaneously. → *Appendix D*
- **[B · P2] Creating a connection does not create an agent, and the UI implies it should.** After a successful connect the page still shows the empty "create your first agent" state; the connection↔agent relationship is never explained. → *Appendix B*
- **[A · P3] Terminal step of Add-Connection says "Connect" while already showing "Connected."** The button means "Finish/Done." → *Appendix A*
- **[H · P4] "Data Agents" vs "agent" vs "Data Agent" vs "connector" naming drifts** across the detail modal ("Data Agents" row), the wizard ("Create **Data Agent**"), and the tree (agent + "connector" badge). → *Appendix H*

### OBO / user-auth (the highest-confusion cluster)
- **[C · P2] A healthy user-auth connection reports "Disconnected."** Status + default "Me" identity + "Connect your account" all key off the viewer's personal sign-in, not the connection; the SP had already discovered 27 tables. Reads as broken. → *Appendix C*
- **[E · P3] Reindex vs Reload my tables vs Service account/Me are stacked in one modal with no scoping cue.** Two refresh verbs (shared vs personal) + identity switch + Test + schedule + rate-limit in one dialog. → *Appendix E*
- **[I · P3] Member cannot open a shared OBO agent to see what it is before signing in.** The agent row is disabled behind a "Sign in" badge; you must connect creds before you can inspect the agent. → *Appendix I*

### Create / test / save flow
- **[F · P2] "Save & Continue" is hard-gated on a passing Test, with no save-as-draft; Test warns it "may incur charges."** A reachable-but-not-from-here connector (Fabric TDS :1433) can never be saved via UI. → *Appendix F*
- **[G · P2] Creating a `system_only` connection blocks the HTTP request on synchronous schema discovery with no timeout.** The Fabric create request hung the full TDS timeout; `user_required` (no SP indexing at create) returned instantly. An unreachable warehouse = a hung Save. → *Appendix G*
- **[J · P3] Backend/driver exceptions are surfaced verbatim to the user.** `Unable to load data source client for ms_fabric: libodbc.so.2: cannot open shared object file` (test) and `ElasticsearchClient.__init__() missing 1 required positional argument: 'host'` (create) reach the UI/API as raw text. → *Appendix J*

### Role gating (verified correct — recorded as coverage)
- **[K · ok] Member correctly loses connection controls.** No "Add connection / New agent / Connect data / Connect Git"; only public agents; CONNECTIONS strip is read-only. Matches the RBAC matrix. → *Appendix K*

---

## Appendix

### A — "Connect" button on an already-"Connected" step
- **Repro:** Add connection → SQLite → path `…/demo_sales.db` → Test → Save & Continue → schema-discovery step. Header shows green **Connected** badge; footer shows blue **Connect** button.
- **Screens:** `05c_sqlite_after_save.png`, `06c_powerbi_indexing.png`.
- **Root cause:** `AddConnectionModal.vue` step `'indexing'`: header badge `data.connected` (line ~214) alongside the `finishConnect()` button labelled `data.connect` (line ~255). `finishConnect` only emits `created` + closes — it is a "Done" action mislabelled "Connect".

### B — Connection created ≠ agent created
- **Repro:** From empty org, Add connection → SQLite → finish. `GET /api/connections` = 1, `GET /api/data_sources` = 0. `/agents` still renders the empty state "Connect your data to create your first agent."
- **Screens:** `05c` (finish), `06z` (still-empty agents), `09c` (wizard is a *separate* path).
- **Root cause:** connections and data_sources (agents) are distinct entities (`data_source.connection_ids`, `DataSourceCreate` "three modes"). `finishConnect` never creates a data_source. No UI copy bridges the two.

### C — Healthy OBO connection shows "Disconnected"
- **Repro:** Open `PowerBI (user required)` detail as admin. Panel shows **Tables 27**, **Discovered 27 model tables · 6s**, **Last indexed 1m ago** — and **Status: Disconnected**, identity toggle defaulted to **Me**, "Connect your account to query as yourself."
- **Screen:** `08a_conn_detail_pbi_obo.png`.
- **Root cause:** `ConnectionDetailModal.vue` `queryIdentity` computed defaults to `'self'` (line 583-586); `isConnected`/status reflect the viewer's personal credential state, not the SP-indexed catalog. The two states share one "Status" line with no distinction between "connection healthy" and "you haven't linked your account."

### D — Count noun inconsistency (the core report)
- **Repro:** `Docs Folder (files)` connection detail → count label reads **Files** (correct). Open **Docs Agent** (built on that files connection) → header count row reads **"0 tables · 0 tools · 0 files · 0 instructions."**
- **Screens:** `10a_conn_detail_files.png` (says *Files*), `10b_agent_detail_files.png` (says *0 tables*).
- **Root cause:** modal is shape-aware — `ConnectionDetailModal.vue:643-648` maps `objects→Collections`, `files→Files`, `tools→Tools`, else `Tables`. The agent view hard-codes the nouns — `KnowledgeExplorer.vue:401-404` emits `countTables / countTools / countFiles / countInstructions` regardless of `data_shape`. Also note `objects` renders as **"Collections"** in the modal but the internal shape is `objects` — a third word for the same thing.

### E — Refresh verbs & identity stacked in one modal
- **Repro:** `PowerBI (user required)` detail as admin shows, top-to-bottom: Reindex; Auto-reindex schedule (Interval/At-time, "every 12 hours"); Request rate limit; **Run queries as: Service account / Me**; **Connect** (self) / (when linked) **Reload my tables** + Disconnect; then **Test**, **Edit**, **Delete Connection**.
- **Screen:** `08a`.
- **Root cause / behavior:** `reindex()` → `POST /connections/{id}/reindex` (shared SP catalog, line 959); `reloadMySchema()` → `POST /connections/{id}/my-schema/refresh` (personal overlay, line 1112). Same "circular-arrow" icon and near-identical placement; the scope difference (everyone vs just-me) is only conveyed by the small label text.

### F — Save gated on a passing (billable) Test; no draft
- **Repro:** Fabric form fully filled → **Test connection** → hangs then errors (TDS :1433 unreachable here). **Save and Continue** stays `disabled`. Test tooltip: *"Regular charges may occur."* No alternative to persist.
- **Screens:** `04c_fabric_form_filled.png`, `04d_fabric_test_result.png` (Testing… + "Regular charges may occur").
- **Root cause:** `ConnectForm.vue:162-167` — submit `:disabled="submitting || !connectionTestPassed"`; test button carries `data.testCharges` tooltip (line 150). No save-without-test / draft path.

### G — `system_only` create blocks on synchronous schema discovery
- **Repro:** `POST /api/connections` with a `ms_fabric` `system_only` body → request hangs ~full TDS timeout (client ReadTimeout at 25s); the same body as `user_required` returns `200` immediately (no SP indexing at create time).
- **Evidence:** seeding log — `Fabric SP (system) → TIMEOUT`, `Fabric (user required) → 200`.
- **Root cause:** connection create runs schema discovery inline for system_only before responding (`connection.py` create → indexing). Unreachable/slow warehouse ⇒ hung Save with no client-visible timeout or progress. (In the UI the mandatory Test partly masks this; via API and on slow-but-reachable warehouses it's exposed.)

### H — Naming drift for the agent object
- "Data Agents" (count row, `ConnectionDetailModal.vue:50`), "Create **Data Agent**" (wizard title, `09c`), "agent" everywhere else, plus a **connector** badge on some agents (`KnowledgeExplorer.vue:173`). Four labels for two concepts.

### I — Member can't inspect a shared OBO agent pre-sign-in
- **Repro:** As member, `Shared PBI Agent` shows a **Sign in** badge and the row is disabled (`needsSignIn` ⇒ `:disabled`); you must OBO-connect before you can open the agent to see its tables/instructions.
- **Screen:** `12a_member_shared_obo_agent.png`. **Root cause:** `KnowledgeExplorer.vue:173` TreeGroup `:disabled="needsSignIn(agent)"`.

### J — Raw exceptions surfaced to user/API
- **Test:** `Unable to load data source client for ms_fabric: libodbc.so.2: cannot open shared object file` (before ODBC driver installed).
- **Create:** `POST /api/connections` (elasticsearch, wrong config key) → `500`/detail `ElasticsearchClient.__init__() missing 1 required positional argument: 'host'` — an internal `TypeError` returned instead of a validation error.
- **Impact:** leaks internals; unhelpful to the user. Should be a friendly "couldn't connect / invalid configuration" message.

### K — Member role gating (correct — coverage record)
- **Repro:** member on `/agents`: header has no New/Connect Git; body has no Add connection / New agent / Connect data; only 7 public agents visible; CONNECTIONS strip present but read-only; shared OBO agent gated behind "Sign in."
- **Screens:** `11a_agents_member.png`, `12a`. Matches `member` permission set (no `create_data_source`/`manage_connections`). No leak of admin-only controls observed.

---

## Coverage notes / what to test next
- **Onboarding connect step** (`/onboarding/data`) reuses the same `ConnectForm` (`pages/onboarding/data/index.vue:72`), so Findings A/F/J apply there too — but I could not capture it live: public sign-up is disabled once an org exists, and the seeded admin has already dismissed onboarding. Needs a fresh invited user to screenshot.
- **Objects/`Collections`** noun (Finding D) is confirmed in code and in the modal mapping; a live Elasticsearch/Mongo connection wasn't stood up (no reachable cluster) — the "Collections" label is code-verified, not screenshotted.
- **MCP / Custom-API / Browser** tool-connector forms and the custom-queries/RLS editor were not driven button-by-button.
- **Fabric end-to-end** (query/RLS) is not testable from this sandbox (:1433 egress) — only the form/validation paths were exercised.
