from __future__ import annotations

from typing import Any, Dict, Optional, Type

from pydantic import BaseModel

# Import provider Config/Credentials from the provider module
from app.schemas.data_sources.configs import (
    # Configs
    PostgreSQLConfig,
    SQLiteConfig,
    OracleConfig,
    SnowflakeConfig,
    BigQueryConfig,
    NetSuiteConfig,
    SQLConfig,
    MssqlConfig,
    PrestoConfig,
    GoogleAnalyticsConfig,
    GCPConfig,
    AWSCostConfig,
    AWSAthenaConfig,
    VerticaConfig,
    AwsRedshiftConfig,
    TableauConfig,
    SalesforceConfig,
    ClickhouseConfig,
    PinotConfig,
    MongoDBConfig,
    PostHogConfig,
    # DuckDB
    DuckDBConfig,
    DuckDBNoAuthCredentials,
    DuckDBAwsCredentials,
    DuckDBGcpCredentials,
    DuckDBAzureCredentials,
    # Azure Data Explorer
    AzureDataExplorerConfig,
    AzureDataExplorerCredentials,
    # Databricks SQL
    DatabricksSqlConfig,
    DatabricksSqlCredentials,
    # Power BI
    PowerBIConfig,
    PowerBICredentials,
    # Power BI Report Server (on-prem)
    PowerBIReportServerConfig,
    PowerBIReportServerCredentials,
    # QVD Files
    QVDConfig,
    QVDCredentials,
    # Qlik Sense (live connector)
    QlikSenseConfig,
    QlikSenseApiKeyCredentials,
    QlikSenseOAuthM2MCredentials,
    # Microsoft Fabric
    MSFabricConfig,
    MSFabricCredentials,
    # SharePoint / OneDrive / Google Drive (file connectors)
    SharePointConfig,
    SharePointCredentials,
    OneDriveConfig,
    OneDriveCredentials,
    GoogleDriveConfig,
    GoogleDriveCredentials,
    # Sybase SQL Anywhere
    SybaseConfig,
    # Timbr
    TimbrConfig,
    TimbrTokenCredentials,
    TimbrA2AConfig,
    TimbrA2ATokenCredentials,
    # Sisense
    SisenseConfig,
    SisenseCredentials,
    # Oracle BI (OBIEE / OAS / OAC)
    OracleBIConfig,
    OracleBICredentials,
    # Credentials
    PostgreSQLCredentials,
    SQLiteCredentials,
    OracleCredentials,
    SnowflakeCredentials,
    SnowflakeKeypairCredentials,
    BigQueryCredentials,
    NetSuiteCredentials,
    SQLCredentials,
    PrestoCredentials,
    GoogleAnalyticsCredentials,
    GCPCredentials,
    AWSCostCredentials,
    AWSAthenaCredentials,
    AWSAthenaDefaultCredentials,
    VerticaCredentials,
    AwsRedshiftUserPassCredentials,
    AwsRedshiftIAMCredentials,
    AwsRedshiftAssumeRoleCredentials,
    TableauPATCredentials,
    SalesforceCredentials,
    MongoDBCredentials,
    PostHogCredentials,
    # MCP
    MCPConfig,
    MCPNoAuthCredentials,
    MCPBearerCredentials,
    MCPOAuthAppCredentials,
    # Custom API
    CustomAPIConfig,
    CustomAPINoAuthCredentials,
    CustomAPIBearerCredentials,
    CustomAPIKeyCredentials,
    # OAuth Delegated
    OAuthDelegatedCredentials,
)

