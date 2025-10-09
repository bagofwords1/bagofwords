from __future__ import annotations

from typing import Optional, List

from pydantic import BaseModel, Field


# PostgreSQL
class PostgreSQLCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    # Password can be empty for some deployments; treat as optional/blank-allowed
    password: str = Field("", title="Password", description="", json_schema_extra={"ui:type": "password"})


class PostgreSQLConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(5432, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})


# MySQL/MariaDB/MSSQL - Combined since they share the same structure
class SQLCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class SQLConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(..., ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})


# Snowflake
class SnowflakeCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class SnowflakeConfig(BaseModel):
    account: str = Field(..., title="Account", description="The unique account identifier. For example: ABCDEF-GHIJKL", json_schema_extra={"ui:type": "string"})
    warehouse: str = Field(..., title="Warehouse", description="", json_schema_extra={"ui:type": "string"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})
    schema: str = Field(..., title="Schema", description="", json_schema_extra={"ui:type": "string"})


# BigQuery - credentials_json already contains all auth info
class BigQueryCredentials(BaseModel):
    credentials_json: str = Field(..., title="Service Account JSON", description="", json_schema_extra={"ui:type": "textarea"})


class BigQueryConfig(BaseModel):
    project_id: str = Field(..., title="Project ID", description="", json_schema_extra={"ui:type": "string"})
    dataset: str = Field(..., title="Dataset", description="", json_schema_extra={"ui:type": "string"})


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
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})


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
    sandbox: bool = False
    domain: str = "login"


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
        title="URIs",
        description="One URI pattern per line for parquet/csv files. Supports wildcards. Examples: s3:// or az://",
        json_schema_extra={"ui:type": "textarea"}
    )

__all__ = [
    # Configs
    "PostgreSQLConfig",
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
    "DuckDBNoAuthCredentials",
    "DuckDBAwsCredentials",
    "DuckDBGcpCredentials",
    "DuckDBAzureCredentials",
    # Credentials
    "PostgreSQLCredentials",
    "SnowflakeCredentials",
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
    "DuckDBAzureCredentials"
]


