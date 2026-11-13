"""Every client class production can build says how its parameters render.

Contract: each class `resolve_client_class` returns for a registry key, and
each `app.data_sources.fast` class defining `execute_query`, is in exactly one
bucket:

  A. it has a parameter renderer (`renderer_for` is not None);
  B. it is declared in `PARAMS_REFUSED` with a reason naming the file:line of
     its `execute_query` (params are refused there, or are the driver's own);
  C. its `execute_query` takes no positional text (or it has none).

The three sets are the plan's closed lists, so a client added, moved or
removed fails here until someone decides which bucket it is in. A class that
cannot be imported fails too: it cannot be shown to be complete.
"""
from __future__ import annotations

import inspect
import re

import pytest

from app.ai.code_execution.client_profiles import PARAMS_REFUSED
from app.ai.code_execution.query_params import renderer_for
from app.data_sources.clients.base import DataSourceClient
from tests.unit._client_enumeration import enumerated_client_classes

A = {
    # From the guard profile's dialect.
    "PostgresqlClient", "AwsRedshiftClient", "MysqlClient", "MariadbClient", "MSSQLClient", "MsFabricClient",
    "OracledbClient", "SqliteClient", "SnowflakeClient", "BigqueryClient", "AwsAthenaClient", "TrinoClient",
    "ClickhouseClient", "DatabricksSqlClient", "SparkConnectClient", "DuckDBClient", "TeradataClient",
    "DruidClient",
    # Renderer filled on a guarded row.
    "Db2Client", "IbmiClient", "SplunkClient",
    # Renderer-only rows.
    "VerticaClient", "SybaseClient", "SapHanaClient", "PinotClient", "TimbrClient", "CSVClient", "QVDClient",
    "PBIXClient", "PowerBIReportServerClient", "SalesforceClient", "PostHogClient", "AzureDataExplorerClient",
    "FastQueryClient", "NetsuiteClient", "OracleBIClient", "SisenseClient", "PriorityErpClient",
    "SapDatasphereClient", "MecmClient",
}
B = {
    # JSON query spec / envelope.
    "ElasticsearchClient", "OpenSearchClient", "BrocadeClient", "AppDynamicsClient", "AriaOperationsClient",
    "AzureCostClient", "CheckPointClient", "CiscoApicClient", "CrowdStrikeClient", "DellOmeClient",
    "F5BigIpClient", "HpeOneViewClient", "IntuneClient", "JaegerClient", "KubernetesClient", "MondayClient",
    "NetAppOntapClient", "ServiceNowClient", "SharepointListsClient", "SpectrumClient", "VCenterClient",
    "ZabbixClient", "MongodbClient",
    # MDX / DAX.
    "SapBwXmlaClient", "InforOlapClient", "AnalysisServicesClient", "PowerBIClient", "InforEpmClient",
    # Other query text with no declared renderer.
    "PrometheusClient", "AwsCloudWatchClient", "BusinessObjectsClient", "TimbrA2aClient", "GmailMailClient",
    # File / document id or JSON file spec.
    "NetworkDirClient", "GitHubRepoClient", "GitLabRepoClient", "BitbucketRepoClient", "OnedriveClient",
    "SharepointClient", "OnenoteClient", "GraphMailClient", "GraphTeamsClient", "GoogleDriveClient", "S3Client",
    "DocumentumClient", "SharepointOnpremClient", "KmsLighthouseClient",
    # Positional id that is not query text.
    "QlikSenseClient", "QlikSenseOnPremClient", "TableauClient", "AwsCostClient",
    # Native driver params.
    "Neo4jClient",
}
C = {"BrowserClient", "McpClient", "CustomApiClient", "RemoteServerClient", "RemoteSMBClient"}

_POSITIONAL = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.VAR_POSITIONAL,
)


