# Feedback Loop — RBAC, agents, training, evals, dashboard sharing, full-stack e2e

Full simulation of a real multi-team deployment, run 2026-08-08 in a cloud
sandbox against a fresh local stack (backend `:8000`, frontend `:3000`,
sqlite, sandbox enterprise license, real Anthropic API for every LLM call).
Driven almost entirely through the real UI via Playwright; a handful of
direct API calls were used only for adversarial/robustness probes (invalid
input, permission-boundary checks, destructive-action checks) called out
explicitly below.

## What was set up (all through the UI unless noted)

- Admin signup, onboarding skip, Anthropic provider (`Claude 4.5 Haiku`
  only) added and tested in `/settings/models`.
- SQLite **Chinook** demo agent ("Music Store") installed one-click from the
  connector catalog — 11 tables discovered.
- **Power BI** connection created with a real service-principal against a
  real Azure/Entra tenant — 19 live model tables discovered, agent built
  with all tables active.
- Microsoft **Fabric** warehouse connection attempted with real credentials
  — blocked by this sandbox's network (raw TDS/1433 egress refused) and a
  missing ODBC driver; see "Environment constraints" below. Not a product
  bug.
- Custom RBAC role **Agent-Builders**: zero org-wide permissions, only
  `create_data_sources` resource grants scoped to the Power BI and SQLite
  Chinook connections — created via Settings → Roles, verified against
  `resource_grants`.
- Two more custom roles (**Data Steward**: org-wide `manage_instructions`
  + `manage_evals`; **Viewer**: baseline only) and **6 groups** (Data Team,
  Finance, Sales, Engineering, Executive, Support).
- **20 org members** bulk-invited via the real CSV import UI
  (`Settings → Members → Import`), distributed across the 6 groups and 5
  roles (admin/member/Agent-Builders/Data Steward/Viewer) via the per-row
  role dropdown and per-group "Add member" dialogs.
- Three of those members registered via their real invite links and used
  in their own logged-in sessions for permission verification (carla:
  Agent-Builders, grace: Viewer/query-only, henry: manage).
- Agent instructions created directly and via a real **training session**
  (`mode=training` report) — the AI proposed two instructions from natural
  chat prompts; one was **accepted**, one **rejected** through the real
  hunk-review UI (`/instructions/{id}/hunks/accept-all` and `reject-all`).
- The Power BI agent **shared** with `grace` (query-only) and `henry`
  (manage) via the agent Settings → Members dialog.
- An **eval suite** ("Core Queries") on the Chinook agent with two real
  LLM-judged test cases — one designed to pass, one designed to fail —
  both run for real against Claude 4.5 Haiku: **Success (1/1)** and
  **Failed (0/1)** respectively.
- A **dashboard** built live from a chat report (KPI tile + bar chart over
  real Chinook data), shared with a group and a user via the real
  ShareModal, and given a recurring **cron schedule** ("Daily at 8:00 AM")
  via the CronModal — confirmed visible from the shared user's own session
  under Dashboards → "Shared with me".
- A round of adversarial/robustness probes: duplicate role/group names,
  re-importing the same member CSV (idempotency), a malformed CSV, an
  XSS-payload role name, license seat-cap enforcement at the exact limit,
  and deleting a connection that's actively in use by two agents.

## Environment constraints hit (not product bugs)

- **Raw TDS (port 1433) egress blocked** by this sandbox's network proxy —
  confirmed via a direct `/dev/tcp` check against the real Fabric warehouse
  hostname before even touching the UI. Live Fabric SQL is therefore
  unreachable from this environment regardless of driver availability.
  Matches the identical finding in
  `entra-sso-fabric-powerbi-obo-e2e.md` from a prior run against the same
  tenant.
- This bare `uv sync` dev checkout has **no ODBC stack** (`unixodbc`,
  `msodbcsql`) pre-installed — the production Docker image installs it
  explicitly (`Dockerfile:14,127-133`); a dev sandbox boot doesn't.
- The **vendored JS libraries** dashboards/artifacts render with (React,
  ReactDOM, Tailwind, Babel-standalone, ECharts, pdf.js) are deliberately
  git-ignored under `frontend/public/libs/` and fetched by
  `scripts/download-vendor-libs.sh` — a step this sandbox's initial boot
  skipped, causing every dashboard to fail with "React is not defined"
  until the script was run. **Worth adding to the `sandbox-feedback-loop`
  skill's boot steps** so future runs don't lose time here.
- Headless Chromium cannot reach `login.microsoftonline.com` from this
  sandbox (`ERR_CONNECTION_RESET`), so interactive "Sign in with Microsoft"
  per-user OAuth flows can't be driven by Playwright here — matches the
  prior documented finding.
