# Cloud Managed Identity Support

## Overview

Bagofwords will support Managed Identity (MI) authentication for the three major public clouds: Azure, AWS, and GCP. The design must be clean, decoupled, and easily extensible to additional cloud providers in the future.

## Architecture

A `CloudCredentialProvider` interface — one implementation per cloud, resolved by a factory. The rest of the codebase never imports Azure, AWS, or GCP SDKs directly.

```
app/core/cloud_identity/
    __init__.py
    base.py          # CloudCredentialProvider abstract interface
    azure.py         # AzureCredentialProvider
    aws.py           # AWSCredentialProvider
    gcp.py           # GCPCredentialProvider
    factory.py       # Maps cloud name → provider class; self-registers on import
```

The `Connection` model gets two new fields:

- `auth_method` — enum: `credentials | azure_mi | aws_iam | gcp_wi`
- `cloud_identity_config` — JSON: provider-specific settings (scopes, role ARNs, etc.)

The connector code stays the same for each service — only the token acquisition path differs, and that is fully encapsulated inside each provider.

---

## Connectors Affected

### Azure Managed Identity — 8 connectors

| Connector | Service |
|---|---|
| `mssql_client.py` | Azure SQL |
| `azure_data_explorer_client.py` | Azure Data Explorer |
| `analysis_services_client.py` | Azure Analysis Services |
| `ms_fabric_client.py` | Microsoft Fabric |
| `powerbi_client.py` | Power BI |
| `graph_drive_client.py` | SharePoint / OneDrive |
| `postgresql_client.py` | Azure Database for PostgreSQL |
| `mysql_client.py` | Azure Database for MySQL |

### AWS IAM Role — 6 connectors

| Connector | Service |
|---|---|
| `aws_athena_client.py` | Athena |
| `aws_redshift_client.py` | Redshift |
| `aws_cost_client.py` | Cost Explorer |
| `postgresql_client.py` | RDS PostgreSQL / Aurora |
| `mysql_client.py` | RDS MySQL / Aurora |
| `mariadb_client.py` | RDS MariaDB |

### GCP Workload Identity — 6 connectors

| Connector | Service |
|---|---|
| `bigquery_client.py` | BigQuery |
| `gcp_client.py` | GCP multi-service |
| `google_analytics_client.py` | Google Analytics |
| `google_drive_client.py` | Google Drive |
| `postgresql_client.py` | Cloud SQL PostgreSQL |
| `mysql_client.py` | Cloud SQL MySQL |

**Total: 20 connector × cloud combinations across 3 clouds.**

Note: `postgresql_client.py` and `mysql_client.py` appear in all three clouds. The connector code is unchanged — `auth_method` on the `Connection` determines which provider is resolved.

---

## Implementation Details per Cloud Provider

### Azure Managed Identity

**How it works:**

Azure AD issues short-lived OAuth2 bearer tokens scoped to a specific Azure resource. These tokens are used differently depending on the target service:

- **Native Azure SDKs** (Power BI, Graph, ADX, Fabric) — the `DefaultAzureCredential` object is passed directly to the SDK client. The SDK handles token acquisition, caching, and refresh internally.
- **Legacy protocols** (Azure SQL, Azure PostgreSQL, Azure MySQL via ODBC/pyodbc) — the raw token must be extracted, encoded as UTF-16-LE, packed into a struct, and injected via the `SQL_COPT_SS_ACCESS_TOKEN` connection attribute. This token expires (~1 hour) and must be refreshed per new physical connection.

**Credential resolution chain (`DefaultAzureCredential`):**

When running in AKS with Workload Identity enabled, the chain resolves in order:
1. Environment variables (`AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`)
2. **Workload Identity** — projected ServiceAccount OIDC token exchanged for Azure AD token ← hits this in AKS
3. Managed Identity endpoint
4. Azure CLI (`az login`) ← hits this in local dev

Same code works in AKS and locally with no changes.

**Resource scopes by service:**

| Service | Token scope |
|---|---|
| Azure SQL / PostgreSQL / MySQL | `https://database.windows.net/.default` |
| Azure Storage / Blob | `https://storage.azure.com/.default` |
| Azure Data Explorer | `https://kusto.kusto.windows.net/.default` |
| Microsoft Graph (SharePoint, OneDrive) | `https://graph.microsoft.com/.default` |
| Power BI / Fabric | `https://analysis.windows.net/powerbi/api/.default` |

