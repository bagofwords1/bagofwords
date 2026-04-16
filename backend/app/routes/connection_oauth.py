"""
Connection OAuth Routes.

Handles the OAuth authorization code flow for per-user data source authentication.
Two endpoints:
  GET /connections/{connection_id}/oauth/authorize  — start the flow
  GET /connections/oauth/callback                   — handle the redirect
"""
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import jwt as pyjwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import get_async_db
from app.models.user import User
from app.models.connection import Connection
from app.models.user_connection_credentials import UserConnectionCredentials
from app.core.auth import current_user, SECRET
from app.settings.config import settings
from app.settings.logging_config import get_logger
from app.services.connection_oauth_service import (
    generate_pkce_pair,
    get_oauth_params,
    exchange_code_for_tokens,
)

logger = get_logger(__name__)

router = APIRouter(prefix="/connections", tags=["connection-oauth"])


# ---------------------------------------------------------------------------
# Signed state — binds connection_id + user_id to an HMAC-signed JWT. The OAuth
# state parameter is fully client-controlled, so we never trust values returned
# by the callback until we've verified the signature. This prevents:
#   - Tampering with connection_id to store tokens against a different connection
#   - Tampering with user_id to impersonate a different user in the callback
#   - Replaying an expired state
# ---------------------------------------------------------------------------

_STATE_AUDIENCE = "conn_oauth"
_STATE_TTL_SECONDS = 600  # 10 minutes — generous for slow OAuth providers


def _encode_state(connection_id: str, user_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "cid": connection_id,
        "uid": user_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=_STATE_TTL_SECONDS)).timestamp()),
        "aud": _STATE_AUDIENCE,
        "nonce": secrets.token_urlsafe(16),
    }
    return pyjwt.encode(payload, SECRET, algorithm="HS256")


def _decode_state(state: str) -> dict:
    """Raises HTTPException on any problem."""
    try:
        payload = pyjwt.decode(
            state,
            SECRET,
            algorithms=["HS256"],
            audience=_STATE_AUDIENCE,
            options={"require": ["cid", "uid", "exp", "aud"]},
        )
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(status_code=400, detail="OAuth state expired")
    except pyjwt.InvalidTokenError as e:
        raise HTTPException(status_code=400, detail=f"Invalid OAuth state: {e}")
    return payload


# ---------------------------------------------------------------------------
# Cookie helpers — only the PKCE code_verifier needs a cookie (it must survive
# the browser redirect). It's a per-flow secret known only to this session.
# ---------------------------------------------------------------------------


def _cookie_secure() -> bool:
    base_url = (settings.bow_config.base_url or "").lower()
    return base_url.startswith("https://")


def _set_verifier_cookie(response, code_verifier: str) -> None:
    response.set_cookie(
        key="conn_oauth_verifier",
        value=code_verifier,
        max_age=_STATE_TTL_SECONDS,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/api/connections",
    )


def _clear_verifier_cookie(response) -> None:
    response.delete_cookie(key="conn_oauth_verifier", path="/api/connections")


def _error_redirect(frontend_url: str, message: str) -> RedirectResponse:
    """Build a safe redirect back to the frontend with URL-encoded error details."""
    query = urlencode({"oauth": "error", "message": message or "oauth_failed"})
    response = RedirectResponse(url=f"{frontend_url}/data?{query}")
    _clear_verifier_cookie(response)
    return response


async def _ensure_connection_access(
    db: AsyncSession, user: User, connection: Connection
) -> None:
    """Raise 403 if the user doesn't have access to this connection."""
    from app.routes.connection import _user_can_access_connection, _is_org_admin
    org = connection.organization
    is_admin = await _is_org_admin(db, user, org) if org else False
    if is_admin:
        return
    if await _user_can_access_connection(db, user, connection):
        return
    raise HTTPException(status_code=403, detail="Access denied to this connection")


def _ensure_oauth_policy(connection: Connection) -> None:
    """Raise 400 if the connection is not configured for user OAuth sign-in."""
    if connection.auth_policy != "user_required":
        raise HTTPException(
            status_code=400,
            detail="This connection does not require per-user authentication",
        )
    allowed = connection.allowed_user_auth_modes or []
    if "oauth" not in allowed:
        raise HTTPException(
            status_code=400,
            detail="OAuth sign-in is not enabled for this connection",
        )


# ---------------------------------------------------------------------------
# Authorize
# ---------------------------------------------------------------------------

@router.get("/{connection_id}/oauth/authorize")
async def oauth_authorize(
    connection_id: str,
    request: Request,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
):
    """Start OAuth flow for a connection. Returns {authorization_url}."""
    # Load connection
    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
        .where(Connection.id == connection_id)
    )
    connection = result.scalars().first()
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Enforce: user can access this connection AND connection is configured for OAuth
    await _ensure_connection_access(db, user, connection)
    _ensure_oauth_policy(connection)

    # Get OAuth params for this connection type
    try:
        oauth_params = get_oauth_params(connection)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # PKCE verifier stays in a cookie (it's per-session, not security-sensitive to tamper).
    code_verifier, code_challenge = generate_pkce_pair()

    # State is a signed JWT binding connection_id + user_id. This is what prevents a
    # callback from being associated with the wrong connection or user.
    state = _encode_state(connection_id=str(connection.id), user_id=str(user.id))

    redirect_uri = f"{settings.bow_config.base_url}/api/connections/oauth/callback"

    # Build authorization URL
    params = {
        "response_type": "code",
        "client_id": oauth_params["client_id"],
        "redirect_uri": redirect_uri,
        "scope": oauth_params["scopes"],
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",  # Google-specific, ignored by others
    }
    authorization_url = f"{oauth_params['authorize_url']}?{urlencode(params)}"

    response = JSONResponse({"authorization_url": authorization_url})
    _set_verifier_cookie(response, code_verifier)
    return response


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------

