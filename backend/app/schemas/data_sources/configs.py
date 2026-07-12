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
    use_tcps: bool = Field(
        False,
        title="Use TCPS (TLS)",
        description="Connect over TCPS (TLS-encrypted SQL*Net) instead of plain TCP. Enable when the listener port only accepts TLS connections.",
        json_schema_extra={"ui:type": "boolean"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify the server TLS certificate when using TCPS. Disable for certificates signed by an internal CA the backend host does not trust.",
        json_schema_extra={"ui:type": "boolean"},
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
    oauth_client_id: Optional[str] = Field(
        None,
        title="OAuth Client ID",
        description="Google OAuth 2.0 Client ID for user sign-in (from Google Cloud Console > Credentials > OAuth 2.0 Client IDs)",
        json_schema_extra={"ui:type": "string"}
    )
    oauth_client_secret: Optional[str] = Field(
        None,
        title="OAuth Client Secret",
        description="Google OAuth 2.0 Client Secret for user sign-in",
        json_schema_extra={"ui:type": "password"}
    )


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
    account_id: str = Field(..., title="Account ID", description="", json_schema_extra={"ui:type": "string"})
    consumer_key: str = Field(..., title="Consumer Key", description="", json_schema_extra={"ui:type": "string"})
    consumer_secret: str = Field(..., title="Consumer Secret", description="", json_schema_extra={"ui:type": "password"})
    token_id: str = Field(..., title="Token ID", description="", json_schema_extra={"ui:type": "string"})
    token_secret: str = Field(..., title="Token Secret", description="", json_schema_extra={"ui:type": "password"})


class NetSuiteConfig(BaseModel):
    table_filter: Optional[str] = Field(
        None,
        title="Table Filter",
        description="Optional comma-separated list of table names to include in schema discovery. If empty, discovers all tables.",
        json_schema_extra={"ui:type": "textarea"}
    )


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


# ServiceNow
class ServiceNowCredentials(BaseModel):
    username: str = Field(..., title="Username", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class ServiceNowConfig(BaseModel):
    instance_url: str = Field(
        ...,
        title="Instance URL",
        description="Your ServiceNow instance URL, e.g. https://acme.service-now.com",
        json_schema_extra={"ui:type": "string"}
    )
    tables: Optional[str] = Field(
        None,
        title="Tables",
        description="Optional comma-separated list of tables to expose. If empty, uses a curated set of common ITSM tables.",
        json_schema_extra={"ui:type": "textarea"}
    )
    discover_all: bool = Field(
        False,
        title="Discover All Tables",
        description="Discover all business tables including custom (u_/x_) tables instead of the curated set.",
        json_schema_extra={"ui:type": "boolean"}
    )
    display_values: bool = Field(
        True,
        title="Display Values",
        description="Return human-readable display values for reference and choice fields.",
        json_schema_extra={"ui:type": "boolean"}
    )


# Zabbix
class ZabbixTokenCredentials(BaseModel):
    api_token: str = Field(
        ...,
        title="API Token",
        description="A Zabbix API token (Users → API tokens). Recommended for Zabbix 5.4+, and the way to connect in SSO environments.",
        json_schema_extra={"ui:type": "password"},
    )


class ZabbixUserPassCredentials(BaseModel):
    username: str = Field(
        ...,
        title="Username",
        description="A Zabbix user with read access to the monitored hosts. Used for older installs without API tokens.",
        json_schema_extra={"ui:type": "string"},
    )
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class ZabbixConfig(BaseModel):
    url: str = Field(
        ...,
        title="Zabbix URL",
        description="Your Zabbix frontend URL, e.g. https://zabbix.acme.com (the /api_jsonrpc.php endpoint is appended automatically).",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify the server's TLS certificate. Disable only for self-signed certificates.",
        json_schema_extra={"ui:type": "boolean"},
    )
    history_window_days: int = Field(
        7,
        ge=1,
        le=365,
        title="History Window (days)",
        description="Default lookback window applied when querying metric history/trends without an explicit time range.",
        json_schema_extra={"ui:type": "number"},
    )


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


class MssqlKerberosCredentials(BaseModel):
    """Windows Integrated authentication via Kerberos (service keytab).

    The app authenticates with its own Kerberos identity: either the process
    default credential cache (populated by kinit / KRB5_CLIENT_KTNAME) or a
    dedicated principal resolved from the client keytab.
    """
    use_kerberos: bool = Field(
        True,
        title="Use Kerberos (Windows Integrated)",
        description="Authenticate with the app's Kerberos identity instead of a SQL login. Requires krb5 to be configured on the server (keytab + krb5.conf).",
        json_schema_extra={"ui:type": "boolean"},
    )
    kerberos_principal: Optional[str] = Field(
        None,
        title="Service Principal (optional)",
        description="Client principal to authenticate as, e.g. svc-bow@CORP.EXAMPLE.COM. Requires a matching keytab entry. Leave blank to use the default credential cache.",
        json_schema_extra={"ui:type": "string"},
    )

    @model_validator(mode="after")
    def validate_kerberos_enabled(cls, model: "MssqlKerberosCredentials") -> "MssqlKerberosCredentials":
        if not model.use_kerberos:
            raise ValueError("Kerberos must remain enabled for this authentication method.")
        return model


class MssqlKerberosDelegatedCredentials(BaseModel):
    """Per-user Kerberos SSO via constrained delegation (S4U2Self + S4U2Proxy).

    The app's service account obtains a delegated ticket for the end user's AD
    principal and queries SQL Server as that user. Requires the service account
    to be trusted for constrained delegation with protocol transition in AD.
    """
    use_kerberos: bool = Field(
        True,
        title="Use Kerberos SSO",
        description="Queries run under your own Active Directory identity via Kerberos Constrained Delegation.",
        json_schema_extra={"ui:type": "boolean"},
    )
    kerberos_impersonate: Optional[str] = Field(
        None,
        title="Active Directory Principal (UPN)",
        description="Your AD user principal name, e.g. jdoe@corp.example.com. Leave blank to use your login identity.",
        json_schema_extra={"ui:type": "string"},
    )

    @model_validator(mode="after")
    def validate_kerberos_enabled(cls, model: "MssqlKerberosDelegatedCredentials") -> "MssqlKerberosDelegatedCredentials":
        if not model.use_kerberos:
            raise ValueError("Kerberos must remain enabled for this authentication method.")
        return model


class MssqlConfig(SQLConfig):
    port: int = Field(1433, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    schema: Optional[str] = Field(
        None,
        title="Schema",
        description="Optional schema or comma-separated list of schemas",
        json_schema_extra={"ui:type": "string"}
    )
    odbc_driver: int = Field(
        18,
        title="ODBC Driver Version",
        description="ODBC driver version (17 or 18). Use 17 for SQL Server 2008 compatibility",
        json_schema_extra={"ui:type": "select", "ui:options": [17, 18]}
    )
    encrypt: bool = Field(
        True,
        title="Encrypt Connection",
        description="Encrypt the connection. Disable for SQL Server 2008 without TLS support",
        json_schema_extra={"ui:type": "boolean"}
    )
    additional_params: Optional[dict] = Field(
        default_factory=dict,
        title="Additional Connection Parameters",
        description="Extra ODBC keywords sent as-is, e.g. ApplicationIntent=ReadOnly. Security keys (Encrypt, credentials, driver) cannot be overridden.",
        json_schema_extra={"ui:type": "keyvalue"}
    )


class SybaseConfig(SQLConfig):
    port: int = Field(2638, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})


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


# Trino
class TrinoCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field("", title="Password", description="Required for HTTPS only", json_schema_extra={"ui:type": "password"})


class TrinoConfig(BaseModel):
    host: str = Field(..., title="Host", description="", json_schema_extra={"ui:type": "string"})
    port: int = Field(8080, ge=1, le=65535, title="Port", description="", json_schema_extra={"ui:type": "number"})
    catalog: str = Field(..., title="Catalog", description="", json_schema_extra={"ui:type": "string"})
    schema: str = Field(..., title="Schema", description="", json_schema_extra={"ui:type": "string"})
    protocol: str = Field("http", title="Protocol", description="http or https", json_schema_extra={"ui:type": "string"})


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


class AWSAthenaDefaultCredentials(BaseModel):
    """No credentials required — boto3 resolves via its default chain (env vars, instance profile, IRSA, etc.)."""
    class Config:
        extra = 'allow'


class AWSAthenaConfig(BaseModel):
    region: str = Field(..., title="Region", description="", json_schema_extra={"ui:type": "string"})
    database: str = Field(..., title="Database", description="", json_schema_extra={"ui:type": "string"})
    workgroup: str = Field("primary", title="Workgroup", description="", json_schema_extra={"ui:type": "string"})
    s3_output_location: Optional[str] = Field(None, title="S3 Output Location", description="Leave blank if your workgroup has a default output location", json_schema_extra={"ui:type": "string"})
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


# Teradata
class TeradataCredentials(BaseModel):
    user: str = Field(..., title="User", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class TeradataConfig(BaseModel):
    host: str = Field(..., title="Host", description="Teradata system hostname or IP (e.g. the TPA/COP name)", json_schema_extra={"ui:type": "string"})
    port: int = Field(1025, ge=1, le=65535, title="Port", description="Teradata listener port (default 1025)", json_schema_extra={"ui:type": "number"})
    database: str = Field(
        ...,
        title="Database",
        description="Database to query. In Teradata a database is the namespace (≈ schema). Can be a comma-separated list.",
        json_schema_extra={"ui:type": "string"},
    )
    logmech: str = Field(
        "TD2",
        title="Logon Mechanism",
        description="Authentication mechanism. TD2 (default) for native users; LDAP/KRB5/TDNEGO for directory-based logon (common on-prem).",
        json_schema_extra={"ui:type": "select", "ui:options": ["TD2", "LDAP", "KRB5", "TDNEGO"]},
    )


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
    api_version: str = Field("3.21", title="API Version", description="Tableau REST API version. Change only for older on-prem Tableau Server.", json_schema_extra={"ui:type": "string"})
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


# Apache Druid
class DruidConfig(BaseModel):
    host: str = Field(..., title="Host", description="Broker or Router host", json_schema_extra={"ui:type": "string"})
    port: int = Field(8082, ge=1, le=65535, title="Port", description="Broker SQL port (8082) or Router port (8888)", json_schema_extra={"ui:type": "number"})
    secure: bool = Field(False, title="Secure", description="Use HTTPS when true", json_schema_extra={"ui:type": "boolean"})
    path: str = Field("/druid/v2/sql/", title="Path", description="Druid SQL endpoint path", json_schema_extra={"ui:type": "string"})
    schema: Optional[str] = Field(
        None,
        title="Schema",
        description="Optional schema or comma-separated list of schemas (default: druid). System schemas are always excluded.",
        json_schema_extra={"ui:type": "string"}
    )


class DruidTokenCredentials(BaseModel):
    token: str = Field(
        ...,
        title="API Token",
        description="Bearer token sent as 'Authorization: Bearer <token>'.",
        json_schema_extra={"ui:type": "password"},
    )


class DruidBasicTokenCredentials(BaseModel):
    basic_token: str = Field(
        ...,
        title="API Token",
        description="Token sent verbatim as 'Authorization: Basic <token>' — not "
        "base64-encoded. Use this for Imply Polaris 'pok_…' API keys.",
        json_schema_extra={"ui:type": "password"},
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


# OpenSearch
class OpenSearchCredentials(BaseModel):
    user: str = Field(
        ...,
        title="User",
        description="Username for HTTP basic authentication (OpenSearch security plugin).",
        json_schema_extra={"ui:type": "string"}
    )
    password: str = Field(
        ...,
        title="Password",
        description="Password for HTTP basic authentication.",
        json_schema_extra={"ui:type": "password"}
    )


class OpenSearchNoAuthCredentials(BaseModel):
    """Clusters with the security plugin disabled or network-gated access."""
    pass


class OpenSearchConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host",
        description="OpenSearch host (e.g. localhost) or full URL (e.g. https://search.example.com:9200)",
        json_schema_extra={"ui:type": "string"}
    )
    port: int = Field(
        9200,
        ge=1,
        le=65535,
        title="Port",
        description="OpenSearch REST port (default: 9200). Ignored when Host is a full URL.",
        json_schema_extra={"ui:type": "number"}
    )
    secure: bool = Field(
        False,
        title="Use HTTPS",
        description="Connect over HTTPS. Ignored when Host is a full URL.",
        json_schema_extra={"ui:type": "boolean"}
    )
    verify_certs: bool = Field(
        True,
        title="Verify TLS Certificates",
        description="Disable only for clusters using self-signed demo certificates.",
        json_schema_extra={"ui:type": "boolean"}
    )
    index_pattern: Optional[str] = Field(
        None,
        title="Index Pattern",
        description="Optional comma-separated index names or globs to expose (e.g. logs-*,orders). Default: all non-system indices.",
        json_schema_extra={"ui:type": "string"}
    )


# Splunk
class SplunkTokenCredentials(BaseModel):
    api_token: str = Field(
        ...,
        title="Authentication Token",
        description="A Splunk authentication token (Settings → Tokens). Recommended, and the way to connect to Splunk Cloud.",
        json_schema_extra={"ui:type": "password"},
    )


class SplunkUserPassCredentials(BaseModel):
    username: str = Field(
        ...,
        title="Username",
        description="A Splunk user with search access to the target indexes.",
        json_schema_extra={"ui:type": "string"},
    )
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class SplunkConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host",
        description="Splunk host (e.g. splunk.acme.com) or full management URL (e.g. https://splunk.acme.com:8089).",
        json_schema_extra={"ui:type": "string"},
    )
    port: int = Field(
        8089,
        ge=1,
        le=65535,
        title="Management Port",
        description="Splunk REST/management port (default: 8089). Ignored when Host is a full URL.",
        json_schema_extra={"ui:type": "number"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify the server's TLS certificate. Disable only for self-signed certificates.",
        json_schema_extra={"ui:type": "boolean"},
    )
    discovery_window_days: int = Field(
        7,
        ge=1,
        le=365,
        title="Discovery Window (days)",
        description="Default lookback window applied to schema discovery and to searches that omit an explicit time range.",
        json_schema_extra={"ui:type": "number"},
    )
    max_sampled_sourcetypes: int = Field(
        50,
        ge=0,
        le=1000,
        title="Max Sampled Sourcetypes",
        description="Cap on how many sourcetypes (ranked by event volume) get their fields sampled during indexing. The rest stay thin and are discovered on demand. Keeps reindexing cheap.",
        json_schema_extra={"ui:type": "number"},
    )


# Elasticsearch
class ElasticsearchApiKeyCredentials(BaseModel):
    api_key: str = Field(
        ...,
        title="API Key",
        description="An Elasticsearch API key. Paste either the encoded key, or the raw 'id:api_key' pair (Stack Management → API keys). Recommended for Elasticsearch 8.x.",
        json_schema_extra={"ui:type": "password"},
    )


class ElasticsearchCredentials(BaseModel):
    user: str = Field(
        ...,
        title="User",
        description="Username for HTTP basic authentication (e.g. 'elastic' or a role user with read access).",
        json_schema_extra={"ui:type": "string"},
    )
    password: str = Field(
        ...,
        title="Password",
        description="Password for HTTP basic authentication.",
        json_schema_extra={"ui:type": "password"},
    )


class ElasticsearchNoAuthCredentials(BaseModel):
    """Clusters with security disabled or network-gated access."""
    pass


class ElasticsearchConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host",
        description="Elasticsearch host (e.g. localhost) or full URL (e.g. https://es.example.com:9200)",
        json_schema_extra={"ui:type": "string"},
    )
    port: int = Field(
        9200,
        ge=1,
        le=65535,
        title="Port",
        description="Elasticsearch REST port (default: 9200). Ignored when Host is a full URL.",
        json_schema_extra={"ui:type": "number"},
    )
    secure: bool = Field(
        True,
        title="Use HTTPS",
        description="Connect over HTTPS. Elasticsearch 8.x uses TLS by default. Ignored when Host is a full URL.",
        json_schema_extra={"ui:type": "boolean"},
    )
    verify_certs: bool = Field(
        True,
        title="Verify TLS Certificates",
        description="Disable only for clusters using self-signed demo certificates.",
        json_schema_extra={"ui:type": "boolean"},
    )
    index_pattern: Optional[str] = Field(
        None,
        title="Index Pattern",
        description="Optional comma-separated index names or globs to expose (e.g. logs-*,metrics-*). Default: all non-system indices.",
        json_schema_extra={"ui:type": "string"},
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


# Prometheus (time-series metrics via the HTTP API + PromQL)
class PrometheusNoAuthCredentials(BaseModel):
    # Network-gated Prometheus (VPN / internal :9090) needs no secret.
    class Config:
        extra = "allow"


class PrometheusBasicCredentials(BaseModel):
    username: str = Field(
        ...,
        title="Username",
        description="Username for HTTP Basic auth (typically a reverse proxy in front of Prometheus).",
        json_schema_extra={"ui:type": "string"},
    )
    password: str = Field(
        ...,
        title="Password",
        description="Password for HTTP Basic auth.",
        json_schema_extra={"ui:type": "password"},
    )


class PrometheusBearerCredentials(BaseModel):
    token: str = Field(
        ...,
        title="Bearer Token",
        description="Sent as 'Authorization: Bearer <token>'. Used by most hosted/managed Prometheus offerings.",
        json_schema_extra={"ui:type": "password"},
    )


class PrometheusConfig(BaseModel):
    base_url: str = Field(
        ...,
        title="Base URL",
        description="Prometheus server URL, including scheme and port. Example: http://prometheus:9090",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify the server TLS certificate. Disable only for self-signed certs on internal hosts.",
        json_schema_extra={"ui:type": "boolean"},
    )
    org_id: Optional[str] = Field(
        None,
        title="Tenant / Org ID",
        description="Optional 'X-Scope-OrgID' header for multi-tenant back-ends (Thanos, Cortex, Grafana Mimir).",
        json_schema_extra={"ui:type": "string"},
    )
    metric_prefix: Optional[str] = Field(
        None,
        title="Metric Name Filter",
        description="Optional prefix to bound metric discovery on large instances (e.g. 'node_' or 'http_'). Leave blank to index all metrics.",
        json_schema_extra={"ui:type": "string"},
    )


# Jaeger (distributed tracing via the Query JSON HTTP API)
class JaegerNoAuthCredentials(BaseModel):
    # Network-gated Jaeger (internal :16686 / behind a VPN) needs no secret.
    class Config:
        extra = "allow"


class JaegerBasicCredentials(BaseModel):
    username: str = Field(
        ...,
        title="Username",
        description="Username for HTTP Basic auth (typically a reverse proxy in front of Jaeger Query).",
        json_schema_extra={"ui:type": "string"},
    )
    password: str = Field(
        ...,
        title="Password",
        description="Password for HTTP Basic auth.",
        json_schema_extra={"ui:type": "password"},
    )


class JaegerBearerCredentials(BaseModel):
    token: str = Field(
        ...,
        title="Bearer Token",
        description="Sent as 'Authorization: Bearer <token>'. Used when Jaeger Query sits behind an auth proxy or gateway.",
        json_schema_extra={"ui:type": "password"},
    )


class JaegerConfig(BaseModel):
    base_url: str = Field(
        ...,
        title="Base URL",
        description="Jaeger Query URL, including scheme and port. Example: http://jaeger:16686",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify the server TLS certificate. Disable only for self-signed certs on internal hosts.",
        json_schema_extra={"ui:type": "boolean"},
    )
    default_lookback: str = Field(
        "1h",
        title="Default Lookback",
        description="Default search window when a query omits one (e.g. '1h', '6h', '2d').",
        json_schema_extra={"ui:type": "string"},
    )
    default_limit: int = Field(
        20,
        title="Default Trace Limit",
        description="Default maximum number of traces returned by a span search.",
        json_schema_extra={"ui:type": "number"},
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


# Spark Connect
class SparkConnectNoAuthCredentials(BaseModel):
    """No auth — open/dev Spark Connect clusters that don't require a token."""
    # Allow extra so creds provided without auth_type are preserved during validation
    class Config:
        extra = 'allow'


class SparkConnectCredentials(BaseModel):
    token: str = Field(
        ...,
        title="Bearer Token",
        description="Bearer token for the Spark Connect server (sent over the sc:// connection).",
        json_schema_extra={"ui:type": "password"}
    )


class SparkConnectConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host",
        description="Spark Connect server host (e.g., my-spark-host or localhost).",
        json_schema_extra={"ui:type": "string"}
    )
    port: int = Field(
        15002,
        ge=1,
        le=65535,
        title="Port",
        description="Spark Connect server port (default 15002).",
        json_schema_extra={"ui:type": "number"}
    )
    use_ssl: bool = Field(
        False,
        title="Use SSL",
        description="Connect to the Spark Connect server over TLS (sc://...;use_ssl=true).",
        json_schema_extra={"ui:type": "boolean"}
    )
    catalog: Optional[str] = Field(
        None,
        title="Catalog",
        description="Optional catalog to scope schema discovery to. If empty, the session default catalog is used.",
        json_schema_extra={"ui:type": "string"}
    )
    database: Optional[str] = Field(
        None,
        title="Database / Schema",
        description="Optional database (schema) or comma-separated list. If empty, all databases are discovered.",
        json_schema_extra={"ui:type": "string"}
    )
    require_partition_filter: bool = Field(
        False,
        title="Require Partition Filter",
        description="Reject queries that scan a partitioned table without filtering on a partition column (checked via EXPLAIN before the query runs).",
        json_schema_extra={"ui:type": "boolean"}
    )


# OAuth Delegated Credentials (empty — user provides nothing, OAuth flow populates tokens)
class OAuthDelegatedCredentials(BaseModel):
    """No user input needed. The OAuth authorization code flow populates tokens automatically."""
    pass


# Power BI
class PowerBICredentials(BaseModel):
    tenant_id: str = Field(
        ...,
        title="Tenant ID",
        description="Azure AD Tenant ID (Directory ID)",
        json_schema_extra={"ui:type": "string"}
    )
    client_id: str = Field(
        ...,
        title="Client ID",
        description="Azure AD App Registration Client ID",
        json_schema_extra={"ui:type": "string"}
    )
    client_secret: str = Field(
        ...,
        title="Client Secret",
        description="Azure AD App Registration Secret",
        json_schema_extra={"ui:type": "password"}
    )
    oauth_client_id: Optional[str] = Field(
        None,
        title="OAuth Client ID",
        description="App Registration Client ID for user sign-in (authorization code flow). If blank, falls back to Client ID above.",
        json_schema_extra={"ui:type": "string"}
    )
    oauth_client_secret: Optional[str] = Field(
        None,
        title="OAuth Client Secret",
        description="App Registration Secret for user sign-in. If blank, falls back to Client Secret above.",
        json_schema_extra={"ui:type": "password"}
    )


class PowerBIConfig(BaseModel):
    """Auto-discovers workspaces and datasets the service principal has access to."""
    workspaces: Optional[str] = Field(
        None,
        title="Workspaces",
        description="Optional workspace name(s) or ID(s), comma-separated. If empty, all accessible workspaces will be discovered.",
        json_schema_extra={"ui:type": "string"}
    )


# Power BI Report Server (on-prem)
class PowerBIReportServerCredentials(BaseModel):
    username: str = Field(
        ...,
        title="Username",
        description="Windows username. May include domain prefix (e.g. DOMAIN\\user) or be a local machine user.",
        json_schema_extra={"ui:type": "string"}
    )
    password: str = Field(
        ...,
        title="Password",
        description="Windows password",
        json_schema_extra={"ui:type": "password"}
    )
    domain: Optional[str] = Field(
        None,
        title="Domain",
        description="Optional Windows domain (workgroup or AD). If omitted and username doesn't contain a domain, NTLM uses the local machine.",
        json_schema_extra={"ui:type": "string"}
    )


class PowerBIReportServerConfig(BaseModel):
    server_url: str = Field(
        ...,
        title="Server URL",
        description="Base URL of the Power BI Report Server, e.g. http://pbi.example.com or http://pbi.example.com/Reports",
        json_schema_extra={"ui:type": "string"}
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify TLS certificate (disable only for self-signed test servers).",
        json_schema_extra={"ui:type": "boolean"}
    )
    ca_bundle_path: Optional[str] = Field(
        None,
        title="CA Bundle Path",
        description="Optional path to a custom CA bundle for internal certificates.",
        json_schema_extra={"ui:type": "string"}
    )


# Microsoft Fabric
class MSFabricCredentials(BaseModel):
    tenant_id: str = Field(
        ...,
        title="Tenant ID",
        description="Azure AD Tenant ID (Directory ID)",
        json_schema_extra={"ui:type": "string"}
    )
    client_id: str = Field(
        ...,
        title="Client ID",
        description="Azure AD App Registration Client ID",
        json_schema_extra={"ui:type": "string"}
    )
    client_secret: str = Field(
        ...,
        title="Client Secret",
        description="Azure AD App Registration Secret",
        json_schema_extra={"ui:type": "password"}
    )
    oauth_client_id: Optional[str] = Field(
        None,
        title="OAuth Client ID",
        description="App Registration Client ID for user sign-in (authorization code flow). If blank, falls back to Client ID above.",
        json_schema_extra={"ui:type": "string"}
    )
    oauth_client_secret: Optional[str] = Field(
        None,
        title="OAuth Client Secret",
        description="App Registration Secret for user sign-in. If blank, falls back to Client Secret above.",
        json_schema_extra={"ui:type": "password"}
    )


class MSFabricConfig(BaseModel):
    server_hostname: str = Field(
        ...,
        title="Server Hostname",
        description="Fabric SQL endpoint (e.g., abc123.datawarehouse.fabric.microsoft.com)",
        json_schema_extra={"ui:type": "string"}
    )
    database: str = Field(
        ...,
        title="Database",
        description="Warehouse or Lakehouse name",
        json_schema_extra={"ui:type": "string"}
    )
    schema: Optional[str] = Field(
        None,
        title="Schema",
        description="Optional schema or comma-separated list of schemas. If empty, all schemas will be discovered.",
        json_schema_extra={"ui:type": "string"}
    )


# SharePoint (Microsoft Graph)
class SharePointCredentials(BaseModel):
    tenant_id: str = Field(
        ...,
        title="Tenant ID",
        description="Azure AD Tenant ID (Directory ID)",
        json_schema_extra={"ui:type": "string"}
    )
    client_id: str = Field(
        ...,
        title="Client ID",
        description="Azure AD App Registration Client ID (used for OAuth authorization code flow with users)",
        json_schema_extra={"ui:type": "string"}
    )
    client_secret: str = Field(
        ...,
        title="Client Secret",
        description="Azure AD App Registration Secret",
        json_schema_extra={"ui:type": "password"}
    )
    oauth_client_id: Optional[str] = Field(
        None,
        title="OAuth Client ID (override)",
        description="Optional separate App Registration Client ID for user sign-in. Falls back to Client ID above.",
        json_schema_extra={"ui:type": "string"}
    )
    oauth_client_secret: Optional[str] = Field(
        None,
        title="OAuth Client Secret (override)",
        description="Optional separate App Registration Secret for user sign-in. Falls back to Client Secret above.",
        json_schema_extra={"ui:type": "password"}
    )


class SharePointConfig(BaseModel):
    site_url: str = Field(
        ...,
        title="Site URL",
        description="Full SharePoint site URL, e.g. https://contoso.sharepoint.com/sites/Finance",
        json_schema_extra={"ui:type": "string"}
    )
    drive_name: Optional[str] = Field(
        None,
        title="Document Library",
        description="Name of the document library (drive) on the site. Leave blank to use the site's default Documents library.",
        json_schema_extra={"ui:type": "string"}
    )
    folder_path: Optional[str] = Field(
        None,
        title="Folder Path",
        description="Optional folder path within the drive to scope the connection (e.g. 'Reports/2025'). Leave blank for the root.",
        json_schema_extra={"ui:type": "string"}
    )
    allowed_extensions: Optional[str] = Field(
        None,
        title="Allowed Extensions",
        description="Comma-separated list of file extensions to include (e.g. 'xlsx,csv,pdf'). Leave blank for all files.",
        json_schema_extra={"ui:type": "string"}
    )
    recursive: bool = Field(
        False,
        title="Include Subfolders",
        description="Recursively enumerate subfolders. Leave off for flatter, faster catalogs.",
        json_schema_extra={"ui:type": "boolean"}
    )


# OneDrive (Microsoft Graph — same auth as SharePoint, but exposed as an
# MCP-style tool-provider connection rather than a data source. No folder
# scope — each user accesses their entire OneDrive via per-user OAuth.)
class OneDriveCredentials(SharePointCredentials):
    """OneDrive uses the same Microsoft Graph auth as SharePoint."""
    pass


class OneDriveConfig(BaseModel):
    """OneDrive needs no admin-side configuration — each user's OAuth token
    determines what files are visible."""
    pass


# Google Drive
class GoogleDriveCredentials(BaseModel):
    oauth_client_id: str = Field(
        ...,
        title="OAuth Client ID",
        description="Google Cloud OAuth 2.0 Client ID (Web application type)",
        json_schema_extra={"ui:type": "string"}
    )
    oauth_client_secret: str = Field(
        ...,
        title="OAuth Client Secret",
        description="Google Cloud OAuth 2.0 Client Secret",
        json_schema_extra={"ui:type": "password"}
    )
    workspace_domain: Optional[str] = Field(
        None,
        title="Workspace Domain",
        description="Optional Google Workspace domain to restrict sign-in to (e.g. 'company.com'). Sets the `hd` hint on the authorize URL.",
        json_schema_extra={"ui:type": "string"}
    )


class GoogleDriveConfig(BaseModel):
    """Google Drive needs no admin-side configuration — each user's OAuth
    token determines what files are visible."""
    pass


# Network Directory (local / mounted file share — SMB/NFS/local path)
class NetworkDirCredentials(BaseModel):
    """A network directory is accessed via the filesystem (a local path or an
    already-mounted SMB/NFS share), so the app itself needs no credentials —
    mount-level auth is handled by the OS. `extra = 'allow'` keeps the schema
    forgiving if a deployment later stashes mount hints here."""
    class Config:
        extra = "allow"


class NetworkDirConfig(BaseModel):
    root_path: str = Field(
        ...,
        title="Directory Path",
        description=(
            "Absolute path to the directory to expose (a local folder or an "
            "already-mounted network share, e.g. '/mnt/contracts'). All reads, "
            "searches and writes are confined to this directory."
        ),
        json_schema_extra={"ui:type": "string"}
    )
    allowed_extensions: Optional[str] = Field(
        None,
        title="Allowed Extensions (legacy)",
        description="Deprecated — use Include Patterns with '**/*.ext'. Kept for back-compatibility.",
        json_schema_extra={"ui:type": "string", "ui:hidden": True}
    )
    include_globs: Optional[str] = Field(
        None,
        title="Include Patterns (globs)",
        description=(
            "Glob patterns relative to the path — the connection's scope. When "
            "set, ONLY matching files are visible AND readable; access to "
            "anything else is denied. Filter by folder AND type here: "
            "'reports/**/*.csv' (CSVs under reports), '**/*.pdf' (all PDFs), "
            "'files/**' (everything under files/). Comma-separated; '**' crosses "
            "subfolders, '*' is one segment. Leave blank to allow the whole path."
        ),
        json_schema_extra={"ui:type": "string"}
    )
    recursive: bool = Field(
        True,
        title="Include Subfolders",
        description="Recursively enumerate subfolders when listing / searching.",
        json_schema_extra={"ui:type": "boolean"}
    )
    writable: bool = Field(
        False,
        title="Allow Writes",
        description=(
            "Permit the agent to create / overwrite files in this directory "
            "(the write_file tool). Leave off for a read-only connection."
        ),
        json_schema_extra={"ui:type": "boolean"}
    )
    max_file_mb: int = Field(
        100,
        title="Max File Size (MB)",
        description="Skip files larger than this when reading. Guards against loading huge files into memory.",
        json_schema_extra={"ui:type": "number"}
    )
    index_mode: str = Field(
        "content",
        title="Indexing",
        description=(
            "How much to cache from the source. 'none' → nothing cached; the "
            "agent lists/reads live every time (freshest, best for volatile or "
            "huge dirs). 'metadata' → cache the file list on a schedule "
            "(fast listing, no content search). 'content' → also extract "
            "keywords from PDF/Word/PowerPoint/Excel/CSV so the agent can find "
            "files by topic. Reads are always live regardless."
        ),
        json_schema_extra={
            "ui:type": "select",
            "enum": ["none", "metadata", "content"],
            "ui:enumLabels": {
                "none": "None (live only)",
                "metadata": "File list",
                "content": "Contents (searchable)",
            },
        },
    )
    index_content: bool = Field(
        True,
        title="Index File Contents (legacy)",
        description="Deprecated — use Indexing above. Kept for back-compatibility.",
        json_schema_extra={"ui:type": "boolean", "ui:hidden": True}
    )
    max_catalog_objects: int = Field(
        10000,
        title="Max Files",
        description=(
            "Safety cap on how many files are ever enumerated (indexed or listed "
            "live). A directory with more than this is truncated to protect memory "
            "and the catalog — narrow the include-patterns to scope large trees."
        ),
        json_schema_extra={"ui:type": "number"},
    )


# Amazon S3 — a cloud object store exposed as a file catalog.
# Credentials mirror the Athena idiom so boto3 session construction is shared:
# static keys, keys + STS assume-role, or the default credential chain.
class S3KeyCredentials(BaseModel):
    access_key: str = Field(..., title="Access Key", description="AWS access key id.", json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="Secret Key", description="AWS secret access key.", json_schema_extra={"ui:type": "password"})
    session_token: Optional[str] = Field(None, title="Session Token", description="Optional session token for temporary credentials.", json_schema_extra={"ui:type": "password"})


class S3RoleCredentials(BaseModel):
    role_arn: str = Field(..., title="Role ARN", description="ARN of the IAM role to assume (STS) for bucket access.", json_schema_extra={"ui:type": "string"})
    # Static keys are optional: leave blank to assume the role using the
    # deployment's ambient credentials (EC2 instance profile / EKS IRSA / env),
    # mirroring the Athena connector.
    access_key: Optional[str] = Field(None, title="Access Key", description="Optional — access key id used to assume the role. Leave blank to use the instance profile / IRSA.", json_schema_extra={"ui:type": "string"})
    secret_key: Optional[str] = Field(None, title="Secret Key", description="Optional — secret access key used to assume the role. Leave blank to use the instance profile / IRSA.", json_schema_extra={"ui:type": "password"})


class S3DefaultCredentials(BaseModel):
    """No credentials required — boto3 resolves via its default chain (env vars,
    shared config, instance profile, IRSA). Mirrors AWSAthenaDefaultCredentials."""
    class Config:
        extra = "allow"


class S3Config(BaseModel):
    bucket: str = Field(
        ...,
        title="Bucket",
        description="S3 bucket name (e.g. 'my-company-reports').",
        json_schema_extra={"ui:type": "string"},
    )
    prefix: Optional[str] = Field(
        None,
        title="Prefix",
        description=(
            "Key prefix to scope the connection (e.g. 'reports/2025/'). All "
            "listing and reads are confined to this prefix. Leave blank for the "
            "whole bucket."
        ),
        json_schema_extra={"ui:type": "string"},
    )
    region: Optional[str] = Field(
        None,
        title="Region",
        description="AWS region of the bucket (e.g. 'us-west-2'). Recommended — avoids a redirect on the first call.",
        json_schema_extra={"ui:type": "string"},
    )
    endpoint_url: Optional[str] = Field(
        None,
        title="Endpoint URL",
        description="Custom S3 endpoint for S3-compatible stores (MinIO / Cloudflare R2 / Wasabi). Leave blank for AWS.",
        json_schema_extra={"ui:type": "string"},
    )
    allowed_extensions: Optional[str] = Field(
        None,
        title="Allowed Extensions (legacy)",
        description="Deprecated — use Include Patterns with '**/*.ext'. Kept for back-compatibility.",
        json_schema_extra={"ui:type": "string", "ui:hidden": True},
    )
    include_globs: Optional[str] = Field(
        None,
        title="Include Patterns (globs)",
        description=(
            "Glob patterns relative to the prefix — the connection's scope. When "
            "set, ONLY matching objects are visible AND readable; access to "
            "anything else in the prefix is denied. Filter by folder AND type "
            "here: 'docs/**/*.pdf', '**/*.csv', 'reports/**'. Comma-separated; "
            "'**' crosses sub-prefixes, '*' is one segment. Blank = whole prefix. "
            "(The Prefix above is the efficient server-side base; globs refine "
            "within it.)"
        ),
        json_schema_extra={"ui:type": "string"},
    )
    recursive: bool = Field(
        True,
        title="Include Sub-prefixes",
        description="Recursively enumerate keys under the prefix when listing / indexing.",
        json_schema_extra={"ui:type": "boolean"},
    )
    max_file_mb: int = Field(
        100,
        title="Max File Size (MB)",
        description="Reject whole-object (structured) reads above this size. Windowed byte-range reads are exempt.",
        json_schema_extra={"ui:type": "number"},
    )
    index_mode: str = Field(
        "content",
        title="Indexing",
        description=(
            "How much to cache from the bucket. 'none' → nothing cached; list/"
            "read go live (best for huge or volatile buckets). 'metadata' → "
            "cache the object list only. 'content' → also extract keywords for "
            "topic search. Reads are always live regardless."
        ),
        json_schema_extra={
            "ui:type": "select",
            "enum": ["none", "metadata", "content"],
            "ui:enumLabels": {
                "none": "None (live only)",
                "metadata": "Object list",
                "content": "Contents (searchable)",
            },
        },
    )
    index_content: bool = Field(
        True,
        title="Index File Contents (legacy)",
        description="Deprecated — use Indexing above. Kept for back-compatibility.",
        json_schema_extra={"ui:type": "boolean", "ui:hidden": True},
    )
    max_catalog_objects: int = Field(
        5000,
        title="Max Catalog Objects",
        description="Cap on objects indexed into the catalog, so a huge bucket doesn't produce an unbounded catalog.",
        json_schema_extra={"ui:type": "number"},
    )


# QVD Files (QlikView Data)
class QVDCredentials(BaseModel):
    """No credentials needed - file system access only."""
    class Config:
        extra = "allow"


class QVDConfig(BaseModel):
    file_paths: str = Field(
        ...,
        title="File Paths",
        description="QVD file paths or glob patterns (one per line). e.g., /data/*.qvd",
        json_schema_extra={"ui:type": "textarea"}
    )


# CSV Files (comma/delimiter-separated values)
class CSVCredentials(BaseModel):
    """No credentials needed - file system access only."""
    class Config:
        extra = "allow"


class CSVConfig(BaseModel):
    file_paths: str = Field(
        ...,
        title="File Paths",
        description="CSV file paths or glob patterns (one per line). e.g., /data/*.csv",
        json_schema_extra={"ui:type": "textarea"}
    )
    delimiter: str = Field(
        "",
        title="Delimiter",
        description="Column delimiter. Leave blank to auto-detect. e.g., ',' ';' '|' or '\\t' for tab.",
        json_schema_extra={"ui:type": "string"}
    )
    has_header: bool = Field(
        True,
        title="Has Header Row",
        description="Whether the first row contains column names.",
        json_schema_extra={"ui:type": "boolean"}
    )
    encoding: str = Field(
        "utf-8",
        title="Encoding",
        description="File encoding. e.g., utf-8, latin-1.",
        json_schema_extra={"ui:type": "string"}
    )


# Qlik Sense (live connector — Qlik Cloud)
class QlikSenseApiKeyCredentials(BaseModel):
    api_key: str = Field(
        ...,
        title="API Key",
        description="Qlik Cloud API key (bearer token). Generate at 'Settings > API keys' on the tenant.",
        json_schema_extra={"ui:type": "password"},
    )


class QlikSenseOAuthM2MCredentials(BaseModel):
    """OAuth 2.0 Client Credentials (machine-to-machine) for Qlik Cloud.

    Register an OAuth client in the tenant ('Management Console > Integrations >
    OAuth') with grant type 'client_credentials' and copy the client id/secret
    here. Short-lived access tokens are fetched and refreshed automatically.
    """
    client_id: str = Field(
        ...,
        title="OAuth Client ID",
        description="OAuth client ID from the Qlik Cloud Management Console.",
        json_schema_extra={"ui:type": "string"},
    )
    client_secret: str = Field(
        ...,
        title="OAuth Client Secret",
        description="OAuth client secret that pairs with the client ID.",
        json_schema_extra={"ui:type": "password"},
    )
    scope: Optional[str] = Field(
        "user_default",
        title="Scope",
        description="OAuth scope requested at token exchange. Default 'user_default' covers standard Qlik Cloud APIs.",
        json_schema_extra={"ui:type": "string"},
    )


class QlikSenseConfig(BaseModel):
    base_url: str = Field(
        ...,
        title="Base URL",
        description=(
            "Qlik Cloud tenant base URL. "
            "Example: https://tenant.us.qlikcloud.com. "
            "(On-prem Qlik Sense Enterprise on Windows is not supported in v1.)"
        ),
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify TLS certificate when calling Qlik REST and WebSocket endpoints.",
        json_schema_extra={"ui:type": "boolean"},
    )
    space_filter: Optional[str] = Field(
        None,
        title="Space Filter",
        description="Optional comma-separated list of space IDs or names. If empty, all visible spaces are crawled.",
        json_schema_extra={"ui:type": "string"},
    )


# Timbr Semantic Layer
class TimbrTokenCredentials(BaseModel):
    api_key: str = Field(
        ...,
        title="API Key",
        description="Timbr API key for authentication",
        json_schema_extra={"ui:type": "password"},
    )


class TimbrConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host",
        description="Timbr server URL (e.g., https://mytimbr.example.com)",
        json_schema_extra={"ui:type": "string"},
    )
    ontology: str = Field(
        ...,
        title="Ontology",
        description="Name of the Timbr knowledge graph / ontology to connect to",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify SSL certificate when connecting",
        json_schema_extra={"ui:type": "boolean"},
    )


# Timbr A2A (Agent-to-Agent)
class TimbrA2ATokenCredentials(BaseModel):
    api_key: str = Field(
        ...,
        title="API Key",
        description="Timbr API key for authentication",
        json_schema_extra={"ui:type": "password"},
    )


class TimbrA2AConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host",
        description="Timbr server URL (e.g., https://mytimbr.example.com)",
        json_schema_extra={"ui:type": "string"},
    )
    ontology: str = Field(
        ...,
        title="Ontology",
        description="Name of the Timbr knowledge graph / ontology to connect to",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify SSL certificate when connecting",
        json_schema_extra={"ui:type": "boolean"},
    )
    results_limit: int = Field(
        500,
        title="Results Limit",
        description="Maximum number of rows returned per query",
        json_schema_extra={"ui:type": "number"},
    )
    graph_depth: int = Field(
        1,
        title="Graph Depth",
        description="Depth of ontology graph traversal",
        json_schema_extra={"ui:type": "number"},
    )
    retries: int = Field(
        3,
        title="Retries",
        description="Number of retries on query failure",
        json_schema_extra={"ui:type": "number"},
    )


# Oracle BI (OBIEE / Oracle Analytics Server / Oracle Analytics Cloud)
class OracleBICredentials(BaseModel):
    username: str = Field(
        ...,
        title="Username",
        description="Oracle BI / OAC username (email for OAC, domain user for OBIEE/OAS).",
        json_schema_extra={"ui:type": "string"},
    )
    password: str = Field(
        ...,
        title="Password",
        description="Password for the Oracle BI / OAC user.",
        json_schema_extra={"ui:type": "password"},
    )


class OracleBIConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host URL",
        description="Base URL of the Oracle BI instance (e.g., https://analytics.example.com or the OAC instance URL).",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify TLS certificate when calling the SOAP endpoint.",
        json_schema_extra={"ui:type": "boolean"},
    )
    timeout_sec: int = Field(
        60,
        ge=1,
        le=600,
        title="Timeout (sec)",
        description="HTTP timeout for SOAP calls.",
        json_schema_extra={"ui:type": "number"},
    )


# Infor OLAP (Infor d/EPM OLAP / Infor BI — XMLA Provider)
class InforOlapCredentials(BaseModel):
    username: str = Field(
        ...,
        title="Username",
        description="Infor OLAP user for the XMLA endpoint (Basic auth).",
        json_schema_extra={"ui:type": "string"},
    )
    password: str = Field(
        ...,
        title="Password",
        description="Password for the Infor OLAP user.",
        json_schema_extra={"ui:type": "password"},
    )


class InforOlapConfig(BaseModel):
    host: str = Field(
        ...,
        title="XMLA Endpoint URL",
        description=(
            "URL of a Database Worker XMLA endpoint, or — with manager "
            "auto-discovery enabled — the OLAP Service Manager endpoint "
            "(http(s)://<server>:<manager_port>/BI/APP/SOAP/OLAPDB)."
        ),
        json_schema_extra={"ui:type": "string"},
    )
    catalog: str = Field(
        "",
        title="Catalog",
        description="Optional OLAP catalog/database to scope discovery to. Leave blank to list all accessible catalogs.",
        json_schema_extra={"ui:type": "string"},
    )
    manager_discovery: bool = Field(
        False,
        title="Manager auto-discovery",
        description=(
            "Treat the URL as the OLAP Service Manager endpoint and resolve the "
            "database's worker URL via DISCOVER_DATASOURCES (Infor's documented "
            "connection flow). Leave off when the URL already points at a worker."
        ),
        json_schema_extra={"ui:type": "boolean"},
    )
    rewrite_worker_host: bool = Field(
        True,
        title="Use configured host for discovered URLs",
        description=(
            "Replace the hostname in discovered worker URLs with the host from "
            "the endpoint URL. Farms usually advertise internal hostnames that "
            "do not resolve from outside; disable only when workers run on "
            "different machines and their hostnames are resolvable."
        ),
        json_schema_extra={"ui:type": "boolean"},
    )
    tenant: str = Field(
        "single",
        title="Tenant",
        description="XMLA Tenant property sent during manager discovery ('single' for on-premises single-tenant farms).",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify TLS certificate when calling the XMLA endpoint.",
        json_schema_extra={"ui:type": "boolean"},
    )
    timeout_sec: int = Field(
        60,
        ge=1,
        le=600,
        title="Timeout (sec)",
        description="HTTP timeout for XMLA calls.",
        json_schema_extra={"ui:type": "number"},
    )


# Microsoft Analysis Services (SSAS — Multidimensional & Tabular, via XMLA)
class AnalysisServicesCredentials(BaseModel):
    username: str = Field(
        ...,
        title="Username",
        description="SSAS user for the XMLA endpoint (Basic auth).",
        json_schema_extra={"ui:type": "string"},
    )
    password: str = Field(
        ...,
        title="Password",
        description="Password for the SSAS user.",
        json_schema_extra={"ui:type": "password"},
    )


class AnalysisServicesConfig(BaseModel):
    host: str = Field(
        ...,
        title="XMLA Endpoint URL",
        description=(
            "Full URL of the SSAS XMLA endpoint, typically the IIS msmdpump "
            "pump (e.g., https://server/olap/msmdpump.dll)."
        ),
        json_schema_extra={"ui:type": "string"},
    )
    catalog: str = Field(
        "",
        title="Catalog",
        description="Optional database/catalog to scope discovery to. Leave blank to list all accessible catalogs.",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify SSL",
        description="Verify TLS certificate when calling the XMLA endpoint.",
        json_schema_extra={"ui:type": "boolean"},
    )
    timeout_sec: int = Field(
        60,
        ge=1,
        le=600,
        title="Timeout (sec)",
        description="HTTP timeout for XMLA calls.",
        json_schema_extra={"ui:type": "number"},
    )


# Sisense
class SisenseCredentials(BaseModel):
    username: str = Field(
        "",
        title="Username",
        description="Sisense username (email). Leave blank if using API token.",
        json_schema_extra={"ui:type": "string"}
    )
    password: str = Field(
        "",
        title="Password",
        description="Sisense password. Leave blank if using API token.",
        json_schema_extra={"ui:type": "password"}
    )
    api_token: str = Field(
        "",
        title="API Token",
        description="Pre-existing Sisense API bearer token. If provided, username/password are ignored.",
        json_schema_extra={"ui:type": "password"}
    )

    @model_validator(mode="after")
    def validate_auth(cls, model: "SisenseCredentials") -> "SisenseCredentials":
        has_userpass = model.username and model.password
        has_token = bool(model.api_token)
        if not has_userpass and not has_token:
            raise ValueError("Either username/password or api_token must be provided.")
        return model


class SisenseConfig(BaseModel):
    host: str = Field(
        ...,
        title="Host",
        description="Sisense server URL (e.g., https://sisense.company.com)",
        json_schema_extra={"ui:type": "string"}
    )


# MCP Server
class MCPConfig(BaseModel):
    server_url: str = Field(
        ...,
        title="Server URL",
        description="URL of the MCP server (e.g., http://localhost:3000/mcp)",
        json_schema_extra={"ui:type": "string"}
    )
    transport: str = Field(
        "sse",
        title="Transport",
        description="MCP transport protocol",
        json_schema_extra={"ui:type": "select", "options": ["sse", "streamable_http"]}
    )


class MCPNoAuthCredentials(BaseModel):
    class Config:
        extra = "allow"


class MCPBearerCredentials(BaseModel):
    token: str = Field(
        ...,
        title="Bearer Token",
        description="Bearer token for authenticating with the MCP server",
        json_schema_extra={"ui:type": "password"}
    )


class MCPOAuthAppCredentials(BaseModel):
    """Pre-configured OAuth client for an MCP server.

    The admin registers an OAuth client at the identity provider that fronts
    the MCP server (or at the MCP server itself if it's also the authorization
    server). Each user then completes the authorization-code + PKCE dance and
    their per-user access_token is sent to the MCP server on every tool call.
    """
    authorize_url: str = Field(
        ...,
        title="Authorize URL",
        description="OAuth authorization endpoint (e.g. https://idp.example.com/oauth/authorize)",
        json_schema_extra={"ui:type": "string"}
    )
    token_url: str = Field(
        ...,
        title="Token URL",
        description="OAuth token endpoint (e.g. https://idp.example.com/oauth/token)",
        json_schema_extra={"ui:type": "string"}
    )
    client_id: str = Field(
        ...,
        title="Client ID",
        description="OAuth client ID registered at the identity provider",
        json_schema_extra={"ui:type": "string"}
    )
    client_secret: str = Field(
        ...,
        title="Client Secret",
        description="OAuth client secret",
        json_schema_extra={"ui:type": "password"}
    )
    scopes: Optional[str] = Field(
        None,
        title="Scopes",
        description="Space-separated OAuth scopes (e.g. 'openid profile offline_access read:files')",
        json_schema_extra={"ui:type": "string"}
    )
    audience: Optional[str] = Field(
        None,
        title="Resource (Audience)",
        description="Optional RFC 8707 resource indicator — usually the MCP server's URL — to audience-bind the issued token.",
        json_schema_extra={"ui:type": "string"}
    )


# Custom API
class CustomAPIConfig(BaseModel):
    base_url: str = Field(
        ...,
        title="Base URL",
        description="Base URL for the API (e.g., https://api.example.com/v1)",
        json_schema_extra={"ui:type": "string"}
    )
    headers: dict = Field(
        default={},
        title="Custom Headers",
        description="Additional headers to send with every request (e.g., ontology, results-limit)",
        json_schema_extra={"ui:type": "keyvalue"}
    )
    endpoints: list = Field(
        default=[],
        title="Endpoints",
        description="List of API endpoint definitions",
        json_schema_extra={"ui:type": "json"}
    )


class CustomAPINoAuthCredentials(BaseModel):
    class Config:
        extra = "allow"


class CustomAPIBearerCredentials(BaseModel):
    token: str = Field(
        ...,
        title="Bearer Token",
        description="Bearer token for API authentication",
        json_schema_extra={"ui:type": "password"}
    )


class CustomAPIKeyCredentials(BaseModel):
    api_key: str = Field(
        ...,
        title="API Key",
        description="API key for authentication",
        json_schema_extra={"ui:type": "password"}
    )
    api_key_header: str = Field(
        "X-API-Key",
        title="API Key Header",
        description="Header name for the API key",
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
    "TrinoConfig",
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
    "CSVConfig",
    "CSVCredentials",
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
    "TrinoCredentials",
    "GoogleAnalyticsCredentials",
    "GCPCredentials",
    "AWSCostCredentials",
    "AWSAthenaCredentials",
    "AWSAthenaDefaultCredentials",
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
    # Spark Connect
    "SparkConnectNoAuthCredentials",
    "SparkConnectCredentials",
    "SparkConnectConfig",
    # Power BI
    "PowerBICredentials",
    "PowerBIConfig",
    # QVD Files
    "QVDCredentials",
    "QVDConfig",
    # Qlik Sense (live connector)
    "QlikSenseApiKeyCredentials",
    "QlikSenseOAuthM2MCredentials",
    "QlikSenseConfig",
    # Microsoft Fabric
    "MSFabricCredentials",
    "MSFabricConfig",
    # SharePoint / OneDrive / Google Drive (file connectors)
    "SharePointCredentials",
    "SharePointConfig",
    "OneDriveCredentials",
    "OneDriveConfig",
    "GoogleDriveCredentials",
    "GoogleDriveConfig",
    # Sybase SQL Anywhere
    "SybaseConfig",
    # Teradata
    "TeradataCredentials",
    "TeradataConfig",
    # Timbr
    "TimbrTokenCredentials",
    "TimbrConfig",
    # Sisense
    "SisenseCredentials",
    "SisenseConfig",
    # MCP
    "MCPConfig",
    "MCPNoAuthCredentials",
    "MCPBearerCredentials",
    "MCPOAuthAppCredentials",
    # Custom API
    "CustomAPIConfig",
    "CustomAPINoAuthCredentials",
    "CustomAPIBearerCredentials",
    "CustomAPIKeyCredentials",
]