**AKS setup requirements:**
- Cluster must have `--enable-oidc-issuer` and `--enable-workload-identity`
- A User-assigned Managed Identity created and federated against the pod's Kubernetes ServiceAccount
- Pod's ServiceAccount annotated with the MI client ID

---

### AWS IAM Role (IRSA)

**How it works:**

AWS uses IAM Roles for Service Accounts (IRSA). The pod's ServiceAccount is annotated with an IAM Role ARN. AWS injects a projected token into the pod; the AWS SDK exchanges it for temporary STS credentials (access key + secret + session token) valid for ~1 hour.

For **native AWS services** (Athena, Cost Explorer), boto3 resolves credentials automatically from the environment — no explicit token handling needed.

For **RDS databases** (PostgreSQL, MySQL, MariaDB on RDS/Aurora), AWS does not use OAuth tokens. Instead, a short-lived signed authentication token is generated via:

```
rds_client.generate_db_auth_token(
    DBHostname=host,
    Port=port,
    DBUsername=username,
    Region=region
)
```

This token is then passed as the database password. IAM authentication must be enabled on the RDS instance, and the IAM role must have the `rds-db:connect` permission for the specific database user.

**Credential resolution chain (boto3 default):**

1. Environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
2. **IRSA** — `AWS_WEB_IDENTITY_TOKEN_FILE` + `AWS_ROLE_ARN` env vars injected by EKS ← hits this in EKS
3. EC2 Instance Metadata Service (IMDS)
4. AWS CLI profile (`~/.aws/credentials`) ← hits this in local dev

**EKS setup requirements:**
- Cluster must have an OIDC provider associated
- IAM Role created with a trust policy referencing the cluster OIDC issuer and the pod's ServiceAccount
- Pod's ServiceAccount annotated with `eks.amazonaws.com/role-arn`

---

### GCP Workload Identity

**How it works:**

GCP uses Workload Identity Federation. A Kubernetes ServiceAccount is bound to a GCP Service Account. When the pod runs, GCP injects a projected token; the GCP SDK exchanges it for a GCP access token automatically via Application Default Credentials (ADC).

For **native GCP services** (BigQuery, Google Analytics, Google Drive, GCP multi-service), the `google.auth.default()` or `google.oauth2.credentials` flow resolves credentials transparently — no explicit token handling needed in connectors.

For **Cloud SQL** (PostgreSQL, MySQL), there are two approaches:
- **Cloud SQL Auth Proxy** (recommended) — a sidecar or locally running proxy handles IAM auth and presents a standard TCP endpoint; the connector connects normally with no token handling.
- **IAM database authentication** — a short-lived IAM token is used as the database password, similar to the AWS RDS pattern.

**Credential resolution chain (ADC):**

1. `GOOGLE_APPLICATION_CREDENTIALS` env var pointing to a service account key file
2. **Workload Identity** — metadata server on the GKE node ← hits this in GKE
3. `gcloud auth application-default login` ← hits this in local dev

**GKE setup requirements:**
- Cluster must have Workload Identity enabled (`--workload-pool=PROJECT.svc.id.goog`)
- A GCP Service Account created and granted the `roles/iam.workloadIdentityUser` role for the Kubernetes ServiceAccount
- Kubernetes ServiceAccount annotated with `iam.gke.io/gcp-service-account`

---

## Token Injection Summary

All three clouds ultimately inject a short-lived string as the database password for generic databases. The abstraction hides the differences:

| Cloud | Generic DB token mechanism |
|---|---|
| Azure | Azure AD OAuth2 token used directly as password |
| AWS | `rds.generate_db_auth_token()` signed URL used as password |
| GCP | IAM access token used as password (or delegated to Cloud SQL Auth Proxy) |

For native cloud SDKs (non-database services), each cloud SDK accepts the credential object directly and handles everything internally — no manual token extraction needed.

---

## Testability

- Define a `FakeCredentialProvider` that returns static tokens — no cloud SDK setup required in unit tests
- Each real provider (`AzureCredentialProvider`, `AWSCredentialProvider`, `GCPCredentialProvider`) is unit-tested independently with mocked SDK calls
- The factory is tested with registered fakes
- Connectors are tested against the fake provider interface

## Extensibility

Adding a fourth cloud (e.g., Oracle Cloud, Alibaba Cloud):
1. Create a new file `app/core/cloud_identity/oci.py` implementing the interface
2. Call `CredentialProviderFactory.register("oci", OCICredentialProvider)` 
3. Add `gcp_wi` → `oci_mi` to the `auth_method` enum

Zero changes to existing provider code or connectors.
