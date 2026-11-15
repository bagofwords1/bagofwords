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
    # DuckDB SQL over catalog tables the client registers by name in an
    # in-memory DuckDB (`sql_guard._check_duckdb_table`).
    _sql("iceberg_client", "IcebergClient", "duckdb", frozenset({"as_of", "snapshot_id"})),
    # Calcite's lexer (Dremio) and the HiveServer2 engines' lexers (Hive,
    # Impala, Spark Thrift Server) are not pinned against sqlglot's.
    _sql("dremio_client", "DremioClient", "dremio", unknown_lexer=True),
    _sql("hive_client", "HiveClient", "hive", unknown_lexer=True),
    # Timbr's own discovery SQL is Spark SQL (backtick names); its server's
    # lexer is not known.
    _sql("timbr_client", "TimbrClient", "spark", renderer="timbr", unknown_lexer=True),
    # SQL with no sqlglot dialect: checked without a parser.
    _entry("db2_client", "Db2Client", "nonparsing_sql", renderer="ansi", engine="db2"),
    _entry("ibmi_client", "IbmiClient", "nonparsing_sql", renderer="ansi", engine="db2"),
    # Vertica reads a backslash as an escape when StandardConformingStrings
    # is off; SQL Anywhere reads one in every string.
    _entry("vertica_client", "VerticaClient", "nonparsing_sql", renderer="ansi_or_backslash", engine="vertica"),
    _entry("sybase_client", "SybaseClient", "nonparsing_sql", renderer="ansi_or_backslash", engine="sybase"),
    _entry("sap_hana_client", "SapHanaClient", "nonparsing_sql", renderer="ansi", engine="hana"),
    _entry("pinot_client", "PinotClient", "nonparsing_sql", renderer="ansi", engine="pinot"),
    _entry("splunk_client", "SplunkClient", "spl", renderer="spl", accepts_dict=True),
    _entry("neo4j_client", "Neo4jClient", "cypher", extra_kwargs=frozenset({"params"})),
    # Parameter rendering only; these are not checked at the wrapper.
    _renders("netsuite_client", "NetsuiteClient", "ansi"),
    # Logical SQL: whether a backslash escapes is not established.
    _renders("oracle_bi_client", "OracleBIClient", "ansi_or_backslash"),
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
# with a positional statement are refused there, except a JSON query
# document's bare number values (`query_params.render_json_number_params`);
# Neo4j's are its driver's own and never rendered. A declaration for the
# completeness test, keyed by the exact class, not an MRO lookup: a new
# subclass must be declared itself.
# The runtime refusal is `query_params.renderer_for(client) is None`.
def _refused(module: str, name: str, reason: str):
    return (f"{_CLIENTS}.{module}", name), reason


_JSON = "JSON query spec, not SQL text"
_MDX = "MDX / DAX query text with no declared renderer"
_FILE = "file or document id / JSON file spec, not query text"
_ID = "positional id, not query text"

