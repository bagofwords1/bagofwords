"""OAuth 2.1 Authorization Server routes.

Provides the endpoints external apps use to obtain access tokens via the
OAuth 2.1 Authorization Code + PKCE flow. Currently the only protected
resource is the MCP endpoint; additional resources can be added without
changing the auth flow.

Two routers:
  - well_known_router: mounted at root for /.well-known/* metadata
  - router: mounted at /api for /api/oauth/* endpoints
"""

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request, HTTPException, Form
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.services.oauth_server_service import OAuthServerService
from app.settings.config import settings

logger = logging.getLogger(__name__)

# Scopes this authorization server can issue. Extend this list when adding a
# new protected resource; the well-known metadata handlers read from it.
SUPPORTED_SCOPES = ["mcp"]
DEFAULT_SCOPE = SUPPORTED_SCOPES[0]

# ── Well-known metadata (mounted at root, no /api prefix) ──────────

well_known_router = APIRouter(tags=["oauth-metadata"])


def _base_url(request: Request) -> str:
    """Derive the public base URL from config, X-Forwarded-* headers, or request."""
    from app.core.base_url import derive_base_url
    return derive_base_url(request)


@well_known_router.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata(request: Request):
    """RFC 9728 - Protected Resource Metadata."""
    base = _base_url(request)
    return JSONResponse({
        "resource": f"{base}/api/mcp",
        "authorization_servers": [base],
        "scopes_supported": list(SUPPORTED_SCOPES),
    })


@well_known_router.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata(request: Request):
    """RFC 8414 - Authorization Server Metadata."""
    base = _base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/api/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "scopes_supported": list(SUPPORTED_SCOPES),
    })


# ── OAuth API routes (mounted at /api) ─────────────────────────────

router = APIRouter(prefix="/oauth", tags=["oauth"])


@router.get("/authorize")
async def authorize_redirect(
    request: Request,
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    state: Optional[str] = None,
    scope: Optional[str] = None,
    code_challenge: Optional[str] = None,
    code_challenge_method: Optional[str] = None,
):
    """OAuth authorize endpoint.

    Redirects to the Vue frontend consent page, preserving all query params.
    The user logs in there, approves, and the frontend calls POST /api/oauth/authorize.
    """
    if response_type != "code":
        return JSONResponse(
            {"error": "unsupported_response_type"},
            status_code=400,
        )

    # Build redirect to frontend consent page
    base = _base_url(request)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": response_type,
        "scope": scope or DEFAULT_SCOPE,
    }
    if state:
        params["state"] = state
    if code_challenge:
        params["code_challenge"] = code_challenge
    if code_challenge_method:
        params["code_challenge_method"] = code_challenge_method

    consent_url = f"{base}/authorize?{urlencode(params)}"
    return RedirectResponse(url=consent_url, status_code=302)


