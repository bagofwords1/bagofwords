"""Which data-source clients the read-only guard knows, identified by type.

One table, keyed by the defining class's `(__module__, __qualname__)`. A
client is looked up by walking its MRO, so a subclass inherits its parent's
entry and the most specific registered class wins (`PrestoClient` before
`TrinoClient`). Keys are strings, so this module imports no driver.

Never decide anything about a client from `type(client).__name__`: a subclass
with another name would slip past the check. The `family` string is for
display and for the identity-bind grammar choice only.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GuardProfile:
    """How the wrapper checks statements for one client family.

    kind: "sql" (parsed with `dialect`, a dialect sqlglot knows),
    "nonparsing_sql" (SQL without a parser dialect), "spl" (Splunk),
    "cypher" (Neo4j). The guard refuses every query to a client whose
    profile has any other kind.
    extra_kwargs: keyword arguments the client may receive besides the
    statement; any other keyword is refused.
    accepts_dict: the statement may be a dict envelope (checked by the client
    at its dispatch point).
    engine: for "nonparsing_sql", whose literal, comment and write-word
    rules the text is read with (one of `NONPARSING_ENGINES`); None for
    every other kind.
    unknown_lexer: for "sql", the engine is checked with `dialect` but its
    own lexer is not known, so the guard also refuses text whose reading
    turns on a backslash or on where a number ends.
    """

    family: str
    kind: str
    dialect: str | None
    extra_kwargs: frozenset[str] = field(default_factory=frozenset)
    accepts_dict: bool = False
    engine: str | None = None
    unknown_lexer: bool = False

    def __post_init__(self):
        if self.kind == "sql" and not _known_dialect(self.dialect):
            raise ValueError(f"{self.family}: a sql profile needs a dialect sqlglot knows, not {self.dialect!r}")
        if self.kind == "nonparsing_sql" and self.engine not in NONPARSING_ENGINES:
            raise ValueError(f"{self.family}: engine {self.engine!r} is not one of {sorted(NONPARSING_ENGINES)}")
        if self.kind != "nonparsing_sql" and self.engine is not None:
            raise ValueError(f"{self.family}: only a nonparsing_sql profile names an engine, not {self.engine!r}")


# The engines whose rules the guard reads SQL without a parser dialect with.
NONPARSING_ENGINES = frozenset({"db2", "vertica", "sybase", "hana", "pinot"})


def _known_dialect(name) -> bool:
    """Whether `name` is a dialect sqlglot can read: a "sql" profile without
    one would leave its client's queries unchecked."""
    if not isinstance(name, str) or not name:
        return False
    from sqlglot.dialects.dialect import Dialect

    try:
        Dialect.get_or_raise(name)
    except Exception:
        return False
    return True


@dataclass(frozen=True)
class ClientEntry:
    family: str
    profile: GuardProfile | None
    renderer: str | None = None


_CLIENTS = "app.data_sources.clients"


def _sql(module: str, name: str, dialect: str, extra_kwargs: frozenset[str] = frozenset(),
         renderer: str | None = None, unknown_lexer: bool = False):
    return (f"{_CLIENTS}.{module}", name), ClientEntry(
        family=name, profile=GuardProfile(family=name, kind="sql", dialect=dialect, extra_kwargs=extra_kwargs,
                                          unknown_lexer=unknown_lexer),
        renderer=renderer,
    )


def _entry(module: str, name: str, kind: str, renderer: str | None = None, **profile_kwargs):
    return (f"{_CLIENTS}.{module}", name), ClientEntry(
        family=name, profile=GuardProfile(family=name, kind=kind, dialect=None, **profile_kwargs),
        renderer=renderer,
    )


def _renders(module: str, name: str, renderer: str, package: str = _CLIENTS):
    """A client the guard does not check (yet) whose parameters render with
    `renderer` (a `query_params` renderer name)."""
    return (f"{package}.{module}", name), ClientEntry(family=name, profile=None, renderer=renderer)


