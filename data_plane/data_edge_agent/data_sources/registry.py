"""Connection type → client class.

The edge agent reuses the control plane's real data-source clients rather than
reimplementing them (design B1, Step 5): `backend/app/data_sources/clients/*.py`
is on `PYTHONPATH`, so any on-prem type Bow supports — postgres, mysql, mssql,
oracle, clickhouse, mongodb, … — is served over the tunnel by the same tested
client, with no per-type code here.

Resolution goes through the backend registry when it can be imported, and falls
back to the bundled minimal clients otherwise (a stripped image, or a driver the
backend registry's config imports can't load). The type strings must match
`backend/app/schemas/data_source_registry.py` exactly.
"""

from __future__ import annotations

import logging
from typing import Any, Type

from .base import DataSourceClient
from .postgresql_client import PostgresqlClient

logger = logging.getLogger(__name__)

# Bundled fallbacks: used only when the backend client cannot be imported.
_FALLBACK: dict[str, Type[Any]] = {
    "postgresql": PostgresqlClient,
}

# type -> "module.Class", mirroring backend resolve_client_class (client_path or
# its dynamic {type}_client.{Title}Client convention). Extracted from
# backend/app/schemas/data_source_registry.py so the strings stay in step. We
# import the client MODULE directly rather than the backend registry, which
# transitively imports app.settings (fastapi_mail, pydantic-settings, …) and a
# whole config surface the edge agent has no reason to carry (design's
# "no import drags in app.models" check).
_CLIENT_PATHS = {
    'MSSQL': 'app.data_sources.clients.mssql_client.MSSQLClient',
    'analysis_services': 'app.data_sources.clients.analysis_services_client.AnalysisServicesClient',
    'appdynamics': 'app.data_sources.clients.appdynamics_client.AppdynamicsClient',
    'aws_athena': 'app.data_sources.clients.aws_athena_client.AwsAthenaClient',
    'aws_cost': 'app.data_sources.clients.aws_cost_client.AwsCostClient',
    'aws_redshift': 'app.data_sources.clients.aws_redshift_client.AwsRedshiftClient',
    'azure_data_explorer': 'app.data_sources.clients.azure_data_explorer_client.AzureDataExplorerClient',
    'bigquery': 'app.data_sources.clients.bigquery_client.BigqueryClient',
    'browser': 'app.data_sources.clients.browser_client.BrowserClient',
    'businessobjects': 'app.data_sources.clients.businessobjects_client.BusinessobjectsClient',
    'clickhouse': 'app.data_sources.clients.clickhouse_client.ClickhouseClient',
    'csv': 'app.data_sources.clients.csv_client.CSVClient',
    'custom_api': 'app.data_sources.clients.custom_api_client.CustomApiClient',
    'databricks_sql': 'app.data_sources.clients.databricks_sql_client.DatabricksSqlClient',
    'druid': 'app.data_sources.clients.druid_client.DruidClient',
    'duckdb': 'app.data_sources.clients.duckdb_client.DuckDBClient',
    'elasticsearch': 'app.data_sources.clients.elasticsearch_client.ElasticsearchClient',
    'gmail_mail': 'app.data_sources.clients.gmail_mail_client.GmailMailClient',
    'google_drive': 'app.data_sources.clients.google_drive_client.GoogleDriveClient',
    'infor_olap': 'app.data_sources.clients.infor_olap_client.InforOlapClient',
    'jaeger': 'app.data_sources.clients.jaeger_client.JaegerClient',
    'mariadb': 'app.data_sources.clients.mariadb_client.MariadbClient',
    'mcp': 'app.data_sources.clients.mcp_client.McpClient',
    'monday': 'app.data_sources.clients.monday_client.MondayClient',
    'mongodb': 'app.data_sources.clients.mongodb_client.MongodbClient',
    'ms_fabric': 'app.data_sources.clients.ms_fabric_client.MsFabricClient',
    'mysql': 'app.data_sources.clients.mysql_client.MysqlClient',
    'netsuite': 'app.data_sources.clients.netsuite_client.NetsuiteClient',
    'network_dir': 'app.data_sources.clients.network_dir_client.NetworkDirClient',
    'onedrive': 'app.data_sources.clients.graph_drive_client.OnedriveClient',
    'onenote': 'app.data_sources.clients.onenote_client.OnenoteClient',
    'opensearch': 'app.data_sources.clients.opensearch_client.OpenSearchClient',
    'oracle_bi': 'app.data_sources.clients.oracle_bi_client.OracleBIClient',
    'oracledb': 'app.data_sources.clients.oracledb_client.OracledbClient',
    'outlook_mail': 'app.data_sources.clients.outlook_mail_client.OutlookMailClient',
    'pbix': 'app.data_sources.clients.pbix_client.PBIXClient',
    'pinot': 'app.data_sources.clients.pinot_client.PinotClient',
    'postgresql': 'app.data_sources.clients.postgresql_client.PostgresqlClient',
    'posthog': 'app.data_sources.clients.posthog_client.PostHogClient',
    'powerbi': 'app.data_sources.clients.powerbi_client.PowerBIClient',
    'powerbi_report_server': 'app.data_sources.clients.powerbi_report_server_client.PowerbiReportServerClient',
    'priority_erp': 'app.data_sources.clients.priority_erp_client.PriorityErpClient',
    'prometheus': 'app.data_sources.clients.prometheus_client.PrometheusClient',
    'qlik_sense': 'app.data_sources.clients.qlik_sense_client.QlikSenseClient',
    'qlik_sense_onprem': 'app.data_sources.clients.qlik_sense_onprem_client.QlikSenseOnpremClient',
    'qvd': 'app.data_sources.clients.qvd_client.QVDClient',
    's3': 'app.data_sources.clients.s3_client.S3Client',
    'salesforce': 'app.data_sources.clients.salesforce_client.SalesforceClient',
    'sap_bw': 'app.data_sources.clients.sap_bw_xmla_client.SapBwXmlaClient',
    'sap_datasphere': 'app.data_sources.clients.sap_datasphere_client.SapDatasphereClient',
    'sap_hana': 'app.data_sources.clients.sap_hana_client.SapHanaClient',
    'servicenow': 'app.data_sources.clients.servicenow_client.ServicenowClient',
    'sharepoint': 'app.data_sources.clients.sharepoint_client.SharepointClient',
    'sharepoint_lists': 'app.data_sources.clients.sharepoint_lists_client.SharepointListsClient',
    'sisense': 'app.data_sources.clients.sisense_client.SisenseClient',
    'snowflake': 'app.data_sources.clients.snowflake_client.SnowflakeClient',
    'spark_connect': 'app.data_sources.clients.spark_connect_client.SparkConnectClient',
    'splunk': 'app.data_sources.clients.splunk_client.SplunkClient',
    'sqlite': 'app.data_sources.clients.sqlite_client.SqliteClient',
    'sybase': 'app.data_sources.clients.sybase_client.SybaseClient',
    'tableau': 'app.data_sources.clients.tableau_client.TableauClient',
    'teradata': 'app.data_sources.clients.teradata_client.TeradataClient',
    'timbr': 'app.data_sources.clients.timbr_client.TimbrClient',
    'timbr_a2a': 'app.data_sources.clients.timbr_a2a_client.TimbrA2aClient',
    'trino': 'app.data_sources.clients.trino_client.TrinoClient',
    'vertica': 'app.data_sources.clients.vertica_client.VerticaClient',
    'zabbix': 'app.data_sources.clients.zabbix_client.ZabbixClient',
}