def _takes_positional_text(cls) -> bool:
    fn = getattr(cls, "execute_query", None)
    if fn is None:
        return False
    return any(p.kind in _POSITIONAL for p in list(inspect.signature(fn).parameters.values())[1:])


def _buckets(cls, refused=PARAMS_REFUSED) -> list[str]:
    key = (cls.__module__, cls.__qualname__)
    found = []
    if renderer_for(object.__new__(cls)) is not None:
        found.append("A")
    if key in refused and refused[key].strip():
        found.append("B")
    if not _takes_positional_text(cls):
        found.append("C")
    return found


@pytest.fixture(scope="module")
def classes():
    return enumerated_client_classes()


def test_the_enumeration_covers_the_registry_and_the_accelerated_client(classes):
    from app.data_sources.fast.fast_client import FastQueryClient
    from app.schemas.data_source_registry import REGISTRY

    names = [c.__name__ for c in classes]
    assert FastQueryClient in classes
    assert len(names) == len(set(names)) == 97
    # Registry entries without a client_path resolve through the naming fallback.
    for key in ("postgresql", "oracledb", "bigquery", "netsuite", "mysql", "aws_athena", "mariadb",
                "clickhouse", "pinot", "druid", "aws_cost", "vertica", "aws_redshift", "sybase"):
        assert REGISTRY[key].client_path is None, key
    for name in ("PostgresqlClient", "OracledbClient", "BigqueryClient", "NetsuiteClient", "MysqlClient",
                 "AwsAthenaClient", "MariadbClient", "ClickhouseClient", "PinotClient", "DruidClient",
                 "AwsCostClient", "VerticaClient", "AwsRedshiftClient", "SybaseClient"):
        assert name in names, name


def test_every_client_class_is_in_exactly_one_bucket(classes):
    wrong = {c.__name__: _buckets(c) for c in classes if len(_buckets(c)) != 1}
    assert wrong == {}


def test_the_buckets_are_the_closed_lists(classes):
    by_bucket = {"A": set(), "B": set(), "C": set()}
    for cls in classes:
        for bucket in _buckets(cls):
            by_bucket[bucket].add(cls.__name__)
    assert by_bucket["A"] == A
    assert by_bucket["B"] == B
    assert by_bucket["C"] == C


def _definition(cls) -> str:
    owner = next(c for c in cls.__mro__ if "execute_query" in c.__dict__)
    fn = inspect.unwrap(owner.__dict__["execute_query"])
    return f"{inspect.getsourcefile(fn).rsplit('/', 1)[-1]}:{inspect.getsourcelines(fn)[1]}"


def test_every_refusal_declaration_names_an_enumerated_class_and_its_execute_query(classes):
    by_key = {(c.__module__, c.__qualname__): c for c in classes}
    assert set(PARAMS_REFUSED) <= set(by_key), set(PARAMS_REFUSED) - set(by_key)
    for key, reason in PARAMS_REFUSED.items():
        named = re.findall(r"\b[\w]+\.py:\d+\b", reason)
        assert named == [_definition(by_key[key])], (key, reason)


def test_neo4j_is_declared_as_native_params():
    assert "native params" in PARAMS_REFUSED[("app.data_sources.clients.neo4j_client", "Neo4jClient")]


# The bucket rule can fail: a class nobody declared is in no bucket, one
# declared twice is in two.

class _UndeclaredConnector(DataSourceClient):
    description = test_connection = get_schemas = get_schema = prompt_schema = None

    def execute_query(self, sql):
        raise AssertionError("never called")


def test_an_undeclared_connector_with_positional_text_is_in_no_bucket():
    assert _buckets(_UndeclaredConnector) == []


def test_a_connector_with_a_renderer_and_a_refusal_is_in_two_buckets():
    from app.data_sources.clients.vertica_client import VerticaClient

    refused = {(VerticaClient.__module__, VerticaClient.__qualname__): "declared here too, vertica_client.py:58"}
    assert _buckets(VerticaClient, refused) == ["A", "B"]
