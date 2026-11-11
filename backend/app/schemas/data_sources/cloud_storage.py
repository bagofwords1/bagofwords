"""Config + credentials schemas for the Google Cloud Storage and Azure Blob
file sources. These Pydantic models ARE the connect form (rendered by
ConnectForm.vue via GET /available_data_sources).

The scope/indexing fields are shared with the S3 connector so every object
store behaves and reads the same in the form; they are built by small factory
functions because Pydantic orders inherited fields first and the bucket /
container must stay the first field on the form.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

# --------------------------------------------------------------- shared fields

def _prefix_field(noun: str):
    return Field(
        None,
        title="Prefix",
        description=(
            f"Folder-like prefix to scope the connection (e.g. 'reports/2025/'). All "
            f"listing and reads are confined to this prefix. Leave blank for the "
            f"whole {noun}."
        ),
        json_schema_extra={"ui:type": "string"},
    )


def _include_globs_field():
    return Field(
        None,
        title="Include Patterns (globs)",
        description=(
            "Glob patterns relative to the prefix — the connection's scope. When "
            "set, ONLY matching objects are visible AND readable; access to "
            "anything else in the prefix is denied. Filter by folder AND type "
            "here: 'docs/**/*.pdf', '**/*.csv', 'reports/**'. Comma-separated; "
            "'**' crosses sub-folders, '*' is one segment. Blank = whole prefix."
        ),
        json_schema_extra={"ui:type": "string"},
    )


def _recursive_field():
    return Field(
        True,
        title="Include Sub-folders",
        description="Recursively enumerate objects under the prefix when listing / indexing.",
        json_schema_extra={"ui:type": "boolean"},
    )


def _max_file_mb_field():
    return Field(
        100,
        title="Max File Size (MB)",
        description="Reject whole-object (structured) reads above this size. Windowed byte-range reads are exempt.",
        json_schema_extra={"ui:type": "number"},
    )


def _index_mode_field(noun: str):
    return Field(
        "content",
        title="Indexing",
        description=(
            f"How much to cache from the {noun}. 'none' → nothing cached; list/"
            "read go live (best for huge or volatile stores). 'metadata' → "
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


def _max_catalog_objects_field(noun: str):
    return Field(
        5000,
        title="Max Catalog Objects",
        description=f"Cap on objects indexed into the catalog, so a huge {noun} doesn't produce an unbounded catalog.",
        json_schema_extra={"ui:type": "number"},
    )


# ------------------------------------------------------- Google Cloud Storage

class GCSServiceAccountCredentials(BaseModel):
    service_account_json: str = Field(
        ...,
        title="Service Account JSON",
        description=(
            "Key file of a service account with read access to the bucket "
            "(e.g. the 'Storage Object Viewer' role)."
        ),
        json_schema_extra={"ui:type": "textarea"},
    )


class GCSDefaultCredentials(BaseModel):
    """No stored secret — Application Default Credentials of the deployment
    (GKE workload identity, a VM's service account, or
    GOOGLE_APPLICATION_CREDENTIALS)."""

    class Config:
        extra = "allow"


class GCSConfig(BaseModel):
    bucket: str = Field(
        ...,
        title="Bucket",
        description="Cloud Storage bucket name (e.g. 'acme-reports'), without the gs:// prefix.",
        json_schema_extra={"ui:type": "string"},
    )
    prefix: str | None = _prefix_field("bucket")
    project: str | None = Field(
        None,
        title="Project ID",
        description="Google Cloud project that owns the bucket. Optional — read from the service account when blank.",
        json_schema_extra={"ui:type": "string"},
    )
    endpoint_url: str | None = Field(
        None,
        title="Endpoint URL",
        description="Custom Cloud Storage API endpoint (private endpoint or emulator). Leave blank for Google Cloud.",
        json_schema_extra={"ui:type": "string"},
    )
    include_globs: str | None = _include_globs_field()
    recursive: bool = _recursive_field()
    max_file_mb: int = _max_file_mb_field()
    index_mode: str = _index_mode_field("bucket")
    max_catalog_objects: int = _max_catalog_objects_field("bucket")


# ---------------------------------------------------------------- Azure Blob

class AzureBlobConnectionStringCredentials(BaseModel):
    connection_string: str = Field(
        ...,
        title="Connection String",
        description="Storage account connection string (Access keys → Connection string, or a SAS connection string).",
        json_schema_extra={"ui:type": "password"},
    )


class AzureBlobAccountKeyCredentials(BaseModel):
    account_key: str = Field(
        ...,
        title="Account Key",
        description="Storage account access key (Access keys → key1 / key2).",
        json_schema_extra={"ui:type": "password"},
    )


class AzureBlobSasCredentials(BaseModel):
    sas_token: str = Field(
        ...,
        title="SAS Token",
        description="Shared access signature with Read + List permissions on the container (the part after '?').",
        json_schema_extra={"ui:type": "password"},
    )


class AzureBlobServicePrincipalCredentials(BaseModel):
    tenant_id: str = Field(..., title="Tenant ID", description="Microsoft Entra tenant (directory) ID.", json_schema_extra={"ui:type": "string"})
    client_id: str = Field(..., title="Client ID", description="App registration (client) ID with the 'Storage Blob Data Reader' role.", json_schema_extra={"ui:type": "string"})
    client_secret: str = Field(..., title="Client Secret", description="App registration client secret.", json_schema_extra={"ui:type": "password"})


class AzureBlobDefaultCredentials(BaseModel):
    """No stored secret — the deployment's managed identity / workload identity
    / environment, resolved by DefaultAzureCredential."""

    class Config:
        extra = "allow"


class AzureBlobConfig(BaseModel):
    account_name: str | None = Field(
        None,
        title="Storage Account",
        description="Storage account name (e.g. 'acmedata'). Not needed with a connection string.",
        json_schema_extra={"ui:type": "string"},
    )
    container: str = Field(
        ...,
        title="Container",
        description="Blob container name (e.g. 'reports'). Data Lake Storage Gen2 file systems work too.",
        json_schema_extra={"ui:type": "string"},
    )
    prefix: str | None = _prefix_field("container")
    account_url: str | None = Field(
        None,
        title="Account URL",
        description=(
            "Override the blob endpoint (e.g. a sovereign cloud or private "
            "endpoint). Leave blank for https://<account>.blob.core.windows.net."
        ),
        json_schema_extra={"ui:type": "string"},
    )
    include_globs: str | None = _include_globs_field()
    recursive: bool = _recursive_field()
    max_file_mb: int = _max_file_mb_field()
    index_mode: str = _index_mode_field("container")
    max_catalog_objects: int = _max_catalog_objects_field("container")
