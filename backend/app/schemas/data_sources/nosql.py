"""Config + credentials schemas for the DynamoDB, Cosmos DB, Couchbase and
Cassandra connectors. These Pydantic models ARE the connect form.

DynamoDB reuses the S3 connector's AWS credential variants (static keys,
assume-role, default chain) from configs.py.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


def _sample_size_field(noun: str):
    return Field(
        100,
        ge=1,
        le=1000,
        title="Schema Sample Size",
        description=f"How many {noun} to sample per table when inferring its attributes for the catalog.",
        json_schema_extra={"ui:type": "number"},
    )


def _max_rows_field(noun: str, default: int = 5000):
    return Field(
        default,
        ge=1,
        title="Max Rows per Query",
        description=f"Cap on {noun} returned by a single query, so an unfiltered read can't pull a whole table.",
        json_schema_extra={"ui:type": "number"},
    )


# ------------------------------------------------------------------ DynamoDB

class DynamoDBConfig(BaseModel):
    region: str | None = Field(
        None,
        title="Region",
        description="AWS region of the tables (e.g. 'us-east-1').",
        json_schema_extra={"ui:type": "string"},
    )
    tables: str | None = Field(
        None,
        title="Tables",
        description="Comma-separated table names to expose. Leave blank for every table the credentials can list.",
        json_schema_extra={"ui:type": "string"},
    )
    endpoint_url: str | None = Field(
        None,
        title="Endpoint URL",
        description="Custom endpoint (VPC endpoint or DynamoDB Local). Leave blank for AWS.",
        json_schema_extra={"ui:type": "string"},
    )
    sample_size: int = _sample_size_field("items")
    max_items: int = _max_rows_field("items")


# ------------------------------------------------------------------ Cosmos DB

class CosmosDBKeyCredentials(BaseModel):
    account_key: str = Field(
        ...,
        title="Account Key",
        description="A read-only key is recommended (Keys → Read-only Keys → Primary read-only key).",
        json_schema_extra={"ui:type": "password"},
    )


class CosmosDBServicePrincipalCredentials(BaseModel):
    tenant_id: str = Field(..., title="Tenant ID", description="Microsoft Entra tenant (directory) ID.", json_schema_extra={"ui:type": "string"})
    client_id: str = Field(..., title="Client ID", description="App registration with the 'Cosmos DB Built-in Data Reader' role.", json_schema_extra={"ui:type": "string"})
    client_secret: str = Field(..., title="Client Secret", description="App registration client secret.", json_schema_extra={"ui:type": "password"})


class CosmosDBDefaultCredentials(BaseModel):
    """No stored secret — the deployment's managed identity / workload identity
    (DefaultAzureCredential), which needs a Cosmos DB data-plane reader role."""

    class Config:
        extra = "allow"


class CosmosDBConfig(BaseModel):
    endpoint: str = Field(
        ...,
        title="Account Endpoint",
        description="Account URI (e.g. https://acme.documents.azure.com:443/).",
        json_schema_extra={"ui:type": "string"},
    )
    database: str = Field(
        ...,
        title="Database",
        description="Database name.",
        json_schema_extra={"ui:type": "string"},
    )
    containers: str | None = Field(
        None,
        title="Containers",
        description="Comma-separated containers to expose. Leave blank for every container in the database.",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify TLS Certificate",
        description="Turn off only for the local emulator's self-signed certificate.",
        json_schema_extra={"ui:type": "boolean"},
    )
    sample_size: int = _sample_size_field("documents")
    max_items: int = _max_rows_field("documents")


# ------------------------------------------------------------------ Couchbase

class CouchbaseCredentials(BaseModel):
    username: str = Field(..., title="Username", description="Database user (cluster RBAC) with Query Select + Data Reader on the bucket.", json_schema_extra={"ui:type": "string"})
    password: str = Field(..., title="Password", description="", json_schema_extra={"ui:type": "password"})


class CouchbaseConfig(BaseModel):
    connection_string: str = Field(
        ...,
        title="Connection String",
        description=(
            "couchbase://host1,host2 for a self-managed cluster, or the "
            "couchbases://cb.<id>.cloud.couchbase.com string of a Capella cluster."
        ),
        json_schema_extra={"ui:type": "string"},
    )
    bucket: str = Field(..., title="Bucket", description="Bucket to expose.", json_schema_extra={"ui:type": "string"})
    scope: str | None = Field(
        None,
        title="Scope",
        description="Limit to one scope (e.g. 'inventory'). Leave blank for every scope in the bucket.",
        json_schema_extra={"ui:type": "string"},
    )
    verify_ssl: bool = Field(
        True,
        title="Verify TLS Certificate",
        description="For couchbases:// connections. Turn off only for self-signed test clusters.",
        json_schema_extra={"ui:type": "boolean"},
    )
    sample_size: int = _sample_size_field("documents")
    max_rows: int = _max_rows_field("rows")


# ------------------------------------------------------------------ Cassandra

class CassandraCredentials(BaseModel):
    username: str | None = Field(None, title="Username", description="Leave blank when authentication is disabled.", json_schema_extra={"ui:type": "string"})
    password: str | None = Field(None, title="Password", description="", json_schema_extra={"ui:type": "password"})


class CassandraConfig(BaseModel):
    contact_points: str = Field(
        ...,
        title="Contact Points",
        description="Comma-separated node addresses (e.g. 10.0.0.5,10.0.0.6). Works with Apache Cassandra, ScyllaDB and Amazon Keyspaces.",
        json_schema_extra={"ui:type": "string"},
    )
    port: int = Field(9042, ge=1, le=65535, title="Port", description="Native protocol port (9042; 9142 for Amazon Keyspaces).", json_schema_extra={"ui:type": "number"})
    keyspaces: str | None = Field(
        None,
        title="Keyspaces",
        description="Comma-separated keyspaces to expose. Leave blank for every non-system keyspace.",
        json_schema_extra={"ui:type": "string"},
    )
    local_datacenter: str | None = Field(
        None,
        title="Local Datacenter",
        description="Datacenter to route queries to (e.g. 'datacenter1'). Leave blank to use the contact point's.",
        json_schema_extra={"ui:type": "string"},
    )
    ssl: bool = Field(False, title="Use TLS", description="Required for Amazon Keyspaces and most managed clusters.", json_schema_extra={"ui:type": "boolean"})
    verify_ssl: bool = Field(True, title="Verify TLS Certificate", description="", json_schema_extra={"ui:type": "boolean"})
    max_rows: int = _max_rows_field("rows")
