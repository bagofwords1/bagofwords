"""
Connection OAuth Routes.

Handles the OAuth authorization code flow for per-user data source authentication.
Two endpoints:
  GET /connections/{connection_id}/oauth/authorize  — start the flow
  GET /connections/oauth/callback                   — handle the redirect
"""
import uuid
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.dependencies import get_async_db
from app.models.user import User
from app.models.connection import Connection
from app.models.user_connection_credentials import UserConnectionCredentials
from app.core.auth import current_user
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
# Cookie helpers (connection-oauth specific, scoped to /api/connections path)
# ---------------------------------------------------------------------------

def _cookie_secure() -> bool:
    base_url = (settings.bow_config.base_url or "").lower()
    return base_url.startswith("https://")


def _set_oauth_cookies(response, state: str, code_verifier: str, user_id: str):
    cookie_kwargs = dict(
        max_age=300,
        httponly=True,
        secure=_cookie_secure(),
        samesite="lax",
        path="/api/connections",
    )
    response.set_cookie(key="conn_oauth_state", value=state, **cookie_kwargs)
    response.set_cookie(key="conn_oauth_verifier", value=code_verifier, **cookie_kwargs)
    response.set_cookie(key="conn_oauth_user", value=user_id, **cookie_kwargs)


def _clear_oauth_cookies(response):
    for name in ("conn_oauth_state", "conn_oauth_verifier", "conn_oauth_user"):
        response.delete_cookie(key=name, path="/api/connections")


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

    # Check user has access
    from app.routes.connection import _user_can_access_connection, _is_org_admin
    org = connection.organization
    is_admin = await _is_org_admin(db, user, org) if org else False
    if not is_admin and not await _user_can_access_connection(db, user, connection):
        raise HTTPException(status_code=403, detail="Access denied")

    # Get OAuth params for this connection type
    try:
        oauth_params = get_oauth_params(connection)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Generate PKCE pair
    code_verifier, code_challenge = generate_pkce_pair()

    # State encodes connection_id for the callback
    state = f"{connection_id}:{uuid.uuid4().hex}"

    # Build redirect URI
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
    _set_oauth_cookies(response, state, code_verifier, str(user.id))
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
    from the OAuth provider and the JWT cookie may not be sent (SameSite).
    Instead, the user is identified via the conn_oauth_user cookie set during authorize.
    """
    frontend_url = settings.bow_config.base_url or ""

    if error:
        logger.error(f"OAuth callback error: {error} — {error_description}")
        return RedirectResponse(
            url=f"{frontend_url}/data?oauth=error&message={error_description or error}"
        )

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state")

    # Validate state cookie
    stored_state = request.cookies.get("conn_oauth_state")
    if not stored_state or stored_state != state:
        raise HTTPException(status_code=400, detail="Invalid OAuth state")

    code_verifier = request.cookies.get("conn_oauth_verifier")

    # Resolve user from cookie (set during authorize step)
    user_id = request.cookies.get("conn_oauth_user")
    if not user_id:
        raise HTTPException(status_code=401, detail="OAuth session expired")
    user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    # Extract connection_id from state
    try:
        connection_id = state.split(":")[0]
    except (IndexError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid state format")

    # Load connection
    result = await db.execute(
        select(Connection)
        .options(selectinload(Connection.organization), selectinload(Connection.data_sources))
        .where(Connection.id == connection_id)
    )
    connection = result.scalars().first()
    if not connection:
        raise HTTPException(status_code=404, detail="Connection not found")

    # Get OAuth params and exchange code
    try:
        oauth_params = get_oauth_params(connection)
        redirect_uri = f"{settings.bow_config.base_url}/api/connections/oauth/callback"
        tokens = await exchange_code_for_tokens(
            oauth_params, code, redirect_uri, code_verifier
        )
    except ValueError as e:
        logger.error(f"OAuth token exchange failed: {e}")
        return RedirectResponse(
            url=f"{frontend_url}/data?oauth=error&message=Token+exchange+failed"
        )

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
    _clear_oauth_cookies(response)
    return response