from app.settings.config import settings


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
    dev_only: bool = False
    # Legacy flag — derived from `data_shape != "tables"`. Kept for backwards
    # compatibility with callers reading `client.is_document_based`. New code
    # should branch on `data_shape` directly.
    is_document_based: bool = False
    # License tier required to use this data source (e.g., "enterprise")
    requires_license: Optional[str] = None
    # Whether this entry is a traditional data source connection (vs a tool provider like MCP/Custom API)
    is_connection: bool = True

    # ── Connection-shape axes ───────────────────────────────────────────────
    #
    # `data_shape` describes what the agent sees at runtime. Determines copy
    # ("Found N files" vs "Found N tables" vs "N tools available"), how the
    # planner refers to it, and how the agent prompt is templated.
    #
    # `catalog_ownership` describes where the catalog comes from. Critical
    # because per-user-owned catalogs (OneDrive, personal Drive) have NO
    # admin-side catalog — each user's catalog is fully independent, not a
    # filtered subset of an admin universe. The indexing pipeline and UX
    # branch on this.
    #
    #   shared    → admin connection has a single catalog of truth; user
    #               overlays are ACL-filtered subsets (Postgres, SharePoint
    #               site, Power BI with RLS).
    #   per_user  → each user's catalog is independent and primary; admin
    #               connection has no catalog (OneDrive, personal Drive,
    #               personal Notion).
    #   none      → no catalog at all; runtime tool calls only (MCP, REST).
    #
    # `ui_form` selects the admin-side create form on the frontend. Decoupled
    # from data_shape and catalog_ownership so e.g. OneDrive can be a
    # per-user files catalog AND use the lean Integration form.
    data_shape: str = "tables"          # tables | files | objects | tools
    catalog_ownership: str = "shared"   # shared | per_user | none
    ui_form: str = "data_source"        # data_source | integration | mcp | custom_api

    class Config:
        arbitrary_types_allowed = True


_DEV_ENVIRONMENTS = {"development", "dev", "test", "testing"}


def _is_dev_environment() -> bool:
    try:
        if getattr(settings, "TESTING", False):
            return True
        env = (settings.ENVIRONMENT or "").lower()
    except Exception:
        return False
    return env in _DEV_ENVIRONMENTS


