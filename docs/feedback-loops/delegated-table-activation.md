# Feedback Loop — Delegated (user_required) sources: agent table activation was ignored

Reported as: "is it possible that a user wrote a prompt on an agent with Power BI
semantic-model tables, and `create_data` ran on tables it can't use (i.e. not
activated in the agent)?" Yes — and not only for Power BI. On every connection
with **Require user authentication** (`auth_policy=user_required`, ~50
connectors) the agent served a signed-in user **every table their own
credentials could reach upstream**, ignoring the agent manager's table
selection. Power BI additionally let a generated DAX query *run* against a
non-activated table even when the prompt did not show it.

Validated live in a fresh cloud sandbox on 2026-09-08 against a local
PostgreSQL 16 (four DB roles with different grants) and a real Entra / Power BI
tenant (service principal + two delegated users). Before/after numbers below
come from the same sandbox database, run once against the unpatched code and
once against the fix.

---

## Root cause (validated)

Activation is `DataSourceTable.is_active`, written by the tables wizard
(`update_table_status_in_schema`). Three consumers ignored it for a delegated
user:

1. **Schema context (planner + `create_data` resolution).**
   `SchemaContextBuilder.build` takes the per-user overlay branch when
   `_resolve_user_access` → `"user"`. That branch selected
   `UserDataSourceTable.is_accessible == True` and then hard-coded
   `canonical_is_active = True` — the canonical flag was dismissed as
   "unreliable for a user_required source". That was a workaround from when a
   user-discovered model had no canonical row at all (nothing to activate, agent
   empty). `_upsert_user_overlay` has since created that canonical row on every
   sync (union), so the wizard can activate it — but the builder still never
   looked. `CreateDataTool._resolve_active_tables` uses the same builder, so it
   resolved non-activated tables too.

2. **Query-time target map (Power BI / Analysis Services).**
   `_attach_stored_table_metadata` handed the client the table → dataset-GUID
   map with **no `is_active` filter** (explicitly, for the same historical
   reason), and merged the user's overlay metadata for every accessible name.
   `execute_query("…", "Model/Table")` therefore resolved a non-activated table,
   and a name missing from the map fell back to a **live tenant crawl**.

3. **DAX is dataset-scoped.** Activation is per `Dataset/Table`, but
   `executeQueries` takes a dataset GUID: a query whose *target* is activated
   could read any sibling table of the same model in its DAX body.

Every other surface already intersected the two: the tables wizard for a
signed-in user scopes to overlay rows linked to a canonical row and applies the
active filter for non-managers; activation writes only to the canonical row.

---

## The fix

**Rule: a table is in the agent for a user iff the user can reach it upstream
(overlay `is_accessible`) AND the manager activated it (canonical `is_active`).**

- `backend/app/ai/context/builders/schema_context_builder.py` — the overlay
  branch gates each overlay table on its canonical row's `is_active` when
  `active_only` (default). A missing canonical row counts as not activated. The
  relationship-target set is recomputed from what survives, so an FK never
  points at a hidden table. With `active_only=False` (management surfaces) the
  row is emitted flagged inactive, like the service-account path.
- `backend/app/services/data_source_service.py`
  - `_attach_stored_table_metadata`: only activated rows resolve a query
    target; the overlay may enrich an activated name but never adds a
    non-activated one. Non-activated rows go to clients exposing
    `attach_blocked_table_metadata` (Power BI).
  - `read_user_data_source_schema(active_only=…)`: `get_data_source_schema`
    (mentions, `/schema`) honours `include_inactive=False` on the overlay path;
    `_get_prompt_schema` is active-only.
- `backend/app/data_sources/clients/powerbi_client.py`
  - once the platform attached the activated map, a name outside it is
    refused instead of live-crawled (`_table_metadata_attached`);
  - `attach_blocked_table_metadata` + `_assert_dax_tables_activated`: a DAX
    body referencing a non-activated table of the target dataset is refused
    (`'Quoted Name'`, `EVALUATE T`, `T[col]`, `FUNC(T, …)`; string literals
    ignored) with the activated tables listed in the error, so the coder can
    rewrite.

