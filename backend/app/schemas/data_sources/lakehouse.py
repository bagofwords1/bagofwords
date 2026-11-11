"""Config + credentials schemas for the Apache Iceberg, Dremio and Apache
Hive / Impala connectors. These Pydantic models ARE the connect form.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# ------------------------------------------------------------------- Iceberg

class IcebergNoAuthCredentials(BaseModel):
    """REST catalog without authentication (local / network-protected)."""

    class Config:
        extra = "allow"


class IcebergBearerCredentials(BaseModel):
    token: str = Field(..., title="Token", description="Bearer token for the REST catalog.", json_schema_extra={"ui:type": "password"})


class IcebergOAuthCredentials(BaseModel):
    client_id: str = Field(..., title="Client ID", description="OAuth2 client (service principal) id.", json_schema_extra={"ui:type": "string"})
    client_secret: str = Field(..., title="Client Secret", description="", json_schema_extra={"ui:type": "password"})
    scope: str | None = Field(
        None,
        title="Scope",
        description="OAuth2 scope, e.g. PRINCIPAL_ROLE:ALL (Polaris / Snowflake Open Catalog). Leave blank for the catalog's default.",
        json_schema_extra={"ui:type": "string"},
    )
    oauth2_server_uri: str | None = Field(
        None,
        title="Token Endpoint",
        description="OAuth2 token URL. Leave blank to use <catalog URI>/v1/oauth/tokens.",
        json_schema_extra={"ui:type": "string"},
    )


class IcebergAwsKeyCredentials(BaseModel):
    access_key: str = Field(..., title="Access Key", description="AWS access key id (Glue / S3 Tables catalogs and S3 data).", json_schema_extra={"ui:type": "string"})
    secret_key: str = Field(..., title="Secret Key", description="AWS secret access key.", json_schema_extra={"ui:type": "password"})
    session_token: str | None = Field(None, title="Session Token", description="Optional session token for temporary credentials.", json_schema_extra={"ui:type": "password"})


class IcebergAwsDefaultCredentials(BaseModel):
    """No stored secret — the AWS default chain (instance profile, IRSA, env)."""

    class Config:
        extra = "allow"


class IcebergConfig(BaseModel):
    catalog_type: str = Field(
        "rest",
        title="Catalog",
        description="Where the table metadata lives.",
        json_schema_extra={
            "ui:type": "select",
            "enum": ["rest", "glue", "s3tables"],
            "ui:enumLabels": {
                "rest": "Iceberg REST catalog (Polaris, Nessie, Gravitino, Lakekeeper, Unity, …)",
                "glue": "AWS Glue Data Catalog",
                "s3tables": "Amazon S3 Tables",
            },
        },
    )
    uri: str | None = Field(
        None,
        title="Catalog URI",
        description="REST catalog base URL (e.g. https://polaris.acme.com/api/catalog). Not used for Glue / S3 Tables.",
        json_schema_extra={"ui:type": "string"},
    )
    warehouse: str | None = Field(
        None,
        title="Warehouse",
        description=(
            "REST: the catalog / warehouse name. S3 Tables: the table bucket ARN "
            "(arn:aws:s3tables:<region>:<account>:bucket/<name>). Glue: optional."
        ),
        json_schema_extra={"ui:type": "string"},
    )
    region: str | None = Field(
        None,
        title="AWS Region",
        description="Region of the Glue catalog / S3 Tables bucket / S3 data (e.g. us-east-1).",
        json_schema_extra={"ui:type": "string"},
    )
    namespaces: str | None = Field(
        None,
        title="Namespaces",
        description="Comma-separated namespaces (databases) to expose. Leave blank for all.",
        json_schema_extra={"ui:type": "string"},
    )
    vended_credentials: bool = Field(
        True,
        title="Use Vended Credentials",
        description="Let a REST catalog hand out short-lived storage credentials per table (recommended).",
        json_schema_extra={"ui:type": "boolean"},
    )
    s3_endpoint: str | None = Field(
        None,
        title="S3 Endpoint",
        description="Custom S3-compatible endpoint for the data files (MinIO, R2, …). Leave blank for AWS.",
        json_schema_extra={"ui:type": "string"},
    )
    max_scan_rows: int = Field(
        20_000_000,
        ge=1,
        title="Max Rows Scanned per Query",
        description="Refuse a query whose files (after partition pruning) hold more rows than this; add filters instead.",
        json_schema_extra={"ui:type": "number"},
    )
    extra_properties: str | None = Field(
        None,
        title="Extra Catalog Properties (JSON)",
        description='Advanced: extra PyIceberg catalog / FileIO properties, e.g. {"adls.account-name": "acme"}.',
        json_schema_extra={"ui:type": "textarea"},
    )


# -------------------------------------------------------------------- Dremio

class DremioTokenCredentials(BaseModel):
    token: str = Field(
        ...,
        title="Personal Access Token",
        description="Account Settings → Personal Access Tokens (Dremio Cloud and Software).",
        json_schema_extra={"ui:type": "password"},
    )


class DremioPasswordCredentials(BaseModel):
    username: str = Field(..., title="Username", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class DremioConfig(BaseModel):
    url: str = Field(
        ...,
        title="URL",
        description="Dremio Software (e.g. https://dremio.acme.com:9047) or Dremio Cloud API URL (https://api.dremio.cloud).",
        json_schema_extra={"ui:type": "string"},
    )
    project_id: str | None = Field(
        None,
        title="Project ID",
        description="Dremio Cloud only: the project to query.",
        json_schema_extra={"ui:type": "string"},
    )
    spaces: str | None = Field(
        None,
        title="Spaces / Sources",
        description="Comma-separated top-level spaces or sources to expose (e.g. Marketing,lakehouse). Leave blank for all.",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(True, title="Verify TLS Certificate", description="", json_schema_extra={"ui:type": "boolean"})
    max_rows: int = Field(
        100_000,
        ge=1,
        title="Max Rows per Query",
        description="Cap on rows returned by one query.",
        json_schema_extra={"ui:type": "number"},
    )


# ------------------------------------------------------------ Hive / Impala

class HiveNoAuthCredentials(BaseModel):
    username: str | None = Field(None, title="Username", description="Run queries as this user (no password check).", json_schema_extra={"ui:type": "string"})


class HiveLdapCredentials(BaseModel):
    username: str = Field(..., title="Username", description="", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class HiveConfig(BaseModel):
    host: str = Field(..., title="Host", description="HiveServer2 / Impala daemon / Spark Thrift Server host.", json_schema_extra={"ui:type": "string"})
    port: int = Field(
        10000, ge=1, le=65535,
        title="Port",
        description="10000 for HiveServer2 and Spark Thrift, 21050 for Impala.",
        json_schema_extra={"ui:type": "number"},
    )
    engine: str = Field(
        "hive",
        title="Engine",
        description="Which SQL dialect the agent should write.",
        json_schema_extra={
            "ui:type": "select",
            "enum": ["hive", "impala", "spark"],
            "ui:enumLabels": {"hive": "Apache Hive", "impala": "Apache Impala", "spark": "Spark Thrift Server"},
        },
    )
    databases: str | None = Field(
        None,
        title="Databases",
        description="Comma-separated databases to expose (e.g. sales,finance). Leave blank for all.",
        json_schema_extra={"ui:type": "string"},
    )
    max_rows: int = Field(
        100_000, ge=1,
        title="Max Rows per Query",
        description="Cap on rows returned by one query.",
        json_schema_extra={"ui:type": "number"},
    )