@router.get("/oauth/callback")
async def oauth_callback(
    request: Request,
    code: str = None,
    state: str = None,
    error: str = None,
    error_description: str = None,
    db: AsyncSession = Depends(get_async_db),
):
    """Handle OAuth callback — exchange code for tokens and store credentials.

    Does NOT use Depends(current_user) because this is a cross-site redirect
    from the OAuth provider; the user's JWT cookie may not be sent (SameSite).
    The user is identified via the HMAC-signed state parameter, and the
    connection is re-authorized before storing tokens.
    """
    frontend_url = settings.bow_config.base_url or ""

    if error:
        logger.error(f"OAuth callback error: {error} — {error_description}")
        return _error_redirect(frontend_url, error_description or error)

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    # Verify signed state (raises 400 on any problem).
    payload = _decode_state(state)
    connection_id = payload["cid"]
    user_id = payload["uid"]

    # Fail fast if PKCE cookie is missing — the code exchange will fail at the
    # provider without it, returning a confusing error instead.
    code_verifier = request.cookies.get("conn_oauth_verifier")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="Missing PKCE code verifier")

    # Resolve user from signed state, not from an unsigned cookie.
    user = (
        await db.execute(select(User).where(User.id == user_id))
    ).scalars().first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Load connection from signed state.
    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
        .where(Connection.id == connection_id)
    )
    connection = result.scalars().first()
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Re-authorize: even though state was signed by us, enforce policy + access
    # here as a defense-in-depth check (covers cases where a user's grants were
    # revoked between authorize and callback, or the connection was re-configured).
    try:
        await _ensure_connection_access(db, user, connection)
        _ensure_oauth_policy(connection)
    except HTTPException as e:
        logger.warning(
            f"OAuth callback rejected for user {user.id} connection {connection.id}: {e.detail}"
        )
        return _error_redirect(frontend_url, e.detail)

    # Get OAuth params and exchange code
    try:
        oauth_params = get_oauth_params(connection)
        redirect_uri = f"{settings.bow_config.base_url}/api/connections/oauth/callback"
        tokens = await exchange_code_for_tokens(
            oauth_params, code, redirect_uri, code_verifier
        )
    except ValueError as e:
        logger.error(f"OAuth token exchange failed: {e}")
        return _error_redirect(frontend_url, "Token exchange failed")

    # Upsert UserConnectionCredentials
    stmt = select(UserConnectionCredentials).where(
        UserConnectionCredentials.connection_id == connection_id,
        UserConnectionCredentials.user_id == str(user.id),
        UserConnectionCredentials.is_active == True,
    )
    existing = (await db.execute(stmt)).scalars().first()

    if existing:
        existing.auth_mode = "oauth"
        existing.encrypt_credentials(tokens)
        existing.expires_at = datetime.fromisoformat(tokens["expires_at"]) if tokens.get("expires_at") else None
        db.add(existing)
    else:
        row = UserConnectionCredentials(
            connection_id=connection_id,
            user_id=str(user.id),
            organization_id=str(connection.organization_id),
            auth_mode="oauth",
            is_active=True,
            is_primary=True,
            expires_at=datetime.fromisoformat(tokens["expires_at"]) if tokens.get("expires_at") else None,
        )
        row.encrypt_credentials(tokens)
        db.add(row)

    await db.commit()
    logger.info(f"OAuth credentials saved for user {user.id} on connection {connection_id}")

    # Test the connection with the new credentials
    try:
        from app.services.connection_service import ConnectionService
        conn_service = ConnectionService()
        test_result = await conn_service.test_user_connection(
            db=db,
            connection_id=connection_id,
            organization=connection.organization,
            current_user=user,
        )
        if not test_result.get("success"):
            error_msg = test_result.get("message", "Connection test failed")
            logger.warning(f"OAuth connection test failed for user {user.id}: {error_msg}")
            return _error_redirect(frontend_url, error_msg)
    except Exception as e:
        logger.warning(f"OAuth connection test failed: {e}")
        return _error_redirect(frontend_url, str(e))

    # Trigger overlay sync (best-effort)
    try:
        from app.services.data_source_service import DataSourceService
        ds_service = DataSourceService()
        for ds in (connection.data_sources or []):
            await ds_service.get_user_data_source_schema(db=db, data_source=ds, user=user)
    except Exception as e:
        logger.warning(f"Overlay sync after OAuth sign-in failed: {e}")

    # Redirect back to frontend
    response = RedirectResponse(
        url=f"{frontend_url}/data?oauth=success&connection_id={connection_id}"
    )
    _clear_verifier_cookie(response)
    return response
