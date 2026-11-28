# Verify Enterprise connection access rules

An Enterprise administrator can give two authenticated readers access to the same connection and table while granting them different companies and columns. Live queries evaluate warehouse grants at execution. Accelerated custom queries apply their own configured rules when reading the DuckDB artifact. Configure the rules on each surface. A warehouse entitlement table can itself lag the system that populates it.

## Reproduce and verify

1. Start an isolated BOW database at the current Alembic head. Enable the Enterprise RLS feature and the deployment setting `features.fabric_company_rls_activation` for this sandbox. Activation permanently withholds unverifiable historical material across this workspace. Use a disposable workspace.
2. In each disposable source create a base table `bow_sales(company VARCHAR(32), amount INTEGER, private_note VARCHAR(64))`. Insert sales for companies 101 and 202, with amounts 10 and 90 respectively. Create `bow_test_grants(employee VARCHAR(64), company VARCHAR(32), source_name VARCHAR(32))`. Grant one@example.com company 101 and two@example.com company 202 with source_name sales. Use the actual schema and identifier case reported by the database catalog.
3. Set `BOW_ACCESS_POSTGRESQL`, `BOW_ACCESS_MSSQL` and `BOW_ACCESS_ORACLEDB` to JSON client configurations supplied by the sandbox secret store. Set each corresponding `_TABLE` variable to the fully qualified sales table. Set `BOW_ACCESS_MUTABLE_FIXTURE=1` only on these disposable fixtures. The revocation test temporarily deletes a grant and restores it.
4. From backend run the native clients and the authenticated BOW management API together.

   ```sh
   python -m pytest -q tests/integrations/test_live_access_connectors.py tests/e2e/test_live_access_policy.py
   python -m pytest -q tests/unit/test_custom_query_cls.py tests/unit/test_custom_query_rls.py
   ```

5. Open **Connection → Access rules**. Check indexed connections at the catalog level, import the source table names, enter the entitlement table or member attribute, and save the draft. Open a table and select **Column access**. Enable column restrictions, select the visible columns, and choose the audience for each. A column omitted from this list remains hidden if it is later added to the source. Compare the two members before activation. Open connection query history as a full administrator and inspect the submitted statement, parameterized outbound statement and policy fingerprint.
6. For the Fabric signed-in chat, table and code previews, shared report and active-session revocation, follow [the Fabric scenario](fabric-company-rls.md). On the sales mapping explicitly list company_id for both readers and amount for the first reader. Running SELECT * through each bound Fabric client returned the corresponding company's rows and the two different projections. Remove a grant, read the same report and run a fresh query in the existing conversation, then restore the grant.

## Evidence and limits

| Surface | Result observed |
|---|---|
| PostgreSQL 14, SQL Server 2022, Oracle Free 23 | Six native tests and four API checks passed. Covered explicit column grants, exclusion of an unlisted physical column, warehouse revocation, member-attribute revocation, role checks and restricted history. API checks used a simulated licensing decision. |
| Fabric warehouse | Two signed-in Entra readers used Claude chat, previews and a shared report. Committed revocation affected fresh reads in an existing chat. A separate native query on the sales table confirmed company and column scope together. |
| Encrypted accelerated relations | Cached RLS and CLS, schema projection and related dashboard regressions passed in the local suite. |
| Configuration screenshots | Playwright opened the actual Manage connection modal from the signed-in Agents page, saved and restored a draft edit, and queried a disposable PostgreSQL source as two members. The sandbox supplied an Enterprise entitlement. Hebrew RTL and a 390-pixel viewport were checked. |
| Rollout | Activation defaults to off. Directory attribute policies need a fresh sync. An empty profile sync invalidates the prior profile for policy evaluation. The local suite covers loss of that grant and refusal of creator-identity sharing for a CLS-only cached source. Production identity-provider configuration, license issuance, large customer catalogs and revocation during an already executing statement require deployment-specific acceptance. |

The initial regression failed at policy validation because a table rule could not express a projection. Native Oracle checks then exposed two driver-specific issues: base table reflection in the SYSTEM tablespace and uppercase cursor metadata. The verified client queries Oracle's base-table catalog and preserves physical column names.

## Reproduce the editor flow

Seed a disposable PostgreSQL connection with an indexed sales table and grant table. Give two verified workspace members different companies. For the sales rule, grant the amount column only to the first member. Sign in as the administrator and open **Manage connection → Access rules** from the Agents page.

1. Search for the sales table. Open **Connection mapping**, select verified member email, and confirm the Entra tenant field disappears. Return to the catalog.
2. Open the sales table. On **Row access**, change the source value, save, restore the original and save again. Member previews must wait for the saved draft.
3. On **Column access**, inspect the amount column’s audience. In **Preview as**, select the first member and run the preview. Select the second member in the same picker. The previous result must clear immediately. Run the preview again. Each member should see their own company and only the first member should see the amount column.
4. Open **Review activation** and inspect the pending tables and historical-content warning. Opening review alone must leave the active policy unchanged.
5. Switch to Hebrew and repeat navigation in a 390-pixel viewport. The editor should stack its panes and scroll vertically.