PARAMS_REFUSED: dict[tuple[str, str], str] = dict([
    _refused("elasticsearch_client", "ElasticsearchClient", f"{_JSON}, elasticsearch_client.py:1144"),
    _refused("opensearch_client", "OpenSearchClient", f"{_JSON}, opensearch_client.py:382"),
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
    _refused("mongodb_client", "MongodbClient", f"{_JSON} (aggregation pipeline), mongodb_client.py:177"),
    _refused("sap_bw_xmla_client", "SapBwXmlaClient", f"{_MDX}, inherited from xmla_base.py:467"),
    _refused("infor_olap_client", "InforOlapClient", f"{_MDX}, inherited from xmla_base.py:467"),
    _refused("analysis_services_client", "AnalysisServicesClient", f"{_MDX}, analysis_services_client.py:673"),
    _refused("powerbi_client", "PowerBIClient", f"{_MDX}, powerbi_client.py:2114"),
    _refused("infor_epm_client", "InforEpmClient", f"{_MDX}, infor_epm_client.py:466"),
    _refused("prometheus_client", "PrometheusClient", "PromQL with no declared renderer, prometheus_client.py:431"),
    _refused("aws_cloudwatch_client", "AwsCloudWatchClient",
             "Metrics / Logs Insights spec with no declared renderer, aws_cloudwatch_client.py:685"),
    _refused("businessobjects_client", "BusinessObjectsClient",
             "result-object list, not query text, businessobjects_client.py:338"),
    _refused("timbr_a2a_client", "TimbrA2aClient", "natural-language prompt, not query text, timbr_a2a_client.py:224"),
    _refused("gmail_mail_client", "GmailMailClient", "Gmail search text with no declared renderer, gmail_mail_client.py:412"),
    _refused("network_dir_client", "NetworkDirClient",
             f"{_FILE}, or DuckDB SQL over the indexed file tables with no declared renderer, network_dir_client.py:1223"),
    _refused("git_repo_client", "GitHubRepoClient", f"{_FILE}, inherited from network_dir_client.py:1223"),
    _refused("git_repo_client", "GitLabRepoClient", f"{_FILE}, inherited from network_dir_client.py:1223"),
    _refused("git_repo_client", "BitbucketRepoClient", f"{_FILE}, inherited from network_dir_client.py:1223"),
    _refused("graph_drive_client", "OnedriveClient", f"{_FILE}, graph_drive_client.py:1529"),
    _refused("graph_drive_client", "SharepointClient", f"{_FILE}, graph_drive_client.py:1529"),
    _refused("graph_onenote_client", "OnenoteClient", f"{_FILE}, inherited from graph_drive_client.py:1529"),
    _refused("graph_mail_client", "GraphMailClient", f"{_FILE}, inherited from graph_drive_client.py:1529"),
    _refused("graph_teams_client", "GraphTeamsClient", f"{_FILE}, inherited from graph_drive_client.py:1529"),
    _refused("google_drive_client", "GoogleDriveClient", f"{_FILE}, google_drive_client.py:547"),
    _refused("s3_client", "S3Client", f"{_FILE}, inherited from _object_store_common.py:857"),
    _refused("gcs_client", "GCSClient", f"{_FILE}, inherited from _object_store_common.py:857"),
    _refused("azure_blob_client", "AzureBlobClient", f"{_FILE}, inherited from _object_store_common.py:857"),
    # Query languages with no declared renderer; each client binds its own
    # placeholders through its keyword argument `parameters`.
    _refused("cassandra_client", "CassandraClient", "CQL with no declared renderer, cassandra_client.py:205"),
    _refused("cosmosdb_client", "CosmosdbClient", "Cosmos SQL with no declared renderer, cosmosdb_client.py:186"),
    _refused("couchbase_client", "CouchbaseClient", "SQL++ with no declared renderer, couchbase_client.py:179"),
    _refused("dynamodb_client", "DynamodbClient", "PartiQL with no declared renderer, dynamodb_client.py:212"),
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


# Clients the wrapper does not check whose query surface cannot write or run
# a free-form statement, or that check every statement where they send it,
# and one remote agent trusted by an accepted decision, on the condition its
# own code enforces. Each reason names the file:line of the client's own
# code it rests on. A declaration covers the classes below it in the MRO
# only down to the class that defines the `execute_query` in effect: a
# subclass with its own `execute_query` is not covered and must be declared
# itself (`classify`).
def _exempt(module: str, name: str, reason: str, package: str = _CLIENTS):
    return (f"{package}.{module}", name), reason


_DISPATCH = "checked at the client's dispatch point"
_LANGUAGE = "read-only query language API"
_SPEC = "structured spec mapped to read endpoints"
_DUCKDB_FILES = "one DuckDB SELECT over the connection's own views, on a locked connection"

EXEMPT_CLIENTS: dict[tuple[str, str], str] = dict([
    # DuckDB file connections (P2) and accelerated custom tables.
    _exempt("csv_client", "CSVClient", f"{_DISPATCH}: {_DUCKDB_FILES}, csv_client.py:177"),
    _exempt("qvd_client", "QVDClient", f"{_DISPATCH}: {_DUCKDB_FILES}, qvd_client.py:683"),
    _exempt("pbix_client", "PBIXClient", f"{_DISPATCH}: {_DUCKDB_FILES}, pbix_client.py:379"),
    _exempt("powerbi_report_server_client", "PowerBIReportServerClient",
            f"{_DISPATCH}: {_DUCKDB_FILES} (report ids and export format checked before any URL), "
            "powerbi_report_server_client.py:704"),
    _exempt("fast_client", "FastQueryClient",
            f"{_DISPATCH}: artifacts attached READ_ONLY, external access off and configuration locked "
            "before any agent SQL (fast_client.py:167), introspection refused, fast_client.py:286",
            package="app.data_sources.fast"),
    # Read-only rules at the dispatch point (P5c).
    _exempt("mongodb_client", "MongodbClient", f"{_DISPATCH}: read stages and no server JavaScript, mongodb_client.py:198"),
    _exempt("azure_data_explorer_client", "AzureDataExplorerClient",
            f"{_DISPATCH}: management commands refused, azure_data_explorer_client.py:122"),
    _exempt("aws_cost_client", "AwsCostClient", f"{_DISPATCH}: get_, list_ and describe_ operations only, aws_cost_client.py:159"),
    _exempt("zabbix_client", "ZabbixClient", f"{_DISPATCH}: <object>.get methods only, zabbix_client.py:362"),
    _exempt("xmla_base", "XmlaClient", f"{_DISPATCH}: read verbs only, configured catalog only, xmla_base.py:477"),
    _exempt("analysis_services_client", "AnalysisServicesClient",
            f"{_DISPATCH}: read verbs only, configured catalog only, analysis_services_client.py:693"),
    _exempt("powerbi_client", "PowerBIClient",
            f"{_DISPATCH}: DAX only to the executeQueries read API, its ids GUIDs before any URL, powerbi_client.py:2199"),
    # Read-only rules at the dispatch point (P5d).
    _exempt("infor_epm_client", "InforEpmClient",
            f"{_DISPATCH}: the XMLA read rules plus LF or CR LF line ends, on the configured OLAP database, "
            "before the MDX process, infor_epm_client.py:475"),
    _exempt("oracle_bi_client", "OracleBIClient",
            f"{_DISPATCH}: one Logical SQL SELECT as an exact str (a WITH reaching a SELECT); refused in code: "
            "EXECUTE, SELECT_PHYSICAL, PHYSICAL, CALL, AGGREGATES, any word starting with EVALUATE, "
            "'#' anywhere but at the start or after a space, a tab or a line end; "
            "'@{' refused anywhere, oracle_bi_client.py:420"),
    _exempt("opensearch_client", "OpenSearchClient",
            f"{_DISPATCH}: known envelope keys only (opensearch_client.py:401), an index to _search, "
            "or one parsed SQL SELECT before the SQL plugin, opensearch_client.py:450"),
    _exempt("sisense_client", "SisenseClient",
            f"{_DISPATCH}: one parsed SQL SELECT before any request, the table lookup included, "
            "sisense_client.py:449"),
    # A remote agent trusted by decision, on the condition its code enforces.
    _exempt("timbr_a2a_client", "TimbrA2aClient",
            "remote agent with SQL validation enforced: should-validate-sql is always sent true "
            "(timbr_a2a_client.py:110); read-only otherwise rests on Timbr-side settings (accepted by decision)"),
    # Query languages with no statement that writes, each sent to its read endpoint.
    _exempt("salesforce_client", "SalesforceClient", f"{_LANGUAGE}: SOQL through the query resource, salesforce_client.py:712"),
    _exempt("posthog_client", "PostHogClient", f"{_LANGUAGE}: HogQL sent as a HogQLQuery, posthog_client.py:254"),
    _exempt("netsuite_client", "NetsuiteClient", f"{_LANGUAGE}: SuiteQL to the suiteql query endpoint, netsuite_client.py:37"),
    _exempt("cosmosdb_client", "CosmosdbClient",
            f"{_LANGUAGE}: Cosmos DB NoSQL query text through query_items, cosmosdb_client.py:209"),
    _exempt("prometheus_client", "PrometheusClient",
            f"{_LANGUAGE}: PromQL to /api/v1/query and /api/v1/query_range only, prometheus_client.py:453"),
    _exempt("aws_cloudwatch_client", "AwsCloudWatchClient",
            f"{_LANGUAGE}: Logs Insights start_query and metric reads only, aws_cloudwatch_client.py:456"),
    _exempt("elasticsearch_client", "ElasticsearchClient",
            f"{_SPEC}: index (no / ? #) to _search, or the SQL / ES|QL query APIs, elasticsearch_client.py:1206"),
    # A JSON spec or ids the client maps onto read requests it builds.
    _exempt("appdynamics_client", "AppDynamicsClient", f"{_SPEC}: catalog tables to GETs, appdynamics_client.py:306"),
    _exempt("aria_operations_client", "AriaOperationsClient", f"{_SPEC}: table to a _q_ read handler, aria_operations_client.py:660"),
    _exempt("azure_cost_client", "AzureCostClient", f"{_SPEC}: Cost Management query / forecast APIs, azure_cost_client.py:728"),
    _exempt("brocade_client", "BrocadeClient", f"{_SPEC}: GET allow-list plus login / logout, brocade_client.py:161"),
    _exempt("businessobjects_client", "BusinessObjectsClient",
            f"{_SPEC}: result-object names to a universe query the client builds, businessobjects_client.py:372"),
    _exempt("checkpoint_client", "CheckPointClient", f"{_SPEC}: allow-listed show-* commands only, checkpoint_client.py:296"),
    _exempt("cisco_apic_client", "CiscoApicClient", f"{_SPEC}: read-only request allow-list, cisco_apic_client.py:399"),
    _exempt("crowdstrike_client", "CrowdStrikeClient", f"{_SPEC}: catalog tables or one GET, crowdstrike_client.py:554"),
    _exempt("dell_ome_client", "DellOmeClient", f"{_SPEC}: GET allow-list, dell_ome_client.py:302"),
    _exempt("f5_bigip_client", "F5BigIpClient", f"{_SPEC}: catalogued GETs only, f5_bigip_client.py:717"),
    _exempt("hpe_oneview_client", "HpeOneViewClient", f"{_SPEC}: read-only request allow-list, hpe_oneview_client.py:378"),
    _exempt("intune_client", "IntuneClient", f"{_SPEC}: GETs only, intune_client.py:269"),
    _exempt("jaeger_client", "JaegerClient", f"{_SPEC}: GETs only, jaeger_client.py:134"),
    _exempt("kubernetes_client", "KubernetesClient", f"{_SPEC}: every request a GET, kubernetes_client.py:1121"),
    _exempt("monday_client", "MondayClient",
            f"{_SPEC}: one fixed GraphQL query, spec values as variables, monday_client.py:840"),
    _exempt("netapp_ontap_client", "NetAppOntapClient", f"{_SPEC}: GETs only, netapp_ontap_client.py:665"),
    _exempt("servicenow_client", "ServiceNowClient", f"{_SPEC}: GETs only, servicenow_client.py:279"),
    _exempt("graph_list_client", "SharepointListsClient", f"{_SPEC}: GETs only, graph_list_client.py:463"),
    _exempt("spectrum_client", "SpectrumClient", f"{_SPEC}: table to a _q_ read handler, spectrum_client.py:666"),
    _exempt("vcenter_client", "VCenterClient", f"{_SPEC}: table to a _q_ read handler, vcenter_client.py:813"),
    _exempt("qlik_sense_client", "QlikSenseClient",
            f"{_SPEC}: app, dimensions and measures to a session hypercube, qlik_sense_client.py:533"),
    _exempt("qlik_sense_onprem_client", "QlikSenseOnPremClient",
            f"{_SPEC}: app, dimensions and measures to a session hypercube, qlik_sense_onprem_client.py:1354"),
    _exempt("tableau_client", "TableauClient",
            f"{_SPEC}: datasource and field specs to VizQL query-datasource, tableau_client.py:388"),
    _exempt("mecm_client", "MecmClient", f"{_SPEC}: OData options on a GET, mecm_client.py:292"),
    _exempt("priority_erp_client", "PriorityErpClient", f"{_SPEC}: OData options on a GET, priority_erp_client.py:243"),
    _exempt("sap_datasphere_client", "SapDatasphereClient", f"{_SPEC}: OData options on a GET, sap_datasphere_client.py:488"),
    # A file, message or item id (or a JSON file spec) read from the source.
    # With indexed file tables, SQL text goes to one DuckDB SELECT over the
    # agent's selected tables. DuckDB parses agent text only on a locked
    # engine, and the query checks run before the query's own parse.
    _exempt("network_dir_client", "NetworkDirClient",
            f"{_DISPATCH}: file name or JSON file spec read (network_dir_client.py:537), or one DuckDB SELECT "
            "over the selected file tables: told from a file name by a parse on a locked engine "
            "(network_dir_client.py:1223), then checked before it is parsed and run on a locked engine "
            "(network_dir_client.py:1250)"),
    _exempt("graph_drive_client", "GraphDriveClient", f"{_SPEC}: file id or JSON file spec read by GET, graph_drive_client.py:572"),
    _exempt("google_drive_client", "GoogleDriveClient", f"{_SPEC}: file id read by GET, google_drive_client.py:166"),
    _exempt("gmail_mail_client", "GmailMailClient", f"{_SPEC}: message id or Gmail search by GET, gmail_mail_client.py:212"),
    # S3, Google Cloud Storage and Azure Blob: one object read by its key.
    _exempt("_object_store_common", "ObjectStoreClient",
            f"{_SPEC}: object key or JSON file spec read with read_file, _object_store_common.py:857"),
    _exempt("documentum_client", "DocumentumClient", f"{_SPEC}: document id read by GET, documentum_client.py:296"),
    _exempt("sharepoint_onprem_client", "SharepointOnpremClient",
            f"{_SPEC}: file id read over bounded read-only HTTP, sharepoint_onprem_client.py:187"),
    _exempt("kms_lighthouse_client", "KmsLighthouseClient", f"{_SPEC}: item id read by GET, kms_lighthouse_client.py:2174"),
    # No text statement at all.
    _exempt("browser_client", "BrowserClient", "keyword-only API: execute_query(self, **kwargs), browser_client.py:79"),
    _exempt("tool_provider_base", "ToolProviderClient", "no execute_query text: defines none, tool_provider_base.py:9"),
    _exempt("bow_client", "BowClient", "structured spec mapped to read endpoints: a BowQuery request, bow_client.py:68"),
])


def classify(client) -> str:
    """"guarded" (the wrapper checks its statements), "exempt" (declared in
    EXEMPT_CLIENTS for the `execute_query` in effect) or "unknown"."""
    return classify_type(type(client))


def classify_type(cls: type) -> str:
    """`classify` for a client class, before any client is built."""
    entry = _entry_for_type(cls)
    if entry is not None and entry.profile is not None:
        return "guarded"
    mro = cls.__mro__
    owner = next((i for i, c in enumerate(mro) if "execute_query" in c.__dict__), len(mro) - 1)
    if any((c.__module__, c.__qualname__) in EXEMPT_CLIENTS for c in mro[: owner + 1]):
        return "exempt"
    return "unknown"


def client_entry(client) -> ClientEntry | None:
    """The entry of the most specific registered class in `client`'s MRO."""
    return _entry_for_type(type(client))


def _entry_for_type(cls: type) -> ClientEntry | None:
    for c in cls.__mro__:
        entry = _CLIENT_TABLE.get((c.__module__, c.__qualname__))
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
