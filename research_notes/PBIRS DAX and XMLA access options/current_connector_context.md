# Context: what the bagofwords codebase does today (from code reading, not web research)

Source: `backend/app/data_sources/clients/powerbi_report_server_client.py` (1707 lines), `pbix_common.py`, `xmla_base.py`, `analysis_services_client.py`, `schemas/data_sources/configs.py` (PowerBIReportServerConfig: server_url, verify_ssl, ca_bundle_path; credentials: username, password, domain), registry entry `powerbi_report_server`, `docs/powerbi-report-server-plan.md`.

## PBIRS connector (PowerBIReportServerClient)
- REST API v2.0 at `{server}/Reports/api/v2.0`, NTLM via `requests_ntlm.HttpNtlmAuth`, single `requests.Session`, one serial warm-up GET /System before parallel calls (ThreadPoolExecutor, 6-8 workers).
- Discovery endpoints used: /System, /Folders, /CatalogItems, /PowerBIReports, /Reports, /Datasets, /DataSources, /Kpis, /PowerBIReports({id})/DataSources, /DataModelParameters, /DataModelRoles, /Reports({id})/DataSources, /ParameterDefinitions, /Datasets({id})/Model.GetSchema (fallback /Schema), /Datasets({id})/ParameterDefinitions, /CatalogItems({id})/Content/$value.
- NOT used: CacheRefreshPlans (and /History, Model.Execute), DataModelRoleAssignments, Policies, Model.SearchItems, $filter/$top paging, ProductType detection, /DataSets({Id})/Data, Model.GetAggregatedValue, Model.CheckDataSourceConnection, ExcelWorkbooks.
- PBIX path: downloads the whole .pbix via Content/$value (cap 200 MB), parses with pbixray (schema, relationships, dax_measures), writes each table to Parquet under uploads/pbix_data_cache keyed by (report_id, ModifiedDate), then runs the LLM's DuckDB SQL over the Parquet views. Measures are exposed as text only; system prompt tells the LLM to rewrite DAX as SQL. There is a scheduled warm-up job (`warm_all_pbirs_caches`). Cache invalidation key is the catalog item's ModifiedDate.
- RDL path: `/Reports({id})/Export/{fmt}` is tried first (this endpoint does not exist in the REST spec), then falls back to URL access `/ReportServer?{path}&rs:Format=CSV&rs:Command=Render`.
- Shared datasets: POST `/Datasets({id})/Model.GetData` with `{Parameters:[...], maxRows}` and parses the result as CSV/JSON heuristically (spec says response is JSON `{Name, Columns, Rows}`).
- Row-level security: not enforced; whoever's NTLM credentials are configured can download the pbix and read all rows.

## Existing XMLA capability (reusable)
- `XmlaClient` (xmla_base.py): XMLA SOAP over HTTP with Basic auth only (`session.auth = (user, pass)`), Discover (DBSCHEMA_CATALOGS, MDSCHEMA_*), Execute with Format=Tabular, rowset parsing, `_xHHHH_` column decoding, SOAP fault and inline <Error> handling.
- `AnalysisServicesClient` (SSAS): adds Tabular detection via TMSCHEMA_MODEL probe, read-only Tabular discovery via DISCOVER_CSDL_METADATA (physical tables, columns, measures, relationships -> foreign keys), TMSCHEMA enrichment when admin, live-verified DAX execution against SSAS Tabular over msmdpump.dll. Does not support NTLM/Kerberos, EffectiveUserName, or Roles/CustomData properties.
- Cloud `PowerBIClient` uses the Power BI REST `executeQueries` for DAX; it is the blueprint the PBIRS connector was meant to mirror.
