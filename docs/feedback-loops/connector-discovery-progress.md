# Feedback loop — connector discovery progress

Discovery previously returned complete catalogs while most connectors emitted no progress. Power BI consequently showed start/end messages; PostgreSQL exposed intermediate metadata work. This change extends the existing callback pipeline across every registered catalog connector without using `connection_table` rows as a progress counter.

## Validated cause

- The pre-change callback contract check found six incompatible calls across CloudWatch, Prometheus, Jaeger, S3, and Monday: their arguments did not match `(phase, current_item, done, total)`. Error handling concealed the failures.
- SharePoint Lists accepted the callback but never invoked it.
- Forty-seven registered catalog types lacked an explicit discovery callback. Others had partial coverage or inherited it.
- The indexing service throttled writes, but still scheduled a coroutine for every callback. A large column catalog could therefore enqueue far more work than the UI needed.

## Implementation

`backend/app/data_sources/clients/progress.py` provides a shared lifecycle and lazy iteration helpers. Per-discovery context isolates simultaneous connections; helper calls reuse existing metadata loops and results. Unknown totals remain indeterminate, and final counts reflect completed work. Fixed-definition and live-only connectors emit lifecycle events without inventing metadata work.

SQL clients report metadata reads and column processing; shared FK reflection reports schema batches. Power BI reports workspace listings, model inspection, assembly, reused models, and unreadable model counts. Existing file and observability discovery keeps its detail with a compatible callback. Cancellation propagates through fallback handlers, and concurrent BI clients cancel queued futures. Already-running external requests must finish before their workers can exit.

The data flow remains:

```text
get_schemas(progress_callback)
  -> per-run latest state + sampled event buffer
  -> ConnectionIndexing row (at most one scheduled flush, 250 ms interval)
  -> existing indexing endpoint / UI polling
  -> current stage, count, metadata identifier, previous stages, expandable logs
```

Large known catalogs report percentage milestones or time-based updates, plus stage boundaries. Small catalogs retain item callbacks. The persisted log remains capped at 200 entries; it is a sampled activity history, not a second inventory of every object. Completion/error/cancellation force-flush the latest progress. No extra discovery API or SQL queries were introduced.

## Deterministic loops

Run from `backend` with the existing development environment:

```sh
TESTING=true .venv/bin/python -m pytest \
  tests/unit/test_discovery_progress_contract.py \
  tests/unit/test_aws_cloudwatch_client.py \
  tests/unit/test_s3_client.py tests/unit/test_graph_list_client.py \
  --db=sqlite -q
```

The initial contract reproduction failed on the incompatible callbacks. After correction this boundary run passed **129 tests**, with one existing skip. It covers public connector entry points, concurrent callback isolation, reporting failures, exact completion counts, and cancellation. Driver/HTTP boundary comparisons verify identical discovery results and request counts with reporting enabled or disabled, including 10,000 SQL Server tables and 35,000 Snowflake tables.

The earlier core run passed **153 tests**, including Power BI behavior, PostgreSQL, Snowflake semantic views, and indexing API flows. The broad serial stage passed **759 tests** before switching to parallel execution. The remaining suites and targeted reruns passed **740 tests**, including Documentum and Kubernetes. These counts overlap and must not be summed. Final metadata-name/count refinements passed **146 tests**, including the 10k/35k driver and cancellation checks. Final CloudWatch, Elasticsearch, and OpenSearch checks passed **85 tests**.

Documentum tests start a local mock HTTP server, so they need permission to bind a loopback port. The first sandboxed broad run encountered that environmental restriction. A Kubernetes assertion also assumed that every callback was a CRD item; it was updated to require actual discovered CRD detail while allowing lifecycle events.

## UI evidence

With the frontend preview running on port 3100:

```sh
cd frontend
node tests/data_sources/connector-discovery-progress.mjs
```

This harness renders the real `ConnectionIndexingProgress` component with synthetic old/new payloads. It checks all ten locales, Hebrew RTL, 35k-item progress, the bounded viewport for 200 events, and narrow-screen overflow. It does not save connections or use live credentials.

