"""
OAuth Delegated Credentials Service.

Handles OAuth authorization code flow for per-user data source authentication.
Maps connection types to their OAuth provider configuration and manages token lifecycle.
"""
import base64
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.models.connection import Connection
from app.models.user_connection_credentials import UserConnectionCredentials
from app.settings.logging_config import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# PKCE helpers (extracted from auth_providers.py for reuse)
# ---------------------------------------------------------------------------

def generate_pkce_pair() -> Tuple[str, str]:
    """Generate PKCE code_verifier and S256 code_challenge."""
    verifier_bytes = os.urandom(64)
    code_verifier = base64.urlsafe_b64encode(verifier_bytes).decode().rstrip("=")
    challenge = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(challenge).decode().rstrip("=")
    return code_verifier, code_challenge


# ---------------------------------------------------------------------------
# OAuth provider mapping
# ---------------------------------------------------------------------------

def get_oauth_params(connection: Connection) -> dict:
    """Return OAuth provider config for a connection type.

    Returns dict with keys:
        authorize_url, token_url, client_id, client_secret,
        scopes, provider_name
    """
    creds = connection.decrypt_credentials() or {}
    conn_type = connection.type

    if conn_type in ("powerbi", "ms_fabric"):
        tenant_id = creds.get("tenant_id")
        if not tenant_id:
            raise ValueError(f"Connection {connection.id} missing tenant_id in credentials")

        client_id = creds.get("oauth_client_id") or creds.get("client_id")
        client_secret = creds.get("oauth_client_secret") or creds.get("client_secret")

        if not client_id or not client_secret:
            raise ValueError(f"Connection {connection.id} missing client_id/client_secret for OAuth")

        scopes_map = {
            "powerbi": "https://analysis.windows.net/powerbi/api/.default offline_access",
            "ms_fabric": "https://api.fabric.microsoft.com/.default offline_access",
        }

        return {
            "authorize_url": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
            "token_url": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": scopes_map[conn_type],
            "provider_name": "microsoft",
        }

    if conn_type == "bigquery":
        client_id = creds.get("oauth_client_id")
        client_secret = creds.get("oauth_client_secret")

        if not client_id or not client_secret:
            raise ValueError(
                f"Connection {connection.id} missing oauth_client_id/oauth_client_secret for BigQuery OAuth. "
                "Configure these in the connection credentials."
            )

        return {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": "https://www.googleapis.com/auth/bigquery.readonly offline_access",
            "provider_name": "google",
        }

    raise ValueError(f"OAuth not supported for connection type: {conn_type}")


# ---------------------------------------------------------------------------
# Token exchange
# ---------------------------------------------------------------------------

async def exchange_code_for_tokens(
    oauth_params: dict,
    code: str,
    redirect_uri: str,
    code_verifier: Optional[str] = None,
) -> dict:
    """Exchange an authorization code for access/refresh tokens."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": oauth_params["client_id"],
        "client_secret": oauth_params["client_secret"],
    }
    if code_verifier:
        data["code_verifier"] = code_verifier

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            oauth_params["token_url"],
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

    if resp.status_code >= 400:
        logger.error(f"OAuth token exchange failed: {resp.status_code} {resp.text}")
        raise ValueError(f"OAuth token exchange failed: {resp.text}")

    token_data = resp.json()
    expires_in = token_data.get("expires_in", 3600)
    return {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": datetime.fromtimestamp(
            time.time() + int(expires_in), tz=timezone.utc
        ).isoformat(),
        "token_type": token_data.get("token_type", "Bearer"),
    }


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

async def refresh_access_token(
    oauth_params: dict,
    refresh_token: str,
) -> dict:
    """Use a refresh token to obtain new access/refresh tokens."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": oauth_params["client_id"],
        "client_secret": oauth_params["client_secret"],
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            oauth_params["token_url"],
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

    if resp.status_code >= 400:
        logger.error(f"OAuth token refresh failed: {resp.status_code} {resp.text}")
        raise ValueError(f"OAuth token refresh failed: {resp.text}")

    token_data = resp.json()
    expires_in = token_data.get("expires_in", 3600)
    return {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token", refresh_token),
        "expires_at": datetime.fromtimestamp(
            time.time() + int(expires_in), tz=timezone.utc
        ).isoformat(),
        "token_type": token_data.get("token_type", "Bearer"),
    }