The prior editor put connection settings, table rules and member comparisons in one scrolling form. It required a delimited string for conditional column visibility. The signed-in browser run confirmed these controls are now independently accessible. It also caught a blank current-user selector, corrected before capture.

### Verify a team-to-project lookup

Use a trusted member property such as `custom.teams`. Set it to `planning` for one member and `delivery` for another. Populate `security.team_grants(team_id, project_id)` with (planning, P-17) and (delivery, P-24). Index a target table containing records for both projects.

In **Connection mapping**, select the member property, matching grant column `team_id`, and granted value column `project_id`. Leave resource type off for this two-column table. On the target table choose **Row access → Column to filter → project_id**. Existing grants that use email and a resource-type column retain that interpretation.

Run a preview for each member using the same picker. The first read returned P-17 and the second returned P-24. Removing the second member’s trusted property produced **No grant** on the next read in the same session. Restoring the property restored P-24. Missing properties grant no rows. Without a resource type, each matching grant applies to every target table configured to use this lookup.

The first automated regression failed because the policy schema required an email-to-resource mapping with a resource type. The updated compiler and schema passed the legacy email tests, multi-value and missing-property tests, and read/probe bind checks for the four SQL dialects. This follow-up ran live PostgreSQL queries. The other three dialects were checked at compilation in this pass.

[Table catalog](../../media/pr/live-access-rules/catalog-redesign.png) · [Team-to-project mapping](../../media/pr/live-access-rules/grant-property-mapping.png) · [Single member preview](../../media/pr/live-access-rules/single-member-preview.png)

### Preview a wide table as a member

Use a PostgreSQL table with 1,000 columns, a project-level row grant, and 14 matching rows. Give the operations member access to 999 columns, including the row key `project_id` and a late column `review_notes`. Restrict `salary` to finance.

1. Open the table in **Access rules**, select the operations member, and run the preview. The summary reports 10 sampled rows and 999 visible columns. The grid starts with the row key and three other permitted columns.
2. Check `salary`. The result says the column is hidden. Neither the API response nor the protected statement in connection query history contains salary values. Check `review_notes`. The grid and the warehouse projection now contain just the row key and the requested column.
3. Point the member’s grant at a project with no activity. The preview reports no matching rows. Remove the grant and run again. The preview reports no grant. Both responses have zero rows.
4. Switch members and verify the previous result clears. Under **Column access**, search for a late column and check that the list displays at most 12 choices at once.

The regression starts with `cd backend && python -m pytest -q tests/unit/test_fabric_company_rls.py -k wide_preview`. The PostgreSQL browser and API checks above passed. The SQL protection and access-policy regression run passed 1,524 tests and skipped two optional native connector cases. The Hebrew preview also passed at 390 pixels wide.

[Preview flow](../../media/pr/live-access-rules/wide-preview.gif) · [Hidden column](../../media/pr/live-access-rules/wide-preview-hidden.png) · [Visible column](../../media/pr/live-access-rules/wide-preview-column.png) · [Hebrew preview](../../media/pr/live-access-rules/wide-preview-he.png)

### Protected SQL sessions and stored step ownership

Run the focused regressions from `backend` with `python -m pytest -q tests/unit/test_pg_session.py tests/unit/test_fabric_company_rls.py tests/unit/test_agent_step_persistence.py`. On the pre-fix revision, the protected PostgreSQL read reached the driver before confirming literal mode, the two T-SQL compiler checks failed, and the step-storage guard could not take a trusted workspace.

- **PostgreSQL.** Start a disposable source with `standard_conforming_strings=off`. Assign one of two companies to the reader and place the same note containing a backslash in both companies. A protected read for that note returned only the assigned company. The driver-boundary test also verifies refusal when the setting cannot be confirmed and invalidation when it changes during a failed read. Both the protected read and the grant probe now use the session checks in `clients/_protected_sql.py`.
- **SQL Server.** Put an uppercase GUID in a native `uniqueidentifier` column and the same spelling in a text column. Configure each as a row key. The lowercase attribute matched only the native key, the uppercase attribute matched both, and a value with an extra trailing space matched neither. The compiler checks the key type in the executing statement. SQL Server uses a Unicode comparison with the older BIN2 collation, and Fabric retains its UTF-8 collation. Verified on SQL Server 2022, including a database at compatibility level 140. The separate 2017 engine aborted at startup on the test host. Fabric SQL generation was tested on SQL Server 2022 in this pass.
- **Stored results.** A published copy resolves its workspace through its release when its query has no workspace. A detached step can use the server-side execution workspace. Tests check protected and unprotected workspaces, conflicting ownership, and absent or unknown workspace IDs. Unverified ownership continues to withhold the snapshot.