- The "Master PBI" service-principal secret supplied for this run was
  **expired** on the real Azure tenant (`AADSTS7000222`), surfaced verbatim
  and correctly by the product's own Test Connection error message. Worked
  around with the other supplied Entra app, whose secret was valid and
  which also had Power BI read access.

## Bugs found

| # | Severity | Summary |
|---|---|---|
| 1 | Medium | `POST .../roles` and `POST .../groups` with a duplicate name crash with an unhandled `500` + raw traceback (SQL params included) instead of a clean `409` |
| 2 | Medium | `DELETE /api/connections/{id}` hard-deletes every dependent agent with zero confirmation/dry-run; leaves their instructions and eval suites/cases as DB orphans; a dashboard built from the deleted agent's queries silently sticks on "Loading…" forever with no error surfaced |
| 3 | Medium | `BOW_ENCRYPTION_KEY` silently mints a fresh random key on every process start when unset — invalidates all sessions and (in a longer-lived deployment) irrecoverably corrupts every encrypted secret at rest, with no warning logged |
| 4 | Low/Medium | Training-session chat sidebar labels an AI-proposed instruction "Accepted" before any human reviewed it, while the authoritative agent Instructions panel correctly still shows "Pending review" — two parts of the UI disagree about review state |
| 5 | Low | `NameError: name '_mlog' is not defined` in `agent_v2.py` (two call sites reference a helper that's actually local to a different method) — at one call site this masks a **successful** commit as a false "commit failed" in the error log |
| 6 | Low | Dashboard/report thumbnail generation 404s — it launches Playwright's `chromium_headless_shell` channel specifically, a different binary from the general-purpose `chromium` this sandbox has installed |
| 7 | Low | AI-generated dashboard's bar chart shows "$0" on every value label after a real self-heal fixed a `.toFixed()` crash — bar heights and the KPI tile are correct, only the per-bar text label is wrong (LLM-code-generation variance, not deterministic) |
| 8 | Cosmetic | `[Vue warn]` on every `/agents` page load: `TraceModal` receives `reportId=null` where a `String` is expected |

Full repro steps, request/response bodies, and DB verification queries for
every item above are in the session's working notes; the shapes are
reproducible from the one-liners in the table.

## What worked exactly as designed

- **RBAC resource scoping**, both UI and API layers: a user with a role
  granting `create_data_sources` on exactly two connections saw only those
  two connections in the "create agent" flow, successfully created an
  agent on one of them, and got a real `403` from the API when attempting
  anything outside that scope (creating an unrelated connection).
- **Agent sharing** (query vs. manage): a query-only member's direct API
  attempt to write an instruction to the shared agent got `403`; a
  manage-permission member's identical call succeeded.
- **License seat cap**: bulk-importing past the licensed `max_users` limit
  created members up to exactly the cap and cleanly errored the rest,
  dry-run and real run alike, with zero side effects from the dry run.
- **CSV import idempotency**: re-importing the identical file reported
  every row `unchanged`, no duplicates created.
- **XSS safety**: a role named `<script>alert(1)</script>` rendered as
  inert text everywhere in the UI — no dialog fired, no unescaped tag in
  the DOM.
- **Evals**: a real LLM-judged pass and a real LLM-judged fail both ran
  end-to-end and recorded correctly, including triggering a genuine
  "Smart" self-learning instruction suggestion as a side effect of running
  the eval.
- **Dashboard self-heal**: the product's own "Fix Error" button correctly
  diagnosed and iteratively repaired two consecutive real failures in an
  AI-generated dashboard (a missing-dependency crash, then a genuine
  `.toFixed()` bug in its own generated code) across three real Anthropic
  round-trips, converging on a working dashboard.

## Repro pointers

- Sandbox boot: `uv sync --extra dev` (backend) + `yarn install` (frontend)
  per the `sandbox-feedback-loop` skill, **plus**
  `bash scripts/download-vendor-libs.sh` (not currently in the skill —
  needed for any dashboard/artifact rendering) and a pinned
  `BOW_ENCRYPTION_KEY` across restarts (see bug #3).
- Enterprise features (custom roles/groups) needed a license:
  `backend/scripts/gen_sandbox_license.py <max_users>` mints a throwaway
  signed license + swaps the verification pubkey for local-only testing;
  restore `app/ee/license_public_key.pem` from its `.orig` backup before
  committing anything.
- All Playwright driver scripts and screenshots for this run were session
  scratchpad tooling, not repo code.
