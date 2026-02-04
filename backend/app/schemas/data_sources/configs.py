from __future__ import annotations

from typing import Optional, List

from pydantic import BaseModel, Field, model_validator


# PostgreSQL
class PostgreSQLCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    # Password can be empty for some deployments; treat as optional/blank-allowed
    password: str = Field("", title="Password", description="", json_schema_extra={"ui:type": "password"})

#
# OracleDB
#
class OracleCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class OracleConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(1521, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    service_name: str = Field(..., title="Service Name", description="Oracle service name (not SID)", json_schema_extra={"ui:type": "string"})
    schema: Optional[str] = Field(
        None,
        title="Schema",
        description="Optional schema or comma-separated list of schemas",
        json_schema_extra={"ui:type": "string"}
    )


class PostgreSQLConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(5432, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})
    schema: Optional[str] = Field(
        None,
        title="Schema",
        description="Optional schema or comma-separated list of schemas",
        json_schema_extra={"ui:type": "string"}
    )


# SQLite (local file database)
class SQLiteCredentials(BaseModel):
    class Config:
        extra = "allow"


class SQLiteConfig(BaseModel):
    database: str = Field(
        ...,
        title="Database Path",
        description="Absolute path to SQLite .db/.sqlite file. Example: /data/mydb.sqlite",
        json_schema_extra={"ui:type": "string"}
    )


# MySQL/MariaDB/MSSQL - Combined since they share the same structure
class SQLCredentials(BaseModel):
    user: Optional[str] = Field(
        None,
        title="User",
        description="Leave blank to use anonymous database access.",
        json_schema_extra={"ui:type": "string"},
    )
    password: Optional[str] = Field(
        None,
        title="Password",
        description="Leave blank to use anonymous database access or empty password.",
        json_schema_extra={"ui:type": "password"},
    )

    @model_validator(mode="after")
    def validate_user_password(cls, model: "SQLCredentials") -> "SQLCredentials":
        if model.password not in (None, "") and model.user in (None, ""):
            raise ValueError("A user must be provided when supplying a password.")
        return model


class SQLConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(..., ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})


# Snowflake
class SnowflakeCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class SnowflakeKeypairCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    private_key_pem: str = Field(
        ...,
        title="Private Key (PEM)",
        description="PEM-encoded RSA private key used for Snowflake key pair authentication",
        json_schema_extra={"ui:type": "textarea"},
    )
    private_key_passphrase: Optional[str] = Field(
        None,
        title="Private Key Passphrase",
        description="Passphrase for the encrypted private key, if applicable",
        json_schema_extra={"ui:type": "password"},
    )


class SnowflakeConfig(BaseModel):
    account: str = Field(..., title="Account", description="The unique account identifier. For example: ABCDEF-GHIJKL", json_schema_extra={"ui:type": "string"})
    warehouse: str = Field(..., title="Warehouse", description="", json_schema_extra={"ui:type": "string"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})
    schema: str = Field(..., title="Schema", description="Can be a comma-separated list of schemas", json_schema_extra={"ui:type": "string"})
    role: Optional[str] = Field(
        None,
        title="Role",
        description="Optional Snowflake role to use for this connection",
        json_schema_extra={"ui:type": "string"},
    )


# BigQuery - credentials_json already contains all auth info
class BigQueryCredentials(BaseModel):
    credentials_json: str = Field(..., title="Service Account JSON", description="", json_schema_extra={"ui:type": "textarea"})


class BigQueryConfig(BaseModel):
    project_id: str = Field(..., title="Project ID", description="", json_schema_extra={"ui:type": "string"})
    dataset: str = Field(..., title="Dataset", description="", json_schema_extra={"ui:type": "string"})
    maximum_bytes_billed: Optional[int] = Field(
        None,
        title="Max Bytes Billed",
        description="Limit the number of bytes billed for the query. Keep blank to disable",
        json_schema_extra={"ui:type": "number"}
    )
    use_query_cache: bool = Field(
        False,
        title="Use Query Cache",
        description="Allow returning cached results if available",
        json_schema_extra={"ui:type": "boolean"}
    )


# NetSuite - all auth related fields should be in credentials
class NetSuiteCredentials(BaseModel):
    consumer_key: str = Field(..., title="Consumer Key", description="", json_schema_extra={"ui:type": "string"})
    consumer_secret: str = Field(..., title="Consumer Secret", description="", json_schema_extra={"ui:type": "password"})
    token_id: str = Field(..., title="Token ID", description="", json_schema_extra={"ui:type": "string"})
    token_secret: str = Field(..., title="Token Secret", description="", json_schema_extra={"ui:type": "password"})
    account_id: str = Field(..., title="Account ID", description="", json_schema_extra={"ui:type": "string"})


class NetSuiteConfig(BaseModel):
    pass