Unit tests: `tests/unit/test_overlay_inactive_canonical_context.py` (the three
user stories, FK pruning, `active_only=False`),
`tests/unit/test_attach_table_metadata_delegated.py`,
`tests/unit/test_powerbi_client.py::TestActivationEnforcement`.

---

## The user stories (what each user's agent contains)

| Story | Manager activated | User reaches upstream | Agent shows |
|---|---|---|---|
| S1 admin activates 3, user1 reaches 2 of them | customers, orders, employees | customers, orders, products, invoices | **customers, orders** |
| S2 user1 builds on 4, user2 reaches 10 | customers, orders, products, invoices | all 10 | **the 4** |
| S3 same agent, user3 reaches 0 | the 4 | none | **nothing** |

---

## The loop (Postgres, live)

```bash
# Postgres 16 on :5433, demo_db with 10 tables; roles bow_admin (10), bow_user1 (4),
# bow_user2 (10), bow_user3 (0). Backend + frontend per sandbox-feedback-loop.
# Setup is API-only (scratchpad/setup_pg2.py): register admin + 3 invited members,
# Anthropic provider, S1 = admin creates a user_required Postgres connection
# (system creds bow_admin), POST /connections/{id}/refresh, activates 3 tables;
# admin grants user1 `create_data_sources` on the connection, user1 creates S2 on it,
# activates 4; each user POSTs /data_sources/{id}/my-credentials (their own role).

# GET /data_sources/{id}/schema per user (active-only, mentions surface)
S1 admin ['public.customers', 'public.employees', 'public.orders']
S1 user1 ['public.customers', 'public.orders']
S2 user1 ['public.customers', 'public.invoices', 'public.orders', 'public.products']
S2 user2 ['public.customers', 'public.invoices', 'public.orders', 'public.products']
S2 user3 []

# SchemaContextBuilder.build() per user — what the planner sees (scratchpad/builder_check.py)
OLD S1 user1  access=user   planner sees 4: ['public.customers', 'public.invoices', 'public.orders', 'public.products']
NEW S1 user1  access=user   planner sees 2: ['public.customers', 'public.orders']
OLD S2 user2  access=user   planner sees 10: [... every table bow_user2 can read ...]
NEW S2 user2  access=user   planner sees 4: ['public.customers', 'public.invoices', 'public.orders', 'public.products']
OLD/NEW S2 user3 access=user planner sees 0: []

# CreateDataTool._resolve_active_tables per user, requesting
# ['public.customers', 'public.employees', 'public.products']
S1 user1 -> resolves ['public.customers']                      (employees: not reachable; products: not activated)
S2 user2 -> resolves ['public.customers', 'public.products']   (employees: readable by bow_user2, NOT activated)
S2 user3 -> resolves []  warnings=['No active tables matched patterns ...']
```

Real chats (Claude 4.5 Haiku, Playwright-driven):

- **S2 / user2** "List every table you can query here and give the row count of
  each" → customers 40, invoices 150, orders 200, products 30 — "420 total rows
  across 4 tables". Not the 10 the DB role can read.
- **S2 / user2** "How many employees are there, and what is their average
  salary?" → "there's no employees or salary table visible in the schema" and
  lists the 4 tables. `public.employees` is readable by `bow_user2`; the agent
  did not see or query it.
- **S1 / user1** "Which tables are available to you? Count the rows" →
  `public.customers` 40, `public.orders` 200 — "2 tables available".
- **S2 / user3** "How many customers are there?" → asks which data source to
  query; no table was available.

---

## The loop (Power BI, real tenant)

