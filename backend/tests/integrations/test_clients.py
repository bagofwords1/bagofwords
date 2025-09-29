import os
import yaml
import pytest
import logging
from typing import Dict, Generator, Any
import pandas as pd
from app.data_sources.clients.mysql_client import MysqlClient
from app.data_sources.clients.postgresql_client import PostgresqlClient
from app.data_sources.clients.bigquery_client import BigqueryClient
from app.data_sources.clients.snowflake_client import SnowflakeClient
from app.data_sources.clients.clickhouse_client import ClickhouseClient
from app.data_sources.clients.aws_cost_client import AwsCostClient
from app.data_sources.clients.aws_redshift_client import AwsRedshiftClient
from app.data_sources.clients.aws_athena_client import AwsAthenaClient
from app.data_sources.clients.mssql_client import MSSQLClient
from app.data_sources.clients.oracledb_client import OracledbClient
from app.data_sources.clients.salesforce_client import SalesforceClient
from app.data_sources.clients.presto_client import PrestoClient
from app.data_sources.clients.vertica_client import VerticaClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def load_credentials() -> Dict[str, Any]:
    """
    Load credentials from the json file in the tests folder.

    Returns:
        A dictionary containing credentials for all data sources.
    """
    # Construct the file path relative to the current script
    credentials_path = os.path.join(os.path.dirname(__file__), "integrations.json")
    with open(credentials_path, "r") as file:
        return yaml.safe_load(file)


# Load the credentials once and reuse them
CREDENTIALS: Dict[str, Any] = load_credentials()


def ds_kwargs(name: str) -> Dict[str, Any]:
    cfg = dict(CREDENTIALS.get(name, {}))
    if not cfg:
        pytest.skip(f"{name} missing in integrations.json")
    enabled = cfg.pop("enabled", False)
    if not enabled:
        pytest.skip(f"{name} disabled in integrations.json")

    # Merge common block if provided
    common = cfg.pop("common", {}) or {}
    if isinstance(common, dict):
        cfg.update(common)

    # Prefer nested multi-auth structure: { auth: { type, by_auth: { <type>: {...} } } }
    auth = cfg.pop("auth", None)
    if isinstance(auth, dict):
        auth_type = auth.get("type")
        by_auth = auth.get("by_auth") or {}
        if auth_type and isinstance(by_auth, dict):
            selected = by_auth.get(auth_type, {})
            if isinstance(selected, dict):
                cfg.update(selected)

    # Also support a flat structure: { auth_type: "...", "...": { ... } }
    flat_auth_type = cfg.pop("auth_type", None)
    if flat_auth_type and isinstance(cfg.get(flat_auth_type), dict):
        flat_selected = cfg.pop(flat_auth_type)
        cfg.update(flat_selected)

    return cfg

@pytest.fixture
def mysql_client() -> MysqlClient:
    return MysqlClient(**ds_kwargs('mysql'))


@pytest.fixture
def postgresql_client() -> PostgresqlClient:
    return PostgresqlClient(**ds_kwargs('postgresql'))

@pytest.fixture
def mssql_client() -> MSSQLClient:
    return MSSQLClient(**ds_kwargs('mssql'))

@pytest.fixture
def oracledb_client() -> OracledbClient:
    return OracledbClient(**ds_kwargs('oracledb'))

@pytest.fixture
def bigquery_client() -> BigqueryClient:
    return BigqueryClient(**ds_kwargs('bigquery'))


@pytest.fixture
def snowflake_client() -> SnowflakeClient:
    return SnowflakeClient(**ds_kwargs('snowflake'))


@pytest.fixture
def clickhouse_client() -> ClickhouseClient:
    return ClickhouseClient(**ds_kwargs('clickhouse'))

@pytest.fixture
def salesforce_client() -> SalesforceClient:
    return SalesforceClient(**ds_kwargs('salesforce'))

@pytest.fixture
def presto_client() -> PrestoClient:
    return PrestoClient(**ds_kwargs('presto'))

@pytest.fixture
def vertica_client() -> VerticaClient:
    return VerticaClient(**ds_kwargs('vertica'))

#@pytest.fixture
#def google_analytics_client() -> GoogleAnalyticsClient:
#    return GoogleAnalyticsClient(**CREDENTIALS['google_analytics'])

#@pytest.fixture
#def gcp_client() -> GCPClient:
#    return GCPClient(**CREDENTIALS['gcp'])


@pytest.fixture
def aws_cost_client():
    return AwsCostClient(**ds_kwargs('aws'))

@pytest.fixture
def aws_redshift_client():
    return AwsRedshiftClient(**ds_kwargs('aws_redshift'))

@pytest.fixture
def aws_athena_client():
    return AwsAthenaClient(**ds_kwargs('aws_athena'))


def test_mysql_get_schemas(mysql_client):
    schemas = mysql_client.get_schemas()
    logger.info(f"MySQL: Found {len(schemas)} tables in database.")
    assert len(schemas) > 0, "Expected non-empty schemas for MySQL"


def test_postgresql_get_schemas(postgresql_client: PostgresqlClient) -> None:
    schemas = postgresql_client.get_schemas()
    logger.info(f"PostgreSQL: Found {len(schemas)} tables in database.")
    assert len(schemas) > 0, "Expected non-empty schemas for PostgreSQL"

def test_mssql_get_schemas(mssql_client: MSSQLClient) -> None:
    schemas = mssql_client.get_schemas()
    logger.info(f"Mssql: Found {len(schemas)} tables in database.")
    assert len(schemas) > 0, "Expected non-empty schemas for Mssql"