# ---------------------------------------------------------------------------
# OBO (On-Behalf-Of) token exchange — Phase 2
# ---------------------------------------------------------------------------

# Connection types that support OBO auto-provisioning from Entra ID login
ENTRA_OBO_CONNECTION_TYPES = {"powerbi", "ms_fabric"}

# Resource scopes used when requesting OBO tokens per connection type.
# These must match the API permissions granted to the Entra app registration.
# `offline_access` requests a refresh_token so the token can be renewed without
# requiring the user to re-authenticate when the short-lived access token expires.
_OBO_SCOPES = {
    "powerbi": "https://analysis.windows.net/powerbi/api/.default offline_access",
    # Fabric Warehouse SQL endpoint authenticates with Azure SQL tokens, not Fabric API tokens.
    # Requires the app registration to have "Azure SQL Database / user_impersonation" delegated
    # permission with admin consent — the Fabric API scope returns tokens the SQL endpoint rejects.
    "ms_fabric": "https://database.windows.net/user_impersonation offline_access",
}


async def exchange_obo_token(
    login_access_token: str,
    connection: Connection,
) -> dict:
    """Exchange a user's Entra ID login token for a connection-scoped token via OBO flow.

    Uses the `urn:ietf:params:oauth:grant-type:jwt-bearer` grant type with
    `requested_token_use=on_behalf_of` as per the Microsoft identity platform.

    The connection's own OAuth client credentials (client_id / client_secret)
    are used for authentication, and the login token is the assertion.
    """
    conn_type = connection.type
    if conn_type not in ENTRA_OBO_CONNECTION_TYPES:
        raise ValueError(f"OBO not supported for connection type: {conn_type}")

    creds = connection.decrypt_credentials() or {}
    tenant_id = creds.get("tenant_id")
    if not tenant_id:
        raise ValueError(f"Connection {connection.id} missing tenant_id for OBO")

    client_id = creds.get("oauth_client_id") or creds.get("client_id")
    client_secret = creds.get("oauth_client_secret") or creds.get("client_secret")
    if not client_id or not client_secret:
        raise ValueError(f"Connection {connection.id} missing client credentials for OBO")

    scope = _OBO_SCOPES[conn_type]
    token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"

    data = {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "client_id": client_id,
        "client_secret": client_secret,
        "assertion": login_access_token,
        "scope": scope,
        "requested_token_use": "on_behalf_of",
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            token_url,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )

    if resp.status_code >= 400:
        logger.error(f"OBO token exchange failed for connection {connection.id}: {resp.status_code} {resp.text}")
        raise ValueError(f"OBO token exchange failed: {resp.text}")

    token_data = resp.json()
    expires_in = token_data.get("expires_in", 3600)
    return {
        "access_token": token_data["access_token"],
        "refresh_token": token_data.get("refresh_token"),
        "expires_at": datetime.fromtimestamp(
            time.time() + int(expires_in), tz=timezone.utc
        ).isoformat(),
        "token_type": token_data.get("token_type", "Bearer"),
    }


# ---------------------------------------------------------------------------
# Auto-provision connection credentials after Entra ID login
# ---------------------------------------------------------------------------