- [Previous start-only payload](../../media/pr/connector-discovery-progress/before.png)
- [Power BI stages](../../media/pr/connector-discovery-progress/powerbi-stages.png)
- [Large catalog](../../media/pr/connector-discovery-progress/large-catalog.png)
- [Hebrew](../../media/pr/connector-discovery-progress/powerbi-he.png)
- [Mobile](../../media/pr/connector-discovery-progress/mobile.png)
- [Stage and locale flow](../../media/pr/connector-discovery-progress/flow.gif)

## Live confirmation — 2026-09-12

The user authorized read-only tests against their Power BI tenant and Employees SharePoint site. Credentials were not saved in the repository or included in logs. No product connection was created or saved. For reproduction, supply tenant/client/secret through environment variables and construct the real clients; call `test_connection()` and then `get_schemas(progress_callback=...)`.

| Connector | Connection test | Discovery | Callback observations |
|---|---|---|---|
| Power BI | Passed | 19 model tables, 118 columns; 3 workspaces, 9 models inspected | 54 callbacks, including workspace/model stages; 2 listed models could not be introspected |
| SharePoint | Passed | 10 files, recursive metadata discovery | 17 callbacks, including folder listing and file indexing |

These observations verify this tenant and site, not every customer's permissions or data shape. The two unreadable Power BI models remain diagnostics; progress instrumentation does not change their access or fabricate tables.

## Catalog coverage

All **69 registered catalog types** accept the indexing callback and can cancel before external discovery begins. Tools-only connectors are outside this catalog contract. The following phase inventory is derived from each client's discovery method and reachable client helpers. All entries also receive `discovering_schema` and `catalog_ready`; SQL clients using the shared FK helper additionally report `relationship_schemas`.

