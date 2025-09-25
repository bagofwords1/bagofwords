from __future__ import annotations

from typing import Any, Dict, Optional, Type

from pydantic import BaseModel

# Import provider Config/Credentials from the provider module
from app.schemas.data_sources.configs import (
    # Configs
    PostgreSQLConfig,
    SnowflakeConfig,
    BigQueryConfig,
    NetSuiteConfig,
    SQLConfig,
    PrestoConfig,
    GoogleAnalyticsConfig,
    GCPConfig,
    AWSCostConfig,
    AWSAthenaConfig,
    VerticaConfig,
    AwsRedshiftConfig,
    TableauConfig,
    SalesforceConfig,
    # Credentials
    PostgreSQLCredentials,
    SnowflakeCredentials,
    BigQueryCredentials,
    NetSuiteCredentials,
    SQLCredentials,
    PrestoCredentials,
    GoogleAnalyticsCredentials,
    GCPCredentials,
    AWSCostCredentials,
    AWSAthenaCredentials,
    VerticaCredentials,
    AwsRedshiftUserPassCredentials,
    AwsRedshiftIAMCredentials,
    AwsRedshiftAssumeRoleCredentials,
    TableauPATCredentials,
    SalesforceCredentials,
)


class AuthVariant(BaseModel):
    title: str
    schema: Type[BaseModel]
    scopes: list[str] = ["system", "user"]  # which contexts this auth is allowed in

    class Config:
        arbitrary_types_allowed = True


class AuthOptions(BaseModel):
    """Auth options per provider.

    - default: the default auth name for UX
    - by_auth: mapping of auth name -> Pydantic credentials schema class
    """

    default: str
    by_auth: Dict[str, AuthVariant]

    class Config:
        arbitrary_types_allowed = True


class DataSourceRegistryEntry(BaseModel):
    type: str
    title: str
    description: str
    status: str = "active"
    version: str = "1.0.0"
    config_schema: Type[BaseModel]
    credentials_auth: AuthOptions
    # Optional explicit client path; if None, fallback to dynamic resolution
    client_path: Optional[str] = None

    class Config:
        arbitrary_types_allowed = True