async def auto_provision_connection_credentials(
    db: AsyncSession,
    user,
    login_access_token: str,
) -> dict:
    """Auto-provision OAuth credentials for Entra-based connections after OIDC login.

    Queries all connections where:
      - auth_policy = "user_required"
      - "oauth" in allowed_user_auth_modes
      - type in ENTRA_OBO_CONNECTION_TYPES (powerbi, ms_fabric)

    For each, if the user doesn't already have valid credentials, performs
    an OBO token exchange and stores the result.

    Returns a summary dict: {provisioned: [...], skipped: [...], failed: [...]}.
    """
    from sqlalchemy.orm import selectinload

    # Find eligible connections
    stmt = (
        select(Connection)
        .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
        .where(
            Connection.auth_policy == "user_required",
            Connection.type.in_(list(ENTRA_OBO_CONNECTION_TYPES)),
        )
    )
    result = await db.execute(stmt)
    connections = result.scalars().all()

    summary = {"provisioned": [], "skipped": [], "failed": []}

    for connection in connections:
        # Check allowed_user_auth_modes includes oauth
        allowed_modes = connection.allowed_user_auth_modes or []
        if "oauth" not in allowed_modes:
            continue

        # Check if user already has valid credentials
        existing_stmt = select(UserConnectionCredentials).where(
            UserConnectionCredentials.connection_id == connection.id,
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.is_active == True,
            UserConnectionCredentials.auth_mode == "oauth",
        )
        existing = (await db.execute(existing_stmt)).scalars().first()
        if existing and existing.expires_at:
            exp = existing.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp > datetime.now(timezone.utc):
                summary["skipped"].append({"connection_id": connection.id, "reason": "valid_credentials_exist"})
                continue

        # Perform OBO exchange
        try:
            tokens = await exchange_obo_token(login_access_token, connection)
        except Exception as e:
            logger.warning(f"OBO auto-provision failed for connection {connection.id}: {e}")
            summary["failed"].append({"connection_id": connection.id, "error": str(e)})
            continue

        # Upsert credentials
        if existing:
            existing.encrypt_credentials(tokens)
            existing.expires_at = datetime.fromisoformat(tokens["expires_at"]) if tokens.get("expires_at") else None
            db.add(existing)
        else:
            row = UserConnectionCredentials(
                connection_id=connection.id,
                user_id=str(user.id),
                organization_id=str(connection.organization_id),
                auth_mode="oauth",
                is_active=True,
                is_primary=True,
                expires_at=datetime.fromisoformat(tokens["expires_at"]) if tokens.get("expires_at") else None,
            )
            row.encrypt_credentials(tokens)
            db.add(row)

        summary["provisioned"].append({"connection_id": connection.id, "type": connection.type})

        # Trigger overlay sync (best-effort)
        try:
            from app.services.data_source_service import DataSourceService
            ds_service = DataSourceService()
            for ds in (connection.data_sources or []):
                await ds_service.get_user_data_source_schema(db=db, data_source=ds, user=user)
        except Exception as e:
            logger.warning(f"Overlay sync after OBO provision failed for connection {connection.id}: {e}")

    if summary["provisioned"]:
        await db.commit()
        logger.info(
            f"OBO auto-provisioned {len(summary['provisioned'])} connection(s) for user {user.id}: "
            f"{[c['connection_id'] for c in summary['provisioned']]}"
        )

    return summary


async def maybe_refresh_oauth_credentials(
    db: AsyncSession,
    connection: Connection,
    cred_row: UserConnectionCredentials,
) -> dict:
    """Check if OAuth credentials need refresh and refresh if necessary.

    Returns the (possibly refreshed) decrypted credentials dict.
    """
    creds = cred_row.decrypt_credentials()

    if cred_row.auth_mode != "oauth":
        return creds

    expires_at_str = creds.get("expires_at")
    if not expires_at_str:
        return creds

    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except (ValueError, TypeError):
        return creds

    now = datetime.now(timezone.utc)
    # Ensure timezone-aware comparison
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Refresh if token expires within 5 minutes
    if expires_at > now + timedelta(minutes=5):
        return creds

    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        logger.warning(f"OAuth token expired for connection {connection.id} but no refresh_token available")
        return creds

    try:
        oauth_params = get_oauth_params(connection)
        new_tokens = await refresh_access_token(oauth_params, refresh_token)
        # Update stored credentials
        cred_row.encrypt_credentials(new_tokens)
        cred_row.expires_at = datetime.fromisoformat(new_tokens["expires_at"])
        db.add(cred_row)
        await db.commit()
        await db.refresh(cred_row)
        logger.info(f"OAuth token refreshed for connection {connection.id}")
        return new_tokens
    except Exception as e:
        logger.error(f"Failed to refresh OAuth token for connection {connection.id}: {e}")
        return creds