_CLIENT_TABLE: dict[tuple[str, str], ClientEntry] = dict([
    _sql("postgresql_client", "PostgresqlClient", "postgres"),
    _sql("aws_redshift_client", "AwsRedshiftClient", "redshift"),
    _sql("mysql_client", "MysqlClient", "mysql"),
    _sql("mariadb_client", "MariadbClient", "mysql"),
    _sql("mssql_client", "MSSQLClient", "tsql"),
    _sql("ms_fabric_client", "MsFabricClient", "fabric"),
    _sql("oracledb_client", "OracledbClient", "oracle"),
    _sql("sqlite_client", "SqliteClient", "sqlite"),
    _sql("snowflake_client", "SnowflakeClient", "snowflake"),
    _sql("bigquery_client", "BigqueryClient", "bigquery",
         frozenset({"maximum_bytes_billed", "use_query_cache"})),
    _sql("aws_athena_client", "AwsAthenaClient", "athena"),
    _sql("presto_client", "PrestoClient", "presto"),
    _sql("trino_client", "TrinoClient", "trino"),
    _sql("clickhouse_client", "ClickhouseClient", "clickhouse"),
    _sql("databricks_sql_client", "DatabricksSqlClient", "databricks"),
    _sql("spark_connect_client", "SparkConnectClient", "spark"),
    _sql("duckdb_client", "DuckDBClient", "duckdb"),
    _sql("teradata_client", "TeradataClient", "teradata"),
    _sql("druid_client", "DruidClient", "druid"),
    # Timbr's own discovery SQL is Spark SQL (backtick names); its server's
    # lexer is not known.
    _sql("timbr_client", "TimbrClient", "spark", renderer="timbr", unknown_lexer=True),
    # SQL with no sqlglot dialect: checked without a parser.
    _entry("db2_client", "Db2Client", "nonparsing_sql", renderer="ansi", engine="db2"),
    _entry("ibmi_client", "IbmiClient", "nonparsing_sql", renderer="ansi", engine="db2"),
    _entry("vertica_client", "VerticaClient", "nonparsing_sql", renderer="ansi", engine="vertica"),
    _entry("sybase_client", "SybaseClient", "nonparsing_sql", renderer="ansi", engine="sybase"),
    _entry("sap_hana_client", "SapHanaClient", "nonparsing_sql", renderer="ansi", engine="hana"),
    _entry("pinot_client", "PinotClient", "nonparsing_sql", renderer="ansi", engine="pinot"),
    _entry("splunk_client", "SplunkClient", "spl", renderer="spl", accepts_dict=True),
    _entry("neo4j_client", "Neo4jClient", "cypher", extra_kwargs=frozenset({"params"})),
    # Parameter rendering only; these are not checked at the wrapper.
    _renders("netsuite_client", "NetsuiteClient", "ansi"),
    _renders("oracle_bi_client", "OracleBIClient", "ansi"),
    _renders("sisense_client", "SisenseClient", "ansi"),
    # OData options text, which the server percent-decodes.
    _renders("priority_erp_client", "PriorityErpClient", "odata"),
    _renders("sap_datasphere_client", "SapDatasphereClient", "odata"),
    _renders("mecm_client", "MecmClient", "odata"),
    # DuckDB file connections (PBIRS: the query its file route runs on DuckDB)
    # and accelerated custom tables.
    _renders("csv_client", "CSVClient", "duckdb"),
    _renders("qvd_client", "QVDClient", "duckdb"),
    _renders("pbix_client", "PBIXClient", "duckdb"),
    _renders("powerbi_report_server_client", "PowerBIReportServerClient", "duckdb"),
    _renders("fast_client", "FastQueryClient", "duckdb", package="app.data_sources.fast"),
    _renders("salesforce_client", "SalesforceClient", "soql"),
    # HogQL reads string literals like ClickHouse.
    _renders("posthog_client", "PostHogClient", "clickhouse"),
    _renders("azure_data_explorer_client", "AzureDataExplorerClient", "kql"),
])