# Clickhouse
class ClickhouseCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class ClickhouseConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(8123, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    database: Optional[str] = Field(
        None,
        title="Database",
        description="Can be a comma-separated list of databases. If not provided, will use all databases.",
        json_schema_extra={"ui:type": "string"}
    )

    secure: bool = Field(True, title="Secure", description="", json_schema_extra={"ui:type": "boolean"})


# ADP - all fields are sensitive
class ADPCredentials(BaseModel):
    client_id: str = Field(..., title="Client ID", description="", json_schema_extra={"ui:type": "string"})
    client_secret: str = Field(..., title="Client Secret", description="", json_schema_extra={"ui:type": "password"})
    username: str = Field(..., title="Username", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class ADPConfig(BaseModel):
    pass


# Salesforce
class SalesforceCredentials(BaseModel):
    username: str = Field(..., title="Username", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})
    security_token: str = Field(..., title="Security Token", description="", json_schema_extra={"ui:type": "string"})


class SalesforceConfig(BaseModel):
    sandbox: bool = Field(False, title="Sandbox", description="", json_schema_extra={"ui:type": "boolean"})
    domain: str = Field("login", title="Domain", description="", json_schema_extra={"ui:type": "string"})


# Service Demo
class ServiceDemoCredentials(BaseModel):
    access_key: str = Field(..., title="Access Key", description="", json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="Secret Key", description="", json_schema_extra={"ui:type": "password"})


class ServiceDemoConfig(BaseModel):
    region: str = Field(..., title="Region", description="", json_schema_extra={"ui:type": "string"})


# Update the specific config classes to use the new base
class MySQLConfig(SQLConfig):
    port: int = Field(3306, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})


class MariadbConfig(SQLConfig):
    port: int = Field(3306, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})


class MssqlConfig(SQLConfig):
    port: int = Field(1433, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})


# Presto
class PrestoCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class PrestoConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(8080, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    catalog: str = Field(..., title="Catalog", description="", json_schema_extra={"ui:type": "string"})
    schema: str = Field(..., title="Schema", description="", json_schema_extra={"ui:type": "string"})
    protocol: str = Field("http", title="Protocol", description="", json_schema_extra={"ui:type": "string"})


# Google Analytics
class GoogleAnalyticsCredentials(BaseModel):
    service_account_file: str = Field(..., title="Service Account JSON", description="", json_schema_extra={"ui:type": "textarea"})
    property_id: str = Field(..., title="Property ID", description="", json_schema_extra={"ui:type": "string"})


class GoogleAnalyticsConfig(BaseModel):
    pass


# GCP
class GCPCredentials(BaseModel):
    credentials_json: str = Field(..., title="Credentials JSON", description="", json_schema_extra={"ui:type": "textarea"})
    project_id: str = Field(..., title="Project ID", description="", json_schema_extra={"ui:type": "string"})


class GCPConfig(BaseModel):
    pass


# AWS Cost
class AWSCredentials(BaseModel):
    access_key: str = Field(..., title="Access Key", description="", json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="Secret Key", description="", json_schema_extra={"ui:type": "password"})


class AWSCostCredentials(AWSCredentials):
    pass


class AWSCostConfig(BaseModel):
    region_name: str = Field(..., title="Region", description="", json_schema_extra={"ui:type": "string"})


# AWS Athena
class AWSAthenaCredentials(BaseModel):
    access_key: str = Field(..., title="Access Key", description="", json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="Secret Key", description="", json_schema_extra={"ui:type": "password"})
    role_arn: str = Field(..., title="Role ARN", description="", json_schema_extra={"ui:type": "string"})


class AWSAthenaConfig(BaseModel):
    region: str = Field(..., title="Region", description="", json_schema_extra={"ui:type": "string"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})
    workgroup: str = Field("primary", title="Workgroup", description="", json_schema_extra={"ui:type": "string"})
    s3_output_location: str = Field(..., title="S3 Output Location", description="", json_schema_extra={"ui:type": "string"})
    data_source: str = Field("AwsDataCatalog", title="Data Source", description="", json_schema_extra={"ui:type": "string"})


# Vertica
class VerticaCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class VerticaConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(5433, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})
    schema: str = Field("public", title="Schema", description="", json_schema_extra={"ui:type": "string"})


# AWS Redshift
class AwsRedshiftUserPassCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class AwsRedshiftIAMCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    access_key: str = Field(..., title="Access Key", description="", json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="Secret Key", description="", json_schema_extra={"ui:type": "password"})

class AwsRedshiftAssumeRoleCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    role_arn: str = Field(..., title="Role ARN", description="", json_schema_extra={"ui:type": "string"})



class AwsRedshiftConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(5439, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})
    schema: str = Field("public", title="Schema", description="", json_schema_extra={"ui:type": "string"})
    region: Optional[str] = Field(None, title="Region", description="", json_schema_extra={"ui:type": "string"})
    cluster_identifier: Optional[str] = Field(None, title="Cluster Identifier", description="", json_schema_extra={"ui:type": "string"})
    ssl_mode: str = Field("require", title="SSL Mode", description="", json_schema_extra={"ui:type": "string"})
    timeout: int = Field(30, ge=1, le=300, title="Timeout", description="", json_schema_extra={"ui:type": "number"})


# Tableau
class TableauPATCredentials(BaseModel):
    pat_name: str | None = Field(None, title="PAT Name", description="", json_schema_extra={"ui:type": "string"})
    pat_token: str | None = Field(None, title="PAT Token", description="", json_schema_extra={"ui:type": "password"})



class TableauConfig(BaseModel):
    server_url: str = Field(..., title="Server URL", description="", json_schema_extra={"ui:type": "string"})
    site_name: Optional[str] = Field(None, title="Site Name", description="", json_schema_extra={"ui:type": "string"})
    verify_ssl: bool = Field(True, title="Verify SSL", description="", json_schema_extra={"ui:type": "boolean"})
    timeout_sec: int = Field(30, ge=1, le=300, title="Timeout (sec)", description="", json_schema_extra={"ui:type": "number"})
    default_project_id: Optional[str] = Field(None, title="Default Project ID", description="", json_schema_extra={"ui:type": "string"})
    #include_datasource_ids: Optional[List[str]] = None


# DuckDB (files via object stores or local)
class DuckDBNoAuthCredentials(BaseModel):
    # Allow extra so creds provided without auth_type (e.g., aws keys) are preserved during validation
    class Config:
        extra = 'allow'


class DuckDBAwsCredentials(BaseModel):
    access_key: str = Field(..., title="AWS Access Key", description="", json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="AWS Secret Key", description="", json_schema_extra={"ui:type": "password"})
    region: Optional[str] = Field(None, title="Region", description="", json_schema_extra={"ui:type": "string"})
    session_token: Optional[str] = Field(None, title="Session Token (optional)", description="For temporary credentials", json_schema_extra={"ui:type": "password"})


class DuckDBGcpCredentials(BaseModel):
    service_account_json: str = Field(..., title="GCP Service Account JSON", description="", json_schema_extra={"ui:type": "textarea"})


class DuckDBAzureCredentials(BaseModel):
    connection_string: str = Field(..., title="Azure Connection String", description="SAS or account key connection string", json_schema_extra={"ui:type": "string"})


class DuckDBConfig(BaseModel):
    uris: str = Field(
        ...,
        title="URI/Path",
        description="Path to local .duckdb file, or URI pattern per line for parquet/csv files. Supports wildcards. Examples: /data/my.duckdb, s3://, az://",
        json_schema_extra={"ui:type": "textarea"}
    )

# Apache Pinot
class PinotConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(8099, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    secure: bool = Field(True, title="Secure", description="Use HTTPS when true", json_schema_extra={"ui:type": "boolean"})
    path: str = Field("/query/sql", title="Path", description="Broker SQL endpoint path", json_schema_extra={"ui:type": "string"})
    controller: Optional[str] = Field(
        None,
        title="Controller URL",
        description="Optional controller base URL, e.g. http://controller-host:9000",
        json_schema_extra={"ui:type": "string"}
    )
    query_options: Optional[str] = Field(
        None,
        title="Query Options",
        description="Optional queryOptions string, e.g. useMultistageEngine=true",
        json_schema_extra={"ui:type": "string"}
    )


# MongoDB
class MongoDBCredentials(BaseModel):
    user: Optional[str] = Field(
        None,
        title="User",
        description="Username for authentication. Leave blank for unauthenticated connections.",
        json_schema_extra={"ui:type": "string"}
    )
    password: Optional[str] = Field(
        None,
        title="Password",
        description="Password for authentication.",
        json_schema_extra={"ui:type": "password"}
    )


class MongoDBConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host",
        description="MongoDB host (e.g., localhost) or Atlas cluster (e.g., cls.abc.mongodb.net)",
        json_schema_extra={"ui:type": "string"}
    )
    port: int = Field(
        27017,
        ge=1,
        le=65535,
        title="Port",
        description="MongoDB port (default: 27017). Ignored for Atlas/SRV connections.",
        json_schema_extra={"ui:type": "number"}
    )
    database: str = Field(
        ...,
        title="Database",
        description="Database name to connect to",
        json_schema_extra={"ui:type": "string"}
    )
    auth_source: Optional[str] = Field(
        "admin",
        title="Auth Database",
        description="Database to authenticate against (default: admin). Ignored for Atlas.",
        json_schema_extra={"ui:type": "string"}
    )
    tls: bool = Field(
        False,
        title="Enable TLS/SSL",
        json_schema_extra={"ui:type": "boolean"}
    )
    use_srv: bool = Field(
        False,
        title="Use Atlas/SRV",
        json_schema_extra={"ui:type": "boolean"}
    )


