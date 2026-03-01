"""
Pluggable database authentication providers.

Supports dynamic credential generation for managed databases (AWS Aurora,
Azure Database for PostgreSQL, GCP Cloud SQL) instead of static passwords.

Usage:
    provider = get_auth_provider(database_config)
    password = provider.get_password(host, port, username)
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class DatabaseAuthProvider(Protocol):
    """Protocol for database authentication providers.

    Each cloud provider implements this to generate short-lived tokens
    that replace static database passwords.
    """

    def get_password(self, host: str, port: int, username: str) -> str:
        """Return a password or token for the given database endpoint."""
        ...


class StaticPasswordProvider:
    """Default provider — returns the password embedded in the connection URL.

    Used when auth.provider is 'password' or unset (backward compatible).
    """

    def __init__(self, password: str = ""):
        self._password = password

    def get_password(self, host: str, port: int, username: str) -> str:
        return self._password


class AwsIamAuthProvider:
    """AWS RDS/Aurora IAM authentication.

    Generates a short-lived token via boto3 generate_db_auth_token().
    The token is valid for 15 minutes but is only used at connection time —
    established connections are not affected.

    Requires:
      - boto3 (already a project dependency)
      - An IAM role with rds-db:connect permission
      - The DB user must have: GRANT rds_iam TO <username>
      - In K8s: IRSA annotation on the service account
    """

    def __init__(self, region: str):
        self._region = region
        self._client = None

    def _get_client(self):
        if self._client is None:
            import boto3
            self._client = boto3.client("rds", region_name=self._region)
        return self._client

    def get_password(self, host: str, port: int, username: str) -> str:
        client = self._get_client()
        token = client.generate_db_auth_token(
            DBHostname=host,
            Port=port,
            DBUsername=username,
            Region=self._region,
        )
        logger.debug("Generated IAM auth token for %s@%s:%d", username, host, port)
        return token


# ---------------------------------------------------------------------------
# Future providers — implement when needed, the pattern is ready.
# ---------------------------------------------------------------------------
#
# class AzureEntraAuthProvider:
#     """Azure AD / Entra ID authentication for Azure Database for PostgreSQL.
#
#     Uses DefaultAzureCredential to obtain a token scoped to:
#       https://ossrdbms-aad.database.windows.net/.default
#
#     Requires:
#       - azure-identity package
#       - Workload Identity annotation on the K8s service account
#     """
#
#     def get_password(self, host: str, port: int, username: str) -> str:
#         from azure.identity import DefaultAzureCredential
#         credential = DefaultAzureCredential()
#         token = credential.get_token(
#             "https://ossrdbms-aad.database.windows.net/.default"
#         )
#         return token.token
#
#
# class GcpCloudSqlAuthProvider:
#     """GCP Cloud SQL IAM authentication.
#
#     Uses google.auth.default() + IAM generateAccessToken.
#
#     Requires:
#       - google-auth package
#       - Workload Identity binding on the K8s service account
#     """
#
#     def get_password(self, host: str, port: int, username: str) -> str:
#         import google.auth
#         import google.auth.transport.requests
#         credentials, _ = google.auth.default(
#             scopes=["https://www.googleapis.com/auth/cloud-platform"]
#         )
#         credentials.refresh(google.auth.transport.requests.Request())
#         return credentials.token


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "password": lambda cfg: StaticPasswordProvider(cfg.get("password", "")),
    "aws_iam": lambda cfg: AwsIamAuthProvider(region=cfg.get("region", "us-east-1")),
    # "azure_entra": lambda cfg: AzureEntraAuthProvider(),
    # "gcp_iam": lambda cfg: GcpCloudSqlAuthProvider(),
}


def get_auth_provider(database_config) -> DatabaseAuthProvider:
    """Build the appropriate auth provider from the Database config model.

    Args:
        database_config: A Database pydantic model instance.

    Returns:
        A DatabaseAuthProvider that can generate passwords/tokens.
    """
    auth = getattr(database_config, "auth", None)
    if auth is None:
        return StaticPasswordProvider()

    provider_name = auth.provider
    if provider_name not in _PROVIDERS:
        supported = ", ".join(sorted(_PROVIDERS.keys()))
        raise ValueError(
            f"Unknown database auth provider '{provider_name}'. "
            f"Supported: {supported}"
        )

    provider_cfg = {
        "region": getattr(auth, "region", ""),
        "password": getattr(auth, "password", ""),
    }
    provider = _PROVIDERS[provider_name](provider_cfg)
    logger.info("Database auth provider: %s", provider_name)
    return provider