# Clients that take query text positionally but declare no renderer: params
# with a positional statement are refused there (Neo4j's are its driver's
# own and never rendered). A declaration for the completeness test, keyed by
# the exact class, not an MRO lookup: a new subclass must be declared itself.
# The runtime refusal is `query_params.renderer_for(client) is None`.
def _refused(module: str, name: str, reason: str):
    return (f"{_CLIENTS}.{module}", name), reason


_JSON = "JSON query spec, not SQL text"
_MDX = "MDX / DAX query text with no declared renderer"
_FILE = "file or document id / JSON file spec, not query text"
_ID = "positional id, not query text"

PARAMS_REFUSED: dict[tuple[str, str], str] = dict([
    _refused("elasticsearch_client", "ElasticsearchClient", f"{_JSON}, elasticsearch_client.py:1136"),
    _refused("opensearch_client", "OpenSearchClient", f"{_JSON}, opensearch_client.py:367"),
    _refused("brocade_client", "BrocadeClient", f"{_JSON}, brocade_client.py:448"),
    _refused("appdynamics_client", "AppDynamicsClient", f"{_JSON}, appdynamics_client.py:458"),
    _refused("aria_operations_client", "AriaOperationsClient", f"{_JSON}, aria_operations_client.py:634"),
    _refused("azure_cost_client", "AzureCostClient", f"{_JSON}, azure_cost_client.py:698"),
    _refused("checkpoint_client", "CheckPointClient", f"{_JSON}, checkpoint_client.py:985"),
    _refused("cisco_apic_client", "CiscoApicClient", f"{_JSON}, cisco_apic_client.py:786"),
    _refused("crowdstrike_client", "CrowdStrikeClient", f"{_JSON}, crowdstrike_client.py:535"),
    _refused("dell_ome_client", "DellOmeClient", f"{_JSON}, dell_ome_client.py:464"),
    _refused("f5_bigip_client", "F5BigIpClient", f"{_JSON}, f5_bigip_client.py:301"),
    _refused("hpe_oneview_client", "HpeOneViewClient", f"{_JSON}, hpe_oneview_client.py:582"),
    _refused("intune_client", "IntuneClient", f"{_JSON}, intune_client.py:407"),
    _refused("jaeger_client", "JaegerClient", f"{_JSON}, jaeger_client.py:293"),
    _refused("kubernetes_client", "KubernetesClient", f"{_JSON}, kubernetes_client.py:1401"),
    _refused("monday_client", "MondayClient", f"{_JSON}, monday_client.py:680"),
    _refused("netapp_ontap_client", "NetAppOntapClient", f"{_JSON}, netapp_ontap_client.py:374"),
    _refused("servicenow_client", "ServiceNowClient", f"{_JSON}, servicenow_client.py:614"),
    _refused("graph_list_client", "SharepointListsClient", f"{_JSON}, graph_list_client.py:362"),
    _refused("spectrum_client", "SpectrumClient", f"{_JSON}, spectrum_client.py:658"),
    _refused("vcenter_client", "VCenterClient", f"{_JSON}, vcenter_client.py:805"),
    _refused("zabbix_client", "ZabbixClient", f"{_JSON}, zabbix_client.py:341"),
    _refused("mongodb_client", "MongodbClient", f"{_JSON} (aggregation pipeline), mongodb_client.py:156"),
    _refused("sap_bw_xmla_client", "SapBwXmlaClient", f"{_MDX}, inherited from xmla_base.py:460"),
    _refused("infor_olap_client", "InforOlapClient", f"{_MDX}, inherited from xmla_base.py:460"),
    _refused("analysis_services_client", "AnalysisServicesClient", f"{_MDX}, analysis_services_client.py:673"),
    _refused("powerbi_client", "PowerBIClient", f"{_MDX}, powerbi_client.py:2114"),
    _refused("infor_epm_client", "InforEpmClient", f"{_MDX}, infor_epm_client.py:447"),
    _refused("prometheus_client", "PrometheusClient", "PromQL with no declared renderer, prometheus_client.py:425"),
    _refused("aws_cloudwatch_client", "AwsCloudWatchClient",
             "Metrics / Logs Insights spec with no declared renderer, aws_cloudwatch_client.py:685"),
    _refused("businessobjects_client", "BusinessObjectsClient",
             "result-object list, not query text, businessobjects_client.py:338"),
    _refused("timbr_a2a_client", "TimbrA2aClient", "natural-language prompt, not query text, timbr_a2a_client.py:212"),
    _refused("gmail_mail_client", "GmailMailClient", "Gmail search text with no declared renderer, gmail_mail_client.py:412"),
    _refused("network_dir_client", "NetworkDirClient", f"{_FILE}, network_dir_client.py:1061"),
    _refused("git_repo_client", "GitHubRepoClient", f"{_FILE}, inherited from network_dir_client.py:1061"),
    _refused("git_repo_client", "GitLabRepoClient", f"{_FILE}, inherited from network_dir_client.py:1061"),
    _refused("git_repo_client", "BitbucketRepoClient", f"{_FILE}, inherited from network_dir_client.py:1061"),
    _refused("graph_drive_client", "OnedriveClient", f"{_FILE}, graph_drive_client.py:1529"),
    _refused("graph_drive_client", "SharepointClient", f"{_FILE}, graph_drive_client.py:1529"),
    _refused("graph_onenote_client", "OnenoteClient", f"{_FILE}, inherited from graph_drive_client.py:1529"),
    _refused("graph_mail_client", "GraphMailClient", f"{_FILE}, inherited from graph_drive_client.py:1529"),
    _refused("graph_teams_client", "GraphTeamsClient", f"{_FILE}, inherited from graph_drive_client.py:1529"),
    _refused("google_drive_client", "GoogleDriveClient", f"{_FILE}, google_drive_client.py:547"),
    _refused("s3_client", "S3Client", f"{_FILE}, s3_client.py:791"),
    _refused("documentum_client", "DocumentumClient", f"{_FILE}, documentum_client.py:629"),
    _refused("sharepoint_onprem_client", "SharepointOnpremClient", f"{_FILE}, sharepoint_onprem_client.py:393"),
    _refused("kms_lighthouse_client", "KmsLighthouseClient", f"{_FILE}, kms_lighthouse_client.py:2657"),
    _refused("qlik_sense_client", "QlikSenseClient", f"{_ID} (app), qlik_sense_client.py:478"),
    _refused("qlik_sense_onprem_client", "QlikSenseOnPremClient", f"{_ID} (app), qlik_sense_onprem_client.py:1300"),
    _refused("tableau_client", "TableauClient", f"{_ID} (datasource_luid), tableau_client.py:374"),
    _refused("aws_cost_client", "AwsCostClient", f"{_ID} (operation_name), aws_cost_client.py:149"),
    _refused("neo4j_client", "Neo4jClient",
             "native params: passed to the driver, never rendered into the text, neo4j_client.py:316"),
])


def client_entry(client) -> ClientEntry | None:
    """The entry of the most specific registered class in `client`'s MRO."""
    for cls in type(client).__mro__:
        entry = _CLIENT_TABLE.get((cls.__module__, cls.__qualname__))
        if entry is not None:
            return entry
    return None


def guard_profile(client) -> GuardProfile | None:
    entry = client_entry(client)
    return entry.profile if entry is not None else None


_FAMILIES = frozenset(entry.family for entry in _CLIENT_TABLE.values())


def family_name(client) -> str:
    """Display / identity-bind name: the registered family, else the class
    name; the full dotted path when that name is a registered family's, so
    an unregistered class is never read with that family's grammar."""
    entry = client_entry(client)
    if entry is not None:
        return entry.family
    cls = type(client)
    return cls.__name__ if cls.__name__ not in _FAMILIES else f"{cls.__module__}.{cls.__qualname__}"