```bash
# Admin creates "PBI demo": type powerbi, service-principal creds, auth_policy=user_required,
# allowed_user_auth_modes=["oauth"]; POST /connections/{id}/refresh → 19 model tables
# across 7 semantic models indexed by the SP. Admin activates 3:
#   FleetOps/dim_vehicle, SalesPush/Sales, shared_orders/Orders
# demo1 → BOW user1 and demo2 → BOW user2 sign in through /connections/{id}/oauth/authorize
# (Playwright drives login.microsoftonline.com). Post-OAuth overlay sync:
user1 overlay: 19 accessible  (incl. rls_sales/Sales, which the SP cannot see; NOT shared_orders/Orders)
user2 overlay: 20 accessible  (incl. shared_orders/Orders)
canonical: 20 rows, 3 active; rls_sales/Sales created inactive by user1's sync (discovered_by=user)

# GET /data_sources/{id}/schema
admin ['FleetOps/dim_vehicle', 'SalesPush/Sales', 'shared_orders/Orders']
user1 ['FleetOps/dim_vehicle', 'SalesPush/Sales']
user2 ['FleetOps/dim_vehicle', 'SalesPush/Sales', 'shared_orders/Orders']
user3 []

# Query-time enforcement, real executeQueries, per-user delegated client
# (scratchpad/pbi_enforce.py → DataSourceService.construct_clients + execute_query)
[activated target              ] SalesPush/Sales      -> OK 2 rows
[non-activated target          ] SalesPush/Customers  -> REFUSED "Table 'SalesPush/Customers' is not activated for this agent ... Activated tables in this semantic model: SalesPush/Sales."
[non-activated sibling in body ] EVALUATE SUMMARIZE(Customers, ...) via SalesPush/Sales
                                                      -> REFUSED "DAX references table(s) not activated for this agent: SalesPush/Customers ..."
[user-reachable, not activated ] rls_sales/Sales      -> REFUSED (both users)
[activated, user1 no upstream  ] shared_orders/Orders -> user1: HTTP 401 from Power BI (delegated token gates it); user2: OK 2 rows
```

Real chats (Claude 4.5 Haiku, Playwright-driven, real executeQueries):

- **demo1 / user1** "Which Power BI tables can you query here? List them and give
  the row count of each" → "two Power BI tables": FleetOps/dim_vehicle 3 rows,
  SalesPush/Sales 300 rows. demo1 reaches 19 model tables upstream; the agent
  shows the 2 that are both activated and reachable.
- **demo2 / user2** same prompt → "three Power BI tables": dim_vehicle 3,
  Sales 300, shared_orders/Orders 6.
- **demo2 / user2** "How many customers are in the SalesPush Customers table?"
  → "I don't see a 'Customers' table in the SalesPush dataset" and lists the 3
  activated tables. `SalesPush/Customers` exists, is in demo2's overlay, and
  sits in the same semantic model as the activated `SalesPush/Sales` — it was
  neither shown nor queried.
- **demo1 / user1** "What is the total amount in the rls_sales Sales table?" →
  answers from `SalesPush/Sales` (the only Sales table in the agent) and says
  so; `rls_sales/Sales` (reachable by demo1, not activated) was not queried.

---

## Screenshots (assets/delegated-table-activation/)

- `pg_s1_user1_agent.png` — S1 as user1: Tables shows only `public.customers`, `public.orders` (2 of admin's 3).
- `pg_s2_user2_agent.png` — S2 as user2: "4 tables" although `bow_user2` reads 10.
- `pg_s2_user2_chat_tables.png` — user2 row counts: exactly the 4.
- `pg_s2_user2_chat_employees.png` — user2 asks for employees/salary: "no employees or salary table visible".
- `pg_s1_user1_chat_tables.png` — user1 on S1: "2 tables available".
- `pg_s2_user3_chat.png` — user3: nothing to query.
- `pbi_user1_chat_tables.png` — demo1: FleetOps/dim_vehicle (3 rows), SalesPush/Sales (300 rows) — 2 of the 3 activated.
- `pbi_user2_chat_tables.png` — demo2: "three Power BI tables available".
- `pbi_user2_chat_customers.png` — demo2 asks for SalesPush Customers: not in the agent.
- `pbi_user1_chat_rls.png` — demo1 asks for rls_sales Sales: answered from the activated Sales table.
- `pbi_user1_agent.png`, `pbi_user2_agent.png`, `pbi_user3_agent.png` — the agent page per user (2 / 3 / sign-in required).
