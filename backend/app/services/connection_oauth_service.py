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


def parse_expires_at(value: Optional[str]) -> Optional[datetime]:
    """Parse an OAuth ``expires_at`` ISO string into a naive UTC datetime.

    Token responses encode ``expires_at`` as an RFC3339 string with a UTC
    offset (e.g. ``2026-06-02T10:09:32+00:00``), which ``datetime.fromisoformat``
    turns into a timezone-aware datetime. The ``user_connection_credentials``
    columns are ``TIMESTAMP WITHOUT TIME ZONE`` (matching ``created_at`` /
    ``updated_at``, which use naive UTC), and asyncpg rejects aware datetimes
    for those columns. Normalize to naive UTC so the value is consistent with
    the rest of the schema and storable on PostgreSQL.
    """
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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

    if conn_type in ("powerbi", "ms_fabric", "sharepoint", "onedrive", "outlook_mail"):
        tenant_id = creds.get("tenant_id")
        if not tenant_id:
            raise ValueError(f"Connection {connection.id} missing tenant_id in credentials")

        client_id = creds.get("oauth_client_id") or creds.get("client_id")
        client_secret = creds.get("oauth_client_secret") or creds.get("client_secret")

        if not client_id or not client_secret:
            raise ValueError(f"Connection {connection.id} missing client_id/client_secret for OAuth")

        scopes_map = {
            "powerbi": "https://analysis.windows.net/powerbi/api/.default offline_access",
            # Fabric Warehouse/Lakehouse SQL endpoints authenticate with Azure SQL
            # tokens (aud=database.windows.net), NOT Fabric API tokens — the latter
            # are rejected by the SQL endpoint with login error 18456. Requires the
            # app registration to have the "Azure SQL Database / user_impersonation"
            # delegated permission with admin consent. (Matches _OBO_SCOPES.)
            "ms_fabric": "https://database.windows.net/user_impersonation offline_access",
            # Graph delegated scopes for file access. `Sites.Read.All` covers
            # SharePoint sites; `Files.Read.All` covers personal OneDrive and
            # files shared with the user. `openid profile offline_access` give
            # us the user identity + refresh token.
            "sharepoint": "openid profile offline_access Files.Read.All Sites.Read.All User.Read",
            "onedrive": "openid profile offline_access Files.Read.All User.Read",
            # Outlook mail is surfaced through the same Graph file-tool surface;
            # `Mail.Read` covers reading + $search over the signed-in user's
            # messages. Without this entry the authorize route raised "OAuth not
            # supported for connection type: outlook_mail", so its only usable
            # auth mode (Sign in with Microsoft) was unreachable.
            "outlook_mail": "openid profile offline_access Mail.Read User.Read",
        }

        return {
            "authorize_url": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/authorize",
            "token_url": f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": scopes_map[conn_type],
            "provider_name": "microsoft",
        }

    if conn_type == "google_drive":
        client_id = creds.get("oauth_client_id")
        client_secret = creds.get("oauth_client_secret")

        if not client_id or not client_secret:
            raise ValueError(
                f"Connection {connection.id} missing oauth_client_id/oauth_client_secret for Google Drive. "
                "Configure these in the connection credentials."
            )

        # Drive + Sheets read-only. `drive.readonly` is a restricted scope —
        # production usage requires Google's CASA security review. `drive.file`
        # is a narrower alternative if that's a concern.
        scopes = (
            "openid email profile "
            "https://www.googleapis.com/auth/drive.readonly "
            "https://www.googleapis.com/auth/spreadsheets.readonly"
        )

        return {
            "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "client_id": client_id,
            "client_secret": client_secret,
            "scopes": scopes,
            "provider_name": "google",
        }

    if conn_type in ("mcp", "custom_api"):
        # Pre-configured OAuth client for an MCP server OR a Custom API (e.g. the
        # "X Write" preset: POST /2/tweets with the user's X token). The admin
        # registered an OAuth client at the identity provider (which may or may
        # not be the server itself); the per-user dance is standard
        # authorization-code + PKCE. RFC 8707 resource indicator is optional but
        # recommended so the issued token is audience-bound to the server URL.
        # Endpoints + client may be admin-supplied OR obtained via Dynamic Client
        # Registration (mcp_dcr_service.ensure_mcp_oauth_config, run before this in
        # the authorize route). client_secret is OPTIONAL — DCR public clients
        # (token_endpoint_auth_method="none", e.g. Notion) use PKCE only.
        authorize_url = creds.get("authorize_url")
        token_url = creds.get("token_url")
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        if not (authorize_url and token_url and client_id):
            raise ValueError(
                f"MCP connection {connection.id} OAuth is missing authorize_url / token_url / "
                "client_id (run discovery + DCR, or supply them in credentials)."
            )
        # How the token endpoint authenticates the client. Stored on the
        # connection when the admin OAuth app is configured; DCR public clients
        # have no secret. Fall back to Basic auth for X (api.x.com) so existing
        # X connections created before this field existed still work without
        # being recreated — X rejects client_secret-in-body with 401.
        token_endpoint_auth_method = creds.get("token_endpoint_auth_method")
        if not token_endpoint_auth_method:
            if "api.x.com" in (token_url or ""):
                token_endpoint_auth_method = "client_secret_basic"
            elif client_secret:
                token_endpoint_auth_method = "client_secret_post"
            else:
                token_endpoint_auth_method = "none"
        return {
            "authorize_url": authorize_url,
            "token_url": token_url,
            "client_id": client_id,
            "client_secret": client_secret,  # may be None (public client)
            "scopes": creds.get("scopes") or "",
            "audience": creds.get("audience"),
            "token_endpoint_auth_method": token_endpoint_auth_method,
            "provider_name": "mcp",
        }

    if conn_type == "servicenow":
        # ServiceNow OAuth endpoints are instance-specific (unlike Google /
        # Microsoft): {instance_url}/oauth_auth.do and /oauth_token.do. The
        # instance URL lives in the connection *config*, not credentials.
        import json as _json
        config = connection.config
        if isinstance(config, str):
            try:
                config = _json.loads(config)
            except (TypeError, ValueError):
                config = {}
        instance_url = ((config or {}).get("instance_url") or "").rstrip("/")
        if not instance_url:
            raise ValueError(f"Connection {connection.id} missing instance_url in config for ServiceNow OAuth")

        client_id = creds.get("oauth_client_id")
        # Secret is optional: a ServiceNow OAuth app marked "public client"
        # authenticates with PKCE only (no secret at the token endpoint).
        client_secret = creds.get("oauth_client_secret")
        if not client_id:
            raise ValueError(
                f"Connection {connection.id} missing oauth_client_id for ServiceNow OAuth. "
                "Register an OAuth app in the instance (System OAuth → Application Registry) and save its "
                "client ID (and secret, unless it's a public client) on the connection."
            )

        return {
            "authorize_url": f"{instance_url}/oauth_auth.do",
            "token_url": f"{instance_url}/oauth_token.do",
            "client_id": client_id,
            "client_secret": client_secret,  # None → public client (PKCE only)
            # `useraccount` is ServiceNow's standard delegated scope: the token
            # acts as the signing-in user. Refresh tokens are always issued
            # (default ~100-day lifetime); no offline_access-style scope exists.
            "scopes": "useraccount",
            "provider_name": "servicenow",
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
# Client authentication at the token endpoint
# ---------------------------------------------------------------------------

def _apply_client_auth(oauth_params: dict, data: dict) -> Optional[httpx.BasicAuth]:
    """Attach client credentials to a token request per its auth method.

    Returns an ``httpx.BasicAuth`` to pass as ``auth=`` (for
    ``client_secret_basic``) or ``None``. Mutates ``data`` in place:
      - client_secret_basic → move the secret to the Authorization header,
        keep client_id OUT of the body (X rejects a body-carried secret with
        401 unauthorized_client).
      - client_secret_post  → put client_secret in the body (default).
      - none                → public client, no secret.

    The method is derived from ``token_endpoint_auth_method`` when present,
    otherwise inferred from whether a secret exists (backward-compatible with
    connections created before the field existed).
    """
    client_secret = oauth_params.get("client_secret")
    method = oauth_params.get("token_endpoint_auth_method")
    if not method:
        method = "client_secret_post" if client_secret else "none"

    if method == "client_secret_basic":
        if not client_secret:
            raise ValueError("client_secret_basic requires a client_secret")
        # Basic auth carries client_id:client_secret in the header; the body
        # must not repeat the secret. Drop client_id from the body too — X
        # authenticates it solely via the header.
        data.pop("client_id", None)
        data.pop("client_secret", None)
        return httpx.BasicAuth(oauth_params["client_id"], client_secret)

    if method == "client_secret_post":
        if client_secret:
            data["client_secret"] = client_secret
        return None

    if method == "none":
        # Public client — nothing to attach.
        return None

    raise ValueError(f"Unsupported token_endpoint_auth_method: {method}")


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
    }
    auth = _apply_client_auth(oauth_params, data)
    if code_verifier:
        data["code_verifier"] = code_verifier
    # RFC 8707 resource indicator — audience-binds the token. Used by MCP
    # (and any provider that supports it). Ignored by providers that don't.
    if oauth_params.get("audience"):
        data["resource"] = oauth_params["audience"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            oauth_params["token_url"],
            data=data,
            auth=auth,
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
    }
    auth = _apply_client_auth(oauth_params, data)
    if oauth_params.get("audience"):
        data["resource"] = oauth_params["audience"]

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            oauth_params["token_url"],
            data=data,
            auth=auth,
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
ENTRA_OBO_CONNECTION_TYPES = {"powerbi", "ms_fabric", "sharepoint", "onedrive", "outlook_mail"}

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
    # Microsoft Graph delegated scopes for file access.
    "sharepoint": "https://graph.microsoft.com/.default offline_access",
    "onedrive": "https://graph.microsoft.com/.default offline_access",
    # Outlook mail reads over Graph use the same Graph resource; `.default`
    # yields whatever Graph delegated permissions (e.g. Mail.Read) the app
    # registration was granted.
    "outlook_mail": "https://graph.microsoft.com/.default offline_access",
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
      - type in ENTRA_OBO_CONNECTION_TYPES (powerbi, ms_fabric, sharepoint, onedrive)

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

        # Check if user already has a credential/preference row (any auth_mode, so a
        # service-account marker row gets promoted rather than duplicated).
        existing_stmt = select(UserConnectionCredentials).where(
            UserConnectionCredentials.connection_id == connection.id,
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.is_active == True,
        ).order_by(
            UserConnectionCredentials.is_primary.desc(),
            UserConnectionCredentials.updated_at.desc(),
        )
        existing = (await db.execute(existing_stmt)).scalars().first()
        if existing and existing.auth_mode == "oauth" and existing.expires_at:
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
            # Promote a preference-only marker row (auth_mode="service_account") to a
            # real OAuth credential now that we have a delegated token.
            existing.auth_mode = "oauth"
            existing.encrypt_credentials(tokens)
            existing.expires_at = parse_expires_at(tokens.get("expires_at"))
            db.add(existing)
        else:
            row = UserConnectionCredentials(
                connection_id=connection.id,
                user_id=str(user.id),
                organization_id=str(connection.organization_id),
                auth_mode="oauth",
                is_active=True,
                is_primary=True,
                expires_at=parse_expires_at(tokens.get("expires_at")),
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
        cred_row.expires_at = parse_expires_at(new_tokens.get("expires_at"))
        db.add(cred_row)
        await db.commit()
        await db.refresh(cred_row)
        logger.info(f"OAuth token refreshed for connection {connection.id}")
        return new_tokens
    except Exception as e:
        logger.error(f"Failed to refresh OAuth token for connection {connection.id}: {e}")
        return creds