@router.post("/authorize")
async def authorize_approve(
    request: Request,
    user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Called by frontend after user approves the consent.

    Expects JSON body with: client_id, redirect_uri, state, scope, code_challenge, code_challenge_method.
    Returns the redirect URL with the authorization code.
    """
    body = await request.json()
    client_id = body.get("client_id")
    redirect_uri = body.get("redirect_uri")
    state = body.get("state")
    raw_scope = body.get("scope")
    code_challenge = body.get("code_challenge")
    code_challenge_method = body.get("code_challenge_method", "S256")

    if not client_id or not redirect_uri or not code_challenge:
        raise HTTPException(status_code=400, detail="Missing required parameters")

    if code_challenge_method != "S256":
        raise HTTPException(status_code=400, detail="Only S256 code_challenge_method is supported")

    service = OAuthServerService()

    # Validate client
    client = await service.validate_client(db, client_id)
    if not client:
        raise HTTPException(status_code=400, detail="Invalid client_id")

    # Validate redirect_uri
    if not service.validate_redirect_uri(client, redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    # Validate requested scope: must be a non-empty subset of both the server's
    # SUPPORTED_SCOPES and the client's registered scopes.
    scope_source = raw_scope if isinstance(raw_scope, str) and raw_scope.strip() else DEFAULT_SCOPE
    requested_scopes = scope_source.split()
    if not requested_scopes:
        raise HTTPException(status_code=400, detail="invalid_scope")
    client_scopes = set((client.scopes or "").split())
    for s in requested_scopes:
        if s not in SUPPORTED_SCOPES or s not in client_scopes:
            raise HTTPException(status_code=400, detail=f"invalid_scope: {s}")
    scope = " ".join(requested_scopes)

    # Create authorization code
    code = await service.create_authorization_code(
        db=db,
        client_id=client_id,
        user_id=user.id,
        organization_id=organization.id,
        redirect_uri=redirect_uri,
        scope=scope,
        code_challenge=code_challenge,
    )

    # Build callback URL
    callback = redirect_uri
    separator = "&" if "?" in callback else "?"
    callback += f"{separator}code={code}"
    if state:
        callback += f"&state={state}"

    return {"redirect_url": callback}


@router.post("/token")
async def token_endpoint(
    grant_type: str = Form(...),
    code: Optional[str] = Form(None),
    redirect_uri: Optional[str] = Form(None),
    client_id: str = Form(...),
    client_secret: Optional[str] = Form(None),
    code_verifier: Optional[str] = Form(None),
    refresh_token: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_async_db),
):
    """OAuth token endpoint.

    Supports grant_type=authorization_code and grant_type=refresh_token.
    """
    service = OAuthServerService()

    if grant_type == "authorization_code":
        if not code or not code_verifier or not redirect_uri:
            logger.warning("Token request missing params: code=%s code_verifier=%s redirect_uri=%s", bool(code), bool(code_verifier), bool(redirect_uri))
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Missing code, code_verifier, or redirect_uri"},
                status_code=400,
            )

        logger.info("Token exchange attempt: client_id=%s redirect_uri=%s", client_id, redirect_uri)
        result = await service.exchange_code(
            db=db,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        if not result:
            logger.warning("Token exchange failed: client_id=%s redirect_uri=%s", client_id, redirect_uri)
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid or expired authorization code"},
                status_code=400,
            )
        logger.info("Token exchange succeeded: client_id=%s", client_id)
        return JSONResponse(result)

    elif grant_type == "refresh_token":
        if not refresh_token:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "Missing refresh_token"},
                status_code=400,
            )

        logger.info("Refresh token attempt: client_id=%s", client_id)
        result = await service.refresh_access_token(
            db=db,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
        )
        if not result:
            logger.warning("Refresh token failed: client_id=%s", client_id)
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid or expired refresh token"},
                status_code=400,
            )
        logger.info("Refresh token succeeded: client_id=%s", client_id)
        return JSONResponse(result)

    else:
        return JSONResponse(
            {"error": "unsupported_grant_type"},
            status_code=400,
        )


# ── Client CRUD ────────────────────────────────────────────────────

@router.get("/clients")
@requires_permission("manage_settings")
async def list_clients(
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """List OAuth clients for the current organization."""
    service = OAuthServerService()
    return await service.list_clients(db, organization.id)


@router.post("/clients")
@requires_permission("manage_settings")
async def create_client(
    request: Request,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Create an OAuth client for the current organization."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    raw_scopes = body.get("scopes")
    scopes_source = raw_scopes if isinstance(raw_scopes, str) and raw_scopes.strip() else DEFAULT_SCOPE
    requested = scopes_source.split()
    if not requested:
        raise HTTPException(status_code=400, detail="scopes must include at least one supported scope")
    for s in requested:
        if s not in SUPPORTED_SCOPES:
            raise HTTPException(status_code=400, detail=f"Unsupported scope: {s}")
    scopes = " ".join(requested)

    redirect_uris = body.get("redirect_uris")
    if redirect_uris is not None:
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise HTTPException(status_code=400, detail="redirect_uris must be a non-empty list of strings")
        for uri in redirect_uris:
            if not isinstance(uri, str) or not uri.strip():
                raise HTTPException(status_code=400, detail="redirect_uris must be a non-empty list of strings")

    service = OAuthServerService()
    return await service.create_client(
        db=db,
        organization_id=organization.id,
        name=name,
        scopes=scopes,
        redirect_uris=redirect_uris,
    )


@router.get("/clients/{client_id}/info")
async def get_client_info(
    client_id: str,
    db: AsyncSession = Depends(get_async_db),
):
    """Public endpoint: get client name for consent screen."""
    service = OAuthServerService()
    info = await service.get_client_info(db, client_id)
    if not info:
        raise HTTPException(status_code=404, detail="Client not found")
    return info


@router.delete("/clients/{client_db_id}")
@requires_permission("manage_settings")
async def delete_client(
    client_db_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Delete an OAuth client."""
    service = OAuthServerService()
    deleted = await service.delete_client(db, client_db_id, organization.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"ok": True}


@router.post("/clients/{client_db_id}/rotate")
@requires_permission("manage_settings")
async def rotate_client_secret(
    client_db_id: str,
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    """Rotate client secret. Returns the new secret (shown only once)."""
    service = OAuthServerService()
    result = await service.rotate_client_secret(db, client_db_id, organization.id)
    if not result:
        raise HTTPException(status_code=404, detail="Client not found")
    return result