# Central registry for data sources
REGISTRY: Dict[str, DataSourceRegistryEntry] = {
    "postgresql": DataSourceRegistryEntry(
        type="postgresql",
        title="PostgreSQL",
        description="Open-source relational database known for reliability and feature robustness.",
        config_schema=PostgreSQLConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=PostgreSQLCredentials, scopes=["system","user"])
        }),
        client_path=None
    ),
    "snowflake": DataSourceRegistryEntry(
        type="snowflake",
        title="Snowflake",
        description="Cloud-based data warehousing platform that supports SQL queries.",
        config_schema=SnowflakeConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SnowflakeCredentials, scopes=["system","user"])
        }),
        client_path="app.data_sources.clients.snowflake_client.SnowflakeClient",
    ),
    "bigquery": DataSourceRegistryEntry(
        type="bigquery",
        title="Google BigQuery",
        description="Serverless, highly scalable, and cost-effective multi-cloud data warehouse.",
        config_schema=BigQueryConfig,
        credentials_auth=AuthOptions(default="service_account", by_auth={
            "service_account": AuthVariant(title="Service Account JSON", schema=BigQueryCredentials, scopes=["system"])  # system-managed only
        }),
        client_path=None,
    ),
    "netsuite": DataSourceRegistryEntry(
        type="netsuite",
        title="NetSuite",
        description="Cloud-based enterprise resource planning (ERP) software suite.",
        config_schema=NetSuiteConfig,
        credentials_auth=AuthOptions(default="token", by_auth={
            "token": AuthVariant(title="Token-Based Auth", schema=NetSuiteCredentials, scopes=["system"])  # typically system
        }),
        client_path=None,
        status="inactive",
        version="0.0.0",
    ),
    "mysql": DataSourceRegistryEntry(
        type="mysql",
        title="MySQL",
        description="Popular open-source relational database management system.",
        config_schema=SQLConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SQLCredentials, scopes=["system","user"])
        }),
        client_path=None,
    ),
    "aws_athena": DataSourceRegistryEntry(
        type="aws_athena",
        title="AWS Athena",
        description="AWS Athena is a serverless query service that makes it easy to analyze data in Amazon S3 using standard SQL.",
        config_schema=AWSAthenaConfig,
        credentials_auth=AuthOptions(default="key", by_auth={
            "key": AuthVariant(title="AWS Keys", schema=AWSAthenaCredentials, scopes=["system"])  # system
        }),
        client_path=None,
        version="beta",
    ),
    "mariadb": DataSourceRegistryEntry(
        type="mariadb",
        title="Mariadb",
        description="MariaDB is a fast, open-source MySQL replacement.",
        config_schema=SQLConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SQLCredentials, scopes=["system","user"])
        }),
        client_path=None,
    ),
    "salesforce": DataSourceRegistryEntry(
        type="salesforce",
        title="Salesforce",
        description="Cloud-based CRM platform for sales, service, marketing, and more.",
        config_schema=SalesforceConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SalesforceCredentials, scopes=["system"])  # likely system
        }),
        client_path=None,
    ),
    "MSSQL": DataSourceRegistryEntry(
        type="MSSQL",
        title="Microsoft SQL Server",
        description="MSSQL is Microsoft's relational database for managing and analyzing data.",
        config_schema=SQLConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SQLCredentials, scopes=["system","user"])
        }),
        client_path=None,
    ),
    "clickhouse": DataSourceRegistryEntry(
        type="clickhouse",
        title="ClickHouse",
        description="ClickHouse is a fast, open-source columnar database for real-time analytics.",
        config_schema=SQLConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SQLCredentials, scopes=["system","user"])
        }),
        client_path=None,
    ),
    "aws_cost": DataSourceRegistryEntry(
        type="aws_cost",
        title="AWS Cost Explorer",
        description="AWS Cost Explorer helps analyze and visualize your AWS spending and usage patterns over time.",
        config_schema=AWSCostConfig,
        credentials_auth=AuthOptions(default="key", by_auth={
            "key": AuthVariant(title="AWS Keys", schema=AWSCostCredentials, scopes=["system"])  # system
        }),
        client_path=None,
        version="beta",
    ),
    "vertica": DataSourceRegistryEntry(
        type="vertica",
        title="Vertica",
        description="High-performance columnar analytics database optimized for large-scale data warehousing and analytics workloads.",
        config_schema=VerticaConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=VerticaCredentials, scopes=["system","user"])
        }),
        client_path=None,
    ),
    "aws_redshift": DataSourceRegistryEntry(
        type="aws_redshift",
        title="AWS Redshift",
        description="Fully managed, petabyte-scale data warehouse service in the cloud for analytics and business intelligence.",
        config_schema=AwsRedshiftConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=AwsRedshiftUserPassCredentials, scopes=["system","user"]),
            "iam": AuthVariant(title="AWS Keys (IAM)", schema=AwsRedshiftIAMCredentials, scopes=["system"]),
            "arn": AuthVariant(title="Assume Role (ARN)", schema=AwsRedshiftAssumeRoleCredentials, scopes=["system"]),
        }),
        client_path=None,
    ),
    "tableau": DataSourceRegistryEntry(
        type="tableau",
        title="Tableau",
        description="Discover schemas via Metadata API and query published data sources via VizQL Data Service.",
        config_schema=TableauConfig,
        credentials_auth=AuthOptions(default="pat", by_auth={
            "pat": AuthVariant(title="Personal Access Token", schema=TableauPATCredentials, scopes=["system", "user"])  
        }),
        client_path="app.data_sources.clients.tableau_client.TableauClient",
    ),
}


def get_entry(ds_type: str) -> DataSourceRegistryEntry:
    entry = REGISTRY.get(ds_type)
    if not entry:
        raise ValueError(f"Unknown data source type: {ds_type}")
    return entry


def list_available_data_sources() -> list[dict]:
    return [
        {
            "type": e.type,
            "title": e.title,
            "description": e.description,
            "config": e.config_schema.__name__,
            "status": e.status,
            "version": e.version,
        }
        for e in REGISTRY.values()
        if e.status == "active"
    ]


def config_schema_for(ds_type: str) -> Type[BaseModel]:
    return get_entry(ds_type).config_schema


def default_credentials_schema_for(ds_type: str) -> Type[BaseModel]:
    entry = get_entry(ds_type)
    default = entry.credentials_auth.default
    variant = entry.credentials_auth.by_auth.get(default)
    if not variant:
        raise ValueError("No default credentials schema defined")
    return variant.schema


def credentials_schema_for(ds_type: str, auth_type: Optional[str]) -> Type[BaseModel]:
    entry = get_entry(ds_type)
    selected = auth_type or entry.credentials_auth.default
    variant = entry.credentials_auth.by_auth.get(selected)
    if not variant:
        raise ValueError("Unsupported authentication method for this data source")
    return variant.schema


def resolve_client_class(ds_type: str):
    """Resolve client class via configured path; fallback to dynamic naming."""
    from importlib import import_module

    entry = get_entry(ds_type)
    if entry.client_path:
        try:
            module_path, _, class_name = entry.client_path.rpartition(".")
            module = import_module(module_path)
            return getattr(module, class_name)
        except Exception:
            pass

    # Fallback to dynamic resolution used previously
    module_name = f"app.data_sources.clients.{ds_type.lower()}_client"
    title = "".join(word[:1].upper() + word[1:] for word in ds_type.split("_"))
    class_name = f"{title}Client"
    module = import_module(module_name)
    return getattr(module, class_name)