| Connector type | Existing-work phases | Implementation |
|---|---|---|
| `postgresql` | `materialized_views`, `metadata_fallback`, `processing_columns`, `reading_columns`, `relationships` | [postgresql_client.py](../../backend/app/data_sources/clients/postgresql_client.py#L326) |
| `sqlite` | `tables` | [sqlite_client.py](../../backend/app/data_sources/clients/sqlite_client.py#L170) |
| `oracledb` | `columns`, `metadata_fallback`, `reading_columns` | [oracledb_client.py](../../backend/app/data_sources/clients/oracledb_client.py#L291) |
| `sap_hana` | `columns`, `metadata_fallback`, `reading_columns` | [sap_hana_client.py](../../backend/app/data_sources/clients/sap_hana_client.py#L241) |
| `sap_datasphere` | `assets` | [sap_datasphere_client.py](../../backend/app/data_sources/clients/sap_datasphere_client.py#L210) |
| `businessobjects` | `universes` | [businessobjects_client.py](../../backend/app/data_sources/clients/businessobjects_client.py#L224) |
| `sap_bw` | `catalogs`, `cubes` | [xmla_base.py](../../backend/app/data_sources/clients/xmla_base.py#L271) |
| `snowflake` | `columns`, `metadata_fallback`, `reading_columns`, `reading_semantic_views`, `semantic_views` | [snowflake_client.py](../../backend/app/data_sources/clients/snowflake_client.py#L521) |
| `bigquery` | `columns`, `datasets`, `metadata_fallback`, `reading_columns` | [bigquery_client.py](../../backend/app/data_sources/clients/bigquery_client.py#L225) |
| `netsuite` | `columns`, `tables` | [netsuite_client.py](../../backend/app/data_sources/clients/netsuite_client.py#L94) |
| `mysql` | `columns`, `metadata_fallback`, `reading_columns` | [mysql_client.py](../../backend/app/data_sources/clients/mysql_client.py#L182) |
| `aws_athena` | `catalog_pages`, `tables` | [aws_athena_client.py](../../backend/app/data_sources/clients/aws_athena_client.py#L320) |
| `mariadb` | `columns` | [mariadb_client.py](../../backend/app/data_sources/clients/mariadb_client.py#L136) |
| `salesforce` | `Indexing Salesforce objects` | [salesforce_client.py](../../backend/app/data_sources/clients/salesforce_client.py#L609) |
| `servicenow` | `tables` | [servicenow_client.py](../../backend/app/data_sources/clients/servicenow_client.py#L524) |
| `monday` | `boards` | [monday_client.py](../../backend/app/data_sources/clients/monday_client.py#L650) |
| `priority_erp` | `entities`, `tables` | [priority_erp_client.py](../../backend/app/data_sources/clients/priority_erp_client.py#L357) |
| `appdynamics` | `applications` | [appdynamics_client.py](../../backend/app/data_sources/clients/appdynamics_client.py#L394) |
| `zabbix` | `tables` | [zabbix_client.py](../../backend/app/data_sources/clients/zabbix_client.py#L289) |
| `kubernetes` | `custom resources` | [kubernetes_client.py](../../backend/app/data_sources/clients/kubernetes_client.py#L1253) |
| `aria_operations` | `adapter kinds` | [aria_operations_client.py](../../backend/app/data_sources/clients/aria_operations_client.py#L459) |
| `elasticsearch` | `aliases`, `dashboard_spaces`, `indices`, `saved_search_spaces` | [elasticsearch_client.py](../../backend/app/data_sources/clients/elasticsearch_client.py#L968) |
| `splunk` | `schema` | [splunk_client.py](../../backend/app/data_sources/clients/splunk_client.py#L660) |
| `MSSQL` | `columns`, `metadata_fallback`, `reading_columns` | [mssql_client.py](../../backend/app/data_sources/clients/mssql_client.py#L357) |
| `clickhouse` | `columns`, `metadata_fallback`, `reading_columns` | [clickhouse_client.py](../../backend/app/data_sources/clients/clickhouse_client.py#L180) |
| `trino` | `columns` | [trino_client.py](../../backend/app/data_sources/clients/trino_client.py#L109) |
| `azure_data_explorer` | `schema`, `tables` | [azure_data_explorer_client.py](../../backend/app/data_sources/clients/azure_data_explorer_client.py#L207) |
| `pinot` | `tables` | [pinot_client.py](../../backend/app/data_sources/clients/pinot_client.py#L150) |
| `druid` | `columns` | [druid_client.py](../../backend/app/data_sources/clients/druid_client.py#L270) |
| `aws_cost` | Lifecycle only: fixed definition or no shared catalog | [aws_cost_client.py](../../backend/app/data_sources/clients/aws_cost_client.py#L35) |
| `aws_cloudwatch` | `log_groups`, `metrics` | [aws_cloudwatch_client.py](../../backend/app/data_sources/clients/aws_cloudwatch_client.py#L422) |
| `vertica` | `columns`, `metadata_fallback`, `reading_columns` | [vertica_client.py](../../backend/app/data_sources/clients/vertica_client.py#L222) |
| `teradata` | `columns` | [teradata_client.py](../../backend/app/data_sources/clients/teradata_client.py#L195) |
| `aws_redshift` | `columns`, `metadata_fallback`, `reading_columns` | [aws_redshift_client.py](../../backend/app/data_sources/clients/aws_redshift_client.py#L491) |
| `tableau` | `dataset_schemas`, `datasets` | [tableau_client.py](../../backend/app/data_sources/clients/tableau_client.py#L180) |
| `duckdb` | `tables` | [duckdb_client.py](../../backend/app/data_sources/clients/duckdb_client.py#L256) |
| `mongodb` | `collections` | [mongodb_client.py](../../backend/app/data_sources/clients/mongodb_client.py#L272) |
| `opensearch` | `aliases`, `indices` | [opensearch_client.py](../../backend/app/data_sources/clients/opensearch_client.py#L332) |
| `posthog` | Lifecycle only: fixed definition or no shared catalog | [posthog_client.py](../../backend/app/data_sources/clients/posthog_client.py#L218) |
| `prometheus` | `metrics` | [prometheus_client.py](../../backend/app/data_sources/clients/prometheus_client.py#L340) |
| `jaeger` | `services` | [jaeger_client.py](../../backend/app/data_sources/clients/jaeger_client.py#L254) |
| `databricks_sql` | `columns`, `metadata_fallback`, `reading_columns` | [databricks_sql_client.py](../../backend/app/data_sources/clients/databricks_sql_client.py#L210) |
| `spark_connect` | `databases`, `tables` | [spark_connect_client.py](../../backend/app/data_sources/clients/spark_connect_client.py#L398) |
| `powerbi` | `admin_scan`, `assembling_models`, `inspected_models`, `listing_workspaces`, `model_introspection`, `model_metadata`, `relationships`, `reused_models`, `unreadable_models`, `workspace_models`, `workspace_reports` | [powerbi_client.py](../../backend/app/data_sources/clients/powerbi_client.py#L1362) |
| `powerbi_report_server` | `kpis`, `listing`, `pbix_reports`, `rdl_reports`, `shared_datasets` | [powerbi_report_server_client.py](../../backend/app/data_sources/clients/powerbi_report_server_client.py#L874) |
| `network_dir` | `indexing files` | [network_dir_client.py](../../backend/app/data_sources/clients/network_dir_client.py#L771) |
| `s3` | `files` | [s3_client.py](../../backend/app/data_sources/clients/s3_client.py#L656) |
| `qvd` | `qvd_files` | [qvd_client.py](../../backend/app/data_sources/clients/qvd_client.py#L714) |
| `pbix` | `pbix_files` | [pbix_client.py](../../backend/app/data_sources/clients/pbix_client.py#L409) |
| `csv` | `csv_files` | [csv_client.py](../../backend/app/data_sources/clients/csv_client.py#L215) |
| `qlik_sense` | `applications` | [qlik_sense_client.py](../../backend/app/data_sources/clients/qlik_sense_client.py#L367) |
| `qlik_sense_onprem` | `applications` | [qlik_sense_onprem_client.py](../../backend/app/data_sources/clients/qlik_sense_onprem_client.py#L1155) |
| `documentum` | `listing Documentum folders` | [documentum_client.py](../../backend/app/data_sources/clients/documentum_client.py#L610) |
| `sharepoint_onprem` | `files`, `listing SharePoint libraries` | [sharepoint_onprem_client.py](../../backend/app/data_sources/clients/sharepoint_onprem_client.py#L430) |
| `sharepoint` | `indexing files`, `listing folders` | [graph_drive_client.py](../../backend/app/data_sources/clients/graph_drive_client.py#L1136) |
| `sharepoint_lists` | `lists` | [graph_list_client.py](../../backend/app/data_sources/clients/graph_list_client.py#L239) |
| `onedrive` | `indexing files`, `listing folders` | [graph_drive_client.py](../../backend/app/data_sources/clients/graph_drive_client.py#L1136) |
| `onenote` | `indexing pages`, `listing sections` | [graph_onenote_client.py](../../backend/app/data_sources/clients/graph_onenote_client.py#L716) |
| `outlook_mail` | Lifecycle only: fixed definition or no shared catalog | [graph_mail_client.py](../../backend/app/data_sources/clients/graph_mail_client.py#L111) |
| `gmail_mail` | Lifecycle only: fixed definition or no shared catalog | [gmail_mail_client.py](../../backend/app/data_sources/clients/gmail_mail_client.py#L277) |
| `google_drive` | `files`, `listing_files` | [google_drive_client.py](../../backend/app/data_sources/clients/google_drive_client.py#L390) |
| `ms_fabric` | `columns`, `metadata_fallback`, `reading_columns` | [ms_fabric_client.py](../../backend/app/data_sources/clients/ms_fabric_client.py#L397) |
| `sybase` | `columns` | [sybase_client.py](../../backend/app/data_sources/clients/sybase_client.py#L136) |
| `timbr` | `concepts` | [timbr_client.py](../../backend/app/data_sources/clients/timbr_client.py#L213) |
| `timbr_a2a` | Lifecycle only: fixed definition or no shared catalog | [timbr_a2a_client.py](../../backend/app/data_sources/clients/timbr_a2a_client.py#L178) |
| `sisense` | `models` | [sisense_client.py](../../backend/app/data_sources/clients/sisense_client.py#L219) |
| `oracle_bi` | `subject_areas` | [oracle_bi_client.py](../../backend/app/data_sources/clients/oracle_bi_client.py#L185) |
| `infor_olap` | `catalogs`, `cubes` | [xmla_base.py](../../backend/app/data_sources/clients/xmla_base.py#L271) |
| `analysis_services` | `catalogs`, `cubes` | [analysis_services_client.py](../../backend/app/data_sources/clients/analysis_services_client.py#L637) |