def test_oracledb_get_schemas(oracledb_client: OracledbClient) -> None:
    schemas = oracledb_client.get_schemas()
    logger.info(f"Oracledb: Found {len(schemas)} tables in database.")
    assert len(schemas) > 0, "Expected non-empty schemas for Oracledb"

def test_bigquery_get_schemas(bigquery_client: BigqueryClient) -> None:
    schemas = bigquery_client.get_schemas()
    logger.info(f"BigQuery: Found {len(schemas)} tables in dataset.")
    assert len(schemas) > 0, "Expected non-empty schemas for BigQuery"


def test_snowflake_get_schemas(snowflake_client: SnowflakeClient) -> None:
    schemas = snowflake_client.get_schemas()
    logger.info(f"Snowflake: Found {len(schemas)} tables in the database.")
    for schema in schemas:
        logger.info(f"Table: {schema.name}, Type: {type(schema)}")
    assert len(schemas) > 0, "Expected non-empty schemas for Snowflake"


def test_clickhouse_get_schemas(clickhouse_client: ClickhouseClient) -> None:
    schemas = clickhouse_client.get_schemas()
    logger.info(f"ClickHouse: Found {len(schemas)} tables in the database.")
    for schema in schemas:
        logger.info(f"Table: {schema.name}, Type: {type(schema)}")
    assert len(schemas) > 0, "Expected non-empty schemas for ClickHouse"

def test_salesforce_get_schemas(salesforce_client: SalesforceClient) -> None:
    schemas = salesforce_client.get_schemas()
    logger.info(f"Salesforce: Found {len(schemas)} schema tables.")
    for schema in schemas:
        logger.info(f"Table: {schema.name}, Type: {type(schema)}")
    assert len(schemas) > 0, "Expected non-empty schemas for Salesforce"

def test_salesforce_get_accounts(salesforce_client: SalesforceClient) -> None:
    records = salesforce_client.execute_query("SELECT Id, Name FROM Account")
    logger.info(f"Salesforce: Found {len(records)} accounts")
    assert len(records) > 0, "Expected non-empty accounts list for Salesforce"

def test_salesforce_get_opportunities(salesforce_client: SalesforceClient) -> None:
    records = salesforce_client.execute_query("SELECT Id, Name, StageName, Amount, CloseDate FROM Opportunity")
    logger.info(f"Salesforce: Found {len(records)} opportunities")
    assert len(records) > 0, "Expected non-empty opportunities for Salesforce"

def test_salesforce_get_contacts(salesforce_client: SalesforceClient) -> None:
    records = salesforce_client.execute_query("SELECT Id, FirstName, LastName, Email, Account.Name FROM Contact")
    logger.info(f"Salesforce: Found {len(records)} contacts")
    assert len(records) > 0, "Expected non-empty contacts for Salesforce"

def test_salesforce_get_leads(salesforce_client: SalesforceClient) -> None:
    records = salesforce_client.execute_query("SELECT Id, FirstName, LastName, Company, Status FROM Lead LIMIT 10")
    logger.info(f"Salesforce: Found {len(records)} leads")
    assert len(records) > 0, "Expected non-empty leads for Salesforce"

def test_salesforce_get_cases(salesforce_client: SalesforceClient) -> None:
    records = salesforce_client.execute_query("SELECT Id, CaseNumber, Status, Priority, Subject FROM Case LIMIT 10")
    logger.info(f"Salesforce: Found {len(records)} cases")
    assert len(records) > 0, "Expected non-empty cases for Salesforce"

def test_presto_connection(presto_client: PrestoClient) -> None:
    connection_status = presto_client.test_connection()
    logger.info(f"Presto Connection: {connection_status['message']}")
    assert connection_status["success"], "Expected successful connection to Presto"

def test_presto_get_schemas(presto_client: PrestoClient) -> None:
    schemas = presto_client.get_schemas()
    logger.info(f"Presto: Found {len(schemas)} tables in catalog.")
    assert len(schemas) > 0, "Expected non-empty schemas for Presto"


def test_presto_execute_query(presto_client: PrestoClient) -> None:
    query = "SELECT * FROM tpch.tiny.nation LIMIT 10"  # Example query using TPC-H catalog
    df: pd.DataFrame = presto_client.execute_query(query)
    logger.info(f"Presto: Query returned {len(df)} rows.")
    assert len(df) > 0, "Expected non-empty result from Presto query"

def test_presto_show_tables(presto_client: PrestoClient) -> None:
    query = "SHOW TABLES FROM tpch.tiny"
    df: pd.DataFrame = presto_client.execute_query(query)
    logger.info(f"Presto: Available tables in tpch.tiny: {df.iloc[:, 0].tolist()}")
    assert "nation" in df.iloc[:, 0].values, "Expected 'nation' table in 'tpch.tiny' schema"


def test_aws_cost_get_schemas(aws_cost_client):
    schemas = aws_cost_client.get_schemas()
    logger.info(f"AWS Cost Explorer: Found {len(schemas)} tables.")
    assert len(schemas) > 0, "Expected non-empty schemas for AWS Cost Explorer"

def test_aws_cost_execute_query(aws_cost_client):
    parameters = {
        "TimePeriod": {"Start": "2024-01-01", "End": "2024-12-01"},
        "Granularity": "MONTHLY",
        "Metrics": ["BlendedCost"]
    }
    df = aws_cost_client.execute_query("get_cost_and_usage", parameters)
    logger.info(f"AWS Cost Explorer query returned {len(df)} rows.")
    assert len(df) > 0, "Expected non-empty result from AWS Cost Explorer query"