def _entry_visible(entry: DataSourceRegistryEntry) -> bool:
    if not entry.dev_only:
        return True
    return _is_dev_environment()


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
    "sqlite": DataSourceRegistryEntry(
        type="sqlite",
        title="SQLite",
        description="Query local SQLite database files. Supports absolute paths.",
        config_schema=SQLiteConfig,
        credentials_auth=AuthOptions(
            default="none",
            by_auth={
                "none": AuthVariant(
                    title="No Auth Required",
                    schema=SQLiteCredentials,
                    scopes=["system"],
                )
            },
        ),
        client_path="app.data_sources.clients.sqlite_client.SqliteClient",
    ),
    "oracledb": DataSourceRegistryEntry(
        type="oracledb",
        title="Oracle Database",
        description="Enterprise-grade relational database. Connect via service name; optional schema scoping.",
        config_schema=OracleConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=OracleCredentials, scopes=["system","user"])
        }),
        client_path=None
    ),
    "snowflake": DataSourceRegistryEntry(
        type="snowflake",
        title="Snowflake",
        description="Cloud-based data warehousing platform that supports SQL queries.",
        config_schema=SnowflakeConfig,
        credentials_auth=AuthOptions(
            default="userpass",
            by_auth={
                "userpass": AuthVariant(
                    title="Username / Password",
                    schema=SnowflakeCredentials,
                    scopes=["system", "user"],
                ),
                "keypair": AuthVariant(
                    title="Key Pair (Private Key)",
                    schema=SnowflakeKeypairCredentials,
                    scopes=["system", "user"],
                ),
            },
        ),
        client_path="app.data_sources.clients.snowflake_client.SnowflakeClient",
    ),
    "bigquery": DataSourceRegistryEntry(
        type="bigquery",
        title="Google BigQuery",
        description="Serverless, highly scalable, and cost-effective multi-cloud data warehouse.",
        config_schema=BigQueryConfig,
        credentials_auth=AuthOptions(default="service_account", by_auth={
            "service_account": AuthVariant(title="Service Account JSON", schema=BigQueryCredentials, scopes=["system", "user"]),
            "oauth": AuthVariant(title="Sign in with Google", schema=OAuthDelegatedCredentials, scopes=["user"]),
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
        status="active",
        version="1.0.0",
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
        credentials_auth=AuthOptions(default="default", by_auth={
            "default": AuthVariant(title="AWS Default (IAM Role / Instance Profile)", schema=AWSAthenaDefaultCredentials, scopes=["system", "user"]),
            "key": AuthVariant(title="AWS Access Keys", schema=AWSAthenaCredentials, scopes=["system", "user"]),
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
            "userpass": AuthVariant(title="Username / Password", schema=SalesforceCredentials, scopes=["system", "user"])  # likely system
        }),
        client_path=None,
    ),
    "MSSQL": DataSourceRegistryEntry(
        type="MSSQL",
        title="Microsoft SQL Server",
        description="MSSQL is Microsoft's relational database for managing and analyzing data.",
        config_schema=MssqlConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SQLCredentials, scopes=["system","user"])
        }),
        client_path=None,
    ),
    "clickhouse": DataSourceRegistryEntry(
        type="clickhouse",
        title="ClickHouse",
        description="ClickHouse is a fast, open-source columnar database for real-time analytics.",
        config_schema=ClickhouseConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SQLCredentials, scopes=["system","user"])
        }),
        client_path=None,
    ),
    "azure_data_explorer": DataSourceRegistryEntry(
        type="azure_data_explorer",
        title="Azure Data Explorer",
        description="Azure Data Explorer (Kusto) is a fast and highly scalable data exploration service for log and telemetry data.",
        config_schema=AzureDataExplorerConfig,
        credentials_auth=AuthOptions(default="service_principal", by_auth={
            "service_principal": AuthVariant(title="Service Principal (AAD App)", schema=AzureDataExplorerCredentials, scopes=["system", "user"])
        }),
        client_path="app.data_sources.clients.azure_data_explorer_client.AzureDataExplorerClient",
    ),
    "pinot": DataSourceRegistryEntry(
        type="pinot",
        title="Apache Pinot",
        description="Real-time OLAP datastore queried via Broker SQL API.",
        config_schema=PinotConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SQLCredentials, scopes=["system","user"])
        }),
        client_path=None,
        version="beta",
    ),
    "aws_cost": DataSourceRegistryEntry(
        type="aws_cost",
        title="AWS Cost Explorer",
        description="AWS Cost Explorer helps analyze and visualize your AWS spending and usage patterns over time.",
        config_schema=AWSCostConfig,
        credentials_auth=AuthOptions(default="key", by_auth={
            "key": AuthVariant(title="AWS Keys", schema=AWSCostCredentials, scopes=["system", "user"])  # system
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
            "iam": AuthVariant(title="AWS Keys (IAM)", schema=AwsRedshiftIAMCredentials, scopes=["system", "user"]),
            "arn": AuthVariant(title="Assume Role (ARN)", schema=AwsRedshiftAssumeRoleCredentials, scopes=["system", "user"]),
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
        requires_license="enterprise",
    ),
    "duckdb": DataSourceRegistryEntry(
        type="duckdb",
        title="DuckDB",
        description="Query parquet/csv from S3/GCS/Azure/local via DuckDB views.",
        config_schema=DuckDBConfig,
        credentials_auth=AuthOptions(default="none", by_auth={
            "none": AuthVariant(title="No Auth (public/local)", schema=DuckDBNoAuthCredentials, scopes=["system"]),
            "aws": AuthVariant(title="AWS Keys", schema=DuckDBAwsCredentials, scopes=["system"]),
            "gcp": AuthVariant(title="GCP Service Account", schema=DuckDBGcpCredentials, scopes=["system","user"]),
            "azure": AuthVariant(title="Azure Connection String", schema=DuckDBAzureCredentials, scopes=["system"])  
        }),
        client_path="app.data_sources.clients.duckdb_client.DuckDBClient",
    ),
    "mongodb": DataSourceRegistryEntry(
        type="mongodb",
        title="MongoDB",
        description="Document-oriented NoSQL database for flexible, scalable applications.",
        config_schema=MongoDBConfig,
        credentials_auth=AuthOptions(
            default="userpass",
            by_auth={
                "userpass": AuthVariant(
                    title="Username / Password",
                    schema=MongoDBCredentials,
                    scopes=["system", "user"]
                )
            }
        ),
        client_path="app.data_sources.clients.mongodb_client.MongodbClient",
        is_document_based=True,
        data_shape="objects",
    ),
    "posthog": DataSourceRegistryEntry(
        type="posthog",
        title="PostHog",
        description="Product analytics platform - query events, users, sessions, and more with HogQL.",
        config_schema=PostHogConfig,
        credentials_auth=AuthOptions(
            default="api_key",
            by_auth={
                "api_key": AuthVariant(
                    title="Personal API Key",
                    schema=PostHogCredentials,
                    scopes=["system", "user"]
                )
            }
        ),
        client_path="app.data_sources.clients.posthog_client.PostHogClient",
        version="beta",
    ),
    "databricks_sql": DataSourceRegistryEntry(
        type="databricks_sql",
        title="Databricks SQL",
        description="Databricks SQL Warehouse - serverless data warehouse with Unity Catalog. Powers Genie AI/BI.",
        config_schema=DatabricksSqlConfig,
        credentials_auth=AuthOptions(
            default="pat",
            by_auth={
                "pat": AuthVariant(
                    title="Personal Access Token",
                    schema=DatabricksSqlCredentials,
                    scopes=["system", "user"]
                )
            }
        ),
        client_path="app.data_sources.clients.databricks_sql_client.DatabricksSqlClient",
    ),
    "powerbi": DataSourceRegistryEntry(
        type="powerbi",
        title="Power BI",
        description="Query Power BI semantic models via DAX. Auto-discovers workspaces, datasets, and reports.",
        config_schema=PowerBIConfig,
        credentials_auth=AuthOptions(
            default="service_principal",
            by_auth={
                "service_principal": AuthVariant(
                    title="Service Principal (Azure AD)",
                    schema=PowerBICredentials,
                    scopes=["system"]
                ),
                "oauth": AuthVariant(
                    title="Sign in with Microsoft",
                    schema=OAuthDelegatedCredentials,
                    scopes=["user"]
                ),
            }
        ),
        client_path="app.data_sources.clients.powerbi_client.PowerBIClient",
        requires_license="enterprise",
    ),
    "powerbi_report_server": DataSourceRegistryEntry(
        type="powerbi_report_server",
        title="Power BI Report Server",
        description="On-prem Power BI Report Server. Discovers reports, paginated reports, shared datasets, KPIs, and upstream data-source lineage via NTLM-authenticated REST. PBIX semantic models are queryable via DuckDB over a cached Parquet snapshot (data reflects the last PBIX refresh, not live upstream — connect the upstream source directly for live data).",
        config_schema=PowerBIReportServerConfig,
        credentials_auth=AuthOptions(
            default="userpass",
            by_auth={
                "userpass": AuthVariant(
                    title="Username / Password (NTLM)",
                    schema=PowerBIReportServerCredentials,
                    scopes=["system", "user"]
                )
            }
        ),
        client_path="app.data_sources.clients.powerbi_report_server_client.PowerBIReportServerClient",
        requires_license="enterprise",
    ),
    "qvd": DataSourceRegistryEntry(
        type="qvd",
        title="Qlik (QVD)",
        description="Query Qlik (.qvd) files.",
        config_schema=QVDConfig,
        credentials_auth=AuthOptions(
            default="none",
            by_auth={
                "none": AuthVariant(
                    title="No Authentication",
                    schema=QVDCredentials,
                    scopes=["system"]
                )
            }
        ),
        client_path="app.data_sources.clients.qvd_client.QVDClient",
        requires_license="enterprise",
    ),
    "qlik_sense": DataSourceRegistryEntry(
        type="qlik_sense",
        title="Qlik Sense",
        description=(
            "Live Qlik Sense Cloud connector: discover apps (models) via REST and "
            "run hypercube queries against them via the Qlik Engine API (QIX) over WebSocket."
        ),
        config_schema=QlikSenseConfig,
        credentials_auth=AuthOptions(
            default="api_key",
            by_auth={
                "api_key": AuthVariant(
                    title="API Key",
                    schema=QlikSenseApiKeyCredentials,
                    scopes=["system", "user"],
                ),
                "oauth_m2m": AuthVariant(
                    title="OAuth 2.0 (Client Credentials)",
                    schema=QlikSenseOAuthM2MCredentials,
                    scopes=["system", "user"],
                ),
            },
        ),
        client_path="app.data_sources.clients.qlik_sense_client.QlikSenseClient",
        requires_license="enterprise",
    ),
    "sharepoint": DataSourceRegistryEntry(
        type="sharepoint",
        title="SharePoint",
        description="Read and analyze files from SharePoint document libraries — Excel, CSV, and documents become available to the agent.",
        config_schema=SharePointConfig,
        # Default captures the admin's Entra app credentials (tenant, client_id,
        # client_secret) — these are required by the OAuth flow even when each
        # user signs in individually. The "oauth" variant is the per-user flow
        # that consumes the admin app credentials at runtime.
        credentials_auth=AuthOptions(
            default="service_principal",
            by_auth={
                "service_principal": AuthVariant(
                    title="Entra ID App (Service Principal)",
                    schema=SharePointCredentials,
                    scopes=["system", "user"],
                ),
                "oauth": AuthVariant(
                    title="Sign in with Microsoft",
                    schema=OAuthDelegatedCredentials,
                    scopes=["user"],
                ),
            },
        ),
        client_path="app.data_sources.clients.graph_drive_client.SharepointClient",
        # SharePoint catalog is shared (admin curates a site/library); each
        # user's overlay is an ACL-filtered subset of that catalog.
        is_document_based=True,
        data_shape="files",
        catalog_ownership="shared",
        requires_license="enterprise",
    ),
    "onedrive": DataSourceRegistryEntry(
        type="onedrive",
        title="OneDrive",
        description="Read and analyze files from your OneDrive — Excel, CSV, and documents become available to the agent.",
        config_schema=OneDriveConfig,
        credentials_auth=AuthOptions(
            default="service_principal",
            by_auth={
                "service_principal": AuthVariant(
                    title="Entra ID App (Service Principal)",
                    schema=OneDriveCredentials,
                    scopes=["system", "user"],
                ),
                "oauth": AuthVariant(
                    title="Sign in with Microsoft",
                    schema=OAuthDelegatedCredentials,
                    scopes=["user"],
                ),
            },
        ),
        client_path="app.data_sources.clients.graph_drive_client.OnedriveClient",
        # Agent-attachable data source whose catalog is per-user-owned: each
        # user's OneDrive is fully independent (not a subset of an admin
        # universe). Admin save just registers the OAuth app; per-user
        # catalog is fetched after each user signs in.
        is_document_based=True,
        data_shape="files",
        catalog_ownership="per_user",
        ui_form="integration",
        requires_license="enterprise",
    ),
    "google_drive": DataSourceRegistryEntry(
        type="google_drive",
        title="Google Drive",
        description="Read and analyze files from your Google Drive — Sheets, Excel, CSV, and documents become available to the agent.",
        config_schema=GoogleDriveConfig,
        credentials_auth=AuthOptions(
            default="oauth_app",
            by_auth={
                "oauth_app": AuthVariant(
                    title="Google OAuth Client",
                    schema=GoogleDriveCredentials,
                    scopes=["system", "user"],
                ),
                "oauth": AuthVariant(
                    title="Sign in with Google",
                    schema=OAuthDelegatedCredentials,
                    scopes=["user"],
                ),
            },
        ),
        client_path="app.data_sources.clients.google_drive_client.GoogleDriveClient",
        is_document_based=True,
        data_shape="files",
        catalog_ownership="per_user",
        ui_form="integration",
        requires_license="enterprise",
    ),
    "ms_fabric": DataSourceRegistryEntry(
        type="ms_fabric",
        title="Microsoft Fabric",
        description="Microsoft Fabric Warehouse and Lakehouse SQL endpoints with Azure AD authentication.",
        config_schema=MSFabricConfig,
        credentials_auth=AuthOptions(
            default="service_principal",
            by_auth={
                "service_principal": AuthVariant(
                    title="Service Principal (Azure AD)",
                    schema=MSFabricCredentials,
                    scopes=["system"]
                ),
                "oauth": AuthVariant(
                    title="Sign in with Microsoft",
                    schema=OAuthDelegatedCredentials,
                    scopes=["user"]
                ),
            }
        ),
        client_path="app.data_sources.clients.ms_fabric_client.MsFabricClient",
    ),
    "sybase": DataSourceRegistryEntry(
        type="sybase",
        title="Sybase SQL Anywhere",
        description="SAP/Sybase SQL Anywhere relational database, connected via FreeTDS over TDS protocol.",
        config_schema=SybaseConfig,
        credentials_auth=AuthOptions(default="userpass", by_auth={
            "userpass": AuthVariant(title="Username / Password", schema=SQLCredentials, scopes=["system", "user"])
        }),
        client_path=None,
        requires_license="enterprise",
    ),
    "timbr": DataSourceRegistryEntry(
        type="timbr",
        title="Timbr AI",
        description="Ontology-based semantic layer. Query concepts, properties, relationships, and measures via SQL.",
        config_schema=TimbrConfig,
        credentials_auth=AuthOptions(
            default="api_key",
            by_auth={
                "api_key": AuthVariant(
                    title="API Key",
                    schema=TimbrTokenCredentials,
                    scopes=["system", "user"],
                )
            }
        ),
        client_path="app.data_sources.clients.timbr_client.TimbrClient",
        requires_license="enterprise",
    ),
    "timbr_a2a": DataSourceRegistryEntry(
        type="timbr_a2a",
        title="Timbr A2A",
        description="Agent-to-Agent semantic layer. Send natural-language prompts and get structured results.",
        config_schema=TimbrA2AConfig,
        credentials_auth=AuthOptions(
            default="api_key",
            by_auth={
                "api_key": AuthVariant(
                    title="API Key",
                    schema=TimbrA2ATokenCredentials,
                    scopes=["system", "user"],
                )
            }
        ),
        client_path="app.data_sources.clients.timbr_a2a_client.TimbrA2aClient",
        requires_license="enterprise",
        dev_only=True,
    ),
    "sisense": DataSourceRegistryEntry(
        type="sisense",
        title="Sisense",
        description="Query Sisense ElastiCubes and live models via SQL. Auto-discovers data models, tables, and dashboards.",
        config_schema=SisenseConfig,
        credentials_auth=AuthOptions(
            default="userpass",
            by_auth={
                "userpass": AuthVariant(
                    title="Username / Password",
                    schema=SisenseCredentials,
                    scopes=["system", "user"]
                )
            }
        ),
        client_path="app.data_sources.clients.sisense_client.SisenseClient",
        requires_license="enterprise",
    ),
    "oracle_bi": DataSourceRegistryEntry(
        type="oracle_bi",
        title="Oracle BI",
        description="Query Oracle BI subject areas via Logical SQL. Works with OBIEE 11g/12c, Oracle Analytics Server, and Oracle Analytics Cloud.",
        config_schema=OracleBIConfig,
        credentials_auth=AuthOptions(
            default="userpass",
            by_auth={
                "userpass": AuthVariant(
                    title="Username / Password",
                    schema=OracleBICredentials,
                    scopes=["system", "user"],
                )
            },
        ),
        client_path="app.data_sources.clients.oracle_bi_client.OracleBIClient",
        requires_license="enterprise",
    ),
    "mcp": DataSourceRegistryEntry(
        type="mcp",
        title="MCP Server",
        description="Connect to a Model Context Protocol server to access external tools for discovery, knowledge, and data ingestion.",
        config_schema=MCPConfig,
        credentials_auth=AuthOptions(
            default="none",
            by_auth={
                "none": AuthVariant(
                    title="No Auth",
                    schema=MCPNoAuthCredentials,
                    scopes=["system"],
                ),
                "bearer": AuthVariant(
                    title="Bearer Token",
                    schema=MCPBearerCredentials,
                    scopes=["system", "user"],
                ),
                "api_key": AuthVariant(
                    title="API Key",
                    schema=CustomAPIKeyCredentials,
                    scopes=["system", "user"],
                ),
                "oauth_app": AuthVariant(
                    title="OAuth Client (admin-configured)",
                    schema=MCPOAuthAppCredentials,
                    scopes=["system", "user"],
                ),
                "oauth": AuthVariant(
                    title="Sign in (per-user OAuth)",
                    schema=OAuthDelegatedCredentials,
                    scopes=["user"],
                ),
            },
        ),
        client_path="app.data_sources.clients.mcp_client.McpClient",
        version="beta",
        is_connection=False,
        data_shape="tools",
        catalog_ownership="none",
        ui_form="mcp",
    ),
    "custom_api": DataSourceRegistryEntry(
        type="custom_api",
        title="Custom API",
        description="Connect to any REST API by defining endpoint schemas. Endpoints are exposed as callable tools.",
        config_schema=CustomAPIConfig,
        credentials_auth=AuthOptions(
            default="none",
            by_auth={
                "none": AuthVariant(
                    title="No Auth",
                    schema=CustomAPINoAuthCredentials,
                    scopes=["system"],
                ),
                "bearer": AuthVariant(
                    title="Bearer Token",
                    schema=CustomAPIBearerCredentials,
                    scopes=["system", "user"],
                ),
                "api_key": AuthVariant(
                    title="API Key",
                    schema=CustomAPIKeyCredentials,
                    scopes=["system", "user"],
                ),
            },
        ),
        client_path="app.data_sources.clients.custom_api_client.CustomApiClient",
        version="beta",
        is_connection=False,
        data_shape="tools",
        catalog_ownership="none",
        ui_form="custom_api",
    ),
}


def get_entry(ds_type: str) -> DataSourceRegistryEntry:
    entry = REGISTRY.get(ds_type)
    if not entry:
        raise ValueError(f"Unknown data source type: {ds_type}")
    if entry.dev_only and not _is_dev_environment():
        raise ValueError(f"Unknown data source type: {ds_type}")
    return entry


def list_available_data_sources(include_tool_providers: bool = True) -> list[dict]:
    """List entries the frontend can offer in the add-connection grid.

    `is_connection` discriminates data-source-shaped entries (Postgres,
    Snowflake, SharePoint) from tool-provider integrations (OneDrive,
    Google Drive, MCP, Custom API). Frontends can group / route accordingly.
    """
    return [
        {
            "type": e.type,
            "title": e.title,
            "description": e.description,
            "config": e.config_schema.__name__,
            "status": e.status,
            "version": e.version,
            "requires_license": e.requires_license,
            "is_connection": e.is_connection,
            "data_shape": e.data_shape,
            "catalog_ownership": e.catalog_ownership,
            "ui_form": e.ui_form,
        }
        for e in REGISTRY.values()
        if (
            e.status == "active"
            and _entry_visible(e)
            and (e.is_connection or include_tool_providers)
        )
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


def tool_provider_types() -> set[str]:
    """Connection types that act as tool providers (is_connection=False).

    Used by the agent runtime to find connections whose tools can be called
    from the agent, by the indexing service to skip schema indexing, and by
    the create/update flows to skip data-source-flavoured validation.
    """
    return {t for t, e in REGISTRY.items() if not e.is_connection}


def resolve_client_class(ds_type: str):
    """Resolve client class via configured path; fallback to dynamic naming."""
    from importlib import import_module
    import logging

    logger = logging.getLogger(__name__)

    entry = get_entry(ds_type)
    if entry.client_path:
        try:
            module_path, _, class_name = entry.client_path.rpartition(".")
            module = import_module(module_path)
            return getattr(module, class_name)
        except Exception as exc:
            # The explicit client_path is the contract; falling back to the
            # naming-convention path silently has caused real bugs (a broken
            # import in graph_drive_client showed up as "No module named
            # onedrive_client"). Surface the actual failure rather than
            # swallowing it.
            logger.exception(
                "resolve_client_class: configured client_path %r failed to import "
                "for type=%r; falling back to dynamic resolution. Real error: %s",
                entry.client_path, ds_type, exc,
            )

    # Fallback to dynamic resolution used previously
    module_name = f"app.data_sources.clients.{ds_type.lower()}_client"
    title = "".join(word[:1].upper() + word[1:] for word in ds_type.split("_"))
    class_name = f"{title}Client"
    module = import_module(module_name)
    return getattr(module, class_name)