# Azure Data Explorer (Kusto)
class AzureDataExplorerCredentials(BaseModel):
    client_id: str = Field(..., title="Client ID", description="Azure AD Application (Client) ID", json_schema_extra={"ui:type": "string"})
    client_secret: str = Field(..., title="Client Secret", description="Azure AD Application Secret", json_schema_extra={"ui:type": "password"})
    tenant_id: str = Field(..., title="Tenant ID", description="Azure AD Tenant ID", json_schema_extra={"ui:type": "string"})


class AzureDataExplorerConfig(BaseModel):
    cluster_url: str = Field(
        ...,
        title="Cluster URL",
        description="Azure Data Explorer cluster URL (e.g., https://mycluster.region.kusto.windows.net)",
        json_schema_extra={"ui:type": "string"}
    )
    database: str = Field(..., title="Database", description="Database name", json_schema_extra={"ui:type": "string"})


# PostHog
class PostHogCredentials(BaseModel):
    api_key: str = Field(
        ...,
        title="Personal API Key",
        description="PostHog Personal API Key with query:read and project:read scopes",
        json_schema_extra={"ui:type": "password"}
    )


class PostHogConfig(BaseModel):
    host: str = Field(
        "https://us.posthog.com",
        title="Host",
        description="PostHog instance URL (us.posthog.com, eu.posthog.com, or self-hosted)",
        json_schema_extra={"ui:type": "string"}
    )
    project_id: str = Field(
        ...,
        title="Project ID",
        description="PostHog Project ID (found in project settings)",
        json_schema_extra={"ui:type": "string"}
    )


# Databricks SQL
class DatabricksSqlCredentials(BaseModel):
    access_token: str = Field(
        ...,
        title="Personal Access Token",
        description="Databricks personal access token for authentication",
        json_schema_extra={"ui:type": "password"}
    )


class DatabricksSqlConfig(BaseModel):
    server_hostname: str = Field(
        ...,
        title="Server Hostname",
        description="Databricks workspace hostname (e.g., abc123.cloud.databricks.com)",
        json_schema_extra={"ui:type": "string"}
    )
    http_path: str = Field(
        ...,
        title="HTTP Path",
        description="SQL warehouse HTTP path (e.g., /sql/1.0/warehouses/abc123)",
        json_schema_extra={"ui:type": "string"}
    )
    catalog: str = Field(
        ...,
        title="Catalog",
        description="Unity Catalog name to use",
        json_schema_extra={"ui:type": "string"}
    )
    schema: Optional[str] = Field(
        None,
        title="Schema",
        description="Optional schema or comma-separated list of schemas. If empty, all schemas in the catalog will be discovered.",
        json_schema_extra={"ui:type": "string"}
    )


__all__ = [
    # Configs
    "PostgreSQLConfig",
    "SQLiteConfig",
    "OracleConfig",
    "SnowflakeConfig",
    "BigQueryConfig",
    "NetSuiteConfig",
    "SQLConfig",
    "PrestoConfig",
    "GoogleAnalyticsConfig",
    "GCPConfig",
    "AWSCostConfig",
    "AWSAthenaConfig",
    "VerticaConfig",
    "AwsRedshiftConfig",
    "TableauConfig",
    "DuckDBConfig",
    "PinotConfig",
    "MongoDBConfig",
    "PostHogConfig",
    "DuckDBNoAuthCredentials",
    "DuckDBAwsCredentials",
    "DuckDBGcpCredentials",
    "DuckDBAzureCredentials",
    # Credentials
    "PostgreSQLCredentials",
    "SQLiteCredentials",
    "OracleCredentials",
    "SnowflakeCredentials",
    "SnowflakeKeypairCredentials",
    "BigQueryCredentials",
    "NetSuiteCredentials",
    "SQLCredentials",
    "PrestoCredentials",
    "GoogleAnalyticsCredentials",
    "GCPCredentials",
    "AWSCostCredentials",
    "AWSAthenaCredentials",
    "VerticaCredentials",
    "AwsRedshiftCredentials",
    "TableauCredentials",
    "DuckDBNoAuthCredentials",
    "DuckDBAwsCredentials",
    "DuckDBGcpCredentials",
    "DuckDBAzureCredentials",
    "AzureDataExplorerCredentials",
    "AzureDataExplorerConfig",
    "MongoDBCredentials",
    "PostHogCredentials",
    # Databricks SQL
    "DatabricksSqlCredentials",
    "DatabricksSqlConfig",
]