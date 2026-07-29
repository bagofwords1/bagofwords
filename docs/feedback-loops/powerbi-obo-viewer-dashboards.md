# Feedback Loop — Power BI OBO per-viewer shared dashboards (real tenant)

Live validation against `bow14.onmicrosoft.com` in a cloud sandbox, extending
`powerbi-obo-rls.md`. This run proves the **per-viewer shared-dashboard**
feature end to end on a real RLS Power BI source: a dashboard shared with
viewer identity auto-runs under each viewer's own OBO token, and each viewer
sees only their RLS slice — never the owner's snapshot, and never each other's.

Everything below was measured, not inferred.

## Setup (mostly through the product UI)

- Org scaled to **100 members** (incl. local `demo1@example.com` /
  `demo2@example.com`) and **17 agents** across varied connection types
  (Postgres, Snowflake, BigQuery, MySQL, DuckDB, MSSQL, ClickHouse, Redshift,
  Trino, Oracle, …) — a realistic multi-agent tenant, not a single-source toy.
- **Power BI connection created in the UI**: master service principal as
  system credentials, "Require user authentication" on, OAuth app = the login
  app. The live Test Connection returned *"Connected successfully. Found 6
  model tables."* An agent (`bow14 PBI`) was created over it and its tables
  activated — all through the wizard.
- Tenant fixtures reused / rebuilt: `rls_sales` (RLS roles `USOnly`/`EMEAOnly`)
  and `shared_orders` (item-shared to demo2, Build, no workspace role). The
  item-share workspace was rebuilt this run (`obo-itemshare2`, dataset
  `shared_orders`, 6 rows / 13 600) since the prior one had been cleaned up.
- Enterprise features (Power BI is `requires_license="enterprise"`) exercised
  under a locally-minted sandbox license; a fixed `BOW_ENCRYPTION_KEY` so the
  out-of-process harnesses can decrypt the stored connection credentials.

`POST /v1.0/myorg/RefreshUserPermissions` was called for both demo users before
measuring (Power BI caches permissions — see the prior loop).

## Harnesses (all three, through the product client stack)

| | demo1 (Viewer + Build + USOnly) | demo2 (Build only + EMEAOnly) |
|---|---|---|
| OBO overlay (`e2e_powerbi_obo_rls.py`) | **7 tables**, incl. `rls_sales/Sales`, not `shared_orders/Orders` | **8 tables**, incl. `shared_orders/Orders` |
| DAX on `rls_sales/Sales` (`e2e_powerbi_rls_query.py`) | **3 rows, US, 9 000**, group-scoped (no fallback) | **3 rows, EMEA, 4 600**, tenant-level fallback |
| DAX on `shared_orders/Orders` | n/a | **6 rows**, tenant-level fallback (group-scoped 401) |
| overlay vs agent context (`e2e_powerbi_user_visibility.py`) | 7 / 7 | 8 / 8 |

Matches the prior loop's expected numbers exactly. (The two harnesses that pick
"first data source" gained a `BOW_RLS_DS` / `BOW_VIS_DS` env to name the agent —
in a 17-agent org the old `.first()` was a lottery.)

## Per-viewer shared dashboard — the new surface

A published dashboard `Sales by Region (Power BI RLS)` over the `bow14 PBI`
agent, one query executing real DAX (`EVALUATE Sales` on `rls_sales/Sales`,
aggregated to Region → Amount), shared **public + viewer identity**. Driven
through the real `/r/{id}` page with Playwright:

| Viewer | Auto-run gate → result | Rows |
|---|---|---|
| demo1 | auto-runs under demo1's OBO token → **US 9 000** | their RLS slice |
| demo2 | auto-runs under demo2's OBO token → **EMEA 4 600** | their RLS slice |
| anonymous | **sign-in gate** (Power BI brand icon), no rows, no SQL | withheld |

Confirmed identical at the HTTP layer (`POST /r/{id}/run` → `executed_as:
viewer`, `steps_succeeded: 1`; the public step endpoint returns each viewer's
own slice with `snapshot_withheld: false`, and for the anonymous reader
`snapshot_withheld: true` with empty `data` **and empty `code`**). The owner's
seeded snapshot (`(owner snapshot — not shared)`) never reached any viewer.

This is the whole per-viewer feature — auto-run on open, per-user
`step_user_results`, the withholding policy, and the state-specific gate —
working against a real RLS Power BI source with two real Entra identities on
disjoint slices of one shared dashboard.

Screenshots: `scratchpad/shots_pbi/` (connection test, agent, per-viewer
dashboards, anonymous sign-in gate).

## Notes

- RLS role membership still cannot be automated (service-layer permission,
  portal-only) — roles were assigned manually in the prior loop and persist.
- The `bow14 PBI` agent was made org-public and its user-contributed tables
  (`rls_sales/Sales`, `shared_orders/Orders`) activated by an admin — the same
  activation-is-a-gate behavior documented in the prior loop.