def _backend_resolve(type_name: str):
    """Import the real backend client class for a type, or None.

    Guarded: a client module may need a driver absent from a minimal image, or
    pull in something unimportable here. A miss is not fatal — the caller falls
    back to a bundled client if one exists.
    """
    path = _CLIENT_PATHS.get(type_name)
    if path is None:
        return None
    module_path, _, class_name = path.rpartition(".")
    try:
        from importlib import import_module
        return getattr(import_module(module_path), class_name)
    except Exception as e:  # ImportError, missing driver
        logger.debug("registry.backend_resolve_failed type=%s err=%s", type_name, e)
        return None


def resolve_client_class(type_name: str) -> Type[Any]:
    cls = _backend_resolve(type_name)
    if cls is not None:
        return cls
    if type_name in _FALLBACK:
        return _FALLBACK[type_name]
    raise ValueError(
        f"unsupported data source type {type_name!r}: backend client could not "
        f"be resolved and no bundled fallback exists"
    )


def construct_client(type_name: str, params: dict[str, Any]) -> Any:
    """Build a client, passing only what its constructor accepts.

    Config and credentials are merged by the caller and arrive as one dict, so
    narrowing here is what lets a connection carry agent-side keys the client
    itself knows nothing about.
    """
    import inspect

    cls = resolve_client_class(type_name)
    sig = inspect.signature(cls.__init__)
    accepts_kwargs = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if accepts_kwargs:
        allowed = params
    else:
        allowed = {k: v for k, v in params.items() if k in sig.parameters and k != "self"}
    return cls(**allowed)


def supported_types() -> list[str]:
    # Best-effort: the bundled fallbacks plus, when the backend registry is
    # importable, everything it lists.
    types = set(_FALLBACK)
    try:
        from app.schemas.data_source_registry import list_types  # type: ignore
        types.update(list_types())
    except Exception:
        pass
    return sorted(types)
