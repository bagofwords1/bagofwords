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
3. On **Column access**, inspect the amount column’s audience. In **Preview as**, select the first member and run the preview. Select **Compare another member** and run the second preview. Both panels must show their assigned company and only the first panel should have an amount column.
4. Open **Review activation** and inspect the pending tables and historical-content warning. Opening review alone must leave the active policy unchanged.
5. Switch to Hebrew and repeat navigation in a 390-pixel viewport. The editor should stack its panes and scroll vertically.

The prior editor put connection settings, table rules and member comparisons in one scrolling form. It required a delimited string for conditional column visibility. The signed-in browser run confirmed these controls are now independently accessible. It also caught a blank current-user selector, corrected before capture.

[Catalog](../../media/pr/live-access-rules/catalog-redesign.png) · [Connection mapping](../../media/pr/live-access-rules/connection-mapping-redesign.png) · [Row editor](../../media/pr/live-access-rules/row-access-redesign.png) · [Column editor](../../media/pr/live-access-rules/column-access-redesign.png) · [Recorded flow](../../media/pr/live-access-rules/editor-flow.gif)
